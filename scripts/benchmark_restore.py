#!/usr/bin/env python3
"""Run isolated archive API checks against the two frozen native fixtures.

No installed configuration or existing library is read or changed. Each first
call uses a new process and a new library. Diagnostic runs add observation
overhead and are deliberately excluded from the uninstrumented first-call
summary. A local download simulation is not evidence of Google Drive storage.
"""

import argparse
import contextlib
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import socket
import subprocess
import sys
import time
import traceback
from unittest.mock import patch
import zipfile


FIXTURE_SHA256 = {
    "F06": "3470e1d590dc1b75e38fe2dc8e0dc709f7013ae240da0842bc11241be6d71a7e",
    "S05": "1466225d47ad3a83f04d9b3f485b8465a9965fe5cb00299c0d5bc420d691b70a",
}
CHUNK = 1024 * 1024
INJECTED_ERROR = "benchmark_restore: injected write_index failure"


def utc():
    return datetime.now(timezone.utc).isoformat()


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def write(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def error_data(error):
    value = {"type": type(error).__name__, "message": str(error),
             "traceback": "".join(traceback.format_exception(type(error), error, error.__traceback__))}
    if hasattr(error, "as_dict"):
        value["structured"] = error.as_dict()
    for key in ("errno", "winerror", "filename", "filename2"):
        field = getattr(error, key, None)
        if field is not None:
            value[key] = str(field) if key.startswith("filename") else field
    return value


def select_code(scripts_root):
    root = Path(scripts_root).resolve()
    if not (root / "codex_learner/archives.py").is_file():
        raise ValueError("--scripts-root must contain the isolated codex_learner package")
    if any(name == "codex_learner" or name.startswith("codex_learner.") for name in sys.modules):
        raise RuntimeError("Select the product before importing codex_learner")
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(root))
    return root


def module_identity(scripts_root):
    root = Path(scripts_root).resolve()
    expected = {path.relative_to(root).as_posix(): sha(path)
                for path in sorted((root / "codex_learner").glob("*.py"))}
    actual = {}
    for name, module in sorted(sys.modules.items()):
        if name == "codex_learner" or name.startswith("codex_learner."):
            path = Path(module.__file__).resolve()
            if not path.is_relative_to(root):
                raise AssertionError("Imported product module is outside the selected installation")
            actual[name] = {"path": str(path), "sha256": sha(path)}
    return {"scripts_root": str(root), "source_files_sha256": expected,
            "actual_product_modules": actual,
            "probe_helper_sha256": sha(Path(__file__).with_name("restore_probe.py")),
            "driver_sha256": sha(Path(__file__))}


def snapshot(library, course_id, case, name):
    """Best-effort post-call material is distinct from the pre-rollback probe."""
    folder = case / "states" / name
    folder.mkdir(parents=True)
    files, errors = {}, []
    try:
        paths = sorted(library.root.rglob("*"))
    except OSError as error:
        paths = []
        errors.append(error_data(error))
    for path in paths:
        try:
            if path.is_symlink():
                files[path.relative_to(library.root).as_posix()] = {"symbolic_link": True}
            elif path.is_file():
                info = path.stat()
                files[path.relative_to(library.root).as_posix()] = {
                    "bytes": info.st_size, "sha256": sha(path),
                    "file_attributes": getattr(info, "st_file_attributes", None)}
        except OSError as error:
            errors.append({"path": str(path), "error": error_data(error)})
    for relative in ("library.json", ".archive-index.json", course_id + "/course.json",
                     course_id + "/course.previous.json"):
        source = library.root / relative
        try:
            if source.is_file() and not source.is_symlink():
                shutil.copyfile(source, folder / source.name)
        except OSError as error:
            errors.append({"path": str(source), "error": error_data(error)})
    value = {"files": files, "observation_errors": errors,
             "stage_paths_after_call": [str(p) for p in sorted(case.glob(".learner-restore-*"))],
             "observation_scope": "Before/after the API call, not the instant of rename failure."}
    write(folder / "snapshot.json", value)
    return value


def seed_released(library, archive, expected, manifest, case):
    from codex_learner import archives

    directory = library.root / manifest["course_id"]
    directory.mkdir()
    # inspect_archive has already validated the closed manifest and each path.
    # Never use extractall or a preparation restore that could mask a first call.
    with zipfile.ZipFile(archive) as zipped:
        for relative, entry in manifest["files"].items():
            target = directory / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            with zipped.open("course/" + relative) as source, target.open("xb") as output:
                shutil.copyfileobj(source, output, CHUNK)
            if target.stat().st_size != entry["bytes"] or sha(target) != entry["sha256"]:
                raise AssertionError("Synthetic preparation did not match the frozen archive")
    snapshot(library, manifest["course_id"], case, "active-fixture")
    downloaded = case / "simulated-download.zip"
    shutil.copyfile(archive, downloaded)
    verification = archives.confirm_download(downloaded, expected, source_declaration="web_download",
                                              archive_path=archive)
    write(case / "local-download-simulation.json",
          {"simulation": True, "real_web_download": False, "receipt": verification})
    receipt = archives.release_course(library, manifest["course_id"], archive, verification, confirmed=True)
    write(case / "release-receipt.json", receipt)
    snapshot(library, manifest["course_id"], case, "released-fixture")
    if len(archives.archived_courses(library)) != 1:
        raise AssertionError("Synthetic release did not retain its archive guard")


def offline_verify(library, manifest, case):
    from codex_learner import archives
    from codex_learner.blobs import materialize, verify_artifacts
    from codex_learner.common import read_json

    course_id = manifest["course_id"]
    directory = library.root / course_id
    comparisons, expanded, references, sources = [], {}, {}, []
    with patch.object(socket.socket, "connect", side_effect=AssertionError("Offline verification attempted a connection")):
        for relative, entry in manifest["files"].items():
            path = directory / relative
            actual = {"path": relative, "bytes": path.stat().st_size, "sha256": sha(path)}
            actual["match"] = actual["bytes"] == entry["bytes"] and actual["sha256"] == entry["sha256"]
            comparisons.append(actual)
            if not actual["match"]:
                write(case / "offline-file-mismatch.json", actual)
                raise AssertionError("Restored file does not match its closed manifest: " + relative)
            snapshot_file = (relative in ("course.json", "course.previous.json")
                             or relative.endswith(".json") and
                             (relative.startswith("migration-backups/") or relative.startswith("before-restore-")))
            if snapshot_file and not entry.get("opaque_preserved_original"):
                value = read_json(path)
                references[relative] = verify_artifacts(directory, value)
                expanded[relative] = materialize(value, directory)
        loaded = library.load(course_id)
        if materialize(loaded, directory) != expanded["course.json"]:
            raise AssertionError("Library readback differs from the verified current manifest")
        for source_id, source in loaded["sources"].items():
            path = directory / source["path"]
            sources.append({"source_id": source_id, "path": source["path"],
                            "bytes": path.stat().st_size, "sha256": sha(path)})
        if archives.archived_courses(library):
            raise AssertionError("Restored course is still present in the archive index")
    write(case / "offline-expanded-snapshots.json", expanded)
    value = {"status": "PASS", "files": comparisons, "manifest_files": len(comparisons),
             "transitive_reference_counts": references, "source_reads": sources,
             "socket_connect_rejected_during_readback": True,
             "network_scope": "Local API readback only; not full-system isolation or Drive verification."}
    write(case / "offline-verification.json", value)
    return {key: value[key] for key in ("status", "manifest_files", "transitive_reference_counts")}


def run_case(case, scripts_root, input_archive, expected, group, *, diagnostic=False, collect_acl=False,
             worker_started_at_utc=None):
    """Internal helper; CLI callers always supply the fixed fixture SHA above."""
    started_at_utc = worker_started_at_utc or utc()
    from codex_learner import archives
    from codex_learner.storage import Library

    case = Path(case).resolve()
    input_archive = Path(input_archive).resolve()
    if case.exists():
        raise FileExistsError("Sample directory already exists; it will not be reused")
    if sha(input_archive) != expected:
        raise ValueError("Input archive SHA-256 does not match the frozen fixture")
    case.mkdir()
    result = {"schema_version": 1, "status": "RUNNING", "group": group,
              "started_at_utc": started_at_utc, "pid": os.getpid(), "python": sys.executable,
              "worker_time_scope": "Driver entry through final snapshot; final result write and process exit are bounded by supervisor timestamps.",
              "python_version": sys.version, "cwd": os.getcwd(), "case": str(case),
              "diagnostic": diagnostic, "first_call_acceptance_eligible": not diagnostic and group != "fault-chain",
              "automatic_retry": False, "preparation_restore_calls": 0,
              "archive_sha256": expected, "restore_calls": [],
              "scope": "Isolated installed Python API; not CLI, native panel, or real Drive."}
    archive = case / "input.zip"
    library, course_id = None, None
    try:
        result["identity_before"] = module_identity(scripts_root)
        shutil.copyfile(input_archive, archive)
        checked = archives.inspect_archive(archive, expected)
        manifest = checked["manifest"]
        course_id = manifest["course_id"]
        write(case / "archive-manifest.json", manifest)
        library = Library(case / "library")
        library.initialize()
        write(case / "config-record.json", {"library_root": str(library.root),
                                            "scope": "API path record; no CLI config parser invoked"})
        if group in ("released", "fault-chain"):
            seed_released(library, archive, expected, manifest, case)

        def call(tag, injected_context=None):
            snapshot(library, course_id, case, tag + "-before")
            row = {"tag": tag, "started_at_utc": utc(), "pid": os.getpid(),
                   "probe_enabled": diagnostic, "injection_enabled": injected_context is not None}
            probe = None
            if diagnostic:
                from restore_probe import RestoreFailureProbe
                probe = RestoreFailureProbe(library.root, course_id, case / (tag + "-probe"),
                                            collect_acl=collect_acl)
            start = time.perf_counter()
            try:
                with contextlib.ExitStack() as stack:
                    if injected_context is not None:
                        stack.enter_context(injected_context)
                    if probe is not None:
                        stack.enter_context(probe)
                    row["receipt"] = archives.restore_archive(library, archive, expected, confirmed=True)
                row["status"] = "SUCCESS"
            except Exception as error:
                row.update(status="ERROR", error=error_data(error))
            row.update(completed_at_utc=utc(), elapsed_seconds=time.perf_counter() - start,
                       elapsed_scope="API plus optional probe/injection, excluding before/after snapshots")
            if probe is not None:
                try:
                    row["probe_report"] = probe.report()
                except Exception as error:
                    row["probe_report_error"] = error_data(error)
            result["restore_calls"].append(row)
            write(case / (tag + "-call.json"), row)
            snapshot(library, course_id, case, tag + "-after")
            return row

        if group == "fault-chain":
            result["normal_after_injection_status"] = "BLOCKED"
            guard_before = archives.archived_courses(library)
            hits = []

            def fail_index(*args, **kwargs):
                hits.append({"at_utc": utc(), "pid": os.getpid(), "library": str(library.root)})
                raise OSError(INJECTED_ERROR)

            fault = call("injected-index", patch.object(archives, "_write_index", side_effect=fail_index))
            result["index_injection_hits"] = hits
            details = fault.get("error", {}).get("structured", {})
            if not (len(hits) == 1 and fault["status"] == "ERROR"
                    and details.get("error") == "restore_failed" and details.get("phase") == "write_index"
                    and details.get("original_error", {}).get("message") == INJECTED_ERROR
                    and details.get("rollback", {}).get("status") == "completed"):
                raise AssertionError("The expected write_index fault was not reached and rolled back; downstream chain is BLOCKED")
            guard_after = archives.archived_courses(library)
            result["archive_guard_unchanged_after_injection"] = guard_after == guard_before
            if (len(guard_after) != 1 or guard_after != guard_before
                    or (library.root / course_id / "course.json").exists()):
                raise AssertionError("Injected failure changed course enablement or removed its archive guard")
            row = call("normal-after-injection")
            result["normal_after_injection_status"] = "PENDING_READBACK" if row["status"] == "SUCCESS" else "FAIL"
        else:
            row = call("primary-first")
        if row["status"] != "SUCCESS":
            raise AssertionError("Restore failed; the first failure is preserved and is not retried")
        result["offline_verification"] = offline_verify(library, manifest, case)
        if group == "fault-chain":
            result["normal_after_injection_status"] = "PASS"
        result["status"] = "PASS"
    except Exception as error:
        result.update(status="FAIL", terminal_error=error_data(error))
        if result.get("normal_after_injection_status") == "PENDING_READBACK":
            result["normal_after_injection_status"] = "FAIL"
    finally:
        try:
            result["identity_after"] = module_identity(scripts_root)
            if ("identity_before" in result and result["identity_before"]["source_files_sha256"]
                    != result["identity_after"]["source_files_sha256"]):
                raise AssertionError("Product source files changed during the sample")
        except Exception as error:
            result["identity_error"] = error_data(error)
            result["status"] = "FAIL"
        if library is not None and course_id is not None:
            try:
                snapshot(library, course_id, case, "final")
            except Exception as error:
                result["final_snapshot_error"] = error_data(error)
                result["status"] = "FAIL"
        result["completed_at_utc"] = utc()
        write(case / "result.json", result)
    return result


def summarize(samples, *, mode, repeat):
    groups = {}
    for group in ("empty", "released") if mode != "fault-chain" else ("fault-chain",):
        rows = [row for row in samples if row["group"] == group]
        groups[group] = {"attempts": len(rows), "PASS": sum(row["status"] == "PASS" for row in rows),
                         "FAIL": sum(row["status"] != "PASS" for row in rows),
                         "primary_first_calls_reached": sum(row.get("primary_first_calls", 0) for row in rows)}
    planned = 1 if mode == "fault-chain" else 2 * repeat
    complete = len(samples) == planned
    passed = complete and all(row["status"] == "PASS" for row in samples)
    return {"complete": complete, "groups": groups,
            "status": "PASS" if passed else "FAIL", "diagnostics_only": mode == "diagnostic",
            "first_call_groups": groups if mode == "first" else {},
            "full_first_call_sample_count": (mode == "first" and repeat == 30 and complete
                                             and all(row["primary_first_calls_reached"] == 30
                                                     for row in groups.values())),
            "scope": "This version, machine, and run only; old failures remain unchanged."}


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--scripts-root", required=True, type=Path)
    result.add_argument("--f06-archive", required=True, type=Path)
    result.add_argument("--s05-archive", required=True, type=Path)
    result.add_argument("--output-dir", required=True, type=Path)
    result.add_argument("--mode", choices=("first", "fault-chain", "diagnostic"), default="first")
    result.add_argument("--repeat", type=int, default=30)
    result.add_argument("--timeout", type=float, default=180)
    result.add_argument("--collect-acl", action="store_true",
                        help="Diagnostic mode only: optional read-only ACL collection delays rollback")
    result.add_argument("--_worker-group", choices=("empty", "released", "fault-chain"), help=argparse.SUPPRESS)
    return result


def main(argv=None):
    started_at_utc = utc()
    args = parser().parse_args(argv)
    if args.repeat < 1 or args.timeout <= 0:
        raise ValueError("repeat and timeout must be positive")
    if args.collect_acl and args.mode != "diagnostic":
        raise ValueError("ACL observation is limited to explicitly instrumented diagnostic runs")
    scripts = args.scripts_root.resolve()
    inputs = {"F06": args.f06_archive.resolve(), "S05": args.s05_archive.resolve()}
    if not (scripts / "codex_learner/archives.py").is_file():
        raise ValueError("Isolated scripts root is missing")
    for label, path in inputs.items():
        if sha(path) != FIXTURE_SHA256[label]:
            raise ValueError(label + " archive does not match its fixed SHA-256")
    output = args.output_dir.resolve()
    if output.exists():
        raise FileExistsError("--output-dir must not exist; old evidence cannot be overwritten")
    if args._worker_group:
        select_code(scripts)
        label = "F06" if args._worker_group == "empty" else "S05"
        result = run_case(output, scripts, inputs[label], FIXTURE_SHA256[label], args._worker_group,
                          diagnostic=args.mode == "diagnostic", collect_acl=args.collect_acl,
                          worker_started_at_utc=started_at_utc)
        print(json.dumps({"case": str(output), "status": result["status"]}))
        return 0 if result["status"] == "PASS" else 1
    output.mkdir(parents=True)
    report = {"schema_version": 1, "started_at_utc": utc(), "mode": args.mode,
              "repeat_per_group": args.repeat if args.mode != "fault-chain" else 1,
              "python": sys.executable, "driver_sha256": sha(Path(__file__)),
              "platform": {"system": platform.system(), "release": platform.release(),
                           "machine": platform.machine()}, "system": platform.system(),
              "required_native_first_calls_per_group": 30,
              "scripts_root": str(scripts), "fixed_inputs": {label: {"path": str(path), "sha256": FIXTURE_SHA256[label]}
                                                             for label, path in inputs.items()},
              "automatic_retry": False, "scope": "Synthetic local API checks; no real Drive/native UI claim.",
              "samples": [], "complete": False}
    write(output / "results.json", report)
    cases = [("fault-chain", 1)] if args.mode == "fault-chain" else [
        (group, number) for group in ("empty", "released") for number in range(1, args.repeat + 1)]
    for group, number in cases:
        name = f"{group}-{number:02d}"
        case = output / name
        command = [sys.executable, str(Path(__file__).resolve()), "--scripts-root", str(scripts),
                   "--f06-archive", str(inputs["F06"]), "--s05-archive", str(inputs["S05"]),
                   "--output-dir", str(case), "--mode", args.mode, "--_worker-group", group]
        if args.collect_acl:
            command.append("--collect-acl")
        row = {"sample": name, "group": group, "case": str(case), "command": command,
               "started_at_utc": utc(), "fresh_subprocess": True, "diagnostic": args.mode == "diagnostic"}
        try:
            completed = subprocess.run(command, cwd=output, capture_output=True, timeout=args.timeout)
            (output / (name + ".stdout.txt")).write_bytes(completed.stdout)
            (output / (name + ".stderr.txt")).write_bytes(completed.stderr)
            row.update(returncode=completed.returncode, stdout=completed.stdout.decode("utf-8", errors="replace"),
                       stderr=completed.stderr.decode("utf-8", errors="replace"))
        except subprocess.TimeoutExpired as error:
            (output / (name + ".stdout.txt")).write_bytes(error.stdout or b"")
            (output / (name + ".stderr.txt")).write_bytes(error.stderr or b"")
            row.update(returncode=None, timeout=True, stdout=(error.stdout or b"").decode("utf-8", errors="replace"),
                       stderr=(error.stderr or b"").decode("utf-8", errors="replace"), error=error_data(error))
        except OSError as error:
            (output / (name + ".stdout.txt")).write_bytes(b"")
            (output / (name + ".stderr.txt")).write_bytes(b"")
            row.update(returncode=None, stdout="", stderr="", error=error_data(error))
        row["completed_at_utc"] = utc()
        try:
            value = json.loads((case / "result.json").read_text(encoding="utf-8"))
            row["worker_status"] = value["status"]
            row["worker_started_at_utc"] = value["started_at_utc"]
            row["worker_completed_at_utc"] = value["completed_at_utc"]
            row["worker_pid"] = value["pid"]
            row["restore_call_count"] = len(value.get("restore_calls", []))
            row["primary_first_calls"] = sum(call.get("tag") == "primary-first"
                                             for call in value.get("restore_calls", []))
            if group != "fault-chain" and value["status"] == "PASS" and row["primary_first_calls"] != 1:
                raise ValueError("A passing first-call sample must contain exactly one primary-first API call")
            row["status"] = "PASS" if row["returncode"] == 0 and value["status"] == "PASS" else "FAIL"
        except (OSError, ValueError, KeyError) as error:
            row.update(status="FAIL", worker_result_error=error_data(error))
        write(output / (name + "-command.json"), row)
        report["samples"].append(row)
        report.update(summarize(report["samples"], mode=args.mode, repeat=args.repeat))
        write(output / "results.json", report)
    report["completed_at_utc"] = utc()
    write(output / "results.json", report)
    print(json.dumps({"output": str(output), "status": report["status"], "groups": report["groups"]}))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError) as error:
        print(json.dumps({"status": "BLOCKED", "input_or_driver_error": error_data(error)}), file=sys.stderr)
        raise SystemExit(2)

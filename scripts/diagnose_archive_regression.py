#!/usr/bin/env python3
"""Observe the real interrupted-release regression in fresh persistent fixtures.

Only two packaged test methods are allowed. Every run is instrumented diagnostic
work, never a replacement for uninstrumented first-call acceptance. Setup/seed
precedes the probe; its Python-stream coverage begins at test-method entry.
The fixture is retained even after success. No personal configuration is read.
"""

import argparse
import contextlib
import importlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
import zipfile

from benchmark_restore import error_data, module_identity, offline_verify, select_code, sha, utc, write
from restore_probe import RestoreFailureProbe

ROOT = Path(__file__).resolve().parents[1]
CASES = {
    "partial": "test_release_failure_before_course_manifest_unlink_can_restore_matching_residue",
    "full": "test_release_failure_before_first_manifest_member_unlink_can_restore_matching_residue",
}
SCOPE = "Instrumented synthetic regression only; no first-call, performance, native UI or Drive acceptance."
NATIVE_ARCHIVE_SHA256 = "ede14f3b361e3f148dc63111598daf79151edce4662ed926061565b645494ea4"


def load_tests(scripts_root):
    # Bind the package before support.py adjusts sys.path for its test helpers.
    select_code(scripts_root)
    importlib.import_module("codex_learner")
    sys.path.insert(0, str(ROOT / "tests"))
    module = importlib.import_module("test_archives")
    for name, relative in (("test_archives", "tests/test_archives.py"),
                           ("support", "tests/support.py"), ("create_demo", "scripts/create_demo.py")):
        if Path(sys.modules[name].__file__).resolve() != (ROOT / relative).resolve():
            raise RuntimeError("Diagnostic helper was imported from an unexpected source: " + name)
    return module.ArchiveTests


def diagnostic_identity():
    return {name: sha(ROOT / name) for name in (
        "tests/test_archives.py", "tests/support.py", "scripts/create_demo.py",
        "scripts/diagnose_archive_regression.py", "scripts/restore_probe.py", "scripts/benchmark_restore.py")}


class PersistentDirectory:
    def __init__(self, path):
        path.mkdir()
        self.name = str(path)

    def cleanup(self):
        pass  # This test fixture is the diagnostic evidence, never auto-deleted.


class CapturedResult(unittest.TestResult):
    def __init__(self):
        super().__init__()
        self.records = []

    def capture(self, kind, error):
        row = {"kind": kind, "error": error_data(error[1]), "causes": []}
        cause, seen = error[1].__cause__, set()
        while cause is not None and id(cause) not in seen:
            seen.add(id(cause))
            row["causes"].append(error_data(cause))
            cause = cause.__cause__
        self.records.append(row)

    def addError(self, test, error):
        self.capture("ERROR", error)
        super().addError(test, error)

    def addFailure(self, test, error):
        self.capture("FAIL", error)
        super().addFailure(test, error)


def fixture_snapshot(fixture):
    files, errors = {}, []
    for path in sorted(fixture.rglob("*")):
        try:
            if path.is_symlink():
                files[path.relative_to(fixture).as_posix()] = {"symbolic_link": True}
            elif path.is_file():
                info = path.stat()
                files[path.relative_to(fixture).as_posix()] = {
                    "bytes": info.st_size, "sha256": sha(path),
                    "file_attributes": getattr(info, "st_file_attributes", None)}
        except OSError as error:
            errors.append({"path": str(path), "error": error_data(error)})
    return {"files": files, "errors": errors,
            "stage_paths_after_test": [str(p) for p in fixture.glob(".learner-restore-*")],
            "scope": "Before/after test observation; not the rename failure instant."}


def run_sample(output, scripts_root, case_name, test_class, *, collect_acl=False, archive=None):
    if case_name not in CASES:
        raise ValueError("Only the packaged partial/full interruption cases are allowed")
    if archive is not None and (case_name != "partial" or sha(archive) != NATIVE_ARCHIVE_SHA256):
        raise ValueError("Replay requires the fixed native-failure ZIP and the partial case")
    output = Path(output).resolve()
    output.mkdir()  # Exclusive: a previous sample can never be reused.
    method = CASES[case_name]
    original = getattr(test_class, method)
    fixture = output / "fixture"
    row = {"schema_version": 1, "case": case_name, "test_method": method,
           "status": "RUNNING", "diagnostic_only": True, "first_call_acceptance_eligible": False,
           "automatic_retry": False, "started_at_utc": utc(), "pid": os.getpid(),
           "python": sys.executable, "python_version": sys.version, "cwd": os.getcwd(),
           "fixture": str(fixture), "retain_fixture_policy": True, "scope": SCOPE,
           "fixture_origin": "verified_native_failure_archive" if archive else "test_created_archive",
           "archive_creation_expected": archive is None,
           "archive_creation_attempted": False, "archive_creation_call_completed": False,
           "timing_limit": "Current regression assertions read/hash residue files before restore; the probe also changes timing. This preserves the business sequence, not the original failing run's timing.",
           "errors": [], "probe_coverage": "Setup/seed precedes the probe; earlier/native/external handles are not covered."}
    probe = None

    def method_with_probe(test):
        nonlocal probe
        write(output / "before-test.json", fixture_snapshot(fixture))
        probe = RestoreFailureProbe(test.library.root, "sample-course", output / "probe",
                                    collect_acl=collect_acl)
        with probe:
            original(test)  # Preserve the exact test's create/release/guard/restore flow.

    overrides = {
        "make_test_directory": lambda self: PersistentDirectory(fixture),
        method: method_with_probe,
    }
    if archive is None:
        def create_archive(test, *args, **kwargs):
            row["archive_creation_attempted"] = True
            result = test_class.archive(test, *args, **kwargs)
            row["archive_creation_call_completed"] = True
            return result
        overrides["archive"] = create_archive
    if archive is not None:
        def replay_seed(test):
            from codex_learner import archives
            checked = archives.inspect_archive(archive, NATIVE_ARCHIVE_SHA256)
            manifest = checked["manifest"]
            if manifest["course_id"] != "sample-course":
                raise ValueError("The fixed fixture must retain its original course ID")
            directory = test.library.root / manifest["course_id"]
            directory.mkdir()
            with zipfile.ZipFile(archive) as zipped:
                for relative, expected in manifest["files"].items():
                    target = directory / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with zipped.open("course/" + relative) as source, target.open("xb") as stream:
                        shutil.copyfileobj(source, stream, 1024 * 1024)
                    if target.stat().st_size != expected["bytes"] or sha(target) != expected["sha256"]:
                        raise ValueError("Native fixture changed during replay preparation")
            local = fixture / "archives/course.zip"
            local.parent.mkdir()
            shutil.copyfile(archive, local)
            if sha(local) != NATIVE_ARCHIVE_SHA256:
                raise ValueError("Copied native fixture failed integrity verification")
            test.replayed_archive = {"path": str(local), "sha256": NATIVE_ARCHIVE_SHA256,
                                     "state_revision": manifest["state_revision"]}
        overrides.update(seed_test_library=replay_seed,
                         archive=lambda self: dict(self.replayed_archive))
    persistent_case = type("PersistentArchiveDiagnostic", (test_class,), overrides)
    test = persistent_case(method)
    captured = CapturedResult()
    write(output / "started.json", row)
    try:
        row["identity_before"] = module_identity(scripts_root)
        row["diagnostic_source_before"] = diagnostic_identity()
        row["diagnostic_driver_sha256"] = sha(Path(__file__))
        with (output / "test.stdout.txt").open("w", encoding="utf-8", newline="\n") as out, \
                (output / "test.stderr.txt").open("w", encoding="utf-8", newline="\n") as err, \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            test.run(captured)
        row.update(tests_run=captured.testsRun, errors=captured.records,
                   skips=[reason for _, reason in captured.skipped])
        row["status"] = "PASS" if captured.wasSuccessful() and captured.testsRun == 1 and not captured.skipped else "FAIL"
        if row["status"] == "PASS":
            from codex_learner import archives
            archive = fixture / "archives/course.zip"
            checked = archives.inspect_archive(archive)
            row["archive_sha256"] = sha(archive)
            write(output / "archive-manifest.json", checked["manifest"])
            row["offline_verification"] = offline_verify(test.library, checked["manifest"], output)
    except Exception as error:
        row["status"] = "FAIL"
        row["errors"].append({"kind": "DRIVER_OR_READBACK_ERROR", "error": error_data(error)})
    finally:
        if row["status"] == "RUNNING":
            row["status"] = "INTERRUPTED"
        try:
            row["probe"] = probe.report() if probe is not None else None
            row["publication_failure_events"] = len(row["probe"]["events"]) if row["probe"] else None
            row["event_count_complete"] = bool(row["probe"] is not None
                                                and not row["probe"]["observer_errors"]
                                                and not any(event.get("collection_errors") for event in row["probe"]["events"]))
            row["failure_observation"] = ("NOT_STARTED" if probe is None else
                                          "CAPTURED" if row["publication_failure_events"] else
                                          "NOT_CAPTURED" if row["event_count_complete"] else "UNKNOWN")
        except Exception as error:
            row.update(status="FAIL", publication_failure_events=None, event_count_complete=False,
                       failure_observation="UNKNOWN")
            row["errors"].append({"kind": "PROBE_REPORT_ERROR", "error": error_data(error)})
        row["root_cause"] = "UNCONFIRMED"
        try:
            fixture.lstat()
            row["fixture_preserved"] = True
        except FileNotFoundError:
            row["fixture_preserved"] = False
        except OSError as error:
            row["fixture_preserved"] = None
            row["fixture_inspection_error"] = error_data(error)
        try:
            row["identity_after"] = module_identity(scripts_root)
            if row.get("identity_before", {}).get("source_files_sha256") != row["identity_after"]["source_files_sha256"]:
                raise AssertionError("Product source identity changed during the sample")
            row["diagnostic_source_after"] = diagnostic_identity()
            if row.get("diagnostic_source_before") != row["diagnostic_source_after"]:
                raise AssertionError("Diagnostic source identity changed during the sample")
            write(output / "after-test.json", fixture_snapshot(fixture))
        except Exception as error:
            row["status"] = "FAIL"
            row["errors"].append({"kind": "FINAL_OBSERVATION_ERROR", "error": error_data(error)})
        row["completed_at_utc"] = utc()
        write(output / "result.json", row)
    return row


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scripts-root", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--case", choices=tuple(CASES), default="partial")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--collect-acl", action="store_true")
    parser.add_argument("--archive", type=Path, help="Optional fixed native-failure ZIP replay (partial case only; skips new archive creation)")
    parser.add_argument("--_worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if not 1 <= args.repeat <= 10 or args.timeout <= 0:
        parser.error("Use 1–10 explicitly planned samples and a positive timeout")
    scripts, output = args.scripts_root.resolve(), args.output_dir.resolve()
    if not (scripts / "codex_learner/archives.py").is_file():
        raise ValueError("--scripts-root must identify the isolated product package")
    if output.exists():
        raise FileExistsError("Choose a new output directory; previous evidence is never overwritten")
    if output.is_relative_to(scripts.parent):
        raise ValueError("Diagnostic output must be outside the selected installation")
    archive = args.archive.resolve() if args.archive else None
    if archive is not None and (args.case != "partial" or sha(archive) != NATIVE_ARCHIVE_SHA256):
        raise ValueError("Replay requires the fixed native-failure ZIP and the partial case")
    if args._worker:
        test_class = load_tests(scripts)
        row = run_sample(output, scripts, args.case, test_class, collect_acl=args.collect_acl, archive=archive)
        print(json.dumps({"status": row["status"], "case": args.case,
                          "failure_events": row["publication_failure_events"]}))
        return 0 if row["status"] == "PASS" else 1
    output.mkdir(parents=True)
    report = {"schema_version": 1, "started_at_utc": utc(), "case": args.case,
              "diagnostic_only": True, "first_call_acceptance_eligible": False,
              "automatic_retry": False, "scope": SCOPE, "samples": [], "complete": False}
    report["fixture_origin"] = "verified_native_failure_archive" if archive else "test_created_archive"
    write(output / "results.json", report)
    for number in range(1, args.repeat + 1):
        sample = output / f"sample-{number:02d}"
        command = [sys.executable, str(Path(__file__).resolve()), "--scripts-root", str(scripts),
                   "--output-dir", str(sample), "--case", args.case, "--_worker"]
        if args.collect_acl:
            command.append("--collect-acl")
        if archive is not None:
            command.extend(["--archive", str(archive)])
        row = {"command": command, "started_at_utc": utc(), "sample": str(sample)}
        write(output / f"sample-{number:02d}-started.json", row)
        try:
            done = subprocess.run(command, cwd=output, capture_output=True, timeout=args.timeout)
            row.update(returncode=done.returncode, stdout=done.stdout, stderr=done.stderr)
        except (subprocess.TimeoutExpired, OSError) as error:
            row.update(returncode=None, stdout=getattr(error, "stdout", None) or b"",
                       stderr=getattr(error, "stderr", None) or b"", error=error_data(error))
        row["completed_at_utc"] = utc()
        for stream in ("stdout", "stderr"):
            path = output / f"sample-{number:02d}.{stream}.txt"
            path.write_bytes(row.pop(stream))
            row[stream + "_path"] = str(path)
        try:
            worker = json.loads((sample / "result.json").read_text(encoding="utf-8"))
            row["worker_status"] = worker["status"]
            row["publication_failure_events"] = worker["publication_failure_events"]
            row["event_count_complete"] = worker["event_count_complete"]
            row["status"] = "PASS" if row["returncode"] == 0 and worker["status"] == "PASS" else "FAIL"
        except (OSError, ValueError, KeyError) as error:
            row.update(status="FAIL", worker_result_error=error_data(error))
        report["samples"].append(row)
        write(output / f"sample-{number:02d}-command.json", row)
        write(output / "results.json", report)
    report.update(complete=True, completed_at_utc=utc(),
                  status="PASS" if all(row["status"] == "PASS" for row in report["samples"]) else "FAIL")
    known = [row.get("publication_failure_events") for row in report["samples"]]
    report["publication_failure_events"] = sum(value for value in known if value is not None)
    report["event_count_complete"] = all(row.get("event_count_complete", False) for row in report["samples"])
    report["failure_observation"] = ("CAPTURED" if report["publication_failure_events"] else
                                     "NOT_CAPTURED" if report["event_count_complete"] else "UNKNOWN")
    report["root_cause"] = "UNCONFIRMED"
    write(output / "results.json", report)
    print(json.dumps({"status": report["status"], "failure_observation": report["failure_observation"], "scope": SCOPE}))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError) as error:
        print(json.dumps({"status": "BLOCKED", "error": error_data(error)}), file=sys.stderr)
        raise SystemExit(2)

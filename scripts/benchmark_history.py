#!/usr/bin/env python3
"""Stress synthetic multi-version history in an isolated temporary library.

The fixture uses real operation handlers followed by one compact/validate/verify
write, avoiding thousands of repeated transactions during fixture construction.
This is structural scale evidence, not observed long-term human usage.
"""

import argparse
import copy
import json
import hashlib
import math
import os
import shutil
import statistics
import re
import platform
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "skills/codex-learner/scripts"), str(ROOT / "scripts")]

from benchmark_memory import peak_memory, request


def create_fixture(root, records):
    from codex_learner.blobs import compact, file_sha256, materialize, verify_artifacts, iter_body_refs
    from codex_learner.common import digest, now, validate_state
    from codex_learner.operations import Engine
    from codex_learner.storage import Library, atomic_write_json
    from create_demo import seed

    library = Library(root)
    library.initialize()
    seed(library)
    engine = Engine(library)
    inputs = root.parent / "synthetic-inputs"
    inputs.mkdir()
    source_ids = ["sample-source-v1"]
    for index in range(1, 20):
        source_id = f"stress-source-{index:02d}"
        path = inputs / f"{source_id}.md"
        path.write_text(f"# Synthetic source {index}\nReference evidence for stress fixture {index}.\n", encoding="utf-8")
        engine.apply(request("import_source", "import-" + source_id, source_id=source_id, path=str(path)))
        source_ids.append(source_id)

    state = library.load("sample-course")
    directory = library.course_dir("sample-course")

    def bulk_apply(action, identifier, **fields):
        """Use business validation, but defer snapshot rotation to one bulk write."""
        command = request(action, identifier, **fields)
        state["state_revision"] += 1
        result = getattr(engine, "op_" + action)(state, directory, command, None)
        state["updated_at"] = now()
        state["operations"][identifier] = {
            "fingerprint": digest(command),
            "receipt": {"course_id": "sample-course", "operation_id": identifier,
                        "state_revision": state["state_revision"], "recorded_at": state["updated_at"],
                        "result": copy.deepcopy(result), "duplicate": False},
        }
        return command

    contexts = []
    for chapter_index, (chapter_id, question_id) in enumerate((("chapter-1", "q1"), ("chapter-2", "q3"))):
        chapter = state["chapters"][chapter_id]
        template = materialize(state["questions"][question_id]["versions"]["qv1"], directory)
        goals = copy.deepcopy(chapter["versions"]["v1"]["goals"])
        for number in range(1, 11):
            version, qversion = f"v{number}", f"qv{number}"
            refs = [{"source_id": source_ids[chapter_index * 10 + number - 1], "locator": "line:2"}]
            if number > 1:
                bulk_apply("publish_content", f"publish-{chapter_id}-{version}", chapter_id=chapter_id,
                           content_version=version, title=f"Synthetic {chapter_id} {version}", goals=goals,
                           parts=[{"part_id": "part-1", "text": f"Synthetic lecture {chapter_id}/{version}. " * 20,
                                   "source_refs": refs}])
                rubric = copy.deepcopy(template["rubrics"]["r1"])
                rubric["source_refs"] = refs
                bulk_apply("publish_question", f"publish-{question_id}-{qversion}", chapter_id=chapter_id,
                           content_version=version, question_id=question_id, question_version=qversion,
                           rubric_version="r1", prompt=f"Synthetic prompt {chapter_id}/{version}.",
                           checks=template["checks"], rubric=rubric)
            contexts.append({"chapter_id": chapter_id, "content_version": version,
                             "question_id": question_id, "question_version": qversion})

    retries = []
    counter = 0
    for group, context in enumerate(contexts):
        bulk_apply("adopt_content", f"adopt-{group:02d}", chapter_id=context["chapter_id"],
                   content_version=context["content_version"])
        question = state["questions"][context["question_id"]]["versions"][context["question_version"]]
        refs = question["rubrics"]["r1"]["source_refs"]
        count = records // len(contexts) + int(group < records % len(contexts))
        for _ in range(count):
            sid = f"stress-answer-{counter:05d}"
            command = bulk_apply("submit_answer", sid, **context, submission_id=sid,
                                 answer=f"Synthetic original answer {counter:05d}.\r\n" + "a" * 1000)
            if counter in (0, records - 1):
                retries.append(command)
            bulk_apply("assess", "assess-" + sid, submission_id=sid, assessment_id="assess-" + sid,
                       rubric_version="r1", feedback=f"Synthetic feedback {counter:05d}.\n" + "f" * 1000,
                       feedback_help="hint" if counter % 5 == 0 else "none",
                       results={check: {"outcome": "meets", "reason": "Synthetic source-backed grading reason.",
                                        "source_refs": refs} for check in question["checks"]})
            counter += 1

    state = compact(state, directory)
    validate_state(state, "sample-course")
    verify_artifacts(directory, state)
    atomic_write_json(directory / "course.json", state)
    atomic_write_json(directory / "course.previous.json", state)
    original_hash = file_sha256(directory / "course.json")
    for command in retries:
        receipt = engine.apply(command)
        if not receipt["duplicate"] or receipt["result"]["answer"] != command["answer"]:
            raise AssertionError("Synthetic receipt failed exact idempotent replay.")
    if file_sha256(directory / "course.json") != original_hash:
        raise AssertionError("Idempotent replay changed the fixture.")
    reachable = list(iter_body_refs(state, directory))
    return {
        "unique_reachable_artifacts": len(reachable),
        "unique_json_artifacts": sum(ref.get("format") == "json" for ref in reachable),
        "construction": "Synthetic bulk operation handlers, then compact, validate_state and verify_artifacts.",
        "retry_request": retries[0],
        "fixture_manifest_sha256": original_hash,
        "fixture_previous_sha256": file_sha256(directory / "course.previous.json"),
        "state_revision": state["state_revision"],
        "historical_submissions": len(state["submissions"]), "historical_assessments": len(state["assessments"]),
        "help_events": len(state["help_events"]), "sources": len(state["sources"]),
        "lecture_versions": sum(len(chapter["versions"]) for chapter in state["chapters"].values()),
        "question_versions": sum(len(question["versions"]) for question in state["questions"].values()),
        "submission_receipts": records, "total_operation_receipts": len(state["operations"]),
        "idempotent_replay_probes": len(retries), "manifest_bytes": (directory / "course.json").stat().st_size,
        "artifact_bytes": sum(path.stat().st_size for path in directory.rglob("*")
                              if path.is_file() and path.name not in ("course.json", "course.previous.json", ".lock")),
    }


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def select_code(root):
    """Select product source before importing any product module in this process."""
    root = Path(root).resolve()
    if any(name == "codex_learner" or name.startswith("codex_learner.") for name in sys.modules):
        raise RuntimeError("Select benchmark product source before importing codex_learner")
    sys.path[:0] = [str(root / "skills/codex-learner/scripts"), str(root / "scripts")]
    return root


def source_identity(root):
    root = Path(root).resolve()
    modules = root / "skills/codex-learner/scripts/codex_learner"
    version_text = (modules / "__init__.py").read_text(encoding="utf-8")
    version_match = re.search(r"^__version__\s*=\s*['\"]([^'\"]+)", version_text, re.M)
    build = root / "BUILD.json"
    build_info = json.loads(build.read_text(encoding="utf-8")) if build.is_file() else None
    commit = None
    if build_info is None:
        try:
            top = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=root, capture_output=True,
                                 text=True, check=True, timeout=10).stdout.strip()
            if Path(top).resolve() == root:
                commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True,
                                        text=True, check=True, timeout=10).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    return {
        "root": str(root), "source_commit": build_info.get("commit") if build_info is not None else commit,
        "identity_source": "BUILD.json" if build_info is not None else "git_root" if commit else "unversioned",
        "learner_version": version_match[1] if version_match else None,
        "module_sha256": {p.name: file_hash(p) for p in sorted(modules.glob("*.py"))},
        "dependency_lock_sha256": {name: file_hash(root / name) for name in
                                   ("requirements-base.lock", "requirements-ocr-py312.lock") if (root / name).is_file()},
        "build_metadata": build_info,
        "package_sha256": None,
        "package_identity_note": "No installed ZIP identity inferred from source files; bind this report to the independently verified package SHA when handing off.",
    }


def worker(operation, root, fixture, profile=False):
    from codex_learner.operations import Engine
    from codex_learner.storage import Library
    from codex_learner.views import course_history, course_summary
    from codex_learner import blobs, storage, common
    if profile:
        # The same probe implementation must measure both product revisions.
        import importlib.util
        spec = importlib.util.spec_from_file_location("learner_history_profile", ROOT / "scripts/history_profile.py")
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        OperationProfile = helper.OperationProfile

    library = Library(root)
    directory = library.course_dir("sample-course")
    probe = OperationProfile(root) if profile else None
    details = {}
    command = None
    if operation == "retry":
        command = fixture["retry_request"]
    elif operation == "submit":
        sid = "measured-submit"
        command = request("submit_answer", sid, chapter_id="chapter-1", content_version="v10",
                          question_id="q1", question_version="qv10", submission_id=sid,
                          answer="新的简短作答。\r\n末行保留  ")
    with probe if probe else nullcontext():
        operation_started_at_utc = datetime.now(timezone.utc).isoformat()
        started = time.monotonic()
        if operation == "list":
            result = library.list_courses()
            if len(result) != 1 or "error" in result[0]:
                raise AssertionError("The stress course was not readable.")
            count = len(result)
        elif operation == "review":
            result = course_summary(library.load("sample-course"))
            count = len(result["chapters"])
            if count != 2:
                raise AssertionError("Course overview lost a chapter.")
        elif operation in ("history_page", "history_page_with_bodies"):
            include_bodies = operation == "history_page_with_bodies"
            result = course_history(library.load("sample-course"), page=1, page_size=20, include_bodies=include_bodies)
            count = len(result["items"])
            if count != 20 or any(("answer" in item) != include_bodies for item in result["items"]):
                raise AssertionError("History page did not respect the page/body selection.")
            details = {"page": 1, "page_size": 20, "include_bodies": include_bodies,
                       "history_total": result["total"]}
        else:
            result = Engine(library).apply(command)
            count = int(result["result"]["submission_id"] == command["submission_id"])
            if count != 1 or result["duplicate"] != (operation == "retry"):
                raise AssertionError("Submission/retry receipt did not match the requested operation.")
            details["duplicate"] = result["duplicate"]
        serialized_at = time.monotonic()
        encoded_bytes = len(json.dumps(result, ensure_ascii=False).encode("utf-8"))
        serialization_seconds = time.monotonic() - serialized_at
        elapsed = time.monotonic() - started
        peak, method = peak_memory()
        operation_completed_at_utc = datetime.now(timezone.utc).isoformat()
    # Correctness checks deliberately follow timing AND the process peak capture.
    state = library.load("sample-course")
    after_hash = file_hash(directory / "course.json")
    previous_hash = file_hash(directory / "course.previous.json")
    increment = int(operation == "submit")
    if len(state["submissions"]) != fixture["historical_submissions"] + increment:
        raise AssertionError("Unexpected submission count after operation")
    if len(state["operations"]) != fixture["total_operation_receipts"] + increment:
        raise AssertionError("Unexpected operation count after operation")
    if state["state_revision"] != fixture["state_revision"] + increment:
        raise AssertionError("Unexpected revision after operation")
    if operation != "submit" and after_hash != fixture["fixture_manifest_sha256"]:
        raise AssertionError("Read/retry operation changed the manifest")
    if previous_hash != fixture["fixture_manifest_sha256"]:
        raise AssertionError("Previous valid snapshot was not preserved")
    if command is not None and result["result"]["answer"] != command["answer"]:
        raise AssertionError("Answer text did not round-trip exactly")
    actual_modules = {}
    for module in (blobs, storage, common):
        path = Path(module.__file__).resolve()
        actual_modules[module.__name__] = {"path": str(path), "sha256": file_hash(path)}
    return {"operation": operation, "peak_bytes": peak, "peak_mib": round(peak / (1024 * 1024), 3),
            "operation_started_at_utc": operation_started_at_utc,
            "operation_completed_at_utc": operation_completed_at_utc,
            "measurement": method, "elapsed_seconds": round(elapsed, 6), "result_count": count,
            "serialized_result_bytes": encoded_bytes, "serialization_seconds": serialization_seconds,
            "request_bytes": len(json.dumps(command, ensure_ascii=False).encode("utf-8")) if command else None,
            "fixture_manifest_sha256": fixture["fixture_manifest_sha256"],
            "actual_product_modules": actual_modules,
            "correctness": {"passed": True, "before_revision": fixture["state_revision"],
                            "after_revision": state["state_revision"],
                            "before_submissions": fixture["historical_submissions"],
                            "after_submissions": len(state["submissions"]),
                            "manifest_sha256_after": after_hash, "previous_sha256_after": previous_hash,
                            "answer_exact": command is not None},
            "profile": probe.report() if probe else None, **details}


def run_subprocess(arguments, timeout=600):
    """Never discard worker errors: callers persist this result before continuing."""
    started = time.monotonic()
    started_at_utc = datetime.now(timezone.utc).isoformat()
    try:
        run = subprocess.run(arguments, capture_output=True, text=True, encoding="utf-8", timeout=timeout)
        result = {"returncode": run.returncode, "stdout": run.stdout, "stderr": run.stderr,
                  "process_started_at_utc": started_at_utc,
                  "process_completed_at_utc": datetime.now(timezone.utc).isoformat(),
                  "process_wall_seconds": time.monotonic() - started}
    except (subprocess.TimeoutExpired, OSError) as exc:
        def text(value):
            return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value or ""
        return {"returncode": None, "stdout": text(getattr(exc, "stdout", "")),
                "stderr": text(getattr(exc, "stderr", "")), "error": type(exc).__name__ + ": " + str(exc),
                "process_started_at_utc": started_at_utc,
                "process_completed_at_utc": datetime.now(timezone.utc).isoformat(),
                "process_wall_seconds": time.monotonic() - started, "ok": False}
    try:
        result["result"] = json.loads(result["stdout"])
    except (ValueError, TypeError) as exc:
        result["error"] = "Invalid worker JSON: " + str(exc)
    result["ok"] = result["returncode"] == 0 and isinstance(result.get("result"), dict)
    return result


def summarize(samples, variants, operations):
    summaries = []
    for variant in variants:
        for operation in operations:
            rows = [s for s in samples if s["variant"] == variant and s["operation"] == operation
                    and not s["warmup"] and not s["profiled"] and not s.get("diagnostic", False)]
            good = [s["result"] for s in rows if s["ok"]]
            times = sorted(s["elapsed_seconds"] for s in good)
            summaries.append({
                "variant": variant, "operation": operation, "samples": len(rows),
                "successful_samples": len(good), "failures": len(rows) - len(good),
                "median_seconds": statistics.median(times) if times else None,
                "p95_seconds": times[math.ceil(0.95 * len(times)) - 1] if times else None,
                "p95_method": "nearest-rank: sorted[ceil(0.95*n)-1] of successful, non-warmup, unprofiled samples",
                "min_seconds": min(times) if times else None, "max_seconds": max(times) if times else None,
                "peak_bytes": max((s["peak_bytes"] for s in good), default=None),
            })
    return summaries


def save_report(path, report):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".pending")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def main():
    available = ("list", "review", "history_page", "history_page_with_bodies", "submit", "retry")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records", type=int, default=1000)
    parser.add_argument("--budget-mib", type=int, default=256)
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/history-benchmark.json")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--warmup", type=int, default=0)
    parser.add_argument("--operations", default=",".join(available[:-1]))
    profiling = parser.add_mutually_exclusive_group()
    profiling.add_argument("--profile", action="store_true", help="Run extra instrumented samples, excluded from latency summaries")
    profiling.add_argument("--profile-only", action="store_true", help="Only run one unprofiled control and one profiled diagnostic per operation/variant; no latency or memory acceptance")
    parser.add_argument("--baseline-root", type=Path, help="Read-only product source checkout; never a personal course library")
    parser.add_argument("--_build-fixture", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--_worker", choices=available, help=argparse.SUPPRESS)
    parser.add_argument("--_library", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--_fixture-info", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--_code-root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    parser.add_argument("--_profile-worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    operations = args.operations.split(",")
    if not 20 <= args.records <= 10000 or args.budget_mib < 1:
        parser.error("Use 20–10000 historical records and a positive memory budget.")
    if not 1 <= args.repeat <= 1000 or not 0 <= args.warmup <= 100:
        parser.error("Use 1–1000 repetitions and 0–100 warmup runs.")
    if len(set(operations)) != len(operations) or any(op not in available for op in operations):
        parser.error("Operations must be distinct choices from: " + ",".join(available))
    if args._build_fixture or args._worker:
        if not args._library:
            parser.error("Internal fixture and operation workers require an isolated library path.")
        timing = {"worker_started_at_utc": datetime.now(timezone.utc).isoformat(), "worker_pid": os.getpid()}
        try:
            code_root = select_code(args._code_root)
            if args._build_fixture:
                result = create_fixture(args._library, args.records)
            else:
                if not args._fixture_info:
                    parser.error("An operation worker requires synthetic fixture metadata.")
                fixture = json.loads(args._fixture_info.read_text(encoding="utf-8"))
                result = worker(args._worker, args._library, fixture, args._profile_worker)
                for value in result["actual_product_modules"].values():
                    if not Path(value["path"]).is_relative_to(code_root):
                        raise AssertionError("Worker imported product code outside selected source root")
        except BaseException:
            timing["worker_completed_at_utc"] = datetime.now(timezone.utc).isoformat()
            print(json.dumps({"failed_worker_timing": timing}), file=sys.stderr, flush=True)
            raise
        timing["worker_completed_at_utc"] = datetime.now(timezone.utc).isoformat()
        result.update(timing)
        print(json.dumps(result, ensure_ascii=False))
        return 0

    roots = {"candidate": ROOT}
    if args.baseline_root:
        if not (args.baseline_root / "skills/codex-learner/scripts/codex_learner/__init__.py").is_file():
            parser.error("--baseline-root must contain an unpacked Learner product source tree")
        roots = {"baseline": args.baseline_root.resolve(), **roots}
    identities = {name: source_identity(root) for name, root in roots.items()}
    report = {
        "schema_version": 2, "checked_at": datetime.now(timezone.utc).isoformat(),
        "display_timezone": "Asia/Shanghai", "platform": platform.platform(), "python": sys.version,
        "python_executable": sys.executable, "learner_version": identities["candidate"]["learner_version"],
        "target_budget_mib": args.budget_mib, "sources": identities,
        "benchmark_sha256": file_hash(Path(__file__)),
        "profile_helper_sha256": file_hash(ROOT / "scripts/history_profile.py"),
        "configuration": {"records": args.records, "repeat": 0 if args.profile_only else args.repeat,
                          "warmup": 0 if args.profile_only else args.warmup,
                          "operations": operations, "profile": args.profile or args.profile_only,
                          "profile_only": args.profile_only},
        "fixture": None, "operations": [], "samples": [], "summaries": [],
        "passed": None if args.profile_only else False,
        "diagnostics_only": args.profile_only, "diagnostics_passed": None,
        "complete": False, "fixture_construction_in_separate_process": True,
        "fixture_construction_in_daily_peak": False, "fixture_copy_in_worker_peak": False,
        "codex_client_measured": False, "google_drive_measured": False, "ocr_provider_measured": False,
        "cache_state": "OS filesystem cache uncontrolled; copying fixtures may warm it. Each operation uses a fresh process; this is not cold-disk testing.",
        "scope": "Independent Python process peak RSS/working set captured immediately after operation plus JSON serialization, before post-operation correctness checks. Process wall time includes startup and checks and is reported separately.",
        "write_integrity_cost": "Writes verify all reachable historical artifacts before backup rotation; latency depends on metadata and artifact bytes.",
        "limits": "Synthetic structure, not real human usage. No physical-disk I/O, full-history export, OCR, native Codex/Drive memory, or unbounded-history claim. Profiled diagnostics are separate from unprofiled latency distributions. In profile-only mode all samples, including the single unprofiled control per operation/variant, are diagnostic: passed is null and no latency/memory acceptance is evaluated. A control/profile difference is an observation, not a stable probe-overhead estimate. Low sample counts do not establish stable tail latency.",
    }
    failure_root = args.output.resolve().parent / (args.output.stem + "-failures-" + uuid.uuid4().hex[:8])

    def preserve_failure(path, evidence, label, fixture=None):
        destination = failure_root / label
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(destination))
        (destination / "benchmark-failure.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
        if fixture is not None:
            (destination / "benchmark-fixture-info.json").write_text(json.dumps(fixture, ensure_ascii=False, indent=2), encoding="utf-8")
        return str(destination)

    save_report(args.output, report)
    with tempfile.TemporaryDirectory(prefix="codex-learner-history-") as temporary:
        temporary = Path(temporary)
        fixture_root = temporary / "fixture" / "library"
        fixture_root.parent.mkdir()
        first_root = next(iter(roots.values()))
        build = run_subprocess([sys.executable, str(Path(__file__).resolve()), "--_code-root", str(first_root),
                                "--_library", str(fixture_root), "--_build-fixture", "--records", str(args.records)])
        report["fixture_build"] = build
        if not build["ok"]:
            report["fixture_failure_path"] = preserve_failure(fixture_root.parent, build, "fixture-build")
            report["complete"] = True
            if args.profile_only:
                report["diagnostics_passed"] = False
            save_report(args.output, report)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 1
        fixture = build["result"]
        report["fixture"] = fixture
        fixture_info = temporary / "fixture-info.json"
        fixture_info.write_text(json.dumps(fixture, ensure_ascii=False), encoding="utf-8")
        report["fixture_volume"] = {"temporary_root": str(temporary), "filesystem": "not_measured",
                                   "device": fixture_root.stat().st_dev}
        plan = ([(0, False, False), (1, False, True)] if args.profile_only else
                [(i, i < args.warmup, False) for i in range(args.warmup + args.repeat)])
        if args.profile:
            plan.append((0, False, True))
        for iteration, warmup, profiled in plan:
            # Alternate order between variants, and restore exactly the same snapshot every time.
            ordered = list(roots.items())
            if iteration % 2:
                ordered.reverse()
            for operation in operations:
                for variant, code_root in ordered:
                    case_root = temporary / "case-library"
                    shutil.copytree(fixture_root, case_root)
                    actual_start_hash = file_hash(case_root / "sample-course/course.json")
                    if actual_start_hash != fixture["fixture_manifest_sha256"]:
                        raise AssertionError("Fixture copy changed its starting manifest")
                    arguments = [sys.executable, str(Path(__file__).resolve()), "--_code-root", str(code_root),
                                 "--_library", str(case_root), "--_worker", operation,
                                 "--_fixture-info", str(fixture_info)]
                    if profiled:
                        arguments.append("--_profile-worker")
                    sample = run_subprocess(arguments)
                    sample.update({"variant": variant, "operation": operation, "iteration": iteration,
                                   "warmup": warmup, "profiled": profiled,
                                   "diagnostic": args.profile_only,
                                   "diagnostic_control": args.profile_only and not profiled,
                                   "starting_manifest_sha256": actual_start_hash,
                                   "process_launch_order": len(report["samples"]),
                                   "process_first_for_variant_operation": not any(
                                       row["variant"] == variant and row["operation"] == operation for row in report["samples"])})
                    # Successful stdout is parsed once instead of duplicated as an escaped giant JSON string.
                    if sample["ok"]:
                        sample.pop("stdout")
                        expected = identities[variant]["module_sha256"]
                        for item in sample["result"]["actual_product_modules"].values():
                            if item["sha256"] != expected[Path(item["path"]).name]:
                                sample["ok"] = False
                                sample["error"] = "Product source changed while benchmark was running"
                    if not sample["ok"]:
                        sample["failed_sample_path"] = preserve_failure(
                            case_root, sample, f"sample-{len(report['samples']):04d}-{variant}-{operation}", fixture)
                    report["samples"].append(sample)
                    report["summaries"] = [] if args.profile_only else summarize(report["samples"], roots, operations)
                    save_report(args.output, report)
                    if sample["ok"]:
                        shutil.rmtree(case_root)
        report["operations"] = [row["result"] for row in report["samples"] if row["variant"] == "candidate"
                                and not row["warmup"] and not row["profiled"] and not row["diagnostic"] and row["ok"]]
        if args.profile_only:
            report["diagnostics_passed"] = all(row["ok"] for row in report["samples"])
        else:
            report["passed"] = (all(row["ok"] for row in report["samples"])
                                and all(row["result"]["peak_bytes"] < args.budget_mib * 1024 * 1024
                                        for row in report["samples"] if not row["warmup"] and not row["profiled"]))
        report["complete"] = True
    save_report(args.output, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if (report["diagnostics_passed"] if args.profile_only else report["passed"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Measure isolated Python operations against equal-count, differently sized history.

This measures worker-process peak RSS/working set, not Codex, Google Drive,
OCR providers, or fixture construction. No personal library is accepted.
"""

import argparse
import json
import os
import platform
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "skills/codex-learner/scripts"), str(ROOT / "scripts")]


def peak_memory():
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
                (name, ctypes.c_size_t) for name in (
                    "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                    "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage",
                    "PagefileUsage", "PeakPagefileUsage")]

        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            raise ctypes.WinError(ctypes.get_last_error())
        return counters.PeakWorkingSetSize, "Windows PeakWorkingSetSize"
    import resource
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(peak if sys.platform == "darwin" else peak * 1024), "getrusage(RUSAGE_SELF).ru_maxrss"


def request(action, identifier, **fields):
    return {"action": action, "course_id": "sample-course", "operation_id": identifier, **fields}


def create_fixture(root, records, body_bytes):
    from codex_learner.operations import Engine
    from codex_learner.storage import Library
    from create_demo import seed
    library = Library(root)
    library.initialize()
    seed(library)
    engine = Engine(library)
    context = {"chapter_id": "chapter-1", "content_version": "v1", "question_id": "q1", "question_version": "qv1"}
    state = library.load("sample-course")
    refs = state["questions"]["q1"]["versions"]["qv1"]["rubrics"]["r1"]["source_refs"]
    for index in range(records):
        sid = f"history-{index:04d}"
        prefix = sid + "\n"
        answer = prefix + "a" * max(0, body_bytes - len(prefix))
        engine.apply(request("submit_answer", sid, **context, submission_id=sid, answer=answer))
        del answer
        prefix = "feedback-" + sid + "\n"
        feedback = prefix + "f" * max(0, body_bytes - len(prefix))
        engine.apply(request("assess", "assess-" + sid, submission_id=sid, assessment_id="assess-" + sid,
                             rubric_version="r1", feedback=feedback, feedback_help="none",
                             results={"compute": {"outcome": "meets", "reason": "Synthetic benchmark evidence.",
                                                  "source_refs": refs}}))
        del feedback
    directory = library.course_dir("sample-course")
    return {"manifest_bytes": (directory / "course.json").stat().st_size,
            "body_file_bytes": sum(path.stat().st_size for path in (directory / "bodies").rglob("*") if path.is_file()),
            "historical_submissions": records, "historical_assessments": records,
            "bytes_per_answer_or_feedback": body_bytes}


def worker(operation, library_root):
    from codex_learner.operations import Engine
    from codex_learner.storage import Library
    from codex_learner.views import course_summary
    library = Library(library_root)
    started = time.monotonic()
    if operation == "list":
        count = len(library.list_courses())
    elif operation == "review":
        count = len(course_summary(library.load("sample-course"))["chapters"])
    else:
        sid = "measured-" + uuid.uuid4().hex
        receipt = Engine(library).apply(request(
            "submit_answer", sid, chapter_id="chapter-1", content_version="v1", question_id="q1",
            question_version="qv1", submission_id=sid, answer="一次新的短作答。"))
        count = int(receipt["result"]["submission_id"] == sid)
    elapsed = time.monotonic() - started
    peak, method = peak_memory()
    return {"operation": operation, "peak_bytes": peak, "peak_mib": round(peak / (1024 * 1024), 3),
            "measurement": method, "elapsed_seconds": round(elapsed, 6), "result_count": count}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records", type=int, default=8)
    parser.add_argument("--small-body-kib", type=int, default=1)
    parser.add_argument("--large-body-mib", type=int, default=8)
    parser.add_argument("--budget-mib", type=int, default=256)
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/memory-benchmark.json")
    parser.add_argument("--_worker", choices=("submit", "review", "list"), help=argparse.SUPPRESS)
    parser.add_argument("--_library", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--_build-fixture", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--_body-bytes", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args._worker:
        if not args._library:
            parser.error("Internal worker needs its isolated fixture path.")
        print(json.dumps(worker(args._worker, args._library), ensure_ascii=False))
        return 0
    if args._build_fixture:
        if not args._library or not args._body_bytes:
            parser.error("Internal fixture builder needs its isolated path and body size.")
        print(json.dumps(create_fixture(args._library, args.records, args._body_bytes), ensure_ascii=False))
        return 0
    if not (1 <= args.records <= 100 and 1 <= args.small_body_kib <= 1024
            and 1 <= args.large_body_mib <= 32 and args.budget_mib > 0):
        parser.error("Use 1–100 records, 1–1024 small KiB, 1–32 large MiB and a positive budget.")
    results = {}
    with tempfile.TemporaryDirectory(prefix="codex-learner-memory-") as temporary:
        for name, size in (("small", args.small_body_kib * 1024), ("enlarged", args.large_body_mib * 1024 * 1024)):
            root = Path(temporary) / name
            # Build in another process: on POSIX, a child can inherit the launcher's
            # high-water RSS across exec. Keep this coordinator free of large bodies.
            fixture = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--_build-fixture",
                                      "--_library", str(root), "--records", str(args.records),
                                      "--_body-bytes", str(size)], capture_output=True, text=True,
                                     encoding="utf-8", timeout=600, check=True)
            info = json.loads(fixture.stdout)
            info["operations"] = []
            for operation in ("list", "review", "submit"):
                run = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--_worker", operation,
                                      "--_library", str(root)], capture_output=True, text=True,
                                     encoding="utf-8", timeout=120, check=True)
                info["operations"].append(json.loads(run.stdout))
            results[name] = info
    ratio = results["enlarged"]["manifest_bytes"] / results["small"]["manifest_bytes"]
    passed = all(item["peak_bytes"] < args.budget_mib * 1024 * 1024
                 for result in results.values() for item in result["operations"])
    comparable = 0.9 <= ratio <= 1.1
    from codex_learner import __version__
    report = {
        "checked_at": datetime.now(timezone.utc).isoformat(), "platform": platform.platform(),
        "python": sys.version, "learner_version": __version__, "target_budget_mib": args.budget_mib,
        "scope": "Isolated Python worker process peak RSS/working set for list, overview review, and a short new submission.",
        "write_integrity_cost": "Before rotating the backup, a mutation verifies referenced artifacts with streaming hashes; write latency therefore depends on total referenced history bytes.",
        "fixture_construction_included_in_daily_peak": False,
        "fixture_construction_in_separate_process": True,
        "codex_client_measured": False, "google_drive_measured": False, "ocr_provider_measured": False,
        "fixtures": results, "manifest_size_ratio": round(ratio, 6),
        "comparable_metadata": comparable, "operations_under_budget": passed,
        "passed": passed and comparable,
        "limits": "Synthetic fixed-count history; does not measure full-history export, large new answers, OCR, or arbitrarily growing manifest metadata.",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

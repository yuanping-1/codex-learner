#!/usr/bin/env python3
"""Stress synthetic multi-version history in an isolated temporary library.

The fixture uses real operation handlers followed by one compact/validate/verify
write, avoiding thousands of repeated transactions during fixture construction.
This is structural scale evidence, not observed long-term human usage.
"""

import argparse
import copy
import json
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

from benchmark_memory import peak_memory, request


def create_fixture(root, records):
    from codex_learner.blobs import compact, file_sha256, materialize, verify_artifacts
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
    return {
        "construction": "Synthetic bulk operation handlers, then compact, validate_state and verify_artifacts.",
        "historical_submissions": len(state["submissions"]), "historical_assessments": len(state["assessments"]),
        "help_events": len(state["help_events"]), "sources": len(state["sources"]),
        "lecture_versions": sum(len(chapter["versions"]) for chapter in state["chapters"].values()),
        "question_versions": sum(len(question["versions"]) for question in state["questions"].values()),
        "submission_receipts": records, "total_operation_receipts": len(state["operations"]),
        "idempotent_replay_probes": len(retries), "manifest_bytes": (directory / "course.json").stat().st_size,
        "artifact_bytes": sum(path.stat().st_size for path in directory.rglob("*")
                              if path.is_file() and path.name not in ("course.json", "course.previous.json", ".lock")),
    }


def worker(operation, root):
    from codex_learner.operations import Engine
    from codex_learner.storage import Library
    from codex_learner.views import course_history, course_summary

    library = Library(root)
    started = time.monotonic()
    details = {}
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
        details = {"page": 1, "page_size": 20, "include_bodies": include_bodies, "history_total": result["total"]}
    else:
        sid = "measured-" + uuid.uuid4().hex
        result = Engine(library).apply(request("submit_answer", sid, chapter_id="chapter-1",
                                               content_version="v10", question_id="q1", question_version="qv10",
                                               submission_id=sid, answer="新的简短作答。"))
        count = int(result["result"]["submission_id"] == sid and not result["duplicate"])
        if count != 1:
            raise AssertionError("New submission was not confirmed.")
    encoded_bytes = len(json.dumps(result, ensure_ascii=False).encode("utf-8"))
    elapsed = time.monotonic() - started
    peak, method = peak_memory()
    return {"operation": operation, "peak_bytes": peak, "peak_mib": round(peak / (1024 * 1024), 3),
            "measurement": method, "elapsed_seconds": round(elapsed, 6), "result_count": count,
            "serialized_result_bytes": encoded_bytes, **details}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records", type=int, default=1000)
    parser.add_argument("--budget-mib", type=int, default=256)
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/history-benchmark.json")
    parser.add_argument("--_build-fixture", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--_worker", choices=("list", "review", "history_page", "history_page_with_bodies", "submit"),
                        help=argparse.SUPPRESS)
    parser.add_argument("--_library", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not 20 <= args.records <= 10000 or args.budget_mib < 1:
        parser.error("Use 20–10000 historical records and a positive memory budget.")
    if args._build_fixture or args._worker:
        if not args._library:
            parser.error("Internal fixture and operation workers require an isolated library path.")
        result = create_fixture(args._library, args.records) if args._build_fixture else worker(args._worker, args._library)
        print(json.dumps(result, ensure_ascii=False))
        return 0

    def run_worker(root, *arguments):
        run = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--_library", str(root), *arguments],
                             capture_output=True, text=True, encoding="utf-8", check=True, timeout=600)
        return json.loads(run.stdout)

    with tempfile.TemporaryDirectory(prefix="codex-learner-history-") as temporary:
        root = Path(temporary) / "library"
        fixture = run_worker(root, "--_build-fixture", "--records", str(args.records))
        operations = [run_worker(root, "--_worker", operation) for operation in
                      ("list", "review", "history_page", "history_page_with_bodies", "submit")]
    from codex_learner import __version__
    passed = all(item["peak_bytes"] < args.budget_mib * 1024 * 1024 for item in operations)
    report = {
        "checked_at": datetime.now(timezone.utc).isoformat(), "platform": platform.platform(),
        "python": sys.version, "learner_version": __version__, "target_budget_mib": args.budget_mib,
        "fixture": fixture, "operations": operations, "passed": passed,
        "fixture_construction_in_separate_process": True, "fixture_construction_in_daily_peak": False,
        "codex_client_measured": False, "google_drive_measured": False, "ocr_provider_measured": False,
        "scope": "Independent Python process peak RSS/working set and operation plus JSON serialization time.",
        "write_integrity_cost": "Writes stream-verify referenced artifacts before backup rotation; latency scales with historical metadata and artifact bytes.",
        "limits": "Synthetic structure, not real long-term human usage. No full-history export, OCR, native Codex/Drive memory, or unbounded history claim. Timings are single runs, not latency percentiles.",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Measure incremental migration of an isolated schema-1 file larger than 64 MiB.

Fixture construction, preview and confirmed migration run in separate processes.
The expanded legacy fixture is written one body at a time, never as one JSON value.
"""

import argparse
import json
import platform
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "skills/codex-learner/scripts"), str(ROOT / "scripts")]

from benchmark_memory import create_fixture as create_v2_fixture, peak_memory


def expanded_json(value, directory):
    """Stream legacy JSON, resolving one bounded scalar or receipt at a time."""
    from codex_learner.blobs import BODY_FIELDS, is_body_ref, read_body
    if isinstance(value, dict):
        yield "{"
        for index, (key, item) in enumerate(dict.items(value)):
            if index:
                yield ","
            logical = key[:-4] if key.endswith("_ref") else None
            expand = (logical in BODY_FIELDS or logical == "receipt") and is_body_ref(item)
            yield json.dumps(logical if expand else key, ensure_ascii=False)
            yield ":"
            yield from expanded_json(read_body(directory, item) if expand else item, directory)
        yield "}"
    elif isinstance(value, list):
        yield "["
        for index, item in enumerate(value):
            if index:
                yield ","
            yield from expanded_json(item, directory)
        yield "]"
    else:
        yield from json.JSONEncoder(ensure_ascii=False, allow_nan=False).iterencode(value)


def build_fixture(root):
    from codex_learner.blobs import file_sha256, lazy
    from codex_learner.common import read_json
    from codex_learner.storage import Library, atomic_write

    create_v2_fixture(root, records=6, body_bytes=4 * 1024 * 1024)
    library = Library(root)
    directory = library.course_dir("sample-course")
    state = library.load("sample-course")
    previous_path = directory / "course.previous.json"
    previous = lazy(read_json(previous_path), directory)
    expectations = {
        "answer_hashes": {sid: sub["answer_ref"]["sha256"] for sid, sub in state["submissions"].items()},
        "ids": {kind: sorted(state[kind]) for kind in ("submissions", "assessments", "help_events", "operations")},
        "source_hashes": {sid: source["sha256"] for sid, source in state["sources"].items()},
        "state_revision": state["state_revision"], "created_at": state["created_at"], "updated_at": state["updated_at"],
    }
    for value, target in ((previous, previous_path), (state, directory / "course.json")):
        value["schema_version"] = 1
        value.pop("source_verifications", None)
        atomic_write(target, (fragment.encode("utf-8") for fragment in expanded_json(value, directory)))
    expectations["original_bytes"] = (directory / "course.json").stat().st_size
    expectations["original_sha256"] = file_sha256(directory / "course.json")
    expectations["previous_sha256"] = file_sha256(previous_path)
    if expectations["original_bytes"] <= 64 * 1024 * 1024:
        raise AssertionError("Migration fixture did not exceed the normal 64 MiB reader limit.")
    (root.parent / "expectations.json").write_text(json.dumps(expectations, ensure_ascii=False), encoding="utf-8")
    return {"construction": "Six 4 MiB answers and six 4 MiB feedback bodies; legacy receipts repeat answers. Stream-expanded from v2 metadata, one scalar at a time.",
            "original_bytes": expectations["original_bytes"], "original_sha256": expectations["original_sha256"],
            "previous_sha256": expectations["previous_sha256"], "submissions": 6, "assessments": 6,
            "bytes_per_answer_or_feedback": 4 * 1024 * 1024, "expected_answer_hashes": expectations["answer_hashes"]}


def measure(operation, root):
    from unittest import mock
    import ijson
    from codex_learner.blobs import file_sha256
    from codex_learner.common import LearnerError
    from codex_learner.migration import migrate_course, preview_migration
    from codex_learner.storage import Library

    expected = json.loads((root.parent / "expectations.json").read_text(encoding="utf-8"))
    library = Library(root)
    directory = library.course_dir("sample-course")
    original = directory / "course.json"
    original_read_text, original_read_bytes = Path.read_text, Path.read_bytes

    def guarded_text(path, *args, **kwargs):
        if path == original and path.stat().st_size > 64 * 1024 * 1024:
            raise AssertionError("Full legacy Path.read_text is forbidden.")
        return original_read_text(path, *args, **kwargs)

    def guarded_bytes(path, *args, **kwargs):
        if path == original and path.stat().st_size > 64 * 1024 * 1024:
            raise AssertionError("Full legacy Path.read_bytes is forbidden.")
        return original_read_bytes(path, *args, **kwargs)

    started = time.monotonic()
    try:
        library.load("sample-course")
    except LearnerError as exc:
        if exc.code != "migration_required":
            raise
        ordinary_load_error = exc.code
    else:
        raise AssertionError("Normal reader unexpectedly accepted the oversized legacy manifest.")

    with mock.patch.object(Path, "read_text", guarded_text), mock.patch.object(Path, "read_bytes", guarded_bytes), \
            mock.patch("json.load", side_effect=AssertionError("Full legacy json.load is forbidden.")), \
            mock.patch.object(ijson, "basic_parse", wraps=ijson.basic_parse) as incremental:
        if operation == "preview":
            result = preview_migration(library, "sample-course")
            integrity = {"original_unchanged": file_sha256(original) == expected["original_sha256"],
                         "previous_unchanged": file_sha256(directory / "course.previous.json") == expected["previous_sha256"]}
        else:
            result = migrate_course(library, "sample-course", expected["original_sha256"], confirmed=True)
            state = library.load("sample-course")
            backup = Path(result.pop("backup"))
            result["backup_relative_path"] = backup.relative_to(directory).as_posix()
            actual_hashes = {sid: file_sha256(directory / sub["answer_ref"]["path"])
                             for sid, sub in state["submissions"].items()}
            source_hashes = {sid: file_sha256(directory / source["path"]) for sid, source in state["sources"].items()}
            backup_hash = file_sha256(backup / "course.json")
            previous_backup_hash = file_sha256(backup / "course.previous.json")
            integrity = {
                "schema_two": state["schema_version"] == 2,
                "all_ids_preserved": all(sorted(state[kind]) == values for kind, values in expected["ids"].items()),
                "answer_bytes_preserved": actual_hashes == expected["answer_hashes"],
                "source_bytes_preserved": source_hashes == expected["source_hashes"],
                "revision_and_timestamps_preserved": all(state[key] == expected[key] for key in
                                                         ("state_revision", "created_at", "updated_at")),
                "original_backup_exact": backup_hash == expected["original_sha256"],
                "previous_backup_exact": previous_backup_hash == expected["previous_sha256"],
                "previous_snapshot_is_original": file_sha256(directory / "course.previous.json") == expected["original_sha256"],
            }
            result.update(actual_answer_hashes=actual_hashes, backup_sha256=backup_hash,
                          previous_backup_sha256=previous_backup_hash)
    if not all(integrity.values()) or incremental.call_count != 1 or result["reader"] != "ijson_incremental":
        raise AssertionError(f"Migration integrity failed: {integrity}")
    elapsed = time.monotonic() - started
    peak, method = peak_memory()
    return {"operation": operation, "peak_bytes": peak, "peak_mib": round(peak / (1024 * 1024), 3),
            "measurement": method, "elapsed_seconds": round(elapsed, 6), "ordinary_load_error": ordinary_load_error,
            "legacy_full_read_guarded": True, "ijson_calls": incremental.call_count, "ijson_backend": ijson.backend,
            "integrity": integrity, "result": result}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--budget-mib", type=int, default=256)
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/migration-benchmark.json")
    parser.add_argument("--_worker", choices=("build", "preview", "migrate"), help=argparse.SUPPRESS)
    parser.add_argument("--_library", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.budget_mib < 1:
        parser.error("The memory budget must be positive.")
    if args._worker:
        if not args._library:
            parser.error("Internal workers require an isolated fixture path.")
        result = build_fixture(args._library) if args._worker == "build" else measure(args._worker, args._library)
        print(json.dumps(result, ensure_ascii=False))
        return 0

    def run_worker(root, operation):
        run = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--_worker", operation,
                              "--_library", str(root)], capture_output=True, text=True, encoding="utf-8", timeout=600)
        if run.returncode:
            raise RuntimeError(f"{operation} worker failed:\n{run.stderr}")
        return json.loads(run.stdout)

    with tempfile.TemporaryDirectory(prefix="codex-learner-migration-memory-") as temporary:
        root = Path(temporary) / "library"
        fixture = run_worker(root, "build")
        operations = [run_worker(root, operation) for operation in ("preview", "migrate")]
    from codex_learner import __version__
    passed = all(item["peak_bytes"] < args.budget_mib * 1024 * 1024 for item in operations)
    report = {
        "checked_at": datetime.now(timezone.utc).isoformat(), "platform": platform.platform(),
        "python": sys.version, "learner_version": __version__, "target_budget_mib": args.budget_mib,
        "fixture": fixture, "operations": operations, "passed": passed,
        "fixture_construction_in_separate_process": True, "fixture_construction_in_migration_peak": False,
        "codex_client_measured": False, "google_drive_measured": False, "ocr_provider_measured": False,
        "limits": "Synthetic oversized schema-1 input with bounded 4 MiB scalars; not real human history or a claim for arbitrary scalar/metadata sizes. Independent Linux/Windows Python worker measurements only; timings are single runs.",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

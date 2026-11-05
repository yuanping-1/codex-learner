"""Explicit, side-by-side schema 1 migration with bounded streaming input."""
import hashlib
import json
import shutil
import tempfile
import sys
from pathlib import Path

from .blobs import BODY_FIELDS, compact, file_sha256, iter_body_refs, verify_artifacts, write_body
from .common import LearnerError, now, require, validate_state
from .storage import atomic_copy, atomic_write_json, file_lock, write_artifact_from_file

MAX_SCALAR_BYTES = 16 * 1024 * 1024
MAX_MANIFEST_BYTES = 32 * 1024 * 1024
MAX_METADATA_ESTIMATE = 96 * 1024 * 1024


def _preflight(path):
    # ijson is incremental across records; each scalar still has to fit in RAM.
    # Scan without materializing a giant legacy string, reject before decoding.
    inside = escaped = False
    length = maximum = 0
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            for byte in block:
                if inside:
                    length += 1
                    require(length <= MAX_SCALAR_BYTES,
                            "A legacy scalar exceeds the 16 MiB migration safety limit; split it with a reviewed converter.",
                            "migration_capacity")
                    if escaped:
                        escaped = False
                    elif byte == 92:
                        escaped = True
                    elif byte == 34:
                        inside = False
                        maximum = max(maximum, length)
                elif byte == 34:
                    inside = True
                    length = 0
    return maximum


def _stream_compact(path, directory):
    try:
        import ijson
    except ImportError as exc:
        raise LearnerError("parser_missing", "Migration requires the pinned ijson base dependency.") from exc
    stack = []
    root = None
    metadata_estimate = 0

    def account(amount):
        nonlocal metadata_estimate
        metadata_estimate += amount
        require(metadata_estimate <= MAX_METADATA_ESTIMATE,
                "Legacy metadata exceeds the incremental migration safety budget.", "migration_capacity")

    def attach(value):
        nonlocal root
        if not stack:
            root = value
            return
        parent = stack[-1]
        if isinstance(parent[0], list):
            account(16 + (sys.getsizeof(value) if not isinstance(value, (dict, list)) else 0))
            parent[0].append(value)
        else:
            key = parent[1]
            account(96 + sys.getsizeof(key))
            require(key is not None and key not in parent[0],
                    "Duplicate or conflicting JSON field in legacy course.", "invalid_json")
            if key in BODY_FIELDS and (isinstance(value, str) or
                    key in {"required", "alternatives", "critical_errors"} and isinstance(value, list)):
                account(768)  # small immutable reference including path/hash strings
                parent[0][key + "_ref"] = write_body(directory, value, json_value=not isinstance(value, str))
            elif key == "receipt" and isinstance(value, dict) and len(stack) >= 3 and stack[-3][1] == "operations":
                account(768)
                parent[0]["receipt_ref"] = write_body(directory, value, json_value=True)
            else:
                account(sys.getsizeof(value) if not isinstance(value, (dict, list)) else 0)
                parent[0][key] = value
            parent[1] = None

    try:
        with path.open("rb") as stream:
            for event, value in ijson.basic_parse(stream, use_float=True):
                if event in ("start_map", "start_array"):
                    account(256)
                    stack.append([{} if event == "start_map" else [], None, set()])
                elif event == "map_key":
                    require(value not in stack[-1][2], "Duplicate JSON key in legacy course.", "invalid_json")
                    stack[-1][2].add(value)
                    stack[-1][1] = value
                elif event in ("end_map", "end_array"):
                    completed = stack.pop()[0]
                    attach(completed)
                else:
                    attach(value)
    except (ValueError, TypeError, ijson.JSONError) as exc:
        raise LearnerError("invalid_json", "Legacy course cannot be streamed safely.") from exc
    return root


def _build(library, course_id, stage):
    directory = library.course_dir(course_id)
    original = directory / "course.json"
    require(original.is_file() and not original.is_symlink(), "Legacy course is missing.", "course_missing")
    size = original.stat().st_size
    # Staged sidecars + immutable old snapshot + atomic switch need room. Extra
    # space for source artifacts is checked below before their staged copy.
    require(shutil.disk_usage(directory).free >= size * 3 + 16 * 1024 * 1024,
            "Not enough temporary disk space for a verified migration and backup.", "disk_space")
    source_hash = file_sha256(original)
    largest_scalar = _preflight(original)
    state = _stream_compact(original, stage)
    require(isinstance(state, dict) and state.get("schema_version") == 1,
            "Migration only accepts schema 1; no implicit downgrade or repeated migration.", "unsupported_schema")
    # Structural validation after compaction checks relationships, without
    # retaining original bodies or manufacturing new evidence of verification.
    state["schema_version"] = 2
    state.setdefault("source_verifications", {})
    for source in state["sources"].values():
        source["legacy_claim"] = True
        for segment in source["segments"]:
            segment["legacy_verified"] = segment.get("verified", False)
            segment["legacy_claim"] = True
            segment["verified"] = False
    state["migration"] = {"from_schema": 1, "original_sha256": source_hash,
                          "original_revision": state["state_revision"], "migrated_at": now(),
                          "legacy_verification": "historical_claim_requires_review"}
    validate_state(state, course_id)
    refs = list(iter_body_refs(state))
    external_bytes = sum((directory / ref["path"]).stat().st_size for ref in refs
                         if not (stage / ref["path"]).exists() and (directory / ref["path"]).is_file())
    require(shutil.disk_usage(directory).free >= external_bytes + size + 16 * 1024 * 1024,
            "Not enough disk space to verify the complete staged course.", "disk_space")
    for ref in refs:
        if not (stage / ref["path"]).exists():
            from .blobs import verify_ref
            original_artifact = verify_ref(directory, ref)
            write_artifact_from_file(stage, ref["path"], original_artifact)
    verified_count = verify_artifacts(stage, state)
    target = stage / "course.json"
    atomic_write_json(target, state)
    require(target.stat().st_size <= MAX_MANIFEST_BYTES,
            "Compact manifest exceeds the configured safe core budget.", "migration_capacity")
    require(file_sha256(original) == source_hash, "Legacy course changed during preview.", "migration_changed")
    report = {"course_id": course_id, "from_schema": 1, "to_schema": 2,
              "state_revision": state["state_revision"], "original_sha256": source_hash,
              "original_bytes": size, "manifest_bytes": target.stat().st_size,
              "largest_json_scalar_bytes": largest_scalar, "verified_artifacts": verified_count,
              "submission_count": len(state["submissions"]), "operation_count": len(state["operations"]),
              "legacy_verification": "requires_review", "reader": "ijson_incremental"}
    return state, report


def preview_migration(library, course_id):
    directory = library.course_dir(course_id)
    with file_lock(directory / ".lock"):
        with tempfile.TemporaryDirectory(prefix=".migration-preview-", dir=library.root) as temp:
            _, report = _build(library, course_id, Path(temp))
    return {**report, "preview": True, "changed": False}


def migrate_course(library, course_id, expected_sha256, confirmed=False):
    require(confirmed, "Inspect migration preview then explicitly confirm the conversion.", "confirmation_required")
    directory = library.course_dir(course_id)
    with file_lock(directory / ".lock"):
        require(file_sha256(directory / "course.json") == expected_sha256,
                "Course changed since migration preview.", "migration_changed")
        with tempfile.TemporaryDirectory(prefix=".migration-stage-", dir=library.root) as temp:
            stage = Path(temp)
            state, report = _build(library, course_id, stage)
            require(report["original_sha256"] == expected_sha256, "Migration preview no longer matches.", "migration_changed")
            backup = directory / "migration-backups" / expected_sha256
            backup.mkdir(parents=True, exist_ok=True)
            # Preserve bytes, IDs, timestamps, old receipts and previous valid
            # version. Never collect any body referenced by these snapshots.
            atomic_copy(directory / "course.json", backup / "course.json")
            previous = directory / "course.previous.json"
            if previous.exists():
                atomic_copy(previous, backup / "course.previous.json")
            atomic_write_json(backup / "migration.json", report)
            for path in stage.rglob("*"):
                if path.is_file() and path.name != "course.json":
                    write_artifact_from_file(directory, path.relative_to(stage).as_posix(), path)
            verify_artifacts(directory, state)
            atomic_copy(directory / "course.json", previous)
            atomic_copy(stage / "course.json", directory / "course.json")
            try:
                restored = library.load(course_id)
                require(restored["migration"]["original_sha256"] == expected_sha256,
                        "Migration readback failed.", "commit_uncertain")
            except (LearnerError, OSError) as exc:
                raise LearnerError("commit_uncertain", "Migration may have completed; inspect course and preserved backup.") from exc
    return {**report, "preview": False, "changed": True, "backup": str(backup)}

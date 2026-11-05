"""Targeted body reads and streaming full export."""
import json
from pathlib import Path

from .blobs import BODY_FIELDS, is_body_ref, read_body, verify_artifacts
from .common import require
from .storage import atomic_write, file_sha256
from .views import submission_detail


def targeted_read(state, *, submission=None, source=None, locator=None, chapter=None,
                  content_version=None, part=None, question=None, question_version=None):
    require(sum(bool(value) for value in (submission, source, chapter, question)) == 1,
            "Choose exactly one submission, source location, lecture part, or question.")
    if submission:
        return submission_detail(state, submission)
    if source:
        record = state["sources"].get(source)
        require(record is not None and locator, "A known source and locator are required.", "unknown_target")
        segment = next((s for s in record["segments"] if s["locator"] == locator), None)
        require(segment is not None, "Unknown source locator.", "unknown_target")
        return {"source_id": source, **segment, "text": segment["text"]}
    if chapter:
        record = state["chapters"].get(chapter)
        require(record is not None and part, "A known chapter and part are required.", "unknown_target")
        version = content_version or record["current_version"]
        require(version in record["versions"], "Unknown content version.", "unknown_target")
        item = next((p for p in record["versions"][version]["parts"] if p["part_id"] == part), None)
        require(item is not None, "Unknown lecture part.", "unknown_target")
        return {"chapter_id": chapter, "content_version": version, **item, "text": item["text"]}
    record = state["questions"].get(question)
    require(record is not None and question_version in record["versions"],
            "A known question and explicit question version are required.", "unknown_target")
    q = record["versions"][question_version]
    return {"question_id": question, "question_version": question_version,
            "chapter_id": q["chapter_id"], "prompt": q["prompt"]}


def _expanded(value, directory):
    if isinstance(value, dict):
        yield "{"
        first = True
        for key, item in dict.items(value):
            logical = key[:-4] if key.endswith("_ref") else None
            if logical in BODY_FIELDS | {"receipt"} and is_body_ref(item):
                key, item = logical, read_body(directory, item)
            if not first:
                yield ","
            first = False
            yield json.dumps(key, ensure_ascii=False) + ":"
            yield from _expanded(item, directory)
        yield "}"
    elif isinstance(value, list):
        yield "["
        for index, item in enumerate(value):
            if index:
                yield ","
            yield from _expanded(item, directory)
        yield "]"
    else:
        yield json.dumps(value, ensure_ascii=False, allow_nan=False)


def export_course(library, course_id, output):
    state = library.load(course_id)
    directory = library.course_dir(course_id)
    output = Path(output).expanduser().absolute()
    require(not output.exists(), "Choose a new export path.", "export_exists")
    require(not output.resolve().is_relative_to(directory.resolve()),
            "Exports belong outside the active course.", "invalid_path")
    verify_artifacts(directory, state)
    output.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(output, (part.encode("utf-8") for part in _expanded(state, directory)))
    return {"path": str(output), "sha256": file_sha256(output), "state_revision": state["state_revision"],
            "format": "expanded_course_json", "restorable_archive": False}

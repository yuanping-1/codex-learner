"""Immutable, content-addressed bodies and metadata-only lazy records.

Iteration, serialization and deepcopy never read bodies. Only an explicit logical
field lookup does; a missing/corrupt body is an error, never an empty string.
"""
import copy
import hashlib
import json
from pathlib import Path

from .common import LearnerError, canonical, require

BODY_FIELDS = frozenset({"text", "answer", "feedback", "reason", "prompt",
                         "reference_answer", "required", "alternatives", "critical_errors"})
CHUNK_SIZE = 1024 * 1024


def file_sha256(path):
    value = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(CHUNK_SIZE), b""):
            value.update(chunk)
    return value.hexdigest()


def artifact_path(directory, relative):
    require(isinstance(relative, str) and relative and "\\" not in relative,
            "Invalid artifact path.", "invalid_path")
    rel = Path(relative)
    require(not rel.is_absolute() and all(part not in ("..", ".") for part in rel.parts),
            "Artifact escaped course directory.", "invalid_path")
    root = Path(directory).resolve()
    path = root / rel
    require(path.resolve().is_relative_to(root), "Artifact escaped course directory.", "invalid_path")
    cursor = path
    while cursor != root:
        require(not cursor.is_symlink(), "Artifact symbolic links are not supported.", "invalid_path")
        cursor = cursor.parent
    return path


def is_body_ref(value):
    return (isinstance(value, dict) and {"path", "sha256"} <= set(value)
            and set(value) <= {"path", "sha256", "byte_length", "encoding", "format"})


def validate_ref(ref):
    import re
    require(isinstance(ref, dict) and isinstance(ref.get("path"), str)
            and bool(re.fullmatch(r"[0-9a-f]{64}", ref.get("sha256", "")))
            and ("byte_length" not in ref or type(ref["byte_length"]) is int and ref["byte_length"] >= 0),
            "Malformed body reference.", "corrupt_state")
    return ref


def verify_ref(directory, ref):
    validate_ref(ref)
    path = artifact_path(directory, ref["path"])
    require(path.is_file(), f"Referenced artifact is missing: {ref['path']}", "artifact_missing")
    require(("byte_length" not in ref or path.stat().st_size == ref["byte_length"])
            and file_sha256(path) == ref["sha256"],
            f"Referenced artifact failed integrity verification: {ref['path']}", "artifact_corrupt")
    return path


def read_body(directory, ref):
    path = verify_ref(directory, ref)
    try:
        if ref.get("format") == "json":
            from .common import read_json
            return lazy(read_json(path), directory)
        return _read_text_exact(path)
    except (UnicodeError, ValueError) as exc:
        raise LearnerError("artifact_corrupt", f"Body cannot be decoded: {ref['path']}") from exc


def _read_text_exact(path):
    # newline='' preserves CRLF, CR, LF and mixed original answer line endings.
    with path.open("r", encoding="utf-8", newline="") as stream:
        return stream.read()


class LazyRecord(dict):
    def __init__(self, data, directory):
        super().__init__(data)
        self._directory = Path(directory)

    def __getitem__(self, key):
        if dict.__contains__(self, key):
            return dict.__getitem__(self, key)
        ref_key = str(key) + "_ref"
        if key in BODY_FIELDS | {"receipt"} and dict.__contains__(self, ref_key):
            ref = dict.__getitem__(self, ref_key)
            if is_body_ref(ref):
                return read_body(self._directory, ref)
        raise KeyError(key)

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default

    def __deepcopy__(self, memo):
        result = LazyRecord({}, self._directory)
        memo[id(self)] = result
        for key, value in dict.items(self):
            result[key] = copy.deepcopy(value, memo)
        return result


def lazy(value, directory):
    if isinstance(value, dict):
        return LazyRecord({k: lazy(v, directory) for k, v in dict.items(value)}, directory)
    if isinstance(value, list):
        return [lazy(v, directory) for v in value]
    return value


def write_body(directory, value, json_value=False):
    from .storage import write_artifact, atomic_write_json
    if not json_value:
        require(isinstance(value, str), "Body must be text.")
        data = value.encode("utf-8")
        hash_value = hashlib.sha256(data).hexdigest()
        ref = write_artifact(Path(directory), f"bodies/{hash_value[:2]}/{hash_value}.txt", data)
        return {**ref, "encoding": "utf-8"}
    # Encode JSON incrementally; receipts contain references to the same text
    # files as the authoritative records rather than copies of answer text.
    encoder = json.JSONEncoder(ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    digest = hashlib.sha256()
    byte_length = 0
    for fragment in encoder.iterencode(value):
        encoded = fragment.encode("utf-8")
        digest.update(encoded)
        byte_length += len(encoded)
    hash_value = digest.hexdigest()
    relative = f"bodies/{hash_value[:2]}/{hash_value}.json"
    path = artifact_path(directory, relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        require(file_sha256(path) == hash_value, "Immutable receipt was modified.", "artifact_corrupt")
    else:
        atomic_write_json(path, value)
    return {"path": relative, "sha256": hash_value, "byte_length": byte_length,
            "encoding": "utf-8", "format": "json"}


def compact(value, directory, _path=()):
    """Externalize only new logical bodies; existing refs are copied as metadata."""
    if isinstance(value, list):
        return [compact(v, directory, _path + (str(i),)) for i, v in enumerate(value)]
    if not isinstance(value, dict):
        return value
    result = {}
    for key, item in dict.items(value):
        if key in BODY_FIELDS and (isinstance(item, str) or
                key in {"required", "alternatives", "critical_errors"} and isinstance(item, list)):
            result[key + "_ref"] = write_body(directory, item, json_value=not isinstance(item, str))
        elif key == "receipt" and isinstance(item, dict) and len(_path) >= 2 and _path[-2] == "operations":
            result["receipt_ref"] = write_body(directory, compact(item, directory, _path + (key,)), json_value=True)
        else:
            result[key] = compact(item, directory, _path + (key,))
    return result


def materialize(value, directory=None):
    """Explicitly expand one requested receipt/body, never called for lists/views."""
    if isinstance(value, list):
        return [materialize(v, directory) for v in value]
    if not isinstance(value, dict):
        return value
    directory = directory or getattr(value, "_directory", None)
    result = {}
    for key, item in dict.items(value):
        name = key[:-4] if key.endswith("_ref") else None
        if (name in BODY_FIELDS or name == "receipt") and is_body_ref(item):
            require(directory is not None, "Body directory is required.")
            result[name] = materialize(read_body(directory, item), directory)
        else:
            result[key] = materialize(item, directory)
    return result


def iter_body_refs(value, directory=None, _seen=None):
    """Yield source originals, bodies, and transitive references in receipts."""
    seen = set() if _seen is None else _seen
    if isinstance(value, dict):
        if isinstance(value.get("path"), str) and isinstance(value.get("sha256"), str):
            identity = (value["path"], value["sha256"])
            if identity not in seen:
                seen.add(identity)
                yield value
                if directory is not None and value.get("format") == "json":
                    yield from iter_body_refs(read_body(directory, value), directory, seen)
        for item in dict.values(value):
            yield from iter_body_refs(item, directory, seen)
    elif isinstance(value, list):
        for item in value:
            yield from iter_body_refs(item, directory, seen)


def verify_artifacts(directory, state):
    count = 0
    for ref in iter_body_refs(state, directory):
        verify_ref(directory, ref)
        count += 1
    return count

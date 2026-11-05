"""Immutable, content-addressed bodies and metadata-only lazy records.

Iteration, serialization and deepcopy never read bodies. Only an explicit logical
field lookup does; a missing/corrupt body is an error, never an empty string.
"""
import copy
import hashlib
import json
import stat
from pathlib import Path, PureWindowsPath

from .common import LearnerError, canonical, parse_json_text, require

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
    require(isinstance(relative, str) and relative and "\\" not in relative and ":" not in relative,
            "Invalid artifact path.", "invalid_path")
    # Validate the original spelling before Path normalizes '.' or empty parts.
    # These are portable course-relative names: Windows trims trailing dots and
    # spaces, and reserves device names even when they have an extension.
    parts = relative.split("/")
    rel = Path(relative)
    require(not rel.is_absolute() and not rel.drive and all(
        part not in ("", "..", ".") and part == part.rstrip(" .")
        and not PureWindowsPath(part).is_reserved() for part in parts),
            "Artifact escaped course directory.", "invalid_path")
    root = Path(directory).resolve()
    # Check every component without following it. With a resolved root and no
    # navigation/device components, rejecting all links/reparse points keeps
    # the path beneath that root without resolving the full path a second time.
    # No ancestor result is cached across references or verification calls.
    cursor = root
    for part in parts:
        cursor /= part
        try:
            info = cursor.lstat()
        except (FileNotFoundError, NotADirectoryError):
            # New immutable bodies may need directories that do not exist yet.
            # Readers still require an existing regular file below.
            continue
        require(not stat.S_ISLNK(info.st_mode)
                and not getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT,
                "Artifact symbolic links and reparse points are not supported.", "invalid_path")
    return cursor


def _artifact_file_info(path, relative):
    try:
        info = path.stat()
    except (FileNotFoundError, NotADirectoryError):
        raise LearnerError("artifact_missing", f"Referenced artifact is missing: {relative}") from None
    require(stat.S_ISREG(info.st_mode), f"Referenced artifact is missing: {relative}", "artifact_missing")
    return info


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
    info = _artifact_file_info(path, ref["path"])
    require(("byte_length" not in ref or info.st_size == ref["byte_length"])
            and file_sha256(path) == ref["sha256"],
            f"Referenced artifact failed integrity verification: {ref['path']}", "artifact_corrupt")
    return path


def _read_verified_json(directory, ref):
    """Hash and parse the same bytes, keeping only this one JSON body in memory."""
    validate_ref(ref)
    path = artifact_path(directory, ref["path"])
    info = _artifact_file_info(path, ref["path"])
    expected_length = ref.get("byte_length")
    require(expected_length is None or info.st_size == expected_length,
            f"Referenced artifact failed integrity verification: {ref['path']}", "artifact_corrupt")
    data = bytearray()
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(CHUNK_SIZE), b""):
            require(expected_length is None or len(data) + len(chunk) <= expected_length,
                    f"Referenced artifact failed integrity verification: {ref['path']}", "artifact_corrupt")
            digest.update(chunk)
            data.extend(chunk)
    require((expected_length is None or len(data) == expected_length)
            and digest.hexdigest() == ref["sha256"],
            f"Referenced artifact failed integrity verification: {ref['path']}", "artifact_corrupt")
    try:
        text = data.decode("utf-8")
    except UnicodeError as exc:
        raise LearnerError("invalid_json", f"Invalid JSON: {path}", path=str(path)) from exc
    del data
    return parse_json_text(text, path)


def read_body(directory, ref):
    if isinstance(ref, dict) and ref.get("format") == "json":
        return lazy(_read_verified_json(directory, ref), directory)
    path = verify_ref(directory, ref)
    try:
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


def _walk_body_refs(value, directory, seen, verify):
    if isinstance(value, dict):
        if isinstance(value.get("path"), str) and isinstance(value.get("sha256"), str):
            identity = (value["path"], value["sha256"])
            if identity not in seen:
                seen.add(identity)
                is_json = directory is not None and value.get("format") == "json"
                if is_json:
                    body = _read_verified_json(directory, value)
                elif verify:
                    verify_ref(directory, value)
                yield value
                if is_json:
                    yield from _walk_body_refs(body, directory, seen, verify)
        for item in dict.values(value):
            yield from _walk_body_refs(item, directory, seen, verify)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_body_refs(item, directory, seen, verify)


def iter_body_refs(value, directory=None, _seen=None):
    """Yield source originals, bodies, and transitive references in receipts."""
    seen = set() if _seen is None else _seen
    yield from _walk_body_refs(value, directory, seen, verify=False)


def verify_artifacts(directory, state):
    # JSON traversal already verifies the bytes it parses; do not read/hash
    # these receipts again. The seen set lives only for this verification.
    return sum(1 for _ in _walk_body_refs(state, directory, set(), verify=True))

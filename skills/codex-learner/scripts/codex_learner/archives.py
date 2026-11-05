"""Closed course archives; a Drive folder copy is never proof of cloud storage.

Only immutable course artifacts and explicit state snapshots enter an archive.
The active library remains local. No network client or credential handling lives here.
"""

import hashlib
import json
import os
import shutil
import stat
import tempfile
import uuid
import zipfile
from pathlib import Path, PurePosixPath

from .common import LearnerError, canonical, identifier, now, read_json, require
from .storage import Library, atomic_write, file_lock


FORMAT = "codex-learner-course-archive"
INDEX_NAME = ".archive-index.json"
CHUNK = 1024 * 1024
MANAGED_ROOTS = {"sources", "lectures", "bodies", "blobs", "answers", "assessments", "artifacts"}
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_JSON_SCALAR_BYTES = 16 * 1024 * 1024
MAX_ARCHIVE_FILES = 100000
MAX_EXPANDED_BYTES = 16 * 1024 * 1024 * 1024
DISK_RESERVE_BYTES = 16 * 1024 * 1024


def _require_disk_space(path, required_bytes, purpose):
    """Inspect only the local filesystem; this says nothing about Drive quota."""
    directory = Path(path).expanduser().absolute()
    while not directory.exists():
        directory = directory.parent
    available = shutil.disk_usage(directory).free
    if available < required_bytes:
        raise LearnerError("disk_space", f"Insufficient local disk space for {purpose}.",
                           path=str(directory), required_bytes=required_bytes,
                           available_bytes=available, scope="local_filesystem")


def _sha(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(CHUNK), b""):
            result.update(chunk)
    return result.hexdigest()


def _relative(value):
    require(isinstance(value, str) and value and "\\" not in value and ":" not in value,
            "Archive paths must be relative POSIX paths.", "invalid_archive")
    path = PurePosixPath(value)
    require(not path.is_absolute() and all(p not in ("", ".", "..") for p in value.split("/")),
            "Archive path escapes its course.", "invalid_archive")
    return path


def _managed(value):
    path = _relative(value)
    permitted = (value in ("course.json", "course.previous.json")
                 or (len(path.parts) == 1 and value.startswith("before-restore-") and value.endswith(".json"))
                 or path.parts[0] in MANAGED_ROOTS
                 or (path.parts[0] == "migration-backups" and value.endswith(".json")))
    require(permitted, "Archive references a file outside managed course data.", "invalid_archive")
    require(not any(p in ("config.json", ".env", "install-manifest.json", "__pycache__", ".venv")
                    for p in path.parts), "Runtime configuration cannot enter a course archive.", "invalid_archive")
    return value


def _file(directory, relative):
    _managed(relative)
    path = directory / relative
    require(not any(p.is_symlink() for p in [path, *path.parents] if p != directory.parent),
            "Archive artifacts cannot be symbolic links.", "invalid_path")
    require(path.resolve().is_relative_to(directory.resolve()) and path.is_file(),
            "A required course artifact is missing.", "artifact_missing")
    return path


def _snapshot_details(open_stream):
    """Scan references without retaining legacy answer/source/receipt bodies."""
    try:
        import ijson
    except ImportError as exc:
        raise LearnerError("parser_missing", "Archive verification requires the pinned ijson base dependency.") from exc
    inside = escaped = False
    length = 0
    with open_stream() as stream:
        for block in iter(lambda: stream.read(CHUNK), b""):
            for byte in block:
                if inside:
                    length += 1
                    require(length <= MAX_JSON_SCALAR_BYTES,
                            "A JSON scalar exceeds the safe archive reader limit.", "archive_capacity")
                    if escaped:
                        escaped = False
                    elif byte == 92:
                        escaped = True
                    elif byte == 34:
                        inside = False
                elif byte == 34:
                    inside, length = True, 0
    stack, references, identities = [], [], set()
    metadata = {}
    fields = {"path", "sha256", "format", "encoding", "byte_length", "bytes"}
    try:
        with open_stream() as stream:
            for event, value in ijson.basic_parse(stream, use_float=True):
                if event in ("start_map", "start_array"):
                    stack.append({"kind": event, "key": None, "keys": set(), "fields": {}})
                elif event == "map_key":
                    frame = stack[-1]
                    require(value not in frame["keys"], "Duplicate JSON key in snapshot.", "invalid_archive")
                    frame["keys"].add(value)
                    frame["key"] = value
                elif event in ("end_map", "end_array"):
                    frame = stack.pop()
                    ref = frame["fields"]
                    if isinstance(ref.get("path"), str) and isinstance(ref.get("sha256"), str):
                        identity = (ref["path"], ref["sha256"])
                        if identity not in identities:
                            identities.add(identity)
                            references.append(ref)
                    if stack:
                        stack[-1]["key"] = None
                elif stack:
                    frame = stack[-1]
                    key = frame["key"]
                    if key in fields:
                        frame["fields"][key] = value
                    if len(stack) == 1 and key in ("course_id", "title", "state_revision", "schema_version"):
                        metadata[key] = value
                    frame["key"] = None
    except (ValueError, TypeError, IndexError, ijson.JSONError) as exc:
        raise LearnerError("invalid_archive", "A course snapshot cannot be parsed safely.") from exc
    return metadata, references


def _reachable_refs(snapshot, directory):
    _, pending = _snapshot_details(lambda: snapshot.open("rb"))
    seen = set()
    while pending:
        ref = pending.pop()
        identity = (ref["path"], ref["sha256"])
        if identity in seen:
            continue
        seen.add(identity)
        yield ref
        if ref.get("format") == "json":
            path = _file(directory, ref["path"])
            require(_sha(path) == ref["sha256"], "Receipt body failed integrity verification.", "artifact_corrupt")
            _, nested = _snapshot_details(lambda: path.open("rb"))
            pending.extend(nested)
def _load(library, course_id):
    return getattr(library, "load_manifest", library.load)(course_id)


def _index(library):
    path = library.root / INDEX_NAME
    value = read_json(path) if path.exists() else {"schema_version": 1, "courses": {}}
    require(value.get("schema_version") == 1 and isinstance(value.get("courses"), dict),
            "Archive index is invalid; preserve it for recovery.", "invalid_archive_index")
    return value


def _ordinary_local_archive(path):
    """Inspect metadata without traversing links, cloud placeholders or shares."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        get_drive_type = ctypes.WinDLL("kernel32", use_last_error=True).GetDriveTypeW
        get_drive_type.argtypes = [wintypes.LPCWSTR]
        get_drive_type.restype = wintypes.UINT
        # Mapped network drives do not necessarily use UNC syntax. Unknown or
        # unavailable roots also cannot establish an ordinary local copy.
        if get_drive_type(path.anchor) not in (2, 3, 5, 6):
            return False
    try:
        # Inspect ancestors first: lstat on the leaf alone would still follow a
        # parent junction/symlink and could contact a remote target.
        for component in (*reversed(path.parents), path):
            details = component.lstat()
            attributes = getattr(details, "st_file_attributes", 0)
            if (stat.S_ISLNK(details.st_mode)
                    or attributes & (0x400 | 0x1000 | 0x40000 | 0x400000)):
                return False
        return stat.S_ISREG(details.st_mode)
    except OSError:
        return False


def archive_availability(entry):
    """Describe retrieval needs without opening a ZIP or accessing Drive paths.

    Local existence is only a hint: restore_archive must still verify the ZIP
    and every referenced member. Never persist this transient filesystem view
    or upgrade the index's cloud-sync/provenance declaration.
    """
    drive_roots = [Path(os.path.normpath(path)).expanduser().absolute().parent
                   for path in entry.get("drive_paths", []) if isinstance(path, str)]
    local = []
    for key in ("retrieved_path", "archive_path"):
        value = entry.get(key)
        if not isinstance(value, str) or not value:
            continue
        path = Path(os.path.normpath(value)).expanduser()
        if (not path.is_absolute() or str(path).startswith(("\\\\", "//"))
                or any(path.is_relative_to(root) for root in drive_roots)):
            continue
        if _ordinary_local_archive(path):
            if str(path) not in local:
                local.append(str(path))
    available = bool(local)
    return {
        "restore_status": "local_copy_requires_verification" if available else "download_required",
        "network_required": not available,
        "local_archive_paths": local,
        "local_archive_integrity_verified": False,
        "restore_message": (
            "本地归档副本存在，核验后可离线恢复。"
            if available else
            "未找到已登记的本地归档，需要联网取回完整归档后再校验恢复；"
            "若已有其他本地归档，可直接选择并校验恢复。"
        ),
    }


def archived_courses(library):
    """Return lightweight records plus current local retrieval requirements."""
    library.check()
    return [{**entry, **archive_availability(entry)} for entry in _index(library)["courses"].values()]


def _write_index(library, value):
    atomic_write(library.root / INDEX_NAME, canonical(value).encode("utf-8"))


def _copy(source, destination, replace=True):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".pending-archive-", dir=destination.parent)
    try:
        with os.fdopen(fd, "wb") as output, Path(source).open("rb") as incoming:
            shutil.copyfileobj(incoming, output, CHUNK)
            output.flush()
            os.fsync(output.fileno())
        if replace:
            os.replace(temporary, destination)
        elif os.name == "nt":
            # Windows rename refuses an existing target; os.replace does not.
            os.rename(temporary, destination)
        else:
            os.link(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def create_archive(library, course_id, output_path):
    """Mark an in-flight export so another process cannot release its inputs."""
    directory = library.course_dir(course_id)
    task = directory / (".archive-running-" + uuid.uuid4().hex + ".json")
    with file_lock(directory / ".lock"):
        _load(library, course_id)
        atomic_write(task, canonical({"status": "running", "pid": os.getpid(), "started_at": now()}).encode("utf-8"))
    try:
        return _create_archive(library, course_id, output_path)
    finally:
        task.unlink(missing_ok=True)


def _create_archive(library, course_id, output_path):
    """Capture metadata under a short lock, then stream immutable artifacts to ZIP."""
    directory = library.course_dir(course_id)
    output = Path(output_path).expanduser().absolute()
    require(not output.exists() and not output.with_suffix(output.suffix + ".sha256").exists(),
            "Choose a new archive path; archives are immutable.", "archive_exists")
    require(not output.resolve().is_relative_to(library.root.resolve()),
            "Store archives outside the active library.", "invalid_path")
    _require_disk_space(library.root.parent, DISK_RESERVE_BYTES, "archive staging")
    _require_disk_space(output.parent, DISK_RESERVE_BYTES, "archive publication")
    output.parent.mkdir(parents=True, exist_ok=True)
    # Snapshots stay on the course filesystem, enabling short-lock hard links.
    with tempfile.TemporaryDirectory(prefix=".learner-archive-", dir=library.root.parent) as work:
        stage = Path(work)
        snapshots, artifacts, opaque_originals = {}, {}, set()
        with file_lock(directory / ".lock"):
            state = _load(library, course_id)
            snapshot_paths = [directory / "course.json"]
            if (directory / "course.previous.json").exists():
                snapshot_paths.append(directory / "course.previous.json")
            snapshot_paths.extend(sorted(directory.glob("before-restore-*.json")))
            snapshot_paths.extend(sorted((directory / "migration-backups").rglob("*.json")))
            for path in snapshot_paths:
                relative = path.relative_to(directory).as_posix()
                path = _file(directory, relative)
                captured = stage / "snapshots" / relative
                captured.parent.mkdir(parents=True, exist_ok=True)
                try:
                    os.link(path, captured)
                except OSError:
                    # Filesystems without hard links still preserve consistency.
                    _require_disk_space(stage, path.stat().st_size + DISK_RESERVE_BYTES,
                                        "the fallback snapshot copy")
                    _copy(path, captured)
                snapshots[relative] = captured
            title, revision, schema = state["title"], state["state_revision"], state["schema_version"]
        for relative, captured in snapshots.items():
            try:
                references = list(_reachable_refs(captured, directory))
            except LearnerError as exc:
                if relative.startswith("before-restore-") and exc.code in ("invalid_archive", "archive_capacity"):
                    # Storage deliberately retains malformed original bytes.
                    # Preserve them as evidence, never treat them as live state.
                    opaque_originals.add(relative)
                    continue
                raise
            for ref in references:
                name = _managed(ref["path"])
                old = artifacts.get(name)
                require(old is None or old[1] == ref["sha256"],
                        "Snapshots disagree about an immutable artifact.", "artifact_conflict")
                artifacts[name] = (_file(directory, name), ref["sha256"])
        files = {}
        for relative, path in snapshots.items():
            files[relative] = {"sha256": _sha(path), "bytes": path.stat().st_size}
            if relative in opaque_originals:
                files[relative]["opaque_preserved_original"] = True
        for relative, (path, expected) in artifacts.items():
            require(_sha(path) == expected, "Course artifact hash does not match its reference.", "artifact_corrupt")
            files[relative] = {"sha256": expected, "bytes": path.stat().st_size}
        manifest = {"format": FORMAT, "archive_version": 1, "course_id": course_id,
                    "title": title, "state_revision": revision, "course_schema": schema,
                    "created_at": now(), "files": files}
        manifest_data = canonical(manifest).encode("utf-8")
        # DEFLATE can slightly expand incompressible input. Include conservative
        # compression, ZIP headers/names, manifest, and filesystem headroom.
        raw_bytes = sum(entry["bytes"] for entry in files.values())
        zip_bytes = (raw_bytes + (raw_bytes + 99) // 100 + len(manifest_data)
                     + sum(2 * len(("course/" + name).encode("utf-8")) + 512 for name in files))
        same_volume = stage.stat().st_dev == output.parent.stat().st_dev
        # Publication keeps the temporary ZIP while copying the destination ZIP.
        _require_disk_space(stage, zip_bytes * (2 if same_volume else 1) + DISK_RESERVE_BYTES,
                            "the temporary and destination archive copies" if same_volume else "the temporary archive")
        if not same_volume:
            _require_disk_space(output.parent, zip_bytes + DISK_RESERVE_BYTES, "the destination archive copy")
        partial = stage / "archive.zip"
        with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
            archive.writestr("MANIFEST.json", manifest_data)
            for relative in sorted(files):
                path = snapshots.get(relative) or artifacts[relative][0]
                archive.write(path, "course/" + relative)
        result = inspect_archive(partial)
        _require_disk_space(output.parent, partial.stat().st_size + DISK_RESERVE_BYTES,
                            "archive publication")
        _copy(partial, output, replace=False)
        atomic_write(output.with_suffix(output.suffix + ".sha256"),
                     (result["sha256"] + "  " + output.name + "\n").encode("utf-8"))
    return {"path": str(output), "sha256": result["sha256"], "course_id": course_id,
            "state_revision": revision, "files": len(files), "status": "local_archive_verified"}


def inspect_archive(path, expected_sha256=None):
    """Stream all members and verify the closed manifest, without extracting files."""
    path = Path(path).expanduser().absolute()
    require(path.is_file() and not path.is_symlink(), "Archive file is unavailable.", "archive_missing")
    checksum = _sha(path)
    if expected_sha256 is not None:
        require(checksum == expected_sha256, "Archive SHA-256 does not match.", "archive_corrupt")
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            names = [entry.filename for entry in entries]
            require(len(names) == len(set(names)) and len(names) <= MAX_ARCHIVE_FILES,
                    "Archive has duplicate or excessive members.", "invalid_archive")
            require("MANIFEST.json" in names and archive.getinfo("MANIFEST.json").file_size <= MAX_MANIFEST_BYTES,
                    "Archive manifest is missing or too large.", "invalid_archive")
            manifest = json.loads(archive.read("MANIFEST.json"))
            require(isinstance(manifest, dict) and manifest.get("format") == FORMAT
                    and manifest.get("archive_version") == 1, "Unsupported archive format.", "invalid_archive")
            identifier(manifest.get("course_id"))
            files = manifest.get("files")
            require(isinstance(files, dict) and "course.json" in files,
                    "Archive has no course state.", "invalid_archive")
            require(set(names) == {"MANIFEST.json", *("course/" + name for name in files)},
                    "Archive members differ from the closed manifest.", "invalid_archive")
            require(sum(entry.file_size for entry in entries) <= MAX_EXPANDED_BYTES,
                    "Archive exceeds the expanded size limit.", "archive_too_large")
            for relative, expected in files.items():
                _managed(relative)
                require(not expected.get("opaque_preserved_original")
                        or ("/" not in relative and relative.startswith("before-restore-") and relative.endswith(".json")),
                        "Only preserved damaged originals may be opaque.", "invalid_archive")
                entry = archive.getinfo("course/" + relative)
                require(not entry.is_dir() and not stat.S_ISLNK(entry.external_attr >> 16),
                        "Archive members must be regular files.", "invalid_archive")
                require(isinstance(expected, dict) and type(expected.get("bytes")) is int
                        and expected["bytes"] == entry.file_size,
                        "Archive member size mismatch.", "archive_corrupt")
                digest = hashlib.sha256()
                with archive.open(entry) as stream:
                    for chunk in iter(lambda: stream.read(CHUNK), b""):
                        digest.update(chunk)
                require(digest.hexdigest() == expected.get("sha256"),
                        "Archive member SHA-256 mismatch.", "archive_corrupt")
            def details(relative):
                return _snapshot_details(lambda: archive.open("course/" + relative))

            state, _ = details("course.json")
            require(state.get("course_id") == manifest["course_id"]
                    and state.get("state_revision") == manifest.get("state_revision")
                    and state.get("schema_version") == manifest.get("course_schema"),
                    "Course snapshot does not match its manifest.", "invalid_archive")
            snapshots = [name for name in files if name in ("course.json", "course.previous.json")
                         or name.startswith("migration-backups/") or name.startswith("before-restore-")]
            snapshots = [name for name in snapshots if not files[name].get("opaque_preserved_original")]
            pending = [ref for name in snapshots for ref in details(name)[1]]
            seen = set()
            while pending:
                ref = pending.pop()
                require(ref["path"] in files and files[ref["path"]]["sha256"] == ref["sha256"],
                        "Archive lacks a referenced course artifact.", "archive_incomplete")
                if ref.get("format") == "json" and ref["path"] not in seen:
                    seen.add(ref["path"])
                    pending.extend(details(ref["path"])[1])
    except (zipfile.BadZipFile, UnicodeError, ValueError, KeyError, TypeError, AttributeError, RuntimeError) as exc:
        raise LearnerError("invalid_archive", "Archive is malformed; no course was restored.") from exc
    return {"path": str(path), "sha256": checksum, "manifest": manifest, "status": "archive_verified"}


def copy_to_drive(archive_path, drive_directory):
    """Copy to a user-selected sync folder. This proves only local copying."""
    checked = inspect_archive(archive_path)
    source = Path(checked["path"])
    destination = Path(drive_directory).expanduser().absolute() / source.name
    require(destination.resolve() != source.resolve(), "Drive copy must be a different file.", "invalid_path")
    if destination.exists():
        require(_sha(destination) == checked["sha256"], "Drive destination already has different data.", "archive_exists")
    else:
        _require_disk_space(destination.parent, source.stat().st_size + DISK_RESERVE_BYTES,
                            "the local Drive-folder copy (cloud quota is unverified)")
        _copy(source, destination, replace=False)
    require(_sha(destination) == checked["sha256"], "Local Drive-folder copy failed verification.", "archive_corrupt")
    atomic_write(destination.with_suffix(destination.suffix + ".sha256"),
                 (checked["sha256"] + "  " + destination.name + "\n").encode("utf-8"))
    tracking_path = source.with_suffix(source.suffix + ".drive-copy.json")
    tracking = read_json(tracking_path) if tracking_path.exists() else {"sha256": checked["sha256"], "directories": []}
    require(tracking.get("sha256") == checked["sha256"] and isinstance(tracking.get("directories"), list),
            "Existing Drive-copy receipt does not match this archive.", "archive_conflict")
    tracking["directories"] = sorted(set(tracking["directories"]) | {str(destination.parent.resolve())})
    tracking["status"] = "sync_unconfirmed"
    atomic_write(tracking_path, canonical(tracking).encode("utf-8"))
    return {"status": "sync_unconfirmed", "path": str(destination), "sha256": checked["sha256"],
            "cloud_upload_verified": False, "message": "Only the local folder copy was verified; download from Drive separately."}


def confirm_download(download_path, expected_sha256, *, source_declaration, archive_path, forbidden_paths=()):
    """Verify a separate package after the user explicitly declares a web download.

Hashes prove package identity, not network provenance. The receipt keeps that
distinction explicit and must not be synthesized from a sync-folder/cache copy.
"""
    require(source_declaration == "web_download", "Explicitly declare a fresh web download.", "download_declaration_required")
    download = Path(download_path).expanduser().absolute()
    original = Path(archive_path).expanduser().absolute()
    forbidden_paths = list(forbidden_paths)
    tracking_path = original.with_suffix(original.suffix + ".drive-copy.json")
    if tracking_path.exists():
        tracking = read_json(tracking_path)
        require(tracking.get("sha256") == expected_sha256 and isinstance(tracking.get("directories"), list),
                "Drive-copy receipt does not match the expected archive.", "archive_conflict")
        forbidden_paths.extend(tracking["directories"])
    for forbidden in (original, *map(Path, forbidden_paths)):
        forbidden = forbidden.expanduser().absolute()
        require(download.resolve() != forbidden.resolve()
                and not (forbidden.is_dir() and download.resolve().is_relative_to(forbidden.resolve()))
                and not (download.exists() and forbidden.is_file() and os.path.samefile(download, forbidden)),
                "A sync-folder, original, or cache file is not an independent web download.", "download_not_independent")
    checked = inspect_archive(download, expected_sha256)
    return {"status": "download_verified", "source_declaration": source_declaration,
            "network_provenance": "user_declared", "path": str(download), "original_path": str(original),
            "forbidden_paths": [str(Path(path).expanduser().absolute()) for path in forbidden_paths],
            "sha256": checked["sha256"], "course_id": checked["manifest"]["course_id"], "verified_at": now()}


def _unfinished(state, running_tasks):
    pending = [sid for sid, sub in state.get("submissions", {}).items() if sub.get("status") != "assessed"]
    latest = {}
    for assessment in state.get("assessments", {}).values():
        sid = assessment.get("submission_id")
        if sid not in latest or assessment.get("recorded_revision", 0) > latest[sid].get("recorded_revision", 0):
            latest[sid] = assessment
    pending.extend(sid for sid, assessment in latest.items() if assessment.get("invalidated_checks"))
    require(not pending, "Finish pending assessment or reassessment before releasing local files.", "pending_work")
    tasks = list(running_tasks or [])
    for key in ("tasks", "jobs", "active_tasks", "running_tasks"):
        values = state.get(key, [])
        tasks.extend(values.values() if isinstance(values, dict) else values if isinstance(values, list) else [])
    require(not any(not isinstance(task, dict) or task.get("status") not in
                    ("completed", "succeeded", "failed", "cancelled", "expired") for task in tasks),
            "A running task prevents release of local course files.", "running_tasks")
    for source in state.get("sources", {}).values():
        status = source.get("parse_status")
        if isinstance(status, dict):
            require(status.get("complete") is not False,
                    "Source parsing checkpoint is unfinished.", "running_tasks")
        status = status.get("status") if isinstance(status, dict) else status
        require(status not in ("pending", "queued", "running", "processing", "paused", "incomplete", "interrupted"),
                "Finish or explicitly cancel the source parsing task before release.", "running_tasks")


def release_course(library, course_id, archive_path, verification, *, confirmed=False, running_tasks=()):
    """Explicitly release only fully archived files after independent retrieval."""
    require(confirmed, "Explicit confirmation is required to release local course files.", "confirmation_required")
    checked = inspect_archive(archive_path)
    require(isinstance(verification, dict) and verification.get("status") == "download_verified"
            and verification.get("source_declaration") == "web_download", "Verify an independent web download first.", "download_required")
    confirm_download(verification.get("path", ""), checked["sha256"], source_declaration="web_download",
                     archive_path=archive_path, forbidden_paths=verification.get("forbidden_paths", []))
    manifest = checked["manifest"]
    require(manifest["course_id"] == course_id and verification.get("course_id") == course_id,
            "Archive belongs to a different course.", "invalid_archive")
    archive_file = Path(archive_path).expanduser().absolute()
    tracking_path = archive_file.with_suffix(archive_file.suffix + ".drive-copy.json")
    drive_paths = []
    if tracking_path.exists():
        tracking = read_json(tracking_path)
        require(tracking.get("sha256") == checked["sha256"]
                and isinstance(tracking.get("directories"), list)
                and all(isinstance(path, str) for path in tracking["directories"]),
                "Drive-copy receipt does not match this archive.", "archive_conflict")
        drive_paths = sorted({str(Path(path) / archive_file.name) for path in tracking["directories"]})
    directory = library.course_dir(course_id)
    with file_lock(library.root / ".library.lock"), file_lock(directory / ".lock"):
        state = _load(library, course_id)
        _unfinished(state, running_tasks)
        require(not any(directory.glob(".archive-running-*.json")),
                "An archive export is running or was interrupted; finish or explicitly resolve it before release.", "running_tasks")
        require(not any(directory.glob(".task-running-*.json")),
                "A source parsing task is running or was interrupted; finish or explicitly resolve it before release.", "running_tasks")
        require(_sha(directory / "course.json") == manifest["files"]["course.json"]["sha256"],
                "Course changed after archiving; create and retrieve a new archive.", "archive_stale")
        files = {path.relative_to(directory).as_posix() for path in directory.rglob("*") if path.is_file()}
        require(files <= set(manifest["files"]) | {".lock"},
                "Course contains files not included in this archive; preserve them before release.", "unarchived_files")
        for relative, expected in manifest["files"].items():
            require(_sha(_file(directory, relative)) == expected["sha256"],
                    "A course file changed after archiving.", "archive_stale")
        index = _index(library)
        row = {"course_id": course_id, "title": state["title"], "state_revision": state["state_revision"],
               "status": "archived", "archive_sha256": checked["sha256"], "archive_path": str(Path(archive_path).absolute()),
               "retrieved_path": verification["path"], "drive_paths": drive_paths,
               "cloud_sync": "user_declared_download_verified" if drive_paths else "unknown",
               "released_at": now()}
        index["courses"][course_id] = row
        _write_index(library, index)
        # The index is committed first. Storage must reject writes to archived IDs.
        for relative in manifest["files"]:
            (directory / relative).unlink()
        for child in sorted(directory.rglob("*"), key=lambda path: len(path.parts), reverse=True):
            if child.is_dir():
                child.rmdir()
    return row


def restore_archive(library, archive_path, expected_sha256=None, *, confirmed=False):
    """Validate in a separate temporary library, then enable without overwriting."""
    require(confirmed, "Explicit selection is required to restore a course archive.", "confirmation_required")
    library.check()
    checked = inspect_archive(archive_path, expected_sha256)
    manifest = checked["manifest"]
    course_id = manifest["course_id"]
    destination = library.root / course_id
    warning = None
    expanded_bytes = sum(entry["bytes"] for entry in manifest["files"].values())
    _require_disk_space(library.root.parent,
                        expanded_bytes + (library.root / "library.json").stat().st_size + DISK_RESERVE_BYTES,
                        "isolated archive extraction")
    with tempfile.TemporaryDirectory(prefix=".learner-restore-", dir=library.root.parent) as work:
        stage_root = Path(work)
        stage_course = stage_root / course_id
        stage_course.mkdir()
        _copy(library.root / "library.json", stage_root / "library.json")
        with zipfile.ZipFile(archive_path) as archive:
            for relative in manifest["files"]:
                output = stage_course / relative
                output.parent.mkdir(parents=True, exist_ok=True)
                with archive.open("course/" + relative) as incoming, output.open("xb") as stream:
                    shutil.copyfileobj(incoming, stream, CHUNK)
                require(_sha(output) == manifest["files"][relative]["sha256"],
                        "Restored artifact did not match its manifest.", "archive_corrupt")
        _load(Library(stage_root), course_id)
        with file_lock(library.root / ".library.lock"):
            index = _index(library)
            residue = None
            if destination.exists():
                require(not destination.is_symlink() and course_id in index["courses"],
                        "Restore never overwrites an existing course; choose another library.", "course_exists")
                require(index["courses"][course_id].get("archive_sha256") == checked["sha256"],
                        "Released files belong to another archive; restore into a different library.", "archive_conflict")
                # Release may stop before or after removing course.json. The
                # committed index blocks writes, and every residue must match.
                # Only unchanged files from the same verified archive may remain.
                for path in destination.rglob("*"):
                    require(not path.is_symlink(), "Restore cannot replace symbolic links.", "invalid_path")
                    if path.is_file() and path.name != ".lock":
                        relative = path.relative_to(destination).as_posix()
                        require(relative in manifest["files"]
                                and _sha(path) == manifest["files"][relative]["sha256"],
                                "Unarchived local files must be preserved before restoring.", "unarchived_files")
                residue = stage_root / "release-residue"
                destination.rename(residue)
            enabled = False
            try:
                stage_course.rename(destination)
                enabled = True
                if course_id in index["courses"]:
                    del index["courses"][course_id]
                    _write_index(library, index)
            except OSError as exc:
                # atomic_write may have replaced the index before a later fsync
                # error. Never undo the course while its archived guard is gone.
                try:
                    committed = enabled and course_id not in _index(library)["courses"]
                except (OSError, LearnerError) as read_error:
                    raise LearnerError("commit_uncertain", "Restore may have enabled the course; inspect the index and preserved course before retrying.",
                                       course_id=course_id, path=str(destination)) from read_error
                if committed:
                    require(_sha(destination / "course.json") == manifest["files"]["course.json"]["sha256"],
                            "Restored course could not be verified after the index error.", "commit_uncertain")
                    warning = "Index write reported an error; readback confirmed that the restored course is enabled."
                else:
                    if enabled and destination.exists():
                        destination.rename(stage_course)
                    if residue is not None and residue.exists():
                        residue.rename(destination)
                    raise exc
    result = {"course_id": course_id, "state_revision": manifest["state_revision"], "status": "course_restored",
              "archive_sha256": checked["sha256"], "path": str(destination), "network_required": False}
    if warning:
        result["warning"] = warning
    return result

"""One authoritative JSON file per course, with locking and checked writes."""

import copy
import hashlib
import json
import shutil
import os
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path

from .common import LearnerError, SCHEMA_VERSION, canonical, digest, identifier, now, read_json, require, validate_state
from .blobs import (artifact_path, compact, file_sha256, lazy, materialize, read_body,
                    verify_artifacts, iter_body_refs, verify_ref)


def atomic_write(path, data):
    """同目录临时文件、刷盘、替换；不把失败的临时文件当作正式记录。"""
    path = Path(path)
    fd, temp = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            if isinstance(data, bytes):
                stream.write(data)
            else:
                for chunk in data:
                    stream.write(chunk)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        if os.name != "nt":
            directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def atomic_write_json(path, value):
    encoder = json.JSONEncoder(ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    atomic_write(path, (fragment.encode("utf-8") for fragment in encoder.iterencode(value)))


def atomic_copy(source, destination):
    def chunks():
        with open(source, "rb") as stream:
            yield from iter(lambda: stream.read(1024 * 1024), b"")
    atomic_write(destination, chunks())


@contextmanager
def file_lock(path):
    """只锁住本地短事务；锁忙时不等待模型或另一个会话。"""
    with open(path, "a+b") as stream:
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise LearnerError("busy", "Course is being written; retry the unchanged request.") from exc
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def task_owner_alive(task):
    import psutil
    pid = task.get("pid")
    require(type(pid) is int and pid > 0, "Task marker has no valid owner.", "invalid_task")
    try:
        process = psutil.Process(pid)
        return (task.get("process_created_at") is None
                or process.create_time() == task["process_created_at"])
    except psutil.NoSuchProcess:
        return False
    except psutil.AccessDenied:
        return True


@contextmanager
def course_task(library, course_id, operation_id):
    """A parser runs outside the transaction lock, with an eviction guard."""
    import psutil
    directory = library.course_dir(course_id)
    marker = directory / (".task-running-" + identifier(operation_id) + ".json")
    with file_lock(directory / ".lock"):
        library.load_manifest(course_id)
        if marker.exists():
            require(not task_owner_alive(read_json(marker)), "This import is already running.", "busy")
        owner = {"pid": os.getpid(), "process_created_at": psutil.Process().create_time(),
                 "operation_id": operation_id, "status": "running", "started_at": now()}
        atomic_write_json(marker, owner)
    try:
        yield
    finally:
        if marker.exists() and read_json(marker) == owner:
            marker.unlink()


def cleanup_task(library, course_id, name, confirmed=False):
    require(confirmed, "Explicitly select an interrupted task marker to clear.", "confirmation_required")
    require(isinstance(name, str) and Path(name).name == name and name.endswith(".json")
            and name.startswith((".task-running-", ".archive-running-")), "Unknown task marker.", "invalid_task")
    directory = library.course_dir(course_id)
    with file_lock(directory / ".lock"):
        path = directory / name
        require(path.is_file() and not path.is_symlink(), "Task marker is missing.", "invalid_task")
        require(not task_owner_alive(read_json(path)), "Task owner is still running; no marker was removed.", "busy")
        path.unlink()
    return {"course_id": course_id, "cleared_task": name, "course_and_checkpoints_preserved": True}


def read_receipt(state, operation_id, directory):
    receipt = materialize(state["operations"][operation_id]["receipt"], directory)
    require(isinstance(receipt, dict) and receipt.get("course_id") == state["course_id"]
            and receipt.get("operation_id") == operation_id
            and type(receipt.get("state_revision")) is int
            and 1 <= receipt["state_revision"] <= state["state_revision"],
            "Saved receipt identity or revision does not match its operation.", "artifact_corrupt")
    return receipt


class Library:
    def __init__(self, root):
        path = Path(root).expanduser()
        require(path.is_absolute(), "The library path must be absolute.", "invalid_library")
        self.root = path.resolve()

    def initialize(self):
        self.root.mkdir(parents=True, exist_ok=True)
        marker = self.root / "library.json"
        with file_lock(self.root / ".library.lock"):
            if marker.exists():
                self.check()
                return {"library": str(self.root), "created": False}
            require(not any(p.name != ".library.lock" for p in self.root.iterdir()),
                    "Refusing to initialize a nonempty, unrecognized library.", "invalid_library")
            atomic_write(marker, canonical({"schema_version": 1, "library_id": str(uuid.uuid4()),
                                            "created_at": now()}).encode("utf-8"))
        return {"library": str(self.root), "created": True}

    def check(self):
        try:
            marker = read_json(self.root / "library.json")
        except FileNotFoundError as exc:
            raise LearnerError("library_missing", f"Configured library is unavailable: {self.root}",
                               path=str(self.root)) from exc
        require(isinstance(marker, dict) and marker.get("schema_version") == 1
                and isinstance(marker.get("library_id"), str),
                "Unsupported library marker.", "unsupported_schema")

    def course_dir(self, course_id):
        self.check()
        path = self.root / identifier(course_id)
        require(not path.is_symlink(), "Course directories cannot be symbolic links.", "invalid_path")
        return path

    def archived_entry(self, course_id):
        path = self.root / ".archive-index.json"
        if not path.exists():
            return None
        index = read_json(path)
        return index.get("courses", {}).get(course_id)

    def load_manifest(self, course_id):
        directory = self.course_dir(course_id)
        path = directory / "course.json"
        require(not self.archived_entry(course_id), "Historical course requires archive restoration.", "course_archived")
        require(not path.is_symlink(), "Course state cannot be a symbolic link.", "invalid_path")
        try:
            # v1 can contain arbitrarily repeated bodies. Large libraries must
            # use the incremental migration reader before normal access.
            require(path.stat().st_size <= 64 * 1024 * 1024,
                    "Manifest exceeds the safe reader budget; run migrate --preview.", "migration_required")
            return validate_state(read_json(path), course_id)
        except FileNotFoundError as exc:
            if self.archived_entry(course_id):
                raise LearnerError("course_archived", "Historical course requires archive restoration.",
                                   course_id=course_id) from exc
            raise LearnerError("course_missing", f"Course state is missing: {path}",
                               path=str(path), backup=str(directory / "course.previous.json")) from exc

    def load(self, course_id):
        return lazy(self.load_manifest(course_id), self.course_dir(course_id))

    def list_courses(self):
        self.check()
        courses = []
        for directory in sorted(self.root.iterdir()):
            if not directory.is_dir() or directory.name.startswith("."):
                continue
            try:
                state = self.load(directory.name)
                courses.append({"course_id": state["course_id"], "title": state["title"],
                                "state_revision": state["state_revision"], "position": state["position"]})
            except (LearnerError, OSError) as exc:
                archived = self.archived_entry(directory.name)
                courses.append({"course_id": directory.name, "status": "archived", **archived} if archived else
                               {"course_id": directory.name,
                                "error": exc.as_dict() if isinstance(exc, LearnerError) else str(exc)})
        return courses

    def transact(self, request, mutate, create=False):
        operation_id = identifier(request.get("operation_id"))
        directory = self.course_dir(request.get("course_id"))
        fingerprint = digest(request)
        require(not self.archived_entry(request["course_id"]),
                "Archived course must be restored, never recreated.", "course_archived")
        if create:
            directory.mkdir(exist_ok=True)
        require(directory.is_dir(), "Unknown course; no implicit creation.", "course_missing")
        with file_lock(directory / ".lock"):
            require(not self.archived_entry(request["course_id"]),
                    "Historical course requires archive restoration.", "course_archived")
            state_path = directory / "course.json"
            if create and not state_path.exists():
                # 残留课程目录可能包含已丢失的记录，不能据此创建空课程。
                require(not any(p.name != ".lock" for p in directory.iterdir()),
                        "Course directory already contains data.", "course_exists")
                old = None
                state = {"schema_version": SCHEMA_VERSION, "course_id": request["course_id"], "state_revision": 0,
                         "created_at": now(), "title": "", "goals": "", "chapter_order": [], "position": None,
                         **{key: {} for key in ("chapters", "sources", "questions", "submissions",
                                                "assessments", "help_events", "operations", "source_verifications")}}
            else:
                old = self.load(request["course_id"])
                existing = old["operations"].get(operation_id)
                if existing:
                    require(existing["fingerprint"] == fingerprint,
                            "Operation ID already has different data; preserve this request and use a new ID.",
                            "id_conflict")
                    return {**read_receipt(old, operation_id, directory), "duplicate": True}
                require(not create, "Course already exists.", "course_exists")
                require(old["schema_version"] == SCHEMA_VERSION,
                        "Schema 1 is read-only; preview and explicitly migrate first.", "migration_required")
                # Before rotating the last valid snapshot, stream-verify its
                # immutable closure. This costs disk I/O, not body-sized RAM.
                verify_artifacts(directory, old)
                state = copy.deepcopy(old)
            state["state_revision"] += 1
            result = mutate(state, directory)
            state["updated_at"] = now()
            receipt = {"course_id": state["course_id"], "operation_id": operation_id,
                       "state_revision": state["state_revision"], "recorded_at": state["updated_at"],
                       "result": result, "duplicate": False}
            state["operations"][operation_id] = {"fingerprint": fingerprint, "receipt": receipt}
            old_refs = {(ref["path"], ref["sha256"]) for ref in iter_body_refs(old)} if old else set()
            state = compact(state, directory)
            new_refs = [ref for ref in iter_body_refs(state) if (ref["path"], ref["sha256"]) not in old_refs]
            validate_state(state, request["course_id"])
            if old is not None:
                atomic_write_json(directory / "course.previous.json", old)
            atomic_write_json(state_path, state)
            try:
                persisted = self.load(request["course_id"])
                require(persisted["operations"].get(operation_id) == state["operations"][operation_id],
                        "Operation could not be verified after replacement.", "commit_uncertain")
                for ref in new_refs:
                    verify_ref(directory, ref)
                persisted_receipt = read_receipt(persisted, operation_id, directory)
                require(persisted_receipt == materialize(receipt, directory),
                        "Saved receipt does not match this operation result.", "commit_uncertain")
            except (LearnerError, OSError) as exc:
                raise LearnerError("commit_uncertain", "The write may have succeeded; retry the same operation ID.",
                                   operation_id=operation_id) from exc
            return persisted_receipt

    def backup_info(self, course_id):
        directory = self.course_dir(course_id)
        path = directory / "course.previous.json"
        require(not self.archived_entry(course_id), "Restore the archived course first.", "course_archived")
        require(path.stat().st_size <= 64 * 1024 * 1024,
                "Legacy backup exceeds safe reader capacity; use the preserved migration snapshot.", "migration_capacity")
        state = validate_state(read_json(path), course_id)
        verify_artifacts(directory, state)
        return {"path": str(path), "sha256": file_sha256(path),
                "state_revision": state["state_revision"], "updated_at": state["updated_at"]}

    def restore(self, course_id, expected_sha256, confirmed=False):
        require(confirmed, "Explicit user selection is required to restore a backup.", "confirmation_required")
        directory = self.course_dir(course_id)
        with file_lock(directory / ".lock"):
            info = self.backup_info(course_id)
            require(info["sha256"] == expected_sha256, "Backup changed; inspect it again.", "backup_changed")
            target = directory / "course.json"
            preserved = None
            if target.exists():
                preserved = directory / f"before-restore-{uuid.uuid4().hex}.json"
                atomic_copy(target, preserved)
            atomic_copy(directory / "course.previous.json", target)
            restored = self.load(course_id)
            return {**info, "course_id": course_id, "restored_revision": restored["state_revision"],
                    "preserved_original": str(preserved) if preserved else None,
                    "warning": "Records after the backup timestamp may be missing."}


def write_artifact(directory, relative_path, data):
    """Immutable small bodies; large originals use write_artifact_from_file."""
    path = artifact_path(directory, relative_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    hash_value = hashlib.sha256(data).hexdigest()
    if path.exists():
        require(path.stat().st_size == len(data) and file_sha256(path) == hash_value,
                "An immutable artifact already has different contents.", "artifact_conflict")
    else:
        atomic_write(path, data)
    return {"path": str(relative_path), "sha256": hash_value, "byte_length": len(data)}


def write_artifact_from_file(directory, relative_path, source_path):
    path = artifact_path(directory, relative_path)
    source_path = Path(source_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    hash_value = file_sha256(source_path)
    size = source_path.stat().st_size
    if path.exists():
        require(path.stat().st_size == size and file_sha256(path) == hash_value,
                "An immutable artifact already has different contents.", "artifact_conflict")
    else:
        atomic_copy(source_path, path)
        require(path.stat().st_size == size and file_sha256(path) == hash_value,
                "Source changed during copy.", "artifact_corrupt")
    return {"path": str(relative_path), "sha256": hash_value, "byte_length": size}

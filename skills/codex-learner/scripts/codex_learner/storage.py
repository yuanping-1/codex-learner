"""One authoritative JSON file per course, with locking and checked writes."""

import copy
import hashlib
import os
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path

from .common import LearnerError, canonical, digest, identifier, now, read_json, require, validate_state


def atomic_write(path, data):
    """同目录临时文件、刷盘、替换；不把失败的临时文件当作正式记录。"""
    path = Path(path)
    fd, temp = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
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

    def load(self, course_id):
        directory = self.course_dir(course_id)
        path = directory / "course.json"
        require(not path.is_symlink(), "Course state cannot be a symbolic link.", "invalid_path")
        try:
            return validate_state(read_json(path), course_id)
        except FileNotFoundError as exc:
            raise LearnerError("course_missing", f"Course state is missing: {path}",
                               path=str(path), backup=str(directory / "course.previous.json")) from exc

    def list_courses(self):
        self.check()
        courses = []
        for directory in sorted(self.root.iterdir()):
            if not directory.is_dir():
                continue
            try:
                state = self.load(directory.name)
                courses.append({"course_id": state["course_id"], "title": state["title"],
                                "state_revision": state["state_revision"], "position": state["position"]})
            except (LearnerError, OSError) as exc:
                courses.append({"course_id": directory.name,
                                "error": exc.as_dict() if isinstance(exc, LearnerError) else str(exc)})
        return courses

    def transact(self, request, mutate, create=False):
        operation_id = identifier(request.get("operation_id"))
        directory = self.course_dir(request.get("course_id"))
        fingerprint = digest(request)
        if create:
            directory.mkdir(exist_ok=True)
        require(directory.is_dir(), "Unknown course; no implicit creation.", "course_missing")
        with file_lock(directory / ".lock"):
            state_path = directory / "course.json"
            if create and not state_path.exists():
                # 残留课程目录可能包含已丢失的记录，不能据此创建空课程。
                require(not any(p.name != ".lock" for p in directory.iterdir()),
                        "Course directory already contains data.", "course_exists")
                old = None
                state = {"schema_version": 1, "course_id": request["course_id"], "state_revision": 0,
                         "created_at": now(), "title": "", "goals": "", "chapter_order": [], "position": None,
                         **{key: {} for key in ("chapters", "sources", "questions", "submissions",
                                                "assessments", "help_events", "operations")}}
            else:
                old = self.load(request["course_id"])
                existing = old["operations"].get(operation_id)
                if existing:
                    require(existing["fingerprint"] == fingerprint,
                            "Operation ID already has different data; preserve this request and use a new ID.",
                            "id_conflict")
                    return {**existing["receipt"], "duplicate": True}
                require(not create, "Course already exists.", "course_exists")
                state = copy.deepcopy(old)
            state["state_revision"] += 1
            result = mutate(state, directory)
            state["updated_at"] = now()
            receipt = {"course_id": state["course_id"], "operation_id": operation_id,
                       "state_revision": state["state_revision"], "recorded_at": state["updated_at"],
                       "result": result, "duplicate": False}
            state["operations"][operation_id] = {"fingerprint": fingerprint, "receipt": receipt}
            validate_state(state, request["course_id"])
            if old is not None:
                atomic_write(directory / "course.previous.json", canonical(old).encode("utf-8"))
            atomic_write(state_path, canonical(state).encode("utf-8"))
            try:
                persisted = self.load(request["course_id"])
                require(persisted["operations"].get(operation_id) == state["operations"][operation_id],
                        "Operation could not be verified after replacement.", "commit_uncertain")
            except (LearnerError, OSError) as exc:
                raise LearnerError("commit_uncertain", "The write may have succeeded; retry the same operation ID.",
                                   operation_id=operation_id) from exc
            return receipt

    def backup_info(self, course_id):
        directory = self.course_dir(course_id)
        path = directory / "course.previous.json"
        state = validate_state(read_json(path), course_id)
        return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
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
                atomic_write(preserved, target.read_bytes())
            atomic_write(target, (directory / "course.previous.json").read_bytes())
            restored = self.load(course_id)
            return {**info, "course_id": course_id, "restored_revision": restored["state_revision"],
                    "preserved_original": str(preserved) if preserved else None,
                    "warning": "Records after the backup timestamp may be missing."}


def write_artifact(directory, relative_path, data):
    """不可变来源与讲义；中断时允许保留未引用文件。"""
    path = directory / relative_path
    require(path.resolve().is_relative_to(directory.resolve()), "Artifact escaped course directory.", "invalid_path")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        require(path.read_bytes() == data, "An immutable artifact already has different contents.", "artifact_conflict")
    else:
        atomic_write(path, data)
    return {"path": relative_path, "sha256": hashlib.sha256(data).hexdigest()}

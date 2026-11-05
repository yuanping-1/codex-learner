"""Opt-in restore failure observations; never imported by the product CLI.

Use ``with RestoreFailureProbe(library_root, course_id, output_dir) as probe:``
only for separately labelled diagnostic samples. A real publish rename OSError
triggers observations before that *same* exception propagates to product rollback.
No retry, lock acquisition, permission change or product-state write is performed.
All output goes outside the library. Capturing observations delays rollback and
may change timing; these samples must not enter uninstrumented first-call counts.

Tracked Python streams are weakly referenced, never wrapped or kept open. They
cover only successful builtins.open/io.open/Path.open calls during this context,
not native handles, earlier opens or other processes. Ownership/share modes are
UNKNOWN. Windows handle counts are counts only. Optional icacls runs read-only,
with a timeout, after the error; it is not a failure-time ACL snapshot.
"""

import builtins
import ctypes
import io
import json
import os
import stat
import subprocess
import time
import uuid
import weakref
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch


def _utc():
    return datetime.now(timezone.utc).isoformat()


def _error(exc):
    result = {"type": type(exc).__name__, "message": str(exc)}
    for name in ("errno", "winerror", "filename", "filename2"):
        value = getattr(exc, name, None)
        if value is not None:
            result[name] = os.fsdecode(value) if name.startswith("filename") else value
    return result


def _lexical(path):
    # Do not call arbitrary PathLike implementations twice or resolve each open.
    if not isinstance(path, (str, bytes, Path)):
        return None
    return os.path.normcase(os.path.abspath(os.fsdecode(path)))


class RestoreFailureProbe:
    """Diagnostic-only observer for one library/course's publication failures."""

    def __init__(self, library_root, course_id, output_dir, *, collect_acl=False,
                 acl_timeout_seconds=3):
        if (not isinstance(course_id, str) or not course_id or course_id in (".", "..")
                or any(char in course_id for char in ("/", "\\", ":"))):
            raise ValueError("course_id must be one plain path component")
        if acl_timeout_seconds <= 0:
            raise ValueError("ACL timeout must be positive")
        self.library_root = Path(library_root).absolute()
        resolved_root = self.library_root.resolve()
        self.output_dir = Path(output_dir).resolve()
        if self.output_dir == resolved_root or resolved_root in self.output_dir.parents:
            raise ValueError("Diagnostic output must be outside the library")
        self.course_id = course_id
        self.roots = {self.library_root, resolved_root}
        self.targets = {_lexical(root / course_id) for root in self.roots}
        self.stage_parents = {_lexical(root.parent) for root in self.roots}
        self.collect_acl = bool(collect_acl)
        self.acl_timeout_seconds = acl_timeout_seconds
        self.events = []
        self.observer_errors = []
        self.streams = {}
        self.stack = None
        self.started_at_utc = None
        self.finished_at_utc = None

    def _note_error(self, phase, exc):
        # Even logging a broken collector must not replace the product error.
        try:
            if len(self.observer_errors) < 128:
                self.observer_errors.append({"phase": phase, "error": _error(exc)})
        except BaseException:
            pass

    def _track(self, stream, file):
        try:
            identity = id(stream)
            if identity in self.streams and self.streams[identity][0]() is stream:
                return
            path = _lexical(file)
            kind = "path" if path is not None else "descriptor" if isinstance(file, int) else "custom_pathlike"
            reference = weakref.ref(stream, lambda ref: self.streams.pop(identity, None)
                                    if self.streams.get(identity, (None,))[0] is ref else None)
            self.streams[identity] = (reference, {"path": path, "input_kind": kind})
        except BaseException as exc:
            self._note_error("track_stream", exc)

    def _matches(self, source, destination):
        source_key, destination_key = _lexical(source), _lexical(destination)
        if source_key is None or destination_key not in self.targets:
            return False
        source = Path(source_key)
        return (source.name == os.path.normcase(self.course_id)
                and source.parent.name.startswith(".learner-restore-")
                and _lexical(source.parent.parent) in self.stage_parents)

    def _path_observation(self, path):
        row = {"path": str(path), "observed_at_utc": _utc(), "exists": None}
        try:
            status = path.lstat()
            attrs = getattr(status, "st_file_attributes", None)
            row.update(exists=True, mode=status.st_mode, size=status.st_size,
                       device=status.st_dev, inode=status.st_ino,
                       mtime_ns=status.st_mtime_ns, ctime_ns=status.st_ctime_ns,
                       is_directory=stat.S_ISDIR(status.st_mode),
                       is_symlink=stat.S_ISLNK(status.st_mode),
                       windows_file_attributes=attrs,
                       windows_reparse_tag=getattr(status, "st_reparse_tag", None),
                       is_reparse_point=(bool(attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 1024))
                                         if attrs is not None else None))
        except FileNotFoundError as exc:
            row.update(exists=False, observation_error=_error(exc))
        except BaseException as exc:
            row["observation_error"] = _error(exc)
        return row

    def _open_streams(self):
        rows = []
        for reference, metadata in list(self.streams.values()):
            stream = reference()
            if stream is None:
                continue
            row = dict(metadata)
            try:
                if stream.closed:
                    continue
                row["closed"] = False
                row["mode"] = getattr(stream, "mode", None)
                row["file_descriptor"] = stream.fileno()
            except BaseException as exc:
                row["observation_error"] = _error(exc)
            rows.append(row)
        return rows

    def _handle_count(self):
        result = {"status": "UNKNOWN", "count": None,
                  "scope": "Current-process OS handle count only; no path, owner or sharing mode mapping."}
        if os.name != "nt":
            result["reason"] = "Windows API unavailable"
            return result
        try:
            from ctypes import wintypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.GetCurrentProcess.argtypes = []
            kernel.GetCurrentProcess.restype = wintypes.HANDLE
            kernel.GetProcessHandleCount.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            kernel.GetProcessHandleCount.restype = wintypes.BOOL
            count = wintypes.DWORD()
            if not kernel.GetProcessHandleCount(kernel.GetCurrentProcess(), ctypes.byref(count)):
                raise ctypes.WinError(ctypes.get_last_error())
            result.update(status="OBSERVED", count=count.value)
        except BaseException as exc:
            result["observation_error"] = _error(exc)
        return result

    def _acl(self, path):
        row = {"path": str(path), "status": "UNKNOWN", "started_at_utc": _utc()}
        if not self.collect_acl:
            row["reason"] = "Optional ACL collection disabled"
        elif os.name != "nt":
            row["reason"] = "Windows icacls unavailable"
        else:
            try:
                completed = subprocess.run(["icacls", str(path)], capture_output=True,
                                           timeout=self.acl_timeout_seconds, check=False)
                # Hex preserves native console bytes without inventing an encoding.
                row.update(status="OBSERVED" if completed.returncode == 0 else "UNKNOWN",
                           returncode=completed.returncode,
                           stdout_hex=completed.stdout.hex(), stderr_hex=completed.stderr.hex())
            except BaseException as exc:
                row["observation_error"] = _error(exc)
        row["finished_at_utc"] = _utc()
        return row

    def _capture(self, source, destination, exc, raised_at_utc):
        started = time.perf_counter()
        source, destination = Path(source), Path(destination)
        row = {"schema_version": 1, "diagnostic_only": True, "pid": os.getpid(),
               "rename_error_at_utc": raised_at_utc, "capture_started_at_utc": _utc(),
               "source_path": str(source), "destination_path": str(destination),
               "original_error": _error(exc), "observations": {}, "collection_errors": [],
               "external_handle_owners": {"status": "UNKNOWN", "reason": "Not enumerated"},
               "lock_ownership": {"status": "UNKNOWN", "reason": "No lock acquisition or ownership probe performed"},
               "limits": "Observations occur after rename failed and before rollback; no atomic filesystem snapshot. Capture delays rollback and may affect timing."}
        self.events.append(row)

        def observe(name, function):
            try:
                row["observations"][name] = function()
            except BaseException as error:
                row["collection_errors"].append({"phase": name, "error": _error(error)})

        observe("cwd", os.getcwd)
        # Capture cheap facts and tracked streams before optional subprocess work.
        observe("paths", lambda: [self._path_observation(path) for path in
                                  (source, destination, source.parent, destination.parent,
                                   source / ".lock", destination / ".lock",
                                   destination.parent / ".library.lock")])
        observe("open_python_streams", self._open_streams)
        observe("windows_process_handle_count", self._handle_count)
        observe("acl", lambda: [self._acl(path) for path in
                                (source, destination, source.parent, destination.parent)])
        row["capture_finished_at_utc"] = _utc()
        row["capture_seconds"] = time.perf_counter() - started
        name = "rename-failure-" + uuid.uuid4().hex + ".json"
        row["evidence_path"] = str(self.output_dir / name)
        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            # Exclusive creation preserves old evidence even on name collisions.
            with builtins.open(self.output_dir / name, "x", encoding="utf-8", newline="\n") as stream:
                json.dump(row, stream, ensure_ascii=True, indent=2)
                stream.write("\n")
        except BaseException as error:
            row["collection_errors"].append({"phase": "write_evidence", "error": _error(error)})

    def __enter__(self):
        if self.stack is not None:
            raise RuntimeError("A RestoreFailureProbe cannot be entered twice")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.started_at_utc = _utc()
        stack = self.stack = ExitStack()
        original_rename = Path.rename

        def rename(source, target):
            try:
                return original_rename(source, target)
            except OSError as exc:
                try:
                    raised_at_utc = _utc()
                    if self._matches(source, target):
                        self._capture(source, target, exc, raised_at_utc)
                except BaseException as observation_error:
                    self._note_error("capture", observation_error)
                raise  # Propagate the exact original exception and traceback.

        def opened(original):
            def wrapper(file, *args, **kwargs):
                stream = original(file, *args, **kwargs)
                self._track(stream, file)
                return stream
            return wrapper

        try:
            # Path.open is explicit for 3.10's cached io.open entry. Duplicate
            # delegation registers the same raw stream by identity exactly once.
            for target, name in ((builtins, "open"), (io, "open"), (Path, "open")):
                stack.enter_context(patch.object(target, name, opened(getattr(target, name))))
            stack.enter_context(patch.object(Path, "rename", rename))
        except BaseException:
            stack.close()
            self.stack = None
            raise
        return self

    def __exit__(self, *args):
        try:
            return self.stack.__exit__(*args)
        finally:
            self.finished_at_utc = _utc()

    def report(self):
        return {"schema_version": 1, "diagnostic_only": True,
                "started_at_utc": self.started_at_utc, "finished_at_utc": self.finished_at_utc,
                "events": self.events, "observer_errors": self.observer_errors,
                "limits": "Timing affected; not first-call acceptance. Python streams cover opens observed in this context only; native/external handles and lock ownership remain UNKNOWN."}

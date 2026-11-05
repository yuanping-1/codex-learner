"""Opt-in, process-local benchmark probes; never imported by the product CLI."""

import builtins
import hashlib
import io
import json
import os
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import patch


class OperationProfile:
    """Inclusive phase timings and application-level I/O, not physical disk I/O."""

    def __init__(self, library):
        # Resolve only the root, before probes/timing start. Windows temp roots
        # can use an 8.3 alias while product paths contain the expanded root.
        # Per-open classification remains lexical, without resolve/stat calls.
        self.library = os.path.normcase(os.path.abspath(os.fsdecode(library)))
        self.resolved_library = os.path.normcase(os.path.abspath(os.fsdecode(Path(library).resolve())))
        self.library_prefixes = tuple(sorted({self.library, self.resolved_library}, key=len, reverse=True))
        self.phases = {}
        self.calls = {}
        self.phase_calls = {}
        self.files = {}
        self.phase_io = {}
        self.hashes = {"created": 0, "update_calls": 0, "input_bytes": 0}
        self.stack = ExitStack()
        self.active = []

    @contextmanager
    def phase(self, name):
        row = self.phases.setdefault(name, {"calls": 0, "seconds": 0.0})
        row["calls"] += 1
        self.active.append(name)
        started = time.perf_counter()
        try:
            yield
        finally:
            row["seconds"] += time.perf_counter() - started
            self.active.pop()

    def note(self, field, value=1):
        for phase in set(self.active) or {"unattributed"}:
            row = self.phase_io.setdefault(phase, {})
            row[field] = row.get(field, 0) + value

    def timed(self, name, function):
        def wrapped(*args, **kwargs):
            with self.phase(name):
                return function(*args, **kwargs)
        return wrapped

    def observed(self, name, function):
        """Time the underlying call; counters exclude this probe's bookkeeping."""
        def wrapped(*args, **kwargs):
            started = time.perf_counter()
            failed = True
            try:
                result = function(*args, **kwargs)
                failed = False
                return result
            finally:
                elapsed = time.perf_counter() - started
                groups = [self.calls]
                groups += [self.phase_calls.setdefault(phase, {})
                           for phase in set(self.active) or {"unattributed"}]
                for group in groups:
                    row = group.setdefault(name, {"calls": 0, "failures": 0, "seconds": 0.0})
                    row["calls"] += 1
                    row["failures"] += int(failed)
                    row["seconds"] += elapsed
        return wrapped

    def file_key(self, file):
        # Do not evaluate arbitrary __fspath__ implementations a second time.
        if not isinstance(file, (str, bytes, Path)):
            return None
        path = os.path.normcase(os.path.abspath(os.fsdecode(file)))
        for root in self.library_prefixes:
            try:
                if os.path.commonpath((root, path)) == root:
                    return Path(os.path.relpath(path, root)).as_posix()
            except ValueError:  # Different Windows drives or UNC namespaces.
                continue
        return None

    def __enter__(self):
        from codex_learner import blobs, storage
        from codex_learner.operations import Engine
        profile = self
        original_builtin, original_io, original_path_open = builtins.open, io.open, Path.open
        original_sha = hashlib.sha256
        opening_paths = []

        class Stream:
            def __init__(self, stream, row):
                self.stream, self.row = stream, row

            def __getattr__(self, name):
                return getattr(self.stream, name)

            def __enter__(self):
                self.stream.__enter__()
                return self

            def __exit__(self, *args):
                return self.stream.__exit__(*args)

            def record(self, value):
                self.row["read_calls"] += 1
                profile.note("read_calls")
                if isinstance(value, bytes):
                    self.row["binary_read_bytes"] += len(value)
                    profile.note("binary_read_bytes", len(value))
                elif isinstance(value, str):
                    self.row["text_read_characters"] += len(value)
                    profile.note("text_read_characters", len(value))
                    try:
                        size = len(value.encode("utf-8"))
                    except UnicodeError:
                        # A probe must not reject successful surrogateescape reads.
                        self.row["text_utf8_unencodable_reads"] += 1
                        profile.note("text_utf8_unencodable_reads")
                    else:
                        profile.note("text_returned_utf8_bytes", size)
                        self.row["text_returned_utf8_bytes"] += size
                return value

            def read(self, *args, **kwargs):
                return self.record(profile.observed("read", self.stream.read)(*args, **kwargs))

            def readline(self, *args, **kwargs):
                return self.record(profile.observed("readline", self.stream.readline)(*args, **kwargs))

            def readlines(self, *args, **kwargs):
                values = profile.observed("readlines", self.stream.readlines)(*args, **kwargs)
                for value in values:
                    self.record(value)
                return values

            def readinto(self, buffer):
                count = profile.observed("readinto", self.stream.readinto)(buffer)
                self.row["read_calls"] += 1
                self.row["binary_read_bytes"] += count or 0
                profile.note("read_calls")
                profile.note("binary_read_bytes", count or 0)
                return count

            def __iter__(self):
                return self

            def __next__(self):
                value = self.readline()
                if not value:
                    raise StopIteration
                return value

        def tracked_open(original, *, path_entry=False, io_entry=False):
            observed_open = profile.observed("open", original)
            def opened(file, *args, **kwargs):
                # Python 3.10 Path.open can call a cached io.open, while newer
                # versions call the patched module entry point. Observe Path's
                # real entry in both, and count its io.open delegation only once.
                # Identity matching avoids evaluating __fspath__ for this guard.
                if io_entry and any(file is path for path in opening_paths):
                    return original(file, *args, **kwargs)
                if path_entry:
                    opening_paths.append(file)
                try:
                    stream = observed_open(file, *args, **kwargs)
                finally:
                    if path_entry:
                        opening_paths.pop()
                relative = profile.file_key(file)
                if relative is None:
                    return stream
                row = profile.files.setdefault(relative, {
                    "opens": 0, "read_calls": 0, "binary_read_bytes": 0,
                    "text_read_characters": 0, "text_returned_utf8_bytes": 0,
                    "text_utf8_unencodable_reads": 0,
                })
                row["opens"] += 1
                profile.note("opens")
                return Stream(stream, row)
            return opened

        class Hash:
            def __init__(self, *args, **kwargs):
                self.hash = profile.observed("sha256_create", original_sha)(*args, **kwargs)
                value = args[0] if args else kwargs.get("string", b"")
                size = memoryview(value).nbytes
                profile.hashes["created"] += 1
                profile.hashes["input_bytes"] += size
                profile.note("sha256_created")
                profile.note("sha256_input_bytes", size)

            def __getattr__(self, name):
                return getattr(self.hash, name)

            def update(self, value):
                result = profile.observed("sha256_update", self.hash.update)(value)
                size = memoryview(value).nbytes
                profile.hashes["update_calls"] += 1
                profile.note("sha256_update_calls")
                profile.note("sha256_input_bytes", size)
                profile.hashes["input_bytes"] += size
                return result

            def digest(self, *args, **kwargs):
                return profile.observed("sha256_digest", self.hash.digest)(*args, **kwargs)

            def hexdigest(self, *args, **kwargs):
                return profile.observed("sha256_hexdigest", self.hash.hexdigest)(*args, **kwargs)

            def copy(self):
                result = object.__new__(Hash)
                result.hash = profile.observed("sha256_copy", self.hash.copy)()
                return result

        self.stack.enter_context(patch("builtins.open", tracked_open(original_builtin)))
        self.stack.enter_context(patch("io.open", tracked_open(original_io, io_entry=True)))
        self.stack.enter_context(patch.object(Path, "open", tracked_open(original_path_open, path_entry=True)))
        self.stack.enter_context(patch("hashlib.sha256", Hash))
        # Resolve contains metadata calls; artifact_path contains both. These
        # deliberately remain inclusive, including failed calls and nested probes.
        for owner, attribute, name in (
            (Path, "resolve", "path_resolve"),
            # Python 3.10 pathlib can cache os functions in its accessor, so
            # observe Path APIs too. These layers overlap; do not add them.
            (Path, "stat", "metadata_path_stat"),
            (Path, "lstat", "metadata_path_lstat"),
            (os, "stat", "metadata_stat"),
            (os, "lstat", "metadata_lstat"),
            (json, "loads", "json_loads"),
        ):
            self.stack.enter_context(patch.object(owner, attribute, self.observed(name, getattr(owner, attribute))))
        checked_path = self.observed("artifact_path_safety", blobs.artifact_path)
        self.stack.enter_context(patch.object(blobs, "artifact_path", checked_path))
        self.stack.enter_context(patch.object(storage, "artifact_path", checked_path))
        for owner, attribute, phase in (
            (storage, "verify_artifacts", "history_integrity"),
            (storage.Library, "load", "manifest_load"),
            (storage, "validate_state", "state_validation"),
            (storage, "compact", "compact_and_body_writes"),
            (storage, "read_receipt", "receipt_readback"),
            (Engine, "op_submit_answer", "business_mutation"),
            (os, "fsync", "fsync"),
            (os, "replace", "atomic_replace"),
        ):
            self.stack.enter_context(patch.object(owner, attribute, self.timed(phase, getattr(owner, attribute))))
        original_write = storage.atomic_write_json

        def write_json(path, *args, **kwargs):
            name = Path(path).name
            phase = {"course.json": "manifest_write", "course.previous.json": "backup_write"}.get(name, "json_body_write")
            with self.phase(phase):
                return original_write(path, *args, **kwargs)

        self.stack.enter_context(patch.object(storage, "atomic_write_json", write_json))
        original_lock = storage.file_lock

        @contextmanager
        def lock(*args, **kwargs):
            manager = original_lock(*args, **kwargs)
            with self.phase("lock_acquisition"):
                value = manager.__enter__()
            try:
                yield value
            except BaseException:
                import sys
                if not manager.__exit__(*sys.exc_info()):
                    raise
            else:
                manager.__exit__(None, None, None)

        self.stack.enter_context(patch.object(storage, "file_lock", lock))
        return self

    def __exit__(self, *args):
        return self.stack.__exit__(*args)

    def report(self):
        totals = {key: sum(row[key] for row in self.files.values()) for key in (
            "opens", "read_calls", "binary_read_bytes", "text_read_characters", "text_returned_utf8_bytes",
            "text_utf8_unencodable_reads")}
        return {
            "profile_schema_version": 2,
            "phase_timings_inclusive": self.phases,
            "call_timings_inclusive": self.calls,
            "phase_call_timings_inclusive": self.phase_calls,
            "application_file_totals": totals,
            "phase_io_inclusive": self.phase_io,
            "application_files": self.files,
            "sha256": self.hashes,
            "file_classification_roots": {"supplied": self.library, "resolved": self.resolved_library},
            "file_classification": "The library root is resolved once before probes/timing start. Supplied and resolved root prefixes map to one relative file key; per-open classification is lexical with no resolve/stat. Windows normcase folds casing. Short-root and UNC forms are supported only through those captured prefixes; other network aliases, mapped drives and aliases below the root are not inferred or resolved. Integer descriptors and custom PathLike objects are not assigned to a file.",
            "call_scope": "Open, SHA-256, json.loads, Path.resolve, Path.stat/Path.lstat and Python-visible os.stat/os.lstat calls are process-wide during the operation. Path.open is observed directly, including Python 3.10 cached accessors; its delegation to io.open counts once at the outer Path.open entry. Read timings cover wrapped library streams only. artifact_path_safety covers blobs/storage entry points. Failures propagate unchanged and are counted. Read/UTF-8 counters describe successful returns, excluding unencodable text from UTF-8 byte totals.",
            "limits": "Probes add overhead; use separate unprofiled samples for latency and memory acceptance. Phase and call timings are nested/inclusive, not additive: artifact_path includes resolve/metadata, resolve can include metadata, Path metadata can include os metadata, and enclosing timers include nested probe overhead. Per-phase call timings overlap when phases nest. Metadata counters cover Python-visible calls, not every OS syscall; cached/native APIs can bypass os probes (including Python 3.10 pathlib accessors), so absent os counters do not imply zero metadata cost. File reads count returned bytes/characters; text UTF-8 bytes may differ from on-disk bytes after newline decoding. These are application calls, not OS/physical disk I/O; they do not establish a physical-disk bottleneck. Hash counts include request/receipt hashing. JSON timings cover json.loads, excluding surrounding decode and lazy materialization. Uninstrumented work is not assigned to a phase.",
        }

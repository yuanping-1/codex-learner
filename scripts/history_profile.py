"""Opt-in, process-local benchmark probes; never imported by the product CLI."""

import builtins
import hashlib
import io
import os
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import patch


class OperationProfile:
    """Inclusive phase timings and application-level I/O, not physical disk I/O."""

    def __init__(self, library):
        self.library = Path(library).resolve()
        self.phases = {}
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

    def __enter__(self):
        from codex_learner import storage
        from codex_learner.operations import Engine
        profile = self
        original_builtin, original_io = builtins.open, io.open
        original_sha = hashlib.sha256

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
                    profile.note("text_returned_utf8_bytes", len(value.encode("utf-8")))
                    self.row["text_returned_utf8_bytes"] += len(value.encode("utf-8"))
                return value

            def read(self, *args, **kwargs):
                return self.record(self.stream.read(*args, **kwargs))

            def readline(self, *args, **kwargs):
                return self.record(self.stream.readline(*args, **kwargs))

            def readlines(self, *args, **kwargs):
                values = self.stream.readlines(*args, **kwargs)
                for value in values:
                    self.record(value)
                return values

            def readinto(self, buffer):
                count = self.stream.readinto(buffer)
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

        def tracked_open(original):
            def opened(file, *args, **kwargs):
                stream = original(file, *args, **kwargs)
                try:
                    relative = Path(file).resolve().relative_to(profile.library).as_posix()
                except (TypeError, ValueError):
                    return stream
                row = profile.files.setdefault(relative, {
                    "opens": 0, "read_calls": 0, "binary_read_bytes": 0,
                    "text_read_characters": 0, "text_returned_utf8_bytes": 0,
                })
                row["opens"] += 1
                profile.note("opens")
                return Stream(stream, row)
            return opened

        class Hash:
            def __init__(self, value, *args, **kwargs):
                self.hash = original_sha(value, *args, **kwargs)
                profile.hashes["created"] += 1
                profile.hashes["input_bytes"] += len(value)
                profile.note("sha256_created")
                profile.note("sha256_input_bytes", len(value))

            def __getattr__(self, name):
                return getattr(self.hash, name)

            def update(self, value):
                profile.hashes["update_calls"] += 1
                profile.note("sha256_update_calls")
                profile.note("sha256_input_bytes", len(value))
                profile.hashes["input_bytes"] += len(value)
                return self.hash.update(value)

        self.stack.enter_context(patch("builtins.open", tracked_open(original_builtin)))
        self.stack.enter_context(patch("io.open", tracked_open(original_io)))
        self.stack.enter_context(patch("hashlib.sha256", lambda value=b"", *a, **k: Hash(value, *a, **k)))
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
            "opens", "read_calls", "binary_read_bytes", "text_read_characters", "text_returned_utf8_bytes")}
        return {
            "phase_timings_inclusive": self.phases,
            "application_file_totals": totals,
            "phase_io_inclusive": self.phase_io,
            "application_files": self.files,
            "sha256": self.hashes,
            "limits": "Probes add overhead. Phase timings are nested/inclusive, not additive. File reads count returned bytes/characters; text UTF-8 bytes may differ from on-disk bytes after newline decoding. These are application calls, not OS/physical disk I/O. Hash counts include request/receipt hashing during the operation. Uninstrumented work is not assigned to a phase.",
        }

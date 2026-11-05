"""Single-read JSON verification must preserve closure and backup integrity."""

import builtins
from collections import Counter
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from support import CourseCase
from codex_learner import blobs
from codex_learner.common import LearnerError, canonical, read_json
from codex_learner.storage import atomic_write_json


class BlobIntegrityTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)

    def json_ref(self, raw, name="receipt.json"):
        path = self.directory / name
        path.write_bytes(raw)
        return {"path": name, "sha256": hashlib.sha256(raw).hexdigest(),
                "byte_length": len(raw), "encoding": "utf-8", "format": "json"}

    def test_closure_reads_and_hashes_each_unique_file_once(self):
        answer = blobs.write_body(self.directory, "原答\r\n e\u0301 😀  \n")
        inner = self.json_ref(canonical({"answer_ref": answer}).encode(), "inner.json")
        outer = self.json_ref(canonical({"nested": inner, "same_answer": answer}).encode())
        state = {"receipt": outer, "repeated_receipt": outer, "also_answer": answer}
        opened = Counter()
        read_bytes = Counter()
        read_sizes = []
        hashed_bytes = []
        real_open, real_io_open, real_sha256 = builtins.open, io.open, hashlib.sha256

        class Reader:
            def __init__(self, stream, path):
                self.stream, self.path = stream, path

            def __enter__(self):
                self.stream.__enter__()
                return self

            def __exit__(self, *args):
                return self.stream.__exit__(*args)

            def read(self, size=-1):
                read_sizes.append(size)
                data = self.stream.read(size)
                read_bytes[self.path] += len(data)
                return data

            def __getattr__(self, name):
                return getattr(self.stream, name)

        def counted_open(opener, path, *args, **kwargs):
            key = Path(path).name
            opened[key] += 1
            return Reader(opener(path, *args, **kwargs), key)

        class Digest:
            def __init__(self):
                self.digest = real_sha256()
                self.index = len(hashed_bytes)
                hashed_bytes.append(0)

            def update(self, data):
                hashed_bytes[self.index] += len(data)
                self.digest.update(data)

            def hexdigest(self):
                return self.digest.hexdigest()

        with mock.patch("builtins.open", side_effect=lambda *a, **kw: counted_open(real_open, *a, **kw)), \
                mock.patch("io.open", side_effect=lambda *a, **kw: counted_open(real_io_open, *a, **kw)), \
                mock.patch.object(blobs.hashlib, "sha256", side_effect=Digest), \
                mock.patch.object(blobs, "CHUNK_SIZE", 7):
            self.assertEqual(blobs.verify_artifacts(self.directory, state), 3)
        refs = (answer, inner, outer)
        self.assertEqual(opened, Counter({Path(ref["path"]).name: 1 for ref in refs}))
        self.assertEqual(read_bytes, Counter({Path(ref["path"]).name: ref["byte_length"] for ref in refs}))
        self.assertEqual(sorted(hashed_bytes), sorted(ref["byte_length"] for ref in refs))
        self.assertTrue(read_sizes)
        self.assertEqual(set(read_sizes), {7})

    def test_metadata_traversal_does_not_read_or_expand_json(self):
        inner = self.json_ref(b'{"kept":"nested"}', "inner.json")
        outer = self.json_ref(canonical({"nested": inner}).encode())
        with mock.patch("builtins.open", side_effect=AssertionError("Unexpected I/O")), \
                mock.patch("io.open", side_effect=AssertionError("Unexpected I/O")):
            self.assertEqual(list(blobs.iter_body_refs([outer, outer])), [outer])

    def test_verified_json_preserves_text_and_raw_byte_identity(self):
        answer = " \t中文 e\u0301 😀\r\n第二行\r末行\n  "
        raw = ('{\r\n "answer": ' + json.dumps(answer, ensure_ascii=False) + '\r\n}\r\n').encode()
        ref = self.json_ref(raw)
        self.assertEqual(blobs.read_body(self.directory, ref)["answer"], answer)
        self.assertEqual((self.directory / ref["path"]).read_bytes(), raw)
        self.assertEqual(blobs.read_body(self.directory, {k: v for k, v in ref.items()
                                                         if k != "byte_length"})["answer"], answer)

    def test_invalid_json_keeps_strict_parser_errors(self):
        for raw in (b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":Infinity}',
                    b'{"x":-Infinity}', b'{"x":"\xff"}', b'{broken'):
            with self.subTest(raw=raw):
                ref = self.json_ref(raw)
                for read in (lambda: blobs.read_body(self.directory, ref),
                             lambda: read_json(self.directory / ref["path"]),
                             lambda: blobs.verify_artifacts(self.directory, {"receipt": ref})):
                    with self.assertRaises(LearnerError) as error:
                        read()
                    self.assertEqual(error.exception.code, "invalid_json")

    def test_hash_and_length_are_checked_before_json_is_parsed(self):
        ref = self.json_ref(b'{broken')
        for bad in ({**ref, "sha256": "0" * 64}, {**ref, "byte_length": ref["byte_length"] + 1}):
            with self.subTest(ref=bad), self.assertRaises(LearnerError) as error:
                blobs.read_body(self.directory, bad)
            self.assertEqual(error.exception.code, "artifact_corrupt")
        (self.directory / ref["path"]).unlink()
        with self.assertRaises(LearnerError) as error:
            blobs.read_body(self.directory, ref)
        self.assertEqual(error.exception.code, "artifact_missing")

    def test_actual_read_length_is_checked_even_after_initial_stat(self):
        raw = b'{"answer":"unchanged"}'
        ref = self.json_ref(raw)
        for actual in (raw[:-1], raw + b" "):
            with self.subTest(actual=actual), \
                    mock.patch("builtins.open", return_value=io.BytesIO(actual)), \
                    self.assertRaises(LearnerError) as error:
                blobs.read_body(self.directory, ref)
            self.assertEqual(error.exception.code, "artifact_corrupt")

    def test_no_cross_verification_cache_trusts_same_size_and_mtime(self):
        ref = self.json_ref(b'{"answer":"first"}')
        path = self.directory / ref["path"]
        before = path.stat()
        self.assertEqual(blobs.verify_artifacts(self.directory, ref), 1)
        path.write_bytes(b'{"answer":"other"}')
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual(path.stat().st_size, before.st_size)
        self.assertEqual(path.stat().st_mtime_ns, before.st_mtime_ns)
        with self.assertRaises(LearnerError) as error:
            blobs.verify_artifacts(self.directory, ref)
        self.assertEqual(error.exception.code, "artifact_corrupt")


class ClosureBackupTests(CourseCase):
    def test_corruption_reachable_only_through_receipt_blocks_backup_rotation(self):
        self.submit(sid="keep-original", answer="原答\r\n保持  \n")
        directory = self.library.course_dir("sample-course")
        state_path = directory / "course.json"
        state = read_json(state_path)
        receipt = read_json(directory / state["operations"]["keep-original"]["receipt_ref"]["path"])
        nested = blobs.write_body(directory, {"note": "transitive original"}, json_value=True)
        receipt["transitive_evidence"] = nested
        state["operations"]["keep-original"]["receipt_ref"] = blobs.write_body(
            directory, receipt, json_value=True)
        atomic_write_json(state_path, state)
        blobs.verify_artifacts(directory, state)
        current_before = state_path.read_bytes()
        backup_path = directory / "course.previous.json"
        backup_before = backup_path.read_bytes()
        damaged_path = directory / nested["path"]
        stat = damaged_path.stat()
        damaged_path.write_bytes(damaged_path.read_bytes().replace(b"original", b"modified"))
        os.utime(damaged_path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        self.assertEqual(damaged_path.stat().st_size, stat.st_size)
        with self.assertRaises(LearnerError) as error:
            self.submit(sid="must-not-write")
        self.assertEqual(error.exception.code, "artifact_corrupt")
        self.assertEqual(state_path.read_bytes(), current_before)
        self.assertEqual(backup_path.read_bytes(), backup_before)
        self.assertNotIn("must-not-write", self.library.load("sample-course")["submissions"])

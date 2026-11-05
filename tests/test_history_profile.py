"""Diagnostic probes observe real calls without changing stored data or errors."""

import builtins
import hashlib
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/codex-learner/scripts"))
SPEC = importlib.util.spec_from_file_location("history_profile_test_subject", ROOT / "scripts/history_profile.py")
profile_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(profile_module)
OperationProfile = profile_module.OperationProfile

from codex_learner import blobs
from codex_learner.common import LearnerError


class HistoryProfileTests(unittest.TestCase):
    def test_open_classification_never_resolves_and_preserves_exact_reads(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            text = "中文\r\n末行  \n"
            path = root / "answer.txt"
            path.write_bytes(text.encode("utf-8"))
            with OperationProfile(root) as probe:
                with patch.object(Path, "resolve", side_effect=AssertionError("probe added a resolve")):
                    with builtins.open(path, encoding="utf-8", newline="") as stream:
                        self.assertEqual(stream.read(), text)
                    with path.open("rb") as stream:
                        self.assertEqual(stream.read(), text.encode("utf-8"))
            report = probe.report()
        self.assertEqual(report["application_file_totals"]["opens"], 2)
        self.assertEqual(report["call_timings_inclusive"]["open"]["calls"], 2)
        self.assertEqual(report["call_timings_inclusive"]["read"]["calls"], 2)
        self.assertNotIn("path_resolve", report["call_timings_inclusive"])
        self.assertNotIn("metadata_path_stat", report["call_timings_inclusive"])
        self.assertNotIn("metadata_path_lstat", report["call_timings_inclusive"])
        self.assertNotIn("metadata_stat", report["call_timings_inclusive"])
        self.assertNotIn("metadata_lstat", report["call_timings_inclusive"])

    def test_custom_pathlike_is_not_evaluated_again_and_fd_is_not_reopened(self):
        class OncePath:
            calls = 0

            def __fspath__(self):
                self.calls += 1
                if self.calls > 1:
                    raise AssertionError("PathLike was evaluated twice")
                return str(path)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "value.bin"
            path.write_bytes(b"value")
            once = OncePath()
            descriptor = os.open(path, os.O_RDONLY)
            try:
                with OperationProfile(root) as probe:
                    with open(once, "rb") as stream:
                        self.assertEqual(stream.read(), b"value")
                    with open(descriptor, "rb", closefd=False) as stream:
                        self.assertEqual(stream.read(), b"value")
            finally:
                os.close(descriptor)
        self.assertEqual(once.calls, 1)
        self.assertEqual(probe.report()["application_file_totals"]["opens"], 0)
        self.assertEqual(probe.report()["call_timings_inclusive"]["open"]["calls"], 2)

    def test_stream_variants_surrogates_and_hash_copy_keep_results(self):
        payload = b"one\r\ntwo\nthree"
        expected_hash = hashlib.sha256(payload + b"!").hexdigest()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "answer.bin"
            path.write_bytes(payload)
            invalid = root / "surrogate.bin"
            invalid.write_bytes(b"\xff")
            with OperationProfile(root) as probe:
                with path.open("rb") as stream:
                    self.assertEqual(stream.readline(), b"one\r\n")
                    self.assertEqual(stream.readlines(), [b"two\n", b"three"])
                    stream.seek(0)
                    buffer = bytearray(len(payload))
                    self.assertEqual(stream.readinto(buffer), len(payload))
                    self.assertEqual(buffer, payload)
                with invalid.open(encoding="utf-8", errors="surrogateescape") as stream:
                    self.assertEqual(stream.read(), "\udcff")
                digest = hashlib.sha256(payload).copy()
                digest.update(b"!")
                self.assertEqual(digest.hexdigest(), expected_hash)
                self.assertEqual(digest.digest(), bytes.fromhex(expected_hash))
                with self.assertRaises(TypeError):
                    digest.update("invalid")
            report = probe.report()
        self.assertEqual(report["application_file_totals"]["text_utf8_unencodable_reads"], 1)
        self.assertEqual(report["sha256"]["input_bytes"], len(payload) + 1)
        self.assertEqual(report["call_timings_inclusive"]["sha256_update"]["failures"], 1)
        for name in ("readline", "readlines", "readinto", "sha256_copy", "sha256_digest", "sha256_hexdigest"):
            self.assertGreater(report["call_timings_inclusive"][name]["calls"], 0)

    def test_product_path_metadata_hash_and_json_are_separate_inclusive_observations(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "body.json"
            data = b'{"answer":"original"}'
            path.write_bytes(data)
            ref = {"path": path.name, "sha256": hashlib.sha256(data).hexdigest(),
                   "byte_length": len(data), "format": "json"}
            with OperationProfile(root) as probe:
                with probe.phase("history_integrity"):
                    self.assertEqual(blobs.read_body(root, ref)["answer"], "original")
                    with self.assertRaises(LearnerError) as caught:
                        blobs.artifact_path(root, "../outside")
                    self.assertEqual(caught.exception.code, "invalid_path")
            report = probe.report()
        calls = report["call_timings_inclusive"]
        for name in ("artifact_path_safety", "path_resolve", "metadata_path_stat", "open", "read",
                     "sha256_create", "sha256_update", "sha256_hexdigest", "json_loads"):
            self.assertGreater(calls[name]["calls"], 0, name)
            self.assertEqual(report["phase_call_timings_inclusive"]["history_integrity"][name], calls[name])
        self.assertEqual(calls["artifact_path_safety"]["failures"], 1)
        self.assertIn("not additive", report["limits"])
        self.assertIn("not establish a physical-disk bottleneck", report["limits"])

    def test_errors_propagate_unchanged_and_patches_are_restored_after_failure(self):
        original_open, original_sha, original_resolve = builtins.open, hashlib.sha256, Path.resolve
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "broken.json"
            data = b'{"duplicate":1,"duplicate":2}'
            path.write_bytes(data)
            ref = {"path": path.name, "sha256": hashlib.sha256(data).hexdigest(),
                   "byte_length": len(data), "format": "json"}
            with self.assertRaises(LearnerError) as before:
                blobs.read_body(root, ref)
            with self.assertRaisesRegex(RuntimeError, "outside probe"):
                with OperationProfile(root) as probe:
                    with self.assertRaises(LearnerError) as during:
                        blobs.read_body(root, ref)
                    self.assertEqual(before.exception.code, during.exception.code)
                    self.assertEqual(str(before.exception), str(during.exception))
                    with self.assertRaises(FileNotFoundError) as missing:
                        open(root / "missing", "rb")
                    self.assertEqual(missing.exception.filename, str(root / "missing"))
                    raise RuntimeError("outside probe")
        self.assertIs(builtins.open, original_open)
        self.assertIs(hashlib.sha256, original_sha)
        self.assertIs(Path.resolve, original_resolve)
        self.assertEqual(probe.report()["call_timings_inclusive"]["json_loads"]["failures"], 1)
        self.assertEqual(probe.report()["call_timings_inclusive"]["open"]["failures"], 1)

    def test_nested_clocks_are_inclusive_not_additive(self):
        probe = OperationProfile(ROOT)
        with patch.object(profile_module.time, "perf_counter", side_effect=range(6)):
            with probe.phase("parent"):
                probe.observed("outer", lambda: probe.observed("inner", lambda: None)())()
        report = probe.report()
        self.assertEqual(report["phase_timings_inclusive"]["parent"]["seconds"], 5)
        self.assertEqual(report["call_timings_inclusive"]["outer"]["seconds"], 3)
        self.assertEqual(report["call_timings_inclusive"]["inner"]["seconds"], 1)
        self.assertEqual(report["phase_call_timings_inclusive"]["parent"], report["call_timings_inclusive"])


if __name__ == "__main__":
    unittest.main()

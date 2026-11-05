"""The opt-in probe observes failures without replacing exceptions or streams."""

import builtins
import gc
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
import weakref
from pathlib import Path
from unittest.mock import patch

from support import CourseCase
from codex_learner.archives import create_archive, restore_archive
from codex_learner.common import LearnerError
from codex_learner.storage import Library

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("restore_probe_test_subject", ROOT / "scripts/restore_probe.py")
probe_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe_module)
RestoreFailureProbe = probe_module.RestoreFailureProbe


class RestoreProbeTests(unittest.TestCase):
    def fixture(self, root):
        library = root / "library"
        library.mkdir()
        stage = root / ".learner-restore-test" / "course"
        stage.mkdir(parents=True)
        (stage / "body.txt").write_bytes(b"original\r\n")
        return library, stage, library / "course"

    def test_real_failure_is_observed_before_caller_cleanup_and_same_error_propagates(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            library, stage, target = self.fixture(root)
            failure = PermissionError(13, "controlled rename rejection", str(stage), None, str(target))
            with patch.object(Path, "rename", side_effect=failure):
                with RestoreFailureProbe(library, "course", root / "evidence") as probe:
                    with self.assertRaises(PermissionError) as caught:
                        stage.rename(target)
                    self.assertIs(caught.exception, failure)
                    self.assertTrue(stage.exists())
                    self.assertFalse(target.exists())
                    # Caller cleanup happens only after the failure snapshot.
                    (stage / "body.txt").unlink()
                    stage.rmdir()
            report = probe.report()
            self.assertEqual(len(report["events"]), 1)
            event = report["events"][0]
            observed = {row["path"]: row for row in event["observations"]["paths"]}
            self.assertTrue(observed[str(stage)]["exists"])
            self.assertFalse(observed[str(target)]["exists"])
            self.assertEqual(event["original_error"]["errno"], 13)
            self.assertEqual(event["external_handle_owners"]["status"], "UNKNOWN")
            self.assertEqual(json.loads(Path(event["evidence_path"]).read_text())["original_error"], event["original_error"])

    def test_streams_are_original_weakly_tracked_closed_streams_excluded_and_patch_restored(self):
        originals = (builtins.open, io.open, Path.open, Path.rename)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            library, stage, target = self.fixture(root)
            with RestoreFailureProbe(library, "course", root / "evidence") as probe:
                stream = (stage / "body.txt").open("rb")
                self.assertIs(type(stream), io.BufferedReader)
                self.assertEqual(stream.read(), b"original\r\n")
                self.assertEqual(len(probe._open_streams()), 1)
                reference = weakref.ref(stream)
                stream.close()
                self.assertEqual(probe._open_streams(), [])
                del stream
                gc.collect()
                self.assertIsNone(reference())
                self.assertFalse(probe.streams)
            self.assertEqual(originals, (builtins.open, io.open, Path.open, Path.rename))

    def test_custom_pathlike_is_evaluated_once_and_fd_is_not_reopened(self):
        class Once:
            calls = 0
            def __fspath__(self):
                self.calls += 1
                if self.calls != 1:
                    raise AssertionError("evaluated twice")
                return str(path)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            library, stage, target = self.fixture(root)
            path = stage / "body.txt"
            once = Once()
            descriptor = os.open(path, os.O_RDONLY)
            try:
                with RestoreFailureProbe(library, "course", root / "evidence") as probe:
                    with open(once, "rb") as stream:
                        self.assertEqual(stream.read(), b"original\r\n")
                    with open(descriptor, "rb", closefd=False) as stream:
                        self.assertEqual(stream.read(), b"original\r\n")
                self.assertEqual(once.calls, 1)
            finally:
                os.close(descriptor)

    def test_unrelated_failure_and_success_are_not_collected_or_retried(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            library, stage, target = self.fixture(root)
            with RestoreFailureProbe(library, "course", root / "evidence") as probe:
                self.assertEqual(stage.rename(target=target), target)
                with self.assertRaises(FileNotFoundError):
                    (root / "missing").rename(target=root / "other")
            self.assertEqual(probe.report()["events"], [])
            self.assertTrue((target / "body.txt").exists())

    def test_collection_and_evidence_write_errors_never_replace_original(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            library, stage, target = self.fixture(root)
            failure = PermissionError("original failure")
            with patch.object(Path, "rename", side_effect=failure):
                with RestoreFailureProbe(library, "course", root / "evidence") as probe:
                    with patch.object(probe, "_open_streams", side_effect=RuntimeError("collector failed")):
                        with patch.object(probe_module.json, "dump", side_effect=OSError("disk full")):
                            with self.assertRaises(PermissionError) as caught:
                                stage.rename(target)
                    self.assertIs(caught.exception, failure)
            errors = probe.report()["events"][0]["collection_errors"]
            self.assertEqual([row["phase"] for row in errors], ["open_python_streams", "write_evidence"])

    def test_capture_crash_and_path_observation_unknown_leave_original_intact(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            library, stage, target = self.fixture(root)
            failure = PermissionError("original failure")
            with patch.object(Path, "rename", side_effect=failure):
                with RestoreFailureProbe(library, "course", root / "evidence") as probe:
                    with patch.object(Path, "lstat", side_effect=PermissionError("cannot inspect")):
                        unknown = probe._path_observation(stage)
                    self.assertIsNone(unknown["exists"])
                    with patch.object(probe, "_capture", side_effect=KeyboardInterrupt()):
                        with self.assertRaises(PermissionError) as caught:
                            stage.rename(target)
                    self.assertIs(caught.exception, failure)
            self.assertEqual(probe.report()["observer_errors"][0]["phase"], "capture")

    def test_repeated_real_failures_have_distinct_files_without_automatic_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            library, stage, target = self.fixture(root)
            with patch.object(Path, "rename", side_effect=PermissionError("original")) as rename:
                with RestoreFailureProbe(library, "course", root / "evidence") as probe:
                    for _ in range(2):
                        with self.assertRaises(PermissionError):
                            stage.rename(target)
            self.assertEqual(rename.call_count, 2)
            paths = [row["evidence_path"] for row in probe.report()["events"]]
            self.assertEqual(len(set(paths)), 2)
            self.assertTrue(all(Path(path).is_file() for path in paths))

    def test_output_within_library_is_rejected_before_writing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            library, stage, target = self.fixture(root)
            for output in (library, library / "nested/evidence"):
                with self.assertRaises(ValueError):
                    RestoreFailureProbe(library, "course", output)
            self.assertFalse((library / "nested").exists())


class ArchiveRestoreProbeIntegrationTests(CourseCase):
    def test_product_rolls_back_after_observation_and_keeps_original_cause(self):
        archive = create_archive(self.library, "sample-course", self.root / "archive/course.zip")
        fresh = Library(self.root / "fresh")
        fresh.initialize()
        target = fresh.root / "sample-course"
        original_rename = Path.rename
        failure = PermissionError(13, "controlled publication failure")
        failure.winerror = 5

        def fail_publish(source, destination):
            if destination == target:
                raise failure
            return original_rename(source, destination)

        with patch.object(Path, "rename", fail_publish):
            with RestoreFailureProbe(fresh.root, "sample-course", self.root / "observations") as probe:
                with self.assertRaises(LearnerError) as caught:
                    restore_archive(fresh, archive["path"], archive["sha256"], confirmed=True)
        error = caught.exception
        self.assertEqual(error.code, "restore_failed")
        self.assertEqual(error.details["phase"], "publish_course")
        self.assertIs(error.__cause__, failure)
        self.assertEqual(error.details["rollback"]["status"], "completed")
        self.assertFalse(error.details["stage_exists"])
        self.assertFalse(target.exists())
        self.assertEqual(len(probe.events), 1)
        event = probe.events[0]
        self.assertEqual(event["original_error"]["winerror"], 5)
        observed = {row["path"]: row for row in event["observations"]["paths"]}
        self.assertTrue(observed[event["source_path"]]["exists"])
        self.assertFalse(observed[event["destination_path"]]["exists"])
        # The observer does not keep staged streams alive or delay their close.
        streams = event["observations"]["open_python_streams"]
        self.assertTrue(any(row["path"] == os.path.normcase(str(fresh.root / ".library.lock"))
                            for row in streams))
        self.assertFalse(any(row["path"] and event["source_path"] in row["path"] for row in streams))
        recovered = restore_archive(fresh, archive["path"], archive["sha256"], confirmed=True)
        self.assertEqual(recovered["status"], "course_restored")


if __name__ == "__main__":
    unittest.main()

"""Restore fault injection checks state and recovery material, not Windows causes."""

import hashlib
import json
import shutil
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from support import CourseCase
from codex_learner import archives
from codex_learner.archives import (
    archived_courses, confirm_download, create_archive, release_course, restore_archive,
)
from codex_learner.common import LearnerError
from codex_learner.storage import Library


def denied(message, source=None, destination=None):
    error = PermissionError(13, message, str(source) if source else None)
    error.winerror = 5
    if destination is not None:
        error.filename2 = str(destination)
    return error


class ArchiveRestoreFailureTests(CourseCase):
    def archive(self):
        return create_archive(self.library, "sample-course", self.root / "archives/course.zip")

    def fresh_library(self):
        library = Library(self.root / "fresh")
        library.initialize()
        return library

    def released(self, *, keep_residue=False):
        archive = self.archive()
        download = self.root / "web-downloads/course.zip"
        download.parent.mkdir()
        shutil.copyfile(archive["path"], download)
        verified = confirm_download(download, archive["sha256"], source_declaration="web_download",
                                    archive_path=archive["path"])
        destination = self.library.root / "sample-course"
        if keep_residue:
            unlink = Path.unlink

            def stop_before_deletion(path, *args, **kwargs):
                if path == destination / "course.json":
                    raise denied("release interruption", path)
                return unlink(path, *args, **kwargs)

            with patch.object(Path, "unlink", stop_before_deletion), self.assertRaises(OSError):
                release_course(self.library, "sample-course", archive["path"], verified, confirmed=True)
        else:
            release_course(self.library, "sample-course", archive["path"], verified, confirmed=True)
        return archive, destination

    def tree(self, directory):
        return {path.relative_to(directory).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in directory.rglob("*") if path.is_file() and path.name != ".lock"}

    def archive_tree(self, archive):
        manifest = archives.inspect_archive(archive["path"], archive["sha256"])["manifest"]
        return {path: entry["sha256"] for path, entry in manifest["files"].items()}

    def restore(self, archive, library=None):
        return restore_archive(library or self.library, archive["path"], archive["sha256"], confirmed=True)

    def assert_preserved(self, error):
        self.assertEqual(error.code, "commit_uncertain")
        self.assertTrue(error.details["staging_preserved"])
        stage = Path(error.details["stage_path"])
        self.assertTrue(stage.is_dir())
        self.assertTrue(Path(error.details["path"]).parent.is_dir())
        return stage

    def test_residue_move_failure_reports_os_fields_and_preserves_original(self):
        archive, destination = self.released(keep_residue=True)
        before = self.tree(destination)
        rename = Path.rename

        def fail(path, target):
            if path == destination:
                raise denied("residue move denied", path, target)
            return rename(path, target)

        with patch.object(Path, "rename", fail), self.assertRaises(LearnerError) as caught:
            self.restore(archive)
        error = caught.exception
        self.assertEqual(error.code, "restore_failed")
        self.assertEqual(error.details["phase"], "move_residue")
        self.assertEqual(error.details["original_error"]["errno"], 13)
        self.assertEqual(error.details["original_error"]["winerror"], 5)
        self.assertEqual(error.details["original_error"]["filename"], str(destination))
        self.assertEqual(error.details["original_error"]["filename2"], error.details["destination_path"])
        self.assertEqual(json.loads(json.dumps(error.as_dict()))["original_error"]["winerror"], 5)
        self.assertEqual(error.details["rollback"], {"status": "completed", "steps": []})
        self.assertFalse(error.details["staging_preserved"])
        self.assertFalse(Path(error.details["stage_path"]).exists())
        self.assertEqual(self.tree(destination), before)
        self.assertEqual(len(archived_courses(self.library)), 1)
        self.assertEqual(self.restore(archive)["status"], "course_restored")

    def test_fresh_publish_failure_does_not_enable_course_or_retry(self):
        archive = self.archive()
        library = self.fresh_library()
        destination = library.root / "sample-course"
        rename = Path.rename
        attempts = []

        def fail(path, target):
            if target == destination:
                attempts.append((path, target))
                raise denied("publish denied", path, target)
            return rename(path, target)

        with patch.object(Path, "rename", fail), self.assertRaises(LearnerError) as caught:
            self.restore(archive, library)
        error = caught.exception
        self.assertEqual(error.code, "restore_failed")
        self.assertEqual(error.details["phase"], "publish_course")
        self.assertEqual(len(attempts), 1)
        self.assertFalse(destination.exists())
        self.assertFalse(Path(error.details["stage_path"]).exists())
        self.assertTrue(Path(archive["path"]).is_file())
        self.assertEqual(self.restore(archive, library)["status"], "course_restored")

    def test_publish_failure_restores_original_release_residue(self):
        archive, destination = self.released(keep_residue=True)
        before = self.tree(destination)
        rename = Path.rename

        def fail(path, target):
            if target == destination and path.name == "sample-course":
                raise denied("publish denied", path, target)
            return rename(path, target)

        with patch.object(Path, "rename", fail), self.assertRaises(LearnerError) as caught:
            self.restore(archive)
        error = caught.exception
        self.assertEqual(error.details["phase"], "publish_course")
        self.assertEqual(error.details["rollback"]["status"], "completed")
        self.assertEqual(error.details["rollback"]["steps"][0]["phase"], "restore_residue")
        self.assertEqual(self.tree(destination), before)
        self.assertEqual(len(archived_courses(self.library)), 1)

    def test_index_write_failure_rolls_back_both_moves(self):
        archive, destination = self.released(keep_residue=True)
        before = self.tree(destination)
        with patch.object(archives, "_write_index", side_effect=denied("index write denied")):
            with self.assertRaises(LearnerError) as caught:
                self.restore(archive)
        error = caught.exception
        self.assertEqual(error.code, "restore_failed")
        self.assertEqual(error.details["phase"], "write_index")
        self.assertEqual([step["phase"] for step in error.details["rollback"]["steps"]],
                         ["rollback_course", "restore_residue"])
        self.assertTrue(all(step["status"] == "completed" for step in error.details["rollback"]["steps"]))
        self.assertFalse(error.details["staging_preserved"])
        self.assertEqual(self.tree(destination), before)
        self.assertEqual(len(archived_courses(self.library)), 1)

    def test_course_rollback_failure_keeps_original_error_and_both_copies(self):
        archive, destination = self.released(keep_residue=True)
        before = self.tree(destination)
        rename = Path.rename

        def fail(path, target):
            if path == destination and target.name == "sample-course":
                raise denied("course rollback denied", path, target)
            return rename(path, target)

        with patch.object(archives, "_write_index", side_effect=denied("primary index failure")), \
                patch.object(Path, "rename", fail), self.assertRaises(LearnerError) as caught:
            self.restore(archive)
        error = caught.exception
        stage = self.assert_preserved(error)
        self.assertEqual(error.details["phase"], "write_index")
        self.assertIn("primary index failure", error.details["original_error"]["message"])
        self.assertEqual(error.details["rollback"]["phase"], "rollback_course")
        self.assertEqual(error.details["rollback"]["error"]["winerror"], 5)
        self.assertEqual(self.tree(stage / "release-residue"), before)
        self.assertEqual(self.tree(destination), self.archive_tree(archive))
        self.assertEqual(len(archived_courses(self.library)), 1)

    def test_residue_rollback_failure_keeps_staged_course_and_residue(self):
        archive, destination = self.released(keep_residue=True)
        before = self.tree(destination)
        rename = Path.rename

        def fail(path, target):
            if target == destination:
                raise denied("rollback denied" if path.name == "release-residue" else "primary publish failure",
                             path, target)
            return rename(path, target)

        with patch.object(Path, "rename", fail), self.assertRaises(LearnerError) as caught:
            self.restore(archive)
        error = caught.exception
        stage = self.assert_preserved(error)
        self.assertEqual(error.details["phase"], "publish_course")
        self.assertIn("primary publish failure", error.details["original_error"]["message"])
        self.assertEqual(error.details["rollback"]["phase"], "restore_residue")
        self.assertIn("rollback denied", error.details["rollback"]["error"]["message"])
        self.assertFalse(destination.exists())
        self.assertEqual(self.tree(stage / "sample-course"), self.archive_tree(archive))
        self.assertEqual(self.tree(stage / "release-residue"), before)
        self.assertEqual(len(archived_courses(self.library)), 1)
        self.assertTrue(Path(archive["path"]).is_file())

    def test_failed_index_readback_keeps_recovery_material(self):
        archive, destination = self.released(keep_residue=True)
        read_index = archives._index
        calls = []

        def fail_second_read(library):
            calls.append(library.root)
            if len(calls) == 2:
                raise denied("index readback denied", library.root / archives.INDEX_NAME)
            return read_index(library)

        with patch.object(archives, "_write_index", side_effect=denied("primary index failure")), \
                patch.object(archives, "_index", fail_second_read), self.assertRaises(LearnerError) as caught:
            self.restore(archive)
        error = caught.exception
        stage = self.assert_preserved(error)
        self.assertEqual(error.details["phase"], "write_index")
        self.assertEqual(error.details["rollback"]["status"], "state_unknown")
        self.assertEqual(error.details["rollback"]["phase"], "readback")
        self.assertTrue((destination / "course.json").is_file())
        self.assertTrue((stage / "release-residue/course.json").is_file())
        self.assertEqual(len(archived_courses(self.library)), 1)

    def test_index_error_after_commit_returns_success_with_diagnostic(self):
        archive, destination = self.released()
        write_index = archives._write_index

        def committed_then_failed(library, index):
            write_index(library, index)
            raise denied("index fsync denied", library.root / archives.INDEX_NAME)

        with patch.object(archives, "_write_index", committed_then_failed):
            result = self.restore(archive)
        self.assertEqual(result["status"], "course_restored")
        self.assertIn("readback", result["warning"])
        self.assertEqual(result["diagnostics"][0]["phase"], "write_index")
        self.assertEqual(result["diagnostics"][0]["rollback"]["status"], "not_needed_committed")
        self.assertFalse(result["diagnostics"][0]["staging_preserved"])
        self.assertEqual(archived_courses(self.library), [])
        self.assertEqual(self.library.load_manifest("sample-course")["state_revision"], archive["state_revision"])

    def test_cleanup_failure_after_commit_returns_success_and_retained_path(self):
        archive, destination = self.released()
        rmtree = shutil.rmtree

        def fail_cleanup(path, *args, **kwargs):
            if Path(path).name.startswith(".learner-restore-"):
                raise denied("cleanup denied", path)
            return rmtree(path, *args, **kwargs)

        with patch.object(shutil, "rmtree", fail_cleanup):
            result = self.restore(archive)
        self.assertEqual(result["status"], "course_restored")
        self.assertIn("cleanup failed", result["warning"])
        diagnostic = result["diagnostics"][0]
        self.assertEqual(diagnostic["phase"], "cleanup")
        self.assertEqual(diagnostic["rollback"]["status"], "not_needed_committed")
        self.assertEqual(diagnostic["original_error"]["winerror"], 5)
        self.assertTrue(diagnostic["staging_preserved"])
        self.assertTrue(Path(diagnostic["stage_path"]).is_dir())
        self.assertEqual(archived_courses(self.library), [])
        self.assertEqual(self.library.load_manifest("sample-course")["state_revision"], archive["state_revision"])

    def test_failed_cleanup_does_not_replace_primary_failure(self):
        archive = self.archive()
        library = self.fresh_library()
        destination = library.root / "sample-course"
        rename, rmtree = Path.rename, shutil.rmtree

        def fail_publish(path, target):
            if target == destination:
                raise denied("primary publish denied", path, target)
            return rename(path, target)

        def fail_cleanup(path, *args, **kwargs):
            if Path(path).name.startswith(".learner-restore-"):
                raise denied("secondary cleanup denied", path)
            return rmtree(path, *args, **kwargs)

        with patch.object(Path, "rename", fail_publish), patch.object(shutil, "rmtree", fail_cleanup), \
                self.assertRaises(LearnerError) as caught:
            self.restore(archive, library)
        error = caught.exception
        self.assertEqual(error.code, "restore_failed")
        self.assertEqual(error.details["phase"], "publish_course")
        self.assertIn("primary publish denied", error.details["original_error"]["message"])
        self.assertEqual(error.details["cleanup_error"]["phase"], "cleanup")
        self.assertIn("secondary cleanup denied", error.details["cleanup_error"]["message"])
        self.assertTrue(error.details["staging_preserved"])
        self.assertTrue((Path(error.details["stage_path"]) / "sample-course/course.json").is_file())
        self.assertFalse(destination.exists())

    def test_cleanup_and_commit_readback_failure_reports_uncertainty(self):
        archive, destination = self.released()
        rmtree, sha = shutil.rmtree, archives._sha
        cleaning = []

        def fail_cleanup(path, *args, **kwargs):
            if Path(path).name.startswith(".learner-restore-"):
                cleaning.append(path)
                raise denied("cleanup denied", path)
            return rmtree(path, *args, **kwargs)

        def fail_readback(path):
            if cleaning and Path(path) == destination / "course.json":
                raise denied("course readback denied", path)
            return sha(path)

        with patch.object(shutil, "rmtree", fail_cleanup), patch.object(archives, "_sha", fail_readback), \
                self.assertRaises(LearnerError) as caught:
            self.restore(archive)
        error = caught.exception
        self.assert_preserved(error)
        self.assertEqual(error.details["phase"], "cleanup")
        self.assertEqual(error.details["rollback"]["status"], "state_unknown")
        self.assertIn("course readback denied", error.details["rollback"]["error"]["message"])
        self.assertEqual(archived_courses(self.library), [])
        self.assertEqual(self.library.load_manifest("sample-course")["state_revision"], archive["state_revision"])

    def test_cleanup_readback_rejects_a_corrupted_referenced_body(self):
        archive, destination = self.released()
        member = next(path for path in self.archive_tree(archive) if path.startswith("bodies/"))
        rmtree = shutil.rmtree

        def corrupt_then_fail_cleanup(path, *args, **kwargs):
            if Path(path).name.startswith(".learner-restore-"):
                (destination / member).write_bytes(b"damaged after commit")
                raise denied("cleanup denied", path)
            return rmtree(path, *args, **kwargs)

        with patch.object(shutil, "rmtree", corrupt_then_fail_cleanup), self.assertRaises(LearnerError) as caught:
            self.restore(archive)
        error = caught.exception
        self.assert_preserved(error)
        self.assertEqual(error.details["phase"], "cleanup")
        self.assertEqual(error.details["rollback"]["status"], "state_unknown")
        self.assertEqual(archived_courses(self.library), [])
        self.assertEqual((destination / member).read_bytes(), b"damaged after commit")

    def test_cleanup_error_after_removal_does_not_claim_staging_still_exists(self):
        archive = self.archive()
        library = self.fresh_library()
        rmtree = shutil.rmtree

        def removed_then_failed(path, *args, **kwargs):
            rmtree(path, *args, **kwargs)
            if Path(path).name.startswith(".learner-restore-"):
                raise denied("cleanup reported error after removal", path)

        with patch.object(shutil, "rmtree", removed_then_failed):
            result = self.restore(archive, library)
        self.assertEqual(result["status"], "course_restored")
        diagnostic = result["diagnostics"][0]
        self.assertFalse(diagnostic["stage_exists"])
        self.assertFalse(diagnostic["staging_preserved"])
        self.assertTrue(diagnostic["cleanup_failed"])
        self.assertEqual(library.load_manifest("sample-course")["state_revision"], archive["state_revision"])

    def test_publish_error_after_rename_confirms_new_course_enabled(self):
        archive = self.archive()
        library = self.fresh_library()
        destination = library.root / "sample-course"
        rename = Path.rename

        def moved_then_failed(path, target):
            result = rename(path, target)
            if target == destination:
                raise denied("publish reported error after rename", path, target)
            return result

        with patch.object(Path, "rename", moved_then_failed):
            result = self.restore(archive, library)
        self.assertEqual(result["status"], "course_restored")
        self.assertEqual(result["diagnostics"][0]["phase"], "publish_course")
        self.assertEqual(result["diagnostics"][0]["rollback"]["status"], "not_needed_committed")
        self.assertEqual(self.tree(destination), self.archive_tree(archive))

    def test_residue_move_error_after_rename_rolls_back_original_files(self):
        archive, destination = self.released(keep_residue=True)
        before = self.tree(destination)
        rename = Path.rename

        def moved_then_failed(path, target):
            result = rename(path, target)
            if target.name == "release-residue":
                raise denied("move reported error after rename", path, target)
            return result

        with patch.object(Path, "rename", moved_then_failed), self.assertRaises(LearnerError) as caught:
            self.restore(archive)
        error = caught.exception
        self.assertEqual(error.code, "restore_failed")
        self.assertEqual(error.details["phase"], "move_residue")
        self.assertEqual(error.details["rollback"]["status"], "completed")
        self.assertEqual(self.tree(destination), before)
        self.assertEqual(len(archived_courses(self.library)), 1)

    def test_lock_exit_error_after_commit_preserves_scene_and_reports_uncertainty(self):
        archive = self.archive()
        library = self.fresh_library()
        lock = archives.file_lock

        @contextmanager
        def unlock_failed(path):
            with lock(path):
                yield
            raise denied("unlock denied", path)

        with patch.object(archives, "file_lock", unlock_failed), self.assertRaises(LearnerError) as caught:
            self.restore(archive, library)
        error = caught.exception
        self.assert_preserved(error)
        self.assertEqual(error.details["phase"], "unlock_library")
        self.assertEqual(error.details["rollback"]["status"], "state_unknown")
        self.assertEqual(library.load_manifest("sample-course")["state_revision"], archive["state_revision"])
        self.assertEqual(archived_courses(library), [])

    def test_lock_exit_error_does_not_mask_prior_failure_and_rollback(self):
        archive = self.archive()
        library = self.fresh_library()
        destination = library.root / "sample-course"
        lock, rename = archives.file_lock, Path.rename

        @contextmanager
        def unlock_failed(path):
            try:
                with lock(path):
                    yield
            finally:
                raise denied("secondary unlock denied", path)

        def publish_failed(path, target):
            if target == destination:
                raise denied("primary publish denied", path, target)
            return rename(path, target)

        with patch.object(archives, "file_lock", unlock_failed), patch.object(Path, "rename", publish_failed), \
                self.assertRaises(LearnerError) as caught:
            self.restore(archive, library)
        error = caught.exception
        self.assertEqual(error.code, "restore_failed")
        self.assertEqual(error.details["phase"], "publish_course")
        self.assertIn("primary publish denied", error.details["original_error"]["message"])
        self.assertEqual(error.details["rollback"]["status"], "completed")
        self.assertEqual(error.details["lock_error"]["phase"], "unlock_library")
        self.assertIn("secondary unlock denied", error.details["lock_error"]["message"])
        self.assertTrue(error.details["staging_preserved"])
        self.assertFalse(destination.exists())

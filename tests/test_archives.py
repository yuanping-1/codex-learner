"""Archive tests use isolated local folders, never a real Drive upload."""

import errno
import hashlib
import json
import os
import shutil
import stat
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from support import CourseCase
from codex_learner.archives import (
    archive_availability, archived_courses, confirm_download, copy_to_drive, create_archive,
    inspect_archive, release_course, restore_archive,
)
from codex_learner.common import LearnerError, canonical, read_json
from codex_learner.storage import Library, atomic_write
from codex_learner.blobs import materialize
from codex_learner import archives


class ArchiveTests(CourseCase):
    def archive(self, name="course.zip"):
        return create_archive(self.library, "sample-course", self.root / "archives" / name)

    def retrieved(self, archive):
        # The test simulates the explicit user declaration. It proves file
        # verification, not an actual web download or cloud storage service.
        path = self.root / "web-downloads" / Path(archive["path"]).name
        path.parent.mkdir(exist_ok=True)
        shutil.copyfile(archive["path"], path)
        return confirm_download(path, archive["sha256"], source_declaration="web_download",
                                archive_path=archive["path"])

    def test_closed_archive_restores_exact_answers_and_previous_snapshot_offline(self):
        answer = '原答\n"ASCII quotes"\\literal\\n\n'
        self.submit(sid="answer", answer=answer)
        self.assess("answer", feedback_help="solution")
        result = self.archive()
        checked = inspect_archive(result["path"], result["sha256"])
        self.assertIn("course.previous.json", checked["manifest"]["files"])
        self.assertTrue(Path(result["path"] + ".sha256").is_file())
        other = Library(self.root / "restored-library")
        other.initialize()
        restored = restore_archive(other, result["path"], result["sha256"], confirmed=True)
        state = other.load("sample-course")
        self.assertFalse(restored["network_required"])
        self.assertEqual(state["submissions"]["answer"]["answer"], answer)
        self.assertEqual(state["submissions"]["answer"]["received_revision"],
                         self.library.load("sample-course")["submissions"]["answer"]["received_revision"])
        self.assertEqual(state["operations"], self.library.load("sample-course")["operations"])

    def test_archive_keeps_artifacts_reachable_only_from_migration_backup(self):
        directory = self.library.course_dir("sample-course")
        body = directory / "bodies/retained.txt"
        body.parent.mkdir(exist_ok=True)
        body.write_text("迁移前的不可变正文", encoding="utf-8")
        prior = read_json(directory / "course.json")
        prior["legacy_artifact"] = {"path": "bodies/retained.txt", "sha256": hashlib.sha256(body.read_bytes()).hexdigest()}
        backup = directory / "migration-backups/schema1.json"
        backup.parent.mkdir()
        atomic_write(backup, canonical(prior).encode("utf-8"))
        checked = inspect_archive(self.archive()["path"])
        self.assertIn("migration-backups/schema1.json", checked["manifest"]["files"])
        self.assertIn("bodies/retained.txt", checked["manifest"]["files"])

    def test_runtime_config_and_unmanaged_files_are_not_archived_or_deleted(self):
        directory = self.library.course_dir("sample-course")
        private = directory / "config.json"
        private.write_text('{"secret":"test-only"}', encoding="utf-8")
        result = self.archive()
        self.assertNotIn("config.json", inspect_archive(result["path"])["manifest"]["files"])
        with self.assertRaises(LearnerError) as error:
            release_course(self.library, "sample-course", result["path"], self.retrieved(result), confirmed=True)
        self.assertEqual(error.exception.code, "unarchived_files")
        self.assertTrue(private.is_file())
        self.assertTrue((directory / "course.json").is_file())

    def test_corrupt_referenced_artifact_prevents_archive_publication(self):
        state = self.library.load("sample-course")
        source = state["sources"]["sample-source-v1"]
        (self.library.course_dir("sample-course") / source["path"]).write_bytes(b"changed")
        with self.assertRaises(LearnerError) as error:
            self.archive()
        self.assertEqual(error.exception.code, "artifact_corrupt")
        self.assertFalse((self.root / "archives/course.zip").exists())

    def test_drive_copy_remains_unconfirmed_and_is_not_download_evidence(self):
        result = self.archive()
        copied = copy_to_drive(result["path"], self.root / "fake-drive")
        self.assertEqual(copied["status"], "sync_unconfirmed")
        self.assertFalse(copied["cloud_upload_verified"])
        with self.assertRaises(LearnerError):
            release_course(self.library, "sample-course", result["path"], copied, confirmed=True)
        with self.assertRaises(LearnerError) as error:
            confirm_download(copied["path"], result["sha256"], source_declaration="web_download",
                             archive_path=result["path"])
        self.assertEqual(error.exception.code, "download_not_independent")

    def test_download_requires_explicit_declaration_and_distinct_path(self):
        result = self.archive()
        with self.assertRaises(LearnerError) as error:
            confirm_download(result["path"], result["sha256"], source_declaration="sync_folder",
                             archive_path=result["path"])
        self.assertEqual(error.exception.code, "download_declaration_required")
        with self.assertRaises(LearnerError) as error:
            confirm_download(result["path"], result["sha256"], source_declaration="web_download",
                             archive_path=result["path"])
        self.assertEqual(error.exception.code, "download_not_independent")

    def test_download_rejects_hard_link_to_original_file(self):
        result = self.archive()
        linked = self.root / "hard-linked.zip"
        # Probe ordinary files across the same source/target directories. This
        # diagnoses this fixture's ability to create links, not the whole volume
        # or the cause of a past WinError 5. Other download checks remain separate.
        probe_source = Path(result["path"]).parent / "hardlink-capability-source"
        probe_target = linked.parent / "hardlink-capability-target"
        probe_source.write_bytes(b"isolated hard-link capability probe\n")
        try:
            try:
                os.link(probe_source, probe_target)
            except OSError as error:
                unavailable_codes = {getattr(errno, name, None) for name in
                                     ("EACCES", "EPERM", "ENOSYS", "ENOTSUP", "EOPNOTSUPP")}
                unavailable_codes.discard(None)
                unavailable = (error.errno in unavailable_codes
                               or getattr(error, "winerror", None) in {1, 5, 50, 1314})
                # A target appearing despite the error is an uncertain outcome,
                # not proof of unavailable fixture capability.
                if not unavailable:
                    raise
                try:
                    probe_target.lstat()
                except FileNotFoundError:
                    self.skipTest("Hard-link creation unavailable for this fixture; "
                                  f"errno={error.errno}, winerror={getattr(error, 'winerror', None)}: {error}")
                # Existing or unreadable target means an uncertain outcome.
                # Only a confirmed missing target permits the capability skip.
                raise
            self.assertTrue(os.path.samefile(probe_source, probe_target))
        finally:
            probe_target.unlink(missing_ok=True)
            probe_source.unlink(missing_ok=True)
        # Once the ordinary-file probe succeeds, an archive-specific link error
        # must remain ERROR; never convert it to a capability skip.
        os.link(result["path"], linked)
        self.assertTrue(os.path.samefile(result["path"], linked))
        with self.assertRaises(LearnerError) as error:
            confirm_download(linked, result["sha256"], source_declaration="web_download",
                             archive_path=result["path"])
        self.assertEqual(error.exception.code, "download_not_independent")

    def test_pending_assessment_and_running_task_prevent_release(self):
        self.submit(sid="pending")
        result = self.archive()
        with self.assertRaises(LearnerError) as error:
            release_course(self.library, "sample-course", result["path"], self.retrieved(result), confirmed=True)
        self.assertEqual(error.exception.code, "pending_work")
        self.assess("pending")
        result = self.archive("assessed.zip")
        with self.assertRaises(LearnerError) as error:
            release_course(self.library, "sample-course", result["path"], self.retrieved(result),
                           confirmed=True, running_tasks=[{"status": "running"}])
        self.assertEqual(error.exception.code, "running_tasks")

    def test_pending_reassessment_prevents_release(self):
        self.submit(sid="graded")
        self.assess("graded")
        state = self.library.load("sample-course")
        rubric = state["questions"]["q1"]["versions"]["qv1"]["rubrics"]["r1"]
        self.apply("replace_rubric", **self.context, rubric_version="r2", rubric=rubric,
                   affected_checks=["compute"], reason="评分依据需复核")
        result = self.archive()
        with self.assertRaises(LearnerError) as error:
            release_course(self.library, "sample-course", result["path"], self.retrieved(result), confirmed=True)
        self.assertEqual(error.exception.code, "pending_work")

    def test_source_task_marker_blocks_release_without_changing_records(self):
        result = self.archive()
        verification = self.retrieved(result)
        directory = self.library.course_dir("sample-course")
        before = (directory / "course.json").read_bytes()
        marker = directory / ".task-running-import-queued.json"
        marker.write_text('{"status":"running"}', encoding="utf-8")
        with self.assertRaises(LearnerError) as error:
            release_course(self.library, "sample-course", result["path"], verification, confirmed=True)
        self.assertEqual(error.exception.code, "running_tasks")
        self.assertEqual((directory / "course.json").read_bytes(), before)
        self.assertTrue(marker.is_file())
        self.assertEqual(archived_courses(self.library), [])

    def test_changed_course_requires_new_archive_and_retrieval(self):
        result = self.archive()
        verification = self.retrieved(result)
        self.apply("prepare_part", chapter_id="chapter-1", content_version="v1", part_id="part-1")
        with self.assertRaises(LearnerError) as error:
            release_course(self.library, "sample-course", result["path"], verification, confirmed=True)
        self.assertEqual(error.exception.code, "archive_stale")

    def test_explicit_release_retains_index_and_restores_without_overwrite(self):
        self.submit(sid="kept")
        self.assess("kept")
        result = self.archive()
        drive_copy = copy_to_drive(result["path"], self.root / "fake-drive-retained")
        verification = self.retrieved(result)
        with self.assertRaises(LearnerError):
            release_course(self.library, "sample-course", result["path"], verification)
        release_course(self.library, "sample-course", result["path"], verification, confirmed=True)
        self.assertEqual(archived_courses(self.library)[0]["course_id"], "sample-course")
        self.assertEqual(archived_courses(self.library)[0]["drive_paths"], [str(Path(drive_copy["path"]).resolve())])
        self.assertEqual(archived_courses(self.library)[0]["cloud_sync"], "user_declared_download_verified")
        self.assertTrue(Path(result["path"]).is_file())
        self.assertTrue(Path(verification["path"]).is_file())
        self.assertFalse((self.library.root / "sample-course/course.json").exists())
        restore_archive(self.library, verification["path"], result["sha256"], confirmed=True)
        self.assertEqual(archived_courses(self.library), [])
        self.assertEqual(self.library.load("sample-course")["submissions"]["kept"]["answer"], "5 米每秒")
        before = (self.library.root / "sample-course/course.json").read_bytes()
        with self.assertRaises(LearnerError) as error:
            restore_archive(self.library, result["path"], confirmed=True)
        self.assertEqual(error.exception.code, "course_exists")
        self.assertEqual((self.library.root / "sample-course/course.json").read_bytes(), before)

    def test_archived_course_explains_local_verification_and_network_retrieval(self):
        result = self.archive()
        drive_copy = copy_to_drive(result["path"], self.root / "fake-drive-retained")
        verification = self.retrieved(result)
        release_course(self.library, "sample-course", result["path"], verification, confirmed=True)
        index_path = self.library.root / archives.INDEX_NAME
        index_before = index_path.read_bytes()
        with patch.object(archives, "_sha", side_effect=AssertionError("Listing must not read ZIP bodies")):
            row = archived_courses(self.library)[0]
            self.assertEqual(row["restore_status"], "local_copy_requires_verification")
            self.assertFalse(row["network_required"])
            self.assertFalse(row["local_archive_integrity_verified"])
            self.assertIn("核验后可离线恢复", row["restore_message"])
            with self.assertRaises(LearnerError) as error:
                self.library.load("sample-course")
            self.assertFalse(error.exception.details["network_required"])
            self.assertEqual(self.library.list_courses()[0], row)
        # The Drive directory remains present; it must not masquerade as an
        # independently downloaded ordinary local copy.
        Path(result["path"]).unlink()
        Path(verification["path"]).unlink()
        self.assertTrue(Path(drive_copy["path"]).is_file())
        row = archived_courses(self.library)[0]
        self.assertEqual(row["restore_status"], "download_required")
        self.assertTrue(row["network_required"])
        self.assertEqual(row["local_archive_paths"], [])
        self.assertIn("需要联网取回", row["restore_message"])
        self.assertEqual(row["cloud_sync"], "user_declared_download_verified")
        self.assertEqual(self.library.list_courses()[0], row)
        with self.assertRaises(LearnerError) as error:
            self.library.load_manifest("sample-course")
        self.assertEqual(error.exception.code, "course_archived")
        self.assertTrue(error.exception.details["network_required"])
        self.assertIn("需要联网取回", str(error.exception))
        self.assertEqual(index_path.read_bytes(), index_before)
        # Lightweight indexes remain discoverable even if the empty course
        # directory is no longer present.
        shutil.rmtree(self.library.course_dir("sample-course"))
        self.assertEqual(self.library.list_courses(), [row])

    def test_archive_existence_does_not_claim_integrity_or_enable_corrupt_course(self):
        result = self.archive()
        verification = self.retrieved(result)
        release_course(self.library, "sample-course", result["path"], verification, confirmed=True)
        Path(result["path"]).unlink()
        Path(verification["path"]).write_bytes(b"damaged archive")
        row = archived_courses(self.library)[0]
        self.assertFalse(row["network_required"])
        self.assertFalse(row["local_archive_integrity_verified"])
        self.assertIn("核验", row["restore_message"])
        with self.assertRaises(LearnerError):
            restore_archive(self.library, verification["path"], result["sha256"], confirmed=True)
        self.assertFalse((self.library.root / "sample-course/course.json").exists())

    def test_drive_files_and_windows_cloud_placeholders_are_not_offline_copies(self):
        candidate = self.root / "cloud" / "course.zip"
        candidate.parent.mkdir()
        candidate.write_bytes(b"not a downloaded archive")
        entry = {"retrieved_path": str(candidate), "archive_path": str(candidate),
                 "drive_paths": [str(candidate)]}
        self.assertTrue(archive_availability(entry)["network_required"])
        entry["drive_paths"] = []
        for flag in (0x1000, 0x40000, 0x400000):
            with self.subTest(flag=flag), patch.object(Path, "stat", return_value=SimpleNamespace(
                    st_mode=candidate.stat().st_mode, st_file_attributes=flag)):
                self.assertTrue(archive_availability(entry)["network_required"])

    def test_restore_hint_does_not_traverse_parent_junction_or_cloud_placeholder(self):
        parent = self.root / "redirected"
        parent.mkdir()
        candidate = parent / "course.zip"
        calls = []
        original_lstat = Path.lstat
        for flag in (0x400, 0x1000, 0x40000, 0x400000):
            calls.clear()

            def inspect(path):
                calls.append(path)
                if path == candidate:
                    self.fail("Archive hint traversed a redirected/recall-only directory")
                if path == parent:
                    return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=flag)
                return original_lstat(path)

            with self.subTest(flag=flag), patch.object(Path, "lstat", inspect):
                self.assertTrue(archive_availability({"archive_path": str(candidate)})["network_required"])
            self.assertIn(parent, calls)
            self.assertNotIn(candidate, calls)

    def test_restore_hint_rejects_mapped_network_drive_before_stat(self):
        import ctypes
        candidate = self.root / "not-local.zip"
        with patch.object(archives, "os", SimpleNamespace(name="nt")), \
                patch.object(ctypes, "WinDLL", create=True) as kernel, \
                patch.object(Path, "lstat", side_effect=AssertionError("Network drive must not be probed")):
            kernel.return_value.GetDriveTypeW.return_value = 4  # DRIVE_REMOTE
            self.assertFalse(archives._ordinary_local_archive(candidate))
            kernel.return_value.GetDriveTypeW.assert_called_once_with(candidate.anchor)

    def test_hash_mismatch_and_zip_traversal_never_enable_a_course(self):
        result = self.archive()
        other = Library(self.root / "empty-library")
        other.initialize()
        with self.assertRaises(LearnerError):
            restore_archive(other, result["path"], "0" * 64, confirmed=True)
        malicious = self.root / "malicious.zip"
        with zipfile.ZipFile(result["path"]) as original, zipfile.ZipFile(malicious, "w") as altered:
            manifest = json.loads(original.read("MANIFEST.json"))
            content = b"escape"
            manifest["files"]["../escaped.txt"] = {"bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}
            altered.writestr("MANIFEST.json", canonical(manifest))
            for name in original.namelist():
                if name != "MANIFEST.json":
                    altered.writestr(name, original.read(name))
            altered.writestr("course/../escaped.txt", content)
        with self.assertRaises(LearnerError):
            restore_archive(other, malicious, confirmed=True)
        self.assertFalse((other.root / "sample-course").exists())
        self.assertFalse((self.root / "escaped.txt").exists())

    def test_schema1_archive_preserves_original_bytes_without_migration(self):
        directory = self.library.course_dir("sample-course")
        legacy = materialize(self.library.load("sample-course"), directory)
        legacy["schema_version"] = 1
        original = (json.dumps(legacy, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        atomic_write(directory / "course.json", original)
        result = self.archive()
        self.assertEqual((directory / "course.json").read_bytes(), original)
        with zipfile.ZipFile(result["path"]) as archive:
            self.assertEqual(archive.read("course/course.json"), original)
        other = Library(self.root / "legacy-restored")
        other.initialize()
        restore_archive(other, result["path"], confirmed=True)
        self.assertEqual(other.load_manifest("sample-course")["schema_version"], 1)

    def test_interrupted_release_remains_guarded_and_can_be_restored(self):
        result = self.archive()
        verification = self.retrieved(result)
        directory = self.library.course_dir("sample-course")
        unlink = Path.unlink

        def interrupted(path, *args, **kwargs):
            if path == directory / "course.previous.json":
                raise OSError("simulated removal failure")
            return unlink(path, *args, **kwargs)

        with patch.object(Path, "unlink", interrupted), self.assertRaises(OSError):
            release_course(self.library, "sample-course", result["path"], verification, confirmed=True)
        self.assertFalse((directory / "course.json").exists())
        with self.assertRaises(LearnerError) as error:
            self.library.load("sample-course")
        self.assertEqual(error.exception.code, "course_archived")
        restore_archive(self.library, verification["path"], result["sha256"], confirmed=True)
        self.assertTrue((directory / "course.json").is_file())
        self.assertEqual(archived_courses(self.library), [])

    def test_restore_index_failure_rolls_back_enablement(self):
        result = self.archive()
        verification = self.retrieved(result)
        release_course(self.library, "sample-course", result["path"], verification, confirmed=True)
        self.assertEqual(archived_courses(self.library)[0]["drive_paths"], [])
        self.assertEqual(archived_courses(self.library)[0]["cloud_sync"], "unknown")
        with patch("codex_learner.archives._write_index", side_effect=OSError("index write failed")):
            with self.assertRaises(LearnerError) as error:
                restore_archive(self.library, result["path"], confirmed=True)
        self.assertEqual(error.exception.code, "restore_failed")
        self.assertEqual(error.exception.details["phase"], "write_index")
        self.assertFalse((self.library.root / "sample-course/course.json").exists())
        self.assertEqual(len(archived_courses(self.library)), 1)
        restore_archive(self.library, result["path"], confirmed=True)
        self.assertTrue((self.library.root / "sample-course/course.json").is_file())

    def assert_interrupted_release_restores_matching_residue(self, *, before_first_member):
        result = self.archive()
        verification = self.retrieved(result)
        directory = self.library.course_dir("sample-course")
        manifest = inspect_archive(result["path"])["manifest"]
        ordered_members = list(manifest["files"])
        target_member = ordered_members[0] if before_first_member else "course.json"
        target_index = ordered_members.index(target_member)
        expected_deleted = ordered_members[:target_index]
        expected_remaining = {relative: metadata for relative, metadata in manifest["files"].items()
                              if relative not in expected_deleted}
        unlink = Path.unlink
        injected_error = OSError("simulated release interruption before " + target_member)
        hits, deleted = [], []

        def interrupted(path, *args, **kwargs):
            if path == directory / target_member:
                hits.append(target_member)
                raise injected_error
            value = unlink(path, *args, **kwargs)
            if path.is_relative_to(directory):
                deleted.append(path.relative_to(directory).as_posix())
            return value

        with patch.object(Path, "unlink", interrupted), self.assertRaises(OSError) as error:
            release_course(self.library, "sample-course", result["path"], verification, confirmed=True)
        self.assertIs(error.exception, injected_error)
        self.assertEqual(hits, [target_member])
        self.assertEqual(deleted, expected_deleted)
        if before_first_member:
            self.assertEqual(deleted, [])
            self.assertEqual(len(expected_remaining), len(ordered_members))
        else:
            self.assertGreater(len(deleted), 0)
            self.assertLess(len(expected_remaining), len(ordered_members))

        def actual_files():
            return {path.relative_to(directory).as_posix(): {
                "bytes": path.stat().st_size, "sha256": archives._sha(path),
            } for path in directory.rglob("*") if path.is_file() and path.name != ".lock"}

        expected = {relative: {"bytes": metadata["bytes"], "sha256": metadata["sha256"]}
                    for relative, metadata in expected_remaining.items()}
        actual = actual_files()
        self.assertEqual(actual, expected)
        print("Release interruption evidence: " + json.dumps({
            "target_member": target_member, "injection_hits": hits,
            "manifest_files": len(ordered_members), "deleted_files": deleted,
            "remaining_files": len(actual),
            "remaining_lengths_sha256": actual,
        }, sort_keys=True))
        self.assertTrue((directory / "course.json").exists())
        self.assertEqual(len(archived_courses(self.library)), 1)
        with self.assertRaises(LearnerError) as error:
            self.library.load("sample-course")
        self.assertEqual(error.exception.code, "course_archived")
        restore_archive(self.library, result["path"], confirmed=True)
        self.assertEqual(archived_courses(self.library), [])
        self.assertEqual(self.library.load_manifest("sample-course")["state_revision"], result["state_revision"])
        self.assertEqual(actual_files(), {
            relative: {"bytes": metadata["bytes"], "sha256": metadata["sha256"]}
            for relative, metadata in manifest["files"].items()})

    def test_release_failure_before_course_manifest_unlink_can_restore_matching_residue(self):
        self.assert_interrupted_release_restores_matching_residue(before_first_member=False)

    def test_release_failure_before_first_manifest_member_unlink_can_restore_matching_residue(self):
        self.assert_interrupted_release_restores_matching_residue(before_first_member=True)

    def test_preserved_damaged_original_is_archived_as_opaque_evidence(self):
        directory = self.library.course_dir("sample-course")
        damaged = b'{"broken": "unterminated original'
        (directory / "before-restore-damaged.json").write_bytes(damaged)
        result = self.archive()
        files = inspect_archive(result["path"])["manifest"]["files"]
        self.assertTrue(files["before-restore-damaged.json"]["opaque_preserved_original"])
        other = Library(self.root / "preserved-original-library")
        other.initialize()
        restore_archive(other, result["path"], confirmed=True)
        self.assertEqual((other.root / "sample-course/before-restore-damaged.json").read_bytes(), damaged)
        self.assertEqual(other.load_manifest("sample-course")["course_id"], "sample-course")

    def test_restore_index_post_commit_error_keeps_enabled_course(self):
        result = self.archive()
        verification = self.retrieved(result)
        release_course(self.library, "sample-course", result["path"], verification, confirmed=True)
        write_index = archives._write_index

        def committed_then_failed(library, value):
            write_index(library, value)
            raise OSError("directory fsync failed after replacement")

        with patch("codex_learner.archives._write_index", committed_then_failed):
            restored = restore_archive(self.library, result["path"], confirmed=True)
        self.assertEqual(restored["status"], "course_restored")
        self.assertIn("readback", restored["warning"])
        self.assertEqual(archived_courses(self.library), [])
        self.assertEqual(self.library.load_manifest("sample-course")["state_revision"], result["state_revision"])

    def test_archive_disk_shortage_does_not_publish_or_change_course(self):
        directory = self.library.course_dir("sample-course")
        before = (directory / "course.json").read_bytes()
        with patch("codex_learner.archives.shutil.disk_usage", return_value=SimpleNamespace(free=0)):
            with self.assertRaises(LearnerError) as error:
                self.archive()
        self.assertEqual(error.exception.code, "disk_space")
        self.assertEqual((directory / "course.json").read_bytes(), before)
        self.assertFalse((self.root / "archives/course.zip").exists())
        self.assertFalse((self.root / "archives/course.zip.sha256").exists())
        self.assertEqual(list(directory.glob(".archive-running-*.json")), [])
        self.assertEqual(archived_courses(self.library), [])

    def test_restore_disk_shortage_does_not_enable_or_delete_course(self):
        result = self.archive()
        other = Library(self.root / "low-disk-restore")
        other.initialize()
        before = (self.library.root / "sample-course/course.json").read_bytes()
        with patch("codex_learner.archives.shutil.disk_usage",
                   return_value=SimpleNamespace(free=archives.DISK_RESERVE_BYTES)):
            with self.assertRaises(LearnerError) as error:
                restore_archive(other, result["path"], confirmed=True)
        self.assertEqual(error.exception.code, "disk_space")
        self.assertFalse((other.root / "sample-course").exists())
        self.assertEqual((self.library.root / "sample-course/course.json").read_bytes(), before)
        self.assertTrue(Path(result["path"]).is_file())

    def test_drive_copy_disk_shortage_is_local_and_preserves_archive(self):
        result = self.archive()
        before = (self.library.root / "sample-course/course.json").read_bytes()
        with patch("codex_learner.archives.shutil.disk_usage",
                   return_value=SimpleNamespace(free=archives.DISK_RESERVE_BYTES)):
            with self.assertRaises(LearnerError) as error:
                copy_to_drive(result["path"], self.root / "low-disk-drive")
        self.assertEqual(error.exception.code, "disk_space")
        self.assertEqual(error.exception.details["scope"], "local_filesystem")
        self.assertFalse((self.root / "low-disk-drive/course.zip").exists())
        self.assertEqual((self.library.root / "sample-course/course.json").read_bytes(), before)
        self.assertEqual(inspect_archive(result["path"])["sha256"], result["sha256"])

    def test_same_volume_space_budget_keeps_both_archive_copies(self):
        source = self.root / "capacity.txt"
        source.write_text("\n".join(chr(65 + i) * (64 * 1024) for i in range(16)), encoding="utf-8")
        self.apply("import_source", source_id="capacity-source", path=str(source))
        before = (self.library.root / "sample-course/course.json").read_bytes()
        # Original plus extracted text total about 2 MiB. Three MiB can hold
        # one conservative ZIP budget, but not staging and publication together.
        free = archives.DISK_RESERVE_BYTES + 3 * 1024 * 1024
        with patch("codex_learner.archives.shutil.disk_usage", return_value=SimpleNamespace(free=free)):
            with self.assertRaises(LearnerError) as error:
                self.archive()
        self.assertEqual(error.exception.code, "disk_space")
        self.assertIn("temporary and destination", str(error.exception))
        self.assertGreater(error.exception.details["required_bytes"], free)
        self.assertFalse((self.root / "archives/course.zip").exists())
        self.assertEqual((self.library.root / "sample-course/course.json").read_bytes(), before)

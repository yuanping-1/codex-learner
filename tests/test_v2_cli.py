"""Cross-process CLI contracts using temporary libraries and explicit config.

The Drive directory and declared web download below are local fixtures. These
tests prove CLI persistence and package verification, never actual cloud upload
or browser download provenance.
"""

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

from support import CourseCase, ROOT
from codex_learner.blobs import materialize


class V2CliTests(CourseCase):
    def setUp(self):
        super().setUp()
        self.cwd = self.root / "unrelated-working-directory"
        self.cwd.mkdir()
        self.drive = self.root / "simulated-drive-streaming-folder"
        self.drive.mkdir()
        self.config = self.root / "explicit-config.json"
        self.settings = {"schema_version": 1, "library_root": str(self.library.root),
                         "python_executable": sys.executable,
                         "memory": {"core_target_mb": 192},
                         "drive": {"directory": str(self.drive)}}
        self.config.write_text(json.dumps(self.settings), encoding="utf-8")

    def cli(self, *arguments, error=None, config=None):
        process = subprocess.run(
            [sys.executable, str(ROOT / "skills/codex-learner/scripts/learner.py"),
             "--config", str(config or self.config), *map(str, arguments)],
            cwd=self.cwd, capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(process.returncode, 2 if error else 0, process.stderr or process.stdout)
        payload = json.loads(process.stderr if error else process.stdout)
        self.assertEqual(payload["ok"], error is None)
        if error:
            self.assertEqual(payload["error"], error)
            self.assertEqual(process.stdout, "")
            return payload
        self.assertEqual(process.stderr, "")
        return payload["result"]

    def apply_cli(self, request, **kwargs):
        path = self.root / "request.json"
        path.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
        return self.cli("apply", "--request", path, **kwargs)

    def answer_request(self, sid="cli-answer", answer="完整原答\r\n保留换行与中文。"):
        return self.request("submit_answer", **self.context, submission_id=sid, answer=answer)

    def test_configured_doctor_and_read_export_survive_new_processes(self):
        doctor = self.cli("doctor")
        self.assertEqual(doctor["library"], str(self.library.root))
        self.assertEqual(doctor["config_path"], str(self.config))
        self.assertEqual(doctor["core_target_mib"], 192)
        self.assertTrue(doctor["drive"]["active_library_separate"])
        self.assertEqual(doctor["drive"]["cloud_sync"], "unconfirmed")

        request = self.answer_request()
        receipt = self.apply_cli(request)
        revision = receipt["state_revision"]
        self.assertEqual(receipt["result"]["answer"], request["answer"])
        self.assertTrue(self.apply_cli(request)["duplicate"])
        self.apply_cli({**request, "answer": "同一 ID 的不同内容"}, error="id_conflict")
        self.assertEqual(self.library.load("sample-course")["state_revision"], revision)

        history = self.cli("history", "--course", "sample-course", "--page-size", "1")
        self.assertEqual(history["total"], 1)
        self.assertNotIn("answer", history["items"][0])
        self.assertEqual(history["items"][0]["submission_id"], request["submission_id"])
        read = self.cli("read", "--course", "sample-course", "--submission", request["submission_id"])
        self.assertEqual(read["answer"], request["answer"])
        exported = self.root / "exports" / "course-expanded.json"
        result = self.cli("export", "--course", "sample-course", "--output", exported)
        self.assertFalse(result["restorable_archive"])
        self.assertEqual(result["sha256"], hashlib.sha256(exported.read_bytes()).hexdigest())
        self.assertEqual(json.loads(exported.read_text(encoding="utf-8"))["submissions"]["cli-answer"]["answer"],
                         request["answer"])

    def test_migration_preview_hash_confirmation_and_original_receipt_retry(self):
        request = self.answer_request(sid="legacy-cli")
        self.apply_cli(request)
        directory = self.library.course_dir("sample-course")
        legacy = materialize(self.library.load("sample-course"), directory)
        legacy["schema_version"] = 1
        legacy.pop("source_verifications", None)
        original = (json.dumps(legacy, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        (directory / "course.json").write_bytes(original)
        prior_backup = (directory / "course.previous.json").read_bytes()

        preview = self.cli("migrate", "--course", "sample-course", "--preview")
        self.assertTrue(preview["preview"])
        self.assertFalse(preview["changed"])
        self.assertEqual(preview["original_sha256"], hashlib.sha256(original).hexdigest())
        self.assertEqual((directory / "course.json").read_bytes(), original)
        self.cli("migrate", "--course", "sample-course", "--expected-sha256", "0" * 64,
                 "--confirm", error="migration_changed")
        # Supplying a preview hash alone must still leave migration unconfirmed.
        no_confirmation = self.cli("migrate", "--course", "sample-course", "--expected-sha256",
                                   preview["original_sha256"])
        self.assertTrue(no_confirmation["preview"])
        self.assertEqual((directory / "course.json").read_bytes(), original)
        result = self.cli("migrate", "--course", "sample-course", "--expected-sha256",
                          preview["original_sha256"], "--confirm")
        self.assertTrue(result["changed"])
        self.assertEqual(self.library.load_manifest("sample-course")["schema_version"], 2)
        self.assertEqual((Path(result["backup"]) / "course.json").read_bytes(), original)
        self.assertEqual((Path(result["backup"]) / "course.previous.json").read_bytes(), prior_backup)
        duplicate = self.apply_cli(request)
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(duplicate["result"]["answer"], request["answer"])

    def test_local_archive_cli_roundtrip_with_simulated_download_declaration(self):
        request = self.answer_request(sid="archived-cli")
        self.apply_cli(request)
        self.assess("archived-cli")
        directory = self.library.course_dir("sample-course")
        original = (directory / "course.json").read_bytes()
        archive = self.root / "archives" / "course.zip"
        created = self.cli("archive-create", "--course", "sample-course", "--output", archive)
        checksum = created["sha256"]
        checked = self.cli("archive-check", "--archive", archive, "--sha256", checksum)
        self.assertEqual(checked["status"], "archive_verified")
        self.assertEqual(checked["manifest"]["course_id"], "sample-course")

        self.cli("archive-copy", "--archive", archive, "--drive-directory", self.library.root,
                 error="invalid_path")
        copied = self.cli("archive-copy", "--archive", archive)
        self.assertEqual(Path(copied["path"]).parent, self.drive)
        self.assertEqual(copied["status"], "sync_unconfirmed")
        self.assertFalse(copied["cloud_upload_verified"])
        verification = self.root / "download-verification.json"
        self.cli("archive-confirm-download", "--archive", archive, "--sha256", checksum,
                 "--download", copied["path"], "--source-declaration", "web_download",
                 "--output", verification, error="download_not_independent")
        self.assertFalse(verification.exists())

        # Deliberately simulated: no browser or network is used by this test.
        download = self.root / "simulated-web-downloads" / "retrieved.zip"
        download.parent.mkdir()
        shutil.copyfile(archive, download)
        verified = self.cli("archive-confirm-download", "--archive", archive, "--sha256", checksum,
                            "--download", download, "--source-declaration", "web_download",
                            "--output", verification)
        self.assertEqual(verified["network_provenance"], "user_declared")
        self.assertEqual(json.loads(verification.read_text(encoding="utf-8")), verified)
        release_arguments = ("archive-release", "--course", "sample-course", "--archive", archive,
                             "--verification", verification)
        self.cli(*release_arguments, error="confirmation_required")
        self.assertEqual((directory / "course.json").read_bytes(), original)
        released = self.cli(*release_arguments, "--confirm")
        self.assertEqual(released["status"], "archived")
        self.assertFalse((directory / "course.json").exists())
        self.assertEqual(self.cli("archive-list")[0]["course_id"], "sample-course")
        self.cli("read", "--course", "sample-course", "--submission", "archived-cli", error="course_archived")
        restore_arguments = ("archive-restore", "--archive", download, "--sha256", checksum)
        self.cli(*restore_arguments, error="confirmation_required")
        self.assertFalse((directory / "course.json").exists())
        restored = self.cli(*restore_arguments, "--confirm")
        self.assertFalse(restored["network_required"])
        self.assertEqual(self.cli("archive-list"), [])
        self.assertEqual((directory / "course.json").read_bytes(), original)
        self.assertEqual(self.cli("read", "--course", "sample-course", "--submission", "archived-cli")["answer"],
                         request["answer"])

    def test_bad_nested_configuration_returns_json_without_touching_library(self):
        before = (self.library.course_dir("sample-course") / "course.json").read_bytes()
        malformed = self.root / "bad-config.json"
        malformed.write_text(json.dumps({**self.settings, "drive": []}), encoding="utf-8")
        self.cli("doctor", config=malformed, error="invalid_request")
        self.assertEqual((self.library.course_dir("sample-course") / "course.json").read_bytes(), before)

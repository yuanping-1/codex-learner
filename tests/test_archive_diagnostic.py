"""Focused regression diagnostics preserve fixtures, scope and original errors."""

import builtins
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import support
import test_archives as archive_tests
import diagnose_archive_regression as diagnostic
from codex_learner import archives


class ArchiveDiagnosticTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.scripts = support.ROOT / "skills/codex-learner/scripts"

    def run_sample(self, label, case="partial", **kwargs):
        return diagnostic.run_sample(self.root / label, self.scripts, case,
                                     archive_tests.ArchiveTests, **kwargs)

    def test_selected_real_case_retains_archive_residue_evidence_and_verified_readback(self):
        row = self.run_sample("partial")
        self.assertEqual(row["status"], "PASS", row)
        self.assertEqual(row["tests_run"], 1)
        self.assertEqual(row["test_method"], diagnostic.CASES["partial"])
        self.assertTrue(row["diagnostic_only"])
        self.assertFalse(row["first_call_acceptance_eligible"])
        self.assertFalse(row["automatic_retry"])
        self.assertEqual(row["failure_observation"], "NOT_CAPTURED")
        self.assertEqual(row["root_cause"], "UNCONFIRMED")
        self.assertTrue(row["archive_creation_attempted"])
        self.assertTrue(row["archive_creation_call_completed"])
        self.assertTrue(row["fixture_preserved"])
        case = self.root / "partial"
        self.assertTrue((case / "fixture/archives/course.zip").is_file())
        self.assertTrue((case / "fixture/sample-library/sample-course/course.json").is_file())
        self.assertTrue((case / "after-test.json").is_file())
        evidence = (case / "test.stdout.txt").read_text(encoding="utf-8")
        release = json.loads(evidence.split("Release interruption evidence: ", 1)[1].splitlines()[0])
        self.assertEqual(release["target_member"], "course.json")
        self.assertGreater(len(release["deleted_files"]), 0)
        self.assertLess(release["remaining_files"], release["manifest_files"])
        self.assertEqual(row["offline_verification"]["status"], "PASS")

    def test_full_residue_runs_only_the_first_manifest_member_case(self):
        with patch.object(archive_tests.ArchiveTests, diagnostic.CASES["partial"],
                          side_effect=AssertionError("Wrong test was selected")):
            row = self.run_sample("full", "full")
        self.assertEqual(row["status"], "PASS", row)
        evidence = (self.root / "full/test.stdout.txt").read_text(encoding="utf-8")
        release = json.loads(evidence.split("Release interruption evidence: ", 1)[1].splitlines()[0])
        self.assertEqual(release["deleted_files"], [])
        self.assertEqual(release["remaining_files"], release["manifest_files"])

    def test_publication_error_is_observed_before_rollback_and_original_error_is_retained(self):
        rename, opened = Path.rename, builtins.open
        denied = PermissionError("controlled publish denial")
        denied.winerror = 5

        def fail_publish(path, target):
            if path.parent.name.startswith(".learner-restore-") and path.name == "sample-course":
                raise denied
            return rename(path, target)

        with patch.object(Path, "rename", fail_publish):
            row = self.run_sample("failed")
            self.assertIs(Path.rename, fail_publish)
        self.assertIs(Path.rename, rename)
        self.assertIs(builtins.open, opened)
        self.assertEqual(row["status"], "FAIL")
        error = row["errors"][0]["error"]
        self.assertEqual(error["structured"]["phase"], "publish_course")
        self.assertEqual(error["structured"]["original_error"]["winerror"], 5)
        self.assertIn("controlled publish denial", error["traceback"])
        self.assertEqual(row["errors"][0]["causes"][0]["message"], str(denied))
        self.assertEqual(row["publication_failure_events"], 1)
        event = row["probe"]["events"][0]
        source = next(value for value in event["observations"]["paths"]
                      if value["path"] == event["source_path"])
        self.assertTrue(source["exists"])
        self.assertFalse(Path(event["source_path"]).exists())
        self.assertTrue((self.root / "failed/fixture/archives/course.zip").is_file())

    def test_replay_uses_verified_bytes_and_explicitly_skips_new_archive_creation(self):
        first = self.run_sample("original")
        self.assertEqual(first["status"], "PASS", first)
        archive = self.root / "original/fixture/archives/course.zip"
        before = archive.read_bytes()
        checksum = hashlib.sha256(before).hexdigest()
        # The test-only SHA substitutes a freshly generated synthetic fixture;
        # the CLI retains its fixed original native archive SHA.
        with patch.object(diagnostic, "NATIVE_ARCHIVE_SHA256", checksum), \
                patch.object(archive_tests, "create_archive", side_effect=AssertionError("Replay must skip creation")):
            row = self.run_sample("replay", archive=archive)
        self.assertEqual(row["status"], "PASS", row)
        self.assertEqual(row["fixture_origin"], "verified_native_failure_archive")
        self.assertFalse(row["archive_creation_expected"])
        self.assertFalse(row["archive_creation_attempted"])
        self.assertFalse(row["archive_creation_call_completed"])
        self.assertEqual(archive.read_bytes(), before)
        self.assertEqual((self.root / "replay/fixture/archives/course.zip").read_bytes(), before)

    def test_probe_reporting_failure_does_not_remove_original_test_error(self):
        rename = Path.rename

        def fail_publish(path, target):
            if path.parent.name.startswith(".learner-restore-") and path.name == "sample-course":
                raise PermissionError("original product denial")
            return rename(path, target)

        with patch.object(Path, "rename", fail_publish), \
                patch.object(diagnostic.RestoreFailureProbe, "report", side_effect=OSError("report unavailable")):
            row = self.run_sample("unknown")
        self.assertEqual(row["status"], "FAIL")
        self.assertEqual(row["errors"][0]["error"]["structured"]["phase"], "publish_course")
        self.assertEqual(row["errors"][1]["kind"], "PROBE_REPORT_ERROR")
        self.assertIsNone(row["publication_failure_events"])
        self.assertEqual(row["failure_observation"], "UNKNOWN")

    def test_setup_failure_does_not_claim_archive_creation_or_a_preserved_fixture(self):
        with patch.object(archive_tests.ArchiveTests, "setUp", side_effect=OSError("setup failed early")):
            row = self.run_sample("setup-failed")
        self.assertEqual(row["status"], "FAIL")
        self.assertTrue(row["archive_creation_expected"])
        self.assertFalse(row["archive_creation_attempted"])
        self.assertFalse(row["archive_creation_call_completed"])
        self.assertFalse(row["fixture_preserved"])
        self.assertEqual(row["failure_observation"], "NOT_STARTED")
        self.assertFalse(row["event_count_complete"])

    def test_observer_error_does_not_make_zero_recorded_events_a_complete_observation(self):
        report = diagnostic.RestoreFailureProbe.report

        def incomplete(probe):
            value = report(probe)
            value["observer_errors"].append({"phase": "capture", "error": {"message": "collector failed"}})
            return value

        with patch.object(diagnostic.RestoreFailureProbe, "report", incomplete):
            row = self.run_sample("incomplete-observation")
        self.assertEqual(row["status"], "PASS")  # The business test/readback still passed.
        self.assertEqual(row["publication_failure_events"], 0)
        self.assertFalse(row["event_count_complete"])
        self.assertEqual(row["failure_observation"], "UNKNOWN")

    def test_archive_creation_error_preserves_actual_attempt_without_claiming_completion(self):
        with patch.object(archive_tests, "create_archive", side_effect=OSError("creation failed")):
            row = self.run_sample("archive-failed")
        self.assertEqual(row["status"], "FAIL")
        self.assertTrue(row["archive_creation_attempted"])
        self.assertFalse(row["archive_creation_call_completed"])
        self.assertTrue(row["fixture_preserved"])

    def test_rejects_old_output_non_whitelisted_case_and_unverified_replay(self):
        existing = self.root / "old"
        existing.mkdir()
        marker = existing / "kept"
        marker.write_bytes(b"old evidence")
        with self.assertRaises(FileExistsError):
            self.run_sample("old")
        with self.assertRaises(ValueError):
            self.run_sample("invalid", "arbitrary-test")
        bad_archive = self.root / "bad.zip"
        bad_archive.write_bytes(b"not the fixed fixture")
        with self.assertRaises(ValueError):
            self.run_sample("bad", archive=bad_archive)
        self.assertEqual(marker.read_bytes(), b"old evidence")
        self.assertFalse((self.root / "invalid").exists())
        self.assertFalse((self.root / "bad").exists())

    def test_cli_worker_identity_and_scope_are_bound_to_isolated_source(self):
        out = self.root / "cli"
        command = [sys.executable, str(support.ROOT / "scripts/diagnose_archive_regression.py"),
                   "--scripts-root", str(self.scripts), "--output-dir", str(out), "--case", "partial"]
        run = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=90)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        report = json.loads((out / "results.json").read_text(encoding="utf-8"))
        self.assertTrue(report["complete"])
        self.assertEqual(len(report["samples"]), 1)
        self.assertFalse(report["first_call_acceptance_eligible"])
        worker = json.loads((out / "sample-01/result.json").read_text(encoding="utf-8"))
        self.assertNotEqual(worker["pid"], __import__("os").getpid())
        self.assertEqual(worker["identity_before"]["source_files_sha256"],
                         worker["identity_after"]["source_files_sha256"])
        self.assertTrue(worker["identity_after"]["actual_product_modules"])
        for module in worker["identity_after"]["actual_product_modules"].values():
            self.assertTrue(Path(module["path"]).is_relative_to(self.scripts.resolve()))
        self.assertIn("--_worker", report["samples"][0]["command"])
        self.assertTrue((out / "sample-01-command.json").is_file())


if __name__ == "__main__":
    unittest.main()

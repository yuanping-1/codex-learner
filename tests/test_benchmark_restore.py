"""The native restore driver cannot hide early failures or rewrite evidence."""

import contextlib
import io
import json
from pathlib import Path
import subprocess
from unittest.mock import patch

from support import CourseCase, ROOT
from codex_learner import archives
from codex_learner.common import LearnerError
from codex_learner.storage import Library
import benchmark_restore as benchmark


class RestoreBenchmarkTests(CourseCase):
    def fixture(self):
        self.submit(sid="kept", answer="原答\r\n\tunit\u00a0  \n")
        self.assess("kept")
        return archives.create_archive(self.library, "sample-course", self.root / "archive.zip")

    def run_case(self, name, archive, group, **kwargs):
        return benchmark.run_case(self.root / name, ROOT / "skills/codex-learner/scripts",
                                  archive["path"], archive["sha256"], group, **kwargs)

    def test_empty_and_released_first_calls_verify_actual_bytes_and_keep_original_answers(self):
        archive = self.fixture()
        for group in ("empty", "released"):
            with self.subTest(group=group):
                result = self.run_case(group, archive, group)
                self.assertEqual(result["status"], "PASS", result)
                self.assertEqual(len(result["restore_calls"]), 1)
                self.assertEqual(result["restore_calls"][0]["tag"], "primary-first")
                self.assertEqual(result["preparation_restore_calls"], 0)
                self.assertFalse(result["automatic_retry"])
                self.assertTrue(result["first_call_acceptance_eligible"])
                check = json.loads((self.root / group / "offline-verification.json").read_text(encoding="utf-8"))
                self.assertTrue(all(row["match"] for row in check["files"]))
                self.assertIn("course.previous.json", check["transitive_reference_counts"])
                expanded = json.loads((self.root / group / "offline-expanded-snapshots.json").read_text(encoding="utf-8"))
                self.assertEqual(expanded["course.json"]["submissions"]["kept"]["answer"], "原答\r\n\tunit\u00a0  \n")
                self.assertEqual(result["identity_before"]["source_files_sha256"],
                                 result["identity_after"]["source_files_sha256"])
                for module in result["identity_after"]["actual_product_modules"].values():
                    self.assertEqual(benchmark.sha(module["path"]), module["sha256"])

    def test_fault_chain_reaches_index_then_restores_same_library_in_same_process(self):
        result = self.run_case("fault", self.fixture(), "fault-chain")
        self.assertEqual(result["status"], "PASS", result)
        self.assertEqual(result["normal_after_injection_status"], "PASS")
        self.assertEqual(len(result["index_injection_hits"]), 1)
        before, after = result["restore_calls"]
        self.assertEqual(before["tag"], "injected-index")
        self.assertEqual(before["error"]["structured"]["phase"], "write_index")
        self.assertEqual(before["error"]["structured"]["original_error"]["message"], benchmark.INJECTED_ERROR)
        self.assertEqual(after["tag"], "normal-after-injection")
        self.assertEqual(before["pid"], after["pid"])
        self.assertFalse(result["first_call_acceptance_eligible"])
        # The driver resolves its fresh case root; Windows may expand RUNNER~1.
        # Assert the same real destination, not equality of two path spellings.
        self.assertEqual(Path(after["receipt"]["path"]),
                         (self.root / "fault/library/sample-course").resolve())

    def test_early_publish_failure_does_not_satisfy_index_injection_or_retry(self):
        archive = self.fixture()
        rename = Path.rename

        def fail_publish(path, target):
            if path.parent.name.startswith(".learner-restore-") and path.name == "sample-course":
                raise PermissionError("Synthetic early publication denial")
            return rename(path, target)

        with patch.object(Path, "rename", fail_publish):
            result = self.run_case("early", archive, "fault-chain")
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["normal_after_injection_status"], "BLOCKED")
        self.assertEqual(result["index_injection_hits"], [])
        self.assertEqual(len(result["restore_calls"]), 1)
        self.assertEqual(result["restore_calls"][0]["error"]["structured"]["phase"], "publish_course")
        self.assertIn("Synthetic early publication denial", result["restore_calls"][0]["error"]["traceback"])
        self.assertTrue((self.root / "early/input.zip").is_file())
        self.assertTrue((self.root / "early/states/injected-index-after/.archive-index.json").is_file())

    def test_fault_chain_readback_failure_never_leaves_a_passed_normal_restore(self):
        archive = self.fixture()
        with patch.object(benchmark, "offline_verify", side_effect=AssertionError("Synthetic readback failure")):
            result = self.run_case("readback-failure", archive, "fault-chain")
        self.assertEqual(result["restore_calls"][1]["status"], "SUCCESS")
        self.assertEqual(result["normal_after_injection_status"], "FAIL")
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("Synthetic readback failure", result["terminal_error"]["message"])

    def test_diagnostic_captures_real_raised_publish_error_before_product_cleanup(self):
        archive = self.fixture()
        rename = Path.rename

        def fail_publish(path, target):
            if path.parent.name.startswith(".learner-restore-") and path.name == "sample-course":
                raise PermissionError("Synthetic diagnostic publication denial")
            return rename(path, target)

        with patch.object(Path, "rename", fail_publish):
            result = self.run_case("diagnostic", archive, "empty", diagnostic=True)
        self.assertEqual(result["status"], "FAIL")
        self.assertFalse(result["first_call_acceptance_eligible"])
        self.assertEqual(len(result["restore_calls"]), 1)
        row = result["restore_calls"][0]
        self.assertEqual(row["error"]["structured"]["phase"], "publish_course")
        self.assertFalse(row["error"]["structured"]["stage_exists"])
        probe = row["probe_report"]
        self.assertEqual(len(probe["events"]), 1)
        self.assertEqual(probe["observer_errors"], [])
        self.assertTrue(Path(probe["events"][0]["evidence_path"]).is_file())

    def test_existing_sample_and_bad_input_are_rejected_before_any_library_write(self):
        archive = self.fixture()
        present = self.root / "present"
        present.mkdir()
        protected = present / "untouched.txt"
        protected.write_bytes(b"kept")
        with self.assertRaises(FileExistsError):
            self.run_case("present", archive, "empty")
        self.assertEqual(protected.read_bytes(), b"kept")
        bad = dict(archive, sha256="0" * 64)
        with self.assertRaises(ValueError):
            self.run_case("bad", bad, "empty")
        self.assertFalse((self.root / "bad").exists())

    def test_offline_readback_rejects_changed_file_instead_of_marking_restore_pass(self):
        archive = self.fixture()
        result = self.run_case("changed", archive, "empty")
        self.assertEqual(result["status"], "PASS", result)
        case = self.root / "changed"
        manifest = json.loads((case / "archive-manifest.json").read_text(encoding="utf-8"))
        body = next(name for name in manifest["files"] if name.startswith("bodies/"))
        path = case / "library/sample-course" / body
        path.write_bytes(b"x" * path.stat().st_size)
        with self.assertRaises((AssertionError, LearnerError)):
            benchmark.offline_verify(Library(case / "library"), manifest, case)

    def test_supervisor_keeps_success_failure_timeout_and_raw_outputs_without_retry(self):
        archive = self.fixture()
        calls = []

        def worker(command, **kwargs):
            calls.append(command)
            case = Path(command[command.index("--output-dir") + 1])
            if len(calls) == 3:
                raise subprocess.TimeoutExpired(command, 1, output=b"partial\xff", stderr=b"timed out")
            case.mkdir()
            status = "FAIL" if len(calls) == 2 else "PASS"
            benchmark.write(case / "result.json", {"status": status, "started_at_utc": benchmark.utc(),
                                                    "completed_at_utc": benchmark.utc(), "pid": 100,
                                                    "restore_calls": [{"tag": "primary-first"}]})
            return subprocess.CompletedProcess(command, 1 if status == "FAIL" else 0, b"output\xff", b"detail")

        output = self.root / "supervised"
        argv = ["--scripts-root", str(ROOT / "skills/codex-learner/scripts"),
                "--f06-archive", archive["path"], "--s05-archive", archive["path"],
                "--output-dir", str(output), "--repeat", "2"]
        with patch.dict(benchmark.FIXTURE_SHA256, {"F06": archive["sha256"], "S05": archive["sha256"]}), \
                patch.object(benchmark.subprocess, "run", side_effect=worker), \
                contextlib.redirect_stdout(io.StringIO()):
            status = benchmark.main(argv)
        self.assertEqual(status, 1)
        report = json.loads((output / "results.json").read_text(encoding="utf-8"))
        self.assertTrue(report["complete"])
        self.assertEqual(report["groups"], {"empty": {"attempts": 2, "PASS": 1, "FAIL": 1,
                                                     "primary_first_calls_reached": 2},
                                            "released": {"attempts": 2, "PASS": 1, "FAIL": 1,
                                                         "primary_first_calls_reached": 1}})
        self.assertEqual(len(calls), 4)
        self.assertEqual(len({str(Path(c[c.index("--output-dir") + 1])) for c in calls}), 4)
        self.assertEqual((output / "empty-02.stdout.txt").read_bytes(), b"output\xff")
        self.assertEqual((output / "released-01.stdout.txt").read_bytes(), b"partial\xff")
        self.assertTrue(report["samples"][2]["timeout"])
        self.assertEqual(report["samples"][1]["returncode"], 1)
        self.assertTrue(all(row["completed_at_utc"] for row in report["samples"]))

    def test_diagnostics_never_populate_uninstrumented_first_call_groups(self):
        rows = [{"group": group, "status": "PASS"} for group in ("empty", "released")]
        result = benchmark.summarize(rows, mode="diagnostic", repeat=1)
        self.assertEqual(result["status"], "PASS")
        self.assertTrue(result["diagnostics_only"])
        self.assertEqual(result["first_call_groups"], {})

import os
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/codex-learner/scripts"))

from codex_learner.resource_limits import RETURN_LOG_BYTES, run_limited

try:
    import psutil
except ImportError:
    psutil = None


@unittest.skipIf(psutil is None, "Resource watchdog tests require optional psutil")
class ResourceLimitTests(unittest.TestCase):
    def run_python(self, source, **kwargs):
        return run_limited([sys.executable, "-c", source], **kwargs)

    def test_return_code_and_separate_logs(self):
        result = self.run_python("import sys; print('out'); print('err',file=sys.stderr); sys.exit(7)")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["returncode"], 7)
        self.assertEqual(result["stdout"].strip(), "out")
        self.assertEqual(result["stderr"].strip(), "err")
        self.assertEqual(result["hard_limit_enforced"], os.name == "nt")
        self.assertGreater(result["peak_rss_bytes"], 0)
        self.assertTrue(result["cleanup_verified"])
        self.assertEqual(result["surviving_process_ids"], [])
        self.assertTrue(result["observed_process_ids"])

    def test_normal_exit_cleans_background_child(self):
        child = "import time; time.sleep(30)"
        source = ("import subprocess,sys,time; "
                  f"p=subprocess.Popen([sys.executable,'-c',{child!r}]); "
                  "print(p.pid,flush=True); time.sleep(0.5)")
        result = self.run_python(source)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["returncode"], 0)
        child_pid = int(result["stdout"].strip())
        self.assertIn(child_pid, result["observed_process_ids"])
        self.assertTrue(result["cleanup_verified"])
        self.assertEqual(result["surviving_process_ids"], [])

    def test_output_returned_as_bounded_tail(self):
        result = self.run_python("print('x'*300000); print('tail-marker')")
        self.assertEqual(result["returncode"], 0)
        self.assertTrue(result["output_truncated"])
        self.assertLessEqual(len(result["stdout"].encode()), RETURN_LOG_BYTES)
        self.assertTrue(result["stdout"].endswith("tail-marker\n"))

    def test_timeout_kills_descendant(self):
        with tempfile.TemporaryDirectory() as temp:
            marker = Path(temp) / "child-pid"
            child = "import time; time.sleep(30)"
            source = ("import subprocess,sys,time; from pathlib import Path; "
                      f"p=subprocess.Popen([sys.executable,'-c',{child!r}]); "
                      f"Path({str(marker)!r}).write_text(str(p.pid)); time.sleep(30)")
            result = self.run_python(source, timeout=1)
            self.assertEqual(result["status"], "timeout")
            self.assertLess(result["elapsed_seconds"], 6)
            self.assertTrue(marker.exists())
            self.assertTrue(result["cleanup_verified"])
            self.assertEqual(result["surviving_process_ids"], [])
            self.assertIn(int(marker.read_text()), result["observed_process_ids"])
            try:
                process = psutil.Process(int(marker.read_text()))
                self.assertIn(process.status(), (psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD))
            except psutil.NoSuchProcess:
                pass

    @unittest.skipIf(os.name == "nt", "Windows has a commit hard limit; Linux test exercises RSS watchdog")
    def test_rss_budget_terminates_large_allocation(self):
        result = self.run_python("import time; data=bytearray(160*1024*1024); time.sleep(10)",
                                 memory_limit=96*1024*1024)
        self.assertEqual(result["status"], "memory_limit")
        self.assertGreater(result["peak_rss_bytes"], result["memory_limit_bytes"])
        self.assertFalse(result["hard_limit_enforced"])

    def test_rejects_shell_text_before_execution(self):
        with self.assertRaises(ValueError):
            run_limited("python -c unsafe")

    @unittest.skipUnless(os.name == "nt", "Requires a real Windows Job Object")
    def test_windows_job_rejects_over_budget_allocation(self):
        result = self.run_python(
            "try:\n data=bytearray(256*1024*1024)\nexcept MemoryError:\n print('allocation-denied')",
            memory_limit=128*1024*1024)
        self.assertTrue(result["hard_limit_enforced"])
        self.assertEqual(result["hard_limit_kind"], "windows_job_commit")
        self.assertIn("allocation-denied", result["stdout"])
        self.assertLessEqual(result["peak_commit_bytes"], result["memory_limit_bytes"])

    @unittest.skipUnless(os.name == "nt", "Exercises the Windows-specific fail-closed gate")
    def test_windows_failed_job_setup_does_not_start_command(self):
        with tempfile.TemporaryDirectory() as temp:
            marker = Path(temp) / "must-not-exist"
            with mock.patch("codex_learner.resource_limits._WindowsJob", side_effect=OSError("blocked")):
                result = self.run_python(f"from pathlib import Path; Path({str(marker)!r}).touch()")
            self.assertEqual(result["status"], "job_setup_failed")
            self.assertFalse(result["hard_limit_enforced"])
            self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()

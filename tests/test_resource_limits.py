import json
import os
import sys
import tempfile
import subprocess
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
        self.assertTrue(result["stdout"].endswith("tail-marker" + os.linesep))

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

    def test_broken_startup_gate_still_cleans_process_and_handles(self):
        from codex_learner import resource_limits as limits

        class BrokenGate:
            def __init__(self, stream):
                self.stream = stream

            @property
            def closed(self):
                return self.stream.closed

            def write(self, data):
                raise BrokenPipeError("injected startup write failure")

            def flush(self):
                raise BrokenPipeError("injected startup flush failure")

            def close(self):
                self.stream.close()
                raise BrokenPipeError("injected startup close failure")

        captured = []
        windows = os.name == "nt"
        target = "_SuspendedWindowsProcess" if windows else "subprocess.Popen"
        original = limits._SuspendedWindowsProcess if windows else limits.subprocess.Popen

        def factory(*args, **kwargs):
            proc = original(*args, **kwargs)
            proc.stdin = BrokenGate(proc.stdin)
            captured.append(proc)
            return proc

        # Keep the class identity used by the Windows cleanup isinstance check.
        if windows:
            original_init = original.__init__

            def initialize(instance, *args, **kwargs):
                original_init(instance, *args, **kwargs)
                instance.stdin = BrokenGate(instance.stdin)
                captured.append(instance)

            patcher = mock.patch.object(original, "__init__", initialize)
        else:
            patcher = mock.patch("codex_learner.resource_limits." + target, side_effect=factory)
        with tempfile.TemporaryDirectory() as temp:
            marker = Path(temp) / "must-not-exist"
            with patcher:
                result = self.run_python(f"from pathlib import Path; Path({str(marker)!r}).touch()")
            self.assertEqual(result["status"], "monitoring_failed", result)
            self.assertTrue(result["cleanup_verified"], result)
            self.assertFalse(marker.exists())
            self.assertEqual(len(captured), 1)
            self.assertTrue(captured[0].stdin.closed)
            self.assertIsNotNone(captured[0].returncode)
            if windows:
                self.assertIsNone(captured[0]._process_handle)
                self.assertIsNone(captured[0]._thread_handle)

    @unittest.skipUnless(os.name == "nt", "Requires a real Windows Job Object")
    def test_windows_job_rejects_over_budget_allocation(self):
        # Use VirtualAlloc directly so a rejected native commitment is visible
        # independently of Python's allocator and the supervisor's RSS check.
        result = self.run_python(
            """import ctypes, json, time
from ctypes import wintypes
import psutil
kernel = ctypes.WinDLL('kernel32', use_last_error=True)
kernel.VirtualAlloc.argtypes = [ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD, wintypes.DWORD]
kernel.VirtualAlloc.restype = ctypes.c_void_p
kernel.VirtualFree.argtypes = [ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD]
kernel.VirtualFree.restype = wintypes.BOOL
process = psutil.Process()
report = {'baseline_private_bytes': process.memory_info().private}
small = kernel.VirtualAlloc(None, 16*1024*1024, 0x3000, 0x04)
report['small_allocation_succeeded'] = bool(small)
try:
    report['before_rejection_private_bytes'] = process.memory_info().private
    time.sleep(0.3)
    ctypes.set_last_error(0)
    large = kernel.VirtualAlloc(None, 256*1024*1024, 0x3000, 0x04)
    report['allocation_denied'] = not large
    report['allocation_error'] = ctypes.get_last_error()
    report['after_rejection_private_bytes'] = process.memory_info().private
    if large:
        kernel.VirtualFree(large, 0, 0x8000)
    time.sleep(0.3)
finally:
    if small:
        kernel.VirtualFree(small, 0, 0x8000)
print(json.dumps(report))
""",
            memory_limit=128*1024*1024)
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(result["returncode"], 0, result)
        self.assertTrue(result["hard_limit_enforced"])
        self.assertEqual(result["hard_limit_kind"], "windows_job_commit")
        self.assertTrue(result["job_limit_verified"])
        self.assertTrue(result["job_membership_verified"])
        self.assertTrue(result["launcher_created_suspended"])
        self.assertTrue(result["launcher_resumed_after_job_verification"])
        self.assertEqual(result["job_memory_limit_bytes"], 128*1024*1024)
        self.assertEqual(result["job_limit_flags"] & 0x2200, 0x2200)
        self.assertIsNone(result["job_query_error"])
        report = json.loads(result["stdout"])
        self.assertTrue(report["small_allocation_succeeded"], report)
        self.assertTrue(report["allocation_denied"], report)
        self.assertNotEqual(report["allocation_error"], 0)
        self.assertGreaterEqual(report["before_rejection_private_bytes"]
                                - report["baseline_private_bytes"], 16*1024*1024)
        self.assertLessEqual(report["after_rejection_private_bytes"],
                             report["before_rejection_private_bytes"] + 2*1024*1024)
        self.assertGreater(result["private_commit_sample_count"], 1)
        self.assertGreater(result["peak_sampled_private_commit_bytes"], 16*1024*1024)
        self.assertLessEqual(result["peak_sampled_private_commit_bytes"], 128*1024*1024)
        self.assertLessEqual(result["initial_peak_job_memory_used_bytes"], 128*1024*1024)
        self.assertGreater(result["peak_job_memory_used_bytes"], 0)
        self.assertEqual(result["peak_commit_bytes"], result["peak_job_memory_used_bytes"])
        self.assertTrue(result["cleanup_verified"])
        # Preserve both counters in real Windows CI output. The historical job
        # peak is evidence, not a substitute for current successful commitment.
        report.update({key: result[key] for key in (
            "job_memory_limit_bytes", "job_limit_flags", "job_limit_verified",
            "initial_peak_job_memory_used_bytes", "peak_job_memory_used_bytes",
            "peak_sampled_private_commit_bytes",
            "last_sampled_private_commit_bytes", "private_commit_sample_count")})
        print("Windows Job Object evidence: " + json.dumps(report, sort_keys=True))

    @unittest.skipUnless(os.name == "nt", "Requires Windows executable venv redirectors")
    def test_windows_venv_redirector_cannot_escape_delayed_job_assignment(self):
        # A venv python.exe can spawn the real interpreter before reaching a
        # Python-level GO gate. Run the supervisor itself in a fresh venv and
        # delay assignment long enough for the old implementation to escape.
        import venv
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime = root / "venv"
            venv.EnvBuilder(with_pip=False).create(runtime)
            python = runtime / "Scripts/python.exe"
            controller = root / "supervisor.py"
            worker = root / "allocation.py"
            worker.write_text('''import ctypes, json, os, sys, time
from ctypes import wintypes
kernel = ctypes.WinDLL("kernel32", use_last_error=True)
kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel.OpenProcess.restype = wintypes.HANDLE
kernel.GetCurrentProcess.restype = wintypes.HANDLE
kernel.DuplicateHandle.argtypes = [wintypes.HANDLE, wintypes.HANDLE, wintypes.HANDLE,
                                  ctypes.POINTER(wintypes.HANDLE), wintypes.DWORD,
                                  wintypes.BOOL, wintypes.DWORD]
kernel.DuplicateHandle.restype = wintypes.BOOL
kernel.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
kernel.IsProcessInJob.restype = wintypes.BOOL
kernel.CloseHandle.argtypes = [wintypes.HANDLE]
kernel.CloseHandle.restype = wintypes.BOOL
kernel.VirtualAlloc.argtypes = [ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD, wintypes.DWORD]
kernel.VirtualAlloc.restype = ctypes.c_void_p
kernel.VirtualFree.argtypes = [ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD]
kernel.VirtualFree.restype = wintypes.BOOL
with open(sys.argv[1], encoding="utf-8") as stream:
    identity = json.load(stream)
parent = kernel.OpenProcess(0x40, False, identity["supervisor_pid"])
if not parent:
    raise ctypes.WinError(ctypes.get_last_error())
job = wintypes.HANDLE()
try:
    if not kernel.DuplicateHandle(parent, identity["job_handle"], kernel.GetCurrentProcess(),
                                  ctypes.byref(job), 0, False, 2):
        raise ctypes.WinError(ctypes.get_last_error())
finally:
    kernel.CloseHandle(parent)
try:
    member = wintypes.BOOL()
    if not kernel.IsProcessInJob(kernel.GetCurrentProcess(), job, ctypes.byref(member)):
        raise ctypes.WinError(ctypes.get_last_error())
finally:
    kernel.CloseHandle(job)
small = kernel.VirtualAlloc(None, 16*1024*1024, 0x3000, 0x04)
large = None
try:
    time.sleep(0.2)
    ctypes.set_last_error(0)
    large = kernel.VirtualAlloc(None, 192*1024*1024, 0x3000, 0x04)
    report = {"worker_pid": os.getpid(), "target_job_member": bool(member.value),
              "small_allocation_succeeded": bool(small),
              "large_allocation_denied": not large, "allocation_error": ctypes.get_last_error()}
finally:
    if large:
        kernel.VirtualFree(large, 0, 0x8000)
    if small:
        kernel.VirtualFree(small, 0, 0x8000)
print(json.dumps(report), flush=True)
''', encoding="utf-8")
            controller.write_text('''import ctypes, gc, json, os, sys, time
from pathlib import Path
import psutil
from codex_learner import resource_limits as limits
delay = float(sys.argv[1])
identity = Path(sys.argv[2])
worker = sys.argv[3]
observed = {}
original = limits._WindowsJob.attach
def delayed_attach(self, pid, process_handle=None):
    time.sleep(delay)
    observed["descendants_before_assignment"] = [p.pid for p in psutil.Process(pid).children(recursive=True)]
    original(self, pid, process_handle=process_handle)
    identity.write_text(json.dumps({"supervisor_pid": os.getpid(), "job_handle": int(self.handle)}), encoding="utf-8")
limits._WindowsJob.attach = delayed_attach
handles_before = psutil.Process().num_handles()
try:
    result = limits.run_limited([sys.executable, worker, str(identity)], memory_limit=128*1024*1024, timeout=10)
finally:
    limits._WindowsJob.attach = original
gc.collect()
observed.update({"delay_seconds": delay, "run": result,
                 "handles_before": handles_before, "handles_after": psutil.Process().num_handles()})
print(json.dumps(observed))
''', encoding="utf-8")
            environment = dict(os.environ)
            # This fresh venv intentionally has no pip installations. Reuse the
            # current matching-ABI psutil solely for the supervisor's sampler.
            environment["PYTHONPATH"] = os.pathsep.join((
                str(Path(__file__).resolve().parents[1] / "skills/codex-learner/scripts"),
                str(Path(psutil.__file__).resolve().parent.parent)))
            for delay in (0, 0.1, 0.3):
                with self.subTest(delay=delay):
                    completed = subprocess.run(
                        [str(python), str(controller), str(delay), str(root / "job.json"), str(worker)],
                        env=environment, capture_output=True, text=True, encoding="utf-8", timeout=30)
                    self.assertEqual(completed.returncode, 0, completed.stderr)
                    evidence = json.loads(completed.stdout)
                    result = evidence["run"]
                    self.assertEqual(result["status"], "completed", evidence)
                    self.assertEqual(result["returncode"], 0, evidence)
                    self.assertEqual(evidence["descendants_before_assignment"], [], evidence)
                    self.assertTrue(result["job_membership_verified"], evidence)
                    self.assertTrue(result["launcher_created_suspended"], evidence)
                    self.assertTrue(result["launcher_resumed_after_job_verification"], evidence)
                    self.assertTrue(result["hard_limit_enforced"], evidence)
                    self.assertEqual(result["job_memory_limit_bytes"], 128*1024*1024, evidence)
                    allocation = json.loads(result["stdout"])
                    self.assertTrue(allocation["target_job_member"], evidence)
                    self.assertTrue(allocation["small_allocation_succeeded"], evidence)
                    self.assertTrue(allocation["large_allocation_denied"], evidence)
                    self.assertNotEqual(allocation["allocation_error"], 0, evidence)
                    self.assertTrue(result["cleanup_verified"], evidence)
                    self.assertEqual(result["surviving_process_ids"], [], evidence)
                    self.assertLessEqual(evidence["handles_after"], evidence["handles_before"], evidence)
                    print("Windows suspended venv evidence: " + json.dumps(evidence, sort_keys=True))

    @unittest.skipUnless(os.name == "nt", "Exercises the Windows-specific fail-closed gate")
    def test_windows_failed_job_setup_does_not_start_command(self):
        with tempfile.TemporaryDirectory() as temp:
            marker = Path(temp) / "must-not-exist"
            with mock.patch("codex_learner.resource_limits._WindowsJob", side_effect=OSError("blocked")):
                result = self.run_python(f"from pathlib import Path; Path({str(marker)!r}).touch()")
            self.assertEqual(result["status"], "job_setup_failed")
            self.assertFalse(result["hard_limit_enforced"])
            self.assertFalse(marker.exists())

    @unittest.skipUnless(os.name == "nt", "Exercises the Windows-specific readback gate")
    def test_windows_failed_limit_readback_does_not_start_command(self):
        with tempfile.TemporaryDirectory() as temp:
            marker = Path(temp) / "must-not-exist"
            with mock.patch("codex_learner.resource_limits._WindowsJob.verify_limits",
                            side_effect=OSError("readback mismatch")):
                result = self.run_python(f"from pathlib import Path; Path({str(marker)!r}).touch()")
            self.assertEqual(result["status"], "job_setup_failed")
            self.assertFalse(result["hard_limit_enforced"])
            self.assertFalse(result["job_limit_verified"])
            self.assertFalse(marker.exists())
            self.assertTrue(result["cleanup_verified"])

    @unittest.skipUnless(os.name == "nt", "Exercises Windows startup failure and handle cleanup")
    def test_windows_failed_membership_and_resume_close_all_owned_handles(self):
        from codex_learner import resource_limits as limits
        with tempfile.TemporaryDirectory() as temp:
            marker = Path(temp) / "must-not-exist"
            for method in ("_WindowsJob.verify_membership", "_SuspendedWindowsProcess.resume"):
                with self.subTest(method=method):
                    processes = []
                    jobs = []
                    process_init = limits._SuspendedWindowsProcess.__init__
                    job_init = limits._WindowsJob.__init__

                    def capture_process(instance, *args, **kwargs):
                        process_init(instance, *args, **kwargs)
                        processes.append(instance)

                    def capture_job(instance, *args, **kwargs):
                        job_init(instance, *args, **kwargs)
                        jobs.append(instance)

                    with mock.patch.object(limits._SuspendedWindowsProcess, "__init__", capture_process), \
                         mock.patch.object(limits._WindowsJob, "__init__", capture_job), \
                         mock.patch("codex_learner.resource_limits." + method, side_effect=OSError("injected")):
                        result = self.run_python(f"from pathlib import Path; Path({str(marker)!r}).touch()")
                    self.assertEqual(result["status"], "job_setup_failed", result)
                    self.assertFalse(result["hard_limit_enforced"], result)
                    self.assertFalse(result["launcher_resumed_after_job_verification"], result)
                    self.assertTrue(result["cleanup_verified"], result)
                    self.assertFalse(marker.exists())
                    self.assertEqual(len(processes), 1)
                    self.assertTrue(processes[0].stdin.closed)
                    self.assertIsNone(processes[0]._process_handle)
                    self.assertIsNone(processes[0]._thread_handle)
                    self.assertEqual(len(jobs), 1)
                    self.assertIsNone(jobs[0].handle)


if __name__ == "__main__":
    unittest.main()

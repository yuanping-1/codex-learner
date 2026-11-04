"""Run optional parsers outside the learner with bounded resources and logs.

Windows enforces the cumulative Job Object *committed-memory* limit before the
actual parser starts.  The portable RSS watchdog is sampled protection, not a
hard limit: a native allocation can briefly exceed it between observations.
No claim about Windows execution is made when this module runs on another OS.
"""

import json
import os
import signal
import subprocess
import sys
import tempfile
import time


DEFAULT_MEMORY_LIMIT = 2 * 1024 ** 3
POLL_INTERVAL = 0.1
RETURN_LOG_BYTES = 64 * 1024
MAX_LOG_BYTES = 8 * 1024 ** 2

# The actual parser cannot load heavyweight dependencies until its launcher has
# been assigned to the job. Child processes inherit that assignment on Windows.
# stdin is reserved for the gate; parser protocols must use files or arguments.
_LAUNCHER = """import json, subprocess, sys
if sys.stdin.buffer.readline() != b'GO\\n':
    raise SystemExit(125)
try:
    code = subprocess.call(json.loads(sys.argv[1]), stdin=subprocess.DEVNULL)
except OSError as exc:
    print(str(exc), file=sys.stderr)
    raise SystemExit(126)
raise SystemExit(code if code >= 0 else 128 - code)
"""


class _WindowsJob:
    """A non-breakaway, kill-on-close job for the launcher and its descendants."""

    def __init__(self, memory_limit):
        import ctypes
        from ctypes import wintypes

        class BasicLimit(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class ExtendedLimit(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimit), ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        self.ctypes = ctypes
        self.limit_type = ExtendedLimit
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.kernel.CreateJobObjectW.restype = wintypes.HANDLE
        self.kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                       ctypes.c_void_p, wintypes.DWORD]
        self.kernel.SetInformationJobObject.restype = wintypes.BOOL
        self.kernel.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                         ctypes.c_void_p, wintypes.DWORD,
                                                         ctypes.c_void_p]
        self.kernel.QueryInformationJobObject.restype = wintypes.BOOL
        self.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel.OpenProcess.restype = wintypes.HANDLE
        self.kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        self.kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self.kernel.TerminateJobObject.restype = wintypes.BOOL
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.CloseHandle.restype = wintypes.BOOL
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimit()
        # JOB_OBJECT_LIMIT_JOB_MEMORY | JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE.
        limits.BasicLimitInformation.LimitFlags = 0x200 | 0x2000
        limits.JobMemoryLimit = memory_limit
        if not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits),
                                                   ctypes.sizeof(limits)):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def attach(self, pid):
        # Assignment requires PROCESS_SET_QUOTA and PROCESS_TERMINATE.
        process = self.kernel.OpenProcess(0x100 | 0x1, False, pid)
        if not process:
            raise self.ctypes.WinError(self.ctypes.get_last_error())
        try:
            if not self.kernel.AssignProcessToJobObject(self.handle, process):
                raise self.ctypes.WinError(self.ctypes.get_last_error())
        finally:
            self.kernel.CloseHandle(process)

    def read_limits(self):
        """Read the actual kernel settings and the unmodified historical peak."""
        limits = self.limit_type()
        if not self.kernel.QueryInformationJobObject(self.handle, 9, self.ctypes.byref(limits),
                                                     self.ctypes.sizeof(limits), None):
            raise self.ctypes.WinError(self.ctypes.get_last_error())
        return {"limit_flags": int(limits.BasicLimitInformation.LimitFlags),
                "memory_limit_bytes": int(limits.JobMemoryLimit),
                "peak_job_memory_used_bytes": int(limits.PeakJobMemoryUsed)}

    def verify_limits(self, memory_limit):
        limits = self.read_limits()
        required = 0x200 | 0x2000
        if (limits["limit_flags"] & required != required
                or limits["memory_limit_bytes"] != memory_limit):
            raise OSError("Windows Job Object memory/kill-on-close limits failed readback verification")
        return limits

    def terminate(self):
        if self.handle:
            self.kernel.TerminateJobObject(self.handle, 137)

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def _tail(stream, limit=RETURN_LOG_BYTES):
    stream.flush()
    size = os.fstat(stream.fileno()).st_size
    stream.seek(max(0, size - limit))
    return stream.read(limit).decode("utf-8", errors="replace"), size > limit


def _sample_tree(process, psutil, known):
    """Sum RSS and Windows PrivateUsage, remembering identities for cleanup.

    PrivateUsage is currently committed private memory, not the working set or
    a historical peak. This is a sampled process-tree sum, not a job counter.
    https://learn.microsoft.com/en-us/windows/win32/api/psapi/ns-psapi-process_memory_counters_ex
    """
    try:
        processes = [process] + process.children(recursive=True)
    except psutil.NoSuchProcess:
        processes = []
    total = 0
    private_commit = 0 if os.name == "nt" else None
    for item in processes:
        try:
            known[item.pid] = (item, item.create_time())
            memory = item.memory_info()
            total += memory.rss
            if private_commit is not None:
                private_commit += memory.private
        except psutil.NoSuchProcess:
            continue
    return total, private_commit


def _stop_tree(proc, job, known, psutil):
    if job is not None:
        job.terminate()
    elif os.name != "nt":
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    # psutil Process instances remember identity and check PID reuse in kill().
    # This also handles observed children that made their own process group.
    for item, _created in reversed(list(known.values())):
        try:
            item.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    if proc.poll() is None:
        proc.kill()
    proc.wait(timeout=5)


def _surviving_process_ids(known, psutil):
    """Check captured identities, not just PIDs that the OS could have reused."""
    surviving = []
    for pid, (_original, created) in known.items():
        try:
            current = psutil.Process(pid)
            if current.create_time() != created:
                continue
            if current.status() not in (psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD):
                surviving.append(pid)
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied:
            # An inaccessible identity cannot be reported as verified cleanup.
            surviving.append(pid)
    return sorted(surviving)


def _confirm_cleanup(known, psutil):
    deadline = time.monotonic() + 2
    while True:
        surviving = _surviving_process_ids(known, psutil)
        if not surviving or time.monotonic() >= deadline:
            return surviving
        time.sleep(0.02)


def run_limited(command, *, timeout=600, memory_limit=DEFAULT_MEMORY_LIMIT,
                cwd=None, env=None):
    """Return process outcome, bounded log tails and resource evidence.

    ``command`` is an argument list, never shell text. The parser receives no
    stdin; use request files/argv. ``status`` is completed, timeout, memory_limit,
    output_limit, monitoring_failed or job_setup_failed. A completed process may
    have a nonzero returncode. ``hard_limit_enforced`` is true only after an
    actual Windows job assignment and kernel limit readback succeed; its limit counts committed memory,
    while ``peak_rss_bytes`` is the sampled sum of process-tree resident memory.
    ``peak_commit_bytes`` is retained as a compatibility alias for the raw
    ``peak_job_memory_used_bytes`` historical Windows counter. It must not be
    used as a limit-compliance assertion: real Windows runs have reported a
    peak above the limit even while rejecting the oversized allocation. The
    separate ``peak_sampled_private_commit_bytes`` sums current PrivateUsage
    values from observed processes and may miss activity between samples.
    Cleanup evidence covers the launcher and all descendants observed by the
    sampler; it compares both PID and creation time and ignores dead/zombie
    processes. It is not a claim to detect arbitrary unobserved daemon escapes.

    A missing psutil or invalid request fails before launching any process.
    Windows job setup failure fails closed: the supplied command never starts.
    """
    if (not isinstance(command, (list, tuple)) or not command
            or not all(isinstance(arg, str) and arg for arg in command)):
        raise ValueError("command must be a nonempty list of nonempty arguments")
    if timeout <= 0 or type(memory_limit) is not int or memory_limit <= 0:
        raise ValueError("timeout and memory_limit must be positive")
    try:
        import psutil
    except ImportError as exc:
        raise RuntimeError("OCR resource monitoring requires psutil; install requirements-ocr.txt.") from exc

    started = time.monotonic()
    status, detail, peak, peak_commit = "completed", None, 0, None
    hard, job, known = False, None, {}
    job_limits, job_query_error = None, None
    current_private_commit, peak_private_commit, private_samples = None, None, 0
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(
            [sys.executable, "-c", _LAUNCHER, json.dumps(list(command))],
            stdin=subprocess.PIPE, stdout=out, stderr=err, cwd=cwd, env=env,
            start_new_session=os.name != "nt")
        process = psutil.Process(proc.pid)
        known[process.pid] = (process, process.create_time())
        try:
            if os.name == "nt":
                try:
                    job = _WindowsJob(memory_limit)
                    job.attach(proc.pid)
                    job_limits = job.verify_limits(memory_limit)
                    hard = True
                except OSError as exc:
                    status = "job_setup_failed"
                    detail = f"Windows Job Object could not be attached; parser not started: {exc}"
            if status == "completed":
                proc.stdin.write(b"GO\n")
                proc.stdin.flush()
            proc.stdin.close()
            while status == "completed" and proc.poll() is None:
                try:
                    current, private_commit = _sample_tree(process, psutil, known)
                except psutil.AccessDenied as exc:
                    status, detail = "monitoring_failed", str(exc)
                    break
                peak = max(peak, current)
                if private_commit is not None:
                    current_private_commit = private_commit
                    peak_private_commit = max(peak_private_commit or 0, private_commit)
                    private_samples += 1
                if current > memory_limit:
                    status = "memory_limit"
                    detail = "Sampled process-tree RSS exceeded the configured memory limit."
                    break
                if time.monotonic() - started >= timeout:
                    status, detail = "timeout", "Parser exceeded the configured timeout."
                    break
                if max(os.fstat(out.fileno()).st_size, os.fstat(err.fileno()).st_size) > MAX_LOG_BYTES:
                    status, detail = "output_limit", "Parser exceeded the log-file budget."
                    break
                time.sleep(POLL_INTERVAL)
        finally:
            if proc.stdin and not proc.stdin.closed:
                proc.stdin.close()
            if job is not None:
                try:
                    peak_commit = job.read_limits()["peak_job_memory_used_bytes"]
                except OSError as exc:
                    job_query_error = str(exc)
            # Clean up descendants even after a successful command exits.
            try:
                _stop_tree(proc, job, known, psutil)
            finally:
                if job is not None:
                    job.close()
        surviving = _confirm_cleanup(known, psutil)
        stdout, out_cut = _tail(out)
        stderr, err_cut = _tail(err)
        if status == "completed" and max(os.fstat(out.fileno()).st_size,
                                          os.fstat(err.fileno()).st_size) > MAX_LOG_BYTES:
            status, detail = "output_limit", "Parser exceeded the log-file budget."
        return {"status": status, "returncode": proc.returncode,
                "stdout": stdout, "stderr": stderr, "detail": detail,
                "peak_rss_bytes": peak, "peak_commit_bytes": peak_commit,
                "peak_job_memory_used_bytes": peak_commit,
                "initial_peak_job_memory_used_bytes": (
                    job_limits["peak_job_memory_used_bytes"] if job_limits else None),
                "job_memory_limit_bytes": job_limits["memory_limit_bytes"] if job_limits else None,
                "job_limit_flags": job_limits["limit_flags"] if job_limits else None,
                "job_limit_verified": job_limits is not None,
                "job_query_error": job_query_error,
                "last_sampled_private_commit_bytes": current_private_commit,
                "peak_sampled_private_commit_bytes": peak_private_commit,
                "private_commit_sample_count": private_samples,
                "hard_limit_enforced": hard,
                "hard_limit_kind": "windows_job_commit" if hard else None,
                "memory_limit_bytes": memory_limit,
                "output_truncated": out_cut or err_cut,
                "observed_process_ids": sorted(known),
                "cleanup_verified": not surviving,
                "surviving_process_ids": surviving,
                "elapsed_seconds": time.monotonic() - started}

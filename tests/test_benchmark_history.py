"""Repeatable transaction benchmarks preserve scope, identity and failure evidence."""

import contextlib
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("history_benchmark_test_subject", ROOT / "scripts/benchmark_history.py")
benchmark = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(benchmark)


class HistoryBenchmarkTests(unittest.TestCase):
    def test_nearest_rank_excludes_warmup_profile_and_failed_timings(self):
        def sample(value, **fields):
            return {"variant": "candidate", "operation": "submit", "warmup": False,
                    "profiled": False, "ok": True,
                    "result": {"elapsed_seconds": value, "peak_bytes": value * 100}, **fields}
        rows = [sample(i) for i in range(1, 21)]
        rows += [sample(1000, warmup=True), sample(1000, profiled=True), sample(1000, ok=False)]
        result = benchmark.summarize(rows, ["candidate"], ["submit"])[0]
        self.assertEqual(result["samples"], 21)
        self.assertEqual(result["successful_samples"], 20)
        self.assertEqual(result["failures"], 1)
        self.assertEqual(result["median_seconds"], 10.5)
        self.assertEqual(result["p95_seconds"], 19)
        self.assertEqual(result["peak_bytes"], 2000)

    def test_worker_failure_invalid_json_and_timeout_preserve_output(self):
        responses = [
            subprocess.CompletedProcess(["worker"], 3, '{"reason":"failed"}', "failure detail"),
            subprocess.CompletedProcess(["worker"], 0, "not json", "diagnostic"),
            subprocess.TimeoutExpired(["worker"], 1, output=b"partial stdout", stderr=b"partial stderr"),
        ]
        with patch.object(benchmark.subprocess, "run", side_effect=responses):
            results = [benchmark.run_subprocess(["worker"], timeout=1) for _ in responses]
        self.assertTrue(all(not result["ok"] for result in results))
        self.assertEqual(results[0]["returncode"], 3)
        self.assertEqual(results[0]["stderr"], "failure detail")
        self.assertEqual(results[1]["stdout"], "not json")
        self.assertIn("Invalid worker JSON", results[1]["error"])
        self.assertIsNone(results[2]["returncode"])
        self.assertEqual(results[2]["stdout"], "partial stdout")
        self.assertEqual(results[2]["stderr"], "partial stderr")

    def test_repeated_isolated_comparison_and_profile_keep_one_fixture(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "report.json"
            run = subprocess.run([
                sys.executable, str(ROOT / "scripts/benchmark_history.py"), "--records", "20",
                "--repeat", "2", "--warmup", "1", "--operations", "submit,retry", "--profile",
                "--baseline-root", str(ROOT), "--output", str(output),
            ], text=True, encoding="utf-8", capture_output=True, timeout=120)
            self.assertEqual(run.returncode, 0, run.stderr + run.stdout[-3000:])
            report = json.loads(output.read_text(encoding="utf-8"))
        self.assertTrue(report["complete"])
        self.assertTrue(report["passed"])
        self.assertEqual(len(report["samples"]), 16)
        self.assertEqual(len(report["summaries"]), 4)
        self.assertTrue(all(row["samples"] == 2 and row["successful_samples"] == 2
                            for row in report["summaries"]))
        start = report["fixture"]["fixture_manifest_sha256"]
        self.assertEqual({row["starting_manifest_sha256"] for row in report["samples"]}, {start})
        expected_modules = report["sources"]["candidate"]["module_sha256"]
        for sample in report["samples"]:
            result = sample["result"]
            self.assertTrue(result["correctness"]["passed"])
            self.assertEqual(result["correctness"]["before_submissions"], 20)
            self.assertEqual(result["correctness"]["after_submissions"],
                             20 + int(sample["operation"] == "submit"))
            self.assertEqual(result["correctness"]["previous_sha256_after"], start)
            if sample["operation"] == "retry":
                self.assertTrue(result["duplicate"])
                self.assertEqual(result["correctness"]["manifest_sha256_after"], start)
            for value in result["actual_product_modules"].values():
                self.assertEqual(value["sha256"], expected_modules[Path(value["path"]).name])
            if sample["profiled"]:
                self.assertGreater(result["profile"]["application_file_totals"]["opens"], 0)
                self.assertGreater(result["profile"]["sha256"]["input_bytes"], 0)
                if sample["operation"] == "submit":
                    self.assertGreater(result["profile"]["phase_io_inclusive"]["history_integrity"]["sha256_input_bytes"], 0)
            else:
                self.assertIsNone(result["profile"])
        self.assertIn("not cold-disk", report["cache_state"])

    def test_unpacked_build_identity_wins_and_parent_repository_is_not_a_source_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "unpacked"
            modules = root / "skills/codex-learner/scripts/codex_learner"
            modules.mkdir(parents=True)
            (modules / "__init__.py").write_text('__version__ = "0.2.0"', encoding="utf-8")
            (root / "BUILD.json").write_text(json.dumps({"commit": "b" * 40}), encoding="utf-8")
            with patch.object(benchmark.subprocess, "run", side_effect=AssertionError("BUILD identity must not consult enclosing git")):
                identity = benchmark.source_identity(root)
            self.assertEqual(identity["source_commit"], "b" * 40)
            self.assertEqual(identity["identity_source"], "BUILD.json")
            (root / "BUILD.json").unlink()
            outside = subprocess.CompletedProcess([], 0, str(root.parent), "")
            with patch.object(benchmark.subprocess, "run", return_value=outside) as git:
                identity = benchmark.source_identity(root)
            self.assertIsNone(identity["source_commit"])
            self.assertEqual(identity["identity_source"], "unversioned")
            self.assertEqual(git.call_count, 1)
            self.assertIn("--show-toplevel", git.call_args.args[0])

    def test_later_worker_failure_keeps_earlier_sample_and_writes_failed_report(self):
        original = benchmark.run_subprocess

        def run(arguments, **kwargs):
            if "--_worker" in arguments and arguments[arguments.index("--_worker") + 1] == "retry":
                return {"ok": False, "returncode": 7, "stdout": "partial result", "stderr": "injected failure"}
            return original(arguments, **kwargs)

        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "failed.json"
            argv = ["benchmark_history.py", "--records", "20", "--operations", "submit,retry", "--output", str(output)]
            with patch.object(sys, "argv", argv), patch.object(benchmark, "run_subprocess", side_effect=run), \
                    contextlib.redirect_stdout(io.StringIO()):
                code = benchmark.main()
            result = json.loads(output.read_text(encoding="utf-8"))
            kept = Path(result["samples"][1]["failed_sample_path"])
            self.assertTrue((kept / "sample-course/course.json").is_file())
            self.assertTrue((kept / "benchmark-failure.json").is_file())
            self.assertTrue((kept / "benchmark-fixture-info.json").is_file())
        self.assertEqual(code, 1)
        self.assertTrue(result["complete"])
        self.assertFalse(result["passed"])
        self.assertEqual(len(result["samples"]), 2)
        self.assertTrue(result["samples"][0]["ok"])
        self.assertEqual(result["samples"][1]["stdout"], "partial result")
        self.assertEqual(result["samples"][1]["stderr"], "injected failure")
        self.assertEqual(result["summaries"][1]["failures"], 1)
        self.assertIsNone(result["summaries"][1]["p95_seconds"])


if __name__ == "__main__":
    unittest.main()

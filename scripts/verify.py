#!/usr/bin/env python3
"""Run meaningful suites and preserve their exit statuses and current logs."""

import argparse
import json
import platform
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--browser", action="store_true")
    args = parser.parse_args()
    results = []
    for suite in (["tests", "tests/browser"] if args.browser else ["tests"]):
        command = [sys.executable, "-m", "unittest", "discover", "-s", suite, "-v"]
        run = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, encoding="utf-8")
        log = run.stdout + run.stderr
        count = re.search(r"Ran (\d+) tests? in", log)
        total = int(count[1]) if count else 0
        results.append({"command": command, "exit_code": run.returncode, "tests_run": total,
                        "passed": run.returncode == 0 and total > 0, "log": log})
        print(f"{suite}: exit={run.returncode}, tests={total}")
        if run.returncode:
            print(log)
    report = {"checked_at": datetime.now(timezone.utc).isoformat(), "platform": platform.platform(),
              "python": sys.version, "suites": results,
              "native_host_verified": False, "windows_verified": platform.system() == "Windows",
              "new_codex_conversation_verified": False}
    output = ROOT / "artifacts/verification.json"
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if all(r["passed"] for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())

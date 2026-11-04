#!/usr/bin/env python3
"""Build a portable source/skill archive, excluding all local learning data."""

import argparse
import hashlib
import json
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INCLUDED = ["README.md", "requirements-pdf.txt", "requirements-dev.txt",
            "skills", "scripts", "examples", "tests", "docs", ".github",
            "artifacts/verification.json", "artifacts/ui-verification.json"]


def build(output):
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    members = {}
    for name in INCLUDED:
        path = ROOT / name
        for item in ([path] if path.is_file() else path.rglob("*")):
            if (not item.is_file() or "__pycache__" in item.parts or item.suffix == ".pyc"
                    or item.name in ("config.json", "install-manifest.json")):
                continue
            if item.is_symlink():
                raise ValueError(f"Refusing to package a symlink: {item}")
            members[item.relative_to(ROOT).as_posix()] = item.read_bytes()
    hashes = {name: hashlib.sha256(data).hexdigest() for name, data in members.items()}
    members["MANIFEST.sha256.json"] = json.dumps(hashes, sort_keys=True, indent=2).encode("utf-8")
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(members):
            info = zipfile.ZipInfo("codex-learner/" + name, date_time=(2026, 10, 26, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, members[name])
    checksum = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix(output.suffix + ".sha256").write_text(checksum + "  " + output.name + "\n", encoding="utf-8")
    return {"path": str(output.resolve()), "sha256": checksum, "files": len(members)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/codex-learner-0.1.0.zip")
    args = parser.parse_args()
    print(json.dumps(build(args.output), indent=2))

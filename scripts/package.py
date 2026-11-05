#!/usr/bin/env python3
"""Build a deterministic source/skill archive using bounded streaming I/O."""

import argparse
import hashlib
import json
import os
import re
import subprocess
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Explicit source areas, never a recursive copy of the repository or artifacts.
INCLUDED = ["README.md", "requirements-pdf.txt", "requirements-base.txt", "requirements-ocr.txt",
            "requirements-docling.txt", "requirements-dev.txt", "requirements-base.lock",
            "requirements-ocr-py312.lock", "skills/codex-learner",
            "scripts", "examples", "tests", "docs", ".github/workflows"]
BLOCKED_PARTS = {"__pycache__", ".git", ".venv", "venv", "env", "models", "model", "cache",
                 ".cache", "learning-library", "library", "bodies", "originals", "outputs"}
BLOCKED_NAMES = {"config.json", "install-manifest.json", "course.json", "course.previous.json",
                 "library.json", "credentials.json", "token.json", ".env"}
CHUNK = 1024 * 1024


def file_hash(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for data in iter(lambda: stream.read(CHUNK), b""):
            result.update(data)
    return result.hexdigest()


def allowed(name):
    path = Path(name)
    # Historical validation is delivered as a complete, separately checksummed
    # evidence attachment. Never include just the basename-allowed JSON subset.
    if name.startswith("docs/evidence/v0.2/revised-release/"):
        return False
    # The external acceptance task pins the original ZIP hash. Keep it outside
    # subsequent packages instead of shipping instructions for a different ZIP.
    if name == "docs/local-acceptance-handoff-v0.2.md":
        return False
    if (set(path.parts) & BLOCKED_PARTS or path.name in BLOCKED_NAMES or
            re.search(r"(^|[-_.])(secret|credential|token|private[-_]?key)([-_.]|$)", path.name, re.I)):
        return False
    if name in INCLUDED:
        return path.suffix in {".md", ".txt", ".lock"}
    if name == "skills/codex-learner/SKILL.md":
        return True
    if name.startswith(("skills/codex-learner/scripts/", "scripts/", "tests/")):
        return path.suffix == ".py"
    if name.startswith("skills/codex-learner/assets/"):
        return path.suffix in {".html", ".js", ".css"}
    if name.startswith("docs/evidence/v0.2/"):
        return path.name in {"memory-benchmark.json", "history-benchmark.json", "migration-benchmark.json",
                             "ocr-linux-validation.json", "revised-ocr-linux-validation.json",
                             "dependency-lock-validation.json", "windows-job-validation.json", "test-summary.json"}
    if name.startswith(("skills/codex-learner/references/", "docs/")):
        return path.suffix == ".md"
    if name.startswith("examples/"):
        return path.suffix in {".md", ".json"}
    return name.startswith(".github/workflows/") and path.suffix in {".yaml", ".yml"}


def version():
    content = (ROOT / "skills/codex-learner/scripts/codex_learner/__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*[\'"]([^\'"]+)[\'"]', content, re.M)
    if not match:
        raise ValueError("Package version is missing from __init__.py")
    return match[1]


def metadata(hashes):
    try:
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True,
                                capture_output=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        commit = None
    source_hash = hashlib.sha256(json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"manifest_version": 1, "version": version(), "commit": commit,
            "protocol": "codex-learner-course", "read_schemas": [1, 2], "write_schema": 2,
            "source_manifest_sha256": source_hash,
            "validation": "See external acceptance evidence bound to this ZIP SHA-256; packaging is not acceptance."}


def info_for(name):
    info = zipfile.ZipInfo("codex-learner/" + name, date_time=(2026, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    return info


def build(output):
    output = Path(output).expanduser().absolute()
    members = {}
    for name in INCLUDED:
        path = ROOT / name
        for item in ([path] if path.is_file() else sorted(path.rglob("*"))):
            relative = item.relative_to(ROOT).as_posix()
            if item.is_symlink():
                raise ValueError(f"Refusing to package a symlink: {item}")
            if item.is_file() and allowed(relative):
                if item.resolve() == output.resolve():
                    raise ValueError("Package output cannot replace a source file")
                members[relative] = item
    hashes = {name: file_hash(path) for name, path in sorted(members.items())}
    build_info = metadata(hashes)
    generated = {"BUILD.json": json.dumps(build_info, sort_keys=True, indent=2).encode("utf-8")}
    hashes.update({name: hashlib.sha256(data).hexdigest() for name, data in generated.items()})
    generated["MANIFEST.sha256.json"] = json.dumps(hashes, sort_keys=True, indent=2).encode("utf-8")
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".package-", suffix=".zip", dir=output.parent)
    os.close(descriptor)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for name in sorted(set(members) | set(generated)):
                info = info_for(name)
                if name in generated:
                    archive.writestr(info, generated[name])
                    continue
                # Read and hash a second time while writing: concurrent source edits
                # fail the build instead of producing a misleading manifest.
                copied = hashlib.sha256()
                with members[name].open("rb") as source, archive.open(info, "w") as target:
                    for data in iter(lambda: source.read(CHUNK), b""):
                        copied.update(data)
                        target.write(data)
                if copied.hexdigest() != hashes[name]:
                    raise ValueError(f"Source changed during packaging: {name}; retry after editing stops")
        checksum = file_hash(temporary)
        os.replace(temporary, output)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    output.with_suffix(output.suffix + ".sha256").write_text(checksum + "  " + output.name + "\n", encoding="utf-8")
    return {"path": str(output.resolve()), "sha256": checksum, "files": len(members) + len(generated),
            "version": build_info["version"], "commit": build_info["commit"]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    output = args.output
    if output is None:
        commit = metadata({})["commit"]
        if not commit:
            parser.error("Outside Git, specify a new --output path explicitly.")
        output = ROOT / "artifacts" / f"codex-learner-{version()}-perf-{commit[:12]}.zip"
    print(json.dumps(build(output), indent=2))

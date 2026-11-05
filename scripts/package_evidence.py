#!/usr/bin/env python3
"""Package explicitly selected engineering evidence, without running acceptance.

Paths inside the ZIP retain the product archive's codex-learner/ repository
prefix, so both attachments can be extracted into the same parent directory.
"""

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
import zipfile
from pathlib import Path

from package import CHUNK, file_hash, info_for

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = "docs/evidence/v0.2/revised-release/"
SNAPSHOT_RECORDS = (
    "release-verification.json", "windows-job-validation.json", "package-verification.json",
    "recheck-task.md", "revised-final-core.log", "revised-browser.log", "revised-ocr.log",
    "windows310-core-final.log", "windows312-core-final.log", "windows-ocr-final.log",
    "revised-ocr-linux-validation.json",
)
REQUIRED = (
    "docs/project-engineering-report-2026-11-05.md",
    "docs/engineering-review-decisions-2026-11-05.md",
    "docs/implementation.md", "docs/delivery-perf-candidate.md",
    "docs/local-acceptance-handoff-v0.2.md",
    *(SNAPSHOT + name for name in ("README.md", "evidence-manifest.json", *SNAPSHOT_RECORDS)),
    *("docs/evidence/v0.2/" + name for name in (
        "memory-benchmark.json", "history-benchmark.json", "migration-benchmark.json",
        "ocr-linux-validation.json", "revised-ocr-linux-validation.json",
        "dependency-lock-validation.json", "windows-job-validation.json", "test-summary.json",
    )),
)
OPTIONAL = (
    "docs/native-repair-02c922d.md", "docs/native-repair-recheck-v0.2.md",
    *("docs/evidence/v0.2/repair-02c922d/" + name for name in (
        "README.md", "core-first.log", "core-command-observation.json", "validation.json",
    )),
    "docs/performance-v0.2.md", "docs/performance-recheck-v0.2.md",
    "docs/local-recheck-7946513-2026-11-05.md", "docs/recovery-diagnostic-recheck-v0.2.md",
    "docs/evidence/v0.2/performance-baseline.json",
    "docs/evidence/v0.2/performance-candidate.json",
    "docs/evidence/v0.2/performance-comparison.json",
    "docs/evidence/v0.2/performance-profile.json",
    "docs/evidence/v0.2/performance-memory.json",
)


def selected_files():
    """Never recursively collect a directory or follow any source symlink."""
    members = {}
    for name in REQUIRED + OPTIONAL:
        path = ROOT
        for part in Path(name).parts:
            path = path / part
            if path.is_symlink():
                raise ValueError(f"Evidence source cannot be a symbolic link: {name}")
        if not path.exists() and name in OPTIONAL:
            continue
        if not path.is_file():
            raise ValueError(f"Required evidence file is missing or not a file: {name}")
        members[name] = path
    return members


def snapshot_metadata(members, hashes):
    """Validate the historical snapshot before placing it in a new envelope."""
    path = members[SNAPSHOT + "evidence-manifest.json"]
    if path.stat().st_size > 64 * 1024:
        raise ValueError("Historical evidence manifest exceeds its bounded reader.")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    records = manifest.get("files", [])
    if (len(records) != len(SNAPSHOT_RECORDS)
            or {record.get("snapshot_path") for record in records} != set(SNAPSHOT_RECORDS)):
        raise ValueError("Historical evidence manifest must cover exactly the eleven original records.")
    for record in records:
        name = SNAPSHOT + record["snapshot_path"]
        if hashes[name] != record.get("sha256") or members[name].stat().st_size != record.get("bytes"):
            raise ValueError(f"Historical evidence no longer matches its frozen manifest: {name}")
    return manifest


def source_commit():
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True,
                              capture_output=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def build(output):
    output = Path(output).expanduser().absolute()
    members = selected_files()
    if output.resolve() in {path.resolve() for path in members.values()}:
        raise ValueError("Evidence ZIP cannot overwrite an input file.")
    hashes = {name: file_hash(path) for name, path in sorted(members.items())}
    historical = snapshot_metadata(members, hashes)
    metadata = {
        "schema_version": 1, "bundle_source_commit": source_commit(),
        "scope": "Evidence collection only; no acceptance results are created or upgraded by packaging.",
        "historical_product_commit": historical.get("source_commit"),
        "historical_package_sha256": historical.get("package_sha256"),
        "historical_user_native_acceptance": historical.get("revised_user_native_acceptance"),
        "historical_full_delivery": historical.get("full_delivery"),
        "input_files": len(members),
        "optional_files_included": [name for name in OPTIONAL if name in members],
        "optional_files_absent": [name for name in OPTIONAL if name not in members],
        "product_zip_included": False,
    }
    generated = {"EVIDENCE_BUILD.json": (json.dumps(metadata, sort_keys=True, indent=2) + "\n").encode("utf-8")}
    hashes.update({name: hashlib.sha256(data).hexdigest() for name, data in generated.items()})
    generated["EVIDENCE_MANIFEST.sha256.json"] = (json.dumps(hashes, sort_keys=True, indent=2) + "\n").encode("utf-8")
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".evidence-", suffix=".zip", dir=output.parent)
    os.close(descriptor)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for name in sorted(set(members) | set(generated)):
                info = info_for(name)
                if name in generated:
                    archive.writestr(info, generated[name])
                    continue
                copied = hashlib.sha256()
                with members[name].open("rb") as source, archive.open(info, "w") as target:
                    for data in iter(lambda: source.read(CHUNK), b""):
                        copied.update(data)
                        target.write(data)
                if copied.hexdigest() != hashes[name]:
                    raise ValueError(f"Evidence changed during packaging: {name}")
        checksum = file_hash(temporary)
        os.replace(temporary, output)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    output.with_suffix(output.suffix + ".sha256").write_text(checksum + "  " + output.name + "\n", encoding="utf-8")
    return {"path": str(output.resolve()), "sha256": checksum, "files": len(members) + len(generated),
            "bundle_source_commit": metadata["bundle_source_commit"], "historical_product_commit": metadata["historical_product_commit"]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    output = args.output
    if output is None:
        commit = source_commit()
        if not commit:
            parser.error("Outside Git, specify a new --output path explicitly.")
        output = ROOT / "artifacts" / f"codex-learner-0.2.0-perf-{commit[:12]}-evidence.zip"
    print(json.dumps(build(output), indent=2))

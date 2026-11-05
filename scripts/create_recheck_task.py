#!/usr/bin/env python3
"""Bind the packaged native recheck instructions to the actual candidate ZIP."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import zipfile

from package import CHUNK, file_hash


def create(package, output):
    package, output = Path(package), Path(output)
    if package.resolve() == output.resolve():
        raise ValueError("The task cannot replace its candidate ZIP.")
    checksum = file_hash(package)
    prefix = "codex-learner/"
    with zipfile.ZipFile(package) as archive:
        names = archive.namelist()
        manifest = json.loads(archive.read(prefix + "MANIFEST.sha256.json"))
        if len(names) != len(set(names)) or set(names) != {
                prefix + name for name in manifest} | {prefix + "MANIFEST.sha256.json"}:
            raise ValueError("Candidate members do not match its manifest.")
        for name, expected in manifest.items():
            digest = hashlib.sha256()
            with archive.open(prefix + name) as source:
                for chunk in iter(lambda: source.read(CHUNK), b""):
                    digest.update(chunk)
            if digest.hexdigest() != expected:
                raise ValueError(f"Candidate member failed verification: {name}")
        build = json.loads(archive.read(prefix + "BUILD.json"))
        commit = build.get("commit", "")
        if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise ValueError("The candidate must identify a complete source commit.")
        template = archive.read(prefix + "docs/native-followup-recheck-v0.2.md").decode("utf-8")
    if file_hash(package) != checksum:
        raise ValueError("Candidate changed during task generation.")
    text = ("# 本轮定向复验候选：固定包身份\n\n"
            f"- 候选文件：`{package.name}`\n"
            f"- 完整源提交：`{commit}`\n"
            f"- ZIP SHA-256：`{checksum}`\n"
            f"- 包内受清单覆盖成员：{len(manifest)}\n\n"
            "先核对以上身份，再执行下方任务。仅操作新的隔离安装和合成测试库；"
            "本文件生成不代表本机验收已通过。正文中的相对文档路径以解压后的 docs/ 为基准。\n\n"
            "---\n\n" + template)
    output.parent.mkdir(parents=True, exist_ok=True)
    encoded = text.encode("utf-8")
    if output.exists() and output.read_bytes() != encoded:
        raise ValueError("Choose a new task path; existing evidence will not be overwritten.")
    output.write_bytes(encoded)
    return {"path": str(output.resolve()), "sha256": file_hash(output),
            "package_sha256": checksum, "source_commit": commit}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(create(args.package, args.output), ensure_ascii=False, indent=2))

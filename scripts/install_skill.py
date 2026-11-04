#!/usr/bin/env python3
"""Install a self-contained skill and fixed library config without global edits."""

import argparse
import hashlib
import json
import shutil
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "skills/codex-learner"
sys.path.insert(0, str(SOURCE / "scripts"))

from codex_learner.common import LearnerError, canonical, read_json, require
from codex_learner.storage import Library, atomic_write


def source_files(directory):
    return {p.relative_to(directory).as_posix(): p for p in directory.rglob("*")
            if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"
            and p.name not in ("config.json", "install-manifest.json")}


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def install(destination, library_path, backup_and_replace=False):
    destination = Path(destination).expanduser().absolute()
    require(not destination.is_symlink(), "Install destination must not be a symbolic link.")
    require(not SOURCE.resolve().is_relative_to(destination.resolve())
            and not destination.resolve().is_relative_to(SOURCE.resolve()),
            "Install outside the source skill and its parent directories.")
    library = Library(Path(library_path).expanduser().absolute())
    require(not library.root.is_relative_to(destination.resolve())
            and not destination.resolve().is_relative_to(library.root),
            "Keep skill installation and learning data in separate directories.")
    files = source_files(SOURCE)
    hashes = {name: file_hash(path) for name, path in files.items()}
    manifest = {"schema_version": 1, "files": hashes}
    config = {"schema_version": 1, "library_root": str(library.root), "python_executable": sys.executable}
    if destination.exists():
        same = (source_files(destination).keys() == files.keys()
                and all(file_hash(destination / name) == value for name, value in hashes.items())
                and (destination / "config.json").is_file()
                and read_json(destination / "config.json") == config)
        if same:
            library.check()
            return {"destination": str(destination), "library": str(library.root), "changed": False}
        require(backup_and_replace, "Existing skill differs; preserve it with --backup-and-replace.",
                "existing_skill")
    library.initialize()
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = destination.parent / (".codex-learner-install-" + uuid.uuid4().hex)
    stage.mkdir()
    for name, source in files.items():
        output = stage / name
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, output)
    atomic_write(stage / "config.json", canonical(config).encode("utf-8"))
    atomic_write(stage / "install-manifest.json", canonical(manifest).encode("utf-8"))
    backup = None
    if destination.exists():
        # 备份放在 skills 扫描目录之外，避免把旧版也注册成同名 Skill。
        backup_root = destination.parent.parent / "skill-backups"
        backup_root.mkdir(exist_ok=True)
        backup = backup_root / ("codex-learner-" + uuid.uuid4().hex)
        destination.rename(backup)
    try:
        stage.rename(destination)
    except OSError:
        if backup and not destination.exists():
            backup.rename(destination)
        raise
    return {"destination": str(destination), "library": str(library.root), "changed": True,
            "backup": str(backup) if backup else None}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--destination", type=Path, default=Path.home() / ".agents/skills/codex-learner")
    parser.add_argument("--library", type=Path, required=True)
    parser.add_argument("--backup-and-replace", action="store_true")
    args = parser.parse_args()
    try:
        print(json.dumps(install(args.destination, args.library, args.backup_and_replace), ensure_ascii=False, indent=2))
        return 0
    except (LearnerError, OSError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

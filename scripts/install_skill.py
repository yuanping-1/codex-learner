#!/usr/bin/env python3
"""Install a self-contained skill and fixed library config without global edits."""

import argparse
import copy
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
from codex_learner import __version__


def source_files(directory):
    return {p.relative_to(directory).as_posix(): p for p in directory.rglob("*")
            if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"
            and p.name not in ("config.json", "install-manifest.json")}


def installable(name):
    path = Path(name)
    if set(path.parts) & {".venv", "venv", "env", "models", "cache", ".cache", "library", "bodies", "originals"}:
        return False
    return (name == "SKILL.md" or
            (name.startswith("scripts/") and path.suffix == ".py") or
            (name.startswith("assets/") and path.suffix in (".html", ".js", ".css")) or
            (name.startswith("references/") and path.suffix == ".md"))


def file_hash(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def install(destination, library_path=None, backup_and_replace=False, *, python_executable=None):
    """Install code only; upgrades preserve settings and never migrate course data.

    ``library_path`` may be omitted on upgrade. Supplying a different path is an
    error rather than silently switching the user's active learning library.
    """
    destination = Path(destination).expanduser().absolute()
    require(not destination.is_symlink(), "Install destination must not be a symbolic link.")
    require(not SOURCE.resolve().is_relative_to(destination.resolve())
            and not destination.resolve().is_relative_to(SOURCE.resolve()),
            "Install outside the source skill and its parent directories.")
    config_path = destination / "config.json"
    require(not config_path.is_symlink(), "Install configuration must not be a symbolic link.")
    existing = read_json(config_path) if config_path.exists() else None
    if existing is not None:
        require(isinstance(existing, dict) and isinstance(existing.get("library_root"), str),
                "Existing configuration is invalid; preserve it and repair explicitly.")
        config = copy.deepcopy(existing)
        configured_library = Path(config["library_root"]).expanduser().absolute()
        require(library_path is None or Path(library_path).expanduser().resolve() == configured_library.resolve(),
                "An upgrade preserves library_root; change the configured library explicitly before upgrading.",
                "library_change")
        library_path = configured_library
    else:
        require(library_path is not None, "A new installation requires --library.", "library_required")
        config = {"schema_version": 1, "python_executable": sys.executable,
                  "memory": {"core_target_mb": 256, "ocr_limit_mb": 2048},
                  "ocr": {"profile": "rapidocr", "memory_limit_mb": 2048, "timeout_seconds": 300},
                  "drive": {}}
    library = Library(Path(library_path).expanduser().absolute())
    require(not library.root.is_relative_to(destination.resolve())
            and not destination.resolve().is_relative_to(library.root),
            "Keep skill installation and learning data in separate directories.")
    files = {name: path for name, path in source_files(SOURCE).items() if installable(name)}
    require(all(not path.is_symlink() for path in files.values()), "Skill source cannot contain symbolic links.")
    hashes = {name: file_hash(path) for name, path in files.items()}
    manifest = {"schema_version": 1, "version": __version__, "files": hashes,
                "read_schemas": [1, 2], "write_schema": 2}
    if existing is None:
        config["library_root"] = str(library.root)
    if python_executable is not None:
        config["python_executable"] = str(Path(python_executable).expanduser().absolute())
    if destination.exists():
        try:
            installed_manifest = destination / "install-manifest.json"
            manifest_same = (not installed_manifest.is_symlink() and read_json(installed_manifest) == manifest)
        except (LearnerError, FileNotFoundError):
            manifest_same = False
        same = (source_files(destination).keys() == files.keys()
                and all(not (destination / name).is_symlink()
                        and file_hash(destination / name) == value for name, value in hashes.items())
                and (destination / "config.json").is_file()
                and read_json(destination / "config.json") == config and manifest_same)
        if same:
            library.check()
            return {"destination": str(destination), "library": str(library.root), "changed": False}
        require(backup_and_replace, "Existing skill differs; preserve it with --backup-and-replace.",
                "existing_skill")
    library.initialize()
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = destination.parent / (".codex-learner-install-" + uuid.uuid4().hex)
    stage.mkdir()
    backup = None
    try:
        for name, source in files.items():
            output = stage / name
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, output)
            require(file_hash(output) == hashes[name],
                    "Source files changed during installation; retry when editing has finished.", "source_changed")
        atomic_write(stage / "config.json", canonical(config).encode("utf-8"))
        atomic_write(stage / "install-manifest.json", canonical(manifest).encode("utf-8"))
        if destination.exists():
            # Keep backups outside the skills discovery directory.
            backup_root = destination.parent.parent / "skill-backups"
            backup_root.mkdir(exist_ok=True)
            backup = backup_root / ("codex-learner-" + uuid.uuid4().hex)
            destination.rename(backup)
        stage.rename(destination)
    except OSError:
        if backup and not destination.exists():
            backup.rename(destination)
        raise
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return {"destination": str(destination), "library": str(library.root), "changed": True,
            "backup": str(backup) if backup else None}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--destination", type=Path, default=Path.home() / ".agents/skills/codex-learner")
    parser.add_argument("--library", type=Path, help="Required only for a new installation; upgrades preserve the current library")
    parser.add_argument("--python-executable", type=Path, help="Explicitly replace the core Python path; otherwise preserve it")
    parser.add_argument("--backup-and-replace", action="store_true")
    args = parser.parse_args()
    try:
        print(json.dumps(install(args.destination, args.library, args.backup_and_replace,
                                 python_executable=args.python_executable), ensure_ascii=False, indent=2))
        return 0
    except (LearnerError, OSError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

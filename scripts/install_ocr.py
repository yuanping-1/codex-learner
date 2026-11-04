#!/usr/bin/env python3
"""Prepare an isolated OCR environment; downloads require explicit CLI actions.

Ordinary Skill installation never invokes this script. Running without --install
or --prefetch only prints a plan. No existing course or Drive content is changed.
"""

import argparse
import copy
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/codex-learner/scripts"))
from codex_learner.common import LearnerError, canonical, read_json, require
from codex_learner.storage import atomic_write, file_lock

CHUNK = 1024 * 1024
MAX_MODEL_BYTES = 512 * 1024 * 1024
MODEL_LICENSE = "https://github.com/RapidAI/RapidOCR/blob/v3.10.0/python/MODEL_LICENSES.md"
MODEL_BASE = "https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.9.2/onnx/"
# Release hashes, independently checked against the official v3.10.0 notice.
DEFAULT_MANIFEST = {
    "schema_version": 1, "provider": "rapidocr", "model_version": "rapidocr-3.10.0-defaults",
    "license": "Apache-2.0", "license_notice": MODEL_LICENSE,
    "models": {
        "det": {"path": "PP-OCRv6_det_small.onnx",
                "sha256": "090f04abcd9d9a7498bc4ebf677e4cb9bdce1fe4197ddb7e529f1ef44e1ff94f",
                "url": MODEL_BASE + "PP-OCRv6/det/PP-OCRv6_det_small.onnx"},
        "rec": {"path": "PP-OCRv6_rec_small.onnx",
                "sha256": "6f327246b50388f3c176ae304bd95767ea6dc0c9ae92153ef8cbe210b3c14884",
                "url": MODEL_BASE + "PP-OCRv6/rec/PP-OCRv6_rec_small.onnx"},
        "cls": {"path": "ch_ppocr_mobile_v2.0_cls_mobile.onnx",
                "sha256": "e47acedf663230f8863ff1ab0e64dd2d82b838fceb5957146dab185a89d6215c",
                "url": MODEL_BASE + "PP-OCRv4/cls/ch_ppocr_mobile_v2.0_cls_mobile.onnx"},
    },
}


def sha256(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(CHUNK), b""):
            result.update(chunk)
    return result.hexdigest()


def dependency_manifest(requirements):
    """Bind environment receipts to both direct requirements and their locks."""
    pending = [Path(requirements)]
    found = {}
    while pending:
        path = pending.pop().resolve()
        require(path.is_relative_to(ROOT), "Dependency files must belong to the release bundle.")
        name = path.relative_to(ROOT).as_posix()
        if name in found:
            continue
        found[name] = sha256(path)
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith(("-r ", "-c ")):
                pending.append(path.parent / line.strip()[3:].strip())
    return found


def separate(left, right):
    return not left.is_relative_to(right) and not right.is_relative_to(left)


def validate_locations(skill, config, runtime):
    """The OCR runtime must never live inside synced or authoritative data."""
    protected = [skill.resolve(), Path(config["library_root"]).expanduser().resolve()]
    drive = config.get("drive", {})
    if isinstance(drive, dict):
        for key in ("directory", "root", "path", "stream_directory", "archive_directory", "verification_directory"):
            if drive.get(key):
                protected.append(Path(drive[key]).expanduser().resolve())
    for path in protected:
        require(separate(runtime.resolve(), path),
                "Keep the OCR runtime outside the Skill, course library, and configured Drive directories.",
                "invalid_ocr_location")


def validate_manifest(manifest, provider):
    require(isinstance(manifest, dict) and manifest.get("schema_version") == 1
            and manifest.get("provider") == provider and isinstance(manifest.get("models"), dict)
            and bool(manifest["models"]), "Invalid OCR model manifest.", "invalid_models")
    if provider == "rapidocr":
        require({"det", "rec", "cls"} <= set(manifest["models"]),
                "RapidOCR requires det, rec and cls model entries.", "invalid_models")
    paths = set()
    for model in manifest["models"].values():
        require(isinstance(model, dict), "Invalid model entry.", "invalid_models")
        name = model.get("path")
        require(isinstance(name, str) and bool(name) and "\\" not in name and ":" not in name,
                "Model paths must be portable relative paths.", "invalid_models")
        path = PurePosixPath(name)
        require(not path.is_absolute() and ".." not in path.parts and str(path) not in (".", "")
                and name not in paths, "Unsafe or duplicate model path.", "invalid_models")
        require(isinstance(model.get("sha256"), str) and re.fullmatch("[0-9a-f]{64}", model["sha256"]),
                "Every model must have a fixed SHA-256.", "invalid_models")
        paths.add(name)
    return manifest


def prefetch_models(manifest, directory, *, opener=urllib.request.urlopen):
    """Stream verified models into place, retaining prior good files on failure."""
    provider = manifest.get("provider") if isinstance(manifest, dict) else None
    validate_manifest(manifest, provider)
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    transferred = []
    for name, model in manifest["models"].items():
        target = directory / model["path"]
        require(target.resolve().is_relative_to(directory) and not target.is_symlink()
                and all(not parent.is_symlink() for parent in target.parents if parent != directory),
                "Model path cannot escape its directory or use symbolic links.", "invalid_models")
        if target.is_file() and sha256(target) == model["sha256"]:
            continue
        url = model.get("url", "")
        require(isinstance(url, str) and url.startswith("https://"),
                "Explicit model prefetch requires an HTTPS URL for every missing model.", "invalid_models")
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".model-", dir=target.parent)
        try:
            digest = hashlib.sha256()
            size = 0
            started = time.monotonic()
            with os.fdopen(descriptor, "wb") as output, opener(url, timeout=30) as source:
                for chunk in iter(lambda: source.read(CHUNK), b""):
                    size += len(chunk)
                    require(size <= MAX_MODEL_BYTES and time.monotonic() - started <= 300,
                            "Model prefetch exceeded the file size or time limit.", "model_download_limit")
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            require(digest.hexdigest() == model["sha256"],
                    "Downloaded model SHA-256 differs from the release manifest.", "model_hash_mismatch")
            if "size" in model:
                require(size == model["size"], "Downloaded model size differs from manifest.", "model_hash_mismatch")
            os.replace(temporary, target)
            transferred.append(name)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    offline = copy.deepcopy(manifest)
    for model in offline["models"].values():
        model["size"] = (directory / model["path"]).stat().st_size
    manifest_path = directory / "models.json"
    atomic_write(manifest_path, canonical(offline).encode("utf-8"))
    return {"manifest": str(manifest_path), "downloaded": transferred}


def default_runtime():
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "CodexLearner/runtime"
    return Path.home() / ".local/share/codex-learner-runtime"


def setup_ocr(skill, runtime_root, *, python_executable=None, install=False, prefetch=False,
              manifest_path=None, provider="rapidocr", memory_limit_mb=None):
    require(provider in ("rapidocr", "docling"), "Unknown OCR provider.")
    skill = Path(skill).expanduser().resolve()
    config_path = skill / "config.json"
    require(not config_path.is_symlink(), "Skill configuration cannot be a symbolic link.")
    config = read_json(config_path)
    require(isinstance(config, dict) and isinstance(config.get("library_root"), str), "Invalid Skill configuration.")
    key = "ocr" if provider == "rapidocr" else "docling"
    require(isinstance(config.get(key, {}), dict), "OCR settings must be a JSON object.", "invalid_config")
    if memory_limit_mb is None:
        memory_limit_mb = config.get(key, {}).get("memory_limit_mb", 2048)
    require(type(memory_limit_mb) is int and 128 <= memory_limit_mb <= 2048,
            "The conservative OCR profile must stay between 128 and 2048 MiB.", "invalid_memory_budget")
    runtime = Path(runtime_root).expanduser().resolve()
    validate_locations(skill, config, runtime)
    home = runtime / provider
    environment = home / "venv"
    executable = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    models = home / "models"
    cache = home / "cache"
    for managed in (home, environment, models, cache):
        require(not managed.is_symlink() and managed.resolve().is_relative_to(runtime),
                "OCR runtime directories cannot escape through symbolic links.", "invalid_ocr_location")
        validate_locations(skill, config, managed)
    selected_python = str(python_executable or sys.executable)
    requirements = ROOT / ("requirements-docling.txt" if provider == "docling" else "requirements-ocr.txt")
    plan = {"provider": provider, "runtime": str(home), "python_executable": str(executable),
            "requirements": str(requirements), "model_dir": str(models), "cache_dir": str(cache),
            "memory_limit_mb": memory_limit_mb, "installed": False, "prefetched": False,
            "configuration_changed": False}
    if not install and not prefetch:
        return {**plan, "next_step": "Use --install to install packages, then --prefetch to fetch checked models explicitly."}
    manifest = read_json(Path(manifest_path)) if manifest_path else copy.deepcopy(DEFAULT_MANIFEST)
    if prefetch:
        require(provider != "docling" or manifest_path,
                "Docling requires an explicit release model manifest; its models are never fetched implicitly.",
                "models_required")
        validate_manifest(manifest, provider)
    home.mkdir(parents=True, exist_ok=True)
    with file_lock(home / ".setup.lock"):
        marker_path = home / "environment.json"
        if install:
            require(not environment.exists() or marker_path.is_file(),
                    "Refusing to replace an unrecognized OCR environment.", "existing_environment")
            probe = subprocess.run([selected_python, "-c", "import sys; print('%s.%s' % sys.version_info[:2])"],
                                   check=True, capture_output=True, text=True)
            require(probe.stdout.strip() == "3.12",
                    "The isolated OCR lock targets Python 3.12; select it explicitly with --python.", "unsupported_python")
            if not executable.is_file():
                subprocess.run([selected_python, "-m", "venv", str(environment)], check=True)
            atomic_write(marker_path, canonical({"schema_version": 1, "provider": provider,
                                                 "python": probe.stdout.strip(), "ready": False}).encode("utf-8"))
            cache.mkdir(parents=True, exist_ok=True)
            child_environment = {**os.environ, "PIP_CACHE_DIR": str(cache / "pip")}
            subprocess.run([str(executable), "-m", "pip", "install", "-r", str(requirements)],
                           check=True, env=child_environment)
            atomic_write(marker_path, canonical({"schema_version": 1, "provider": provider,
                                                 "python": probe.stdout.strip(), "ready": True,
                                                 "requirements_sha256": sha256(requirements),
                                                 "dependency_manifest": dependency_manifest(requirements)}).encode("utf-8"))
            plan["installed"] = True
        require(executable.is_file() and marker_path.is_file() and read_json(marker_path).get("ready") is True,
                "Run --install successfully before fetching or activating OCR models.", "ocr_not_installed")
        if prefetch:
            result = prefetch_models(manifest, models)
            plan.update(prefetched=True, model_manifest=result["manifest"], downloaded=result["downloaded"])
        # Re-read so independently changed Drive or library configuration is retained.
        latest = read_json(config_path)
        require(isinstance(latest, dict) and isinstance(latest.get(key, {}), dict),
                "OCR settings changed to an invalid value during setup.", "invalid_config")
        validate_locations(skill, latest, runtime)
        settings = copy.deepcopy(latest.get(key, {}))
        settings.update({"profile": provider, "python_executable": str(executable), "model_dir": str(models),
                         "cache_dir": str(cache), "model_manifest": str(models / "models.json"),
                         "worker_lock": str(runtime / ".ocr-worker.lock"),
                         "memory_limit_mb": memory_limit_mb, "timeout_seconds": settings.get("timeout_seconds", 300)})
        latest[key] = settings
        if latest != read_json(config_path):
            atomic_write(config_path, canonical(latest).encode("utf-8"))
            plan["configuration_changed"] = True
    return plan


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skill", type=Path, default=Path.home() / ".agents/skills/codex-learner")
    parser.add_argument("--runtime-root", type=Path, default=default_runtime())
    parser.add_argument("--python", dest="python_executable", help="Base Python 3.12 executable used to create the isolated venv")
    parser.add_argument("--install", action="store_true", help="Explicitly create the venv and install pinned dependencies")
    parser.add_argument("--prefetch", action="store_true", help="Explicitly download fixed models and verify their SHA-256")
    parser.add_argument("--prefetch-manifest", type=Path, help="Optional fixed model release manifest; required for Docling")
    parser.add_argument("--docling", action="store_true", help="Prepare the separate optional Docling environment")
    parser.add_argument("--memory-limit-mb", type=int, help="Preserve the current budget by default; new installs use 2048 MiB")
    args = parser.parse_args()
    try:
        result = setup_ocr(args.skill, args.runtime_root, python_executable=args.python_executable,
                           install=args.install, prefetch=args.prefetch, manifest_path=args.prefetch_manifest,
                           provider="docling" if args.docling else "rapidocr", memory_limit_mb=args.memory_limit_mb)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (LearnerError, OSError, subprocess.CalledProcessError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

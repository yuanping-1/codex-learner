"""Read-only diagnostics: accessible Drive paths never imply cloud sync."""
import importlib.util
import shutil
import sys
from pathlib import Path

from . import __version__
from .common import canonical, read_json, require
from .storage import file_sha256


def _disk(path):
    path = Path(path).expanduser()
    if not path.exists():
        return {"path": str(path), "accessible": False}
    usage = shutil.disk_usage(path)
    return {"path": str(path), "accessible": True, "free_bytes": usage.free}


def doctor(library, config, config_path=None):
    import hashlib
    library.check()
    skill = Path(__file__).resolve().parents[2]
    manifest_path = skill / "install-manifest.json"
    installation = {"path": str(skill), "manifest_present": manifest_path.is_file()}
    if manifest_path.exists():
        manifest = read_json(manifest_path)
        require(isinstance(manifest, dict) and isinstance(manifest.get("files"), dict)
                and bool(manifest["files"]), "Installed manifest has no file hashes.", "invalid_manifest")
        mismatches = []
        for relative, expected in manifest.get("files", {}).items():
            path = (skill / relative).resolve()
            if not path.is_relative_to(skill) or not path.is_file() or file_sha256(path) != expected:
                mismatches.append(relative)
        installation.update(manifest_sha256=file_sha256(manifest_path), mismatches=mismatches,
                            matches_manifest=not mismatches, installed_version=manifest.get("version"))
    ocr = dict(config.get("ocr", {}))
    providers = {name: importlib.util.find_spec(name) is not None
                 for name in ("bs4", "markdown_it", "ijson", "psutil")}
    ocr_status = {"configured": bool(ocr.get("python_executable")), "profile": ocr.get("profile", "rapidocr"),
                  "python_executable": ocr.get("python_executable"),
                  "python_available": bool(ocr.get("python_executable") and Path(ocr["python_executable"]).is_file()),
                  "models_verified": False,
                  "dependencies": "checked_when_worker_starts", "memory_limit_mib": ocr.get("memory_limit_mb", 2048),
                  "memory_limit_bytes": ocr.get("memory_limit_mb", 2048) * 1024 ** 2,
                  "commit_limit": "Windows Job Object", "working_set_measurement": "process_group_peak_rss_bytes"}
    if ocr.get("model_manifest") and ocr.get("model_dir"):
        from .ocr import offline_models
        try:
            manifest, paths = offline_models(ocr["model_dir"], ocr["model_manifest"], ocr_status["profile"])
            ocr_status.update(models_verified=True, model_count=len(paths),
                              model_manifest_sha256=file_sha256(ocr["model_manifest"]))
        except Exception as exc:
            ocr_status["model_error"] = str(exc)
    if ocr.get("cache_dir"):
        ocr_status["cache"] = _disk(ocr["cache_dir"])
    drive = config.get("drive", {})
    drive_path = drive.get("directory") or drive.get("path") or drive.get("archive_directory")
    drive_status = {"configured": bool(drive_path), "mode": "desktop_streaming", "cloud_sync": "unconfirmed"}
    if drive_path:
        drive_status.update(_disk(drive_path))
        active = library.root.resolve()
        drive_root = Path(drive_path).expanduser().resolve()
        drive_status["active_library_separate"] = not (active.is_relative_to(drive_root) or drive_root.is_relative_to(active))
    tasks = []
    from .storage import task_owner_alive
    for folder in library.root.iterdir():
        if folder.is_dir() and not folder.is_symlink() and not folder.name.startswith("."):
            for pattern in (".task-running-*.json", ".archive-running-*.json"):
                for marker in folder.glob(pattern):
                    try:
                        tasks.append({"course_id": folder.name, "task": marker.name,
                                      "owner_running": task_owner_alive(read_json(marker))})
                    except Exception as exc:
                        tasks.append({"course_id": folder.name, "task": marker.name, "error": str(exc)})
    return {"version": __version__, "tasks": tasks, "read_schemas": [1, 2], "write_schema": 2,
            "library": str(library.root), "library_accessible": True, "library_disk": _disk(library.root),
            "python": sys.version.split()[0], "python_executable": sys.executable,
            "configured_python": config.get("python_executable"), "config_path": str(config_path) if config_path else None,
            "installation": installation, "base_dependencies": providers, "ocr": ocr_status,
            "core_target_mib": config.get("memory", {}).get("core_target_mb", 256), "drive": drive_status,
            "host_submission": "requires_real_client_validation",
            "new_conversation_discovery": "requires_real_client_validation"}

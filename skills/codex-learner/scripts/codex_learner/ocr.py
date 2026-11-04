"""Optional, offline OCR process boundary. Never imports an OCR model in the core."""

import hashlib
import json
import os
import sys
import uuid
from contextlib import ExitStack
from pathlib import Path

from .common import LearnerError, canonical, require
from .storage import atomic_write, file_lock, file_sha256

PROTOCOL_VERSION = 1
MAX_MEMORY_BYTES = 2 * 1024 ** 3
MAX_PIXELS = 3_000_000
MAX_SIDE = 2000
DPI = 150
CORE_RESERVE_BYTES = 256 * 1024 ** 2


def offline_models(model_dir, manifest_path, profile="rapidocr"):
    root = Path(model_dir).expanduser().resolve()
    require(root.is_dir(), "Offline model directory is unavailable.", "ocr_models_missing")
    manifest_path = Path(manifest_path)
    require(manifest_path.is_file(), "Offline model manifest is unavailable.", "ocr_models_missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    require(manifest.get("schema_version") == 1 and manifest.get("provider") == profile,
            "Unsupported OCR model manifest.", "ocr_models_invalid")
    models = manifest.get("models")
    require(isinstance(models, dict) and models, "Model manifest has no files.", "ocr_models_invalid")
    if profile == "rapidocr":
        require({"det", "rec", "cls"} <= set(models), "Detection, recognition and orientation models are required.",
                "ocr_models_missing")
    paths = {}
    for key, item in models.items():
        require(isinstance(item, dict) and isinstance(item.get("path"), str), "Invalid model record.", "ocr_models_invalid")
        relative = Path(item["path"])
        path = (root / relative).resolve()
        require(not relative.is_absolute() and path.is_relative_to(root) and path.is_file(),
                "Model path is missing or outside model directory.", "ocr_models_invalid")
        require(file_sha256(path) == item.get("sha256"), "Offline model checksum mismatch.", "ocr_models_invalid")
        paths[key] = str(path)
    return manifest, paths


def run_ocr(source_path, source_sha256, config, *, page_numbers=None, force=False, resume=True, crop=None):
    """One provider worker per local cache, including checkpoint publication."""
    require(isinstance(config, dict), "OCR is not configured; install an explicit offline provider.", "ocr_unconfigured")
    cache_root = Path(config.get("cache_dir", "")).expanduser()
    require(cache_root.is_absolute(), "OCR cache must use an absolute local path.", "ocr_unconfigured")
    cache_root.mkdir(parents=True, exist_ok=True)
    lock_paths = {cache_root / ".ocr-worker.lock"}
    if config.get("worker_lock"):
        shared_lock = Path(config["worker_lock"]).expanduser()
        require(shared_lock.is_absolute(), "Shared OCR worker lock must be an absolute path.", "ocr_unconfigured")
        shared_lock.parent.mkdir(parents=True, exist_ok=True)
        lock_paths.add(shared_lock)
    try:
        with ExitStack() as locks:
            for lock_path in sorted(lock_paths):
                locks.enter_context(file_lock(lock_path))
            return _run_ocr_locked(source_path, source_sha256, config, page_numbers=page_numbers,
                                   force=force, resume=resume, crop=crop)
    except LearnerError as exc:
        if exc.code == "busy":
            raise LearnerError("ocr_busy", "Another OCR worker is using this cache; retry the unchanged request.",
                               worker_started=False, retryable=True) from exc
        raise


def _run_ocr_locked(source_path, source_sha256, config, *, page_numbers=None, force=False, resume=True, crop=None):
    require(isinstance(config, dict), "OCR is not configured; install an explicit offline provider.", "ocr_unconfigured")
    profile = config.get("profile", "rapidocr")
    require(profile in ("rapidocr", "docling"), "Unknown OCR provider.")
    executable = Path(config.get("python_executable", "")).expanduser()
    require(executable.is_absolute() and executable.is_file(), "OCR Python must be an existing absolute path.", "ocr_unconfigured")
    model_dir = Path(config.get("model_dir", "")).expanduser()
    manifest_path = Path(config.get("model_manifest", "")).expanduser()
    cache_root = Path(config.get("cache_dir", "")).expanduser()
    require(model_dir.is_absolute() and manifest_path.is_absolute() and cache_root.is_absolute(),
            "OCR model, manifest and cache paths must be absolute.", "ocr_unconfigured")
    if page_numbers is not None:
        require(isinstance(page_numbers, list) and page_numbers
                and all(type(n) is int and n >= 1 for n in page_numbers) and len(set(page_numbers)) == len(page_numbers),
                "OCR pages must be distinct original page numbers starting at one.")
    if profile == "docling":
        require(page_numbers is not None and len(page_numbers) <= 3,
                "Docling requires an explicit range of at most three original pages.", "ocr_scope_required")
    manifest, _ = offline_models(model_dir, manifest_path, profile)
    memory_mb = config.get("memory_limit_mb", 2048)
    timeout = config.get("timeout_seconds", 300)
    require(type(memory_mb) is int and 64 <= memory_mb <= 2048,
            "OCR memory budget must be 64–2048 MiB; it is never raised automatically.")
    require(isinstance(timeout, (int, float)) and 0 < timeout <= 3600, "OCR timeout must be 1–3600 seconds.")
    identity = {"protocol_version": PROTOCOL_VERSION, "source_sha256": source_sha256,
                "profile": profile, "manifest_sha256": file_sha256(manifest_path),
                "pages": sorted(page_numbers) if page_numbers else None, "force": bool(force),
                "max_pixels": MAX_PIXELS, "max_side": MAX_SIDE, "dpi": DPI,
                "worker_sha256": file_sha256(Path(__file__).with_name("ocr_worker.py")), "crop": crop}
    require(type(resume) is bool, "resume must be true or false.")
    if not resume:
        identity["run_nonce"] = uuid.uuid4().hex
    fingerprint = hashlib.sha256(canonical(identity).encode()).hexdigest()
    output = cache_root / fingerprint
    output.mkdir(parents=True, exist_ok=True)
    request = {**identity, "fingerprint": fingerprint, "source_path": str(Path(source_path).resolve()),
               "output_dir": str(output.resolve()), "model_dir": str(model_dir.resolve()),
               "model_manifest": str(manifest_path.resolve())}
    request_file = output / "request.json"
    atomic_write(request_file, canonical(request).encode())
    environment = dict(os.environ)
    scripts = str(Path(__file__).resolve().parents[1])
    environment.update(PYTHONPATH=scripts, HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
                       HF_DATASETS_OFFLINE="1", OMP_NUM_THREADS="2", MKL_NUM_THREADS="2",
                       OPENBLAS_NUM_THREADS="2")
    import psutil
    available = psutil.virtual_memory().available
    required = memory_mb * 1024 ** 2 + CORE_RESERVE_BYTES
    if available < required:
        raise LearnerError("ocr_memory_unavailable",
                           "Available physical memory is below the OCR budget plus the 256 MiB core reserve.",
                           worker_started=False, available_memory_bytes=available,
                           required_available_bytes=required, memory_limit_bytes=memory_mb * 1024 ** 2)
    from .resource_limits import run_limited
    try:
        run = run_limited([str(executable), "-m", "codex_learner.ocr_worker", "--request", str(request_file)],
                          timeout=timeout, memory_limit=memory_mb * 1024 ** 2, env=environment)
    except RuntimeError as exc:
        raise LearnerError("ocr_supervisor_missing", str(exc)) from exc
    memory_evidence = {key: run.get(key) for key in (
        "hard_limit_kind", "job_memory_limit_bytes", "job_limit_flags", "job_limit_verified",
        "initial_peak_job_memory_used_bytes", "peak_job_memory_used_bytes", "job_query_error",
        "peak_sampled_private_commit_bytes", "last_sampled_private_commit_bytes", "private_commit_sample_count")}
    if run["status"] != "completed" or run["returncode"] != 0:
        completed = 0
        try:
            checkpoint = json.loads((output / "checkpoint.json").read_text(encoding="utf-8"))
            if checkpoint.get("fingerprint") == fingerprint:
                completed = len(checkpoint.get("pages", []))
        except (OSError, ValueError, TypeError):
            pass
        raise LearnerError("ocr_failed", "OCR did not finish; completed page checkpoints remain retryable.",
                           status=run["status"], returncode=run["returncode"],
                           stderr=run.get("stderr", "")[-4000:], checkpoint=str(output / "checkpoint.json"),
                           unfinished=True, completed_pages=completed, peak_rss_bytes=run["peak_rss_bytes"],
                           peak_commit_bytes=run.get("peak_commit_bytes"), memory_limit_bytes=memory_mb * 1024 ** 2,
                           hard_limit_enforced=run["hard_limit_enforced"], elapsed_seconds=run["elapsed_seconds"],
                           cleanup_verified=run.get("cleanup_verified", False),
                           surviving_process_ids=run.get("surviving_process_ids", []),
                           preview_manifest=str(output / "previews" / "manifest.json")
                           if (output / "previews" / "manifest.json").is_file() else None)
    result_path = output / "result.json"
    require(result_path.is_file(), "OCR worker returned no result.", "ocr_protocol_error")
    result = json.loads(result_path.read_text(encoding="utf-8"))
    require(result.get("protocol_version") == PROTOCOL_VERSION and result.get("fingerprint") == fingerprint,
            "OCR response does not match the requested source and configuration.", "ocr_protocol_error")
    pages = []
    for page in result.get("pages", []):
        converted = dict(page)
        for key, artifact in page["artifacts"].items():
            path = (output / artifact["path"]).resolve()
            require(path.is_relative_to(output.resolve()) and path.is_file()
                    and file_sha256(path) == artifact["sha256"], "OCR artifact is missing or corrupt.", "ocr_artifact_invalid")
            converted[key + "_path"] = str(path)
        pages.append(converted)
    return {"pages": pages, "run": {"provider": profile, "model_version": manifest.get("model_version"),
            "model_manifest_sha256": identity["manifest_sha256"], "source_sha256": source_sha256,
            "protocol_version": PROTOCOL_VERSION, "peak_rss_bytes": run["peak_rss_bytes"],
            "peak_commit_bytes": run.get("peak_commit_bytes"), "completed_pages": len(pages),
            "source_page_count": result.get("source_page_count"), "unfinished": False,
            "available_memory_bytes_at_start": available, "core_reserve_bytes": CORE_RESERVE_BYTES,
            "observed_process_ids": run.get("observed_process_ids", []),
            "cleanup_verified": run.get("cleanup_verified", False),
            "surviving_process_ids": run.get("surviving_process_ids", []),
            "hard_limit_enforced": run["hard_limit_enforced"], "memory_limit_bytes": memory_mb * 1024 ** 2,
            "elapsed_seconds": run["elapsed_seconds"], "resumed_pages": result.get("resumed_pages", 0),
            "versions": result.get("versions", {}), **memory_evidence}}

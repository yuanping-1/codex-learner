"""One-page-at-a-time OCR worker; only this process loads optional OCR libraries."""

import argparse
import importlib.metadata
import json
import math
import socket
import sys
from pathlib import Path

from .common import LearnerError, canonical, require
from .ocr import DPI, MAX_PIXELS, MAX_SIDE, PROTOCOL_VERSION, offline_models
from .storage import atomic_write, file_sha256


def render_scale(width, height, dpi=DPI):
    require(width > 0 and height > 0, "Invalid page dimensions.", "ocr_page_invalid")
    require(type(dpi) is int and 72 <= dpi <= 300, "Rendering dpi must be 72–300.")
    return min(dpi / 72, MAX_SIDE / max(width, height), math.sqrt(MAX_PIXELS / (width * height)))


def _artifact(path, output):
    return {"path": path.relative_to(output).as_posix(), "sha256": file_sha256(path),
            "byte_length": path.stat().st_size}


def _check_page(page, output):
    for item in page.get("artifacts", {}).values():
        path = (output / item["path"]).resolve()
        if not path.is_relative_to(output.resolve()) or not path.is_file() or file_sha256(path) != item["sha256"]:
            return False
    return bool(page.get("artifacts", {}).get("text"))


def _deny_network(*args, **kwargs):
    raise OSError("Offline OCR worker cannot access network; prefetch and verify models separately.")


def _rapid_engine(request):
    _, paths = offline_models(request["model_dir"], request["model_manifest"], "rapidocr")
    from rapidocr import RapidOCR
    params = {"Det.model_path": paths["det"], "Rec.model_path": paths["rec"], "Cls.model_path": paths["cls"],
              "Global.model_root_dir": request["model_dir"], "Global.max_side_len": MAX_SIDE,
              "Global.return_word_box": False, "Global.return_single_char_box": False,
              "Det.limit_type": "max", "Det.limit_side_len": MAX_SIDE, "Rec.rec_batch_num": 1,
              "EngineConfig.onnxruntime.intra_op_num_threads": 2,
              "EngineConfig.onnxruntime.inter_op_num_threads": 1,
              "EngineConfig.onnxruntime.enable_cpu_mem_arena": False,
              "EngineConfig.onnxruntime.use_cuda": False, "EngineConfig.onnxruntime.use_dml": False}
    if "rec_keys" in paths:
        params["Rec.rec_keys_path"] = paths["rec_keys"]
    return RapidOCR(params=params)


def _usable_text(text):
    return bool(text.strip()) and "\ufffd" not in text and not any(ord(c) < 32 and c not in "\n\r\t" for c in text)


def _native_text(page):
    textpage = page.get_textpage()
    try:
        return textpage.get_text_range()
    finally:
        textpage.close()


def _save_page(output, number, text, method, geometry, data, image=None):
    require(len(text.encode("utf-8")) <= 256 * 1024,
            "Extracted page exceeds 256 KiB; no text was truncated.", "ocr_text_budget")
    base = f"page-{number:06d}"
    text_path, data_path = output / (base + ".txt"), output / (base + ".json")
    atomic_write(text_path, text.encode("utf-8"))
    atomic_write(data_path, canonical(data).encode("utf-8"))
    artifacts = {"text": _artifact(text_path, output), "data": _artifact(data_path, output)}
    if image is not None:
        image_path = output / (base + ".png")
        image.save(image_path, format="PNG")
        artifacts["image"] = _artifact(image_path, output)
    return {"original_page_no": number, "method": method, "status": "pending_review",
            "geometry": geometry, "artifacts": artifacts}


def process_pdf(request, engine_factory=None):
    """Keep only one rendered page; cached results hold paths and hashes, not pixels."""
    import pypdfium2 as pdfium
    source, output = Path(request["source_path"]), Path(request["output_dir"])
    require(source.stat().st_size <= 32 * 1024 * 1024, "Source exceeds 32 MiB.", "source_too_large")
    require(source.is_file() and file_sha256(source) == request["source_sha256"],
            "OCR source checksum mismatch.", "ocr_source_invalid")
    output.mkdir(parents=True, exist_ok=True)
    checkpoint_path = output / "checkpoint.json"
    versions = request.get("runtime_versions", {})
    checkpoint = {"protocol_version": PROTOCOL_VERSION, "fingerprint": request["fingerprint"],
                  "runtime_versions": versions, "pages": []}
    if checkpoint_path.exists():
        previous = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        require(previous.get("fingerprint") == request["fingerprint"], "OCR checkpoint identity mismatch.", "ocr_checkpoint_invalid")
        require(previous.get("runtime_versions", {}) == versions,
                "OCR provider packages changed; retry with resume=false to create a separate cache.",
                "ocr_environment_changed")
        checkpoint = previous
    cached = {p["original_page_no"]: p for p in checkpoint["pages"] if _check_page(p, output)}
    engine, resumed, results, extracted_bytes = None, 0, [], 0
    factory = engine_factory or (lambda: _rapid_engine(request))
    with pdfium.PdfDocument(source) as document:
        page_count = len(document)
        require(page_count <= 500, "Source exceeds the 500-page processing budget.", "ocr_page_budget")
        selected = request.get("pages") or list(range(1, len(document) + 1))
        require(selected and all(type(n) is int and 1 <= n <= len(document) for n in selected),
                "Requested OCR page is outside the original document.", "ocr_page_invalid")
        for number in selected:
            if number in cached:
                results.append(cached[number])
                extracted_bytes += cached[number]["artifacts"]["text"]["byte_length"]
                require(extracted_bytes <= 8 * 1024 * 1024,
                        "Cached extracted text exceeds 8 MiB; no source was published.", "ocr_text_budget")
                resumed += 1
                continue
            page = document[number - 1]
            bitmap = array = image = ocr_result = None
            try:
                width, height = page.get_size()
                original_crop = list(page.get_cropbox())
                geometry = {"page_width_points": width, "page_height_points": height,
                            "rotation_degrees": page.get_rotation(), "coordinate_system": "top-left-pixels",
                            "crop_box_points": original_crop, "original_page_no": number}
                crop = request.get("crop")
                if crop:
                    rect = crop["rect"]
                    require(crop["page_no"] == number and len(selected) == 1
                            and original_crop[0] <= rect[0] < rect[2] <= original_crop[2]
                            and original_crop[1] <= rect[1] < rect[3] <= original_crop[3],
                            "Crop must be inside the selected original page crop box.", "ocr_crop_invalid")
                    # This mutates only the in-memory page object. The original
                    # PDF file remains untouched, and pixels are rendered anew.
                    page.set_cropbox(*rect)
                    width, height = page.get_size()
                    geometry["requested_crop_rect_points"] = list(rect)
                native = _native_text(page) if not crop else ""
                if _usable_text(native) and not request.get("force") and not crop:
                    record = _save_page(output, number, native, "native_pdf", geometry,
                                        {"text": native, "original_page_no": number, "method": "native_pdf"})
                else:
                    if engine is None:
                        engine = factory()
                    scale = render_scale(width, height, crop.get("dpi", DPI) if crop else DPI)
                    bitmap = page.render(scale=scale, limit_image_cache=True)
                    require(bitmap.width * bitmap.height <= MAX_PIXELS + MAX_SIDE * 2
                            and max(bitmap.width, bitmap.height) <= MAX_SIDE + 1,
                            "Rendered page exceeds pixel budget.", "ocr_page_invalid")
                    array = bitmap.to_numpy()
                    ocr_result = engine(array)
                    texts = list(ocr_result.txts or ())
                    scores = [float(v) for v in (ocr_result.scores or ())]
                    boxes = ocr_result.boxes.tolist() if ocr_result.boxes is not None else []
                    transform = bitmap.get_posconv(page)
                    pdf_boxes = [[list(transform.to_page(round(x), round(y))) for x, y in box] for box in boxes]
                    geometry.update(render_scale=scale, width_pixels=bitmap.width, height_pixels=bitmap.height)
                    image = bitmap.to_pil()
                    record = _save_page(output, number, "\n".join(texts), "rapidocr", geometry,
                                        {"original_page_no": number, "method": "rapidocr", "texts": texts,
                                         "scores": scores, "boxes": boxes, "boxes_pdf_points": pdf_boxes,
                                         "geometry": geometry}, image)
                extracted_bytes += record["artifacts"]["text"]["byte_length"]
                require(extracted_bytes <= 8 * 1024 * 1024,
                        "Extracted text exceeds 8 MiB; completed pages remain retryable.", "ocr_text_budget")
                results.append(record)
                checkpoint["pages"] = results
                atomic_write(checkpoint_path, canonical(checkpoint).encode())
            finally:
                # Result objects may retain the input image. Drop every wrapper
                # before freeing the shared bitmap and page handles.
                ocr_result = array = None
                if image is not None:
                    image.close()
                    image = None
                if bitmap is not None:
                    bitmap.close()
                    bitmap = None
                page.close()
    return {"protocol_version": PROTOCOL_VERSION, "fingerprint": request["fingerprint"],
            "pages": results, "resumed_pages": resumed, "source_page_count": page_count}


def _prepare_docling_previews(request):
    """Keep bounded review images before importing any Docling model library.

    These are recovery aids, not completed OCR pages. The separate manifest is
    updated after each image so a later resource-limit termination preserves
    already rendered previews. Bounds apply to these previews, not to Docling's
    own internal rasterization, which remains under the process memory budget.
    """
    import pypdfium2 as pdfium

    output = Path(request["output_dir"])
    folder = output / "previews"
    folder.mkdir(parents=True, exist_ok=True)
    manifest_path = folder / "manifest.json"
    versions = request.get("runtime_versions", {})
    manifest = {"protocol_version": PROTOCOL_VERSION, "fingerprint": request["fingerprint"],
                "source_sha256": request["source_sha256"], "runtime_versions": versions,
                "status": "pending_review", "completed": False, "rows": []}
    # Do not reuse unchecked image files or previews made by another renderer.
    cached = {}
    if manifest_path.exists():
        try:
            previous = json.loads(manifest_path.read_text(encoding="utf-8"))
            if (previous.get("fingerprint") == request["fingerprint"]
                    and previous.get("runtime_versions", {}) == versions):
                for row in previous.get("rows", []):
                    item = row.get("image", {})
                    image_path = (output / item.get("path", "")).resolve()
                    if (image_path.is_relative_to(output.resolve()) and image_path.is_file()
                            and file_sha256(image_path) == item.get("sha256")):
                        cached[row["original_page_no"]] = {**row, "status": "pending_review", "completed": False}
        except (ValueError, KeyError, TypeError, OSError):
            cached = {}
    pages = request["pages"]
    manifest["rows"] = [cached[number] for number in pages if number in cached]
    atomic_write(manifest_path, canonical(manifest).encode())
    with pdfium.PdfDocument(request["source_path"]) as document:
        require(len(document) <= 500, "Source exceeds the 500-page processing budget.", "ocr_page_budget")
        require(pages and all(type(n) is int and 1 <= n <= len(document) for n in pages),
                "Requested Docling page is outside the original document.", "ocr_page_invalid")
        for number in pages:
            if number in cached:
                continue
            page = document[number - 1]
            bitmap = image = None
            try:
                width, height = page.get_size()
                scale = render_scale(width, height)
                bitmap = page.render(scale=scale, limit_image_cache=True)
                require(bitmap.width * bitmap.height <= MAX_PIXELS + MAX_SIDE * 2
                        and max(bitmap.width, bitmap.height) <= MAX_SIDE + 1,
                        "Docling preview exceeds pixel budget.", "ocr_page_invalid")
                geometry = {"page_width_points": width, "page_height_points": height,
                            "rotation_degrees": page.get_rotation(), "crop_box_points": list(page.get_cropbox()),
                            "coordinate_system": "top-left-pixels", "original_page_no": number,
                            "render_scale": scale, "width_pixels": bitmap.width, "height_pixels": bitmap.height}
                image = bitmap.to_pil()
                image_path = folder / f"page-{number:06d}.png"
                image.save(image_path, format="PNG")
                manifest["rows"].append({"original_page_no": number, "geometry": geometry,
                                         "image": _artifact(image_path, output),
                                         "status": "pending_review", "completed": False})
                manifest["rows"].sort(key=lambda row: pages.index(row["original_page_no"]))
                atomic_write(manifest_path, canonical(manifest).encode())
            finally:
                if image is not None:
                    image.close()
                    image = None
                if bitmap is not None:
                    bitmap.close()
                page.close()
    return manifest_path


def process_docling(request):
    """Explicit <=3-page enhancement; model memory stays inside the worker."""
    pages = request.get("pages")
    require(isinstance(pages, list) and 1 <= len(pages) <= 3, "Docling requires one to three explicit pages.")
    require(Path(request["source_path"]).stat().st_size <= 32 * 1024 * 1024,
            "Source exceeds 32 MiB.", "source_too_large")
    require(file_sha256(request["source_path"]) == request["source_sha256"],
            "OCR source checksum mismatch.", "ocr_source_invalid")
    output = Path(request["output_dir"])
    output.mkdir(parents=True, exist_ok=True)
    cached = {}
    checkpoint_path = output / "checkpoint.json"
    if checkpoint_path.exists():
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        require(checkpoint.get("fingerprint") == request["fingerprint"],
                "OCR checkpoint identity mismatch.", "ocr_checkpoint_invalid")
        require(checkpoint.get("runtime_versions", {}) == request.get("runtime_versions", {}),
                "OCR provider packages changed; retry with resume=false to create a separate cache.",
                "ocr_environment_changed")
        cached = {p["original_page_no"]: p for p in checkpoint.get("pages", []) if _check_page(p, output)}
    if all(number in cached for number in pages):
        return {"protocol_version": PROTOCOL_VERSION, "fingerprint": request["fingerprint"],
                "pages": [cached[n] for n in pages], "resumed_pages": len(pages)}
    preview_path = _prepare_docling_previews(request)
    preview_manifest = json.loads(preview_path.read_text(encoding="utf-8"))
    bounded_scale = min(row["geometry"]["render_scale"] for row in preview_manifest["rows"])
    _, model_paths = offline_models(request["model_dir"], request["model_manifest"], "docling")
    from docling.datamodel.accelerator_options import AcceleratorDevice, AcceleratorOptions
    from docling.datamodel.backend_options import ThreadedDoclingParseBackendOptions
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions, RapidOcrOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption
    rapid_params = {"Global.max_side_len": MAX_SIDE, "Det.limit_type": "max", "Det.limit_side_len": MAX_SIDE,
                    "Rec.rec_batch_num": 1, "EngineConfig.onnxruntime.intra_op_num_threads": 2,
                    "EngineConfig.onnxruntime.inter_op_num_threads": 1,
                    "EngineConfig.onnxruntime.enable_cpu_mem_arena": False,
                    "EngineConfig.onnxruntime.use_cuda": False, "EngineConfig.onnxruntime.use_dml": False}
    explicit_paths = ({"det_model_path": model_paths["det"], "rec_model_path": model_paths["rec"],
                       "cls_model_path": model_paths["cls"]}
                      if {"det", "rec", "cls"} <= set(model_paths) else {})
    if "rec_keys" in model_paths:
        explicit_paths["rec_keys_path"] = model_paths["rec_keys"]
    options = PdfPipelineOptions(artifacts_path=request["model_dir"], do_ocr=True, do_table_structure=True,
                                 do_formula_enrichment=False, do_picture_description=False,
                                 generate_page_images=False, generate_picture_images=False,
                                 generate_parsed_pages=False, images_scale=bounded_scale,
                                 ocr_batch_size=1, layout_batch_size=1,
                                 table_batch_size=1, queue_max_size=1, document_timeout=120,
                                 accelerator_options=AcceleratorOptions(device=AcceleratorDevice.CPU, num_threads=2),
                                 ocr_options=RapidOcrOptions(backend="onnxruntime", lang=["ch"],
                                                             scale=bounded_scale, rapidocr_params=rapid_params,
                                                             **explicit_paths))
    converter = DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(
        pipeline_options=options, backend_options=ThreadedDoclingParseBackendOptions(max_concurrent_results=1))})
    records, resumed = [], 0
    for number in pages:
        if number in cached:
            records.append(cached[number])
            resumed += 1
            continue
        result = converter.convert(request["source_path"], page_range=(number, number))
        # Never silently publish partial conversions as complete OCR.
        require(str(result.status.value) == "success", "Docling page conversion was incomplete.", "ocr_partial")
        text = result.document.export_to_markdown()
        data = result.document.export_to_dict()
        record = _save_page(output, number, text, "docling", {"original_page_no": number}, data)
        records.append(record)
        atomic_write(output / "checkpoint.json", canonical({"protocol_version": PROTOCOL_VERSION,
                     "fingerprint": request["fingerprint"], "runtime_versions": request.get("runtime_versions", {}),
                     "pages": records}).encode())
        del result, data
    return {"protocol_version": PROTOCOL_VERSION, "fingerprint": request["fingerprint"], "pages": records,
            "resumed_pages": resumed}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True, type=Path)
    args = parser.parse_args()
    try:
        request = json.loads(args.request.read_text(encoding="utf-8"))
        require(request.get("protocol_version") == PROTOCOL_VERSION, "Unsupported OCR protocol.")
        versions = {}
        for package in ("rapidocr", "onnxruntime", "pypdfium2", "docling"):
            try:
                versions[package] = importlib.metadata.version(package)
            except importlib.metadata.PackageNotFoundError:
                pass
        request["runtime_versions"] = versions
        # The only documented networking step is explicit model prefetch. Reject
        # accidental model/font downloads and remote document URLs at runtime.
        socket.create_connection = _deny_network
        socket.socket.connect = _deny_network
        socket.socket.connect_ex = _deny_network
        require(Path(request["source_path"]).is_file(), "Worker accepts a local PDF path only.")
        result = process_docling(request) if request["profile"] == "docling" else process_pdf(request)
        result["versions"] = versions
        atomic_write(Path(request["output_dir"]) / "result.json", canonical(result).encode())
        print(json.dumps({"ok": True, "pages": len(result["pages"]), "resumed_pages": result["resumed_pages"]}))
        return 0
    except Exception as exc:
        error = exc.as_dict() if isinstance(exc, LearnerError) else {"error": type(exc).__name__, "message": str(exc)}
        print(json.dumps({"ok": False, **error}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

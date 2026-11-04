"""Bounded source import: immutable originals, body references and honest review state."""

import json
import math
import re
import shutil
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path
from urllib.parse import urlparse
from xml.etree import ElementTree

from .common import LearnerError, identifier, now, require
from .storage import file_sha256, read_body, write_artifact_from_file

MAX_SOURCE_BYTES = 32 * 1024 * 1024
COPY_CHUNK_BYTES = 1024 * 1024
MAX_STRUCTURE_BYTES = 1024 * 1024
MAX_TEXT_BYTES = 8 * 1024 * 1024
MAX_SEGMENT_BYTES = 256 * 1024
MAX_SEGMENTS = 20_000
MAX_PAGES = 500
PARSE_TIMEOUT_SECONDS = 60
DPI_DEFAULT = 150


def copy_source(source, target):
    """Do not create a whole-document bytes object, even for the original copy."""
    size = 0
    with Path(source).open("rb") as incoming, Path(target).open("xb") as outgoing:
        while block := incoming.read(COPY_CHUNK_BYTES):
            size += len(block)
            require(size <= MAX_SOURCE_BYTES, "Source exceeds 32 MiB.", "source_too_large")
            outgoing.write(block)
    return size


def _segment(locator, text, *, reviewed=True, kind="text", **extra):
    return {"locator": locator, "text": text, "verified": reviewed,
            "verification_status": "verified" if reviewed else "pending_review", "kind": kind, **extra}


def _resource(resources, missing, locator, uri, kind="image"):
    resources.append({"locator": locator, "uri": uri, "kind": kind, "status": "missing"})
    missing.append(f"{locator}: {kind} resource not archived; inspect the original ({uri})")


def _markdown(path, missing, resources):
    require(path.stat().st_size <= MAX_STRUCTURE_BYTES,
            "Structured Markdown exceeds the 1 MiB parser budget; split the source explicitly.", "parser_budget")
    try:
        from markdown_it import MarkdownIt
    except ImportError as exc:
        raise LearnerError("parser_missing", "Markdown import needs requirements-base.txt.") from exc
    text = path.read_text(encoding="utf-8-sig")
    tokens = MarkdownIt("commonmark").enable("table").parse(text)
    complex_lines = set()
    for token in tokens:
        start, end = token.map or (0, 0)
        if token.type in ("table_open", "html_block"):
            complex_lines.update(range(start + 1, end + 1))
            missing.append(f"line:{start + 1}: structured content requires review")
        for child in token.children or []:
            if child.type == "image":
                _resource(resources, missing, f"line:{start + 1}", child.attrGet("src") or "unknown")
                complex_lines.update(range(start + 1, end + 1))
            elif child.type == "html_inline":
                complex_lines.update(range(start + 1, end + 1))
                missing.append(f"line:{start + 1}: inline HTML requires review")
    lines = text.splitlines()
    formula_lines, closing = set(), None
    delimiters = re.compile(r"(?<!\\)\$\$|\\\[|\\\]|\\(?:begin|end)\{(?:equation\*?|align\*?|gather\*?|displaymath|math)\}")
    for i, line in enumerate(lines, 1):
        if closing is not None:
            formula_lines.add(i)
        for match in delimiters.finditer(line):
            token = match.group()
            formula_lines.add(i)
            if token == closing:
                closing = None
            elif closing is None:
                if token == "$$":
                    closing = "$$"
                elif token == r"\[":
                    closing = r"\]"
                elif token.startswith(r"\begin{"):
                    closing = token.replace(r"\begin{", r"\end{", 1)
    for i, line in enumerate(lines, 1):
        if not line.strip():
            continue
        math = i in formula_lines or bool(re.search(r"(?<!\\)\$[^$]+\$|\\\(|\\\[|<math\b|<svg\b", line, re.I))
        if math:
            missing.append(f"line:{i}: formula or drawing requires visual review")
        yield _segment(f"line:{i}", line, reviewed=i not in complex_lines and not math)


def _html(path, missing, resources):
    require(path.stat().st_size <= MAX_STRUCTURE_BYTES,
            "HTML exceeds the 1 MiB structural parser budget; supply a smaller archived section.", "parser_budget")
    try:
        from bs4 import BeautifulSoup, Comment, NavigableString
    except ImportError as exc:
        raise LearnerError("parser_missing", "HTML import needs requirements-base.txt.") from exc
    with path.open(encoding="utf-8-sig") as stream:
        soup = BeautifulSoup(stream, "html.parser")
    for node in soup(["script", "style", "noscript", "template"]):
        node.decompose()
    for i, node in enumerate(soup.find_all(["img", "iframe", "object", "embed"]), 1):
        uri = node.get("src") or node.get("data") or "embedded resource"
        _resource(resources, missing, f"resource:{i}", str(uri), "image" if node.name == "img" else node.name)
    # Select structural blocks once; a table's cells and math's children must not
    # also become flattened, supposedly verified paragraphs.
    names = ["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "pre", "table", "math", "svg"]
    selected = {id(n) for n in soup.find_all(names) if not n.find_parent(names)}
    blocks, loose_text, loose_visual, loose_container = [], [], False, None
    containers = {"div", "section", "article", "figure", "figcaption", "blockquote", "header", "footer",
                  "nav", "aside", "main", "dt", "dd", "address", "body"}

    def flush_loose_text():
        nonlocal loose_text, loose_visual, loose_container
        if loose_text:
            block = soup.new_tag("div")
            block.string = " ".join(loose_text)
            if loose_visual:
                block["data-learner-unarchived-image"] = "true"
            blocks.append(block)
        loose_text, loose_visual, loose_container = [], False, None

    # Preserve text outside the recognized blocks in DOM order. Otherwise a
    # single <p> would make adjacent div/section/figcaption text disappear.
    for node in soup.descendants:
        if id(node) in selected:
            flush_loose_text()
            blocks.append(node)
        elif not any(id(parent) in selected for parent in node.parents):
            if isinstance(node, NavigableString) and not isinstance(node, Comment) and node.strip():
                container = next((id(parent) for parent in node.parents if parent.name in containers), id(soup))
                if loose_container is not None and loose_container != container:
                    flush_loose_text()
                loose_container = container
                loose_text.append(str(node).strip())
            elif getattr(node, "name", None) == "img":
                loose_visual = True
    flush_loose_text()
    for i, node in enumerate(blocks, 1):
        locator = f"block:{i}"
        complex_node = (node.name in ("table", "math", "svg") or node.has_attr("data-learner-unarchived-image")
                        or bool(node.find(["table", "math", "svg", "sup", "sub", "img"])))
        if node.name == "table":
            rows = []
            for row in node.find_all("tr"):
                rows.append(" | ".join(cell.get_text(" ", strip=True) for cell in row.find_all(["td", "th"], recursive=False)))
            text = "\n".join(rows)
            structure = {"_structure": {"rows": [line.split(" | ") for line in rows]}}
        elif complex_node:
            # Preserve formula markup instead of silently changing x^2 to x2.
            text = str(node)
            structure = {}
        else:
            text = node.get_text(" ", strip=True)
            structure = {}
        if text.strip():
            if complex_node:
                missing.append(f"{locator}: table, equation or graphic requires visual review")
            yield _segment(locator, text, reviewed=not complex_node,
                           kind=node.name if node.name in ("table", "math", "svg") else "text", **structure)


def _docx(path, missing, resources):
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    with zipfile.ZipFile(path) as archive:
        require(sum(info.file_size for info in archive.infolist()) <= MAX_SOURCE_BYTES,
                "Expanded DOCX exceeds 32 MiB.", "source_too_large")
        for name in archive.namelist():
            if name.startswith(("word/header", "word/footer", "word/footnotes", "word/endnotes")):
                missing.append(f"{name}: not extracted; inspect the archived original")
        require(archive.getinfo("word/document.xml").file_size <= MAX_STRUCTURE_BYTES,
                "DOCX document XML exceeds the 1 MiB parser budget; split the source explicitly.", "parser_budget")
        xml = archive.read("word/document.xml")
        require(b"<!DOCTYPE" not in xml and b"<!ENTITY" not in xml,
                "DOCX XML entities are not supported.", "unsupported_source")
        root = ElementTree.fromstring(xml)
        body = root.find("w:body", ns)
        require(body is not None, "DOCX has no document body.", "unreadable_source")
        paragraph, table = 0, 0
        for element in body:
            if element.tag.endswith("}p"):
                paragraph += 1
                text = "".join(node.text or "" for node in element.findall(".//w:t", ns))
                locator, kind = f"paragraph:{paragraph}", "text"
            elif element.tag.endswith("}tbl"):
                table += 1
                rows = [" | ".join("".join(n.text or "" for n in cell.findall(".//w:t", ns))
                                   for cell in row.findall("w:tc", ns)) for row in element.findall("w:tr", ns)]
                text, locator, kind = "\n".join(rows), f"table:{table}", "table"
            else:
                continue
            visual = any(node.tag.endswith(("}drawing", "}oMath", "}pict")) for node in element.iter())
            if visual or kind == "table":
                missing.append(f"{locator}: table, drawing or equation needs visual review")
            if text.strip():
                yield _segment(locator, text, reviewed=not visual and kind != "table", kind=kind)


def _pdf(path, missing, resources, page_numbers=None):
    try:
        from pypdf import PdfReader
        from pypdf.errors import PyPdfError
    except ImportError:
        require(shutil.which("pdftotext") is not None,
                "PDF import needs pypdf or pdftotext.", "pdf_reader_missing")
        # The command writes to disk; stdout is not an unbounded document buffer.
        with tempfile.TemporaryDirectory(prefix="learner-pdf-text-") as folder:
            target = Path(folder) / "text.txt"
            result = subprocess.run(["pdftotext", "-layout", str(path), str(target)],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
            require(result.returncode == 0, "PDF text extraction failed.", "unreadable_source")
            require(target.stat().st_size <= MAX_TEXT_BYTES,
                    "PDF extracted text exceeds 8 MiB; no text was truncated.", "parser_budget")
            with target.open(encoding="utf-8") as stream:
                page, lines = 1, []
                for line in stream:
                    pieces = line.split("\f")
                    for j, piece in enumerate(pieces):
                        if j:
                            require(page <= MAX_PAGES, "PDF exceeds the 500-page processing budget.", "parser_budget")
                            if page_numbers is None or page in page_numbers:
                                yield _pdf_segment(page, "".join(lines), missing)
                            page, lines = page + 1, []
                        lines.append(piece)
                if "".join(lines).strip() and (page_numbers is None or page in page_numbers):
                    yield _pdf_segment(page, "".join(lines), missing)
                require(page_numbers is None or max(page_numbers) <= page,
                        "Page range exceeds the source.")
        return
    try:
        with path.open("rb") as stream:
            reader = PdfReader(stream)
            require(not reader.is_encrypted, "Encrypted PDF needs an unlocked copy.", "encrypted_source")
            require(len(reader.pages) <= MAX_PAGES, "PDF exceeds the 500-page processing budget.", "parser_budget")
            require(page_numbers is None or max(page_numbers) <= len(reader.pages), "Page range exceeds the source.")
            for i, page in enumerate(reader.pages, 1):
                if page_numbers is None or i in page_numbers:
                    yield _pdf_segment(i, page.extract_text() or "", missing)
    except PyPdfError as exc:
        raise LearnerError("unreadable_source", "PDF could not be parsed; preserve the original.") from exc


def _pdf_segment(page, text, missing):
    missing.append(f"page:{page}: " + ("visual verification required" if text.strip() else "no readable text; OCR required"))
    return _segment(f"page:{page}", text, reviewed=False, kind="page", original_page_no=page,
                    extraction_method="native_pdf")


def iter_segments(path, extension, missing, resources, page_numbers=None):
    if extension == ".txt":
        with path.open(encoding="utf-8-sig") as stream:
            for i, line in enumerate(iter(lambda: stream.readline(MAX_SEGMENT_BYTES + 1), ""), 1):
                require(len(line.encode("utf-8")) <= MAX_SEGMENT_BYTES,
                        "Source segment exceeds 256 KiB; no text was truncated.", "parser_budget")
                if line.strip():
                    yield _segment(f"line:{i}", line.rstrip("\r\n"))
    elif extension == ".md":
        yield from _markdown(path, missing, resources)
    elif extension in (".html", ".htm"):
        yield from _html(path, missing, resources)
    elif extension == ".docx":
        yield from _docx(path, missing, resources)
    elif extension == ".pdf":
        yield from _pdf(path, missing, resources, page_numbers)
    else:
        raise LearnerError("unsupported_source", "Use PDF, DOCX, UTF-8 MD/TXT, or an archived HTML page.")


def extract(data, extension):
    """Compatibility helper for small test fixtures; production uses paths."""
    require(len(data) <= MAX_SOURCE_BYTES, "Source exceeds 32 MiB.", "source_too_large")
    with tempfile.TemporaryDirectory(prefix="learner-extract-") as folder:
        path = Path(folder) / ("source" + extension)
        path.write_bytes(data)
        missing, resources = [], []
        segments = list(iter_segments(path, extension, missing, resources))
        require(bool(segments), "No readable content was found.", "unreadable_source")
        return segments, missing


def cleanup_prepared_source(prepared):
    if prepared and prepared.get("_temporary"):
        prepared["_temporary"].cleanup()


def import_options(request, ocr_config):
    """Normalize explicit aliases; resource overrides may only lower a budget."""
    profile = request.get("profile", "auto")
    require(profile in ("auto", "native", "rapidocr", "docling"), "Unknown source extraction profile.")
    if "ocr" in request:
        require(type(request["ocr"]) is bool, "ocr must be true or false.")
        require("profile" not in request, "Use profile or legacy ocr, not both.")
        profile = "rapidocr" if request["ocr"] else "native"
    if profile == "auto":
        configured = (isinstance(ocr_config, dict) and all(
            isinstance(ocr_config.get(key), str) and ocr_config[key]
            for key in ("python_executable", "model_dir", "model_manifest", "cache_dir")))
        profile = ocr_config.get("profile", "rapidocr") if configured else "native"
    pages = request.get("page_numbers", request.get("ocr_pages"))
    require(not ("page_numbers" in request and "ocr_pages" in request), "Use only one page-number field.")
    if "page_range" in request:
        require(pages is None, "Use page_range or page_numbers, not both.")
        value = request["page_range"]
        require(isinstance(value, (list, tuple)) and len(value) == 2
                and all(type(n) is int for n in value) and 1 <= value[0] <= value[1] <= MAX_PAGES,
                "page_range must be [first,last], inclusive original page numbers from 1 to 500.")
        pages = list(range(value[0], value[1] + 1))
    if pages is not None:
        require(isinstance(pages, list) and pages and all(type(n) is int and 1 <= n <= MAX_PAGES for n in pages)
                and len(set(pages)) == len(pages), "Page numbers must be distinct original pages from 1 to 500.")
    crop = request.get("crop")
    if crop is not None:
        require(isinstance(crop, dict) and set(crop) <= {"page_no", "rect", "dpi"}, "Invalid crop fields.")
        number, rect, dpi = crop.get("page_no"), crop.get("rect"), crop.get("dpi", DPI_DEFAULT)
        require(type(number) is int and 1 <= number <= MAX_PAGES
                and isinstance(rect, list) and len(rect) == 4
                and all(type(v) in (int, float) and math.isfinite(v) for v in rect)
                and rect[0] < rect[2] and rect[1] < rect[3]
                and type(dpi) is int and 72 <= dpi <= 300,
                "crop needs page_no, a finite PDF [left,bottom,right,top] rect, and dpi 72–300.")
        require(pages is None or pages == [number], "A crop request targets exactly its one original page.")
        require(profile == "rapidocr", "Local crop rereading requires the RapidOCR provider.")
        pages, crop = [number], {"page_no": number, "rect": rect, "dpi": dpi}
    resume = request.get("resume", True)
    require(type(resume) is bool, "resume must be true or false.")
    require(type(request.get("force_ocr", False)) is bool, "force_ocr must be true or false.")
    config = dict(ocr_config) if isinstance(ocr_config, dict) else None
    if "memory_budget_mb" in request:
        budget = request["memory_budget_mb"]
        require(type(budget) is int and 64 <= budget <= 2048, "memory_budget_mb must be 64–2048.")
        require(config is not None and budget <= config.get("memory_limit_mb", 2048),
                "A per-import memory budget may only lower the configured OCR budget.")
        config["memory_limit_mb"] = budget
    if profile != "native":
        require(config is not None and config.get("profile", "rapidocr") == profile,
                "The requested OCR provider is not configured.", "ocr_unconfigured")
    return profile, pages, resume, config, crop


def _worker_segments(pages, missing):
    for page in pages:
        text_path = Path(page["text_path"])
        require(text_path.stat().st_size <= MAX_SEGMENT_BYTES,
                "OCR page exceeds the 256 KiB text budget; no text was truncated.", "parser_budget")
        text = text_path.read_text(encoding="utf-8")
        number = page["original_page_no"]
        missing.append(f"page:{number}: " + ("extracted content requires visual review" if text.strip()
                                             else "no readable text; inspect original"))
        crop = page.get("geometry", {}).get("requested_crop_rect_points")
        locator = f"page:{number}" + (":crop:" + ",".join(str(v) for v in crop) if crop else "")
        yield _segment(locator, text, reviewed=False, kind="crop" if crop else "page", original_page_no=number,
                       extraction_method=page["method"], ocr_status=page["status"], ocr_artifact=page)


def prepare_source(request, ocr_config=None):
    """Parse outside the course lock; caller must cleanup in a finally block."""
    path = Path(request["path"]).expanduser()
    require(path.is_absolute(), "Source paths must be absolute.")
    require(path.is_file(), "Source file is unavailable.", "source_missing")
    require(path.stat().st_size <= MAX_SOURCE_BYTES, "Source exceeds 32 MiB.", "source_too_large")
    extension, url = path.suffix.lower(), request.get("url")
    profile, page_numbers, resume, provider_config, crop = import_options(request, ocr_config)
    require(extension == ".pdf" or (page_numbers is None and not request.get("ocr")
                                   and request.get("profile", "auto") in ("auto", "native")),
            "Page selection and OCR profiles currently apply to PDF files only.")
    if extension in (".html", ".htm"):
        parsed = urlparse(url) if isinstance(url, str) else None
        require(parsed is not None and parsed.scheme in ("http", "https") and parsed.hostname
                and parsed.username is None and parsed.password is None,
                "Archived web pages require their original HTTP(S) URL without credentials.")
    temporary = tempfile.TemporaryDirectory(prefix="learner-source-")
    stage = Path(temporary.name)
    original = stage / ("original" + extension)
    prepared = {"_temporary": temporary, "_stage": stage, "_original": original,
                "extension": extension, "original_name": path.name, "segments": [], "missing": [],
                "resources": [], "url": url, "captured_at": request.get("captured_at", now())}
    try:
        copy_source(path, original)
        prepared["source_sha256"] = file_sha256(original)
        if extension == ".pdf" and profile != "native":
            from .ocr import run_ocr
            result = run_ocr(original, prepared["source_sha256"], provider_config,
                             page_numbers=page_numbers, force=request.get("force_ocr", False), resume=resume, crop=crop)
            prepared["ocr_run"] = result["run"]
            raw_segments = _worker_segments(result["pages"], prepared["missing"])
        else:
            raw_segments = iter_segments(original, extension, prepared["missing"], prepared["resources"], page_numbers)
        prepared["reading_scope"] = {"profile": profile if extension == ".pdf" else "native",
                                     "page_numbers": page_numbers, "full_source_requested": page_numbers is None,
                                     "crop": crop}
        (stage / "segments").mkdir()
        total_text_bytes, started = 0, time.monotonic()
        for i, segment in enumerate(raw_segments, 1):
            require(i <= MAX_SEGMENTS and time.monotonic() - started <= PARSE_TIMEOUT_SECONDS,
                    "Source exceeds the segment or parsing-time budget; no source was published.", "parser_budget")
            if "_structure" in segment:
                structure_path = stage / "segments" / f"{i:06d}.json"
                structure_path.write_text(json.dumps(segment.pop("_structure"), ensure_ascii=False), encoding="utf-8")
                segment["_structure_path"] = structure_path
            text = segment.pop("text")
            size = len(text.encode("utf-8"))
            total_text_bytes += size
            require(size <= MAX_SEGMENT_BYTES and total_text_bytes <= MAX_TEXT_BYTES,
                    "Source exceeds the 256 KiB segment or 8 MiB extracted-text budget; no text was truncated.",
                    "parser_budget")
            body = stage / "segments" / f"{i:06d}.txt"
            body.write_text(text, encoding="utf-8", newline="")
            segment.update(_text_path=body, text_bytes=body.stat().st_size, text_nonempty=bool(text.strip()))
            prepared["segments"].append(segment)
        require(prepared["segments"], "No readable content was found.", "unreadable_source")
        return prepared
    except (UnicodeError, zipfile.BadZipFile, ElementTree.ParseError, KeyError) as exc:
        cleanup_prepared_source(prepared)
        raise LearnerError("unreadable_source", f"Unable to read {extension} source.") from exc
    except BaseException:
        cleanup_prepared_source(prepared)
        raise


def archive_source(state, directory, request, prepared):
    source_id = identifier(request["source_id"])
    require(source_id not in state["sources"], "A source version is immutable; use a new source_id.", "id_conflict")
    prefix = f"sources/{source_id}"
    artifact = write_artifact_from_file(directory, f"{prefix}/original{prepared['extension']}", prepared["_original"])
    segments = []
    for i, source_segment in enumerate(prepared["segments"], 1):
        segment = {k: v for k, v in source_segment.items() if not k.startswith("_") and k != "ocr_artifact"}
        segment["text_ref"] = {**write_artifact_from_file(directory, f"{prefix}/segments/{i:06d}.txt",
                                                          source_segment["_text_path"]), "encoding": "utf-8"}
        if source_segment.get("_structure_path"):
            segment["structure_ref"] = write_artifact_from_file(directory, f"{prefix}/segments/{i:06d}.json",
                                                                  source_segment["_structure_path"])
        if source_segment.get("ocr_artifact"):
            page = source_segment["ocr_artifact"]
            for key in ("image_path", "data_path"):
                if page.get(key):
                    item = Path(page[key])
                    segment[key.replace("_path", "_ref")] = write_artifact_from_file(
                        directory, f"{prefix}/pages/{page['original_page_no']:06d}/{item.name}", item)
            segment["provenance"] = {k: page[k] for k in ("original_page_no", "method", "status", "geometry") if k in page}
        segments.append(segment)
    record = {k: v for k, v in prepared.items() if not k.startswith("_") and k not in ("segments", "source_sha256")}
    state["sources"][source_id] = {**artifact, **record, "source_id": source_id, "segments": segments,
                                  "imported_at": now()}
    return {"source_id": source_id, "sha256": artifact["sha256"], "segments": len(segments),
            "missing": prepared["missing"]}


def segment_text(segment, directory=None):
    if "text_ref" in segment:
        require(directory is not None, "Source body directory is required.", "body_unavailable")
        return read_body(directory, segment["text_ref"])
    return segment.get("text", "")


def check_references(state, references, directory=None):
    directory = directory or getattr(state, "_directory", None)
    require(isinstance(references, list) and references, "At least one verified source reference is required.")
    for ref in references:
        require(isinstance(ref, dict), "A source reference must be an object.")
        source = state["sources"].get(ref.get("source_id"))
        require(source is not None, "Unknown source reference.")
        segment = next((s for s in source["segments"] if s["locator"] == ref.get("locator")), None)
        require(segment is not None and segment.get("verified") is True
                and not segment.get("legacy_claim", False)
                and segment.get("verification_status") != "legacy_claim"
                and segment.get("status") != "legacy_claim",
                "Source location is missing or needs verification.", "unverified_source")
        event_id = segment.get("verification_event_id")
        require(not source.get("legacy_claim", False) or bool(event_id),
                "Legacy source requires explicit location review.", "unverified_source")
        if event_id:
            event = state.get("source_verifications", {}).get(event_id)
            expected_hash = segment.get("text_ref", {}).get("sha256")
            require(event is not None and event.get("source_id") == ref["source_id"]
                    and event.get("source_sha256") == source["sha256"]
                    and event.get("segment_hashes", {}).get(ref["locator"]) == expected_hash,
                    "Source verification is not bound to these bytes.", "unverified_source")
        require(bool(segment_text(segment, directory).strip()),
                "Source location has no readable text.", "unverified_source")

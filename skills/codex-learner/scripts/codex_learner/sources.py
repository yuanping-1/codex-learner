"""Archive supplied material without moving originals or guessing missing text."""

import io
import shutil
import subprocess
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse
from xml.etree import ElementTree

from .common import LearnerError, identifier, now, require
from .storage import write_artifact

MAX_SOURCE_BYTES = 32 * 1024 * 1024


class PageText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hidden = 0
        self.parts = []
        self.images = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self.hidden += 1
        if tag == "img":
            self.images += 1
        if tag in ("h1", "h2", "h3", "h4", "p", "div", "br", "li", "tr"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript") and self.hidden:
            self.hidden -= 1

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def extract(data, extension):
    missing = []
    if extension in (".txt", ".md"):
        text = data.decode("utf-8-sig")
        segments = [{"locator": f"line:{i}", "text": line, "verified": True}
                    for i, line in enumerate(text.splitlines(), 1) if line.strip()]
    elif extension == ".docx":
        ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            require(sum(info.file_size for info in archive.infolist()) <= MAX_SOURCE_BYTES,
                    "Expanded DOCX exceeds the import limit.", "source_too_large")
            xml = archive.read("word/document.xml")
            for name in archive.namelist():
                if name.startswith(("word/header", "word/footer", "word/footnotes", "word/endnotes")):
                    missing.append(f"{name}: not extracted; inspect the archived original")
            require(b"<!DOCTYPE" not in xml and b"<!ENTITY" not in xml,
                    "DOCX XML entities are not supported.", "unsupported_source")
            root = ElementTree.fromstring(xml)
            body = root.find("w:body", ns)
            require(body is not None, "DOCX has no document body.", "unreadable_source")
            segments = []
            paragraph, table = 0, 0
            for element in body:
                if element.tag.endswith("}p"):
                    paragraph += 1
                    text = "".join(element.itertext()) if not list(element) else "".join(
                        node.text or "" for node in element.findall(".//w:t", ns))
                    locator = f"paragraph:{paragraph}"
                elif element.tag.endswith("}tbl"):
                    table += 1
                    rows = []
                    for row in element.findall("w:tr", ns):
                        rows.append(" | ".join("".join(node.text or "" for node in cell.findall(".//w:t", ns))
                                               for cell in row.findall("w:tc", ns)))
                    text, locator = "\n".join(rows), f"table:{table}"
                else:
                    continue
                visual = any(node.tag.endswith(("}drawing", "}oMath", "}pict")) for node in element.iter())
                if text.strip():
                    segments.append({"locator": locator, "text": text, "verified": not visual})
                if visual:
                    missing.append(f"{locator}: drawing or equation needs visual review")
    elif extension == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError:
            require(shutil.which("pdftotext") is not None,
                    "PDF import needs pypdf or pdftotext; no source was published.", "pdf_reader_missing")
            result = subprocess.run(["pdftotext", "-layout", "-", "-"], input=data, capture_output=True,
                                    timeout=60, check=False)
            require(result.returncode == 0, "PDF text extraction failed.", "unreadable_source")
            pages = result.stdout.decode("utf-8").split("\f")
            if pages and not pages[-1].strip():
                pages.pop()
        else:
            from pypdf.errors import PyPdfError
            try:
                reader = PdfReader(io.BytesIO(data))
                require(not reader.is_encrypted, "Encrypted PDF needs an unlocked copy.", "encrypted_source")
                pages = [page.extract_text() or "" for page in reader.pages]
            except PyPdfError as exc:
                raise LearnerError("unreadable_source", "PDF could not be parsed; preserve the original.") from exc
        segments = [{"locator": f"page:{i}", "text": text, "verified": False}
                    for i, text in enumerate(pages, 1)]
        missing = [f"page:{i}: " + ("visual verification required" if text.strip() else "no readable text; OCR required")
                   for i, text in enumerate(pages, 1)]
    elif extension in (".html", ".htm"):
        parser = PageText()
        parser.feed(data.decode("utf-8-sig"))
        segments = [{"locator": f"block:{i}", "text": line.strip(), "verified": True}
                    for i, line in enumerate("".join(parser.parts).splitlines(), 1) if line.strip()]
        if parser.images:
            missing.append(f"{parser.images} images are not archived by text import; archive and verify separately")
    else:
        raise LearnerError("unsupported_source", "Use PDF, DOCX, UTF-8 MD/TXT, or an archived HTML page.")
    require(bool(segments), "No readable content was found.", "unreadable_source")
    return segments, missing


def prepare_source(request):
    """读取和解析在课程锁外进行，保存副本与引用在事务内完成。"""
    path = Path(request["path"]).expanduser()
    require(path.is_absolute(), "Source paths must be absolute.")
    require(path.is_file(), "Source file is unavailable.", "source_missing")
    require(path.stat().st_size <= MAX_SOURCE_BYTES, "Source exceeds 32 MiB.", "source_too_large")
    data = path.read_bytes()
    require(len(data) <= MAX_SOURCE_BYTES, "Source exceeds 32 MiB.", "source_too_large")
    extension = path.suffix.lower()
    url = request.get("url")
    if extension in (".html", ".htm"):
        require(isinstance(url, str) and urlparse(url).scheme in ("http", "https")
                and bool(urlparse(url).hostname) and urlparse(url).username is None
                and urlparse(url).password is None,
                "Archived web pages require their original HTTP(S) URL without credentials.")
    try:
        segments, missing = extract(data, extension)
    except (UnicodeError, zipfile.BadZipFile, ElementTree.ParseError, KeyError) as exc:
        raise LearnerError("unreadable_source", f"Unable to read {extension} source.") from exc
    return {"data": data, "extension": extension, "original_name": path.name, "segments": segments,
            "missing": missing, "url": url, "captured_at": request.get("captured_at", now())}


def archive_source(state, directory, request, prepared):
    source_id = identifier(request["source_id"])
    require(source_id not in state["sources"], "A source version is immutable; use a new source_id.", "id_conflict")
    artifact = write_artifact(directory, f"sources/{source_id}/original{prepared['extension']}", prepared["data"])
    state["sources"][source_id] = {**artifact, "source_id": source_id,
                                  **{k: v for k, v in prepared.items() if k != "data"},
                                  "imported_at": now()}
    return {"source_id": source_id, "sha256": artifact["sha256"],
            "segments": len(prepared["segments"]), "missing": prepared["missing"]}


def check_references(state, references):
    require(isinstance(references, list) and references, "At least one verified source reference is required.")
    for ref in references:
        require(isinstance(ref, dict), "A source reference must be an object.")
        source = state["sources"].get(ref.get("source_id"))
        require(source is not None, "Unknown source reference.")
        segment = next((s for s in source["segments"] if s["locator"] == ref.get("locator")), None)
        require(segment is not None and segment["verified"] and bool(segment["text"].strip()),
                "Source location is missing or needs verification.", "unverified_source")

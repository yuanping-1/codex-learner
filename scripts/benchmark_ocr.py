#!/usr/bin/env python3
"""Explicit local OCR measurement; creates fixtures, never installs or downloads.

Run with the configured OCR Python (Pillow/pypdf are needed to create fixtures).
The repeated-page fixture measures scaling, not diverse OCR accuracy.
"""

import argparse
import json
import platform
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/codex-learner/scripts"))

from codex_learner.ocr import run_ocr
from codex_learner.storage import file_sha256


def create_fixture(path, label, font_path=None):
    from PIL import Image, ImageDraw, ImageFont
    image = Image.new("RGB", (1240, 1754), "white")
    try:
        draw = ImageDraw.Draw(image)
        font = ImageFont.truetype(str(font_path), 52) if font_path else ImageFont.load_default(size=48)
        lines = ["100 m in 20 seconds", "Average speed = 5 m/s", "36 km/h = 10 m/s",
                 "OCR requires visual review", label]
        if font_path:
            lines.insert(0, "比例与单位 验收样例")
        for row, line in enumerate(lines):
            draw.text((60, 100 + row * 140), line, font=font, fill="black")
        image.save(path, "PDF", resolution=150)
    finally:
        image.close()


def repeated_pdf(source, target, count):
    from pypdf import PdfReader, PdfWriter
    writer = PdfWriter()
    with source.open("rb") as incoming:
        reader = PdfReader(incoming)
        for _ in range(count):
            writer.add_page(reader.pages[0])
        with target.open("wb") as outgoing:
            writer.write(outgoing)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path, help="Existing Skill config or direct OCR config JSON")
    parser.add_argument("--work-dir", required=True, type=Path, help="Dedicated local fixture directory, outside a course")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--font", type=Path, help="Optional installed font with Chinese glyphs")
    parser.add_argument("--crop", action="store_true", help="Also reread an original PDF crop at 300 DPI, within pixel limits")
    args = parser.parse_args()
    settings = json.loads(args.config.read_text(encoding="utf-8"))
    config = settings.get("ocr", settings)
    if config.get("profile", "rapidocr") != "rapidocr":
        parser.error("This benchmark is for the RapidOCR provider.")
    work = args.work_dir.resolve()
    work.mkdir(parents=True, exist_ok=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    module = ROOT / "skills/codex-learner/scripts/codex_learner"
    report = {
        "platform": platform.platform(), "benchmark_python": sys.version,
        "fixture": "Self-authored scanned page repeated for 10/100-page scaling; not a diverse accuracy benchmark",
        "windows_job_tested": sys.platform == "win32",
        "code_sha256": {name: file_sha256(module / name) for name in ("ocr.py", "ocr_worker.py", "resource_limits.py")},
        "benchmark_sha256": file_sha256(__file__), "results": [],
    }

    def save():
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    def measure(source, kind, pages, **options):
        started = time.monotonic()
        result = run_ocr(source, file_sha256(source), config, resume=False, **options)
        assert len(result["pages"]) == pages
        assert result["run"]["cleanup_verified"] and not result["run"]["surviving_process_ids"]
        sample = Path(result["pages"][0]["text_path"]).read_text(encoding="utf-8")
        assert "100" in sample and "20" in sample and "5" in sample
        row = {"kind": kind, "file": source.name, "pages": pages, "source_sha256": file_sha256(source),
               "source_bytes": source.stat().st_size, "wall_seconds": time.monotonic() - started,
               "run": result["run"], "sample_text": sample,
               "all_pending_review": all(p["status"] == "pending_review" for p in result["pages"]),
               "geometry": result["pages"][0].get("geometry")}
        report["results"].append(row)
        save()
        print(json.dumps({"kind": kind, "pages": pages, "peak_rss_bytes": row["run"]["peak_rss_bytes"],
                          "seconds": row["wall_seconds"], "cleanup_verified": row["run"]["cleanup_verified"]}), flush=True)

    try:
        base = work / "fixture-base.pdf"
        create_fixture(base, "Repeated page", args.font)
        for count in (10, 100):
            source = work / f"fixture-{count}.pdf"
            repeated_pdf(base, source, count)
            measure(source, "page_scaling", count)
        for label in ("A", "B", "C"):
            source = work / f"consecutive-{label}.pdf"
            create_fixture(source, f"Separate document {label}", args.font)
            measure(source, "consecutive_document", 1)
        if args.crop:
            measure(base, "original_pdf_crop", 1, page_numbers=[1],
                    crop={"page_no": 1, "rect": [20, 390, 550, 780], "dpi": 300})
        ten, hundred = report["results"][:2]
        report["rss_ratio_100_to_10"] = hundred["run"]["peak_rss_bytes"] / ten["run"]["peak_rss_bytes"]
        report["complete"] = True
    except Exception as error:
        report["complete"] = False
        report["error"] = error.as_dict() if hasattr(error, "as_dict") else {"type": type(error).__name__, "message": str(error)}
        save()
        raise
    save()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

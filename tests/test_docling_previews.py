"""Recovery previews are real PDF renders, not evidence of Docling inference."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from support import ROOT
from test_ocr import text_pdf
from codex_learner.common import LearnerError
from codex_learner.ocr_worker import _prepare_docling_previews, process_docling, process_pdf
from codex_learner.storage import file_sha256


@unittest.skipUnless(importlib.util.find_spec("pypdfium2"), "Optional pypdfium2 is not installed")
class DoclingPreviewTests(unittest.TestCase):
    def request(self, root, pages=7, selected=None):
        source = root / "input.pdf"
        text_pdf(source, pages)
        return {"source_path": str(source), "source_sha256": file_sha256(source),
                "output_dir": str(root / "output"), "pages": selected or [3, 7],
                "fingerprint": "preview-fixture", "runtime_versions": {"pypdfium2": "fixture-version"},
                "model_dir": str(root / "missing-models"), "model_manifest": str(root / "missing-manifest")}

    def test_previews_keep_original_page_numbers_before_model_validation(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            request = self.request(root)
            with self.assertRaises(LearnerError) as error:
                process_docling(request)
            self.assertEqual(error.exception.code, "ocr_models_missing")
            output = root / "output"
            manifest = json.loads((output / "previews/manifest.json").read_text())
            self.assertFalse(manifest["completed"])
            self.assertEqual(manifest["runtime_versions"], request["runtime_versions"])
            self.assertEqual([row["original_page_no"] for row in manifest["rows"]], [3, 7])
            for row in manifest["rows"]:
                self.assertFalse(row["completed"])
                self.assertEqual(row["status"], "pending_review")
                self.assertEqual(file_sha256(output / row["image"]["path"]), row["image"]["sha256"])
                geometry = row["geometry"]
                self.assertLessEqual(max(geometry["width_pixels"], geometry["height_pixels"]), 2001)
                self.assertLessEqual(geometry["width_pixels"] * geometry["height_pixels"], 3_004_000)
            self.assertFalse((output / "checkpoint.json").exists())
            Path(request["source_path"]).rename(root / "moved.pdf")

    def test_failure_on_later_preview_preserves_first_and_releases_handles(self):
        from PIL import Image
        original_save = Image.Image.save
        calls = 0

        def fail_second(image, target, *args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("simulated failure after first completed preview")
            return original_save(image, target, *args, **kwargs)

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            request = self.request(root)
            with patch.object(Image.Image, "save", fail_second), self.assertRaises(OSError):
                _prepare_docling_previews(request)
            manifest = json.loads((root / "output/previews/manifest.json").read_text())
            self.assertEqual([row["original_page_no"] for row in manifest["rows"]], [3])
            self.assertFalse(manifest["completed"])
            self.assertFalse((root / "output/checkpoint.json").exists())
            Path(request["source_path"]).rename(root / "moved.pdf")

    def test_resume_rejects_changed_runtime_versions_without_relabeling_cached_text(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            request = self.request(root, pages=1, selected=[1])
            process_pdf(request, engine_factory=lambda: self.fail("native text does not need OCR"))
            checkpoint = root / "output/checkpoint.json"
            before = checkpoint.read_bytes()
            request["runtime_versions"] = {"pypdfium2": "different-fixture-version"}
            with self.assertRaises(LearnerError) as error:
                process_pdf(request, engine_factory=lambda: self.fail("must reject before OCR"))
            self.assertEqual(error.exception.code, "ocr_environment_changed")
            self.assertEqual(checkpoint.read_bytes(), before)

    def test_cached_pages_cannot_bypass_total_text_budget(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            request = self.request(root, pages=33, selected=list(range(1, 34)))
            output = root / "output"
            output.mkdir()
            body = output / "repeated-page.txt"
            body.write_bytes(b"x" * (256 * 1024))
            artifact = {"path": body.name, "sha256": file_sha256(body), "byte_length": body.stat().st_size}
            cached = [{"original_page_no": number, "artifacts": {"text": artifact}} for number in request["pages"]]
            (output / "checkpoint.json").write_text(json.dumps({"fingerprint": request["fingerprint"],
                    "runtime_versions": request["runtime_versions"], "pages": cached}))
            with self.assertRaises(LearnerError) as error:
                process_pdf(request, engine_factory=lambda: self.fail("cached pages must not reload OCR"))
            self.assertEqual(error.exception.code, "ocr_text_budget")


if __name__ == "__main__":
    unittest.main()

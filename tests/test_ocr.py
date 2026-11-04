"""OCR protocol/native-page tests; model quality is measured separately."""
import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from support import ROOT
from codex_learner.common import LearnerError
from codex_learner.ocr import MAX_PIXELS, MAX_SIDE, offline_models, run_ocr
from codex_learner.ocr_worker import process_pdf, render_scale
from codex_learner.storage import file_lock, file_sha256


def text_pdf(path, pages=1):
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
    writer = PdfWriter()
    font = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'),
                             NameObject('/BaseFont'): NameObject('/Helvetica')})
    font_ref = writer._add_object(font)
    for number in range(1, pages + 1):
        page = writer.add_blank_page(width=300, height=200)
        page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): font_ref})})
        stream = DecodedStreamObject()
        stream.set_data(f'BT /F1 18 Tf 20 100 Td (Original page {number}) Tj ET'.encode())
        page[NameObject('/Contents')] = writer._add_object(stream)
    with path.open('wb') as target:
        writer.write(target)


class OcrConfigurationTests(unittest.TestCase):
    def test_render_size_is_bounded_before_raster_allocation(self):
        for width, height in [(595, 842), (10000, 10000), (200, 20000)]:
            scale = render_scale(width, height)
            self.assertLessEqual(width * height * scale ** 2, MAX_PIXELS + 1)
            self.assertLessEqual(max(width, height) * scale, MAX_SIDE + 1)

    def test_models_require_correct_hash_and_cannot_escape_the_model_root(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            model = root / 'model.onnx'
            model.write_bytes(b'explicit fixture, not a real model')
            item = {'path': model.name, 'sha256': file_sha256(model)}
            manifest = {'schema_version': 1, 'provider': 'rapidocr', 'models': {k: dict(item) for k in ('det','rec','cls')}}
            path = root / 'manifest.json'
            path.write_text(json.dumps(manifest))
            self.assertEqual(len(offline_models(root, path)[1]), 3)
            manifest['models']['det']['path'] = '../outside.onnx'
            path.write_text(json.dumps(manifest))
            with self.assertRaises(LearnerError): offline_models(root, path)

    def test_unconfigured_provider_does_not_start_any_process(self):
        with self.assertRaises(LearnerError) as error:
            run_ocr('/unused.pdf', '0' * 64, None)
        self.assertEqual(error.exception.code, 'ocr_unconfigured')

    def test_shared_cache_rejects_a_second_worker_before_writing_request(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with file_lock(root / '.ocr-worker.lock'):
                with self.assertRaises(LearnerError) as error:
                    run_ocr('/unused.pdf', '0' * 64, {'cache_dir': str(root)})
            self.assertEqual(error.exception.code, 'ocr_busy')
            self.assertFalse(error.exception.details['worker_started'])
            self.assertEqual(list(root.glob('*/request.json')), [])

    def test_low_available_memory_reports_budget_without_starting_worker(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            manifest = root / 'manifest.json'
            manifest.write_text('{}')
            config = {'python_executable': sys.executable, 'model_dir': str(root),
                      'model_manifest': str(manifest), 'cache_dir': str(root / 'cache')}
            with patch('codex_learner.ocr.offline_models', return_value=({}, {})), \
                 patch('psutil.virtual_memory', return_value=SimpleNamespace(available=1)), \
                 patch('codex_learner.resource_limits.run_limited') as worker:
                with self.assertRaises(LearnerError) as error:
                    run_ocr('/unused.pdf', '0' * 64, config)
            self.assertEqual(error.exception.code, 'ocr_memory_unavailable')
            self.assertEqual(error.exception.details['available_memory_bytes'], 1)
            self.assertFalse(error.exception.details['worker_started'])
            worker.assert_not_called()

    def test_providers_with_different_caches_share_one_worker_lock(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            shared = root / '.ocr-worker.lock'
            with file_lock(shared):
                with self.assertRaises(LearnerError) as error:
                    run_ocr('/unused.pdf', '0' * 64,
                            {'cache_dir': str(root / 'docling' / 'cache'), 'worker_lock': str(shared)})
            self.assertEqual(error.exception.code, 'ocr_busy')
            self.assertFalse(error.exception.details['worker_started'])


@unittest.skipUnless(importlib.util.find_spec('pypdfium2'), 'Optional pypdfium2 is not installed')
class NativePdfWorkerTests(unittest.TestCase):
    def test_native_text_prefers_original_page_numbers_and_resumes_without_models(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'input.pdf'
            text_pdf(source, 7)
            request = {'source_path': str(source), 'source_sha256': file_sha256(source),
                       'output_dir': str(root / 'output'), 'pages': [3, 7], 'force': False, 'fingerprint': 'test'}
            factory = lambda: self.fail('Native text must not initialize OCR models')
            first = process_pdf(request, engine_factory=factory)
            second = process_pdf(request, engine_factory=factory)
            self.assertEqual([p['original_page_no'] for p in first['pages']], [3, 7])
            self.assertEqual(second['resumed_pages'], 2)
            for page in first['pages']:
                text = (root / 'output' / page['artifacts']['text']['path']).read_text()
                self.assertIn(f"Original page {page['original_page_no']}", text)
            source.rename(root / 'moved.pdf')  # no dangling Windows PDF handle

    def test_out_of_range_page_fails_instead_of_renumbering(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'input.pdf'
            text_pdf(source)
            with self.assertRaises(LearnerError):
                process_pdf({'source_path': str(source), 'source_sha256': file_sha256(source),
                             'output_dir': str(root / 'output'), 'pages': [2], 'fingerprint': 'test'})

    def test_crop_is_rendered_from_original_with_rotation_and_pdf_coordinates(self):
        import numpy as np
        from types import SimpleNamespace
        from pypdf import PdfReader, PdfWriter
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            initial, source = root / 'initial.pdf', root / 'rotated.pdf'
            text_pdf(initial)
            reader = PdfReader(initial)
            page = reader.pages[0]
            page.rotate(90)
            writer = PdfWriter()
            writer.add_page(page)
            with source.open('wb') as stream: writer.write(stream)
            checksum = file_sha256(source)
            def fake_engine(array):
                height, width = array.shape[:2]
                boxes = np.array([[[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]]])
                return SimpleNamespace(txts=('crop fixture',), scores=(1.0,), boxes=boxes)
            request = {'source_path': str(source), 'source_sha256': checksum, 'output_dir': str(root / 'output'),
                       'pages': [1], 'fingerprint': 'crop', 'crop': {'page_no': 1, 'rect': [20, 80, 280, 140], 'dpi': 300}}
            result = process_pdf(request, engine_factory=lambda: fake_engine)
            row = result['pages'][0]
            self.assertEqual(row['geometry']['rotation_degrees'], 90)
            self.assertEqual(row['geometry']['requested_crop_rect_points'], [20, 80, 280, 140])
            data = json.loads((root / 'output' / row['artifacts']['data']['path']).read_text())
            for x, y in data['boxes_pdf_points'][0]:
                self.assertTrue(19 <= x <= 281 and 79 <= y <= 141)
            self.assertEqual(file_sha256(source), checksum)

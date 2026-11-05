"""The documented OCR benchmark works with the installed OCR dependency set."""

import importlib.util
import json
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from support import ROOT


@unittest.skipUnless(importlib.util.find_spec('pypdfium2') and importlib.util.find_spec('PIL'),
                     'Optional PDFium/Pillow OCR dependencies are not installed')
class OcrBenchmarkTests(unittest.TestCase):
    def run_isolated(self, script):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [sys.executable, '-c', textwrap.dedent(script), str(ROOT), directory],
                text=True, capture_output=True, encoding='utf-8', timeout=60,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def test_10_and_100_page_fixtures_need_no_pypdf_and_release_handles(self):
        result = self.run_isolated('''
            import hashlib, importlib.abc, importlib.util, json, sys
            from pathlib import Path

            class RejectPyPdf(importlib.abc.MetaPathFinder):
                def find_spec(self, fullname, path=None, target=None):
                    if fullname == 'pypdf' or fullname.startswith('pypdf.'):
                        raise ModuleNotFoundError('pypdf is absent from the OCR environment')

            sys.meta_path.insert(0, RejectPyPdf())
            root, work = map(Path, sys.argv[1:])
            spec = importlib.util.spec_from_file_location('benchmark', root / 'scripts/benchmark_ocr.py')
            benchmark = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(benchmark)
            import pypdfium2 as pdfium

            source = work / 'base.pdf'
            benchmark.create_fixture(source, 'Dependency regression')
            def page_digest(document, number):
                page = document[number]
                try:
                    bitmap = page.render(scale=0.25)
                    try:
                        return hashlib.sha256(bytes(bitmap.buffer)).hexdigest()
                    finally:
                        bitmap.close()
                finally:
                    page.close()
            with pdfium.PdfDocument(source) as document:
                expected = page_digest(document, 0)
            rows = []
            for count in (10, 100):
                output = work / f'{count}.pdf'
                benchmark.repeated_pdf(source, output, count)
                with pdfium.PdfDocument(output) as document:
                    assert len(document) == count
                    assert page_digest(document, 0) == expected
                    assert page_digest(document, count - 1) == expected
                assert output.stat().st_size <= 32 * 1024 * 1024
                renamed = output.with_suffix('.released.pdf')
                output.rename(renamed)
                renamed.unlink()
                rows.append(count)
            source.rename(work / 'source-released.pdf')
            assert not any(name == 'pypdf' or name.startswith('pypdf.') for name in sys.modules)
            print(json.dumps({'page_counts': rows, 'pypdf_imported': False}))
        ''')
        self.assertEqual(result['page_counts'], [10, 100])
        self.assertFalse(result['pypdf_imported'])

    def test_memory_preflight_failure_is_reported_without_hiding_it_as_dependency_error(self):
        result = self.run_isolated('''
            import importlib.abc, importlib.util, json, sys
            from pathlib import Path
            from unittest.mock import patch

            class RejectPyPdf(importlib.abc.MetaPathFinder):
                def find_spec(self, fullname, path=None, target=None):
                    if fullname == 'pypdf' or fullname.startswith('pypdf.'):
                        raise ModuleNotFoundError('pypdf is absent from the OCR environment')

            sys.meta_path.insert(0, RejectPyPdf())
            root, work = map(Path, sys.argv[1:])
            spec = importlib.util.spec_from_file_location('benchmark', root / 'scripts/benchmark_ocr.py')
            benchmark = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(benchmark)
            from codex_learner.common import LearnerError
            config = work / 'config.json'
            config.write_text(json.dumps({'profile': 'rapidocr', 'memory_limit_mb': 2048}), encoding='utf-8')
            report = work / 'result.json'
            failure = LearnerError('ocr_memory_unavailable', 'Insufficient available physical memory',
                                   worker_started=False)
            args = ['benchmark_ocr.py', '--config', str(config), '--work-dir', str(work / 'fixtures'),
                    '--output', str(report)]
            with patch.object(sys, 'argv', args), patch.object(benchmark, 'run_ocr', side_effect=failure):
                try:
                    benchmark.main()
                except LearnerError as error:
                    assert error is failure
                else:
                    raise AssertionError('Memory failure must propagate')
            saved = json.loads(report.read_text(encoding='utf-8'))
            assert saved['complete'] is False
            assert saved['results'] == []
            assert saved['error']['error'] == 'ocr_memory_unavailable'
            assert saved['error']['worker_started'] is False
            assert json.loads(config.read_text(encoding='utf-8'))['memory_limit_mb'] == 2048
            print(json.dumps({'error': saved['error']['error'], 'budget_unchanged': True}))
        ''')
        self.assertEqual(result['error'], 'ocr_memory_unavailable')
        self.assertTrue(result['budget_unchanged'])


if __name__ == '__main__':
    unittest.main()

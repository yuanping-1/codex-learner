"""Source regressions that keep structure and review state honest."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from support import CourseCase
from codex_learner.common import LearnerError
from codex_learner.sources import (MAX_SOURCE_BYTES, archive_source, check_references,
                                  cleanup_prepared_source, extract, import_options, prepare_source, segment_text)


class StructuralSourceTests(unittest.TestCase):
    def test_html_metadata_never_becomes_verified_lesson_text(self):
        segments, missing = extract(
            b'<!doctype html><?audit ignored?><html><head><title>Metadata title</title>'
            b'<style>body {color:red}</style></head><body><!-- hidden comment -->'
            b'<div>Visible <b>lesson</b></div><p>Second paragraph</p></body></html>', '.html')
        self.assertEqual([segment['text'] for segment in segments], ['Visible lesson', 'Second paragraph'])
        self.assertEqual([segment['locator'] for segment in segments], ['block:1', 'block:2'])
        self.assertFalse(missing)
        with self.assertRaises(LearnerError) as error:
            extract(b'<!doctype html><?audit ignored?><!-- comment -->', '.html')
        self.assertEqual(error.exception.code, 'unreadable_source')

    def test_html_table_and_math_never_flatten_into_verified_prose(self):
        segments, missing = extract(b'<table><tr><th>Distance</th><th>Time</th></tr>'
                                    b'<tr><td>100 m</td><td>20 s</td></tr></table>'
                                    b'<p>Area <math><msup><mi>x</mi><mn>2</mn></msup></math></p>', '.html')
        self.assertIn('Distance | Time', segments[0]['text'])
        self.assertIn('100 m | 20 s', segments[0]['text'])
        self.assertIn('<msup>', segments[1]['text'])
        self.assertTrue(all(not s['verified'] for s in segments))
        self.assertTrue(missing)

    def test_html_mixed_bare_container_text_keeps_document_order(self):
        segments, _ = extract(b'<div>Important <b>first</b></div><p>Second paragraph</p>'
                              b'<figure><figcaption>Caption text</figcaption></figure><div>Important last</div>', '.html')
        self.assertEqual([item['text'] for item in segments],
                         ['Important first', 'Second paragraph', 'Caption text', 'Important last'])

    def test_markdown_external_image_is_a_missing_dependency(self):
        segments, missing = extract('# Circuit\n![diagram](circuit.png)\n'.encode(), '.md')
        image = next(s for s in segments if s['locator'] == 'line:2')
        self.assertFalse(image['verified'])
        self.assertTrue(any('circuit.png' in m for m in missing))

    def test_multiline_formula_bodies_and_unclosed_blocks_require_review(self):
        text = ('Prose\n$$\na + b\n$$\nAfter\n\\[\nx^2\n\\]\n'
                '\\begin{equation}\ny = z\n\\end{equation}\nFinal prose\n$$\nunclosed body\n')
        segments, _ = extract(text.encode(), '.md')
        reviewed = {item['locator']: item['verified'] for item in segments}
        self.assertTrue(all(reviewed[f'line:{n}'] for n in (1, 5, 12)))
        self.assertTrue(all(not reviewed[f'line:{n}'] for n in (2, 3, 4, 6, 7, 8, 9, 10, 11, 13, 14)))

    def test_original_is_copied_without_whole_file_read_bytes(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'source.txt'
            path.write_text('逐字来源\nsecond line', encoding='utf-8')
            with patch.object(Path, 'read_bytes', side_effect=AssertionError('whole file read')):
                prepared = prepare_source({'path': str(path)})
            try:
                self.assertEqual(prepared['_original'].read_text(encoding='utf-8'), '逐字来源\nsecond line')
                self.assertNotIn('data', prepared)
                self.assertNotIn('text', prepared['segments'][0])
                self.assertEqual(prepared['segments'][0]['_text_path'].read_text(encoding='utf-8'), '逐字来源')
                stage = prepared['_stage']
            finally:
                cleanup_prepared_source(prepared)
            self.assertFalse(stage.exists())

    def test_size_limit_precedes_staging_and_parser(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'large.txt'
            with path.open('wb') as stream:
                stream.truncate(MAX_SOURCE_BYTES + 1)
            with self.assertRaises(LearnerError) as error:
                prepare_source({'path': str(path)})
            self.assertEqual(error.exception.code, 'source_too_large')

    def test_structural_parser_has_its_own_preallocation_budget(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'large.html'
            path.write_text('<p>x</p>' * 140000, encoding='utf-8')
            with self.assertRaises(LearnerError) as error:
                prepare_source({'path': str(path), 'url': 'https://example.org/lesson'})
            self.assertEqual(error.exception.code, 'parser_budget')

    def test_profile_aliases_preserve_page_numbers_and_cannot_raise_memory(self):
        config = {'profile': 'rapidocr', 'memory_limit_mb': 1024}
        profile, pages, resume, lowered, crop = import_options(
            {'profile': 'rapidocr', 'page_range': [3, 7], 'memory_budget_mb': 512, 'resume': False}, config)
        self.assertEqual((profile, pages, resume, lowered['memory_limit_mb'], crop),
                         ('rapidocr', [3, 4, 5, 6, 7], False, 512, None))
        self.assertEqual(config['memory_limit_mb'], 1024)
        for request in ({'memory_budget_mb': 2048}, {'page_range': [0, 3]}, {'resume': 'yes'},
                        {'profile': 'native', 'ocr': True}, {'ocr_pages': [1], 'page_numbers': [1]}):
            with self.subTest(request=request), self.assertRaises(LearnerError):
                import_options(request, config)

    def test_configured_pdf_uses_worker_native_output_without_core_reparse(self):
        with tempfile.TemporaryDirectory() as folder:
            path, text = Path(folder) / 'source.pdf', Path(folder) / 'page.txt'
            path.write_bytes(b'fixture delegated to the worker')
            text.write_text('native page text', encoding='utf-8')
            worker_result = {'run': {'provider': 'rapidocr'}, 'pages': [
                {'original_page_no': 1, 'method': 'native_pdf', 'status': 'pending_review', 'text_path': str(text)}]}
            with patch('codex_learner.ocr.run_ocr', return_value=worker_result) as worker, \
                 patch('codex_learner.sources.iter_segments', side_effect=AssertionError('duplicate parse')):
                configured = {'profile': 'rapidocr', **{key: '/fixture' for key in
                              ('python_executable', 'model_dir', 'model_manifest', 'cache_dir')}}
                prepared = prepare_source({'path': str(path)}, configured)
            try:
                self.assertEqual(prepared['segments'][0]['_text_path'].read_text(encoding='utf-8'), 'native page text')
                self.assertFalse(prepared['segments'][0]['verified'])
                worker.assert_called_once()
            finally:
                cleanup_prepared_source(prepared)

    def test_base_install_ocr_defaults_keep_pdf_auto_on_native_reader(self):
        from pypdf import PdfWriter
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'source.pdf'
            writer = PdfWriter()
            writer.add_blank_page(width=300, height=200)
            with path.open('wb') as output:
                writer.write(output)
            defaults = {'profile': 'rapidocr', 'memory_limit_mb': 2048, 'timeout_seconds': 300}
            with patch('codex_learner.ocr.run_ocr', side_effect=AssertionError('unconfigured provider started')):
                prepared = prepare_source({'path': str(path)}, defaults)
            try:
                self.assertEqual(prepared['reading_scope']['profile'], 'native')
                self.assertEqual(prepared['segments'][0]['extraction_method'], 'native_pdf')
            finally:
                cleanup_prepared_source(prepared)


class SourceReferenceTests(CourseCase):
    def test_stored_segments_are_references_and_corruption_is_not_accepted(self):
        source = self.library.load('sample-course')['sources']['sample-source-v1']
        segment = next(s for s in source['segments'] if s['locator'] == 'line:2')
        self.assertIn('text_ref', segment)
        self.assertNotIn('text', dict(segment))
        directory = self.library.course_dir('sample-course')
        self.assertIn('平均速率', segment_text(segment, directory))
        body = directory / segment['text_ref']['path']
        body.write_text('changed', encoding='utf-8')
        with self.assertRaises(LearnerError):
            check_references(self.library.load('sample-course'), self.refs, directory)

    def test_legacy_claim_cannot_support_new_content_without_explicit_review(self):
        state = self.library.load('sample-course')
        source = state['sources']['sample-source-v1']
        segment = next(s for s in source['segments'] if s['locator'] == 'line:2')
        segment.update(legacy_claim=True, legacy_verified=True, verified=False)
        with self.assertRaises(LearnerError):
            check_references(state, self.refs, self.library.course_dir('sample-course'))

    def test_unknown_source_id_does_not_leave_a_temporary_import(self):
        path = self.root / 'import.txt'
        path.write_text('new material', encoding='utf-8')
        directories = []
        original_factory = tempfile.TemporaryDirectory
        def factory(*args, **kwargs):
            result = original_factory(*args, **kwargs)
            directories.append(Path(result.name))
            return result
        with patch('codex_learner.sources.tempfile.TemporaryDirectory', side_effect=factory):
            with self.assertRaises(LearnerError):
                self.apply('import_source', source_id='sample-source-v1', path=str(path))
            self.assertTrue(directories)
            self.assertTrue(all(not directory.exists() for directory in directories))

import hashlib
import io
import zipfile
from pathlib import Path

from support import CourseCase
from codex_learner.common import LearnerError
from codex_learner.sources import extract
from create_demo import sample_requests


class SourceTests(CourseCase):
    def test_e05_archived_source_survives_original_removal_and_retry(self):
        original = self.root / "original.txt"
        original.write_text("原文第一行\n第二行", encoding="utf-8")
        request = self.request("import_source", source_id="independent", path=str(original))
        self.engine.apply(request)
        original.rename(self.root / "moved.txt")
        source = self.library.load("sample-course")["sources"]["independent"]
        archived = self.library.root / "sample-course" / source["path"]
        self.assertEqual(archived.read_text(encoding="utf-8"), "原文第一行\n第二行")
        self.assertEqual(hashlib.sha256(archived.read_bytes()).hexdigest(), source["sha256"])
        self.assertTrue(self.engine.apply(request)["duplicate"])

    def test_e04_docx_preserves_paragraph_table_locations_and_reports_drawing(self):
        stream = io.BytesIO()
        xml = '''<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
        <w:body><w:p><w:r><w:t>第一段</w:t></w:r></w:p>
        <w:tbl><w:tr><w:tc><w:p><w:r><w:t>表格</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
        <w:p><w:r><w:t>有图</w:t><w:drawing/></w:r></w:p></w:body></w:document>'''
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("word/document.xml", xml)
        segments, missing = extract(stream.getvalue(), ".docx")
        self.assertEqual([s["locator"] for s in segments], ["paragraph:1", "table:1", "paragraph:2"])
        self.assertEqual(segments[1]["text"], "表格")
        self.assertFalse(segments[2]["verified"])
        self.assertTrue(missing)

    def test_e04_archived_webpage_excludes_executable_text_and_lists_missing_images(self):
        path = self.root / "web.html"
        path.write_text('<h1>标题</h1><p>正文</p><script>steal()</script><img src="remote.png">', encoding="utf-8")
        self.apply("import_source", source_id="web", path=str(path), url="https://example.org/lesson")
        source = self.library.load("sample-course")["sources"]["web"]
        self.assertEqual(source["url"], "https://example.org/lesson")
        self.assertNotIn("steal", "".join(s["text"] for s in source["segments"]))
        self.assertTrue(source["missing"])

    def test_e04_pdf_without_readable_text_is_not_verified_teaching_material(self):
        # 无第三方生成器的最小有效空白 PDF；不以空提取结果冒充读取成功。
        objects = [b"<< /Type /Catalog /Pages 2 0 R >>",
                   b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                   b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] /Resources << >> >>"]
        data, offsets = b"%PDF-1.4\n", [0]
        for i, obj in enumerate(objects, 1):
            offsets.append(len(data))
            data += str(i).encode() + b" 0 obj\n" + obj + b"\nendobj\n"
        offset = len(data)
        data += b"xref\n0 4\n0000000000 65535 f \n"
        data += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets[1:])
        data += f"trailer\n<< /Size 4 /Root 1 0 R >>\nstartxref\n{offset}\n%%EOF".encode()
        segments, missing = extract(data, ".pdf")
        self.assertEqual(segments[0]["locator"], "page:1")
        self.assertFalse(segments[0]["verified"])
        self.assertIn("OCR", missing[0])

    def test_unverified_rubric_and_missing_references_cannot_publish(self):
        request = next(r for r in sample_requests() if r.get("question_id") == "q1")
        request.update(question_id="bad", operation_id="bad")
        request["rubric"]["verified"] = False
        with self.assertRaises(LearnerError):
            self.engine.apply(request)
        request["rubric"]["verified"] = True
        request["rubric"]["source_refs"][0]["locator"] = "missing"
        with self.assertRaises(LearnerError):
            self.engine.apply(request)
        self.assertNotIn("bad", self.library.load("sample-course")["questions"])

    def test_unsupported_format_does_not_publish_partial_reference(self):
        path = self.root / "legacy.doc"
        path.write_bytes(b"legacy")
        with self.assertRaises(LearnerError):
            self.apply("import_source", source_id="unsupported", path=str(path))
        self.assertNotIn("unsupported", self.library.load("sample-course")["sources"])

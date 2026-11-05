import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from create_recheck_task import create


class RecheckTaskTests(unittest.TestCase):
    def package(self, directory, *, corrupt=False):
        files = {"BUILD.json": json.dumps({"commit": "a" * 40}).encode(),
                 "docs/performance-recheck-v0.2.md": "# 隔离验收\n保留正式课程。\n".encode()}
        manifest = {name: hashlib.sha256(value).hexdigest() for name, value in files.items()}
        if corrupt:
            files["docs/performance-recheck-v0.2.md"] += b"tampered"
        package = directory / "candidate.zip"
        with zipfile.ZipFile(package, "w") as archive:
            for name, value in files.items():
                archive.writestr("codex-learner/" + name, value)
            archive.writestr("codex-learner/MANIFEST.sha256.json", json.dumps(manifest))
        return package

    def test_task_binds_actual_zip_and_preserves_packaged_instructions(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            package = self.package(root)
            output = root / "recheck.md"
            result = create(package, output)
            self.assertEqual(result["package_sha256"], hashlib.sha256(package.read_bytes()).hexdigest())
            text = output.read_text(encoding="utf-8")
            self.assertIn(result["package_sha256"], text)
            self.assertIn("a" * 40, text)
            self.assertTrue(text.endswith("# 隔离验收\n保留正式课程。\n"))
            self.assertEqual(create(package, output), result)

    def test_corruption_does_not_publish_a_task(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            package = self.package(root, corrupt=True)
            output = root / "recheck.md"
            with self.assertRaises(ValueError):
                create(package, output)
            self.assertFalse(output.exists())

    def test_different_existing_task_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            package = self.package(root)
            output = root / "recheck.md"
            output.write_bytes(b"old evidence")
            with self.assertRaises(ValueError):
                create(package, output)
            self.assertEqual(output.read_bytes(), b"old evidence")

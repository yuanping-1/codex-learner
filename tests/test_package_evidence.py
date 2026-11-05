"""Complete deterministic evidence bundles, with explicit input boundaries."""

import hashlib
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import package_evidence


class EvidencePackageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = self.root / "repo"
        for name in package_evidence.REQUIRED:
            self.write(name, "synthetic evidence " + name + "\n")
        records = []
        for name in package_evidence.SNAPSHOT_RECORDS:
            path = self.repo / (package_evidence.SNAPSHOT + name)
            records.append({"snapshot_path": name, "source_path": "historical/" + name,
                            "bytes": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        self.write(package_evidence.SNAPSHOT + "evidence-manifest.json", json.dumps({
            "source_commit": "a" * 40, "package_sha256": "b" * 64,
            "revised_user_native_acceptance": "PENDING", "full_delivery": "PARTIAL", "files": records,
        }))

    def write(self, name, text):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_reproducible_bundle_is_complete_and_excludes_unlisted_material(self):
        for name in (package_evidence.SNAPSHOT + "course.json", package_evidence.SNAPSHOT + "credentials.json",
                     "docs/evidence/v0.2/private-result.json", "models/model.onnx", "artifacts/unknown.json"):
            self.write(name, "must never be bundled")
        self.write("docs/evidence/v0.2/performance-comparison.json", '{"scope":"synthetic timing"}\n')
        with patch.object(package_evidence, "ROOT", self.repo), \
                patch.object(Path, "read_bytes", side_effect=AssertionError("unbounded source read")):
            one = package_evidence.build(self.root / "one.zip")
            two = package_evidence.build(self.root / "two.zip")
        self.assertEqual(one["sha256"], two["sha256"])
        self.assertEqual(one["files"], len(package_evidence.REQUIRED) + 3)
        with zipfile.ZipFile(one["path"]) as archive:
            manifest = json.loads(archive.read("codex-learner/EVIDENCE_MANIFEST.sha256.json"))
            self.assertEqual(set(manifest) - {"EVIDENCE_BUILD.json"},
                             set(package_evidence.REQUIRED) | {"docs/evidence/v0.2/performance-comparison.json"})
            self.assertIn("docs/local-acceptance-handoff-v0.2.md", manifest)
            self.assertEqual(set(archive.namelist()), {"codex-learner/" + name for name in manifest}
                             | {"codex-learner/EVIDENCE_MANIFEST.sha256.json"})
            for name, checksum in manifest.items():
                self.assertEqual(hashlib.sha256(archive.read("codex-learner/" + name)).hexdigest(), checksum)
            metadata = json.loads(archive.read("codex-learner/EVIDENCE_BUILD.json"))
            self.assertEqual(metadata["historical_user_native_acceptance"], "PENDING")
            self.assertFalse(metadata["product_zip_included"])
            self.assertIn("docs/evidence/v0.2/performance-profile.json", metadata["optional_files_absent"])
        self.assertTrue(Path(one["path"] + ".sha256").read_text().startswith(one["sha256"] + "  "))

    def test_missing_or_altered_historical_record_preserves_previous_output(self):
        output = self.root / "existing.zip"
        output.write_bytes(b"older complete attachment")
        path = self.repo / (package_evidence.SNAPSHOT + package_evidence.SNAPSHOT_RECORDS[0])
        path.write_text("changed evidence", encoding="utf-8")
        with patch.object(package_evidence, "ROOT", self.repo):
            with self.assertRaisesRegex(ValueError, "frozen manifest"):
                package_evidence.build(output)
            path.unlink()
            with self.assertRaisesRegex(ValueError, "missing"):
                package_evidence.build(output)
        self.assertEqual(output.read_bytes(), b"older complete attachment")
        self.assertFalse(list(self.root.glob(".evidence-*")))

    def test_symlink_ancestor_is_rejected_even_for_optional_source(self):
        ancestor = self.repo / "docs/evidence/v0.2"
        original = Path.is_symlink
        with patch.object(package_evidence, "ROOT", self.repo), \
                patch.object(Path, "is_symlink", lambda path: path == ancestor or original(path)):
            with self.assertRaisesRegex(ValueError, "symbolic link"):
                package_evidence.build(self.root / "evidence.zip")

    def test_source_changed_after_hash_does_not_replace_previous_attachment(self):
        output = self.root / "existing.zip"
        output.write_bytes(b"older complete attachment")
        original = package_evidence.snapshot_metadata

        def validate_then_change(members, hashes):
            result = original(members, hashes)
            members["docs/implementation.md"].write_text("concurrent edit", encoding="utf-8")
            return result

        with patch.object(package_evidence, "ROOT", self.repo), \
                patch.object(package_evidence, "snapshot_metadata", side_effect=validate_then_change):
            with self.assertRaisesRegex(ValueError, "changed during packaging"):
                package_evidence.build(output)
        self.assertEqual(output.read_bytes(), b"older complete attachment")
        self.assertFalse(list(self.root.glob(".evidence-*")))


if __name__ == "__main__":
    unittest.main()

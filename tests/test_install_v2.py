"""Upgrade preservation, isolated OCR setup and reproducible distribution checks."""

import hashlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "skills/codex-learner/scripts"))
import install_ocr
import install_skill
import package
from codex_learner.common import LearnerError


class InstallV2Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.skill = self.root / "user/.agents/skills/codex-learner"
        self.library = self.root / "courses"

    def config(self):
        return json.loads((self.skill / "config.json").read_text(encoding="utf-8"))

    def save_config(self, config):
        (self.skill / "config.json").write_text(json.dumps(config), encoding="utf-8")

    def test_upgrade_preserves_all_settings_and_legacy_data_without_migration(self):
        install_skill.install(self.skill, self.library)
        config = self.config()
        config.update(python_executable="C:/Existing/Python/python.exe",
                      ocr={"python_executable": "/outside/ocr/python", "memory_limit_mb": 768},
                      drive={"directory": "/existing/private-drive"},
                      extension={"keep": ["未知配置", 1]})
        self.save_config(config)
        course = self.library / "legacy"
        course.mkdir()
        original = b'{"schema_version":1,"unrecognized":"leave entirely unchanged"}'
        (course / "course.json").write_bytes(original)
        # An older code file forces the ordinary deliberate upgrade path.
        (self.skill / "scripts/learner.py").write_text("# previous version\n", encoding="utf-8")
        upgraded = install_skill.install(self.skill, backup_and_replace=True)
        self.assertTrue(upgraded["changed"])
        self.assertEqual(self.config(), config)
        self.assertEqual((course / "course.json").read_bytes(), original)
        self.assertEqual(json.loads((Path(upgraded["backup"]) / "config.json").read_text(encoding="utf-8")), config)
        before = (self.skill / "config.json").stat().st_mtime_ns
        self.assertFalse(install_skill.install(self.skill)["changed"])
        self.assertEqual((self.skill / "config.json").stat().st_mtime_ns, before)

    def test_new_install_requires_library_and_upgrade_cannot_silently_switch(self):
        with self.assertRaises(LearnerError):
            install_skill.install(self.skill)
        self.assertFalse(self.skill.exists())
        install_skill.install(self.skill, self.library)
        with self.assertRaises(LearnerError):
            install_skill.install(self.skill, self.root / "other-library", backup_and_replace=True)
        self.assertFalse((self.root / "other-library").exists())
        self.assertEqual(Path(self.config()["library_root"]), self.library.resolve())

    def test_failed_upgrade_restores_existing_skill(self):
        install_skill.install(self.skill, self.library)
        old_file = self.skill / "custom.txt"
        old_file.write_text("preserve me", encoding="utf-8")
        original_rename = Path.rename

        def failing_stage(path, target):
            if path.name.startswith(".codex-learner-install-"):
                raise OSError("controlled rename failure")
            return original_rename(path, target)

        with patch.object(Path, "rename", failing_stage):
            with self.assertRaises(OSError):
                install_skill.install(self.skill, backup_and_replace=True)
        self.assertEqual(old_file.read_text(encoding="utf-8"), "preserve me")
        self.assertFalse(list(self.skill.parent.glob(".codex-learner-install-*")))

    def test_damaged_manifest_requires_deliberate_replacement(self):
        install_skill.install(self.skill, self.library)
        (self.skill / "install-manifest.json").write_text("{}", encoding="utf-8")
        with self.assertRaises(LearnerError):
            install_skill.install(self.skill)
        result = install_skill.install(self.skill, backup_and_replace=True)
        self.assertTrue(result["changed"])
        manifest = json.loads((self.skill / "install-manifest.json").read_text(encoding="utf-8"))
        self.assertTrue(manifest["files"])
        self.assertIn("scripts/learner.py", manifest["files"])

    def test_ocr_preview_keeps_budget_and_has_no_side_effects(self):
        install_skill.install(self.skill, self.library)
        config = self.config()
        config["ocr"]["memory_limit_mb"] = 768
        self.save_config(config)
        runtime = self.root / "runtime"
        with patch.object(install_ocr.subprocess, "run", side_effect=AssertionError("preview must not execute")):
            result = install_ocr.setup_ocr(self.skill, runtime)
        self.assertEqual(result["memory_limit_mb"], 768)
        self.assertFalse(runtime.exists())
        self.assertEqual(self.config(), config)

    def test_ocr_rejects_skill_library_drive_and_over_budget_locations(self):
        install_skill.install(self.skill, self.library)
        config = self.config()
        config["drive"] = {"directory": str(self.root / "Drive")}
        self.save_config(config)
        for runtime in (self.skill / "ocr", self.library / "ocr", self.root / "Drive/ocr", self.root):
            with self.subTest(runtime=runtime), self.assertRaises(LearnerError):
                install_ocr.setup_ocr(self.skill, runtime)
        with self.assertRaises(LearnerError):
            install_ocr.setup_ocr(self.skill, self.root / "runtime", memory_limit_mb=4096)

    def test_failed_dependency_install_does_not_activate_or_overwrite_config(self):
        install_skill.install(self.skill, self.library)
        original = self.config()
        runtime = self.root / "runtime"

        def execute(command, **kwargs):
            if "-c" in command:
                return subprocess.CompletedProcess(command, 0, stdout="3.12\n")
            if "venv" in command:
                executable = runtime / "rapidocr/venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
                executable.parent.mkdir(parents=True)
                executable.write_bytes(b"fake isolated Python")
                return subprocess.CompletedProcess(command, 0)
            raise subprocess.CalledProcessError(1, command)

        with patch.object(install_ocr.subprocess, "run", side_effect=execute):
            with self.assertRaises(subprocess.CalledProcessError):
                install_ocr.setup_ocr(self.skill, runtime, install=True)
        self.assertEqual(self.config(), original)
        marker = json.loads((runtime / "rapidocr/environment.json").read_text(encoding="utf-8"))
        self.assertFalse(marker["ready"])
        self.assertFalse((runtime / "rapidocr/models").exists())

    def test_explicit_environment_install_preserves_core_settings_without_prefetch(self):
        install_skill.install(self.skill, self.library)
        config = self.config()
        config["drive"] = {"directory": str(self.root / "Drive")}
        config["ocr"]["memory_limit_mb"] = 1024
        self.save_config(config)
        runtime = self.root / "runtime"

        def execute(command, **kwargs):
            if "-c" in command:
                return subprocess.CompletedProcess(command, 0, stdout="3.12\n")
            if "venv" in command:
                executable = runtime / "rapidocr/venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
                executable.parent.mkdir(parents=True)
                executable.write_bytes(b"fake isolated Python")
            return subprocess.CompletedProcess(command, 0)

        with patch.object(install_ocr.subprocess, "run", side_effect=execute), \
                patch.object(install_ocr, "prefetch_models", side_effect=AssertionError("implicit model download")):
            result = install_ocr.setup_ocr(self.skill, runtime, install=True)
        after = self.config()
        self.assertTrue(result["installed"])
        self.assertFalse(result["prefetched"])
        self.assertEqual(after["python_executable"], config["python_executable"])
        self.assertEqual(after["drive"], config["drive"])
        self.assertEqual(after["ocr"]["memory_limit_mb"], 1024)
        self.assertEqual(after["ocr"]["worker_lock"], str(runtime / ".ocr-worker.lock"))
        self.assertTrue(Path(after["ocr"]["python_executable"]).is_file())
        self.assertFalse(Path(after["ocr"]["model_manifest"]).exists())

    def test_prefetch_verifies_hash_and_retains_old_model_on_failure(self):
        good = b"small fixture only, not an OCR model"
        manifest = {"schema_version": 1, "provider": "rapidocr", "model_version": "test",
                    "models": {key: {"path": key + ".onnx", "sha256": hashlib.sha256(good).hexdigest(),
                                     "url": "https://example.invalid/" + key}
                               for key in ("det", "rec", "cls")}}
        models = self.root / "models"
        result = install_ocr.prefetch_models(manifest, models, opener=lambda *args, **kwargs: io.BytesIO(good))
        self.assertEqual(set(result["downloaded"]), {"det", "rec", "cls"})
        with patch.object(install_ocr, "sha256", return_value="0" * 64):
            with self.assertRaises(LearnerError):
                install_ocr.prefetch_models(manifest, models, opener=lambda *args, **kwargs: io.BytesIO(b"broken"))
        self.assertEqual((models / "det.onnx").read_bytes(), good)
        self.assertFalse(list(models.glob(".model-*")))
        self.assertEqual(json.loads((models / "models.json").read_text())["models"]["det"]["sha256"],
                         hashlib.sha256(good).hexdigest())

    def test_prefetch_rejects_path_traversal_before_network(self):
        manifest = {"schema_version": 1, "provider": "docling", "models": {
            "model": {"path": "../outside", "sha256": "0" * 64, "url": "https://example.invalid/model"}}}
        with self.assertRaises(LearnerError):
            install_ocr.prefetch_models(manifest, self.root / "models", opener=lambda *a, **k: self.fail("network"))
        self.assertFalse((self.root / "outside").exists())


class PackageV2Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.write("skills/codex-learner/scripts/codex_learner/__init__.py", '__version__ = "0.2.0"\n')
        self.write("skills/codex-learner/SKILL.md", "source\n")
        self.write("README.md", "read me\n")
        self.write("scripts/example.py", "print('example')\n")

    def write(self, name, value):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")
        return path

    def test_package_is_streamed_reproducible_and_excludes_private_data(self):
        for name in ("skills/codex-learner/config.json", "skills/codex-learner/models/model.onnx",
                     "examples/personal/course.json", "examples/credentials.json", "scripts/.env",
                     "scripts/token.json", "artifacts/verification.json", "tests/cache/private.py"):
            self.write(name, "private material must be absent")
        with patch.object(package, "ROOT", self.repo), patch.object(Path, "read_bytes", side_effect=AssertionError("unbounded read")):
            first = package.build(self.root / "one.zip")
            second = package.build(self.root / "two.zip")
        self.assertEqual(first["sha256"], second["sha256"])
        with zipfile.ZipFile(first["path"]) as archive:
            names = archive.namelist()
            self.assertFalse(any(b"private material" in archive.read(name) for name in names))
            manifest = json.loads(archive.read("codex-learner/MANIFEST.sha256.json"))
            for name, expected in manifest.items():
                self.assertEqual(hashlib.sha256(archive.read("codex-learner/" + name)).hexdigest(), expected)
            build = json.loads(archive.read("codex-learner/BUILD.json"))
            self.assertEqual(build["version"], "0.2.0")
            self.assertEqual(build["read_schemas"], [1, 2])
            self.assertEqual(build["write_schema"], 2)
            self.assertEqual(build["protocol"], "codex-learner-course")

    def test_concurrent_edit_fails_without_replacing_previous_package(self):
        output = self.root / "release.zip"
        output.write_bytes(b"previous package")
        with patch.object(package, "ROOT", self.repo), patch.object(package, "file_hash", return_value="0" * 64):
            with self.assertRaisesRegex(ValueError, "Source changed"):
                package.build(output)
        self.assertEqual(output.read_bytes(), b"previous package")
        self.assertFalse(list(self.root.glob(".package-*")))


if __name__ == "__main__":
    unittest.main()

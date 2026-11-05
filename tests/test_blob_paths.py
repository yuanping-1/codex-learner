"""Portable artifact paths retain containment without a second full resolve."""

import hashlib
import os
from pathlib import Path
import stat
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import support  # Put the local product and scripts on sys.path.
from codex_learner import blobs
from codex_learner.common import LearnerError


class ArtifactPathTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "course"
        self.root.mkdir()

    def ref(self, relative, data=b"unchanged"):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return {"path": relative, "sha256": hashlib.sha256(data).hexdigest(),
                "byte_length": len(data)}

    def rejected(self, relative):
        with self.assertRaises(LearnerError) as error:
            blobs.artifact_path(self.root, relative)
        self.assertEqual(error.exception.code, "invalid_path")

    def test_rejects_navigation_and_windows_alias_spellings_before_normalization(self):
        for relative in ("", ".", "..", "./body", "body/./file", "body/../file",
                         "body//file", "body/", "/outside", "//server/share",
                         "C:outside", "C:/outside", "body\\file", "body/file:stream",
                         ".. /outside", "body./file", "body /file", "body/file.",
                         "body/file ", "NUL", "body/CON.txt", "body/COM1", "body/LPT9.dat",
                         "body/COM\u00b9.txt", "body/CONIN$", "body/CONOUT$"):
            with self.subTest(relative=relative):
                self.rejected(relative)

    def test_missing_tail_is_allowed_for_writes_but_not_reads(self):
        relative = "new/deep/body.txt"
        self.assertEqual(blobs.artifact_path(self.root, relative), self.root.resolve() / relative)
        ref = {"path": relative, "sha256": hashlib.sha256(b"body").hexdigest(), "byte_length": 4}
        with self.assertRaises(LearnerError) as error:
            blobs.verify_ref(self.root, ref)
        self.assertEqual(error.exception.code, "artifact_missing")
        ref = blobs.write_body(self.root, "\u539f\u7b54\r\n\U0001f600 e\u0301  \n")
        self.assertEqual(blobs.read_body(self.root, ref), "\u539f\u7b54\r\n\U0001f600 e\u0301  \n")

    def test_regular_unicode_names_and_hidden_files_remain_readable(self):
        ref = self.ref("\u6765\u6e90/..note/.\u7b54\u6848.txt", "\u539f\u7b54\r\n\U0001f600  \n".encode())
        self.assertEqual(blobs.verify_ref(self.root, ref), self.root.resolve() / ref["path"])
        self.assertEqual(blobs.read_body(self.root, ref), "\u539f\u7b54\r\n\U0001f600  \n")

    def test_directory_and_non_directory_ancestors_are_not_read_as_bodies(self):
        self.ref("file")
        (self.root / "directory").mkdir()
        for relative in ("directory", "file/child"):
            with self.subTest(relative=relative), self.assertRaises(LearnerError) as error:
                blobs.verify_ref(self.root, {"path": relative, "sha256": "0" * 64})
            self.assertEqual(error.exception.code, "artifact_missing")

    def test_internal_external_and_dangling_symbolic_links_are_rejected(self):
        ref = self.ref("original/file")
        external = self.root.parent / "outside"
        external.mkdir()
        (external / "file").write_bytes(b"unchanged")
        for name, target in (("internal", self.root / "original"), ("external", external),
                             ("dangling", self.root.parent / "missing")):
            link = self.root / name
            try:
                link.symlink_to(target, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"Directory symlinks unavailable: {exc}")
            with self.subTest(name=name):
                self.rejected(name + "/file")
        # A linked leaf is rejected even when its content and hash are correct.
        (self.root / "linked-file").symlink_to(self.root / ref["path"])
        self.rejected("linked-file")

    def test_reparse_ancestor_and_leaf_are_rejected_without_following_children(self):
        self.ref("parent/body")
        original_lstat = Path.lstat
        for target in (self.root / "parent", self.root / "parent/body"):
            visited = []

            def observe(path, *args, **kwargs):
                visited.append(path)
                if path == target:
                    return SimpleNamespace(st_mode=stat.S_IFDIR,
                                           st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
                return original_lstat(path, *args, **kwargs)

            with self.subTest(target=target), mock.patch.object(Path, "lstat", observe):
                self.rejected("parent/body")
            self.assertEqual(visited[-1], target)

    @unittest.skipUnless(os.name == "nt", "Windows directory junction semantics")
    def test_real_windows_junctions_inside_and_outside_root_are_rejected(self):
        self.ref("inside/body")
        outside = self.root.parent / "outside"
        outside.mkdir()
        (outside / "body").write_bytes(b"unchanged")
        for name, target in (("junction-inside", self.root / "inside"), ("junction-outside", outside)):
            link = self.root / name
            run = subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
                                 capture_output=True, text=True, errors="replace")
            if run.returncode:
                self.skipTest(f"Directory junction creation unavailable: {run.stdout} {run.stderr}")
            try:
                self.assertTrue(link.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
                self.rejected(name + "/body")
            finally:
                os.rmdir(link)

    def test_root_and_ancestors_are_rechecked_for_each_reference(self):
        self.ref("parent/body")
        original_resolve, original_lstat = Path.resolve, Path.lstat
        roots, ancestors = [], []
        changed = False

        def resolve(path, *args, **kwargs):
            roots.append(path)
            return original_resolve(path, *args, **kwargs)

        def lstat(path, *args, **kwargs):
            ancestors.append(path)
            if changed and path == self.root / "parent":
                return SimpleNamespace(st_mode=stat.S_IFDIR,
                                       st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
            return original_lstat(path, *args, **kwargs)

        with mock.patch.object(Path, "resolve", resolve), mock.patch.object(Path, "lstat", lstat):
            blobs.artifact_path(self.root, "parent/body")
            changed = True
            self.rejected("parent/body")
        self.assertEqual(roots, [self.root, self.root])
        self.assertEqual(ancestors.count(self.root / "parent"), 2)

    def test_metadata_failure_is_propagated_without_treating_the_path_as_safe(self):
        denied = PermissionError("cannot inspect artifact path")
        with mock.patch.object(Path, "lstat", side_effect=denied), self.assertRaises(PermissionError) as error:
            blobs.artifact_path(self.root, "parent/body")
        self.assertIs(error.exception, denied)


if __name__ == "__main__":
    unittest.main()

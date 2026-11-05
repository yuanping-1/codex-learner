"""Evidence for compact manifests, exact bodies and explicit legacy conversion."""

import copy
import hashlib
import json
from pathlib import Path
from unittest import mock

from support import CourseCase
from codex_learner import blobs
from codex_learner.common import LearnerError, canonical
from codex_learner.migration import migrate_course, preview_migration
from codex_learner.views import course_summary


class V2StorageTests(CourseCase):
    @property
    def directory(self):
        return self.library.course_dir("sample-course")

    def manifest(self):
        return json.loads((self.directory / "course.json").read_bytes())

    def legacy_fixture(self, answer=" \t旧版中文 e\u0301 😀\r\n第二行\r末行\n "):
        """Expand the current record shapes into the original schema-1 representation."""
        self.apply("record_help", **self.context, event_id="legacy-hint", level="hint", source="user_request")
        request = self.submit(sid="legacy-answer", answer=answer)
        self.assess("legacy-answer", assessment_id="legacy-assessment", feedback="旧反馈\r\n完整保留")
        state = blobs.materialize(self.library.load("sample-course"))
        state["schema_version"] = 1
        state.pop("source_verifications", None)
        previous_path = self.directory / "course.previous.json"
        previous = blobs.materialize(blobs.lazy(json.loads(previous_path.read_bytes()), self.directory))
        previous["schema_version"] = 1
        previous.pop("source_verifications", None)
        original_bytes = (json.dumps(state, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        previous_bytes = (json.dumps(previous, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        (self.directory / "course.json").write_bytes(original_bytes)
        previous_path.write_bytes(previous_bytes)
        return state, original_bytes, previous_bytes, request

    def test_manifest_externalizes_bodies_and_receipts_share_exact_answer(self):
        answer = " \t中文 e\u0301 😀\r\n第二行\r第三行\n\\literal\\n\n "
        request = self.submit(sid="exact-answer", answer=answer)
        self.assess("exact-answer", feedback="反馈\r\n保持原样")
        manifest = self.manifest()
        self.assertEqual(manifest["schema_version"], 2)

        def check_compact(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if (key in blobs.BODY_FIELDS and item is not None) or key == "receipt":
                        self.fail(f"Manifest contains inline body field: {key}")
                    check_compact(item)
            elif isinstance(value, list):
                for item in value:
                    check_compact(item)

        check_compact(manifest)
        answer_ref = manifest["submissions"]["exact-answer"]["answer_ref"]
        receipt_ref = manifest["operations"][request["operation_id"]]["receipt_ref"]
        raw_receipt = json.loads((self.directory / receipt_ref["path"]).read_bytes())
        self.assertEqual(raw_receipt["result"]["answer_ref"], answer_ref)
        self.assertNotIn("answer", raw_receipt["result"])
        self.assertEqual((self.directory / answer_ref["path"]).read_bytes(), answer.encode("utf-8"))
        self.assertEqual(answer_ref["sha256"], hashlib.sha256(answer.encode("utf-8")).hexdigest())
        self.assertEqual(self.library.load("sample-course")["submissions"]["exact-answer"]["answer"], answer)
        self.assertEqual(self.engine.apply(request)["result"]["answer"], answer)
        self.submit(sid="same-text-new-submission", answer=answer, replaces_submission_id="exact-answer")
        self.assertEqual(self.manifest()["submissions"]["same-text-new-submission"]["answer_ref"], answer_ref)

    def test_loading_copying_serializing_and_summaries_do_not_read_history_bodies(self):
        self.submit(sid="historical", answer="历史原答" * 1000)
        self.assess("historical", feedback="长反馈" * 1000)
        with mock.patch.object(blobs, "read_body", side_effect=AssertionError("Unexpected body hydration")):
            state = self.library.load("sample-course")
            copied = copy.deepcopy(state)
            encoded = canonical(copied)
            summary = course_summary(copied)
            courses = self.library.list_courses()
        self.assertIn("answer_ref", encoded)
        self.assertNotIn("历史原答", encoded)
        self.assertEqual(summary["chapters"][0]["goals"][0]["checks"][0]["status"], "passed")
        self.assertEqual(courses[0]["course_id"], "sample-course")

    def test_stable_id_retry_keeps_manifest_and_conflict_preserves_original(self):
        request = self.submit(sid="stable-v2", answer="第一次的完整原答\r\n")
        before = (self.directory / "course.json").read_bytes()
        paths = {str(path.relative_to(self.directory)) for path in self.directory.rglob("*") if path.is_file()}
        self.assertTrue(self.engine.apply(request)["duplicate"])
        self.assertEqual((self.directory / "course.json").read_bytes(), before)
        self.assertEqual({str(path.relative_to(self.directory)) for path in self.directory.rglob("*")
                          if path.is_file()}, paths)
        with self.assertRaises(LearnerError) as error:
            self.engine.apply({**request, "answer": "修改后不能冒充相同请求"})
        self.assertEqual(error.exception.code, "id_conflict")
        self.assertEqual((self.directory / "course.json").read_bytes(), before)

    def damaged_body_is_rejected(self, missing):
        self.submit(sid="damaged-body", answer="不可丢失的原答")
        self.assess("damaged-body")  # Both current and previous manifests now reference the answer.
        info = self.library.backup_info("sample-course")
        ref = self.manifest()["submissions"]["damaged-body"]["answer_ref"]
        path = self.directory / ref["path"]
        if missing:
            path.unlink()
        else:
            original = path.read_bytes()
            path.write_bytes(b"X" + original[1:])
        expected = "artifact_missing" if missing else "artifact_corrupt"
        # Metadata can still be inspected; explicit body reads must never invent empty text.
        state = self.library.load("sample-course")
        with self.assertRaises(LearnerError) as error:
            _ = state["submissions"]["damaged-body"]["answer"]
        self.assertEqual(error.exception.code, expected)
        before = (self.directory / "course.json").read_bytes()
        for action in (lambda: self.library.backup_info("sample-course"),
                       lambda: self.library.restore("sample-course", info["sha256"], confirmed=True)):
            with self.assertRaises(LearnerError) as error:
                action()
            self.assertEqual(error.exception.code, expected)
        self.assertEqual((self.directory / "course.json").read_bytes(), before)
        self.assertEqual(list(self.directory.glob("before-restore-*.json")), [])

    def test_missing_body_refuses_read_and_backup_restore(self):
        self.damaged_body_is_rejected(missing=True)

    def test_corrupt_body_refuses_read_and_backup_restore(self):
        self.damaged_body_is_rejected(missing=False)

    def test_schema_one_reads_but_requires_explicit_migration_for_writes(self):
        legacy, original, previous, _ = self.legacy_fixture()
        self.assertEqual(self.library.load("sample-course")["submissions"]["legacy-answer"]["answer"],
                         legacy["submissions"]["legacy-answer"]["answer"])
        with self.assertRaises(LearnerError) as error:
            self.submit(sid="must-not-write", answer="等待显式迁移")
        self.assertEqual(error.exception.code, "migration_required")
        report = preview_migration(self.library, "sample-course")
        self.assertTrue(report["preview"])
        self.assertFalse(report["changed"])
        self.assertEqual(report["reader"], "ijson_incremental")
        for sha, confirm, expected in ((report["original_sha256"], False, "confirmation_required"),
                                       ("0" * 64, True, "migration_changed")):
            with self.assertRaises(LearnerError) as error:
                migrate_course(self.library, "sample-course", sha, confirmed=confirm)
            self.assertEqual(error.exception.code, expected)
        self.assertEqual((self.directory / "course.json").read_bytes(), original)
        self.assertEqual((self.directory / "course.previous.json").read_bytes(), previous)

    def test_migration_preserves_history_identity_bytes_and_idempotency(self):
        legacy, original, previous, original_request = self.legacy_fixture()
        preview = preview_migration(self.library, "sample-course")
        result = migrate_course(self.library, "sample-course", preview["original_sha256"], confirmed=True)
        migrated = self.library.load("sample-course")
        materialized = blobs.materialize(migrated)
        self.assertEqual(migrated["schema_version"], 2)
        self.assertEqual(migrated["state_revision"], legacy["state_revision"])
        self.assertEqual(migrated["created_at"], legacy["created_at"])
        self.assertEqual(migrated["updated_at"], legacy["updated_at"])
        for key in ("submissions", "assessments", "help_events", "operations"):
            self.assertEqual(materialized[key], legacy[key], key)
        for source_id, source in migrated["sources"].items():
            self.assertTrue(source["legacy_claim"])
            self.assertEqual(source["sha256"], legacy["sources"][source_id]["sha256"])
            for old, segment in zip(legacy["sources"][source_id]["segments"], source["segments"]):
                self.assertEqual(segment["text"], old["text"])
                self.assertEqual(segment["legacy_verified"], old["verified"])
                self.assertTrue(segment["legacy_claim"])
                self.assertFalse(segment["verified"])
        self.assertEqual(migrated["source_verifications"], {})
        self.assertEqual(migrated["migration"]["original_sha256"], hashlib.sha256(original).hexdigest())
        backup = Path(result["backup"])
        self.assertEqual((backup / "course.json").read_bytes(), original)
        self.assertEqual((backup / "course.previous.json").read_bytes(), previous)
        self.assertEqual((self.directory / "course.previous.json").read_bytes(), original)
        before_retry = (self.directory / "course.json").read_bytes()
        duplicate = self.engine.apply(original_request)
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(duplicate["result"]["answer"], legacy["submissions"]["legacy-answer"]["answer"])
        self.assertEqual((self.directory / "course.json").read_bytes(), before_retry)
        self.assertEqual((backup / "course.json").read_bytes(), original)

    def test_large_legacy_migration_uses_incremental_json_reader(self):
        import ijson
        _, original, _, _ = self.legacy_fixture(answer="历史大段中文\r\n" * 80000)
        self.assertGreater(len(original), 1024 * 1024)
        legacy_path = self.directory / "course.json"
        real_read_text, real_read_bytes = Path.read_text, Path.read_bytes

        def guarded_text(path, *args, **kwargs):
            self.assertNotEqual(path, legacy_path, "Legacy input must not use Path.read_text")
            return real_read_text(path, *args, **kwargs)

        def guarded_bytes(path, *args, **kwargs):
            self.assertNotEqual(path, legacy_path, "Legacy input must not be read as one bytes object")
            return real_read_bytes(path, *args, **kwargs)

        with mock.patch.object(Path, "read_text", guarded_text), \
                mock.patch.object(Path, "read_bytes", guarded_bytes), \
                mock.patch("json.load", side_effect=AssertionError("Do not json.load legacy input")), \
                mock.patch.object(ijson, "basic_parse", wraps=ijson.basic_parse) as parse:
            report = preview_migration(self.library, "sample-course")
        self.assertTrue(parse.called)
        self.assertEqual(report["reader"], "ijson_incremental")
        self.assertLess(report["manifest_bytes"], len(original) // 10)
        self.assertEqual(legacy_path.read_bytes(), original)

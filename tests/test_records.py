import copy
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from support import CourseCase, ROOT
from codex_learner.common import LearnerError, canonical, read_json
from codex_learner.storage import Library, atomic_write, file_lock
from create_demo import sample_requests, seed
from install_skill import install


class RecordTests(CourseCase):
    def test_e01_exact_answer_is_saved_before_any_feedback(self):
        answer = '中文第一行\n"quoted" \\ backslash\n$(touch should-not-exist)\n</script><script>bad()</script>'
        request = self.submit(answer=answer)
        state = self.library.load("sample-course")
        self.assertEqual(state["submissions"][request["submission_id"]]["answer"], answer)
        self.assertEqual(state["submissions"][request["submission_id"]]["status"], "pending_assessment")
        self.assertEqual(state["assessments"], {})
        self.assertEqual(self.status(), "pending_assessment")

    def test_e12_same_old_snapshot_accepts_two_questions(self):
        revision = self.library.load("sample-course")["state_revision"]
        self.submit(sid="first", state_revision=revision)
        self.submit(sid="second", question_id="q2", state_revision=revision, answer="包含停留")
        state = self.library.load("sample-course")
        self.assertEqual(set(state["submissions"]), {"first", "second"})
        self.assertEqual(state["position"]["chapter_id"], "chapter-1")

    def test_e13_duplicate_is_idempotent_different_payload_is_conflict(self):
        request = self.submit(sid="same")
        revision = self.library.load("sample-course")["state_revision"]
        receipt = self.engine.apply(request)
        self.assertTrue(receipt["duplicate"])
        self.assertEqual(self.library.load("sample-course")["state_revision"], revision)
        with self.assertRaises(LearnerError) as error:
            self.engine.apply({**request, "answer": "different"})
        self.assertEqual(error.exception.code, "id_conflict")
        self.assertEqual(self.library.load("sample-course")["submissions"]["same"]["answer"], request["answer"])

    def test_unknown_target_preserves_other_courses(self):
        seed(self.library, "another-course")
        before = self.library.load("sample-course")
        with self.assertRaises(LearnerError):
            self.submit(question_id="nonexistent")
        self.assertEqual(self.library.load("sample-course"), before)
        self.assertEqual(self.library.load("another-course")["submissions"], {})
        self.assertEqual(len(self.library.list_courses()), 2)

    def test_e14_old_chapter_answer_does_not_navigate(self):
        self.apply("navigate", chapter_id="chapter-2", content_version="v1")
        self.submit(sid="old-chapter")
        self.assertEqual(self.library.load("sample-course")["position"]["chapter_id"], "chapter-2")

    def test_e15_failed_replace_keeps_valid_state_and_backup(self):
        before = self.library.load("sample-course")
        target = self.library.root / "sample-course/course.json"
        def failing(path, data):
            if Path(path) == target:
                raise OSError("simulated full disk")
            return atomic_write(path, data)
        with patch("codex_learner.storage.atomic_write", side_effect=failing):
            with self.assertRaises(OSError):
                self.submit(sid="failed")
        self.assertEqual(self.library.load("sample-course"), before)
        self.assertEqual(read_json(target.with_name("course.previous.json")), before)

    def test_e15_readback_failure_retry_recovers_one_record(self):
        original = self.library.load
        calls = 0
        def failing(course_id):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("simulated interrupted readback")
            return original(course_id)
        request = self.request("submit_answer", **self.context, submission_id="uncertain", answer="5")
        with patch.object(self.library, "load", side_effect=failing):
            with self.assertRaises(LearnerError) as error:
                self.engine.apply(request)
        self.assertEqual(error.exception.code, "commit_uncertain")
        receipt = self.engine.apply(request)
        self.assertTrue(receipt["duplicate"])
        self.assertEqual(len(original("sample-course")["submissions"]), 1)

    def test_e15_lock_busy_leaves_request_retryable(self):
        request = self.request("submit_answer", **self.context, submission_id="busy-answer", answer="5")
        lock = self.library.root / "sample-course/.lock"
        with file_lock(lock):
            with self.assertRaises(LearnerError) as error:
                self.engine.apply(request)
        self.assertEqual(error.exception.code, "busy")
        self.assertFalse(self.engine.apply(request)["duplicate"])

    def test_e16_corruption_needs_explicit_backup_selection(self):
        self.submit(sid="backup-answer")
        info = self.library.backup_info("sample-course")
        target = self.library.root / "sample-course/course.json"
        target.write_text("{broken", encoding="utf-8")
        with self.assertRaises(LearnerError):
            self.library.load("sample-course")
        with self.assertRaises(LearnerError):
            self.library.restore("sample-course", info["sha256"])
        receipt = self.library.restore("sample-course", info["sha256"], confirmed=True)
        self.assertEqual(Path(receipt["preserved_original"]).read_text(), "{broken")
        self.assertEqual(self.library.load("sample-course")["state_revision"], info["state_revision"])
        self.assertEqual(len(self.library.list_courses()), 1)

    def test_e16_missing_state_does_not_recreate_course(self):
        target = self.library.root / "sample-course/course.json"
        target.rename(target.with_name("missing-original.json"))
        with self.assertRaises(LearnerError):
            self.library.load("sample-course")
        with self.assertRaises(LearnerError):
            self.apply("create_course", title="same", goals="same")
        self.assertFalse(target.exists())

    def test_unknown_schema_and_duplicate_json_keys_fail_closed(self):
        target = self.library.root / "sample-course/course.json"
        state = self.library.load("sample-course")
        state["schema_version"] = 99
        target.write_text(canonical(state), encoding="utf-8")
        with self.assertRaises(LearnerError) as error:
            self.submit()
        self.assertEqual(error.exception.code, "unsupported_schema")
        target.write_text('{"schema_version":1,"schema_version":2}', encoding="utf-8")
        with self.assertRaises(LearnerError):
            self.library.load("sample-course")

    def test_nested_corruption_does_not_replace_last_valid_backup(self):
        self.submit(sid="creates-backup")
        target = self.library.root / "sample-course/course.json"
        backup = target.with_name("course.previous.json").read_bytes()
        state = self.library.load("sample-course")
        del state["chapters"]["chapter-1"]["versions"]["v1"]["checkpoint"]
        target.write_text(canonical(state), encoding="utf-8")
        with self.assertRaises(LearnerError) as error:
            self.submit(sid="must-not-write")
        self.assertEqual(error.exception.code, "corrupt_state")
        self.assertEqual(target.with_name("course.previous.json").read_bytes(), backup)

    def test_installer_rejects_nested_library_without_touching_source(self):
        destination = self.root / "skill"
        with self.assertRaises(LearnerError):
            install(destination, destination / "library")
        self.assertFalse(destination.exists())

    def test_library_not_recreated_when_configured_root_is_missing(self):
        library = Library(self.root / "missing")
        with self.assertRaises(LearnerError):
            library.list_courses()
        self.assertFalse(library.root.exists())

    def test_path_traversal_is_rejected(self):
        with self.assertRaises(LearnerError):
            self.library.load("../escape")

    def test_malformed_request_returns_json_error_without_changing_records(self):
        path = self.root / "malformed.json"
        path.write_text(json.dumps(self.request("publish_content", chapter_id="bad", content_version="v1",
                                               title="bad", goals=[None], parts=[None])), encoding="utf-8")
        before = self.library.load("sample-course")
        result = subprocess.run([sys.executable, str(ROOT / "skills/codex-learner/scripts/learner.py"),
                                 "--library", str(self.library.root), "apply", "--request", str(path)],
                                capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stderr)["error"], "invalid_request")
        self.assertEqual(self.library.load("sample-course"), before)

    def test_two_processes_do_not_overwrite_each_other(self):
        requests = [self.request("submit_answer", **self.context, submission_id=f"parallel-{i}", answer=str(i))
                    for i in range(2)]
        processes = []
        for index, request in enumerate(requests):
            path = self.root / f"request-{index}.json"
            path.write_text(json.dumps(request), encoding="utf-8")
            process = subprocess.Popen([sys.executable, str(ROOT / "skills/codex-learner/scripts/learner.py"),
                                        "--library", str(self.library.root), "apply", "--request", str(path)],
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
            processes.append((process, request))
        for process, request in processes:
            stdout, stderr = process.communicate(timeout=15)
            if process.returncode:
                self.assertEqual(process.returncode, 2, stderr)
                self.assertEqual(json.loads(stderr)["error"], "busy")
                self.engine.apply(request)
            else:
                self.assertTrue(json.loads(stdout)["ok"])
        self.assertEqual(set(self.library.load("sample-course")["submissions"]), {"parallel-0", "parallel-1"})

    def test_cli_new_process_and_different_cwd_restore_same_record(self):
        # 这是进程级恢复验证，不冒充真实 Codex 新对话发现测试。
        self.submit(sid="persistent", answer="跨进程恢复\n完整原答")
        destination = self.root / "user/.agents/skills/codex-learner"
        first = install(destination, self.library.root)
        second = install(destination, self.library.root)
        self.assertTrue(first["changed"])
        self.assertFalse(second["changed"])
        elsewhere = self.root / "another-working-directory"
        elsewhere.mkdir()
        result = subprocess.run([sys.executable, str(destination / "scripts/learner.py"),
                                 "show", "--course", "sample-course"], cwd=elsewhere,
                                capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["result"]["submissions"]["persistent"]["answer"], "跨进程恢复\n完整原答")

    def test_installer_preserves_existing_skill_and_requires_deliberate_replace(self):
        destination = self.root / "user/.agents/skills/codex-learner"
        install(destination, self.library.root)
        custom = destination / "custom.txt"
        custom.write_text("user work", encoding="utf-8")
        with self.assertRaises(LearnerError):
            install(destination, self.library.root)
        receipt = install(destination, self.library.root, backup_and_replace=True)
        self.assertEqual((Path(receipt["backup"]) / "custom.txt").read_text(), "user work")
        self.assertNotIn(destination.parent, Path(receipt["backup"]).parents)

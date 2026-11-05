import copy
import json
import shutil
import subprocess
import sys
from unittest.mock import patch

from support import CourseCase, ROOT
from create_demo import sample_requests
from codex_learner.common import LearnerError, body_hash, canonical
from codex_learner.views import course_history, course_summary, retest_history, submission_detail


class V2LearningTests(CourseCase):
    def publish_retest(self, question_id, kind="delayed", **overrides):
        request = copy.deepcopy(next(r for r in sample_requests() if r.get("question_id") == "q1"))
        request.update(operation_id="publish-" + question_id, question_id=question_id,
                       retest_context={"baseline_submission_id": "baseline", "kind": kind,
                                       "context": "同一目标的延迟检查或新情境应用。"})
        request.update(overrides)
        return self.engine.apply(request)

    def mark_source_legacy(self):
        def mark(state, directory):
            source = state["sources"]["sample-source-v1"]
            source["legacy_claim"] = True
            for segment in source["segments"]:
                segment["legacy_verified"] = segment["verified"]
                segment["verified"] = False
                segment["legacy_claim"] = True
                segment["verification_status"] = "legacy_claim"
            return {"status": "legacy_fixture"}
        self.library.transact(self.request("legacy_fixture"), mark)

    def test_retest_elapsed_is_observed_and_groups_keep_independent_results(self):
        with patch("codex_learner.operations.now", return_value="2026-10-01T10:00:00+00:00"):
            self.submit(sid="baseline")
        self.assess("baseline", feedback_help="solution")
        self.publish_retest("delayed-question")
        with patch("codex_learner.operations.now", return_value="2026-10-04T10:00:03+00:00"):
            self.submit(sid="delayed-answer", question_id="delayed-question")
        self.assess("delayed-answer", outcome="needs_work")
        self.publish_retest("transfer-question", kind="transfer")
        with patch("codex_learner.operations.now", return_value="2026-10-05T10:00:00+00:00"):
            self.submit(sid="transfer-answer", question_id="transfer-question")
        self.assess("transfer-answer")

        state = self.library.load("sample-course")
        history = course_history(state, include_bodies=True)
        self.assertEqual(history["groups"], {"immediate": 1, "delayed": 1, "transfer": 1})
        delayed = retest_history(state, kind="delayed")["items"][0]
        self.assertEqual(delayed["retest"]["baseline_submission_id"], "baseline")
        self.assertEqual(delayed["retest"]["elapsed_seconds"], 3 * 86400 + 3)
        self.assertEqual(delayed["assessment"]["results"]["compute"]["status"], "needs_work")
        transfer = retest_history(state, kind="transfer")["items"][0]
        self.assertEqual(transfer["retest"]["elapsed_seconds"], 4 * 86400)
        self.assertEqual(transfer["effective_help"], "none")
        self.assertEqual(self.status(), "passed")
        self.assertEqual(submission_detail(state, "baseline")["answer"], "5 米每秒")
        self.assertEqual(len(state["submissions"]), 3)

    def test_retest_rejects_invented_delay_unknown_baseline_and_reused_question(self):
        self.submit(sid="baseline")
        base = {"baseline_submission_id": "baseline", "kind": "delayed", "context": "延迟检查"}
        for context in ["七天后复测", {**base, "elapsed_days": 7},
                        {**base, "baseline_submission_id": "unknown"}, {**base, "kind": "mastered"}]:
            with self.subTest(context=context), self.assertRaises(LearnerError):
                self.publish_retest("bad-retest", retest_context=context)
        with self.assertRaises(LearnerError):
            self.publish_retest("q1", question_version="qv2")
        self.assertNotIn("bad-retest", self.library.load("sample-course")["questions"])

    def test_legacy_retest_context_is_retained_without_inventing_a_delay(self):
        def legacy_context(state, directory):
            question = state["questions"]["q1"]["versions"]["qv1"]
            question["retest_context"] = "旧记录自述一周后检查，未绑定基线。"
            question.pop("retest_schema_version", None)
            return {"status": "legacy_retest_fixture"}
        self.library.transact(self.request("legacy_retest_fixture"), legacy_context)
        self.submit(sid="legacy-answer")
        history = retest_history(self.library.load("sample-course"))
        item = history["items"][0]
        self.assertEqual(item["kind"], "legacy_unclassified")
        self.assertIn("未绑定基线", item["legacy_retest_context"])
        self.assertNotIn("retest", item)
        self.assertNotIn("elapsed_seconds", item)

    def test_history_pages_metadata_without_reading_bodies(self):
        for index in range(3):
            self.submit(sid=f"answer-{index}", answer=f"逐字原答 {index}\r\n")
            self.assess(f"answer-{index}")
        state = self.library.load("sample-course")
        with patch("codex_learner.blobs.read_body", side_effect=AssertionError("Summary opened a body")):
            page = course_history(state, page=2, page_size=1)
            summary = course_summary(state)
        self.assertEqual(page["total"], 3)
        self.assertEqual(page["items"][0]["submission_id"], "answer-1")
        self.assertEqual(page["next_page"], 3)
        self.assertNotIn("answer", page["items"][0])
        self.assertEqual(summary["pending"], [])

        from codex_learner.blobs import read_body
        with patch("codex_learner.blobs.read_body", wraps=read_body) as reader:
            full = course_history(state, page=2, page_size=1, include_bodies=True)
        self.assertEqual(full["items"][0]["answer"], "逐字原答 1\r\n")
        opened = {call.args[1]["path"] for call in reader.call_args_list}
        self.assertIn(state["submissions"]["answer-1"]["answer_ref"]["path"], opened)
        self.assertNotIn(state["submissions"]["answer-0"]["answer_ref"]["path"], opened)
        self.assertNotIn(state["submissions"]["answer-2"]["answer_ref"]["path"], opened)
        for kwargs in ({"page": 0}, {"page_size": 101}, {"page": True}, {"kind": "unknown"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(LearnerError):
                course_history(state, **kwargs)

    def test_question_revision_has_explanation_without_erasing_old_answer(self):
        self.submit(sid="old")
        self.assess("old")
        request = copy.deepcopy(next(r for r in sample_requests() if r.get("question_id") == "q1"))
        request.update(operation_id="publish-new-version", question_version="qv2")
        self.engine.apply(request)
        row = next(c for goal in self.summary()["goals"] for c in goal["checks"] if c["check_id"] == "compute")
        self.assertEqual(row["status"], "unassessed")
        self.assertEqual(row["unassessed_reason"], "question_revised")
        old = submission_detail(self.library.load("sample-course"), "old")
        self.assertFalse(old["current"])
        self.assertEqual(old["answer"], "5 米每秒")

    def test_legacy_source_needs_hash_bound_review_for_new_evidence(self):
        self.mark_source_legacy()
        state = self.library.load("sample-course")
        source = state["sources"]["sample-source-v1"]
        segment = next(s for s in source["segments"] if s["locator"] == "line:2")
        request = copy.deepcopy(next(r for r in sample_requests() if r.get("question_id") == "q1"))
        request.update(operation_id="publish-after-review", question_id="reviewed-question")
        with self.assertRaises(LearnerError) as error:
            self.engine.apply(request)
        self.assertEqual(error.exception.code, "unverified_source")

        review = self.request("verify_source", source_id="sample-source-v1", locators=["line:2"],
                              evidence="逐字对照归档原件第 2 行，已核对数值、单位和关系。",
                              expected_source_sha256=source["sha256"],
                              expected_segment_hashes={"line:2": body_hash(segment, "text")})
        receipt = self.engine.apply(review)
        self.assertTrue(self.engine.apply(review)["duplicate"])
        # Reviewing the general definition alone must not authorize the other
        # source locations now cited by the sample rubric.
        partial = self.library.load("sample-course")
        with self.assertRaises(LearnerError) as error:
            self.engine.apply(request)
        self.assertEqual(error.exception.code, "unverified_source")
        self.assertEqual(self.library.load("sample-course")["state_revision"], partial["state_revision"])
        self.assertNotIn("reviewed-question", self.library.load("sample-course")["questions"])

        required_locators = {ref["locator"] for ref in request["rubric"]["source_refs"]}
        remaining_locators = sorted(required_locators - {"line:2"})
        self.assertTrue(remaining_locators)
        complete_review = self.request(
            "verify_source", source_id="sample-source-v1", locators=remaining_locators,
            evidence="对照归档原件逐项核对题目引用的其余数值、单位和含义。",
            expected_source_sha256=source["sha256"],
            expected_segment_hashes={s["locator"]: body_hash(s, "text") for s in source["segments"]
                                     if s["locator"] in remaining_locators})
        self.engine.apply(complete_review)
        self.engine.apply(request)
        state = self.library.load("sample-course")
        event = state["source_verifications"][review["operation_id"]]
        self.assertEqual(event["source_sha256"], source["sha256"])
        self.assertEqual(event["segment_hashes"], review["expected_segment_hashes"])
        self.assertEqual(receipt["result"]["verification_event_id"], review["operation_id"])
        self.assertEqual(len(state["source_verifications"]), 2)
        self.assertEqual(state["source_verifications"][complete_review["operation_id"]]["segment_hashes"],
                         complete_review["expected_segment_hashes"])
        self.assertIn("reviewed-question", state["questions"])
        remaining = next(s for s in state["sources"]["sample-source-v1"]["segments"]
                         if s["locator"] not in required_locators)
        self.assertTrue(remaining["legacy_claim"])
        self.assertFalse(remaining["verified"])

    def test_verification_conflict_does_not_mutate_or_replace_prior_event(self):
        first = self.request("verify_source", source_id="sample-source-v1", locators=["line:2"],
                             evidence="第一次对照原件核验。")
        self.engine.apply(first)
        before = self.library.load("sample-course")
        original = copy.deepcopy(before["source_verifications"][first["operation_id"]])
        with self.assertRaises(LearnerError) as error:
            self.apply("verify_source", source_id="sample-source-v1", locators=["line:2"],
                       evidence="来自旧文本的核验请求。", expected_segment_hashes={"line:2": "0" * 64})
        self.assertEqual(error.exception.code, "verification_conflict")
        self.assertEqual(self.library.load("sample-course")["state_revision"], before["state_revision"])
        self.apply("verify_source", source_id="sample-source-v1", locators=["line:2"],
                   evidence="另一次独立核验，保留首次证据。")
        state = self.library.load("sample-course")
        self.assertEqual(len(state["source_verifications"]), 2)
        self.assertEqual(state["source_verifications"][first["operation_id"]], original)

    def test_changed_archived_original_cannot_be_marked_verified(self):
        state = self.library.load("sample-course")
        path = self.library.root / "sample-course" / state["sources"]["sample-source-v1"]["path"]
        with path.open("ab") as stream:
            stream.write(b"changed")
        with self.assertRaises(LearnerError) as error:
            self.apply("verify_source", source_id="sample-source-v1", locators=["line:2"], evidence="核对已变化原件。")
        self.assertEqual(error.exception.code, "artifact_corrupt")
        self.assertEqual(self.library.load("sample-course")["state_revision"], state["state_revision"])

    def test_lecture_ref_returns_one_complete_part_and_retry_keeps_it(self):
        request = self.request("prepare_part", chapter_id="chapter-1", content_version="v1", part_id="part-1")
        first = self.engine.apply(request)
        again = self.engine.apply(request)
        self.assertEqual(first["result"]["part"]["text"], again["result"]["part"]["text"])
        self.assertTrue(again["duplicate"])
        state = self.library.load("sample-course")
        part = state["chapters"]["chapter-1"]["versions"]["v1"]["parts"][0]
        self.assertIn("text_ref", part)
        self.assertNotIn("text", part)
        self.assertEqual(part["text"], first["result"]["part"]["text"])

    def test_ids_named_like_body_fields_survive_export_and_retry(self):
        from codex_learner.reads import export_course
        ids = ("answer", "answer_ref", "receipt", "receipt_ref", "text_ref")
        requests = [self.submit(sid=sid, answer=f"ID 为 {sid} 的逐字原答\r\n") for sid in ids]
        output = self.root / "expanded.json"
        export_course(self.library, "sample-course", output)
        state = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(set(state["submissions"]), set(ids))
        for request in requests:
            sid = request["submission_id"]
            self.assertEqual(state["submissions"][sid]["answer"], request["answer"])
            receipt = self.engine.apply(request)
            self.assertEqual(receipt["operation_id"], sid)
            self.assertEqual(receipt["result"]["answer"], request["answer"])

    def test_commit_checks_new_bodies_even_when_receipt_contains_only_ids(self):
        from codex_learner.storage import atomic_write_json
        self.submit(sid="body-integrity")
        def corrupt_after_commit(path, value):
            atomic_write_json(path, value)
            if path.name == "course.json" and value.get("assessments"):
                assessment = next(iter(value["assessments"].values()))
                (path.parent / assessment["feedback_ref"]["path"]).write_text("corrupt", encoding="utf-8")
        with patch("codex_learner.storage.atomic_write_json", side_effect=corrupt_after_commit):
            with self.assertRaises(LearnerError) as error:
                self.assess("body-integrity")
        self.assertEqual(error.exception.code, "commit_uncertain")

    def test_retry_rejects_a_valid_receipt_belonging_to_another_operation(self):
        first = self.submit(sid="receipt-one")
        self.submit(sid="receipt-two", answer="第二条答案")
        state = self.library.load("sample-course")
        state["operations"]["receipt-one"]["receipt_ref"] = copy.deepcopy(
            state["operations"]["receipt-two"]["receipt_ref"])
        path = self.library.course_dir("sample-course") / "course.json"
        path.write_text(canonical(state), encoding="utf-8")
        with self.assertRaises(LearnerError):
            self.engine.apply(first)

    def test_write_with_missing_history_body_preserves_last_valid_backup(self):
        self.submit(sid="missing-history-body")
        directory = self.library.course_dir("sample-course")
        state = self.library.load("sample-course")
        backup = directory / "course.previous.json"
        before_backup = backup.read_bytes()
        before_current = (directory / "course.json").read_bytes()
        self.library.backup_info("sample-course")
        ref = state["submissions"]["missing-history-body"]["answer_ref"]
        (directory / ref["path"]).unlink()
        with self.assertRaises(LearnerError) as error:
            self.apply("navigate", chapter_id="chapter-2", content_version="v1")
        self.assertEqual(error.exception.code, "artifact_missing")
        self.assertEqual(backup.read_bytes(), before_backup)
        self.assertEqual((directory / "course.json").read_bytes(), before_current)
        self.library.backup_info("sample-course")

    def test_cli_rejects_missing_review_id_and_non_object_config_as_json(self):
        learner = str(ROOT / "skills/codex-learner/scripts/learner.py")
        request = self.root / "review.json"
        request.write_text('{"action":"review"}', encoding="utf-8")
        commands = [["--library", str(self.library.root), "apply", "--request", str(request)]]
        for index, value in enumerate(([], None, 0, False)):
            config = self.root / f"invalid-config-{index}.json"
            config.write_text(json.dumps(value), encoding="utf-8")
            commands.append(["--config", str(config), "list"])
        for command in commands:
            with self.subTest(command=command):
                process = subprocess.run([sys.executable, learner, *command], capture_output=True,
                                         text=True, encoding="utf-8", timeout=15)
                self.assertEqual(process.returncode, 2, process.stderr)
                self.assertFalse(json.loads(process.stderr)["ok"])

    def test_migration_metadata_budget_stops_incremental_reader_early(self):
        import ijson
        from codex_learner.migration import _stream_compact
        source = self.root / "many-small-records.json"
        source.write_text(json.dumps({f"record-{i}": {"label": "短元数据", "order": i}
                                     for i in range(1000)}), encoding="utf-8")
        before = source.read_bytes()
        stage = self.root / "migration-stage"
        stage.mkdir()
        parser = ijson.basic_parse
        consumed = 0
        def observed(*args, **kwargs):
            nonlocal consumed
            for event in parser(*args, **kwargs):
                consumed += 1
                yield event
        with patch("codex_learner.migration.MAX_METADATA_ESTIMATE", 4096), \
                patch.object(ijson, "basic_parse", side_effect=observed):
            with self.assertRaises(LearnerError) as error:
                _stream_compact(source, stage)
        self.assertEqual(error.exception.code, "migration_capacity")
        self.assertLess(consumed, 100, "Metadata budget must stop parsing before retaining all 1000 records")
        self.assertEqual(source.read_bytes(), before)
        self.assertEqual(list(stage.iterdir()), [])

    def test_source_preparation_blocks_release_and_cleans_task_after_failure(self):
        from codex_learner.archives import create_archive, confirm_download, release_course
        directory = self.library.course_dir("sample-course")
        before = (directory / "course.json").read_bytes()
        archive = create_archive(self.library, "sample-course", self.root / "before-import.zip")
        # Local fixture only: this declaration does not prove a web download.
        download = self.root / "simulated-web-download.zip"
        shutil.copyfile(archive["path"], download)
        verification = confirm_download(download, archive["sha256"], source_declaration="web_download",
                                        archive_path=archive["path"])
        request = self.request("import_source", source_id="in-flight-source", path=str(self.root / "input.txt"))
        def preparing(*args, **kwargs):
            with self.assertRaises(LearnerError) as release_error:
                release_course(self.library, "sample-course", archive["path"], verification, confirmed=True)
            self.assertEqual(release_error.exception.code, "running_tasks")
            with self.assertRaises(LearnerError) as duplicate_error:
                self.engine.apply(request)
            self.assertEqual(duplicate_error.exception.code, "busy")
            raise LearnerError("probe_cancelled", "Controlled import failure after concurrency checks")
        with patch("codex_learner.operations.prepare_source", side_effect=preparing):
            with self.assertRaises(LearnerError) as error:
                self.engine.apply(request)
        self.assertEqual(error.exception.code, "probe_cancelled")
        self.assertEqual((directory / "course.json").read_bytes(), before)
        # A failed parser must remove its own marker, allowing explicit release.
        result = release_course(self.library, "sample-course", archive["path"], verification, confirmed=True)
        self.assertEqual(result["status"], "archived")

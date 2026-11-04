import copy

from support import CourseCase
from codex_learner.common import LearnerError
from create_demo import sample_requests


class LearningTests(CourseCase):
    def publish_question(self, qid="q1", version="qv2", content="v1"):
        request = next(r for r in sample_requests() if r.get("question_id") == "q1")
        request.update(operation_id=f"publish-{qid}-{version}-{content}", question_id=qid,
                       question_version=version, content_version=content)
        return self.engine.apply(request)

    def new_content(self, version="v2"):
        request = next(r for r in sample_requests() if r["action"] == "publish_content")
        request.update(operation_id="content-" + version, content_version=version)
        self.engine.apply(request)

    def test_e06_continue_prepares_but_does_not_confirm(self):
        base = {"chapter_id": "chapter-1", "content_version": "v1"}
        self.apply("prepare_part", **base, part_id="part-1")
        self.apply("prepare_part", **base, part_id="part-1")
        self.assertEqual(self.summary()["checkpoint"]["confirmed"], [])
        with self.assertRaises(LearnerError):
            self.apply("prepare_part", **base, part_id="part-2")
        self.apply("confirm_part", **base, part_id="part-1")
        self.apply("prepare_part", **base, part_id="part-2")
        receipt = self.apply("confirm_part", **base, part_id="part-1")
        self.assertEqual(receipt["result"]["status"], "already_confirmed")
        self.assertEqual(self.summary()["checkpoint"]["pending"], "part-2")
        self.assertEqual(self.summary()["checkpoint"]["confirmed"], ["part-1"])

    def test_e07_exercise_gate_and_explicit_skip_have_distinct_records(self):
        base = {"chapter_id": "chapter-1", "content_version": "v1"}
        with self.assertRaises(LearnerError):
            self.apply("begin_exercises", **base)
        with self.assertRaises(LearnerError):
            self.apply("begin_exercises", **base, confirm_last=True)
        self.apply("begin_exercises", **base, skip_remaining=True)
        cp = self.summary()["checkpoint"]
        self.assertEqual(cp["confirmed"], [])
        self.assertEqual(cp["skipped"], ["part-1", "part-2"])
        self.assertEqual(self.summary()["status"], "incomplete")

    def test_final_part_confirmation_atomically_enters_exercises(self):
        base = {"chapter_id": "chapter-1", "content_version": "v1"}
        self.apply("prepare_part", **base, part_id="part-1")
        self.apply("confirm_part", **base, part_id="part-1")
        self.apply("prepare_part", **base, part_id="part-2")
        self.apply("begin_exercises", **base, confirm_last=True)
        self.assertEqual(self.summary()["checkpoint"]["confirmed"], ["part-1", "part-2"])
        self.assertEqual(self.summary()["checkpoint"]["stage"], "exercises")
        self.assertEqual(self.summary()["status"], "incomplete")

    def test_early_answer_does_not_change_lecture_stage(self):
        self.submit()
        self.assertEqual(self.summary()["checkpoint"]["stage"], "lecture")
        self.assertEqual(self.summary()["checkpoint"]["confirmed"], [])

    def test_e08_recorded_outcomes_do_not_infer_mastery_from_answer_text(self):
        for outcome, expected in [("meets", "passed"), ("needs_work", "needs_work"), ("uncertain", "uncertain")]:
            with self.subTest(outcome=outcome):
                request = self.submit(answer="包含所有关键词并不证明理解")
                self.assess(request["submission_id"], outcome=outcome)
                self.assertEqual(self.status(), expected)
        # 真正的模型判题质量仍需宿主会话验收，测试不生成伪造的语义通过结论。

    def test_e09_feedback_help_applies_only_to_later_answers(self):
        self.submit(sid="original")
        self.assess("original", feedback_help="solution")
        self.assertEqual(self.status(), "passed")
        self.submit(sid="repeat")
        self.assess("repeat")
        self.assertEqual(self.status(), "assisted")
        state = self.library.load("sample-course")
        self.assertEqual(state["submissions"]["original"]["help_snapshot"], "none")
        self.assertEqual(state["submissions"]["repeat"]["help_snapshot"], "solution")

    def test_help_request_is_persisted_before_answer_exposure(self):
        self.apply("record_help", **self.context, event_id="hint", level="hint", source="user_request")
        self.submit(sid="hinted")
        self.assess("hinted")
        self.assertEqual(self.status(), "assisted")
        self.assertEqual(self.library.load("sample-course")["submissions"]["hinted"]["help_snapshot"], "hint")

    def test_e10_one_pass_does_not_cover_another_required_check(self):
        self.submit(sid="one")
        self.assess("one")
        self.assertEqual(self.summary()["goals"][0]["passed_checks"], 1)
        self.assertEqual(self.summary()["status"], "incomplete")
        self.submit(sid="two", question_id="q2")
        self.assess("two", outcome="needs_work")
        self.assertEqual(self.status("compute"), "passed")
        self.assertEqual(self.status("boundary"), "needs_work")
        self.submit(sid="three", question_id="q2")
        self.assess("three")
        self.assertEqual(self.summary()["status"], "passed")

    def test_e11_pending_feedback_survives_restart_and_deduplicates(self):
        self.submit(sid="pending")
        self.assertEqual(self.status(), "pending_assessment")
        receipt = self.assess("pending", assessment_id="grading")
        state = self.library.load("sample-course")
        self.assertEqual(state["submissions"]["pending"]["status"], "assessed")
        self.assertEqual(len(state["submissions"]), 1)
        self.assertEqual(receipt["result"]["assessment_id"], "grading")
        with self.assertRaises(LearnerError):
            self.assess("pending", assessment_id="grading")

    def test_e18_new_content_keeps_history_but_does_not_inherit_pass(self):
        self.submit(sid="v1-answer")
        self.assess("v1-answer")
        self.new_content()
        self.assertEqual(self.status(), "passed")
        self.apply("adopt_content", chapter_id="chapter-1", content_version="v2")
        self.assertEqual(self.status(), "unassessed")
        self.assertIn("v1-answer", self.library.load("sample-course")["submissions"])

    def test_unadopted_new_question_does_not_invalidate_current_content(self):
        self.submit(sid="v1-answer")
        self.assess("v1-answer")
        self.new_content()
        self.publish_question(content="v2")
        self.assertEqual(self.status(), "passed")

    def test_e21_late_old_grading_does_not_replace_newer_answer(self):
        self.submit(sid="a")
        self.submit(sid="b")
        self.assess("b", outcome="needs_work", assessment_id="b1")
        self.assess("a", outcome="meets", assessment_id="a1")
        self.assertEqual(self.status(), "needs_work")
        self.assess("a", outcome="meets", supersedes="a1", correction_reason="补充核对", assessment_id="a2")
        self.assertEqual(self.status(), "needs_work")
        self.submit(sid="c")
        self.assertEqual(self.status(), "pending_assessment")

    def test_e22_bad_rubric_invalidates_immediately_and_reassessment_preserves_order(self):
        self.submit(sid="graded")
        self.assess("graded", assessment_id="before")
        question = self.library.load("sample-course")["questions"]["q1"]["versions"]["qv1"]
        self.apply("replace_rubric", **self.context, rubric_version="r2",
                   rubric=question["rubrics"]["r1"], affected_checks=["compute"], reason="依据勘误")
        self.assertEqual(self.status(), "pending_reassessment")
        from codex_learner.views import course_summary
        pending = course_summary(self.library.load("sample-course"))["pending"]
        self.assertEqual(pending[0]["status"], "pending_reassessment")
        self.assertEqual(self.status("boundary"), "unassessed")
        revision = self.library.load("sample-course")["submissions"]["graded"]["received_revision"]
        self.assess("graded", rubric_version="r2", supersedes="before",
                    correction_reason="按新依据复评", assessment_id="after")
        self.assertEqual(self.status(), "passed")
        state = self.library.load("sample-course")
        self.assertEqual(state["submissions"]["graded"]["received_revision"], revision)
        self.assertEqual(set(state["assessments"]), {"before", "after"})

    def test_e23_retest_does_not_reset_existing_evidence_but_question_revision_does(self):
        self.submit(sid="first")
        self.assess("first")
        self.publish_question(qid="retest", version="qv1")
        self.assertEqual(self.status(), "passed")
        self.publish_question(version="qv2")
        self.assertEqual(self.status(), "unassessed")
        self.submit(sid="stale")
        self.assertEqual(self.library.load("sample-course")["submissions"]["stale"]["status"], "pending_content_review")

    def test_e24_retrospective_help_corrects_target_and_later_same_question(self):
        self.submit(sid="before")
        self.assess("before")
        self.submit(sid="target")
        self.assess("target")
        self.submit(sid="later")
        self.assess("later")
        self.apply("record_help", **self.context, event_id="disclosure", level="solution",
                   source="retrospective_disclosure", target_submission_id="target", reason="刚才看过参考答案")
        state = self.library.load("sample-course")
        from codex_learner.views import effective_help
        self.assertEqual(effective_help(state, state["submissions"]["before"]), "none")
        self.assertEqual(effective_help(state, state["submissions"]["target"]), "solution")
        self.assertEqual(effective_help(state, state["submissions"]["later"]), "solution")
        self.assertEqual(state["submissions"]["target"]["help_snapshot"], "none")
        self.assertEqual(self.status(), "assisted")

    def test_help_requested_after_submission_does_not_retroactively_change_it(self):
        self.submit(sid="before-help")
        self.apply("record_help", **self.context, event_id="now", level="solution", source="user_request")
        self.assess("before-help")
        self.assertEqual(self.status(), "passed")

    def test_e25_late_historical_answer_does_not_compete_with_current_evidence(self):
        self.publish_question(version="qv2")
        self.submit(sid="new", question_version="qv2")
        self.assess("new")
        self.submit(sid="old")
        self.assertEqual(self.status(), "passed")
        with self.assertRaises(LearnerError):
            self.assess("old")
        self.apply("resolve_history", submission_id="old")
        self.assess("old", outcome="needs_work")
        self.assertEqual(self.status(), "passed")
        self.assertEqual(self.library.load("sample-course")["submissions"]["old"]["evaluation_scope"], "history_only")

    def test_e20_two_chapters_can_complete_and_restore_independently(self):
        for chapter, question_ids in [("chapter-1", ["q1", "q2"]), ("chapter-2", ["q3"])]:
            self.apply("navigate", chapter_id=chapter, content_version="v1")
            for part in ["part-1", "part-2"]:
                self.apply("prepare_part", chapter_id=chapter, content_version="v1", part_id=part)
                self.apply("confirm_part", chapter_id=chapter, content_version="v1", part_id=part)
            self.apply("begin_exercises", chapter_id=chapter, content_version="v1")
            for question in question_ids:
                answer = {"q1": "100/20 = 5 米每秒", "q2": "计入停留，使用全程总时间",
                          "q3": "36 × 1000 ÷ 3600 = 10 米每秒"}[question]
                request = self.submit(question_id=question, chapter_id=chapter, answer=answer)
                self.assess(request["submission_id"])
            self.assertEqual(self.summary(chapter)["status"], "passed")
        self.assertEqual(self.library.load("sample-course")["position"]["chapter_id"], "chapter-2")
        self.assertEqual(self.summary("chapter-1")["status"], "passed")

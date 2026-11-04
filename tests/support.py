import sys
import tempfile
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "skills/codex-learner/scripts"), str(ROOT / "scripts")]

from codex_learner.operations import Engine
from codex_learner.storage import Library
from codex_learner.views import chapter_summary
from create_demo import seed


class CourseCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.library = Library(self.root / "sample-library")
        self.library.initialize()
        seed(self.library)
        self.engine = Engine(self.library)
        self.context = {"chapter_id": "chapter-1", "content_version": "v1",
                        "question_id": "q1", "question_version": "qv1"}
        self.refs = [{"source_id": "sample-source-v1", "locator": "line:2"}]

    def request(self, action, **fields):
        return {"action": action, "course_id": "sample-course", "operation_id": uuid.uuid4().hex, **fields}

    def apply(self, action, **fields):
        return self.engine.apply(self.request(action, **fields))

    def submit(self, sid=None, answer="5 米每秒", **fields):
        sid = sid or uuid.uuid4().hex
        request = self.request("submit_answer", **{**self.context, "submission_id": sid, "answer": answer, **fields})
        request["operation_id"] = sid
        self.engine.apply(request)
        return request

    def assess(self, submission_id, outcome="meets", **fields):
        state = self.library.load("sample-course")
        sub = state["submissions"][submission_id]
        q = state["questions"][sub["question_id"]]["versions"][sub["question_version"]]
        return self.apply("assess", **{
            "submission_id": submission_id, "assessment_id": uuid.uuid4().hex,
            "rubric_version": q["current_rubric"], "feedback": "依据材料核对了推理和单位。",
            "feedback_help": "none",
            "results": {check: {"outcome": outcome, "reason": "根据原答与来源作逐项评价。",
                               "source_refs": q["rubrics"][q["current_rubric"]]["source_refs"]}
                        for check in q["checks"]}, **fields})

    def summary(self, chapter_id="chapter-1"):
        return chapter_summary(self.library.load("sample-course"), chapter_id)

    def status(self, check="compute"):
        return next(c["status"] for g in self.summary()["goals"] for c in g["checks"] if c["check_id"] == check)

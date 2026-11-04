#!/usr/bin/env python3
"""Create an isolated sample course, never implicit personal learning records."""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/codex-learner/scripts"))

from codex_learner.operations import Engine
from codex_learner.panel import render
from codex_learner.storage import Library


def sample_requests(course_id="sample-course"):
    def req(action, suffix, **fields):
        return {"action": action, "operation_id": "sample-" + suffix, "course_id": course_id, **fields}
    ref = [{"source_id": "sample-source-v1", "locator": "line:2"}]
    yield req("create_course", "create", title="比例与单位 · 验收样例", goals="理解平均速率与单位换算。",
              outline=[{"chapter_id": "chapter-1", "title": "平均速率", "goals": "解释、计算并辨别适用边界"},
                       {"chapter_id": "chapter-2", "title": "单位换算", "goals": "正确换算常见速率单位"}])
    yield req("import_source", "source", source_id="sample-source-v1", path=str(ROOT / "examples/sample.md"))
    for cid, title, checks in [
        ("chapter-1", "平均速率", [("compute", "解释并计算速率"), ("boundary", "辨别适用边界")]),
        ("chapter-2", "单位换算", [("convert", "换算速率单位")]),
    ]:
        refs = ref if cid == "chapter-1" else [{"source_id": "sample-source-v1", "locator": "line:6"}]
        yield req("publish_content", cid, chapter_id=cid, content_version="v1", title=title,
                  goals=[{"goal_id": "understand", "label": title,
                          "checks": [{"check_id": key, "label": label} for key, label in checks]}],
                  parts=[{"part_id": "part-1", "text": "本段目标：" + title + "。请结合原资料讲解机制和例子。",
                          "source_refs": refs},
                         {"part_id": "part-2", "text": "核对单位、适用边界并小结。章末再进入练习。",
                          "source_refs": refs}])
    for qid, cid, check, prompt, answer, refs in [
        ("q1", "chapter-1", "compute", "跑完 100 米用时 20 秒。平均速率是多少？解释公式和单位。",
         "5 米每秒，因为总路程除以总时间。", ref),
        ("q2", "chapter-1", "boundary", "途中停留时，计算平均速率是否计入停留时间？说明理由。",
         "计入停留时间，使用全程总时间。", ref),
        ("q3", "chapter-2", "convert", "把 36 千米每小时换算为米每秒，并说明换算步骤。",
         "36 × 1000 ÷ 3600 = 10 米每秒。",
         [{"source_id": "sample-source-v1", "locator": "line:6"}]),
    ]:
        yield req("publish_question", qid, chapter_id=cid, content_version="v1",
                  question_id=qid, question_version="qv1", rubric_version="r1", prompt=prompt, checks=[check],
                  rubric={"verified": True, "criteria": {check: {
                      "required": answer, "alternatives": "等价的正确解释、分数或换算写法均可。",
                      "critical_errors": "公式、时间范围或单位错误不可判为符合依据。"}},
                      "reference_answer": answer, "source_refs": refs})


def seed(library, course_id="sample-course"):
    engine = Engine(library)
    for request in sample_requests(course_id):
        engine.apply(request)
    return library.load(course_id)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--library", type=Path, required=True, help="An isolated sample library, never your personal one")
    parser.add_argument("--panel", type=Path)
    args = parser.parse_args()
    library = Library(args.library)
    library.initialize()
    state = seed(library)
    result = {"sample_course_id": state["course_id"], "library": str(library.root)}
    if args.panel:
        result["panel"] = render(state, args.panel)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

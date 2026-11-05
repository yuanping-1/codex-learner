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


LESSONS = {
    "chapter-1": [
        """目标：用总路程和总时间解释平均速率，并写出正确单位。

设想把一次跑步的完整过程均匀摊开：平均每秒经过多少米，就是这段行程的平均速率。
计算时先确定行程起点、终点，再把这段行程的路程和经过的时间分别合计。
平均速率 = 总路程 ÷ 总时间。路程用米、时间用秒时，结果的单位就是米每秒（m/s）。

例：100 米跑了 20 秒，100 ÷ 20 = 5 m/s。这里的 5 m/s 表示这 20 秒平均每秒走过 5 米；
不表示每一秒都恰好走了 5 米，更不保证终点那一刻的速率也是 5 m/s。

读完后可以用自己的话解释：为什么分子是路程、分母是时间？先确认这一段，再看停留和往返的边界。""",
        """目标：辨别停留、分段和往返时的计算范围。

平均速率的总时间包含途中停留。若走 60 米用 20 秒，停 10 秒，再走 40 米用 20 秒，
全程平均速率是 (60 + 40) ÷ (20 + 10 + 20) = 2 m/s。删去停留时间会改变题目所说的全程。
分段速率一般不能直接取算术平均：先走 60 米用 20 秒（3 m/s），再走 40 米用 40 秒（1 m/s），
全程应算 100 ÷ 60 = 5/3 m/s，而不是 (3 + 1) ÷ 2 = 2 m/s。

平均速度使用位移，平均速率使用路程。例如用 40 秒往返一条 100 米跑道，路程是 200 米，
平均速率为 5 m/s；终点回到起点，位移是 0，平均速度为 0。两者不能混用。

小结：先划定同一段行程，合计路程和包含停留的时间，再相除并写单位。
确认或明确跳过本段后进入章末练习；不确定时可以先请求提示。""",
    ],
    "chapter-2": [
        """目标：把千米每小时换成米每秒，并解释换算中两个因子的来源。

单位换算改变数值的写法，不改变实际运动。1 千米 = 1000 米，1 小时 = 3600 秒。
36 千米每小时表示每小时走 36 千米，也就是 3600 秒走 36000 米。
因此 36 km/h = 36 × 1000 ÷ 3600 m/s = 10 m/s。

可以把换算写成消去旧单位的乘法：
36 km/h × (1000 m / 1 km) × (1 h / 3600 s) = 10 m/s。
路程单位在分子，乘 1000；时间单位在分母，一小时替换为 3600 秒后要除 3600。
只乘 1000 或把时间也乘 3600 都会得到错误的量。

确认这一段前，请解释为什么不是把 36 同时乘以 1000 和 3600。下一段检验反向换算和结果大小。""",
        """目标：用反向换算和量级检查发现错误。

因为 1000/3600 = 1/3.6，千米每小时换米每秒可以除以 3.6。
反过来，米每秒换千米每小时要乘以 3.6：10 m/s × 3600 s/h ÷ 1000 m/km = 36 km/h。
这是从同一组单位关系推出的快捷写法，不需要另背一套互不相干的公式。

迁移例：自行车用 5 分钟走了 1.5 千米。先统一单位，1.5 千米 = 1500 米，5 分钟 = 300 秒；
平均速率为 1500 ÷ 300 = 5 m/s，再换成 5 × 3.6 = 18 km/h。
用反向检查：18 km/h × 1000 ÷ 3600 = 5 m/s，两个答案表示同一速率。

小结：先写目标单位，再逐个替换原单位，保留单位直到消去，最后用反向换算检查。
章末需要同时给出数值、单位和换算步骤；只有正确数字而没有理由时仍需补充解释。""",
    ],
}


def sample_requests(course_id="sample-course"):
    def req(action, suffix, **fields):
        return {"action": action, "operation_id": "sample-" + suffix, "course_id": course_id, **fields}

    def refs(*lines):
        return [{"source_id": "sample-source-v1", "locator": f"line:{line}"} for line in lines]

    # Keep existing IDs and original source line locators stable. Each part now
    # cites its own worked examples as well as the general definitions.
    lesson_refs = {
        "chapter-1": [refs(2, 3, 9), refs(2, 4, 10, 11, 12)],
        "chapter-2": [refs(6, 7, 14), refs(6, 15, 16, 17)],
    }
    yield req("create_course", "create", title="比例与单位 · 验收样例", goals="理解平均速率与单位换算。",
              outline=[{"chapter_id": "chapter-1", "title": "平均速率", "goals": "解释、计算并辨别适用边界"},
                       {"chapter_id": "chapter-2", "title": "单位换算", "goals": "正确换算常见速率单位"}])
    yield req("import_source", "source", source_id="sample-source-v1", path=str(ROOT / "examples/sample.md"))
    for cid, title, checks in [
        ("chapter-1", "平均速率", [("compute", "解释并计算速率"), ("boundary", "辨别适用边界")]),
        ("chapter-2", "单位换算", [("convert", "换算速率单位")]),
    ]:
        yield req("publish_content", cid, chapter_id=cid, content_version="v1", title=title,
                  goals=[{"goal_id": "understand", "label": title,
                          "checks": [{"check_id": key, "label": label} for key, label in checks]}],
                  parts=[{"part_id": "part-1", "text": LESSONS[cid][0],
                          "source_refs": lesson_refs[cid][0]},
                         {"part_id": "part-2", "text": LESSONS[cid][1],
                          "source_refs": lesson_refs[cid][1]}])
    for qid, cid, check, prompt, answer, question_refs in [
        ("q1", "chapter-1", "compute", "跑完 100 米用时 20 秒。平均速率是多少？解释公式和单位。",
         "5 米每秒，因为总路程除以总时间。", refs(2, 3, 9)),
        ("q2", "chapter-1", "boundary", "途中停留时，计算平均速率是否计入停留时间？说明理由。",
         "计入停留时间，使用全程总时间。", refs(2, 10)),
        ("q3", "chapter-2", "convert", "把 36 千米每小时换算为米每秒，并说明换算步骤。",
         "36 × 1000 ÷ 3600 = 10 米每秒。",
         refs(6, 7, 14)),
    ]:
        yield req("publish_question", qid, chapter_id=cid, content_version="v1",
                  question_id=qid, question_version="qv1", rubric_version="r1", prompt=prompt, checks=[check],
                  rubric={"verified": True, "criteria": {check: {
                      "required": answer, "alternatives": "等价的正确解释、分数或换算写法均可。",
                      "critical_errors": "公式、时间范围或单位错误不可判为符合依据。"}},
                      "reference_answer": answer, "source_refs": question_refs})


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

"""Render immutable snapshots; only the host can send requests to Codex."""

import json
import re
import uuid
from pathlib import Path

from .common import now, require
from .views import course_summary


def public_snapshot(state):
    """仅公开题干、作答状态和导航；不把评分依据嵌入未作答面板。"""
    chapters = []
    for cid in state["chapter_order"]:
        chapter = state["chapters"][cid]
        version = chapter["current_version"]
        content = chapter["versions"][version]
        chapters.append({"chapter_id": cid, "title": content["title"], "content_version": version,
                         "checkpoint": content["checkpoint"],
                         "parts": [{"part_id": p["part_id"]} for p in content["parts"]]})
    questions = []
    for qid, question in state["questions"].items():
        for chapter in chapters:
            qversion = question["active_versions"].get(chapter["content_version"])
            if qversion is None:
                continue
            q = question["versions"][qversion]
            if q["chapter_id"] != chapter["chapter_id"]:
                continue
            submissions = [s for s in state["submissions"].values()
                           if s["question_id"] == qid and s["content_version"] == q["content_version"]
                           and s["question_version"] == qversion]
            latest = max(submissions, key=lambda s: s["received_revision"]) if submissions else None
            questions.append({"question_id": qid, "question_version": qversion,
                              "chapter_id": q["chapter_id"], "content_version": q["content_version"],
                              "prompt": q["prompt"], "latest": {
                                  k: latest[k] for k in ("submission_id", "status", "received_at")
                              } if latest else None})
    return {"panel_id": uuid.uuid4().hex, "course_id": state["course_id"], "title": state["title"],
            "state_revision": state["state_revision"], "snapshot_at": now(), "position": state["position"],
            "chapters": chapters, "outline": state.get("outline", []), "questions": questions,
            "summary": course_summary(state)}


def render(state, output, fragment=False):
    output = Path(output)
    require(not output.exists(), "Panel snapshots are immutable; choose a new path.", "snapshot_exists")
    snapshot = public_snapshot(state)
    assets = Path(__file__).resolve().parents[2] / "assets"
    template = (assets / "panel.html").read_text(encoding="utf-8")
    payload = json.dumps(snapshot, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    values = {"SNAPSHOT": payload, "SCRIPT": (assets / "panel.js").read_text(encoding="utf-8")}
    document = re.sub(r"\{\{(SNAPSHOT|SCRIPT)\}\}", lambda match: values[match[1]], template)
    if not fragment:
        document = ('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
                    '<meta name="viewport" content="width=device-width, initial-scale=1">'
                    '<title>Codex 对话学习器 · 面板快照</title>'
                    '<style>:root{color-scheme:light dark}body{font-family:system-ui,sans-serif;'
                    'max-width:736px;margin:auto;padding:1rem}button,input,select,textarea{font:inherit}'
                    'button{padding:.55em .8em}textarea{padding:.5em;box-sizing:border-box;width:100%;min-height:8em}'
                    'a{color:LinkText}</style>'
                    '</head><body>' + document + '</body></html>')
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as stream:
        stream.write(document)
    return {"path": str(output.resolve()), "panel_id": snapshot["panel_id"], "fragment": fragment,
            "status": "snapshot_rendered", "host_verified": False}

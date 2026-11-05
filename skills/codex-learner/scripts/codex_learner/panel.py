"""Render immutable snapshots; only the host can send requests to Codex."""

import json
import re
import uuid
from pathlib import Path

from .common import now, require
from .views import chapter_summary, effective_assessment, effective_help


def brief_retest(value):
    """Only the small, recorded retest context belongs in a panel snapshot."""
    if not isinstance(value, dict):
        return None
    result = {key: value[key] for key in ("kind", "baseline_received_at", "elapsed_seconds")
              if key in value}
    context = value.get("context")
    if isinstance(context, str):
        result["context"] = context[:400]
        result["context_truncated"] = len(context) > 400
    return result or None


def public_snapshot(state):
    """仅公开题干、作答状态和导航；不把评分依据嵌入未作答面板。"""
    position = state["position"]
    current_id = position["chapter_id"] if position else None
    chapters = []
    current = None
    for cid in state["chapter_order"]:
        chapter = state["chapters"][cid]
        version = chapter["current_version"]
        content = chapter["versions"][version]
        entry = {"chapter_id": cid, "title": content["title"], "content_version": version}
        if cid == current_id:
            entry.update(checkpoint=content["checkpoint"],
                         parts=[{"part_id": p["part_id"]} for p in content["parts"]])
            current = entry
        chapters.append(entry)
    questions = []
    if current and current["checkpoint"]["stage"] == "exercises":
        for qid, question in state["questions"].items():
            qversion = question["active_versions"].get(current["content_version"])
            if qversion is None:
                continue
            q = question["versions"][qversion]
            if q["chapter_id"] != current_id:
                continue
            submissions = [s for s in state["submissions"].values()
                           if s["question_id"] == qid and s["content_version"] == q["content_version"]
                           and s["question_version"] == qversion]
            latest = max(submissions, key=lambda s: s["received_revision"]) if submissions else None
            receipt = None
            if latest:
                receipt = {k: latest[k] for k in ("submission_id", "status", "received_at")}
                receipt["help"] = effective_help(state, latest)
                assessment = effective_assessment(state, latest["submission_id"])
                if assessment and assessment.get("invalidated_checks"):
                    receipt["status"] = "pending_reassessment"
                receipt["retest"] = brief_retest(latest.get("retest"))
            questions.append({"question_id": qid, "question_version": qversion,
                              "chapter_id": q["chapter_id"], "content_version": q["content_version"],
                              "prompt": q["prompt"], "latest": receipt,
                              "retest_context": brief_retest(q.get("retest_context"))})
    summary = {"chapters": []}
    if current_id:
        current_summary = chapter_summary(state, current_id)
        # Long feedback and historical answers stay in the read-only review interface.
        for goal in current_summary["goals"]:
            goal["checks"] = [{key: check[key] for key in (
                "check_id", "label", "status", "help", "unassessed_reason") if key in check}
                for check in goal["checks"]]
        summary["chapters"].append(current_summary)
    return {"panel_id": uuid.uuid4().hex, "course_id": state["course_id"], "title": state["title"],
            "state_revision": state["state_revision"], "snapshot_at": now(), "position": position,
            "chapters": chapters, "outline": [
                {key: entry[key] for key in ("chapter_id", "title")}
                for entry in state.get("outline", [])], "questions": questions, "summary": summary}


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

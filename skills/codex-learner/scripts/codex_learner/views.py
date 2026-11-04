"""Derived views: evidence is selected by answer reception, not grading time."""

from datetime import datetime

from .common import body_hash, require

HELP_LEVELS = {"none": 0, "hint": 1, "solution": 2}
RETEST_KINDS = ("delayed", "transfer")
LEGACY_RETEST_KIND = "legacy_unclassified"
HISTORY_KINDS = ("immediate",) + RETEST_KINDS + (LEGACY_RETEST_KIND,)


def effective_help(state, submission):
    level = submission.get("help_snapshot", "none")
    for event in state["help_events"].values():
        if event["question_id"] != submission["question_id"]:
            continue
        target = event.get("target_submission_id")
        applies = (submission["received_revision"] >= state["submissions"][target]["received_revision"]
                   if target else event["recorded_revision"] < submission["received_revision"])
        if applies and HELP_LEVELS[event["level"]] > HELP_LEVELS[level]:
            level = event["level"]
    return level


def current_submission(state, submission):
    chapter = state["chapters"].get(submission["chapter_id"])
    question = state["questions"].get(submission["question_id"])
    return bool(chapter and question
                and chapter["current_version"] == submission["content_version"]
                and question["active_versions"].get(submission["content_version"]) == submission["question_version"]
                and submission["evaluation_scope"] == "current"
                and submission["status"] != "pending_content_review")


def effective_assessment(state, submission_id):
    candidates = [a for a in state["assessments"].values() if a["submission_id"] == submission_id]
    return max(candidates, key=lambda a: a["recorded_revision"]) if candidates else None


def assessment_index(state):
    """Index metadata once without opening any assessment or answer body."""
    result = {}
    for assessment in state["assessments"].values():
        sid = assessment["submission_id"]
        if sid not in result or assessment["recorded_revision"] > result[sid]["recorded_revision"]:
            result[sid] = assessment
    return result


def elapsed_seconds(earlier, later):
    """Compute observed time from persisted timestamps, never a supplied delay."""
    try:
        start, finish = (datetime.fromisoformat(value.replace("Z", "+00:00")) for value in (earlier, later))
        require(start.tzinfo is not None and finish.tzinfo is not None,
                "Retest timestamps must include a timezone.", "invalid_retest")
        elapsed = (finish - start).total_seconds()
    except (AttributeError, TypeError, ValueError) as exc:
        from .common import LearnerError
        raise LearnerError("invalid_retest", "Retest timestamps are invalid.") from exc
    require(elapsed >= 0, "Retest time precedes its baseline; check the system clock.", "invalid_retest")
    return elapsed


def chapter_summary(state, chapter_id, assessments=None):
    chapter = state["chapters"][chapter_id]
    content = chapter["versions"][chapter["current_version"]]
    assessments = assessment_index(state) if assessments is None else assessments
    latest = {}
    revised_checks = set()
    for sub in state["submissions"].values():
        if sub["chapter_id"] != chapter_id:
            continue
        question = state["questions"][sub["question_id"]]
        version = question["versions"][sub["question_version"]]
        if (sub["content_version"] == chapter["current_version"]
                and question["active_versions"].get(sub["content_version"]) != sub["question_version"]):
            revised_checks.update(version["checks"])
        if not current_submission(state, sub):
            continue
        for check_id in version["checks"]:
            if check_id not in latest or sub["received_revision"] > latest[check_id]["received_revision"]:
                latest[check_id] = sub
    checks = {}
    for goal in content["goals"]:
        for check in goal["checks"]:
            check_id = check["check_id"]
            row = {"check_id": check_id, "label": check["label"], "status": "unassessed"}
            if check_id in latest:
                sub = latest[check_id]
                assessment = assessments.get(sub["submission_id"])
                row.update(submission_id=sub["submission_id"], received_revision=sub["received_revision"],
                           help=effective_help(state, sub))
                if assessment is None:
                    row["status"] = "pending_assessment"
                elif check_id in assessment.get("invalidated_checks", []):
                    row["status"] = "pending_reassessment"
                else:
                    result = assessment["results"][check_id]
                    row["assessment_id"] = assessment["assessment_id"]
                    if result.get("reason_ref"):
                        row["reason_ref"] = result["reason_ref"]
                    else:
                        row["reason"] = result["reason"]
                    row["status"] = {"meets": "passed" if row["help"] == "none" else "assisted",
                                     "needs_work": "needs_work", "uncertain": "uncertain"}[result["outcome"]]
            elif check_id in revised_checks:
                row["unassessed_reason"] = "question_revised"
            checks[check_id] = row
    goals = []
    for goal in content["goals"]:
        items = [checks[c["check_id"]] for c in goal["checks"]]
        goals.append({"goal_id": goal["goal_id"], "label": goal["label"], "checks": items,
                      "passed_checks": sum(c["status"] == "passed" for c in items),
                      "required_checks": len(items), "status": "passed" if all(
                          c["status"] == "passed" for c in items) else "incomplete"})
    return {"chapter_id": chapter_id, "content_version": chapter["current_version"], "goals": goals,
            "status": "passed" if all(g["status"] == "passed" for g in goals) else "incomplete",
            "checkpoint": content["checkpoint"]}


def course_summary(state):
    assessments = assessment_index(state)
    pending = []
    for sub in state["submissions"].values():
        assessment = assessments.get(sub["submission_id"])
        if sub["status"] != "assessed" or (assessment and assessment["invalidated_checks"]):
            pending.append({"submission_id": sub["submission_id"],
                            "status": sub["status"] if sub["status"] != "assessed" else "pending_reassessment",
                            "current": current_submission(state, sub)})
    return {"course_id": state["course_id"], "title": state["title"], "state_revision": state["state_revision"],
            "position": state["position"],
            "chapters": [chapter_summary(state, cid, assessments) for cid in state["chapter_order"]],
            "pending": pending}


def _history_kind(state, submission):
    retest = submission.get("retest")
    if isinstance(retest, dict) and retest.get("kind") in RETEST_KINDS:
        return retest["kind"]
    question = state["questions"][submission["question_id"]]["versions"][submission["question_version"]]
    return LEGACY_RETEST_KIND if question.get("retest_context") else "immediate"


def _history_item(state, sub, assessment, include_bodies):
    item = {key: sub.get(key) for key in (
        "submission_id", "chapter_id", "content_version", "question_id", "question_version",
        "received_at", "received_revision", "status", "evaluation_scope", "replaces_submission_id")}
    item.update(kind=_history_kind(state, sub), current=current_submission(state, sub),
                help_snapshot=sub["help_snapshot"], effective_help=effective_help(state, sub),
                answer_sha256=body_hash(sub, "answer"))
    if sub.get("answer_ref"):
        item["answer_ref"] = sub["answer_ref"]
    if sub.get("retest"):
        retest = dict(sub["retest"])
        baseline = state["submissions"].get(retest["baseline_submission_id"])
        require(baseline is not None, "Retest baseline is missing.", "corrupt_state")
        retest["baseline_received_at"] = baseline["received_at"]
        retest["elapsed_seconds"] = elapsed_seconds(baseline["received_at"], sub["received_at"])
        item["retest"] = retest
    elif item["kind"] == LEGACY_RETEST_KIND:
        question = state["questions"][sub["question_id"]]["versions"][sub["question_version"]]
        item["legacy_retest_context"] = question["retest_context"]
        item["retest_warning"] = "Legacy context has no verified baseline; no delay or transfer result is inferred."
    if assessment is not None:
        results = {}
        for check_id, result in assessment["results"].items():
            row = {"outcome": result["outcome"], "source_refs": result["source_refs"]}
            row["status"] = ("pending_reassessment" if check_id in assessment["invalidated_checks"] else
                             {"meets": "passed" if item["effective_help"] == "none" else "assisted",
                              "needs_work": "needs_work", "uncertain": "uncertain"}[result["outcome"]])
            if result.get("reason_ref"):
                row["reason_ref"] = result["reason_ref"]
            if include_bodies:
                row["reason"] = result["reason"]
            results[check_id] = row
        item["assessment"] = {key: assessment.get(key) for key in (
            "assessment_id", "rubric_version", "recorded_at", "recorded_revision", "supersedes",
            "invalidated_checks", "feedback_help")}
        item["assessment"].update(results=results, feedback_sha256=body_hash(assessment, "feedback"))
        if assessment.get("feedback_ref"):
            item["assessment"]["feedback_ref"] = assessment["feedback_ref"]
        if include_bodies:
            item["assessment"]["feedback"] = assessment["feedback"]
    if include_bodies:
        item["answer"] = sub["answer"]
    return item


def _history(state, page, page_size, kind, include_bodies, retests_only):
    require(type(page) is int and page >= 1, "page must be a positive integer.")
    require(type(page_size) is int and 1 <= page_size <= 100, "page_size must be between 1 and 100.")
    allowed = RETEST_KINDS + (LEGACY_RETEST_KIND,) if retests_only else HISTORY_KINDS
    require(kind is None or kind in allowed, "Unknown history kind.")
    require(type(include_bodies) is bool, "include_bodies must be boolean.")
    groups = {name: 0 for name in allowed if name != LEGACY_RETEST_KIND}
    candidates = []
    for sub in state["submissions"].values():
        sub_kind = _history_kind(state, sub)
        if sub_kind not in allowed:
            continue
        groups[sub_kind] = groups.get(sub_kind, 0) + 1
        if kind is None or kind == sub_kind:
            candidates.append(sub)
    candidates.sort(key=lambda sub: sub["received_revision"], reverse=True)
    start = (page - 1) * page_size
    selected = candidates[start:start + page_size]
    assessments = assessment_index(state)
    return {"course_id": state["course_id"], "state_revision": state["state_revision"],
            "page": page, "page_size": page_size, "total": len(candidates), "groups": groups,
            "next_page": page + 1 if start + page_size < len(candidates) else None,
            "items": [_history_item(state, sub, assessments.get(sub["submission_id"]), include_bodies)
                      for sub in selected]}


def course_history(state, page=1, page_size=20, kind=None, include_bodies=False):
    """Page metadata first; opening bodies is explicit and limited to that page."""
    return _history(state, page, page_size, kind, include_bodies, False)


def retest_history(state, page=1, page_size=20, kind=None, include_bodies=False):
    """Separate delayed and transfer evidence without rewriting current mastery."""
    return _history(state, page, page_size, kind, include_bodies, True)


def submission_detail(state, submission_id):
    sub = state["submissions"].get(submission_id)
    require(sub is not None, "Unknown submission.", "unknown_target")
    return _history_item(state, sub, effective_assessment(state, submission_id), True)

"""Derived views: evidence is selected by answer reception, not grading time."""

HELP_LEVELS = {"none": 0, "hint": 1, "solution": 2}


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


def chapter_summary(state, chapter_id):
    chapter = state["chapters"][chapter_id]
    content = chapter["versions"][chapter["current_version"]]
    candidates = [s for s in state["submissions"].values()
                  if s["chapter_id"] == chapter_id and current_submission(state, s)]
    checks = {}
    for goal in content["goals"]:
        for check in goal["checks"]:
            check_id = check["check_id"]
            relevant = [s for s in candidates if check_id in
                        state["questions"][s["question_id"]]["versions"][s["question_version"]]["checks"]]
            row = {"check_id": check_id, "label": check["label"], "status": "unassessed"}
            if relevant:
                sub = max(relevant, key=lambda s: s["received_revision"])
                assessment = effective_assessment(state, sub["submission_id"])
                row.update(submission_id=sub["submission_id"], received_revision=sub["received_revision"],
                           help=effective_help(state, sub))
                if assessment is None:
                    row["status"] = "pending_assessment"
                elif check_id in assessment.get("invalidated_checks", []):
                    row["status"] = "pending_reassessment"
                else:
                    result = assessment["results"][check_id]
                    row.update(assessment_id=assessment["assessment_id"], reason=result["reason"])
                    row["status"] = {"meets": "passed" if row["help"] == "none" else "assisted",
                                     "needs_work": "needs_work", "uncertain": "uncertain"}[result["outcome"]]
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
    pending = []
    for sub in state["submissions"].values():
        assessment = effective_assessment(state, sub["submission_id"])
        if sub["status"] != "assessed" or (assessment and assessment["invalidated_checks"]):
            pending.append({"submission_id": sub["submission_id"],
                            "status": sub["status"] if sub["status"] != "assessed" else "pending_reassessment",
                            "current": current_submission(state, sub)})
    return {"course_id": state["course_id"], "title": state["title"], "state_revision": state["state_revision"],
            "position": state["position"],
            "chapters": [chapter_summary(state, cid) for cid in state["chapter_order"]],
            "pending": pending}

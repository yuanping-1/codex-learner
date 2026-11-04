"""Explicit business operations; generated lesson and grading text stays data."""

import copy

from .common import LearnerError, digest, identifier, nonempty, now, require
from .sources import archive_source, check_references, prepare_source
from .storage import write_artifact
from .views import HELP_LEVELS, current_submission, effective_assessment, effective_help


def context(state, request):
    chapter = state["chapters"].get(request.get("chapter_id"))
    require(chapter is not None, "Unknown chapter.", "unknown_target")
    content = chapter["versions"].get(request.get("content_version"))
    require(content is not None, "Unknown content version.", "unknown_target")
    return chapter, content


def question_context(state, request):
    chapter, content = context(state, request)
    question = state["questions"].get(request.get("question_id"))
    require(question is not None, "Unknown question.", "unknown_target")
    version = question["versions"].get(request.get("question_version"))
    require(version is not None and version["chapter_id"] == request["chapter_id"]
            and version["content_version"] == request["content_version"],
            "Question does not belong to the supplied chapter and content version.", "unknown_target")
    return chapter, content, question, version


def add_help(state, event_id, question_id, level, source, target=None, reason=None):
    identifier(event_id)
    require(event_id not in state["help_events"], "Help event already exists.", "id_conflict")
    require(level in HELP_LEVELS, "Unknown help level.")
    require(question_id in state["questions"], "Unknown question.", "unknown_target")
    if target is not None:
        sub = state["submissions"].get(target)
        require(sub is not None and sub["question_id"] == question_id, "Help correction target mismatch.")
        nonempty(reason, "correction reason")
        require(level != "none", "A retrospective disclosure must identify the help used.")
    state["help_events"][event_id] = {
        "event_id": event_id, "question_id": question_id, "level": level, "source": nonempty(source, "help source"),
        "target_submission_id": target, "reason": reason, "recorded_at": now(),
        "recorded_revision": state["state_revision"]}


def validate_rubric(state, rubric, checks):
    require(isinstance(rubric, dict) and rubric.get("verified") is True,
            "A rubric must be checked against its sources before publication.", "unverified_rubric")
    require(isinstance(rubric.get("criteria"), dict) and set(rubric["criteria"]) == set(checks),
            "Rubric criteria must cover each required check.")
    for criteria in rubric["criteria"].values():
        for field in ("required", "alternatives", "critical_errors"):
            nonempty(criteria.get(field), f"rubric {field}")
    nonempty(rubric.get("reference_answer"), "reference_answer")
    check_references(state, rubric.get("source_refs"))


class Engine:
    def __init__(self, library):
        self.library = library

    def apply(self, request):
        require(isinstance(request, dict), "Request must be a JSON object.")
        action = request.get("action")
        handler = getattr(self, f"op_{action}", None) if isinstance(action, str) else None
        require(handler is not None, "Unknown operation.")
        identifier(request.get("operation_id"))
        identifier(request.get("course_id"))
        try:
            if action == "import_source":
                # 原文件已移动时，同一导入请求的重试仍可返回已归档的结果。
                existing = self.library.load(request["course_id"])["operations"].get(request["operation_id"])
                if existing:
                    require(existing["fingerprint"] == digest(request), "Operation ID conflict.", "id_conflict")
                    return {**existing["receipt"], "duplicate": True}
            prepared = prepare_source(request) if action == "import_source" else None
            return self.library.transact(
                request, lambda state, directory: handler(state, directory, request, prepared),
                create=action == "create_course")
        except KeyError as exc:
            raise LearnerError("invalid_request", f"Missing required field: {exc.args[0]}") from exc
        except (TypeError, AttributeError) as exc:
            raise LearnerError("invalid_request", "Invalid field structure; preserve and correct the request.") from exc

    def op_create_course(self, state, directory, r, prepared):
        state["title"] = nonempty(r.get("title"), "title")
        state["goals"] = nonempty(r.get("goals"), "goals")
        outline = r.get("outline", [])
        require(isinstance(outline, list), "outline must be a list.")
        seen = set()
        for item in outline:
            cid = identifier(item["chapter_id"])
            require(cid not in seen, "Duplicate outline chapter.")
            nonempty(item.get("title"), "chapter title")
            nonempty(item.get("goals"), "chapter goals")
            seen.add(cid)
        state["outline"] = copy.deepcopy(outline)
        return {"status": "course_created"}

    def op_import_source(self, state, directory, r, prepared):
        return archive_source(state, directory, r, prepared)

    def op_verify_source(self, state, directory, r, prepared):
        source = state["sources"].get(r.get("source_id"))
        require(source is not None, "Unknown source.", "unknown_target")
        nonempty(r.get("evidence"), "visual verification evidence")
        locators = r.get("locators")
        require(isinstance(locators, list) and locators, "Specify reviewed locators.")
        for locator in locators:
            segment = next((s for s in source["segments"] if s["locator"] == locator), None)
            require(segment is not None and bool(segment["text"].strip()),
                    "Empty or missing text requires a separately archived transcription.")
            segment["verified"] = True
            segment["verification"] = {"evidence": r["evidence"], "recorded_at": now()}
        return {"source_id": r["source_id"], "verified_locators": locators}

    def op_publish_content(self, state, directory, r, prepared):
        chapter_id, version_id = identifier(r["chapter_id"]), identifier(r["content_version"])
        title = nonempty(r.get("title"), "chapter title")
        chapter = state["chapters"].get(chapter_id)
        require(not chapter or version_id not in chapter["versions"], "Content versions are immutable.", "id_conflict")
        goals, parts = r.get("goals"), r.get("parts")
        require(isinstance(goals, list) and goals and isinstance(parts, list) and parts,
                "Content needs goals and lecture parts.")
        check_ids, goal_ids, part_ids = set(), set(), set()
        for goal in goals:
            goal_id = identifier(goal["goal_id"])
            require(goal_id not in goal_ids, "Duplicate goal ID.")
            goal_ids.add(goal_id)
            nonempty(goal.get("label"), "goal label")
            require(isinstance(goal.get("checks"), list) and goal["checks"], "Each goal needs required checks.")
            for check in goal["checks"]:
                cid = identifier(check["check_id"])
                require(cid not in check_ids, "Check IDs must be unique within content.")
                check_ids.add(cid)
                nonempty(check.get("label"), "check label")
        saved_parts = []
        for part in parts:
            part_id = identifier(part["part_id"])
            require(part_id not in part_ids, "Duplicate lecture part.")
            part_ids.add(part_id)
            text = nonempty(part.get("text"), "lecture text")
            check_references(state, part.get("source_refs"))
            artifact = write_artifact(directory, f"lectures/{chapter_id}/{version_id}/{part_id}.md", text.encode("utf-8"))
            saved_parts.append({**copy.deepcopy(part), **artifact})
        if not chapter:
            chapter = {"title": title, "current_version": version_id, "versions": {}}
            state["chapters"][chapter_id] = chapter
            state["chapter_order"].append(chapter_id)
        chapter["versions"][version_id] = {
            "content_version": version_id, "title": title, "goals": copy.deepcopy(goals), "parts": saved_parts,
            "checkpoint": {"pending": None, "confirmed": [], "skipped": [], "stage": "lecture"},
            "published_at": now()}
        if state["position"] is None:
            state["position"] = {"chapter_id": chapter_id, "content_version": chapter["current_version"]}
        return {"chapter_id": chapter_id, "content_version": version_id,
                "adopted": chapter["current_version"] == version_id}

    def op_adopt_content(self, state, directory, r, prepared):
        chapter, content = context(state, r)
        chapter["current_version"] = r["content_version"]
        if state["position"] and state["position"]["chapter_id"] == r["chapter_id"]:
            state["position"]["content_version"] = r["content_version"]
        return {"status": "content_adopted", "content_version": r["content_version"]}

    def op_navigate(self, state, directory, r, prepared):
        chapter, content = context(state, r)
        require(chapter["current_version"] == r["content_version"], "Adopt content explicitly before navigation.")
        state["position"] = {"chapter_id": r["chapter_id"], "content_version": r["content_version"]}
        return {"position": state["position"]}

    def op_prepare_part(self, state, directory, r, prepared):
        chapter, content = context(state, r)
        require(state["position"] == {"chapter_id": r["chapter_id"], "content_version": r["content_version"]},
                "Navigate explicitly before teaching a different chapter.")
        cp = content["checkpoint"]
        remaining = [p["part_id"] for p in content["parts"] if p["part_id"] not in cp["confirmed"] + cp["skipped"]]
        require(remaining, "No remaining lecture part.")
        require(r["part_id"] == remaining[0], "Cannot skip an unconfirmed part without an explicit skip.")
        cp["pending"] = r["part_id"]
        return {"part": next(p for p in content["parts"] if p["part_id"] == r["part_id"]),
                "checkpoint": cp}

    def op_confirm_part(self, state, directory, r, prepared):
        chapter, content = context(state, r)
        cp = content["checkpoint"]
        require(r["part_id"] in [p["part_id"] for p in content["parts"]], "Unknown part.", "unknown_target")
        if (state["position"] != {"chapter_id": r["chapter_id"], "content_version": r["content_version"]}
                or cp["pending"] != r["part_id"]):
            return {"status": "already_confirmed" if r["part_id"] in cp["confirmed"] else "stale_confirmation",
                    "checkpoint": cp}
        cp["confirmed"].append(r["part_id"])
        cp["pending"] = None
        return {"status": "part_confirmed", "checkpoint": cp}

    def op_begin_exercises(self, state, directory, r, prepared):
        chapter, content = context(state, r)
        require(state["position"] == {"chapter_id": r["chapter_id"], "content_version": r["content_version"]},
                "Navigate explicitly before changing teaching stage.")
        cp = content["checkpoint"]
        remaining = [p["part_id"] for p in content["parts"] if p["part_id"] not in cp["confirmed"] + cp["skipped"]]
        if remaining and r.get("skip_remaining") is True:
            cp["skipped"].extend(remaining)
            cp["pending"] = None
        elif remaining and r.get("confirm_last") is True:
            require(len(remaining) == 1 and remaining[0] == cp["pending"],
                    "Only the prepared final part can be confirmed by this action.")
            cp["confirmed"].append(remaining[0])
            cp["pending"] = None
        else:
            require(not remaining, "Lecture is not confirmed; explicitly confirm or skip.", "lecture_incomplete")
        cp["stage"] = "exercises"
        return {"checkpoint": cp}

    def op_publish_question(self, state, directory, r, prepared):
        chapter, content = context(state, r)
        qid, qversion, rubric_version = (identifier(r[k]) for k in ("question_id", "question_version", "rubric_version"))
        question = state["questions"].get(qid)
        require(not question or qversion not in question["versions"], "Question versions are immutable.", "id_conflict")
        if question:
            require(all(v["chapter_id"] == r["chapter_id"] for v in question["versions"].values()),
                    "A question ID cannot be moved to another chapter.")
        checks = r.get("checks")
        known = {c["check_id"] for goal in content["goals"] for c in goal["checks"]}
        require(isinstance(checks, list) and checks and len(set(checks)) == len(checks) and set(checks) <= known,
                "Question checks must refer to this content's required checks.")
        validate_rubric(state, r.get("rubric"), checks)
        data = {key: r[key] for key in ("chapter_id", "content_version", "question_id", "question_version")}
        data.update(prompt=nonempty(r.get("prompt"), "question prompt"), checks=checks,
                    current_rubric=rubric_version, rubrics={rubric_version: copy.deepcopy(r["rubric"])},
                    retest_context=r.get("retest_context"), published_at=now())
        if not question:
            question = {"active_versions": {}, "versions": {}}
            state["questions"][qid] = question
        question["versions"][qversion] = data
        question["active_versions"][r["content_version"]] = qversion
        return {"question_id": qid, "question_version": qversion}

    def op_record_help(self, state, directory, r, prepared):
        question_context(state, r)
        add_help(state, r["event_id"], r["question_id"], r["level"], r["source"],
                 r.get("target_submission_id"), r.get("reason"))
        return {"event_id": r["event_id"], "level": r["level"]}

    def op_submit_answer(self, state, directory, r, prepared):
        chapter, content, question, version = question_context(state, r)
        sid = identifier(r["submission_id"])
        require(sid not in state["submissions"], "Submission ID already exists; retry its original request.", "id_conflict")
        answer = nonempty(r.get("answer"), "answer")
        previous = r.get("replaces_submission_id")
        if previous:
            old = state["submissions"].get(previous)
            require(old is not None and old["question_id"] == r["question_id"],
                    "Replacement must refer to an answer to the same question.")
        old_version = (chapter["current_version"] != r["content_version"]
                       or question["active_versions"].get(r["content_version"]) != r["question_version"])
        sub = {key: r[key] for key in ("course_id", "chapter_id", "content_version", "question_id", "question_version")}
        sub.update(submission_id=sid, answer=answer, received_at=now(), received_revision=state["state_revision"],
                   help_snapshot="none", evaluation_scope="current",
                   status="pending_content_review" if old_version else "pending_assessment",
                   replaces_submission_id=previous)
        sub["help_snapshot"] = effective_help(state, sub)
        state["submissions"][sid] = sub
        return {"submission_id": sid, "status": sub["status"], "answer": answer, "help": sub["help_snapshot"]}

    def op_resolve_history(self, state, directory, r, prepared):
        sub = state["submissions"].get(r.get("submission_id"))
        require(sub is not None, "Unknown submission.", "unknown_target")
        require(not current_submission(state, sub), "Current submissions cannot be excluded as history.")
        sub["evaluation_scope"] = "history_only"
        sub["status"] = "assessed" if effective_assessment(state, sub["submission_id"]) else "pending_assessment"
        return {"submission_id": sub["submission_id"], "evaluation_scope": "history_only"}

    def op_assess(self, state, directory, r, prepared):
        aid, sid = identifier(r["assessment_id"]), identifier(r["submission_id"])
        require(aid not in state["assessments"], "Assessment ID already exists.", "id_conflict")
        sub = state["submissions"].get(sid)
        require(sub is not None, "Unknown submission.", "unknown_target")
        require(sub["status"] != "pending_content_review", "Choose history-only evaluation for old content first.")
        require(current_submission(state, sub) or sub["evaluation_scope"] == "history_only",
                "Outdated submissions require explicit history-only evaluation.")
        _, _, _, question = question_context(state, sub)
        rubric_version = r["rubric_version"]
        require(rubric_version == question["current_rubric"], "Use the current checked rubric.")
        results = r.get("results")
        require(isinstance(results, dict) and set(results) == set(question["checks"]),
                "Assessment must address every check covered by the question.")
        for result in results.values():
            require(result.get("outcome") in ("meets", "needs_work", "uncertain"), "Unknown assessment outcome.")
            nonempty(result.get("reason"), "assessment reason")
            check_references(state, result.get("source_refs"))
        existing = effective_assessment(state, sid)
        require(r.get("supersedes") == (existing["assessment_id"] if existing else None),
                "Corrections must reference the currently effective assessment.", "assessment_conflict")
        if existing:
            nonempty(r.get("correction_reason"), "correction_reason")
        help_level = r.get("feedback_help")
        require(help_level in HELP_LEVELS, "Explicitly classify feedback help: none, hint, or solution.")
        state["assessments"][aid] = {
            "assessment_id": aid, "submission_id": sid, "rubric_version": rubric_version,
            "results": copy.deepcopy(results), "feedback": nonempty(r.get("feedback"), "feedback"),
            "feedback_help": help_level, "supersedes": r.get("supersedes"),
            "correction_reason": r.get("correction_reason"), "invalidated_checks": [],
            "recorded_revision": state["state_revision"], "recorded_at": now()}
        # 反馈帮助与评价在同一事务内登记；接收原答的帮助快照保持不变。
        add_help(state, "feedback-" + digest(aid), sub["question_id"], help_level, "feedback")
        sub["status"] = "assessed"
        return {"submission_id": sid, "assessment_id": aid, "status": "feedback_recorded",
                "evaluation_scope": sub["evaluation_scope"]}

    def op_replace_rubric(self, state, directory, r, prepared):
        _, _, _, question = question_context(state, r)
        new_version = identifier(r["rubric_version"])
        require(new_version not in question["rubrics"], "Rubric versions are immutable.", "id_conflict")
        validate_rubric(state, r.get("rubric"), question["checks"])
        affected = r.get("affected_checks")
        require(isinstance(affected, list) and affected and set(affected) <= set(question["checks"]),
                "Name affected checks.")
        nonempty(r.get("reason"), "rubric correction reason")
        question["rubrics"][new_version] = {**copy.deepcopy(r["rubric"]), "correction_reason": r["reason"]}
        question["current_rubric"] = new_version
        invalidated = []
        for assessment in state["assessments"].values():
            sub = state["submissions"][assessment["submission_id"]]
            if sub["question_id"] == r["question_id"] and sub["question_version"] == r["question_version"]:
                assessment["invalidated_checks"] = sorted(set(assessment["invalidated_checks"]) | set(affected))
                invalidated.append(assessment["assessment_id"])
        return {"rubric_version": new_version, "invalidated_assessments": invalidated}

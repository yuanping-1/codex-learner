"""Validation and serialization shared by storage and operations."""

import hashlib
import json
import re
from datetime import datetime, timezone


SCHEMA_VERSION = 2
READ_SCHEMAS = (1, 2)


class LearnerError(Exception):
    def __init__(self, code, message, **details):
        super().__init__(message)
        self.code = code
        self.details = details

    def as_dict(self):
        return {"error": self.code, "message": str(self), **self.details}


def require(condition, message, code="invalid_request"):
    if not condition:
        raise LearnerError(code, message)


def identifier(value):
    require(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,95}", value),
            "IDs must contain 1–96 ASCII letters, digits, underscores or hyphens.")
    return value


def nonempty(value, field):
    require(isinstance(value, str) and bool(value.strip()), f"{field} must be nonempty text.")
    return value


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat()


def read_json(path):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"Duplicate JSON key: {key}")
            result[key] = value
        return result
    try:
        return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=pairs,
                          parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    except (ValueError, UnicodeError) as exc:
        raise LearnerError("invalid_json", f"Invalid JSON: {path}", path=str(path)) from exc


def has_body(record, field, nonempty=False):
    # Inspect raw keys: validation must not hydrate every historical body.
    if field in record:
        value = dict.__getitem__(record, field)
        return isinstance(value, str) and (bool(value.strip()) if nonempty else True)
    ref = dict.get(record, field + "_ref")
    return (isinstance(ref, dict) and isinstance(ref.get("path"), str)
            and isinstance(ref.get("sha256"), str)
            and bool(re.fullmatch("[0-9a-f]{64}", ref["sha256"]))
            and (not nonempty or ref.get("byte_length", 1) > 0))


def body_hash(record, field):
    if field in record:
        value = dict.__getitem__(record, field)
        return hashlib.sha256(value.encode("utf-8")).hexdigest()
    ref = dict.get(record, field + "_ref")
    require(isinstance(ref, dict), f"Missing body: {field}", "corrupt_state")
    return ref["sha256"]


def validate_state(state, course_id=None):
    require(isinstance(state, dict), "Course state is not an object.", "corrupt_state")
    require(state.get("schema_version") in READ_SCHEMAS, "Unsupported course schema; no automatic migration.",
            "unsupported_schema")
    identifier(state.get("course_id"))
    if course_id is not None:
        require(state["course_id"] == course_id, "Course identity mismatch.", "corrupt_state")
    require(type(state.get("state_revision")) is int and state["state_revision"] >= 1,
            "Invalid state revision.", "corrupt_state")
    nonempty(state.get("title"), "title")
    for key in ("chapters", "sources", "questions", "submissions", "assessments", "help_events", "operations"):
        require(isinstance(state.get(key), dict), f"Missing or invalid {key}.", "corrupt_state")
    require(isinstance(state.get("chapter_order"), list), "Missing chapter order.", "corrupt_state")
    require(len(set(state["chapter_order"])) == len(state["chapter_order"])
            and set(state["chapter_order"]) == set(state["chapters"]),
            "Chapter order does not match chapters.", "corrupt_state")
    position = state.get("position")
    if position is not None:
        require(isinstance(position, dict) and position.get("chapter_id") in state["chapters"],
                "Invalid learning position.", "corrupt_state")
        chapter = state["chapters"][position["chapter_id"]]
        require(position.get("content_version") == chapter["current_version"],
                "Learning position is not on adopted content.", "corrupt_state")
    for sid, sub in state["submissions"].items():
        require(sub.get("submission_id") == sid and sub.get("course_id") == state["course_id"],
                "Submission identity mismatch.", "corrupt_state")
        require(has_body(sub, "answer") and type(sub.get("received_revision")) is int,
                "Invalid submission.", "corrupt_state")
    try:
        validate_links(state)
    except (KeyError, TypeError, AttributeError, ValueError, IndexError) as exc:
        raise LearnerError("corrupt_state", "Course structure is incomplete or inconsistent; preserve it and inspect backup.") from exc
    return state


def validate_links(state):
    """嵌套记录损坏不能通过校验后覆盖最后一个有效备份。"""
    def valid(condition, message):
        require(condition, message, "corrupt_state")

    def refs(references):
        valid(isinstance(references, list) and bool(references), "Missing source references.")
        for ref in references:
            source = state["sources"][ref["source_id"]]
            valid(any(s["locator"] == ref["locator"] and has_body(s, "text", nonempty=True)
                      and (state["schema_version"] == 2 or s["verified"])
                      for s in source["segments"]), "Unverified or missing source location.")

    checks_by_content = {}
    for source_id, source in state["sources"].items():
        valid(source["source_id"] == source_id and isinstance(source["segments"], list), "Invalid source record.")
        valid(isinstance(source["path"], str) and bool(re.fullmatch("[0-9a-f]{64}", source["sha256"])),
              "Invalid source artifact.")
        locators = [s["locator"] for s in source["segments"]]
        valid(len(set(locators)) == len(locators), "Duplicate source locators.")
        valid(all(has_body(s, "text") and type(s["verified"]) is bool for s in source["segments"]),
              "Invalid source segment.")
    for cid, chapter in state["chapters"].items():
        valid(chapter["current_version"] in chapter["versions"], "Adopted content is missing.")
        for version, content in chapter["versions"].items():
            valid(content["content_version"] == version and bool(content["goals"]), "Invalid chapter content.")
            check_ids = [c["check_id"] for g in content["goals"] for c in g["checks"]]
            goal_ids = [g["goal_id"] for g in content["goals"]]
            valid(len(set(goal_ids)) == len(goal_ids) and all(g["checks"] for g in content["goals"]),
                  "Duplicate goals or empty required checks.")
            valid(len(set(check_ids)) == len(check_ids), "Duplicate required check IDs.")
            checks_by_content[(cid, version)] = set(check_ids)
            parts = [p["part_id"] for p in content["parts"]]
            valid(parts and len(set(parts)) == len(parts), "Invalid lecture parts.")
            for part in content["parts"]:
                valid(has_body(part, "text", nonempty=True), "Invalid lecture text.")
                refs(part["source_refs"])
            cp = content["checkpoint"]
            confirmed, skipped = cp["confirmed"], cp["skipped"]
            valid(isinstance(confirmed, list) and isinstance(skipped, list), "Invalid checkpoint.")
            valid(len(set(confirmed + skipped)) == len(confirmed + skipped)
                  and set(confirmed + skipped) <= set(parts), "Conflicting lecture checkpoints.")
            valid(cp["pending"] is None or cp["pending"] in set(parts) - set(confirmed + skipped),
                  "Pending lecture part conflicts with checkpoints.")
            valid(cp["stage"] in ("lecture", "exercises"), "Invalid teaching stage.")
            valid(cp["stage"] != "exercises" or set(confirmed + skipped) == set(parts),
                  "Exercises entered without explicit reading or skipping.")
    for qid, question in state["questions"].items():
        for version, q in question["versions"].items():
            valid(q["question_id"] == qid and q["question_version"] == version, "Question identity mismatch.")
            valid(q["checks"] and set(q["checks"]) <= checks_by_content[(q["chapter_id"], q["content_version"])],
                  "Question references unknown checks.")
            valid(q["current_rubric"] in q["rubrics"], "Current rubric is missing.")
            for rubric in q["rubrics"].values():
                valid(rubric["verified"] is True and set(rubric["criteria"]) == set(q["checks"]), "Invalid rubric.")
                refs(rubric["source_refs"])
        for content_version, qversion in question["active_versions"].items():
            valid(question["versions"][qversion]["content_version"] == content_version, "Active question version mismatch.")
    for sid, sub in state["submissions"].items():
        q = state["questions"][sub["question_id"]]["versions"][sub["question_version"]]
        valid(q["chapter_id"] == sub["chapter_id"] and q["content_version"] == sub["content_version"],
              "Submission target mismatch.")
        valid(sub["status"] in ("pending_assessment", "pending_content_review", "assessed")
              and sub["evaluation_scope"] in ("current", "history_only")
              and sub["help_snapshot"] in ("none", "hint", "solution"), "Invalid submission status.")
        valid(1 <= sub["received_revision"] <= state["state_revision"], "Invalid submission ordering.")
    for eid, event in state["help_events"].items():
        valid(event["event_id"] == eid and event["question_id"] in state["questions"]
              and event["level"] in ("none", "hint", "solution"), "Invalid help event.")
        valid(type(event["recorded_revision"]) is int and 1 <= event["recorded_revision"] <= state["state_revision"],
              "Invalid help event ordering.")
        if event["target_submission_id"]:
            valid(state["submissions"][event["target_submission_id"]]["question_id"] == event["question_id"],
                  "Help correction target mismatch.")
    chains = {}
    for aid, assessment in sorted(state["assessments"].items(), key=lambda item: item[1]["recorded_revision"]):
        sub = state["submissions"][assessment["submission_id"]]
        q = state["questions"][sub["question_id"]]["versions"][sub["question_version"]]
        valid(assessment["assessment_id"] == aid and assessment["rubric_version"] in q["rubrics"],
              "Invalid assessment identity or rubric.")
        valid(assessment["supersedes"] == chains.get(sub["submission_id"]), "Broken assessment correction chain.")
        chains[sub["submission_id"]] = aid
        valid(set(assessment["results"]) == set(q["checks"])
              and set(assessment["invalidated_checks"]) <= set(q["checks"]), "Invalid assessment checks.")
        for result in assessment["results"].values():
            valid(result["outcome"] in ("meets", "needs_work", "uncertain") and has_body(result, "reason", nonempty=True),
                  "Invalid assessment result.")
            refs(result["source_refs"])
    for operation in state["operations"].values():
        valid(bool(re.fullmatch("[0-9a-f]{64}", operation["fingerprint"]))
              and (("receipt_ref" in operation and has_body(operation, "receipt"))
                   or ("receipt" in operation and operation["receipt"]["course_id"] == state["course_id"])), "Invalid operation record.")

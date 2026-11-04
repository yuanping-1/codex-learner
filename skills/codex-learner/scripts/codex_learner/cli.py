"""JSON requests from files, JSON receipts to stdout, no answer execution."""

import argparse
import json
import sys
from pathlib import Path

from .common import LearnerError, read_json, require
from .operations import Engine
from .panel import render
from .storage import Library
from .views import course_summary


def parser():
    root = argparse.ArgumentParser(description="Codex learner record interface")
    root.add_argument("--library", help="Absolute library root; overrides the installed config")
    root.add_argument("--config", type=Path, help="Installed skill configuration")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="Explicitly initialize an empty library")
    commands.add_parser("list", help="List courses, including any corrupt entries")
    commands.add_parser("doctor", help="Check installed configuration and optional readers")
    apply = commands.add_parser("apply", help="Apply one explicit JSON operation")
    apply.add_argument("--request", type=Path, required=True)
    for action in ("show", "review", "backup-info"):
        cmd = commands.add_parser(action)
        cmd.add_argument("--course", required=True)
    panel = commands.add_parser("render")
    panel.add_argument("--course", required=True)
    panel.add_argument("--output", required=True, type=Path)
    panel.add_argument("--fragment", action="store_true", help="Use inside the host visualization renderer")
    restore = commands.add_parser("restore")
    restore.add_argument("--course", required=True)
    restore.add_argument("--backup-sha256", required=True)
    restore.add_argument("--confirm", action="store_true", help="Use only after explicit user choice")
    return root


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = parser().parse_args(argv)
    try:
        if args.library:
            library_path = args.library
        else:
            config_path = args.config or Path(__file__).resolve().parents[2] / "config.json"
            require(config_path.is_file(), "Install the skill or specify --library; no implicit empty library.",
                    "config_missing")
            config = read_json(config_path)
            require(isinstance(config, dict) and config.get("schema_version") == 1, "Unsupported skill config.")
            library_path = config.get("library_root")
            require(isinstance(library_path, str), "Configuration has no library_root.")
        library = Library(library_path)
        if args.command == "init":
            result = library.initialize()
        elif args.command == "list":
            result = library.list_courses()
        elif args.command == "doctor":
            import importlib.util
            import shutil
            library.check()
            result = {"library": str(library.root), "library_accessible": True, "python": sys.version.split()[0],
                      "pdf_reader": "pypdf" if importlib.util.find_spec("pypdf") else shutil.which("pdftotext"),
                      "host_submission": "requires_real_client_validation",
                      "new_conversation_discovery": "requires_real_client_validation"}
        elif args.command == "apply":
            request = read_json(args.request)
            require(isinstance(request, dict), "Request must be a JSON object.")
            if request.get("action") == "review":
                require(isinstance(request.get("course_id"), str), "review requires a course_id.")
                result = course_summary(library.load(request["course_id"]))
            else:
                result = Engine(library).apply(request)
        elif args.command == "show":
            result = library.load(args.course)
        elif args.command == "review":
            result = course_summary(library.load(args.course))
        elif args.command == "render":
            result = render(library.load(args.course), args.output, args.fragment)
        elif args.command == "backup-info":
            result = library.backup_info(args.course)
        else:
            result = library.restore(args.course, args.backup_sha256, args.confirm)
        print(json.dumps({"ok": True, "result": result}, ensure_ascii=False, indent=2))
        return 0
    except (LearnerError, OSError) as exc:
        error = exc.as_dict() if isinstance(exc, LearnerError) else {"error": "io_error", "message": str(exc)}
        print(json.dumps({"ok": False, **error}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

"""Explicit JSON operations, targeted reads, migration and archival lifecycle."""
import argparse
import json
import sys
from pathlib import Path

from .common import LearnerError, identifier, read_json, require
from .operations import Engine
from .panel import render
from .storage import Library, atomic_write_json
from .views import course_summary, course_history, retest_history


def parser():
    root = argparse.ArgumentParser(description="Codex learner record interface")
    root.add_argument("--library", help="Absolute library root; overrides installed config")
    root.add_argument("--config", type=Path, help="Installed skill configuration")
    commands = root.add_subparsers(dest="command", required=True)
    for action in ("init", "list", "doctor", "archive-list"):
        commands.add_parser(action)
    apply = commands.add_parser("apply")
    apply.add_argument("--request", type=Path, required=True)
    for action in ("show", "review", "backup-info", "read", "history", "retests", "export", "migrate", "archive-create"):
        cmd = commands.add_parser(action)
        cmd.add_argument("--course", required=True)
        if action == "read":
            for name in ("submission", "source", "locator", "chapter", "content-version", "part", "question", "question-version"):
                cmd.add_argument("--" + name)
        if action in ("history", "retests"):
            cmd.add_argument("--page", type=int, default=1)
            cmd.add_argument("--page-size", type=int, default=20)
            cmd.add_argument("--kind")
            cmd.add_argument("--include-bodies", action="store_true")
        if action in ("export", "archive-create"):
            cmd.add_argument("--output", type=Path, required=True)
        if action == "migrate":
            cmd.add_argument("--preview", action="store_true")
            cmd.add_argument("--expected-sha256")
            cmd.add_argument("--confirm", action="store_true")
    panel = commands.add_parser("render")
    panel.add_argument("--course", required=True)
    panel.add_argument("--output", required=True, type=Path)
    panel.add_argument("--fragment", action="store_true")
    restore = commands.add_parser("restore")
    restore.add_argument("--course", required=True)
    restore.add_argument("--backup-sha256", required=True)
    restore.add_argument("--confirm", action="store_true")
    for action in ("archive-check", "archive-copy", "archive-confirm-download", "archive-release", "archive-restore"):
        cmd = commands.add_parser(action)
        cmd.add_argument("--archive", required=True, type=Path)
        if action in ("archive-check", "archive-confirm-download", "archive-restore"):
            cmd.add_argument("--sha256", required=action != "archive-check")
        if action == "archive-copy":
            cmd.add_argument("--drive-directory", type=Path)
        if action == "archive-confirm-download":
            cmd.add_argument("--download", required=True, type=Path)
            cmd.add_argument("--source-declaration", choices=["web_download"], required=True)
            cmd.add_argument("--output", required=True, type=Path, help="Persist the verification receipt")
        if action == "archive-release":
            cmd.add_argument("--course", required=True)
            cmd.add_argument("--verification", required=True, type=Path)
        if action in ("archive-release", "archive-restore"):
            cmd.add_argument("--confirm", action="store_true")
    task = commands.add_parser("task-cleanup")
    task.add_argument("--course", required=True)
    task.add_argument("--task", required=True)
    task.add_argument("--confirm", action="store_true")
    return root


def _configuration(args):
    config_path = args.config or Path(__file__).resolve().parents[2] / "config.json"
    require(args.config is None or config_path.is_file(), "Explicit configuration file is missing.", "config_missing")
    config = read_json(config_path) if config_path.is_file() else {}
    if config_path.is_file():
        require(isinstance(config, dict) and config.get("schema_version") == 1, "Unsupported skill config.")
    for section in ("ocr", "docling", "drive", "memory"):
        require(isinstance(config.get(section, {}), dict), f"Configuration {section} must be an object.")
    for provider in ("ocr", "docling"):
        budget = config.get(provider, {}).get("memory_limit_mb", 2048)
        require(type(budget) is int and 64 <= budget <= 2048,
                f"Configuration {provider}.memory_limit_mb must be 64–2048 MiB.")
    library_path = args.library or config.get("library_root")
    require(isinstance(library_path, str), "Install the skill or specify --library; no implicit empty library.", "config_missing")
    return Library(library_path), config, config_path


def _drive_path(config):
    drive = config.get("drive", {})
    return drive.get("directory") or drive.get("path") or drive.get("archive_directory")


def _archive(args, library, config):
    from . import archives
    if args.command == "archive-list":
        return archives.archived_courses(library)
    if args.command == "archive-create":
        return archives.create_archive(library, args.course, args.output)
    if args.command == "archive-check":
        return archives.inspect_archive(args.archive, args.sha256)
    if args.command == "archive-copy":
        destination = args.drive_directory or _drive_path(config)
        require(destination, "Specify a private Drive streaming directory.", "drive_unconfigured")
        root = Path(destination).expanduser().resolve()
        require(not (library.root.is_relative_to(root) or root.is_relative_to(library.root)),
                "Drive archive directory must be separate from the active library.", "invalid_path")
        return archives.copy_to_drive(args.archive, destination)
    if args.command == "archive-confirm-download":
        forbidden = [p for p in (_drive_path(config), config.get("ocr", {}).get("cache_dir"),
                                 config.get("ocr", {}).get("model_dir")) if p]
        result = archives.confirm_download(args.download, args.sha256, source_declaration=args.source_declaration,
                                           archive_path=args.archive, forbidden_paths=forbidden)
        require(not args.output.exists(), "Verification receipt already exists.", "artifact_conflict")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(args.output, result)
        return result
    if args.command == "archive-release":
        return archives.release_course(library, args.course, args.archive, read_json(args.verification), confirmed=args.confirm)
    return archives.restore_archive(library, args.archive, args.sha256, confirmed=args.confirm)


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = parser().parse_args(argv)
    try:
        library, config, config_path = _configuration(args)
        if args.command == "init":
            result = library.initialize()
        elif args.command == "list":
            result = library.list_courses()
        elif args.command == "doctor":
            from .diagnostics import doctor
            result = doctor(library, config, config_path)
        elif args.command == "apply":
            require(args.request.stat().st_size <= 32 * 1024 * 1024, "Request exceeds 32 MiB safety limit.", "request_too_large")
            request = read_json(args.request)
            require(isinstance(request, dict), "Request must be a JSON object.")
            result = (course_summary(library.load(identifier(request.get("course_id")))) if request.get("action") == "review"
                      else Engine(library, runtime_config=config).apply(request))
        elif args.command == "show":
            result = library.load_manifest(args.course)
        elif args.command == "review":
            result = course_summary(library.load(args.course))
        elif args.command == "read":
            from .reads import targeted_read
            result = targeted_read(library.load(args.course), **{name: getattr(args, name) for name in
                ("submission", "source", "locator", "chapter", "content_version", "part", "question", "question_version")})
        elif args.command in ("history", "retests"):
            function = course_history if args.command == "history" else retest_history
            result = function(library.load(args.course), args.page, args.page_size, args.kind, args.include_bodies)
        elif args.command == "export":
            from .reads import export_course
            result = export_course(library, args.course, args.output)
        elif args.command == "migrate":
            from .migration import preview_migration, migrate_course
            require(not (args.preview and args.confirm), "Choose preview or confirmed migration.")
            if args.confirm:
                require(args.expected_sha256, "Confirmed migration must bind the preview source SHA-256.")
                result = migrate_course(library, args.course, args.expected_sha256, confirmed=True)
            else:
                result = preview_migration(library, args.course)
        elif args.command == "render":
            result = render(library.load(args.course), args.output, args.fragment)
        elif args.command == "backup-info":
            result = library.backup_info(args.course)
        elif args.command == "task-cleanup":
            from .storage import cleanup_task
            result = cleanup_task(library, args.course, args.task, args.confirm)
        elif args.command == "restore":
            result = library.restore(args.course, args.backup_sha256, args.confirm)
        else:
            result = _archive(args, library, config)
        print(json.dumps({"ok": True, "result": result}, ensure_ascii=False, indent=2))
        return 0
    except (LearnerError, OSError) as exc:
        error = exc.as_dict() if isinstance(exc, LearnerError) else {"error": "io_error", "message": str(exc)}
        print(json.dumps({"ok": False, **error}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

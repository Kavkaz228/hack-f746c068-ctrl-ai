"""Operator commands: python -m backend.manage <command> --help.

Uses the same CAREER_QUEST_DATABASE_URL / CAREER_QUEST_STATE_PATH as the server.
Passwords are never accepted as command-line arguments (they would end up in
shell history); use --password-stdin or let the command generate one.
"""

import argparse
import getpass
import json
from pathlib import Path
import sqlite3
import sys

from backend.accounts import AccountError, AccountStore
from backend.data_loader import DEFAULT_DATA_DIR, DatasetValidationError, load_dataset
from backend.db import Database
from backend.settings import Settings, database_url_from_env
from backend.storage import SYSTEM, DatasetStore


def _open(args):
    url = database_url_from_env(args.database) if args.database else database_url_from_env()
    database = Database(url)
    store = DatasetStore(database, load_dataset(args.data_dir))
    settings = Settings.from_env()
    accounts = AccountStore(database, session_hours=settings.session_hours,
                            session_idle_minutes=settings.session_idle_minutes)
    return database, store, accounts


def _password(args) -> str | None:
    if not getattr(args, "password_stdin", False):
        return None
    if sys.stdin.isatty():
        first = getpass.getpass("Password: ")
        if first != getpass.getpass("Repeat password: "):
            raise SystemExit("Passwords do not match")
        return first
    return sys.stdin.readline().rstrip("\n")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m backend.manage", description=__doc__.splitlines()[0])
    parser.add_argument("--database", help="Database URL or SQLite path (default: environment)")
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR), help="Seed used only if the database is empty")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("init-db", help="Apply migrations and seed an empty database")

    create = commands.add_parser("create-user", help="Create an HR or employee account")
    create.add_argument("--email", required=True)
    create.add_argument("--name", required=True)
    create.add_argument("--role", choices=["hr", "employee"], required=True)
    create.add_argument("--employee-id")
    create.add_argument("--password-stdin", action="store_true",
                        help="Read the password from stdin; otherwise a temporary one is generated")
    create.add_argument("--no-change-required", action="store_true",
                        help="Do not force a password change at first sign-in (use only with --password-stdin)")

    reset = commands.add_parser("reset-password", help="Issue a new temporary password")
    reset.add_argument("--email", required=True)

    commands.add_parser("list-users", help="List accounts (no secrets)")

    export = commands.add_parser("export", help="Write the full dataset as JSON")
    export.add_argument("--output", required=True)

    importer = commands.add_parser("import-dir", help="Replace the dataset with a folder of JSON/CSV files")
    importer.add_argument("directory")

    purge = commands.add_parser("purge-audit", help="Delete audit entries older than N days (retention policy)")
    purge.add_argument("--days", type=int, required=True)

    backup = commands.add_parser("backup-sqlite", help="Consistent online copy of a SQLite database")
    backup.add_argument("--output", required=True)

    args = parser.parse_args(argv)
    try:
        if args.command == "backup-sqlite":
            url = database_url_from_env(args.database) if args.database else database_url_from_env()
            if not url.startswith("sqlite:///"):
                raise SystemExit("backup-sqlite works only with SQLite; use pg_dump for PostgreSQL")
            source = sqlite3.connect(url.split(":///", 1)[1])
            target = sqlite3.connect(args.output)
            with target:
                source.backup(target)
            source.close()
            target.close()
            print(json.dumps({"status": "ok", "output": args.output}))
            return

        database, store, accounts = _open(args)
        try:
            if args.command == "init-db":
                info = store.state_info()
                print(json.dumps({"status": "ready", "data_version": info["version"],
                                  "employees": len(store.list_employees()), "users": accounts.count()}))
            elif args.command == "create-user":
                password = _password(args)
                if args.role == "employee" and not store.employee_exists(args.employee_id or ""):
                    raise SystemExit(f"Employee profile '{args.employee_id}' not found")
                user, generated = accounts.create(
                    email=args.email, name=args.name, role=args.role, employee_id=args.employee_id,
                    password=password, must_change_password=not (args.no_change_required and password))
                store.audit(SYSTEM, "user_created", user.user_id, {"role": user.role, "via": "cli"})
                result = {"status": "created", "user": user.public()}
                if generated:
                    result["temporary_password"] = generated
                print(json.dumps(result, ensure_ascii=False, indent=2))
            elif args.command == "reset-password":
                user = accounts.by_email(args.email)
                if user is None:
                    raise SystemExit("User not found")
                user, password = accounts.reset_password(user.user_id)
                store.audit(SYSTEM, "password_reset", user.user_id, {"via": "cli"})
                print(json.dumps({"status": "reset", "email": user.email, "temporary_password": password}))
            elif args.command == "list-users":
                print(json.dumps([user.public() for user in accounts.list()
                                  if not user.email.endswith("@service.invalid")], ensure_ascii=False, indent=2))
            elif args.command == "export":
                Path(args.output).write_text(store.snapshot().model_dump_json(indent=2), encoding="utf-8")
                print(json.dumps({"status": "exported", "output": args.output}))
            elif args.command == "purge-audit":
                if args.days < 30:
                    raise SystemExit("Keep at least 30 days of audit history")
                from datetime import timedelta
                from sqlalchemy import delete
                from backend import db as schema
                from backend.db import utcnow
                with database.write_tx() as connection:
                    removed = connection.execute(delete(schema.audit_log).where(
                        schema.audit_log.c.at < utcnow() - timedelta(days=args.days))).rowcount
                    connection.execute(delete(schema.sessions).where(schema.sessions.c.expires_at < utcnow()))
                store.audit(SYSTEM, "audit_purged", None, {"older_than_days": args.days, "removed": removed})
                print(json.dumps({"status": "purged", "removed": removed}))
            elif args.command == "import-dir":
                dataset = load_dataset(args.directory)
                result = store.update(lambda _current: (dataset, {"status": "imported", "mode": "replace",
                                                                  "employees": len(dataset.employees)}),
                                      replace=True, action="dataset_import_replace",
                                      snapshot_reason="before_import_replace")
                print(json.dumps(result))
        finally:
            database.dispose()
    except (AccountError, DatasetValidationError) as exc:
        raise SystemExit(f"{getattr(exc, 'code', 'error')}: {exc}") from exc


if __name__ == "__main__":
    main()

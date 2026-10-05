"""Operator CLI: accounts, export/import, retention, SQLite backup."""

import contextlib
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

from backend.manage import main


class ManageTests(unittest.TestCase):
    def run_cli(self, *args, stdin=None):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), mock.patch("sys.stdin", io.StringIO(stdin or "")):
            main(["--database", self.database, *args])
        return output.getvalue()

    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.database = str(self.root / "state.sqlite3")

    def test_accounts_export_import_purge_and_backup(self):
        self.assertEqual(json.loads(self.run_cli("init-db"))["employees"], 48)
        created = json.loads(self.run_cli("create-user", "--email", "Ops.HR@Example.test", "--name", "Ops", "--role", "hr"))
        self.assertEqual(created["user"]["email"], "ops.hr@example.test")
        self.assertTrue(created["user"]["must_change_password"])
        self.assertIn("temporary_password", created)
        typed = json.loads(self.run_cli("create-user", "--email", "e1@example.test", "--name", "E1", "--role", "employee",
                                        "--employee-id", "E001", "--password-stdin", "--no-change-required",
                                        stdin="Typed-pass-2026\n"))
        self.assertFalse(typed["user"]["must_change_password"])
        self.assertNotIn("temporary_password", typed)
        with self.assertRaises(SystemExit):
            self.run_cli("create-user", "--email", "x@example.test", "--name", "X", "--role", "employee", "--employee-id", "NOPE")
        with self.assertRaises(SystemExit):
            self.run_cli("create-user", "--email", "weak@example.test", "--name", "W", "--role", "hr", "--password-stdin", stdin="short\n")
        reset = json.loads(self.run_cli("reset-password", "--email", "e1@example.test"))
        self.assertTrue(reset["temporary_password"])
        users = json.loads(self.run_cli("list-users"))
        self.assertEqual(len(users), 2)
        self.assertTrue(all("password_hash" not in user for user in users))
        export = self.root / "export.json"
        self.run_cli("export", "--output", str(export))
        self.assertEqual(len(json.loads(export.read_text(encoding="utf-8"))["employees"]), 48)
        with self.assertRaises(SystemExit):
            self.run_cli("purge-audit", "--days", "7")
        self.assertEqual(json.loads(self.run_cli("purge-audit", "--days", "365"))["removed"], 0)
        backup = self.root / "backup.sqlite3"
        self.run_cli("backup-sqlite", "--output", str(backup))
        self.database = str(backup)
        self.assertEqual(len(json.loads(self.run_cli("list-users"))), 2)


if __name__ == "__main__":
    unittest.main()

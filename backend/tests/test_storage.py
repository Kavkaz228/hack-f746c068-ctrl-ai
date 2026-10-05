"""Transactions must prevent duplicate gains and lost concurrent updates."""

from concurrent.futures import ThreadPoolExecutor
from datetime import date
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from threading import Barrier
import unittest

from backend.api.imports import import_dataset
from backend.api.presenters import build_hr_dashboard, get_hr_dashboard
from backend.engine.progress import complete_activity, promote_employee
from backend.scripts.generate_demo_data import build_demo
from backend.settings import sqlite_url
from backend.storage import DatasetStore, StateConflict
from backend.tests.support import database_url


def completion(index):
    return lambda dataset: complete_activity(dataset, "E001", "EV002", f"CONCURRENT_{index}", date(2026, 9, 23))


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.seed = build_demo()
        self.url = database_url(self, self.directory.name)
        self.store = DatasetStore(self.url, self.seed)
        self.addCleanup(self.store.db.dispose)

    def other_store(self):
        store = DatasetStore(self.url, self.seed)
        self.addCleanup(store.db.dispose)
        return store

    def test_seed_round_trip_preserves_order_and_values(self):
        self.assertEqual(self.store.snapshot().model_dump(mode="json"), self.seed.model_dump(mode="json"))
        # A second store on the same database must not seed again.
        self.assertEqual(self.other_store().snapshot().model_dump(mode="json"), self.seed.model_dump(mode="json"))

    def test_concurrent_retries_apply_one_gain_and_one_history_record(self):
        barrier = Barrier(2)

        def worker(index):
            repository = self.other_store()
            barrier.wait(timeout=10)
            return repository.complete("employee", "same-key", "E001/EV002", "E001", completion(index))

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(worker, (1, 2)))
        self.assertEqual(sorted(replayed for _, replayed in results), [False, True])
        self.assertEqual(results[0][0], results[1][0])
        updated = self.store.snapshot()
        self.assertEqual(len(updated.activity_history), len(self.seed.activity_history) + 1)
        self.assertEqual(updated.employees[0].skills["SK_SYSTEM_DESIGN"], 3)

    def test_concurrent_distinct_completions_do_not_lose_updates(self):
        barrier = Barrier(2)

        def worker(index):
            repository = self.other_store()
            barrier.wait(timeout=10)
            return repository.complete("employee", f"key-{index}", "E001/EV002", "E001", completion(index))

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(worker, (1, 2)))
        self.assertTrue(all(not replayed for _, replayed in results))
        updated = self.store.snapshot()
        self.assertEqual(len(updated.activity_history), len(self.seed.activity_history) + 2)
        self.assertEqual(updated.employees[0].skills["SK_SYSTEM_DESIGN"], 4)
        self.assertEqual({r.record_id for r in updated.activity_history if r.record_id.startswith("CONCURRENT_")},
                         {"CONCURRENT_1", "CONCURRENT_2"})

    def test_transform_failure_rolls_back_and_store_remains_usable(self):
        before = self.store.snapshot().model_dump(mode="json")
        version = self.store.version()

        def invalid_update(dataset):
            dataset.employees[0].skills["SK_SYSTEM_DESIGN"] = 5
            raise ValueError("failed after editing in-memory state")

        with self.assertRaisesRegex(ValueError, "failed after editing"):
            self.store.update(invalid_update)
        with self.assertRaisesRegex(ValueError, "failed after editing"):
            self.store.complete("employee", "failing", "E001/EV002", "E001", invalid_update)
        self.assertEqual(self.store.snapshot().model_dump(mode="json"), before)
        self.assertEqual(self.store.version(), version)
        response, replayed = self.store.complete("employee", "after-failure", "E001/EV002", "E001",
                                                 completion("RECOVERED"))
        self.assertFalse(replayed)
        self.assertEqual(response["skill_changes"][0]["after"], 3)

    def test_scoped_write_touches_only_one_employee_and_history_is_append_only(self):
        before = self.store.snapshot()
        self.store.complete("employee", "k", "E001/EV002", "E001", completion(1))
        after = self.store.snapshot()
        self.assertEqual(after.employees[1:], before.employees[1:])
        self.assertEqual(after.activity_history[:-1], before.activity_history)

        def rewrite_history(dataset):
            payload = dataset.model_dump(mode="json")
            payload["activity_history"][0]["status"] = "completed"
            return type(dataset).model_validate(payload), {"ok": True}

        with self.assertRaisesRegex(RuntimeError, "append-only"):
            self.store.complete("employee", "rewrite", "E001/x", "E001", rewrite_history)

    def test_cached_hr_aggregates_follow_writes(self):
        _, catalog, evaluations, history = self.store.evaluations()
        self.assertEqual(build_hr_dashboard(evaluations, catalog.events, catalog.skills, history),
                         get_hr_dashboard(self.seed))
        self.store.complete("employee", "k", "E001/EV002", "E001", completion(1))
        promoted = self.store.apply("hr", None, "E003/promote", "E003",
                                    lambda dataset: promote_employee(dataset, "E003"))[0]
        self.assertEqual(promoted["previous_grade"], "Middle")
        _, catalog, evaluations, history = self.store.evaluations()
        self.assertEqual(build_hr_dashboard(evaluations, catalog.events, catalog.skills, history),
                         get_hr_dashboard(self.store.snapshot()))

    def test_replace_changes_epoch_and_snapshots_allow_restore(self):
        self.store.complete("employee", "old-key", "E001/EV002", "E001", completion(1))
        with_progress = self.store.snapshot()
        payload = self.seed.model_dump(mode="json")
        self.store.update(lambda current: import_dataset(current, payload, "replace"), replace=True,
                          snapshot_reason="before_import_replace")
        self.assertEqual(self.store.snapshot().model_dump(mode="json"), payload)
        with self.assertRaises(StateConflict) as raised:
            self.store.complete("employee", "old-key", "E001/EV002", "E001", completion(2))
        self.assertEqual(raised.exception.code, "dataset_replaced")
        snapshots = self.store.list_snapshots()
        self.assertEqual(snapshots[0]["reason"], "before_import_replace")
        self.store.restore_snapshot(snapshots[0]["snapshot_id"], actor=self.store_actor())
        self.assertEqual(self.store.snapshot(), with_progress)
        self.assertEqual(self.store.list_snapshots()[0]["reason"], "before_restore")

    def test_append_keeps_existing_rows_and_order(self):
        employee = self.seed.employees[0].model_dump(mode="json")
        employee.update(employee_id="E_APPENDED", name="Synthetic appended employee")
        self.store.update(lambda current: import_dataset(current, {"employees": [employee]}, "append"))
        result = self.store.snapshot()
        self.assertEqual(result.employees[:-1], self.seed.employees)
        self.assertEqual(result.employees[-1].employee_id, "E_APPENDED")

    def test_snapshot_retention(self):
        store = DatasetStore(self.url, self.seed, snapshot_keep=2)
        self.addCleanup(store.db.dispose)
        for _ in range(4):
            store.create_snapshot("manual", self.store_actor())
        self.assertEqual(len(store.list_snapshots()), 2)

    @staticmethod
    def store_actor():
        from backend.storage import Actor
        return Actor("tester", "hr")


class DatabaseUrlTests(unittest.TestCase):
    def test_urls_are_normalized_and_memory_sqlite_is_rejected(self):
        from backend.db import Database
        from backend.settings import database_url_from_env
        self.assertEqual(database_url_from_env("postgres://u:p@h/db"), "postgresql+psycopg://u:p@h/db")
        self.assertEqual(database_url_from_env("postgresql://u:p@h/db"), "postgresql+psycopg://u:p@h/db")
        self.assertTrue(database_url_from_env("state.sqlite3").startswith("sqlite:///"))
        with self.assertRaises(ValueError):
            Database("sqlite:///:memory:")


class LegacyMigrationTests(unittest.TestCase):
    def test_pre_sql_state_file_is_migrated_in_place(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            seed = build_demo()
            progressed, _ = complete_activity(seed, "E001", "EV002", "LEGACY_1", date(2026, 9, 1))
            with sqlite3.connect(path) as connection:
                connection.execute("CREATE TABLE state (id INTEGER PRIMARY KEY CHECK (id = 1), "
                                   "epoch TEXT NOT NULL, payload TEXT NOT NULL)")
                connection.execute("INSERT INTO state VALUES (1, 'x', ?)", (progressed.model_dump_json(),))
            store = DatasetStore(sqlite_url(path), seed)
            self.addCleanup(store.db.dispose)
            self.assertEqual(store.snapshot(), progressed)
            audit = store.list_audit(action="dataset_seeded")
            self.assertEqual(audit[0]["details"]["source"], "legacy_sqlite")
            self.assertEqual(json.loads(json.dumps(audit[0]["details"]))["employees"], len(seed.employees))


if __name__ == "__main__":
    unittest.main()

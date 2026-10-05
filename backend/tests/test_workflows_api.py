"""HR approval of completions, skip/decline, promotion, overview, snapshots and HTTP hardening."""

import io
import json
import os
from tempfile import TemporaryDirectory
import unittest
from unittest import mock
from uuid import uuid4

from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.api.auth import Account
from backend.data_loader import load_dataset
from backend.engine.grade_progress import get_employee_grade_readiness
from backend.engine.recommendation import get_recommendations
from backend.tests.support import database_url, make_settings

HR = {"Authorization": "Bearer hr-test-token-123456"}
EMPLOYEE = {"Authorization": "Bearer employee-test-123456"}


class WorkflowBase(unittest.TestCase):
    settings = {}

    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        accounts = [Account(account_id="hr", role="hr", token="hr-test-token-123456"),
                    Account(account_id="employee", role="employee", employee_id="E001",
                            token="employee-test-123456")]
        self.app = create_app(database_url=database_url(self, directory.name), accounts=accounts,
                              settings=make_settings(**self.settings))
        self.client = self.enterContext(TestClient(self.app))
        self.seed = load_dataset()

    def complete(self, employee_id="E001", event_id="EV002", headers=EMPLOYEE, key=None, **kwargs):
        return self.client.post(f"/employees/{employee_id}/activities/{event_id}/complete",
                                headers={**headers, "Idempotency-Key": str(key or uuid4())}, **kwargs)


class ApprovalModeTests(WorkflowBase):
    settings = {"completion_mode": "approval"}

    def test_employee_request_changes_nothing_until_hr_approves(self):
        before = self.app.state.store.snapshot()
        key = uuid4()
        response = self.complete(key=key, json={"comment": "Сертификат загружен"})
        self.assertEqual(response.status_code, 202, response.text)
        pending = response.json()
        self.assertEqual(pending["status"], "pending_approval")
        self.assertEqual(pending["request"]["event_title"],
                         next(e.title for e in self.seed.events if e.event_id == "EV002"))
        self.assertEqual(self.app.state.store.snapshot(), before)
        replay = self.complete(key=key)
        self.assertEqual((replay.status_code, replay.headers["idempotency-replayed"]), (202, "true"))
        self.assertEqual(replay.json(), pending)
        duplicate = self.complete()
        self.assertEqual((duplicate.status_code, duplicate.json()["error"]), (409, "request_pending"))
        profile = self.client.get("/employees/E001", headers=EMPLOYEE).json()
        self.assertEqual([item["request_id"] for item in profile["pending_completions"]],
                         [pending["request"]["request_id"]])
        title = next(e.title for e in self.seed.events if e.event_id == "EV002")
        self.assertEqual(self.client.get("/completion-requests", headers=EMPLOYEE).json()[0]["event_title"], title)

        request_id = pending["request"]["request_id"]
        self.assertEqual(self.client.post(f"/completion-requests/{request_id}/approve",
                                          headers=EMPLOYEE).status_code, 403)
        approved = self.client.post(f"/completion-requests/{request_id}/approve", headers=HR,
                                    json={"comment": "Проверено"})
        self.assertEqual(approved.status_code, 200, approved.text)
        body = approved.json()
        self.assertEqual(body["request"]["status"], "approved")
        self.assertEqual(body["completion"]["skill_changes"][0]["after"], 3)
        after = self.app.state.store.snapshot()
        self.assertEqual(after.employees[0].skills["SK_SYSTEM_DESIGN"], 3)
        self.assertEqual(after.activity_history[-1].record_id, body["completion"]["record_id"])
        again = self.client.post(f"/completion-requests/{request_id}/approve", headers=HR)
        self.assertEqual((again.status_code, again.json()["error"]), (409, "request_already_decided"))
        self.assertEqual(self.client.get("/completion-requests", headers=HR).json(), [])

    def test_rejection_and_invalid_requests(self):
        invalid = self.complete(event_id="EV026")
        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(self.client.get("/completion-requests?status=all", headers=HR).json(), [])
        request_id = self.complete().json()["request"]["request_id"]
        rejected = self.client.post(f"/completion-requests/{request_id}/reject", headers=HR, json={"comment": "Нет подтверждения"})
        self.assertEqual(rejected.json()["request"]["status"], "rejected")
        self.assertIsNone(rejected.json()["completion"])
        self.assertEqual(self.app.state.store.snapshot().employees[0].skills["SK_SYSTEM_DESIGN"], 2)
        self.assertEqual(self.client.post("/completion-requests/REQ_missing/approve", headers=HR).status_code, 404)

    def test_hr_completion_applies_immediately_and_replace_expires_pending(self):
        self.assertEqual(self.complete(headers=HR).status_code, 200)
        self.complete(event_id="EV006")
        payload = self.seed.model_dump(mode="json")
        files = {name: ("f.json", json.dumps(payload[name]).encode(), "application/json")
                 for name in ("employees", "events", "skills")}
        csv_text = "record_id,employee_id,event_id,status,event_date\n" + "".join(
            f"{r['record_id']},{r['employee_id']},{r['event_id']},{r['status']},{r['event_date']}\n"
            for r in payload["activity_history"])
        files["activity_history"] = ("h.csv", csv_text.encode(), "text/csv")
        self.assertEqual(self.client.post("/dataset/import", headers=HR, data={"mode": "replace"},
                                          files=files).status_code, 200)
        statuses = {item["status"] for item in self.client.get("/completion-requests?status=all", headers=HR).json()}
        self.assertEqual(statuses, {"expired"})


class WorkflowTests(WorkflowBase):
    def test_skip_and_decline_are_stored_and_change_recommendation_history(self):
        before = get_recommendations(self.seed, "E001").recommendations[0]
        response = self.client.post(f"/employees/E001/activities/{before.event_id}/decline", headers=EMPLOYEE)
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result["status"], "declined")
        snapshot = self.app.state.store.snapshot()
        self.assertEqual(snapshot.activity_history[-1].status, "declined")
        self.assertEqual(snapshot.employees[0], self.seed.employees[0])  # skills unchanged
        self.assertEqual(result["recommendations_after"],
                         get_recommendations(snapshot, "E001").model_dump(mode="json"))
        key = str(uuid4())
        first = self.client.post("/employees/E001/activities/EV006/skip",
                                 headers={**EMPLOYEE, "Idempotency-Key": key})
        second = self.client.post("/employees/E001/activities/EV006/skip",
                                  headers={**EMPLOYEE, "Idempotency-Key": key})
        self.assertEqual(first.json(), second.json())
        self.assertEqual(second.headers["idempotency-replayed"], "true")
        self.assertEqual(self.client.post("/employees/E002/activities/EV006/skip", headers=EMPLOYEE).status_code, 403)
        self.assertEqual(self.client.post("/employees/E001/activities/EV026/skip", headers=EMPLOYEE).status_code, 422)

    def test_promotion_requires_mandatory_skills_unless_forced_with_reason(self):
        readiness = get_employee_grade_readiness(self.seed, "E001")
        self.assertFalse(readiness.meets_mandatory)
        self.assertEqual(self.client.post("/employees/E001/promote", headers=EMPLOYEE).status_code, 403)
        refused = self.client.post("/employees/E001/promote", headers=HR)
        self.assertEqual((refused.status_code, refused.json()["error"]), (422, "requirements_not_met"))
        no_reason = self.client.post("/employees/E001/promote", headers=HR, json={"force": True})
        self.assertEqual(no_reason.status_code, 422)
        forced = self.client.post("/employees/E001/promote", headers=HR,
                                  json={"force": True, "reason": "Решение комиссии"})
        self.assertEqual(forced.status_code, 200, forced.text)
        self.assertEqual((forced.json()["new_grade"], forced.json()["forced"]), (readiness.target_grade, True))
        self.assertEqual(self.client.get("/employees/E001", headers=HR).json()["employee"]["grade"],
                         readiness.target_grade)
        ready = next(e.employee_id for e in self.seed.employees
                     if get_employee_grade_readiness(self.seed, e.employee_id).meets_mandatory)
        promoted = self.client.post(f"/employees/{ready}/promote", headers=HR)
        self.assertEqual(promoted.status_code, 200)
        self.assertFalse(promoted.json()["forced"])
        audit = self.client.get("/audit?action=employee_promoted", headers=HR).json()
        self.assertEqual(len(audit), 2)
        self.assertEqual(audit[1]["details"]["reason"], "Решение комиссии")

    def test_promotion_checks_expected_grade_and_replays_retries(self):
        grade = self.seed.employees[0].grade
        stale = self.client.post("/employees/E001/promote", headers=HR,
                                 json={"force": True, "reason": "Комиссия", "expected_grade": "Junior"})
        self.assertEqual((stale.status_code, stale.json()["error"]), (409, "grade_changed"))
        key = str(uuid4())
        body = {"force": True, "reason": "Комиссия", "expected_grade": grade}
        first = self.client.post("/employees/E001/promote", headers={**HR, "Idempotency-Key": key}, json=body)
        again = self.client.post("/employees/E001/promote", headers={**HR, "Idempotency-Key": key}, json=body)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual((again.json(), again.headers["idempotency-replayed"]), (first.json(), "true"))
        double_click = self.client.post("/employees/E001/promote", headers=HR, json=body)
        self.assertEqual(double_click.status_code, 409)  # still expects the old grade
        self.assertEqual(len(self.client.get("/audit?action=employee_promoted", headers=HR).json()), 1)

    def test_values_longer_than_columns_are_rejected_before_storage(self):
        employee = {**self.seed.employees[0].model_dump(mode="json"), "employee_id": "E" * 70, "name": "Long"}
        files = {"employees": ("e.json", json.dumps([employee]).encode(), "application/json")}
        for dry_run in ("true", "false"):
            response = self.client.post("/dataset/import", headers=HR, data={"dry_run": dry_run}, files=files)
            self.assertEqual(response.status_code, 422, response.text)

    def test_ai_quota_is_reserved_atomically(self):
        from concurrent.futures import ThreadPoolExecutor
        from backend.storage import Actor, start_of_utc_day
        store = self.app.state.store
        actor = Actor("quota-user", "employee")
        with ThreadPoolExecutor(8) as pool:
            results = list(pool.map(lambda _: store.reserve_action(actor, "ai_recommendation", "E001", limit=3,
                                                                   since=start_of_utc_day()), range(12)))
        self.assertEqual(sum(result is not None for result in results), 3)

    def test_overview_matches_profiles_in_one_request(self):
        overview = self.client.get("/hr/employees", headers=HR)
        self.assertEqual(overview.status_code, 200)
        items = overview.json()
        self.assertEqual([i["employee_id"] for i in items], [e.employee_id for e in self.seed.employees])
        for item in items[:5]:
            readiness = get_employee_grade_readiness(self.seed, item["employee_id"])
            self.assertEqual(item["readiness_percent"], readiness.readiness_percent)
            top = get_recommendations(self.seed, item["employee_id"], 1).recommendations
            self.assertEqual(item["next_step"]["event_id"] if item["next_step"] else None,
                             top[0].event_id if top else None)
        self.assertEqual(self.client.get("/hr/employees", headers=EMPLOYEE).status_code, 403)
        self.complete()
        updated = {i["employee_id"]: i for i in self.client.get("/hr/employees", headers=HR).json()}
        self.assertGreater(updated["E001"]["readiness_percent"], items[0]["readiness_percent"])

    def test_dry_run_import_snapshots_restore_and_export(self):
        employee = {**self.seed.employees[0].model_dump(mode="json"), "employee_id": "E_NEW", "name": "Synthetic"}
        files = {"employees": ("e.json", json.dumps([employee]).encode(), "application/json")}
        preview = self.client.post("/dataset/import", headers=HR, data={"dry_run": "true"}, files=files)
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertEqual(preview.json()["changes"]["employees"], {"added": 1, "removed": 0})
        self.assertEqual(self.client.get("/employees/E_NEW", headers=HR).status_code, 404)
        self.assertEqual(self.client.post("/dataset/import", headers=HR, files=files).status_code, 200)
        snapshots = self.client.get("/dataset/snapshots", headers=HR).json()
        self.assertEqual(snapshots[0]["reason"], "before_import_append")
        self.assertEqual(snapshots[0]["employees"], len(self.seed.employees))
        restored = self.client.post(f"/dataset/snapshots/{snapshots[0]['snapshot_id']}/restore", headers=HR)
        self.assertEqual(restored.status_code, 200, restored.text)
        self.assertEqual(self.client.get("/employees/E_NEW", headers=HR).status_code, 404)
        exported = self.client.get("/dataset/export", headers=HR)
        self.assertIn("attachment", exported.headers["content-disposition"])
        self.assertEqual(exported.json(), self.seed.model_dump(mode="json"))
        for path in ("/dataset/snapshots", "/dataset/export", "/audit"):
            self.assertEqual(self.client.get(path, headers=EMPLOYEE).status_code, 403)
        self.assertEqual(self.client.post("/dataset/snapshots/999/restore", headers=HR).status_code, 404)

    def test_ai_daily_limit_applies_only_with_provider(self):
        self.app.state.settings.ai_daily_limit = 2
        self.assertEqual({self.client.post("/employees/E001/ai/recommendations", headers=EMPLOYEE).status_code
                          for _ in range(3)}, {200})
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test-not-real"}), \
                mock.patch("backend.api.ai._request_gpt_choice", return_value=None):
            statuses = [self.client.post("/employees/E001/ai/recommendations", headers=EMPLOYEE).status_code
                        for _ in range(3)]
        self.assertEqual(statuses, [200, 200, 429])

    def test_security_headers_body_limits_ready_and_metrics(self):
        response = self.client.get("/health")
        for header in ("x-content-type-options", "x-frame-options", "content-security-policy",
                       "referrer-policy", "x-request-id"):
            self.assertIn(header, response.headers)
        self.assertNotIn("server", response.headers)
        self.assertEqual(self.client.get("/health", headers={"X-Request-ID": "trace-12345678"}).headers["x-request-id"],
                         "trace-12345678")
        self.assertEqual(self.client.get("/ready").json()["status"], "ready")
        big = self.client.post("/auth/login", content=b"{" + b" " * (2 * 1024 * 1024) + b"}",
                               headers={"Content-Type": "application/json"})
        self.assertEqual((big.status_code, big.json()["error"]), (413, "payload_too_large"))
        self.assertEqual(self.client.get("/metrics").status_code, 404)
        self.app.state.settings.metrics_token = "metrics-secret-123"
        self.assertEqual(self.client.get("/metrics").status_code, 401)
        metrics = self.client.get("/metrics", headers={"Authorization": "Bearer metrics-secret-123"})
        self.assertIn("career_quest_http_requests_total", metrics.text)


class ProductionDefaultsTests(unittest.TestCase):
    def test_docs_hidden_and_approval_mode_in_production(self):
        from backend.settings import Settings
        with mock.patch.dict(os.environ, {"CAREER_QUEST_ENV": "production"}, clear=False):
            settings = Settings.from_env()
        self.assertEqual((settings.completion_mode, settings.enable_docs, settings.cookie_secure),
                         ("approval", False, None))
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        app = create_app(database_url=database_url(self, directory.name),
                         settings=make_settings(environment="production", enable_docs=False))
        with TestClient(app) as client:
            self.assertEqual(client.get("/docs").status_code, 404)
            self.assertEqual(client.get("/openapi.json").status_code, 404)
            self.assertEqual(app.state.accounts.count(), 0)  # no demo users in production


if __name__ == "__main__":
    unittest.main()


class BootstrapTests(unittest.TestCase):
    def test_first_hr_from_environment_must_change_password(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        settings = make_settings(environment="production", bootstrap_email="first.hr@example.test",
                                 bootstrap_password="Bootstrap-pass-2026")
        app = create_app(database_url=database_url(self, directory.name), settings=settings)
        with TestClient(app) as client:
            response = client.post("/auth/login", json={"email": "first.hr@example.test",
                                                        "password": "Bootstrap-pass-2026"})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertTrue(response.json()["must_change_password"])
            self.assertEqual(client.get("/hr/dashboard").json()["error"], "password_change_required")
        # A second start must not create another account or reset the password.
        with TestClient(create_app(database_url=app.state.database.url, settings=settings)) as client:
            self.assertEqual(len([u for u in client.app.state.accounts.list()]), 1)

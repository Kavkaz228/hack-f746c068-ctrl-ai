"""Exercise the HTTP contract, authorization, persistence and atomic imports."""

import csv
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.api.auth import Account
from backend.data_loader import HISTORY_COLUMNS, load_dataset
from backend.engine.grade_progress import get_employee_grade_readiness
from backend.api.presenters import get_hr_dashboard
from backend.engine.recommendation import get_recommendations
from backend.engine.skill_gap import get_employee_skill_gaps
from backend.tests.support import database_url, make_settings


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.database_url = database_url(self, self.directory.name)
        self.accounts = [
            Account(account_id="hr", role="hr", token="hr-test-token-123456"),
            Account(account_id="employee", role="employee", employee_id="E001",
                    token="employee-test-123456"),
        ]
        self.hr = {"Authorization": "Bearer hr-test-token-123456"}
        self.employee = {"Authorization": "Bearer employee-test-123456"}
        self.app = create_app(database_url=self.database_url, accounts=self.accounts, settings=make_settings())
        self.client = self.enterContext(TestClient(self.app))
        self.seed = load_dataset()

    @staticmethod
    def json_file(value):
        return ("import.json", json.dumps(value).encode(), "application/json")

    @staticmethod
    def history_file(records):
        stream = io.StringIO(newline="")
        writer = csv.DictWriter(stream, fieldnames=HISTORY_COLUMNS)
        writer.writeheader()
        writer.writerows(records)
        return ("activity_history.csv", stream.getvalue().encode(), "text/csv")

    def new_employee(self, employee_id="E_NEW"):
        result = self.seed.employees[0].model_dump(mode="json")
        result.update(employee_id=employee_id, name="Synthetic imported employee")
        return result

    def complete(self, event_id="EV002", employee_id="E001", key=None, headers=None):
        return self.client.post(
            f"/employees/{employee_id}/activities/{event_id}/complete",
            headers={**(self.employee if headers is None else headers),
                     "Idempotency-Key": str(key or uuid4())},
        )

    def snapshot(self):
        return self.app.state.store.snapshot().model_dump(mode="json")

    def test_health_and_authentication(self):
        self.assertEqual(self.client.get("/health").json(), {"status": "ok"})
        for path in ("/auth/me", "/employees", "/employees/E001", "/employees/E001/recommendations",
                     "/events", "/hr/dashboard"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.json()["error"], "unauthorized")
                self.assertEqual(response.headers["www-authenticate"], "Bearer")
        for headers in ({"Authorization": "Bearer invalid-token"}, {"Authorization": "Basic ignored"}):
            self.assertEqual(self.client.get("/auth/me", headers=headers).status_code, 401)
        self.assertEqual(self.client.post("/dataset/import").status_code, 401)
        self.assertEqual(self.complete(headers={}).status_code, 401)
        identity = self.client.get("/auth/me", headers=self.employee).json()
        self.assertEqual({key: identity[key] for key in ("account_id", "role", "employee_id", "auth")},
                         {"account_id": "employee", "role": "employee", "employee_id": "E001", "auth": "token"})
        self.assertNotIn("token", identity)
        self.assertFalse(identity["consent_required"] or identity["must_change_password"])

    def test_employee_cannot_read_other_history_or_use_hr_endpoints(self):
        for path in ("/employees/E002", "/employees/E002/recommendations", "/employees/UNKNOWN",
                     "/employees", "/hr/dashboard"):
            with self.subTest(path=path):
                response = self.client.get(path, headers=self.employee)
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.json()["error"], "forbidden")
        self.assertEqual(self.complete(employee_id="E002").status_code, 403)
        self.assertEqual(self.client.post("/dataset/import", headers=self.employee).status_code, 403)

    def test_profile_and_recommendations_match_the_domain_and_isolate_history(self):
        response = self.client.get("/employees/E001", headers=self.employee)
        self.assertEqual(response.status_code, 200)
        profile = response.json()
        self.assertEqual(profile["employee"], self.seed.employees[0].model_dump(mode="json"))
        self.assertEqual(profile["skill_gaps"], get_employee_skill_gaps(self.seed, "E001").model_dump(mode="json"))
        self.assertEqual(profile["readiness"], get_employee_grade_readiness(self.seed, "E001").model_dump(mode="json"))
        self.assertEqual(profile["recommendations"], get_recommendations(self.seed, "E001").model_dump(mode="json"))
        events = {event.event_id: event for event in self.seed.events}
        expected_history = [{**r.model_dump(mode="json"), "event_title": events[r.event_id].title,
                             "event_type": events[r.event_id].type}
                            for r in self.seed.activity_history if r.employee_id == "E001"]
        self.assertEqual(profile["history"], expected_history)
        archived_record = next(record for record in profile["history"] if record["event_id"] == "EV026")
        self.assertEqual(archived_record["event_title"], events["EV026"].title)
        self.assertEqual(profile["skill_catalog"], [s.model_dump(mode="json") for s in self.seed.skills.skill_catalog])
        recommendations = self.client.get("/employees/E001/recommendations?limit=1", headers=self.employee)
        self.assertEqual(recommendations.json(), get_recommendations(self.seed, "E001", 1).model_dump(mode="json"))
        self.assertEqual(recommendations.json()["recommendations"][0]["event_id"], "EV002")
        for limit in (0, 4, "bad"):
            self.assertEqual(self.client.get(f"/employees/E001/recommendations?limit={limit}",
                                             headers=self.employee).status_code, 422)

    def test_hr_can_read_all_profiles_list_and_aggregates(self):
        self.assertEqual(self.client.get("/employees/E002", headers=self.hr).status_code, 200)
        employees = self.client.get("/employees", headers=self.hr)
        self.assertEqual(employees.status_code, 200)
        self.assertEqual(len(employees.json()), len(self.seed.employees))
        self.assertEqual(set(employees.json()[0]), {"employee_id", "name", "role", "grade"})
        dashboard = self.client.get("/hr/dashboard", headers=self.hr)
        self.assertEqual(dashboard.status_code, 200)
        self.assertEqual(dashboard.json(), get_hr_dashboard(self.seed).model_dump(mode="json"))
        for path in ("/employees/UNKNOWN", "/employees/UNKNOWN/recommendations"):
            self.assertEqual(self.client.get(path, headers=self.hr).status_code, 404)
        self.assertEqual(self.complete(employee_id="UNKNOWN", headers=self.hr).status_code, 404)

    def test_employee_event_catalog_contains_only_active_eligible_events(self):
        events = self.client.get("/events", headers=self.employee).json()
        self.assertEqual(events, [e.model_dump(mode="json") for e in self.seed.events
                                  if e.active and "Backend Engineer" in e.audience])
        self.assertEqual(self.client.get("/events", headers=self.hr).json(),
                         [e.model_dump(mode="json") for e in self.seed.events])

    def test_completion_requires_uuid_retry_key(self):
        before = self.snapshot()
        url = "/employees/E001/activities/EV002/complete"
        for extra in ({}, {"Idempotency-Key": "not-a-uuid"}):
            response = self.client.post(url, headers={**self.employee, **extra})
            self.assertEqual(response.status_code, 422)
            self.assertEqual(response.json()["error"], "invalid_request")
        self.assertEqual(self.snapshot(), before)

    def test_completion_recalculates_and_persists_with_retry_and_restart(self):
        before = self.snapshot()
        key = uuid4()
        first = self.complete(key=key)
        self.assertEqual(first.status_code, 200, first.text)
        result = first.json()
        self.assertEqual(first.headers["idempotency-replayed"], "false")
        self.assertEqual(result["skill_changes"], [{"skill_id": "SK_SYSTEM_DESIGN", "before": 2,
                                                     "after": 3, "gain_applied": 1}])
        updated = self.app.state.store.snapshot()
        self.assertEqual(result["readiness_before"], get_employee_grade_readiness(self.seed, "E001").model_dump(mode="json"))
        self.assertEqual(result["readiness_after"], get_employee_grade_readiness(updated, "E001").model_dump(mode="json"))
        self.assertEqual(result["skill_gaps"], get_employee_skill_gaps(updated, "E001").model_dump(mode="json"))
        self.assertEqual(result["recommendations"], get_recommendations(updated, "E001").model_dump(mode="json"))
        self.assertEqual(len(updated.activity_history), len(before["activity_history"]) + 1)
        self.assertEqual(updated.activity_history[-1].record_id, result["record_id"])
        after = self.snapshot()
        retry = self.complete(key=key)
        self.assertEqual(retry.json(), result)
        self.assertEqual(retry.headers["idempotency-replayed"], "true")
        self.assertEqual(self.snapshot(), after)
        restarted = create_app(database_url=self.database_url, accounts=self.accounts, settings=make_settings())
        with TestClient(restarted) as client:
            response = client.post("/employees/E001/activities/EV002/complete",
                                   headers={**self.employee, "Idempotency-Key": str(key)})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), result)
            self.assertEqual(response.headers["idempotency-replayed"], "true")
            profile = client.get("/employees/E001", headers=self.employee).json()
            self.assertEqual(profile["employee"]["skills"]["SK_SYSTEM_DESIGN"], 3)
            self.assertEqual(restarted.state.store.snapshot().model_dump(mode="json"), after)

    def test_e001_end_to_end_updates_profile_recommendations_and_hr_dashboard(self):
        profile_before = self.client.get("/employees/E001", headers=self.employee)
        recommendation_before = self.client.get(
            "/employees/E001/recommendations?limit=1", headers=self.employee)
        dashboard_before = self.client.get("/hr/dashboard", headers=self.hr)
        self.assertEqual((profile_before.status_code, recommendation_before.status_code,
                          dashboard_before.status_code), (200, 200, 200))
        profile_before = profile_before.json()
        dashboard_before = dashboard_before.json()
        self.assertEqual(profile_before["employee"]["skills"]["SK_SYSTEM_DESIGN"], 2)
        self.assertAlmostEqual(profile_before["readiness"]["readiness_percent"],
                               81.73076923076923)
        self.assertEqual(recommendation_before.json()["recommendations"][0]["event_id"],
                         "EV002")

        completion = self.complete(event_id="EV002", key=uuid4())
        self.assertEqual(completion.status_code, 200, completion.text)
        self.assertEqual(completion.json()["skill_changes"], [
            {"skill_id": "SK_SYSTEM_DESIGN", "before": 2,
             "after": 3, "gain_applied": 1},
        ])

        profile_after = self.client.get("/employees/E001", headers=self.employee)
        recommendation_after = self.client.get(
            "/employees/E001/recommendations", headers=self.employee)
        dashboard_after = self.client.get("/hr/dashboard", headers=self.hr)
        self.assertEqual((profile_after.status_code, recommendation_after.status_code,
                          dashboard_after.status_code), (200, 200, 200))
        profile_after = profile_after.json()
        dashboard_after = dashboard_after.json()
        self.assertEqual(profile_after["employee"]["skills"]["SK_SYSTEM_DESIGN"], 3)
        self.assertAlmostEqual(profile_after["readiness"]["readiness_percent"],
                               86.53846153846153)
        self.assertEqual(profile_after["readiness"], completion.json()["readiness_after"])
        self.assertEqual(recommendation_after.json(), completion.json()["recommendations"])
        self.assertEqual(recommendation_after.json()["recommendations"][0]["event_id"],
                         "EV006")
        self.assertGreater(dashboard_after["average_readiness_percent"],
                           dashboard_before["average_readiness_percent"])
        self.assertEqual(dashboard_after["participation"]["completed"],
                         dashboard_before["participation"]["completed"] + 1)

        def backend_design_gap(dashboard):
            return next(item for item in dashboard["role_skill_deficits"]
                        if item["role"] == "Backend Engineer"
                        and item["skill_id"] == "SK_SYSTEM_DESIGN")

        role_gap_before = backend_design_gap(dashboard_before)
        role_gap_after = backend_design_gap(dashboard_after)
        self.assertEqual((role_gap_before["eligible_employees"],
                          role_gap_before["affected_employees"]), (9, 7))
        self.assertAlmostEqual(role_gap_before["average_gap_across_role"], 13 / 9)
        self.assertAlmostEqual(role_gap_after["average_gap_across_role"], 12 / 9)

    def test_retry_key_cannot_be_reused_for_another_target(self):
        key = uuid4()
        self.assertEqual(self.complete(key=key).status_code, 200)
        before = self.snapshot()
        response = self.complete(event_id="EV001", key=key)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"], "idempotency_conflict")
        self.assertEqual(self.snapshot(), before)

    def test_invalid_completions_leave_state_unchanged_and_key_can_be_retried(self):
        before = self.snapshot()
        invalid_events = [("UNKNOWN", 404), ("EV026", 422)]
        mismatched = next(e for e in self.seed.events if e.active and "Backend Engineer" not in e.audience)
        invalid_events.append((mismatched.event_id, 422))
        for event_id, status in invalid_events:
            with self.subTest(event=event_id):
                key = uuid4()
                response = self.complete(event_id=event_id, key=key)
                self.assertEqual(response.status_code, status, response.text)
                self.assertEqual(self.snapshot(), before)
        key = uuid4()
        self.assertEqual(self.complete(event_id="UNKNOWN", key=key).status_code, 404)
        self.assertEqual(self.complete(key=key).status_code, 200)

    def test_append_unknown_profile_and_history_then_calculate_and_complete(self):
        employee = self.new_employee()
        records = [{"record_id": f"IMPORT_{index}", "employee_id": "E_NEW", "event_id": "EV003",
                    "status": status, "event_date": f"2026-01-0{index}"}
                   for index, status in enumerate(("skipped", "declined"), 1)]
        response = self.client.post("/dataset/import", headers=self.hr, files={
            "employees": self.json_file([employee]), "activity_history": self.history_file(records),
        })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["employees"], len(self.seed.employees) + 1)
        profile = self.client.get("/employees/E_NEW", headers=self.hr)
        self.assertEqual(profile.status_code, 200)
        self.assertEqual(profile.json()["employee"], employee)
        self.assertEqual(profile.json()["history"], [
            {**record, "event_title": "Public Speaking Meetup", "event_type": "meetup"} for record in records
        ])
        self.assertEqual(profile.json()["recommendations"],
                         get_recommendations(self.app.state.store.snapshot(), "E_NEW").model_dump(mode="json"))
        self.assertEqual(self.complete(employee_id="E_NEW", headers=self.hr).status_code, 200)

    def test_failed_import_has_no_partial_employee_or_history(self):
        before = self.snapshot()
        response = self.client.post("/dataset/import", headers=self.hr, files={
            "employees": self.json_file([self.new_employee()]),
            "activity_history": self.history_file([{
                "record_id": "BAD_REF", "employee_id": "E_NEW", "event_id": "UNKNOWN",
                "status": "skipped", "event_date": "2026-01-01",
            }]),
        })
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"], "invalid_dataset")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.client.get("/employees/E_NEW", headers=self.hr).status_code, 404)
        for employees, expected_status in (([self.new_employee(), self.new_employee("E001")], 409),
                                           ([self.new_employee(), self.new_employee()], 422)):
            response = self.client.post("/dataset/import", headers=self.hr,
                                        files={"employees": self.json_file(employees)})
            self.assertEqual(response.status_code, expected_status, response.text)
            self.assertEqual(self.snapshot(), before)

    def test_replace_requires_all_files_and_invalidates_old_completion_keys(self):
        key = uuid4()
        self.assertEqual(self.complete(key=key).status_code, 200)
        before = self.snapshot()
        response = self.client.post("/dataset/import", headers=self.hr, data={"mode": "replace"},
                                    files={"employees": self.json_file(before["employees"])})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.snapshot(), before)
        payload = self.seed.model_dump(mode="json")
        files = {name: self.json_file(payload[name]) for name in ("employees", "events", "skills")}
        files["activity_history"] = self.history_file(payload["activity_history"])
        response = self.client.post("/dataset/import", headers=self.hr, data={"mode": "replace"}, files=files)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.snapshot(), payload)
        response = self.complete(key=key)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"], "dataset_replaced")
        self.assertEqual(self.snapshot(), payload)
        self.assertEqual(self.complete().status_code, 200)

    def test_invalid_uploads_and_empty_request_are_rejected_atomically(self):
        before = self.snapshot()
        bad_files = [
            {},
            {"employees": ("bad.json", b"[", "application/json")},
            {"employees": ("bad.json", b"\xff", "application/json")},
            {"employees": ("bad.json", b'[{"employee_id":"X","employee_id":"Y"}]', "application/json")},
            {"employees": self.json_file({"employee_id": "X"})},
            {"employees": self.json_file([{**self.new_employee(), "employee_id": []}])},
            {"employees": self.json_file([{**self.new_employee(), "employee_id": {"id": "X"}}])},
            {"activity_history": ("bad.csv", b"employee_id,event_id\nE001,EV002", "text/csv")},
            {"activity_history": ("bad.csv", b"record_id,employee_id,event_id,status,event_date\nX,E001,EV002", "text/csv")},
            {"skills": self.json_file(self.seed.skills.model_dump(mode="json"))},
        ]
        for files in bad_files:
            with self.subTest(files=list(files)):
                response = self.client.post("/dataset/import", headers=self.hr, files=files)
                self.assertEqual(response.status_code, 422, response.text)
                self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()

"""The what-if and comparison endpoints must use the real career rules safely."""

import csv
from datetime import date
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.tests.support import database_url, make_settings
from backend.api.auth import Account
from backend.data_loader import HISTORY_COLUMNS
from backend.engine.progress import complete_activity
from backend.engine.recommendation import get_all_recommendations, get_recommendations
from backend.engine.simulation import compare_activities, simulate_activity
from backend.engine.skill_gap import get_employee_skill_gaps
from backend.models import Dataset
from backend.scripts.generate_demo_data import build_demo


class SimulationDomainTests(unittest.TestCase):
    def setUp(self):
        self.dataset = build_demo()

    def test_preview_matches_completion_without_changing_input_or_history(self):
        before = self.dataset.model_dump(mode="json")
        preview = simulate_activity(self.dataset, "E001", "EV002")
        self.assertEqual(self.dataset.model_dump(mode="json"), before)
        self.assertEqual(preview.employee_id, "E001")
        self.assertEqual(preview.event_id, "EV002")
        self.assertEqual(preview.target_grade, "Senior")
        self.assertEqual(preview.gaps_before, get_employee_skill_gaps(self.dataset, "E001"))

        updated, completed = complete_activity(
            self.dataset, "E001", "EV002", "PREVIEW_PARITY", date(2026, 9, 23))
        self.assertEqual(preview.skill_changes, completed.skill_changes)
        self.assertEqual(preview.readiness_before, completed.readiness_before)
        self.assertEqual(preview.readiness_after, completed.readiness_after)
        self.assertEqual(preview.gaps_after, get_employee_skill_gaps(updated, "E001"))
        self.assertEqual(preview.recommendations_after, completed.recommendations_after)
        self.assertGreater(preview.readiness_after.readiness_percent,
                           preview.readiness_before.readiness_percent)
        self.assertEqual(self.dataset.model_dump(mode="json"), before)
        self.assertEqual(simulate_activity(self.dataset, "E001", "EV002"), preview)

    def test_preview_respects_gain_and_cap_for_multiple_skills(self):
        payload = self.dataset.model_dump(mode="json")
        event = next(item for item in payload["events"] if item["event_id"] == "EV002")
        event["skills"] = [
            {"skill_id": "SK_SYSTEM_DESIGN", "gain": 2, "max_level": 3},
            {"skill_id": "SK_PYTHON", "gain": 2, "max_level": 4},
        ]
        dataset = Dataset.model_validate(payload)
        before = dataset.model_dump(mode="json")
        preview = simulate_activity(dataset, "E001", "EV002")
        changes = {change.skill_id: change for change in preview.skill_changes}
        self.assertEqual((changes["SK_SYSTEM_DESIGN"].before,
                          changes["SK_SYSTEM_DESIGN"].after,
                          changes["SK_SYSTEM_DESIGN"].applied_gain), (2, 3, 1))
        self.assertEqual((changes["SK_PYTHON"].before,
                          changes["SK_PYTHON"].after,
                          changes["SK_PYTHON"].applied_gain), (3, 4, 1))
        self.assertEqual(dataset.model_dump(mode="json"), before)

    def test_non_relevant_but_available_activity_has_honest_zero_readiness_delta(self):
        before = self.dataset.model_dump(mode="json")
        # Leadership is not a requirement for E001's next (Senior) grade.
        preview = simulate_activity(self.dataset, "E001", "EV024")
        self.assertEqual(preview.skill_changes[0].skill_id, "SK_LEADERSHIP")
        self.assertEqual(preview.skill_changes[0].applied_gain, 1)
        self.assertEqual(preview.readiness_after, preview.readiness_before)
        self.assertEqual(preview.gaps_after, preview.gaps_before)
        self.assertEqual(self.dataset.model_dump(mode="json"), before)

    def test_comparison_uses_actual_rank_scores_history_and_readiness(self):
        before = self.dataset.model_dump(mode="json")
        result = compare_activities(self.dataset, "E001", "EV002", "EV003")
        self.assertEqual(result.employee_id, "E001")
        self.assertEqual(result.target_grade, "Senior")
        self.assertEqual((result.first.event_id, result.second.event_id), ("EV002", "EV003"))
        self.assertEqual(result.preferred_event_id, "EV002")
        self.assertGreater(result.first.score, result.second.score)
        self.assertAlmostEqual(result.score_delta, result.first.score - result.second.score)
        self.assertAlmostEqual(result.first.score,
                               result.first.grade_gap_benefit * result.first.history_multiplier)
        self.assertAlmostEqual(result.second.score,
                               result.second.grade_gap_benefit * result.second.history_multiplier)
        self.assertEqual(result.second.participation.skipped, 2)
        self.assertEqual(result.second.participation.declined, 1)
        self.assertEqual(result.first, get_recommendations(self.dataset, "E001", limit=1).recommendations[0])
        self.assertTrue(result.explanation.strip())
        for event_id, readiness in (("EV002", result.readiness_after_first),
                                    ("EV003", result.readiness_after_second)):
            _, completed = complete_activity(
                self.dataset, "E001", event_id, f"COMPARE_{event_id}", date(2026, 9, 23))
            self.assertEqual(readiness, completed.readiness_after)
        self.assertEqual(self.dataset.model_dump(mode="json"), before)

    def test_equal_scores_use_stable_event_id_order(self):
        forward = compare_activities(self.dataset, "E001", "EV002", "EV004")
        reverse = compare_activities(self.dataset, "E001", "EV004", "EV002")
        self.assertEqual(forward.score_delta, 0)
        self.assertEqual(reverse.score_delta, 0)
        self.assertEqual(forward.preferred_event_id, "EV002")
        self.assertEqual(reverse.preferred_event_id, "EV002")

    def test_unknown_imported_profile_and_history_are_not_hard_coded(self):
        payload = self.dataset.model_dump(mode="json")
        payload["employees"].append({
            "employee_id": "E999", "name": "Synthetic newcomer", "role": "Backend Engineer",
            "grade": "Middle", "tenure_months": 3, "skills": {},
        })
        payload["activity_history"].extend([
            {"record_id": "E999_SKIP", "employee_id": "E999", "event_id": "EV003",
             "status": "skipped", "event_date": "2026-01-01"},
            {"record_id": "E999_DECLINE", "employee_id": "E999", "event_id": "EV003",
             "status": "declined", "event_date": "2026-01-02"},
        ])
        dataset = Dataset.model_validate(payload)
        before = dataset.model_dump(mode="json")
        preview = simulate_activity(dataset, "E999", "EV002")
        comparison = compare_activities(dataset, "E999", "EV002", "EV003")
        self.assertEqual(preview.skill_changes[0].before, 0)
        self.assertEqual(preview.skill_changes[0].after, 1)
        self.assertGreater(preview.readiness_after.readiness_percent,
                           preview.readiness_before.readiness_percent)
        self.assertEqual(comparison.second.participation.skipped, 1)
        self.assertEqual(comparison.second.participation.declined, 1)
        self.assertEqual(dataset.model_dump(mode="json"), before)

    def test_invalid_comparison_targets_do_not_mutate_data(self):
        before = self.dataset.model_dump(mode="json")
        cases = [
            ("missing", "EV002", "EV003", "employee_not_found"),
            ("E001", "missing", "EV003", "event_not_found"),
            ("E001", "EV002", "EV002", "same_event"),
            ("E001", "EV026", "EV003", "inactive_event"),
            ("E002", "EV002", "EV003", "ineligible_role"),
            ("E001", "EV002", "EV024", "not_relevant"),
        ]
        for employee_id, first, second, code in cases:
            with self.subTest(code=code), self.assertRaises(ValueError) as caught:
                compare_activities(self.dataset, employee_id, first, second)
            self.assertEqual(getattr(caught.exception, "code", None), code)
        self.assertEqual(self.dataset.model_dump(mode="json"), before)


class SimulationApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        accounts = [
            Account(account_id="hr", role="hr", token="simulation-hr-token-123456"),
            Account(account_id="employee", role="employee", employee_id="E001",
                    token="simulation-employee-token-123456"),
        ]
        self.hr = {"Authorization": "Bearer simulation-hr-token-123456"}
        self.employee = {"Authorization": "Bearer simulation-employee-token-123456"}
        self.app = create_app(database_url=database_url(self, self.directory.name), accounts=accounts,
                              settings=make_settings())
        self.client = self.enterContext(TestClient(self.app))

    def snapshot(self):
        return self.app.state.store.snapshot().model_dump(mode="json")

    def test_http_preview_and_comparison_are_authorized_and_read_only(self):
        before = self.snapshot()
        preview_path = "/employees/E001/simulate/EV002"
        compare_path = "/employees/E001/compare?first_event_id=EV002&second_event_id=EV003"
        options_path = "/employees/E001/comparison-options"
        for path in (preview_path, compare_path, options_path):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 401)
                self.assertEqual(self.client.get(path.replace("E001", "E002"),
                                                 headers=self.employee).status_code, 403)
                response = self.client.get(path, headers=self.employee)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["employee_id"], "E001")
                self.assertEqual(self.client.get(path, headers=self.hr).json(), response.json())
                self.assertEqual(self.snapshot(), before)

        options = self.client.get(options_path, headers=self.employee).json()
        expected = get_all_recommendations(self.app.state.store.snapshot(), "E001")
        self.assertEqual(options, expected.model_dump(mode="json"))
        self.assertEqual(options["recommendations"][:3],
                         get_recommendations(self.app.state.store.snapshot(), "E001").model_dump(mode="json")["recommendations"])
        self.assertIn("EV003", {item["event_id"] for item in options["recommendations"][3:]})

        preview = self.client.get(preview_path, headers=self.employee).json()
        completed = self.client.post(
            "/employees/E001/activities/EV002/complete",
            headers={**self.employee, "Idempotency-Key": str(uuid4())},
        )
        self.assertEqual(completed.status_code, 200, completed.text)
        actual = completed.json()
        self.assertEqual(preview["readiness_after"], actual["readiness_after"])
        self.assertEqual(preview["recommendations_after"], actual["recommendations"])
        self.assertEqual(preview["gaps_after"], actual["skill_gaps"])

    def test_invalid_http_targets_and_parameters_do_not_change_state(self):
        before = self.snapshot()
        cases = [
            ("/employees/UNKNOWN/simulate/EV002", 404),
            ("/employees/E001/simulate/UNKNOWN", 404),
            ("/employees/E001/simulate/EV026", 422),
            ("/employees/E001/compare?first_event_id=EV002&second_event_id=EV002", 422),
            ("/employees/E001/compare?first_event_id=EV002&second_event_id=EV024", 422),
            ("/employees/E001/compare?first_event_id=EV002", 422),
        ]
        for path, status in cases:
            with self.subTest(path=path):
                response = self.client.get(path, headers=self.hr)
                self.assertEqual(response.status_code, status, response.text)
                self.assertEqual(self.snapshot(), before)

    def test_imported_profile_and_history_can_be_previewed(self):
        source = self.app.state.store.snapshot()
        new_employee = source.employees[0].model_dump(mode="json")
        new_employee.update(employee_id="E999", name="Synthetic newcomer", skills={})
        records = [
            {"record_id": "E999_SKIP", "employee_id": "E999", "event_id": "EV003",
             "status": "skipped", "event_date": "2026-01-01"},
            {"record_id": "E999_DECLINE", "employee_id": "E999", "event_id": "EV003",
             "status": "declined", "event_date": "2026-01-02"},
        ]
        stream = io.StringIO(newline="")
        writer = csv.DictWriter(stream, fieldnames=HISTORY_COLUMNS)
        writer.writeheader()
        writer.writerows(records)
        response = self.client.post("/dataset/import", headers=self.hr, files={
            "employees": ("employees.json", json.dumps([new_employee]).encode(), "application/json"),
            "activity_history": ("activity_history.csv", stream.getvalue().encode(), "text/csv"),
        })
        self.assertEqual(response.status_code, 200, response.text)
        before = self.snapshot()
        preview = self.client.get("/employees/E999/simulate/EV002", headers=self.hr)
        comparison = self.client.get(
            "/employees/E999/compare?first_event_id=EV002&second_event_id=EV003",
            headers=self.hr,
        )
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertEqual(comparison.status_code, 200, comparison.text)
        self.assertEqual(preview.json()["skill_changes"][0]["before"], 0)
        self.assertEqual(comparison.json()["second"]["participation"]["declined"], 1)
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()

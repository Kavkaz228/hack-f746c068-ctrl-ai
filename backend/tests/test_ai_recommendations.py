"""Optional GPT recommendation must remain safe, scoped, and reproducible offline."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from backend.api.ai import _candidate_features, _request_gpt_choice
from backend.api.app import create_app
from backend.tests.support import database_url, make_settings
from backend.api.auth import Account
from backend.data_loader import load_dataset
from backend.engine.recommendation import get_all_recommendations, get_recommendations


class AIRecommendationApiTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        accounts = [
            Account(account_id="hr", role="hr", token="hr-test-token-123456"),
            Account(account_id="employee", role="employee", employee_id="E001",
                    token="employee-test-123456"),
        ]
        self.hr = {"Authorization": "Bearer hr-test-token-123456"}
        self.employee = {"Authorization": "Bearer employee-test-123456"}
        self.app = create_app(database_url=database_url(self, temporary.name), accounts=accounts,
                              settings=make_settings())
        self.client = self.enterContext(TestClient(self.app))
        self.seed = load_dataset()

    def snapshot(self):
        return self.app.state.store.snapshot().model_dump(mode="json")

    def post(self, employee_id="E001", headers=None):
        return self.client.post(
            f"/employees/{employee_id}/ai/recommendations",
            headers=self.employee if headers is None else headers,
        )

    def test_missing_key_uses_unchanged_rules_without_provider_call_or_write(self):
        before = self.snapshot()
        expected = get_recommendations(self.seed, "E001")
        with patch.dict("os.environ", {"OPENAI_API_KEY": ""}), patch(
            "backend.api.ai._request_gpt_choice", side_effect=AssertionError("Provider must not be called")
        ):
            response = self.post()

        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result["source"], "rules")
        self.assertEqual(result["fallback_reason"], "not_configured")
        self.assertEqual(result["status"], expected.status)
        self.assertEqual(result["selected_event_id"], expected.recommendations[0].event_id)
        self.assertEqual(result["recommendations"],
                         [choice.model_dump(mode="json") for choice in expected.recommendations])
        self.assertEqual(result["considered_count"],
                         min(12, len(get_all_recommendations(self.seed, "E001").recommendations)))
        self.assertEqual(self.snapshot(), before)

    def test_gpt_can_choose_useful_option_outside_rules_top_three(self):
        before = self.snapshot()
        ranked = get_all_recommendations(self.seed, "E001")
        self.assertNotIn("EV006", [choice.event_id for choice in ranked.recommendations[:3]])
        chosen = next(choice for choice in ranked.recommendations if choice.event_id == "EV006")
        with patch.dict("os.environ", {"OPENAI_API_KEY": "fake-test-key"}), patch(
            "backend.api.ai._request_gpt_choice", return_value="EV006"
        ) as request_choice:
            response = self.post()

        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result["source"], "gpt")
        self.assertIsNone(result["fallback_reason"])
        self.assertEqual(result["selected_event_id"], "EV006")
        self.assertEqual(result["recommendations"][0], chosen.model_dump(mode="json"))
        self.assertLessEqual(len(result["recommendations"]), 3)
        self.assertEqual(len({r["event_id"] for r in result["recommendations"]}),
                         len(result["recommendations"]))
        self.assertIn(chosen.explanation, result["selection_explanation"])
        self.assertEqual(result["model"], "gpt-4o-mini")
        self.assertEqual(result["considered_count"], min(12, len(ranked.recommendations)))
        self.assertEqual(request_choice.call_count, 1)
        called = request_choice.call_args.kwargs
        self.assertEqual(called["api_key"], "fake-test-key")
        self.assertEqual(called["model"], "gpt-4o-mini")
        transmitted = json.dumps(called["payload"], ensure_ascii=False)
        self.assertNotIn("fake-test-key", transmitted)
        self.assertNotIn(self.seed.employees[0].name, transmitted)
        self.assertEqual(self.snapshot(), before)

    def test_invalid_model_event_id_falls_back_to_same_rule_ranking(self):
        before = self.snapshot()
        expected = get_recommendations(self.seed, "E001")
        with patch.dict("os.environ", {"OPENAI_API_KEY": "fake-test-key"}), patch(
            "backend.api.ai._request_gpt_choice", return_value="NONEXISTENT_EVENT"
        ):
            response = self.post()

        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result["source"], "rules")
        self.assertEqual(result["fallback_reason"], "invalid_response")
        self.assertEqual(result["selected_event_id"], expected.recommendations[0].event_id)
        self.assertEqual(result["recommendations"],
                         [choice.model_dump(mode="json") for choice in expected.recommendations])
        self.assertEqual(self.snapshot(), before)

    def test_provider_timeout_falls_back_without_state_change(self):
        before = self.snapshot()
        with patch.dict("os.environ", {"OPENAI_API_KEY": "fake-test-key"}), patch(
            "backend.api.ai._request_gpt_choice", side_effect=TimeoutError("simulated timeout")
        ):
            response = self.post()

        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result["source"], "rules")
        self.assertEqual(result["fallback_reason"], "provider_unavailable")
        self.assertEqual(result["selected_event_id"], "EV002")
        self.assertEqual(self.snapshot(), before)

    def test_no_candidates_never_calls_provider(self):
        before = self.snapshot()
        with patch.dict("os.environ", {"OPENAI_API_KEY": "fake-test-key"}), patch(
            "backend.api.ai._request_gpt_choice", side_effect=AssertionError("Provider must not be called")
        ):
            response = self.post("E003", headers=self.hr)

        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result["status"], "no_skill_gaps")
        self.assertEqual(result["source"], "rules")
        self.assertIsNone(result["selected_event_id"])
        self.assertEqual(result["recommendations"], [])
        self.assertEqual(result["considered_count"], 0)
        self.assertEqual(self.snapshot(), before)

    def test_imported_profile_and_history_can_receive_gpt_recommendation(self):
        newcomer = self.seed.employees[0].model_dump(mode="json")
        newcomer.update(employee_id="E_NEW", name="Synthetic newcomer")
        records = [
            {"record_id": "AI_NEW_SKIP", "employee_id": "E_NEW", "event_id": "EV003",
             "status": "skipped", "event_date": "2026-01-01"},
            {"record_id": "AI_NEW_DECLINE", "employee_id": "E_NEW", "event_id": "EV003",
             "status": "declined", "event_date": "2026-01-02"},
        ]
        import_response = self.client.post("/dataset/import", headers=self.hr, files={
            "employees": ("employees.json", json.dumps([newcomer]).encode(), "application/json"),
            "activity_history": ("activity_history.csv",
                                 ("record_id,employee_id,event_id,status,event_date\n" +
                                  "\n".join(",".join(str(record[key]) for key in
                                                    ("record_id", "employee_id", "event_id", "status", "event_date"))
                                            for record in records)).encode(), "text/csv"),
        })
        self.assertEqual(import_response.status_code, 200, import_response.text)
        before = self.snapshot()
        ranked = get_all_recommendations(self.app.state.store.snapshot(), "E_NEW")
        choice = ranked.recommendations[0]
        with patch.dict("os.environ", {"OPENAI_API_KEY": "fake-test-key"}), patch(
            "backend.api.ai._request_gpt_choice", return_value=choice.event_id
        ):
            response = self.post("E_NEW", headers=self.hr)

        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result["employee_id"], "E_NEW")
        self.assertEqual(result["source"], "gpt")
        self.assertEqual(result["selected_event_id"], choice.event_id)
        self.assertEqual(result["recommendations"][0], choice.model_dump(mode="json"))
        self.assertEqual(self.snapshot(), before)

    def test_authentication_and_employee_scope_are_checked_before_provider(self):
        before = self.snapshot()
        with patch.dict("os.environ", {"OPENAI_API_KEY": "fake-test-key"}), patch(
            "backend.api.ai._request_gpt_choice", side_effect=AssertionError("Provider must not be called")
        ):
            self.assertEqual(self.post(headers={}).status_code, 401)
            self.assertEqual(self.post("E002").status_code, 403)
            self.assertEqual(self.post("UNKNOWN").status_code, 403)
        self.assertEqual(self.snapshot(), before)

    def test_provider_request_uses_strict_non_storing_schema_and_minimal_facts(self):
        ranked = get_all_recommendations(self.seed, "E001")
        payload = {
            "role": ranked.role,
            "current_grade": ranked.current_grade,
            "target_grade": ranked.target_grade,
            "candidates": [_candidate_features(item) for item in ranked.recommendations[:12]],
        }
        fake_response = MagicMock()
        fake_response.read.return_value = json.dumps({
            "status": "completed",
            "output": [{"type": "message", "content": [
                {"type": "output_text", "text": json.dumps({"event_id": "EV006"})},
            ]}],
        }).encode("utf-8")
        fake_response.__enter__.return_value = fake_response

        with patch("backend.api.ai.urlopen", return_value=fake_response) as fake_urlopen:
            chosen_id = _request_gpt_choice(
                api_key="fake-test-key", model="gpt-4o-mini", payload=payload,
            )

        self.assertEqual(chosen_id, "EV006")
        self.assertEqual(fake_urlopen.call_count, 1)
        request = fake_urlopen.call_args.args[0]
        self.assertEqual(fake_urlopen.call_args.kwargs["timeout"], 7)
        self.assertEqual(request.full_url, "https://api.openai.com/v1/responses")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer fake-test-key")
        body = json.loads(request.data.decode("utf-8"))
        self.assertIs(body["store"], False)
        self.assertEqual(body["model"], "gpt-4o-mini")
        output_format = body["text"]["format"]
        self.assertEqual(output_format["type"], "json_schema")
        self.assertIs(output_format["strict"], True)
        self.assertIs(output_format["schema"]["additionalProperties"], False)
        self.assertEqual(output_format["schema"]["properties"]["event_id"]["enum"],
                         [candidate["event_id"] for candidate in payload["candidates"]])
        self.assertEqual(json.loads(body["input"][1]["content"]), payload)
        transmitted = json.dumps(body, ensure_ascii=False)
        self.assertNotIn(self.seed.employees[0].name, transmitted)
        self.assertNotIn('"employee_id"', transmitted)
        self.assertNotIn('"record_id"', transmitted)
        self.assertNotIn('"event_date"', transmitted)
        self.assertNotIn('"activity_history"', transmitted)
        self.assertNotIn("fake-test-key", transmitted)

    def test_provider_reply_without_output_text_has_no_choice(self):
        payload = {"role": "Backend Engineer", "current_grade": "Middle",
                   "target_grade": "Senior", "candidates": [{"event_id": "EV002"}]}
        fake_response = MagicMock()
        fake_response.read.return_value = json.dumps({
            "status": "completed", "output": [{"type": "message", "content": []}],
        }).encode("utf-8")
        fake_response.__enter__.return_value = fake_response
        with patch("backend.api.ai.urlopen", return_value=fake_response):
            self.assertIsNone(_request_gpt_choice(
                api_key="fake-test-key", model="gpt-4o-mini", payload=payload,
            ))

    def test_malformed_provider_reply_has_no_choice(self):
        payload = {"role": "Backend Engineer", "current_grade": "Middle",
                   "target_grade": "Senior", "candidates": [{"event_id": "EV002"}]}
        fake_response = MagicMock()
        fake_response.read.return_value = b"not JSON"
        fake_response.__enter__.return_value = fake_response
        with patch("backend.api.ai.urlopen", return_value=fake_response):
            self.assertIsNone(_request_gpt_choice(
                api_key="fake-test-key", model="gpt-4o-mini", payload=payload,
            ))


if __name__ == "__main__":
    unittest.main()

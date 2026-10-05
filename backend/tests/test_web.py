"""Check the single-host frontend/API entrypoint and its access boundaries."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from fastapi.testclient import TestClient

from backend.api.auth import Account
from backend.tests.support import database_url, make_settings
from backend.web import create_app


class WebTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        frontend = root / "dist"
        (frontend / "assets").mkdir(parents=True)
        self.html = "<!doctype html><html><body>Career Quest</body></html>"
        (frontend / "index.html").write_text(self.html, encoding="utf-8")
        (frontend / "assets" / "app.js").write_text("console.log('app');", encoding="utf-8")
        (root / "access.json").write_text('"private runtime data"', encoding="utf-8")
        accounts = [
            Account(account_id="hr", role="hr", token="hr-test-token-123456"),
            Account(account_id="employee", role="employee", employee_id="E001",
                    token="employee-test-123456"),
        ]
        self.employee = {"Authorization": "Bearer employee-test-123456"}
        self.hr = {"Authorization": "Bearer hr-test-token-123456"}
        self.database_url = database_url(self, root)
        self.app = create_app(frontend, database_url=self.database_url, accounts=accounts, settings=make_settings())
        self.client = self.enterContext(TestClient(self.app))

    def test_frontend_pages_and_assets(self):
        for path in ("/", "/login", "/register", "/hr", "/employee/E001", "/employee/E001/history"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.text, self.html)
        asset = self.client.get("/assets/app.js")
        self.assertEqual(asset.status_code, 200)
        self.assertEqual(asset.text, "console.log('app');")

    def test_spa_pages_get_the_strict_content_security_policy(self):
        for path in ("/", "/employee/docs", "/hr/docs", "/login"):
            policy = self.client.get(path).headers["content-security-policy"]
            self.assertIn("script-src 'self';", policy, path)
            self.assertNotIn("jsdelivr", policy, path)
        self.assertIn("jsdelivr", self.client.get("/api/docs").headers["content-security-policy"])

    def test_unknown_api_assets_and_runtime_are_not_spa_pages(self):
        for path in ("/api/unknown", "/assets/missing.js", "/assets/missing", "/access.json",
                     "/runtime/access.json", "/state.sqlite3", "/%2e%2e/access.json"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 404)
                self.assertNotIn("private runtime data", response.text)
                self.assertNotEqual(response.text, self.html)

    def test_api_lifecycle_authentication_and_role_boundaries(self):
        self.assertEqual(self.client.get("/api/ready").json()["status"], "ready")
        self.assertEqual(self.client.get("/api/health").json(), {"status": "ok"})
        self.assertEqual(self.client.get("/api/auth/me").status_code, 401)
        identity = self.client.get("/api/auth/me", headers=self.employee)
        self.assertEqual(identity.status_code, 200)
        self.assertEqual(identity.json()["employee_id"], "E001")
        self.assertEqual(self.client.get("/api/hr/dashboard", headers=self.employee).status_code, 403)
        self.assertEqual(self.client.get("/api/employees/E002", headers=self.employee).status_code, 403)
        dashboard = self.client.get("/api/hr/dashboard", headers=self.hr)
        self.assertEqual(dashboard.status_code, 200)
        self.assertIn("average_readiness_percent", dashboard.json())

    def test_e001_completion_works_through_mounted_api(self):
        path = "/api/employees/E001"
        before = self.client.get(path, headers=self.employee)
        self.assertEqual(before.status_code, 200)
        complete = self.client.post(
            f"{path}/activities/EV002/complete",
            headers={**self.employee, "Idempotency-Key": str(uuid4())},
        )
        self.assertEqual(complete.status_code, 200)
        after = self.client.get(path, headers=self.employee)
        self.assertEqual(after.status_code, 200)
        self.assertGreater(after.json()["readiness"]["readiness_percent"],
                           before.json()["readiness"]["readiness_percent"])
        self.assertEqual(len(after.json()["history"]), len(before.json()["history"]) + 1)


if __name__ == "__main__":
    unittest.main()

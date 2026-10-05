"""Password sign-in, cookie sessions, gates, CSRF and HR-managed accounts."""

from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from fastapi.testclient import TestClient

from backend.accounts import AccountError, check_password_policy, hash_password, verify_password
from backend.api.app import create_app
from backend.tests.support import database_url, make_settings

PASSWORD = "Synthetic-pass-2026"


class PasswordTests(unittest.TestCase):
    def test_hash_is_salted_and_verifies(self):
        first, second = hash_password(PASSWORD), hash_password(PASSWORD)
        self.assertNotEqual(first, second)
        self.assertTrue(first.startswith("scrypt$"))
        self.assertTrue(verify_password(PASSWORD, first))
        self.assertFalse(verify_password(PASSWORD + "x", first))
        self.assertFalse(verify_password(PASSWORD, "garbage"))
        self.assertFalse(verify_password(PASSWORD, None))

    def test_policy(self):
        for weak in ("short1", "onlyletterslong", "1234567890123", "password1", "aaaaaaaaa1"):
            with self.subTest(password=weak), self.assertRaises(AccountError):
                check_password_policy(weak)
        check_password_policy(PASSWORD)


class AuthApiTests(unittest.TestCase):
    def setUp(self, **settings):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.app = create_app(database_url=database_url(self, directory.name), accounts=[],
                              settings=make_settings(**settings))
        self.client = self.enterContext(TestClient(self.app))
        self.users = self.app.state.accounts
        self.hr_user, _ = self.users.create(email="hr@example.test", name="Synthetic HR", role="hr",
                                            password=PASSWORD, must_change_password=False,
                                            consent_version=self.app.state.settings.consent_version)

    def login(self, email="hr@example.test", password=PASSWORD, client=None, **headers):
        return (client or self.client).post("/auth/login", json={"email": email, "password": password},
                                            headers=headers)

    def test_login_sets_httponly_cookie_and_logout_revokes_it(self):
        self.assertEqual(self.client.get("/auth/me").status_code, 401)
        response = self.login(email="  HR@Example.TEST ")
        self.assertEqual(response.status_code, 200, response.text)
        cookie = response.headers["set-cookie"]
        self.assertIn("cq_session=", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=strict", cookie)
        self.assertNotIn("Secure", cookie)  # plain-HTTP test server; HTTPS gets the Secure flag
        https = self.enterContext(TestClient(self.app, base_url="https://testserver"))
        self.app.state.settings.cookie_secure = None
        self.assertIn("Secure", self.login(client=https).headers["set-cookie"])
        self.app.state.settings.cookie_secure = False
        self.assertNotIn("token", response.json())
        me = self.client.get("/auth/me").json()
        self.assertEqual((me["role"], me["auth"], me["email"]), ("hr", "session", "hr@example.test"))
        self.assertEqual(self.client.get("/hr/dashboard").status_code, 200)
        stolen = self.client.cookies.get("cq_session")
        self.assertEqual(self.client.post("/auth/logout").status_code, 200)
        self.assertEqual(self.client.get("/auth/me").status_code, 401)
        self.client.cookies.set("cq_session", stolen)
        self.assertEqual(self.client.get("/auth/me").status_code, 401)

    def test_wrong_password_is_generic_and_account_locks(self):
        unknown = self.login(email="nobody@example.test")
        wrong = self.login(password="Wrong-pass-2026")
        self.assertEqual((unknown.status_code, wrong.status_code), (401, 401))
        self.assertEqual(unknown.json(), wrong.json())
        for _ in range(4):
            self.login(password="Wrong-pass-2026")
        locked = self.login()  # correct password, but the account is locked
        self.assertEqual(locked.status_code, 401)
        self.assertEqual(locked.json(), wrong.json())  # nothing reveals that the e-mail exists
        reasons = [item["details"]["reason"] for item in self.app.state.store.list_audit(action="login_failed")]
        self.assertEqual(reasons[0], "locked")
        self.assertIn("unknown_user", reasons)
        self.assertEqual(reasons.count("wrong_password"), 5)

    def test_parallel_wrong_guesses_still_lock_the_account(self):
        from concurrent.futures import ThreadPoolExecutor
        users = self.app.state.accounts

        def guess(_):
            try:
                users.authenticate("hr@example.test", "Wrong-pass-2026")
            except Exception as exc:  # noqa: BLE001
                return getattr(exc, "reason", None)

        with ThreadPoolExecutor(10) as pool:
            list(pool.map(guess, range(20)))
        with self.assertRaises(Exception) as raised:
            users.authenticate("hr@example.test", PASSWORD)
        self.assertEqual(raised.exception.reason, "locked")

    def test_session_cookie_requires_same_origin_for_writes(self):
        self.login()
        evil = {"Origin": "https://evil.example"}
        response = self.client.post("/users", json={"email": "x@example.test", "name": "X", "role": "hr"},
                                    headers=evil)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"], "csrf_failed")
        self.assertEqual(self.login(**evil).status_code, 403)
        same = {"Origin": "http://testserver"}
        response = self.client.post("/users", json={"email": "x@example.test", "name": "X", "role": "hr"},
                                    headers=same)
        self.assertEqual(response.status_code, 201, response.text)

    def test_new_employee_must_change_password_and_consent_before_data_access(self):
        self.login()
        created = self.client.post("/users", json={"email": "emp@example.test", "name": "Synthetic Employee",
                                                   "role": "employee", "employee_id": "E001"})
        self.assertEqual(created.status_code, 201, created.text)
        temporary = created.json()["temporary_password"]
        self.assertTrue(created.json()["user"]["must_change_password"])
        duplicate = self.client.post("/users", json={"email": "emp2@example.test", "name": "Dup",
                                                     "role": "employee", "employee_id": "E001"})
        self.assertEqual(duplicate.status_code, 409)
        unknown = self.client.post("/users", json={"email": "emp3@example.test", "name": "Unknown",
                                                   "role": "employee", "employee_id": "NOPE"})
        self.assertEqual(unknown.status_code, 404)

        employee = self.enterContext(TestClient(self.app))
        self.assertEqual(self.login("emp@example.test", temporary, client=employee).status_code, 200)
        me = employee.get("/auth/me").json()
        self.assertTrue(me["must_change_password"] and me["consent_required"])
        blocked = employee.get("/employees/E001")
        self.assertEqual((blocked.status_code, blocked.json()["error"]), (403, "password_change_required"))
        weak = employee.post("/auth/password", json={"current_password": temporary, "new_password": "short"})
        self.assertEqual(weak.status_code, 422)
        wrong = employee.post("/auth/password", json={"current_password": "bad", "new_password": PASSWORD})
        self.assertEqual(wrong.status_code, 400)
        changed = employee.post("/auth/password", json={"current_password": temporary, "new_password": PASSWORD})
        self.assertEqual(changed.status_code, 200, changed.text)
        blocked = employee.get("/employees/E001")
        self.assertEqual((blocked.status_code, blocked.json()["error"]), (403, "consent_required"))
        version = me["consent_version"]
        self.assertEqual(employee.post("/auth/consent", json={"version": "old", "accepted": True}).status_code, 409)
        self.assertEqual(employee.post("/auth/consent", json={"version": version, "accepted": True}).status_code, 200)
        self.assertEqual(employee.get("/employees/E001").status_code, 200)
        self.assertEqual(employee.get("/employees/E002").status_code, 403)
        self.assertEqual(employee.get("/users").status_code, 403)
        self.assertEqual(employee.delete("/auth/consent").status_code, 200)
        self.assertEqual(employee.get("/employees/E001").json()["error"], "consent_required")

    def test_password_change_signs_out_other_sessions(self):
        other = self.enterContext(TestClient(self.app))
        self.login()
        self.login(client=other)
        response = self.client.post("/auth/password", json={"current_password": PASSWORD,
                                                             "new_password": "Another-pass-2027"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get("/auth/me").status_code, 200)
        self.assertEqual(other.get("/auth/me").status_code, 401)

    def test_hr_resets_disables_and_enables_accounts(self):
        self.login()
        created = self.client.post("/users", json={"email": "hr2@example.test", "name": "Second HR", "role": "hr"}).json()
        user_id = created["user"]["user_id"]
        second = self.enterContext(TestClient(self.app))
        self.assertEqual(self.login("hr2@example.test", created["temporary_password"], client=second).status_code, 200)
        reset = self.client.post(f"/users/{user_id}/reset-password").json()
        self.assertNotEqual(reset["temporary_password"], created["temporary_password"])
        self.assertEqual(second.get("/auth/me").status_code, 401)  # sessions revoked on reset
        self.assertEqual(self.client.post(f"/users/{user_id}/disable").json()["is_active"], False)
        disabled = self.login("hr2@example.test", reset["temporary_password"], client=second)
        self.assertEqual(disabled.status_code, 401)
        self.assertEqual(self.app.state.store.list_audit(action="login_failed")[0]["details"]["reason"], "disabled")
        self.assertEqual(self.client.post(f"/users/{self.hr_user.user_id}/disable").status_code, 400)
        self.assertEqual(self.client.post(f"/users/{user_id}/enable").json()["is_active"], True)
        self.assertEqual(self.login("hr2@example.test", reset["temporary_password"], client=second).status_code, 200)
        listed = self.client.get("/users").json()
        self.assertTrue(all(not ({"password_hash", "api_token_hash", "temporary_password"} & set(item)) for item in listed))
        actions = {item["action"] for item in self.client.get("/audit").json()}
        self.assertTrue({"user_created", "password_reset", "user_disabled", "user_enabled",
                         "login_succeeded"} <= actions)

    def test_one_person_account_per_employee_even_without_the_ui_check(self):
        users = self.app.state.accounts
        users.create(email="a@example.test", name="A", role="employee", employee_id="E001")
        with self.assertRaises(AccountError) as raised:
            users.create(email="b@example.test", name="B", role="employee", employee_id="E001")
        self.assertEqual(raised.exception.code, "employee_has_account")
        with self.assertRaises(AccountError) as raised:
            users.create(email="A@example.test", name="A2", role="hr")
        self.assertEqual(raised.exception.code, "user_exists")

    def test_expired_session_is_rejected(self):
        self.login()
        from sqlalchemy import update
        from backend import db as schema
        from datetime import datetime
        with self.app.state.database.write_tx() as connection:
            connection.execute(update(schema.sessions).values(expires_at=datetime(2000, 1, 1)))
        self.assertEqual(self.client.get("/auth/me").status_code, 401)


class LoginRateLimitTests(unittest.TestCase):
    def test_ip_limit(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        app = create_app(database_url=database_url(self, directory.name), accounts=[],
                         settings=make_settings(login_ip_limit_per_15min=3))
        client = self.enterContext(TestClient(app))
        statuses = [client.post("/auth/login", json={"email": f"{uuid4().hex}@example.test",
                                                     "password": "Whatever-2026"}).status_code for _ in range(4)]
        self.assertEqual(statuses, [401, 401, 401, 429])


if __name__ == "__main__":
    unittest.main()

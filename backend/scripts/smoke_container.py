"""Post-deploy check for a running container; completes one activity in its database.

Runs inside the container (docker compose exec web python -m backend.scripts.smoke_container).
It creates or refreshes two synthetic smoke accounts directly in the database,
then exercises the public HTTP interface exactly like a browser: password
sign-in, httpOnly cookie, CSRF origin, role boundaries, completion (with HR
approval when CAREER_QUEST_COMPLETION_MODE=approval) and idempotent retries.
"""

import argparse
from http.cookiejar import CookieJar
import json
import os
import secrets
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener

from backend.accounts import AccountStore
from backend.db import Database
from backend.settings import Settings, database_url_from_env

KEY = "7d1b544a-0ba4-4bde-90c7-1762a9965a72"


def ensure_user(accounts: AccountStore, email: str, role: str, employee_id: str | None, consent: str) -> str:
    password = "Smoke-" + secrets.token_urlsafe(12) + "-1"
    user = accounts.by_email(email)
    if user is None:
        user, _ = accounts.create(email=email, name=f"Smoke {role}", role=role, employee_id=employee_id,
                                  password=password, must_change_password=False, consent_version=consent)
    else:
        accounts.set_password(user.user_id, password, must_change_password=False)
        accounts.record_consent(user.user_id, consent)
    return password


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--replay", action="store_true", help="Require persisted completion from the first run")
    parser.add_argument("--employee-id", default="E001")
    args = parser.parse_args()
    base = "http://127.0.0.1:" + os.environ.get("PORT", "8000")
    settings = Settings.from_env()
    database = Database(database_url_from_env())
    accounts = AccountStore(database)
    hr_password = ensure_user(accounts, "smoke-hr@smoke.invalid", "hr", None, settings.consent_version)
    employee_password = ensure_user(accounts, "smoke-employee@smoke.invalid", "employee", args.employee_id,
                                    settings.consent_version)
    database.dispose()

    def client():
        return build_opener(HTTPCookieProcessor(CookieJar()))

    def call(opener, path, *, method="GET", body=None, headers=None, expected=200):
        request_headers = {"Origin": base, **(headers or {})}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            request_headers["Content-Type"] = "application/json"
        try:
            response = opener.open(Request(base + path, data=data, method=method, headers=request_headers), timeout=15)
        except HTTPError as error:
            response = error
        with response:
            status = response.status
            content = response.read()
            assert status in (expected if isinstance(expected, tuple) else (expected,)), \
                f"{method} {path}: HTTP {status}, expected {expected}: {content[:300]!r}"
            result = json.loads(content) if "application/json" in response.headers.get("Content-Type", "") else content
            return result, response.headers, status

    anonymous, hr, employee = client(), client(), client()
    assert call(anonymous, "/api/health")[0] == {"status": "ok"}
    assert call(anonymous, "/api/ready")[0]["status"] == "ready"
    page, headers, _ = call(anonymous, "/")
    assert b"<html" in page.lower() and "default-src 'self'" in headers["Content-Security-Policy"]
    assert b"<html" in call(anonymous, f"/employee/{args.employee_id}")[0].lower()
    call(anonymous, "/api/docs", expected=200 if settings.enable_docs else 404)
    call(anonymous, "/api/employees", expected=401)
    call(anonymous, "/api/auth/login", method="POST", expected=401,
         body={"email": "smoke-hr@smoke.invalid", "password": "Wrong-password-1"})

    call(hr, "/api/auth/login", method="POST", body={"email": "smoke-hr@smoke.invalid", "password": hr_password})
    call(employee, "/api/auth/login", method="POST",
         body={"email": "smoke-employee@smoke.invalid", "password": employee_password})
    call(employee, "/api/hr/dashboard", expected=403)
    call(hr, "/api/hr/dashboard")
    overview = call(hr, "/api/hr/employees")[0]
    assert any(item["employee_id"] == args.employee_id for item in overview)
    identity = call(employee, "/api/auth/me")[0]
    assert identity["employee_id"] == args.employee_id and identity["auth"] == "session"
    call(employee, "/api/auth/logout", method="POST", headers={"Origin": "https://evil.invalid"}, expected=200)
    call(employee, "/api/auth/login", method="POST",
         body={"email": "smoke-employee@smoke.invalid", "password": employee_password})

    path = f"/api/employees/{args.employee_id}/activities/EV002/complete"
    result, response_headers, status = call(employee, path, method="POST", headers={"Idempotency-Key": KEY},
                                            expected=(200, 202))
    assert response_headers["Idempotency-Replayed"] == ("true" if args.replay else "false")
    repeated, response_headers, _ = call(employee, path, method="POST", headers={"Idempotency-Key": KEY},
                                         expected=status)
    assert response_headers["Idempotency-Replayed"] == "true" and repeated == result
    if status == 202:
        request_id = result["request"]["request_id"]
        if not args.replay:
            decision = call(hr, f"/api/completion-requests/{request_id}/approve", method="POST",
                            body={"comment": "smoke"})[0]
            record_id = decision["completion"]["record_id"]
        else:
            approved = call(hr, "/api/completion-requests?status=approved")[0]
            record_id = next(item["record_id"] for item in approved if item["request_id"] == request_id)
    else:
        record_id = result["record_id"]
    profile = call(employee, f"/api/employees/{args.employee_id}")[0]
    assert sum(item["record_id"] == record_id for item in profile["history"]) == 1
    if not os.environ.get("OPENAI_API_KEY"):
        ai = call(employee, f"/api/employees/{args.employee_id}/ai/recommendations", method="POST")[0]
        assert ai["source"] == "rules" and ai["fallback_reason"] == "not_configured"
    audit = call(hr, "/api/audit?limit=50")[0]
    assert any(item["action"] == "login_succeeded" for item in audit)
    print("Container smoke checks passed (completion mode: "
          f"{'approval' if status == 202 else 'instant'})" + ("; persisted state verified" if args.replay else ""))


if __name__ == "__main__":
    main()

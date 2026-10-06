"""
Failed-login behaviour (opt-in: `pytest -m lockout`). Marked `lockout` only, not `stage1`, so `-m stage1` never
selects them.

Each run of this file costs 7 failed logins against the per-IP counter (`login_throttle`, key `ip:<client ip>`,
20 failures per 15-minute window, `auth.ip-max-failed-logins`), which a successful login does not reset. See the
README for how often it is safe to run and how to reset the counters.
"""
from __future__ import annotations

import pytest

from tests import data, factories

pytestmark = pytest.mark.lockout

MAX_FAILED_LOGINS = 5  # auth.max-failed-logins (user-service config)


def _login(client, email, password):
    return client.post("/api/auth/login", {"email": email, "password": password}, token=None)


def _comparable(body: dict) -> dict:
    return {k: v for k, v in body.items() if k != "timestamp"}


def test_wrong_password_and_unknown_email_give_the_same_401(client):
    """2 failed logins. No user enumeration: same status, code, message and shape."""
    user = factories.register_customer(client, "enumeration")
    wrong_password = _login(client, user.email, data.password()).expect(401, code="INVALID_CREDENTIALS")
    unknown_email = _login(client, data.email("nobody"), data.password()).expect(401, code="INVALID_CREDENTIALS")
    assert _comparable(wrong_password.body) == _comparable(unknown_email.body)
    assert wrong_password.body["message"] == "Invalid email or password"


def test_account_locks_after_max_failures(client):
    """5 failed logins on a throwaway customer; then even the right password gets 429 for lockout-minutes."""
    user = factories.register_customer(client, "lockout")
    for attempt in range(1, MAX_FAILED_LOGINS + 1):
        # The lock is set by the 5th failure, which itself still answers 401
        _login(client, user.email, data.password()).expect(401, code="INVALID_CREDENTIALS")
    locked = _login(client, user.email, user.password).expect(429, code="TOO_MANY_LOGIN_ATTEMPTS")
    assert "accessToken" not in locked.body
    # The lock is per account: another user is unaffected
    other = factories.register_customer(client, "lockout-other")
    other.login()


@pytest.mark.skip_time_based(reason="the account lock lasts auth.lockout-minutes (15 min); expiry not waited out")
def test_account_unlocks_after_lockout_minutes():
    pass

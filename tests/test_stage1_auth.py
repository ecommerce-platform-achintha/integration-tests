"""Stage 1, user-service auth: login shape, forced password change, refresh rotation/reuse, logout."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tests import data, factories
from tests.actors import Actor, decode_jwt

pytestmark = pytest.mark.stage1

TOKEN_FIELDS = {"accessToken", "tokenType", "expiresIn", "expiresAt", "refreshToken", "refreshTokenExpiresAt",
                "publicId", "role", "status", "mustChangePassword"}


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


# --- login ------------------------------------------------------------------------------------------------------

def test_login_success_shape(client):
    user = factories.register_customer(client, "login-shape")
    resp = client.post("/api/auth/login", {"email": user.email.upper(), "password": user.password}, token=None)
    resp.expect(200)
    body = resp.body
    assert TOKEN_FIELDS <= set(body), f"missing {TOKEN_FIELDS - set(body)}"
    assert "statusReason" not in body  # only present when visible (NON_NULL)
    assert body["tokenType"] == "Bearer"
    assert body["expiresIn"] == 600  # auth.access-token-minutes = 10
    assert body["role"] == "ROLE_CUSTOMER" and body["status"] == "ACTIVE"
    assert body["mustChangePassword"] is False
    assert body["publicId"] == user.extra["profile"]["publicId"]
    now = datetime.now(timezone.utc)
    assert timedelta(days=13) < _ts(body["refreshTokenExpiresAt"]) - now <= timedelta(days=14, minutes=1)
    assert "no-store" in resp.headers.get("Cache-Control", "")

    claims = decode_jwt(body["accessToken"])
    assert claims["iss"] == "user-service"
    assert "marketplace" in (claims["aud"] if isinstance(claims["aud"], list) else [claims["aud"]])
    assert claims["pid"] == body["publicId"] and claims["role"] == "ROLE_CUSTOMER"
    assert claims["status"] == "ACTIVE" and isinstance(claims["tv"], int)
    assert claims["exp"] - claims["iat"] == 600 and claims["jti"] and claims["sub"]
    assert "storeId" not in claims and "perms" not in claims


@pytest.mark.parametrize("body", [
    {"email": "", "password": "whatever-password"},
    {"email": "someone@example.com"},
    {},
], ids=["blank-email", "no-password", "empty"])
def test_login_validation_is_400(client, body):
    # Bean validation runs before the service, so these never count as failed logins
    client.post("/api/auth/login", body, token=None).expect(400, code="VALIDATION_FAILED")


def test_protected_route_without_token_is_401(client):
    client.get("/api/users/me", token=None).expect(401, code="UNAUTHORIZED")
    client.get("/api/users/me", token="not-a-jwt").expect(401)


# --- forced password change --------------------------------------------------------------------------------------

@pytest.fixture
def fresh_admin(super_admin, client) -> Actor:
    """Admin still on its temporary password (mustChangePassword=true)."""
    return factories.create_admin(super_admin, client, "fresh-admin", change_initial_password=False)


def test_new_admin_must_change_password(fresh_admin):
    assert fresh_admin.must_change_password
    assert fresh_admin.tokens["mustChangePassword"] is True
    fresh_admin.get("/api/users/me").expect(403, code="PASSWORD_CHANGE_REQUIRED")
    fresh_admin.get("/api/admin/users").expect(403, code="PASSWORD_CHANGE_REQUIRED")
    fresh_admin.get("/api/users/me/addresses").expect(403, code="PASSWORD_CHANGE_REQUIRED")
    fresh_admin.get("/api/admin/merchants/pending").expect(403, code="PASSWORD_CHANGE_REQUIRED")


def test_new_assistant_must_change_password(merchant, client):
    assistant = factories.create_assistant(merchant, client, ["ORDER_VIEW"], change_initial_password=False)
    assert assistant.must_change_password
    assistant.get("/api/users/me").expect(403, code="PASSWORD_CHANGE_REQUIRED")


@pytest.mark.parametrize("path", [
    "/api/admin/banks",        # store-service
    "/api/admin/categories",   # product-service
    "/api/admin/orders",       # order-service
])
def test_password_change_required_in_other_services(fresh_admin, path):
    """Design 3.3: until the password is changed, every endpoint except change-password/logout answers 403."""
    fresh_admin.get(path).expect(403, code="PASSWORD_CHANGE_REQUIRED")


def test_logout_allowed_while_password_change_required(fresh_admin):
    fresh_admin.post("/api/auth/logout", {"refreshToken": fresh_admin.refresh_token}).expect(204)


@pytest.mark.parametrize("new_password, code", [
    ("Short-1", "VALIDATION_FAILED"),                 # < 12 characters
    ("password1234", "VALIDATION_FAILED"),            # deny list
    ("abcabcabcabc", "VALIDATION_FAILED"),            # only 3 distinct characters
    (" Leading-space-pass", "VALIDATION_FAILED"),     # surrounding whitespace
    ("x" * 120 + "Abc-12345", "VALIDATION_FAILED"),   # > 128 characters
], ids=["short", "deny-list", "3-distinct", "whitespace", "too-long"])
def test_change_password_policy_violations(fresh_admin, new_password, code):
    resp = fresh_admin.post("/api/auth/change-password",
                            {"currentPassword": fresh_admin.password, "newPassword": new_password})
    resp.expect(400, code=code)
    assert any(e["field"] == "newPassword" for e in resp.body.get("fieldErrors", [])), resp.describe()


def test_change_password_rule_checks(fresh_admin):
    fresh_admin.post("/api/auth/change-password",
                     {"currentPassword": "Not-the-current-1", "newPassword": data.password()}
                     ).expect(400, code="INVALID_CREDENTIALS")
    fresh_admin.post("/api/auth/change-password",
                     {"currentPassword": fresh_admin.password, "newPassword": fresh_admin.password}
                     ).expect(400, code="PASSWORD_REUSED")
    fresh_admin.post("/api/auth/change-password",
                     {"currentPassword": fresh_admin.password, "newPassword": fresh_admin.email}
                     ).expect(400, code="WEAK_PASSWORD")


def test_change_password_returns_new_tokens_and_revokes_old_ones(fresh_admin, client):
    old_access, old_refresh = fresh_admin.tokens["accessToken"], fresh_admin.refresh_token
    factories.change_password(fresh_admin)
    assert fresh_admin.tokens["mustChangePassword"] is False
    assert fresh_admin.tokens["accessToken"] != old_access and fresh_admin.refresh_token != old_refresh
    fresh_admin.get("/api/users/me").expect(200)
    fresh_admin.get("/api/admin/users").expect(200)
    client.get("/api/users/me", token=old_access).expect(401, code="TOKEN_REVOKED")
    client.post("/api/auth/refresh", {"refreshToken": old_refresh}, token=None
                ).expect(401, code="INVALID_REFRESH_TOKEN")
    # The new password works for login, with no forced change any more
    fresh_admin.login()
    assert not fresh_admin.must_change_password


# --- refresh ----------------------------------------------------------------------------------------------------

def _refresh(client, token):
    return client.post("/api/auth/refresh", {"refreshToken": token}, token=None)


def test_refresh_rotates_and_reuse_revokes_the_family(client):
    user = factories.register_customer(client, "refresh")
    r1 = user.refresh_token
    second = _refresh(client, r1).expect(200)
    assert "no-store" in second.headers.get("Cache-Control", "")
    r2, access2 = second.body["refreshToken"], second.body["accessToken"]
    assert r2 != r1
    client.get("/api/users/me", token=access2).expect(200)

    # Presenting the used token again: reuse detected, the whole family (r2 included) is revoked
    _refresh(client, r1).expect(401, code="REFRESH_TOKEN_REUSED")
    _refresh(client, r2).expect(401, code="INVALID_REFRESH_TOKEN")
    # RefreshTokenService.rotate only revokes refresh tokens: access tokens (tv) stay valid until they expire
    client.get("/api/users/me", token=access2).expect(200)
    # A new login starts a new family
    user.login()
    user.refresh()


def test_reuse_does_not_touch_other_logins(client):
    user = factories.register_customer(client, "refresh-family")
    other_session = client.post("/api/auth/login", {"email": user.email, "password": user.password},
                                token=None).expect(200).body
    r1 = user.refresh_token
    _refresh(client, r1).expect(200)
    _refresh(client, r1).expect(401, code="REFRESH_TOKEN_REUSED")
    _refresh(client, other_session["refreshToken"]).expect(200)


@pytest.mark.parametrize("token", ["garbage", "x" * 100], ids=["garbage", "long"])
def test_refresh_with_unknown_token_is_401(client, token):
    _refresh(client, token).expect(401, code="INVALID_REFRESH_TOKEN")


def test_refresh_validation(client):
    client.post("/api/auth/refresh", {"refreshToken": ""}, token=None).expect(400, code="VALIDATION_FAILED")
    client.post("/api/auth/refresh", {"refreshToken": "x" * 101}, token=None).expect(400, code="VALIDATION_FAILED")


# --- logout -----------------------------------------------------------------------------------------------------

def test_logout_invalidates_the_refresh_token(client):
    user = factories.register_customer(client, "logout")
    other_session = client.post("/api/auth/login", {"email": user.email, "password": user.password},
                                token=None).expect(200).body
    access, refresh = user.tokens["accessToken"], user.refresh_token
    client.post("/api/auth/logout", {"refreshToken": refresh}, token=access).expect(204)
    _refresh(client, refresh).expect(401, code="INVALID_REFRESH_TOKEN")
    # Only that login's family: the other session keeps working
    _refresh(client, other_session["refreshToken"]).expect(200)
    # Logout does not bump tv, so the access token stays usable until it expires (10 min)
    client.get("/api/users/me", token=access).expect(200)


def test_logout_needs_a_token_and_ignores_foreign_refresh_tokens(client):
    a = factories.register_customer(client, "logout-a")
    b = factories.register_customer(client, "logout-b")
    client.post("/api/auth/logout", {"refreshToken": a.refresh_token}, token=None).expect(401)
    # B presenting A's refresh token: 204, but A's token is not revoked
    b.post("/api/auth/logout", {"refreshToken": a.refresh_token}).expect(204)
    a.refresh()

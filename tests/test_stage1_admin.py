"""Stage 1, user-service: admin management (super admin), user search, customer ban/unban, role isolation."""
from __future__ import annotations

import pytest

from tests import data, factories
from tests.pagination import MAX_ADMIN_SIZE, assert_paginated

pytestmark = pytest.mark.stage1


# --- super admin manages admins ---------------------------------------------------------------------------------

def test_create_admin(super_admin, client):
    body = {"email": data.email("admin"), "firstName": "New", "lastName": "Admin", "nic": data.nic(),
            "phone": data.phone(), "temporaryPassword": data.password()}
    created = super_admin.post("/api/super-admin/admins", body).expect(201).body
    assert created["role"] == "ROLE_ADMIN" and created["status"] == "ACTIVE"
    assert created["mustChangePassword"] is True and created["email"] == body["email"]
    assert "temporaryPassword" not in created and "password" not in created
    super_admin.post("/api/super-admin/admins", {**body, "nic": data.nic()}).expect(409, code="EMAIL_ALREADY_EXISTS")


@pytest.mark.parametrize("field, value", [
    ("temporaryPassword", "short"), ("nic", "bad-nic"), ("phone", "123"), ("email", "nope"),
])
def test_create_admin_validation(super_admin, field, value):
    body = {"email": data.email("admin"), "firstName": "New", "lastName": "Admin", "nic": data.nic(),
            "phone": data.phone(), "temporaryPassword": data.password(), field: value}
    resp = super_admin.post("/api/super-admin/admins", body).expect(400, code="VALIDATION_FAILED")
    assert field in {e["field"] for e in resp.body["fieldErrors"]}


def test_list_admins_is_paged(super_admin, admin):
    assert_paginated(super_admin.api, "/api/super-admin/admins", cap=MAX_ADMIN_SIZE)
    body = super_admin.get("/api/super-admin/admins", params={"size": 100}).expect(200).body
    assert all(row["role"] == "ROLE_ADMIN" for row in body["content"])


def test_ban_and_unban_admin(super_admin, client):
    target = factories.create_admin(super_admin, client, "ban-admin")
    old_access, old_refresh = target.tokens["accessToken"], target.refresh_token
    url = f"/api/super-admin/admins/{target.public_id}"

    super_admin.post(f"{url}/ban", {"reason": ""}).expect(400, code="VALIDATION_FAILED")
    banned = super_admin.post(f"{url}/ban", {"reason": "policy violation"}).expect(200).body
    assert banned["status"] == "BANNED" and banned["statusReason"] == "policy violation"
    super_admin.post(f"{url}/ban", {"reason": "again"}).expect(409, code="INVALID_STATUS_TRANSITION")

    # All tokens revoked; sign-in refused with the reason
    client.get("/api/users/me", token=old_access).expect(401, code="TOKEN_REVOKED")
    client.post("/api/auth/refresh", {"refreshToken": old_refresh}, token=None).expect(401)
    refused = client.post("/api/auth/login", {"email": target.email, "password": target.password}, token=None)
    refused.expect(403, code="ACCOUNT_BANNED")
    assert "policy violation" in refused.body["message"]

    super_admin.post(f"{url}/unban", {"reason": ""}).expect(400, code="VALIDATION_FAILED")
    unbanned = super_admin.post(f"{url}/unban", {"reason": "appeal accepted"}).expect(200).body
    assert unbanned["status"] == "ACTIVE"
    super_admin.post(f"{url}/unban", {"reason": "again"}).expect(409, code="INVALID_STATUS_TRANSITION")
    target.login()
    target.get("/api/users/me").expect(200)


def test_super_admin_cannot_be_banned_through_the_api(super_admin):
    super_admin.post(f"/api/super-admin/admins/{super_admin.public_id}/ban", {"reason": "x"}).expect(404)


def test_admin_cannot_call_super_admin_routes(admin):
    admin.get("/api/super-admin/admins").expect(403, code="ACCESS_DENIED")
    admin.post("/api/super-admin/admins", {"email": data.email()}).expect(403, code="ACCESS_DENIED")
    admin.post(f"/api/super-admin/admins/{admin.public_id}/ban", {"reason": "x"}).expect(403, code="ACCESS_DENIED")


# --- admin reads ------------------------------------------------------------------------------------------------

def test_admin_user_search_and_detail(admin, customer):
    assert_paginated(admin.api, "/api/admin/users", cap=MAX_ADMIN_SIZE)
    found = admin.get("/api/admin/users", params={"q": customer.email, "role": "ROLE_CUSTOMER"}).expect(200).body
    assert [row["publicId"] for row in found["content"]] == [customer.public_id]
    detail = admin.get(f"/api/admin/users/{customer.public_id}").expect(200).body
    assert detail["nic"] == customer.extra["registration"]["nic"].upper()  # admins see the full NIC
    admin.get("/api/admin/users/USR-0000-ZZZZZZ").expect(404)
    admin.get("/api/admin/users", params={"sort": "passwordHash"}).expect(400, code="INVALID_SORT")


def test_admin_user_search_unknown_enum_is_400(admin):
    """A bad enum query parameter is a client error (see FINDINGS S1-2)."""
    admin.get("/api/admin/users", params={"role": "ROLE_NOPE"}).expect(400)


# --- role isolation ---------------------------------------------------------------------------------------------

ADMIN_ROUTES = [("GET", "/api/admin/users"), ("GET", "/api/admin/merchants/pending")]
SUPER_ADMIN_ROUTES = [("GET", "/api/super-admin/admins")]
OWNER_ROUTES = [("GET", "/api/merchant/owner/assistants")]
MERCHANT_ROUTES = [("GET", "/api/merchant/application")]

DENIED = {
    "customer": ADMIN_ROUTES + SUPER_ADMIN_ROUTES + OWNER_ROUTES + MERCHANT_ROUTES,
    "merchant": ADMIN_ROUTES + SUPER_ADMIN_ROUTES,
    "assistant": ADMIN_ROUTES + SUPER_ADMIN_ROUTES + OWNER_ROUTES + MERCHANT_ROUTES,
    "admin": SUPER_ADMIN_ROUTES + OWNER_ROUTES + MERCHANT_ROUTES,
    "super_admin": OWNER_ROUTES + MERCHANT_ROUTES,
}


@pytest.mark.parametrize("actor_name, method, path",
                         [(a, m, p) for a, routes in DENIED.items() for m, p in routes])
def test_role_isolation(request, actor_name, method, path):
    actor = request.getfixturevalue(actor_name)
    actor.api.request(method, path).expect(403, code="ACCESS_DENIED")


@pytest.mark.parametrize("actor_name", ["customer", "merchant", "assistant", "admin", "super_admin"])
def test_every_role_can_read_its_profile(request, actor_name):
    actor = request.getfixturevalue(actor_name)
    me = actor.get("/api/users/me").expect(200).body
    assert me["publicId"] == actor.public_id and me["role"] == actor.role


def test_update_profile_cannot_change_email_or_role(client):
    user = factories.register_customer(client, "profile")
    body = {"firstName": "Changed", "lastName": "Name", "phone": data.phone(), "email": data.email(),
            "role": "ROLE_ADMIN"}
    updated = user.put("/api/users/me", body).expect(200).body
    assert updated["firstName"] == "Changed" and updated["phone"] == body["phone"]
    assert updated["email"] == user.email and updated["role"] == "ROLE_CUSTOMER"
    user.put("/api/users/me", {"firstName": "", "lastName": "x"}).expect(400, code="VALIDATION_FAILED")


# --- customer ban / unban ---------------------------------------------------------------------------------------

def test_customer_ban_and_unban(admin, client):
    user = factories.register_customer(client, "ban-customer")
    old_access, old_refresh = user.tokens["accessToken"], user.refresh_token
    url = f"/api/admin/customers/{user.public_id}"

    admin.post(f"{url}/ban", {}).expect(400, code="VALIDATION_FAILED")
    banned = admin.post(f"{url}/ban", {"reason": "chargeback fraud"}).expect(200).body
    assert banned["status"] == "BANNED" and banned["statusReason"] == "chargeback fraud"
    admin.post(f"{url}/ban", {"reason": "again"}).expect(409, code="INVALID_STATUS_TRANSITION")

    # Access tokens revoked (tv bumped), refresh tokens kept: a banned customer may still sign in to view history
    client.get("/api/users/me", token=old_access).expect(401, code="TOKEN_REVOKED")
    refreshed = client.post("/api/auth/refresh", {"refreshToken": old_refresh}, token=None).expect(200).body
    assert refreshed["status"] == "BANNED"
    login = user.login().body
    assert login["status"] == "BANNED" and login["statusReason"] == "chargeback fraud"
    assert user.claims["status"] == "BANNED"
    me = user.get("/api/users/me").expect(200).body
    assert me["status"] == "BANNED" and me["statusReason"] == "chargeback fraud"

    admin.post(f"{url}/unban", {"reason": "resolved"}).expect(200)
    admin.post(f"{url}/unban", {"reason": "again"}).expect(409, code="INVALID_STATUS_TRANSITION")
    login = user.login().body
    assert login["status"] == "ACTIVE" and "statusReason" not in login


def test_customer_ban_route_only_targets_customers(admin, merchant):
    admin.post(f"/api/admin/customers/{merchant.public_id}/ban", {"reason": "x"}).expect(404)
    admin.post("/api/admin/customers/USR-0000-ZZZZZZ/ban", {"reason": "x"}).expect(404)


def test_non_admin_cannot_ban(customer, customer2, merchant):
    for actor in (customer2, merchant):
        actor.post(f"/api/admin/customers/{customer.public_id}/ban", {"reason": "x"}
                   ).expect(403, code="ACCESS_DENIED")

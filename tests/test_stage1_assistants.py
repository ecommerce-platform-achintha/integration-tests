"""Stage 1, user-service: merchant-owner assistant management."""
from __future__ import annotations

import pytest

from tests import data, factories
from tests.pagination import assert_paginated

pytestmark = pytest.mark.stage1

BASE = "/api/merchant/owner/assistants"


def assistant_payload(**overrides) -> dict:
    body = {"email": data.email("assistant"), "firstName": "Ita", "lastName": "Helper", "nic": data.nic(),
            "phone": data.phone(), "permissions": ["ORDER_VIEW"], "temporaryPassword": data.password()}
    body.update(overrides)
    return body


def _login(client, email, password):
    return client.post("/api/auth/login", {"email": email, "password": password}, token=None)


def test_create_assistant(merchant, client):
    body = assistant_payload(permissions=["STOCK_EDIT", "ORDER_VIEW"])
    created = merchant.post(BASE, body).expect(201).body
    assert created["assistantStatus"] == "ACTIVE" and created["mustChangePassword"] is True
    assert created["permissions"] == ["ORDER_VIEW", "STOCK_EDIT"]
    assert created["nic"] == "*" * 9 + body["nic"][-3:]  # Nic.mask for the owner view
    assert "temporaryPassword" not in created

    login = _login(client, body["email"], body["temporaryPassword"]).expect(200).body
    assert login["role"] == "ROLE_ASSISTANT" and login["mustChangePassword"] is True


def test_assistant_forced_password_change_and_claims(merchant, client):
    assistant = factories.create_assistant(merchant, client, ["ORDER_VIEW", "PRODUCT_EDIT"],
                                           change_initial_password=False)
    assistant.get("/api/users/me").expect(403, code="PASSWORD_CHANGE_REQUIRED")
    factories.change_password(assistant)
    assert assistant.claims["perms"] == ["ORDER_VIEW", "PRODUCT_EDIT"]
    assert assistant.store_id == merchant.store_id
    me = assistant.get("/api/users/me").expect(200).body
    assert me["assistantStatus"] == "ACTIVE" and me["permissions"] == ["ORDER_VIEW", "PRODUCT_EDIT"]


@pytest.mark.parametrize("field, value", [
    ("permissions", []), ("temporaryPassword", "short"), ("nic", "nic"), ("email", "x"),
])
def test_create_assistant_validation(merchant, field, value):
    merchant.post(BASE, assistant_payload(**{field: value})).expect(400, code="VALIDATION_FAILED")


def test_unknown_permission_is_400(merchant):
    merchant.post(BASE, assistant_payload(permissions=["DELETE_EVERYTHING"])).expect(400)


def test_pending_merchant_cannot_create_assistants(client):
    pending = factories.register_merchant(client, "pending-owner")
    pending.post(BASE, assistant_payload()).expect(409, code="MERCHANT_NOT_ACTIVE")


def test_nic_is_unique_across_stores(merchant, merchant2):
    nic = data.nic(old_format=True)
    merchant.post(BASE, assistant_payload(nic=nic)).expect(201)
    merchant2.post(BASE, assistant_payload(nic=nic.lower())).expect(409, code="NIC_ALREADY_ASSIGNED")
    merchant.post(BASE, assistant_payload(nic=nic)).expect(409, code="NIC_ALREADY_ASSIGNED")


def test_list_and_get_are_scoped_to_the_store(merchant, merchant2, assistant):
    assert_paginated(merchant.api, BASE)
    listed = merchant.get(BASE, params={"size": 50}).expect(200).body
    assert all(row["publicId"] for row in listed["content"])
    merchant.get(f"{BASE}/{assistant.public_id}").expect(200)
    # Another store's assistant does not exist for merchant2
    merchant2.get(f"{BASE}/{assistant.public_id}").expect(404)
    merchant2.put(f"{BASE}/{assistant.public_id}/permissions", {"permissions": ["ORDER_VIEW"]}).expect(404)
    merchant2.post(f"{BASE}/{assistant.public_id}/ban", {"reason": "x"}).expect(404)
    merchant2.post(f"{BASE}/{assistant.public_id}/remove", {"reason": "x"}).expect(404)
    page = merchant2.get(BASE, params={"size": 50}).expect(200).body
    assert assistant.public_id not in [row["publicId"] for row in page["content"]]


def test_permission_update_revokes_old_tokens(merchant, client):
    assistant = factories.create_assistant(merchant, client, ["ORDER_VIEW"])
    old_access = assistant.tokens["accessToken"]
    url = f"{BASE}/{assistant.public_id}/permissions"
    merchant.put(url, {"permissions": []}).expect(400, code="VALIDATION_FAILED")
    updated = merchant.put(url, {"permissions": ["ORDER_VIEW", "STOCK_EDIT"]}).expect(200).body
    assert updated["permissions"] == ["ORDER_VIEW", "STOCK_EDIT"]

    client.get("/api/users/me", token=old_access).expect(401, code="TOKEN_REVOKED")
    # Only access tokens are revoked: the refresh token gets a new token with the new permissions
    assistant.refresh()
    assert assistant.claims["perms"] == ["ORDER_VIEW", "STOCK_EDIT"]
    assistant.get("/api/users/me").expect(200)


def test_assistants_cannot_manage_assistants(assistant):
    assistant.get(BASE).expect(403, code="ACCESS_DENIED")
    assistant.post(BASE, assistant_payload()).expect(403, code="ACCESS_DENIED")
    assistant.put(f"{BASE}/{assistant.public_id}/permissions", {"permissions": ["ORDER_VIEW"]}
                  ).expect(403, code="ACCESS_DENIED")


def test_ban_and_unban_assistant(merchant, client):
    assistant = factories.create_assistant(merchant, client, ["ORDER_VIEW"])
    old_access = assistant.tokens["accessToken"]
    url = f"{BASE}/{assistant.public_id}"

    merchant.post(f"{url}/ban", {"reason": ""}).expect(400, code="VALIDATION_FAILED")
    banned = merchant.post(f"{url}/ban", {"reason": "left the company"}).expect(200).body
    assert banned["assistantStatus"] == "BANNED_BY_MERCHANT" and banned["statusReason"] == "left the company"
    client.get("/api/users/me", token=old_access).expect(401, code="TOKEN_REVOKED")
    refused = _login(client, assistant.email, assistant.password).expect(403, code="ACCOUNT_BANNED")
    assert "left the company" in refused.body["message"]
    merchant.post(f"{url}/ban", {"reason": "again"}).expect(409, code="INVALID_STATUS_TRANSITION")

    unbanned = merchant.post(f"{url}/unban", {"reason": "back"}).expect(200).body
    assert unbanned["assistantStatus"] == "ACTIVE"
    assistant.login()
    assistant.get("/api/users/me").expect(200)


def test_merchant_ban_releases_the_nic(merchant, merchant2, client):
    nic = data.nic()
    first = merchant.post(BASE, assistant_payload(nic=nic)).expect(201).body
    merchant.post(f"{BASE}/{first['publicId']}/ban", {"reason": "gone"}).expect(200)
    # Released: another store may hire the same person
    merchant2.post(BASE, assistant_payload(nic=nic)).expect(201)
    # ...so lifting the first ban now conflicts
    merchant.post(f"{BASE}/{first['publicId']}/unban", {"reason": "back"}).expect(409, code="NIC_ALREADY_ASSIGNED")


def test_remove_assistant(merchant, client):
    assistant = factories.create_assistant(merchant, client, ["ORDER_VIEW"])
    old_access, old_refresh = assistant.tokens["accessToken"], assistant.refresh_token
    url = f"{BASE}/{assistant.public_id}"
    removed = merchant.post(f"{url}/remove", {"reason": "contract ended"}).expect(200).body
    assert removed["assistantStatus"] == "REMOVED"
    client.get("/api/users/me", token=old_access).expect(401, code="TOKEN_REVOKED")
    client.post("/api/auth/refresh", {"refreshToken": old_refresh}, token=None).expect(401)
    _login(client, assistant.email, assistant.password).expect(403, code="ACCOUNT_REMOVED")
    merchant.post(f"{url}/remove", {"reason": "again"}).expect(409, code="INVALID_STATUS_TRANSITION")
    merchant.post(f"{url}/unban", {"reason": "back"}).expect(409, code="INVALID_STATUS_TRANSITION")
    merchant.put(f"{url}/permissions", {"permissions": ["ORDER_VIEW"]}).expect(409, code="INVALID_STATUS_TRANSITION")


def test_admin_assistant_ban_blocks_the_nic(admin, merchant, merchant2, client):
    nic = data.nic()
    assistant = factories.create_assistant(merchant, client, ["ORDER_VIEW"], nic=nic)
    admin.post(f"/api/admin/users/{assistant.public_id}/assistant-ban", {"reason": ""}
               ).expect(400, code="VALIDATION_FAILED")
    banned = admin.post(f"/api/admin/users/{assistant.public_id}/assistant-ban", {"reason": "fraud"}
                        ).expect(200).body
    assert banned["assistantStatus"] == "BANNED_BY_ADMIN"
    _login(client, assistant.email, assistant.password).expect(403, code="ACCOUNT_BANNED")
    # Permanent NIC block, unlike a merchant ban
    merchant2.post(BASE, assistant_payload(nic=nic)).expect(409, code="NIC_ALREADY_ASSIGNED")
    # The owner cannot lift an admin ban
    merchant.post(f"{BASE}/{assistant.public_id}/unban", {"reason": "x"}).expect(409, code="INVALID_STATUS_TRANSITION")
    # The route only targets assistants
    admin.post(f"/api/admin/users/{merchant.public_id}/assistant-ban", {"reason": "x"}).expect(404)

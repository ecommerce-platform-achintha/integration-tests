"""Stage 1, user-service: customer/merchant registration and merchant onboarding (approve, reject, resubmit)."""
from __future__ import annotations

import pytest

from tests import data, factories
from tests.actors import Actor
from tests.pagination import MAX_ADMIN_SIZE, assert_paginated

pytestmark = pytest.mark.stage1

MAX_APPLICATION_ATTEMPTS = 3  # merchant.max-application-attempts (user-service config)


def _field_errors(resp) -> set[str]:
    return {e["field"] for e in resp.body.get("fieldErrors", [])}


# --- registration -----------------------------------------------------------------------------------------------

def test_register_customer(client):
    body = factories.customer_payload(email=data.email("Mixed").replace("it.", "IT."), nic=data.nic(True).lower())
    resp = client.post("/api/users/register/customer", body, token=None).expect(201)
    profile = resp.body
    assert profile["email"] == body["email"].lower()
    assert profile["role"] == "ROLE_CUSTOMER" and profile["status"] == "ACTIVE"
    assert profile["nic"] == body["nic"].upper()           # Nic.normalize
    assert profile["mustChangePassword"] is False
    assert profile["publicId"] and profile["createdAt"]
    assert profile["phone"] == body["phone"]
    assert not {"password", "passwordHash", "id"} & set(profile)


def test_register_customer_phone_with_spaces(client):
    """PHONE_REGEX ^\\+?[1-9]\\d{7,14}$ is checked before normalizePhone, so separators are refused."""
    body = factories.customer_payload(phone="+94 77 123 4567")
    client.post("/api/users/register/customer", body, token=None).expect(400, code="VALIDATION_FAILED")


def test_register_merchant_is_pending(client):
    merchant = factories.register_merchant(client, "pending-merchant")
    profile = merchant.extra["profile"]
    assert profile["role"] == "ROLE_MERCHANT" and profile["status"] == "PENDING_APPROVAL"
    assert merchant.status == "PENDING_APPROVAL" and merchant.store_id
    app = merchant.get("/api/merchant/application").expect(200).body
    assert app["status"] == "PENDING_APPROVAL" and app["attemptNo"] == 1 and app["attemptsUsed"] == 1
    assert app["maxAttempts"] == MAX_APPLICATION_ATTEMPTS and app["canResubmit"] is False
    assert app["documentKeys"] == merchant.extra["registration"]["documentKeys"]
    assert app["businessName"] == merchant.extra["registration"]["businessName"]


def test_duplicate_email_is_409_case_insensitive(client, customer):
    for email in (customer.email, customer.email.upper()):
        client.post("/api/users/register/customer", factories.customer_payload(email=email), token=None
                    ).expect(409, code="EMAIL_ALREADY_EXISTS")
        client.post("/api/users/register/merchant", factories.merchant_payload(email=email), token=None
                    ).expect(409, code="EMAIL_ALREADY_EXISTS")


@pytest.mark.parametrize("field, value", [
    ("nic", "12345678V"),          # 8 digits
    ("nic", "123456789A"),         # wrong letter
    ("nic", "12345678901"),        # 11 digits
    ("nic", "1234567890123"),      # 13 digits
    ("phone", "0771234567"),       # leading zero
    ("phone", "+9477"),            # too short
    ("phone", "+94771234567890123"),  # too long
    ("phone", "phone-number"),
    ("password", "Short-pw1"),
    ("password", "password1234"),  # deny list
    ("password", "aabbccaabbcc"),  # 3 distinct characters
    ("email", "not-an-email"),
    ("firstName", ""),
])
def test_customer_registration_validation(client, field, value):
    resp = client.post("/api/users/register/customer", factories.customer_payload(**{field: value}), token=None)
    resp.expect(400, code="VALIDATION_FAILED")
    assert field in _field_errors(resp), resp.describe()


@pytest.mark.parametrize("value", ["123456789v", "123456789x", "123456789012"])
def test_valid_nic_formats(client, value):
    client.post("/api/users/register/customer", factories.customer_payload(nic=value), token=None).expect(201)


@pytest.mark.parametrize("keys", [
    [],
    [f"docs/k{i}.pdf" for i in range(11)],
    ["../etc/passwd"],
    ["docs/has space.pdf"],
    ["-starts-with-dash.pdf"],
    [""],
], ids=["empty", "eleven", "dot-start", "space", "dash-start", "blank"])
def test_merchant_document_keys_rules(client, keys):
    resp = client.post("/api/users/register/merchant", factories.merchant_payload(documentKeys=keys), token=None)
    resp.expect(400, code="VALIDATION_FAILED")


def test_merchant_document_keys_accepts_ten(client):
    client.post("/api/users/register/merchant",
                factories.merchant_payload(documentKeys=data.document_keys(10)), token=None).expect(201)


def test_merchant_registration_needs_business_name(client):
    resp = client.post("/api/users/register/merchant", factories.merchant_payload(businessName=" "), token=None)
    resp.expect(400, code="VALIDATION_FAILED")
    assert "businessName" in _field_errors(resp)


def test_registration_ignores_server_controlled_fields(client):
    body = factories.customer_payload(role="ROLE_ADMIN", status="BANNED", storeId="x")
    profile = client.post("/api/users/register/customer", body, token=None).expect(201).body
    assert profile["role"] == "ROLE_CUSTOMER" and profile["status"] == "ACTIVE"


# --- merchant approval ------------------------------------------------------------------------------------------

def _find_pending(admin: Actor, public_id: str) -> dict | None:
    page = 0
    while True:
        body = admin.get("/api/admin/merchants/pending", params={"page": page, "size": 100}).expect(200).body
        for row in body["content"]:
            if row["publicId"] == public_id:
                return row
        page += 1
        if page >= body["totalPages"]:
            return None


def test_pending_list_is_paged_and_lists_new_merchant(admin, client):
    merchant = factories.register_merchant(client, "pending-list")
    factories.register_merchant(client, "pending-list-2")
    assert_paginated(admin.api, "/api/admin/merchants/pending", cap=MAX_ADMIN_SIZE)
    row = _find_pending(admin, merchant.public_id)
    assert row and row["status"] == "PENDING_APPROVAL" and row["role"] == "ROLE_MERCHANT"
    assert row["applicationAttempts"] == 1


def test_approve_then_relogin_shows_active(admin, client):
    merchant = factories.register_merchant(client, "approve")
    old_token = merchant.tokens["accessToken"]
    approved = admin.post(f"/api/admin/merchants/{merchant.public_id}/approve", {"note": "ok"}).expect(200).body
    assert approved["status"] == "ACTIVE"
    assert _find_pending(admin, merchant.public_id) is None
    # Approval does not revoke tokens: the old token is still accepted but its status claim is frozen
    assert merchant.claims["status"] == "PENDING_APPROVAL"
    me = client.get("/api/users/me", token=old_token).expect(200).body
    assert me["status"] == "ACTIVE"  # /me reads the database
    merchant.login()
    assert merchant.status == "ACTIVE"
    # Deciding twice is an invalid transition
    admin.post(f"/api/admin/merchants/{merchant.public_id}/approve").expect(409, code="INVALID_STATUS_TRANSITION")
    admin.post(f"/api/admin/merchants/{merchant.public_id}/reject", {"reason": "late"}
               ).expect(409, code="INVALID_STATUS_TRANSITION")


def test_approve_without_body_and_unknown_merchant(admin, client, customer):
    merchant = factories.register_merchant(client, "approve-no-body")
    admin.post(f"/api/admin/merchants/{merchant.public_id}/approve").expect(200)
    admin.post("/api/admin/merchants/USR-0000-ZZZZZZ/approve").expect(404)
    # A customer's publicId is not a merchant
    admin.post(f"/api/admin/merchants/{customer.public_id}/approve").expect(404)


@pytest.mark.parametrize("body", [None, {}, {"reason": ""}, {"reason": "   "}, {"reason": "x" * 501}],
                         ids=["no-body", "empty", "blank", "spaces", "too-long"])
def test_reject_needs_a_reason(admin, client, body):
    merchant = factories.register_merchant(client, "reject-validation")
    resp = admin.post(f"/api/admin/merchants/{merchant.public_id}/reject", body)
    assert resp.status == 400, resp.describe()


def test_reject_resubmit_until_limit(admin, client):
    merchant = factories.register_merchant(client, "resubmit")
    pid = merchant.public_id
    merchant.post("/api/merchant/application/resubmit",
                  {"businessName": "Again", "documentKeys": data.document_keys()}
                  ).expect(409, code="INVALID_STATUS_TRANSITION")  # still pending

    for attempt in range(1, MAX_APPLICATION_ATTEMPTS + 1):
        reason = f"documents unreadable #{attempt}"
        rejected = admin.post(f"/api/admin/merchants/{pid}/reject", {"reason": reason}).expect(200).body
        assert rejected["status"] == "REJECTED" and rejected["statusReason"] == reason

        login = merchant.login().body
        assert login["status"] == "REJECTED" and login["statusReason"] == reason
        app = merchant.get("/api/merchant/application").expect(200).body
        assert app["status"] == "REJECTED" and app["decision"] == "REJECTED"
        assert app["attemptsUsed"] == attempt
        assert app["canResubmit"] is (attempt < MAX_APPLICATION_ATTEMPTS)

        body = {"businessName": f"Business attempt {attempt + 1}", "documentKeys": data.document_keys()}
        resp = merchant.post("/api/merchant/application/resubmit", body)
        if attempt < MAX_APPLICATION_ATTEMPTS:
            again = resp.expect(200).body
            assert again["status"] == "PENDING_APPROVAL" and again["attemptNo"] == attempt + 1
            assert _find_pending(admin, pid) is not None
        else:
            resp.expect(409, code="APPLICATION_LIMIT_REACHED")


def test_resubmit_validation(admin, client):
    merchant = factories.register_merchant(client, "resubmit-validation")
    admin.post(f"/api/admin/merchants/{merchant.public_id}/reject", {"reason": "no"}).expect(200)
    merchant.post("/api/merchant/application/resubmit", {"businessName": "X", "documentKeys": []}
                  ).expect(400, code="VALIDATION_FAILED")


# --- role checks on onboarding routes ---------------------------------------------------------------------------

@pytest.mark.parametrize("actor_name", ["customer", "merchant", "assistant"])
def test_non_admins_cannot_decide_applications(request, actor_name, client):
    actor = request.getfixturevalue(actor_name)
    pending = factories.register_merchant(client, "rbac-target")
    actor.get("/api/admin/merchants/pending").expect(403, code="ACCESS_DENIED")
    actor.post(f"/api/admin/merchants/{pending.public_id}/approve").expect(403, code="ACCESS_DENIED")


@pytest.mark.parametrize("actor_name", ["customer", "admin", "assistant"])
def test_only_merchants_see_an_application(request, actor_name):
    request.getfixturevalue(actor_name).get("/api/merchant/application").expect(403, code="ACCESS_DENIED")

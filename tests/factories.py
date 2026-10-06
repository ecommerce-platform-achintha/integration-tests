"""
Builders for actors and master data. Every step is a separate function so stage tests can exercise them one by one;
`setup_merchant_store` chains them for the shared `merchant` fixture.
"""
from __future__ import annotations

from typing import Any

from tests import data
from tests.actors import Actor
from tests.api import ApiClient

# Shared master data, created idempotently (409 "already exists" is fine) and forced back to the wanted state
BANK = {"code": "IT_BANK", "name": "Integration Test Bank"}
COURIER = {
    "code": "IT_COURIER",
    "name": "Integration Test Express",
    "trackingUrlTemplate": "https://track.example.com/parcels/{trackingNumber}",
    "trackingNumberRegex": r"^ITX\d{8}$",
    "codSupported": True,
}
CATEGORY = {"slug": "it-general", "name": "Integration Test General", "description": "Owned by integration-tests"}

ALL_ASSISTANT_PERMISSIONS = [
    "ORDER_VIEW", "ORDER_QUOTE", "PAYMENT_VERIFY", "ORDER_SHIP", "PRODUCT_EDIT", "STOCK_EDIT",
    "DISCOUNT_MANAGE", "CUSTOMER_BLOCK", "REVIEW_REPLY",
]


# --- users (user-service) ---------------------------------------------------------------------------------------

def customer_payload(**overrides: Any) -> dict:
    """CustomerRegistrationRequest."""
    body = {
        "email": data.email("customer"),
        "password": data.password(),
        "firstName": "Ita",
        "lastName": "Customer",
        "nic": data.nic(),
        "phone": data.phone(),
    }
    body.update(overrides)
    return body


def merchant_payload(**overrides: Any) -> dict:
    """MerchantRegistrationRequest: documentKeys 1-10 storage keys."""
    body = {
        "email": data.email("merchant"),
        "password": data.password(),
        "firstName": "Ita",
        "lastName": "Merchant",
        "nic": data.nic(),
        "phone": data.phone(),
        "businessName": f"IT Business {data.unique()}",
        "documentKeys": data.document_keys(2),
    }
    body.update(overrides)
    return body


def register_customer(client: ApiClient, label: str = "customer", **overrides: Any) -> Actor:
    """Registers (201, ACTIVE) and logs in."""
    body = customer_payload(**overrides)
    resp = client.post("/api/users/register/customer", body, token=None).expect(201)
    actor = Actor(body["email"], body["password"], client, label, extra={"registration": body,
                                                                          "profile": resp.body})
    actor.login()
    return actor


def register_merchant(client: ApiClient, label: str = "merchant", **overrides: Any) -> Actor:
    """Registers (201, PENDING_APPROVAL) and logs in."""
    body = merchant_payload(**overrides)
    resp = client.post("/api/users/register/merchant", body, token=None).expect(201)
    actor = Actor(body["email"], body["password"], client, label, extra={"registration": body,
                                                                          "profile": resp.body})
    actor.login()
    return actor


def approve_merchant(admin: Actor, merchant: Actor, note: str | None = "approved by integration-tests") -> dict:
    """
    Approves, then re-logs the merchant in: the token's `status` claim is frozen at login and product-service
    reads it.
    """
    resp = admin.post(f"/api/admin/merchants/{merchant.public_id}/approve",
                      {"note": note} if note is not None else None).expect(200)
    merchant.login()
    assert merchant.status == "ACTIVE", f"merchant still {merchant.status} after approval and re-login"
    return resp.body


def change_password(actor: Actor, new_password: str | None = None) -> Actor:
    """POST /api/auth/change-password; the response carries the new tokens (all old ones are revoked)."""
    new_password = new_password or data.password()
    resp = actor.post("/api/auth/change-password",
                      {"currentPassword": actor.password, "newPassword": new_password}).expect(200)
    actor.password = new_password
    actor.set_tokens(resp.body)
    return actor


def create_admin(super_admin: Actor, client: ApiClient, label: str = "admin",
                 change_initial_password: bool = True, **overrides: Any) -> Actor:
    """CreateAdminRequest; admins start with mustChangePassword=true."""
    body = {
        "email": data.email("admin"),
        "firstName": "Ita",
        "lastName": "Admin",
        "nic": data.nic(),
        "phone": data.phone(),
        "temporaryPassword": data.password(),
    }
    body.update(overrides)
    resp = super_admin.post("/api/super-admin/admins", body).expect(201)
    actor = Actor(body["email"], body["temporaryPassword"], client, label, extra={"created": resp.body})
    actor.login()
    if change_initial_password:
        change_password(actor)
    return actor


def create_assistant(merchant: Actor, client: ApiClient, permissions: list[str] | None = None,
                     label: str = "assistant", change_initial_password: bool = True, **overrides: Any) -> Actor:
    """CreateAssistantRequest (owner only, merchant must be ACTIVE); mustChangePassword=true at first login."""
    body = {
        "email": data.email("assistant"),
        "firstName": "Ita",
        "lastName": "Assistant",
        "nic": data.nic(),
        "phone": data.phone(),
        "permissions": permissions if permissions is not None else list(ALL_ASSISTANT_PERMISSIONS),
        "temporaryPassword": data.password(),
    }
    body.update(overrides)
    resp = merchant.post("/api/merchant/owner/assistants", body).expect(201)
    actor = Actor(body["email"], body["temporaryPassword"], client, label, extra={"created": resp.body})
    actor.login()
    if change_initial_password:
        change_password(actor)
    return actor


# --- master data (store-service, product-service) ---------------------------------------------------------------

def ensure_bank(super_admin: Actor, code: str = BANK["code"], name: str = BANK["name"]) -> dict:
    resp = super_admin.post("/api/admin/banks", {"code": code, "name": name, "active": True})
    if resp.status == 409:
        resp.expect(409, code="BANK_CODE_TAKEN")
        current = super_admin.get(f"/api/admin/banks/{code}").expect(200).body
        if not current["active"] or current["name"] != name:
            return super_admin.put(f"/api/admin/banks/{code}", {"name": name, "active": True}).expect(200).body
        return current
    return resp.expect(201).body


def ensure_courier(super_admin: Actor, spec: dict | None = None) -> dict:
    spec = dict(spec or COURIER)
    resp = super_admin.post("/api/admin/couriers", {**spec, "active": True})
    if resp.status == 409:
        resp.expect(409, code="COURIER_CODE_TAKEN")
        current = super_admin.get(f"/api/admin/couriers/{spec['code']}").expect(200).body
        wanted = {k: v for k, v in spec.items() if k != "code"}
        if not current["active"] or any(current[k] != v for k, v in wanted.items()):
            return super_admin.put(f"/api/admin/couriers/{spec['code']}",
                                   {**wanted, "active": True}).expect(200).body
        return current
    return resp.expect(201).body


def ensure_category(admin: Actor, spec: dict | None = None) -> dict:
    spec = dict(spec or CATEGORY)
    resp = admin.post("/api/admin/categories", {**spec, "active": True})
    if resp.status == 409:
        current = admin.get(f"/api/admin/categories/{spec['slug']}").expect(200).body
        if not current["active"]:
            return admin.put(f"/api/admin/categories/{spec['slug']}", {**spec, "active": True}).expect(200).body
        return current
    return resp.expect(201).body


# --- merchant store setup (store-service), one function per step ------------------------------------------------

def store_profile_payload(complete: bool = True, **overrides: Any) -> dict:
    """StoreProfileRequest. Published only when contact phone/email, address, city and district are all set."""
    body: dict[str, Any] = {"name": data.store_name(), "description": "Store created by integration-tests"}
    if complete:
        body.update({
            "contactPhone": data.phone(),
            "contactEmail": data.email("store"),
            "addressLine": "1 Test Street",
            "city": "Colombo",
            "district": "Colombo",
        })
    body.update(overrides)
    return body


def put_store_profile(merchant: Actor, complete: bool = True, **overrides: Any) -> dict:
    body = store_profile_payload(complete, **overrides)
    store = merchant.put("/api/merchant/store", body).expect(200).body
    merchant.extra["store"] = store
    return store


def add_bank_account(merchant: Actor, bank_code: str = BANK["code"], **overrides: Any) -> dict:
    body = {
        "bankCode": bank_code,
        "branch": "Colombo Main",
        "accountName": "IT Merchant Account",
        "accountNumber": data.account_number(),
        "active": True,
    }
    body.update(overrides)
    account = merchant.post("/api/merchant/owner/bank-accounts", body).expect(201).body
    account["_accountNumber"] = body["accountNumber"]  # the full number, for later assertions
    merchant.extra.setdefault("bank_accounts", []).append(account)
    return account


def set_store_couriers(merchant: Actor, courier_codes: list[str]) -> list[dict]:
    couriers = merchant.put("/api/merchant/owner/couriers", {"courierCodes": courier_codes}).expect(200).body
    merchant.extra["couriers"] = couriers
    return couriers


def add_flat_shipping_template(merchant: Actor, courier_code: str = COURIER["code"],
                               amount: str = "350.00") -> dict:
    body = {"courierCode": courier_code, "name": f"Flat {courier_code}", "ruleType": "FLAT",
            "baseAmount": amount, "active": True}
    template = merchant.post("/api/merchant/owner/shipping-templates", body).expect(201).body
    merchant.extra.setdefault("shipping_templates", []).append(template)
    return template


def set_cod(merchant: Actor, enabled: bool) -> dict:
    store = merchant.put("/api/merchant/store/cod", {"enabled": enabled}).expect(200).body
    merchant.extra["store"] = store
    return store


def setup_merchant_store(merchant: Actor, courier_code: str = COURIER["code"],
                         bank_code: str = BANK["code"], cod: bool = True) -> Actor:
    """Complete store profile, one bank account, one courier with a FLAT template, COD on."""
    put_store_profile(merchant, complete=True)
    add_bank_account(merchant, bank_code)
    set_store_couriers(merchant, [courier_code])
    add_flat_shipping_template(merchant, courier_code)
    set_cod(merchant, cod)
    return merchant

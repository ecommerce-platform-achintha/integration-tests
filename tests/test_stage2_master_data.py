"""Stage 2, store-service: banks and couriers master data (super admin writes, admins read)."""
from __future__ import annotations

import pytest

from tests import data, factories
from tests.pagination import MAX_ADMIN_SIZE, assert_paginated

pytestmark = pytest.mark.stage2


def courier_payload(**overrides) -> dict:
    body = {"code": data.code("ITC"), "name": "Throwaway Courier",
            "trackingUrlTemplate": "https://parcel.example.com/t/{trackingNumber}",
            "trackingNumberRegex": r"^TC\d{5}$", "codSupported": False, "active": True}
    body.update(overrides)
    return body


# --- banks ------------------------------------------------------------------------------------------------------

def test_bank_crud(super_admin, admin):
    code = data.code("ITB")
    created = super_admin.post("/api/admin/banks", {"code": code, "name": "Throwaway Bank"}).expect(201).body
    assert created == {**created, "code": code, "name": "Throwaway Bank", "active": True}
    super_admin.post("/api/admin/banks", {"code": code, "name": "Again"}).expect(409, code="BANK_CODE_TAKEN")

    assert admin.get(f"/api/admin/banks/{code}").expect(200).body["name"] == "Throwaway Bank"
    updated = super_admin.put(f"/api/admin/banks/{code}", {"name": "Renamed Bank", "active": False}).expect(200).body
    assert updated["name"] == "Renamed Bank" and updated["active"] is False
    inactive = admin.get("/api/admin/banks", params={"active": "false", "size": 100}).expect(200).body
    assert code in [b["code"] for b in inactive["content"]]

    super_admin.delete(f"/api/admin/banks/{code}").expect(204)
    admin.get(f"/api/admin/banks/{code}").expect(404)
    super_admin.delete(f"/api/admin/banks/{code}").expect(404)


@pytest.mark.parametrize("code", ["boc", "B", "1BANK", "BANK-1", "A" * 21, ""],
                         ids=["lower", "one-char", "digit-first", "dash", "too-long", "blank"])
def test_bank_code_rules(super_admin, code):
    super_admin.post("/api/admin/banks", {"code": code, "name": "Bad"}).expect(400, code="VALIDATION_FAILED")


def test_bank_name_required(super_admin):
    super_admin.post("/api/admin/banks", {"code": data.code("ITB"), "name": "  "}).expect(400)
    super_admin.put(f"/api/admin/banks/{factories.BANK['code']}", {"name": "x"}).expect(400)  # active missing


def test_bank_list_is_paged(admin, bank):
    assert_paginated(admin.api, "/api/admin/banks", cap=MAX_ADMIN_SIZE)


def test_bank_access_control(admin, customer, merchant, assistant, bank):
    admin.post("/api/admin/banks", {"code": data.code("ITB"), "name": "x"}).expect(403, code="ACCESS_DENIED")
    admin.put(f"/api/admin/banks/{bank['code']}", {"name": "x", "active": True}).expect(403, code="ACCESS_DENIED")
    admin.delete(f"/api/admin/banks/{bank['code']}").expect(403, code="ACCESS_DENIED")
    for actor in (customer, merchant, assistant):
        actor.get("/api/admin/banks").expect(403, code="ACCESS_DENIED")
        actor.get(f"/api/admin/banks/{bank['code']}").expect(403, code="ACCESS_DENIED")


def test_bank_in_use_cannot_be_deleted(super_admin, admin, client):
    code = data.code("ITB")
    super_admin.post("/api/admin/banks", {"code": code, "name": "In Use Bank"}).expect(201)
    owner = factories.new_approved_merchant(client, admin, "bank-in-use")
    factories.add_bank_account(owner, code)
    super_admin.delete(f"/api/admin/banks/{code}").expect(409, code="BANK_IN_USE")
    # Deactivating is the way out: no new accounts with it
    super_admin.put(f"/api/admin/banks/{code}", {"name": "In Use Bank", "active": False}).expect(200)
    owner.post("/api/merchant/owner/bank-accounts",
               {"bankCode": code, "branch": "b", "accountName": "n", "accountNumber": data.account_number()}
               ).expect(400, code="BANK_NOT_AVAILABLE")
    banks = owner.get("/api/merchant/owner/bank-accounts/banks").expect(200).body
    assert code not in [b["code"] for b in banks]


# --- couriers ---------------------------------------------------------------------------------------------------

def test_courier_crud(super_admin, admin):
    body = courier_payload()
    created = super_admin.post("/api/admin/couriers", body).expect(201).body
    assert {k: created[k] for k in body} == body
    super_admin.post("/api/admin/couriers", body).expect(409, code="COURIER_CODE_TAKEN")
    assert admin.get(f"/api/admin/couriers/{body['code']}").expect(200).body["code"] == body["code"]

    update = {k: v for k, v in body.items() if k != "code"} | {"name": "Renamed", "codSupported": True}
    updated = super_admin.put(f"/api/admin/couriers/{body['code']}", update).expect(200).body
    assert updated["name"] == "Renamed" and updated["codSupported"] is True

    super_admin.delete(f"/api/admin/couriers/{body['code']}").expect(204)
    admin.get(f"/api/admin/couriers/{body['code']}").expect(404)


@pytest.mark.parametrize("field, value", [
    ("trackingUrlTemplate", "http://parcel.example.com/t/{trackingNumber}"),   # not https
    ("trackingUrlTemplate", "https://parcel.example.com/t/"),                  # no placeholder
    ("trackingUrlTemplate", "parcel.example.com/{trackingNumber}"),            # not absolute
    ("trackingUrlTemplate", "ftp://parcel.example.com/{trackingNumber}"),
    ("trackingNumberRegex", "([A-Z"),                                          # does not compile
    ("trackingNumberRegex", ""),
    ("code", "dhl"),
    ("codSupported", None),
    ("name", " "),
])
def test_courier_validation(super_admin, field, value):
    resp = super_admin.post("/api/admin/couriers", courier_payload(**{field: value}))
    resp.expect(400, code="VALIDATION_FAILED")


def test_courier_list_is_paged(admin, courier, courier_no_cod):
    assert_paginated(admin.api, "/api/admin/couriers", cap=MAX_ADMIN_SIZE)
    active = admin.get("/api/admin/couriers", params={"active": "true", "size": 100}).expect(200).body
    assert {courier["code"], courier_no_cod["code"]} <= {c["code"] for c in active["content"]}


def test_courier_access_control(admin, customer, merchant, courier):
    admin.post("/api/admin/couriers", courier_payload()).expect(403, code="ACCESS_DENIED")
    admin.delete(f"/api/admin/couriers/{courier['code']}").expect(403, code="ACCESS_DENIED")
    for actor in (customer, merchant):
        actor.get("/api/admin/couriers").expect(403, code="ACCESS_DENIED")


def test_courier_in_use_cannot_be_deleted(super_admin, admin, client):
    body = courier_payload()
    super_admin.post("/api/admin/couriers", body).expect(201)
    owner = factories.new_approved_merchant(client, admin, "courier-in-use")
    factories.set_store_couriers(owner, [body["code"]])
    super_admin.delete(f"/api/admin/couriers/{body['code']}").expect(409, code="COURIER_IN_USE")

    # Still in use through a shipping template, even after the store disables it
    factories.add_flat_shipping_template(owner, body["code"])
    factories.set_store_couriers(owner, [])
    super_admin.delete(f"/api/admin/couriers/{body['code']}").expect(409, code="COURIER_IN_USE")

    # Deactivated couriers cannot be enabled and are not offered
    update = {k: v for k, v in body.items() if k != "code"} | {"active": False}
    super_admin.put(f"/api/admin/couriers/{body['code']}", update).expect(200)
    owner.put("/api/merchant/owner/couriers", {"courierCodes": [body["code"]]}
              ).expect(400, code="COURIER_NOT_AVAILABLE")
    available = owner.get("/api/merchant/owner/couriers/available").expect(200).body
    assert body["code"] not in [c["code"] for c in available]

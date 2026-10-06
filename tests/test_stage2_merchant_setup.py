"""Stage 2, store-service: the merchant's bank accounts, enabled couriers and shipping templates."""
from __future__ import annotations

import pytest

from tests import data, factories
from tests.pagination import assert_paginated

pytestmark = pytest.mark.stage2

ACCOUNTS = "/api/merchant/owner/bank-accounts"
COURIERS = "/api/merchant/owner/couriers"
TEMPLATES = "/api/merchant/owner/shipping-templates"
MAX_BANK_ACCOUNTS = 10  # app.stores.max-bank-accounts


@pytest.fixture
def owner(admin, client, bank, courier, courier_no_cod):
    """Throwaway approved merchant with a complete store and nothing else."""
    return factories.new_approved_merchant(client, admin, "setup-owner")


def account_payload(**overrides) -> dict:
    body = {"bankCode": factories.BANK["code"], "branch": "Kandy", "accountName": "IT Owner",
            "accountNumber": data.account_number()}
    body.update(overrides)
    return body


# --- bank accounts ----------------------------------------------------------------------------------------------

def test_bank_account_is_masked_everywhere(owner):
    number = "1234-5678 9012"  # spaces and dashes are removed before validation
    created = owner.post(ACCOUNTS, account_payload(accountNumber=number)).expect(201)
    account = created.body
    assert account["accountNumberMasked"] == "****9012" and account["bankName"] == factories.BANK["name"]
    assert "accountNumber" not in account and "123456789012" not in created.text

    for resp in (owner.get(f"{ACCOUNTS}/{account['publicId']}"), owner.get(ACCOUNTS)):
        resp.expect(200)
        assert "123456789012" not in resp.text and "****9012" in resp.text


def test_bank_account_update_and_delete(owner):
    account = factories.add_bank_account(owner)
    url = f"{ACCOUNTS}/{account['publicId']}"
    # Without accountNumber the number is kept
    kept = owner.put(url, {"bankCode": factories.BANK["code"], "branch": "Galle", "accountName": "Renamed",
                           "active": True}).expect(200).body
    assert kept["accountNumberMasked"] == account["accountNumberMasked"] and kept["branch"] == "Galle"
    changed = owner.put(url, {"bankCode": factories.BANK["code"], "branch": "Galle", "accountName": "Renamed",
                              "accountNumber": "99887766", "active": False}).expect(200).body
    assert changed["accountNumberMasked"] == "****7766" and changed["active"] is False
    owner.put(url, {"bankCode": factories.BANK["code"], "branch": "x", "accountName": "x"}).expect(400)  # active

    owner.delete(url).expect(204)
    owner.get(url).expect(404)
    listed = owner.get(ACCOUNTS, params={"size": 50}).expect(200).body
    assert account["publicId"] not in [a["publicId"] for a in listed["content"]]


@pytest.mark.parametrize("number", ["12345", "1" * 21, "12345abc", "", "12.345.678"],
                         ids=["5-digits", "21-digits", "letters", "blank", "dots"])
def test_bank_account_number_rules(owner, number):
    resp = owner.post(ACCOUNTS, account_payload(accountNumber=number)).expect(400, code="VALIDATION_FAILED")
    if number:
        assert number not in resp.text  # never echoed back


def test_bank_account_unknown_bank(owner):
    owner.post(ACCOUNTS, account_payload(bankCode="NO_SUCH_BANK")).expect(400, code="BANK_NOT_AVAILABLE")


def test_bank_account_limit(owner):
    for _ in range(MAX_BANK_ACCOUNTS):
        owner.post(ACCOUNTS, account_payload()).expect(201)
    owner.post(ACCOUNTS, account_payload()).expect(409, code="BANK_ACCOUNT_LIMIT_REACHED")
    assert_paginated(owner.api, ACCOUNTS)


def test_bank_accounts_need_a_store(admin, client):
    no_store = factories.new_approved_merchant(client, admin, "no-store", store=None)
    no_store.post(ACCOUNTS, account_payload()).expect(409, code="STORE_PROFILE_REQUIRED")


def test_other_merchant_cannot_touch_bank_accounts(merchant, merchant2):
    account = merchant.extra["bank_accounts"][0]
    url = f"{ACCOUNTS}/{account['publicId']}"
    merchant2.get(url).expect(404)
    merchant2.put(url, {"bankCode": factories.BANK["code"], "branch": "x", "accountName": "x",
                        "active": False}).expect(404)
    merchant2.delete(url).expect(404)
    listed = merchant2.get(ACCOUNTS, params={"size": 50}).expect(200).body
    assert account["publicId"] not in [a["publicId"] for a in listed["content"]]
    assert merchant.get(url).expect(200).body["active"] is True  # untouched


def test_active_banks_for_merchants(owner, bank):
    banks = owner.get(f"{ACCOUNTS}/banks").expect(200).body
    assert bank["code"] in [b["code"] for b in banks] and all(b["active"] for b in banks)


# --- store couriers ---------------------------------------------------------------------------------------------

def test_replace_store_couriers(owner, courier, courier_no_cod):
    assert owner.get(COURIERS).expect(200).body == []
    both = factories.set_store_couriers(owner, [courier_no_cod["code"], courier["code"], courier["code"]])
    assert sorted(c["code"] for c in both) == sorted([courier["code"], courier_no_cod["code"]])
    cod = {c["code"]: c["codSupported"] for c in both}
    assert cod == {courier["code"]: True, courier_no_cod["code"]: False}
    only_one = factories.set_store_couriers(owner, [courier["code"]])
    assert [c["code"] for c in only_one] == [courier["code"]]
    assert factories.set_store_couriers(owner, []) == []
    owner.put(COURIERS, {"courierCodes": ["NO_SUCH_COURIER"]}).expect(400, code="COURIER_NOT_AVAILABLE")
    owner.put(COURIERS, {"courierCodes": ["lower"]}).expect(400, code="VALIDATION_FAILED")
    owner.put(COURIERS, {}).expect(400, code="VALIDATION_FAILED")
    available = owner.get(f"{COURIERS}/available").expect(200).body
    assert {courier["code"], courier_no_cod["code"]} <= {c["code"] for c in available}


# --- shipping templates -----------------------------------------------------------------------------------------

def test_shipping_template_types(owner, courier, courier_no_cod):
    factories.set_store_couriers(owner, [courier["code"], courier_no_cod["code"]])

    flat = factories.add_flat_shipping_template(owner, courier["code"], "350.00")
    assert flat["ruleType"] == "FLAT" and flat["rates"] == [] and float(flat["baseAmount"]) == 350
    # One template per courier
    owner.post(TEMPLATES, {"courierCode": courier["code"], "name": "Second", "ruleType": "FLAT",
                           "baseAmount": "1.00"}).expect(409, code="SHIPPING_TEMPLATE_EXISTS")

    per_district = owner.post(TEMPLATES, {
        "courierCode": courier_no_cod["code"], "name": "Districts", "ruleType": "PER_DISTRICT",
        "baseAmount": "600.00",
        "rates": [{"district": "Colombo", "amount": "300.00"}, {"district": "Kandy", "amount": "450.00"}],
    }).expect(201).body
    assert [r["district"] for r in per_district["rates"]] == ["Colombo", "Kandy"]

    # Same courier, switched to weight bands: returned sorted by weight
    weight = owner.put(f"{TEMPLATES}/{per_district['publicId']}", {
        "courierCode": courier_no_cod["code"], "name": "Bands", "ruleType": "WEIGHT_BAND",
        "rates": [{"maxWeightKg": "5", "amount": "800.00"}, {"maxWeightKg": "1", "amount": "300.00"}],
        "active": True,
    }).expect(200).body
    assert [float(r["maxWeightKg"]) for r in weight["rates"]] == [1.0, 5.0]
    assert weight["publicId"] == per_district["publicId"]

    listed = owner.get(TEMPLATES).expect(200).body
    assert {t["courierCode"] for t in listed["content"]} == {courier["code"], courier_no_cod["code"]}
    assert_paginated(owner.api, TEMPLATES)

    owner.delete(f"{TEMPLATES}/{flat['publicId']}").expect(204)
    owner.get(f"{TEMPLATES}/{flat['publicId']}").expect(404)


@pytest.mark.parametrize("body", [
    {"ruleType": "FLAT"},                                                              # no baseAmount
    {"ruleType": "FLAT", "baseAmount": "1", "rates": [{"district": "Colombo", "amount": "1"}]},
    {"ruleType": "PER_DISTRICT", "rates": []},
    {"ruleType": "PER_DISTRICT", "rates": [{"amount": "1"}]},                           # no district
    {"ruleType": "PER_DISTRICT", "rates": [{"district": "Colombo", "amount": "1"},
                                           {"district": "colombo", "amount": "2"}]},    # duplicate
    {"ruleType": "PER_DISTRICT", "rates": [{"district": "Colombo", "maxWeightKg": "1", "amount": "1"}]},
    {"ruleType": "WEIGHT_BAND", "rates": [{"district": "Colombo", "amount": "1"}]},     # no weight
    {"ruleType": "WEIGHT_BAND", "rates": [{"maxWeightKg": "1", "amount": "1"},
                                          {"maxWeightKg": "1.000", "amount": "2"}]},    # duplicate band
    {"ruleType": "WEIGHT_BAND", "rates": [{"maxWeightKg": "0", "amount": "1"}]},        # weight > 0
    {"ruleType": "FLAT", "baseAmount": "-1"},
    {"ruleType": "FLAT", "baseAmount": "1.234"},
], ids=["flat-no-base", "flat-rates", "district-empty", "district-missing", "district-dup", "district-weight",
        "band-no-weight", "band-dup", "band-zero", "negative", "3-decimals"])
def test_shipping_template_validation(owner, courier, body):
    factories.set_store_couriers(owner, [courier["code"]])
    resp = owner.post(TEMPLATES, {"courierCode": courier["code"], "name": "Bad", **body})
    resp.expect(400, code="VALIDATION_FAILED")


def test_shipping_template_needs_an_enabled_courier(owner, courier, courier_no_cod):
    factories.set_store_couriers(owner, [courier["code"]])
    owner.post(TEMPLATES, {"courierCode": courier_no_cod["code"], "name": "x", "ruleType": "FLAT",
                           "baseAmount": "1"}).expect(400, code="COURIER_NOT_ENABLED")


def test_other_merchant_cannot_touch_templates(merchant, merchant2):
    template = merchant.extra["shipping_templates"][0]
    url = f"{TEMPLATES}/{template['publicId']}"
    merchant2.get(url).expect(404)
    merchant2.put(url, {"courierCode": template["courierCode"], "name": "x", "ruleType": "FLAT",
                        "baseAmount": "1"}).expect(404)
    merchant2.delete(url).expect(404)
    merchant.get(url).expect(200)

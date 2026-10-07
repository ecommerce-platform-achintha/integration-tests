"""Stage 4, order-service: the bank-transfer flow (store bank accounts, payment submission with Idempotency-Key,
verify, reject and resubmit) and duplicate references across stores (generic 409, FLG- row, admin review)."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from tests import data, factories
from tests.factories import payment_body, submit_payment

pytestmark = pytest.mark.stage4

CUSTOMER_ORDERS = "/api/customer/orders"
MERCHANT_ORDERS = "/api/merchant/orders"
FLAGS = "/api/admin/flagged-references"


def instant(value: str) -> datetime:
    """Parses '...T06:45:52.859775616Z' (Java gives nanoseconds; Python keeps microseconds)."""
    seconds, _, fraction = value.rstrip("Z").partition(".")
    return datetime.fromisoformat(f"{seconds}.{(fraction + '000000')[:6]}+00:00")


def same_instant(a: str, b: str) -> bool:
    """Equal at Postgres precision: it stores microseconds and rounds, so allow 1 microsecond."""
    return abs(instant(a) - instant(b)) <= timedelta(microseconds=1)


def account_of(merchant) -> dict:
    return merchant.extra["bank_accounts"][0]


@pytest.fixture
def awaiting(buyer, merchant, item) -> dict:
    return factories.bank_order_awaiting_payment(buyer, merchant, item, quantity=2)


# --- bank accounts ----------------------------------------------------------------------------------------------

def test_bank_accounts_only_while_a_transfer_is_expected(buyer, merchant, item):
    order = factories.place_order(buyer, merchant, item, method="BANK_TRANSFER")
    url = f"{CUSTOMER_ORDERS}/{order['publicId']}/bank-accounts"
    buyer.get(url).expect(409, code="INVALID_ORDER_STATE")           # not confirmed yet
    factories.quote(merchant, order)
    buyer.get(url).expect(409, code="INVALID_ORDER_STATE")
    confirmed = factories.confirm(buyer, order)
    assert confirmed["status"] == "AWAITING_PAYMENT" and confirmed["deadlineType"] == "PAYMENT_SUBMISSION"

    resp = buyer.get(url).expect(200)
    assert "no-store" in resp.headers.get("Cache-Control", "")
    [account] = resp.body
    mine = account_of(merchant)
    assert account["publicId"] == mine["publicId"] and account["bankCode"] == factories.BANK["code"]
    assert account["accountNumber"] == mine["_accountNumber"]  # full number, for the payer only
    assert account["bankName"] and account["accountName"] == "IT Merchant Account"


def test_bank_accounts_are_not_shown_for_cod_or_to_others(client, buyer, merchant, item):
    cod = factories.order_to_ready(buyer, merchant, item)
    buyer.get(f"{CUSTOMER_ORDERS}/{cod['publicId']}/bank-accounts").expect(409, code="INVALID_ORDER_STATE")
    order = factories.bank_order_awaiting_payment(buyer, merchant, item)
    other = factories.new_buyer(client, "nosy-buyer")
    other.get(f"{CUSTOMER_ORDERS}/{order['publicId']}/bank-accounts").expect(404)


# --- submit, verify, reject -------------------------------------------------------------------------------------

def test_submit_and_verify(buyer, merchant, item, awaiting):
    assert factories.stock_of(merchant, item) == (48, 2)
    body = payment_body(account_of(merchant)["publicId"], sourceAccountNumber="1234-5678 9012",
                        attachmentKeys=["receipts/a.png"])
    submitted = submit_payment(buyer, awaiting, body).expect(200).body
    assert submitted["status"] == "PAYMENT_SUBMITTED" and submitted["deadlineType"] == "PAYMENT_VERIFICATION"
    [payment] = submitted["payments"]
    assert payment["publicId"].startswith("PAY-") and payment["status"] == "SUBMITTED"
    assert payment["referenceNumber"] == body["referenceNumber"]
    assert payment["sourceAccountMasked"].endswith("9012") and "12345678" not in str(payment)  # masked
    assert payment["destinationBankCode"] == factories.BANK["code"] and len(payment["attachmentUrls"]) == 1
    assert factories.stock_of(merchant, item) == (48, 2)  # still held

    # The store sees the same masked payment
    store_view = merchant.get(f"{MERCHANT_ORDERS}/{awaiting['publicId']}").expect(200).body
    assert store_view["payments"][0]["sourceAccountMasked"] == payment["sourceAccountMasked"]

    verified = merchant.post(f"{MERCHANT_ORDERS}/{awaiting['publicId']}/payment/verify").expect(200).body
    assert verified["status"] == "READY_TO_SHIP" and verified["deadlineType"] == "SHIP_BY"
    assert verified["payments"][0]["status"] == "VERIFIED" and verified["payments"][0]["decidedAt"]
    assert factories.stock_of(merchant, item) == (48, 0)  # committed
    merchant.post(f"{MERCHANT_ORDERS}/{awaiting['publicId']}/payment/verify").expect(409, code="INVALID_ORDER_STATE")

    shipped = factories.ship(merchant, awaiting)
    assert shipped["status"] == "SHIPPED"
    assert buyer.post(f"{CUSTOMER_ORDERS}/{awaiting['publicId']}/received").expect(200).body["status"] == "COMPLETED"


def test_reject_then_resubmit(buyer, merchant, awaiting):
    first = payment_body(account_of(merchant)["publicId"])
    submit_payment(buyer, awaiting, first).expect(200)
    url = f"{MERCHANT_ORDERS}/{awaiting['publicId']}/payment/reject"
    merchant.post(url, {"reason": " "}).expect(400, code="VALIDATION_FAILED")
    rejected = merchant.post(url, {"reason": "nothing arrived"}).expect(200).body
    assert rejected["status"] == "AWAITING_PAYMENT" and rejected["deadlineType"] == "PAYMENT_SUBMISSION"
    assert rejected["payments"][0]["status"] == "REJECTED"
    assert rejected["payments"][0]["rejectionReason"] == "nothing arrived"
    # The deadline goes back to the original payment deadline, not a new one (stored at microsecond precision)
    assert same_instant(rejected["deadlineAt"], awaiting["deadlineAt"])

    # A rejected reference is not "used": the customer may resubmit, even with the same reference
    again = submit_payment(buyer, awaiting, first).expect(200).body
    assert again["status"] == "PAYMENT_SUBMITTED"
    assert sorted(p["status"] for p in again["payments"]) == ["REJECTED", "SUBMITTED"]
    assert merchant.post(f"{MERCHANT_ORDERS}/{awaiting['publicId']}/payment/verify").expect(200).body[
        "status"] == "READY_TO_SHIP"


def test_payment_idempotency(buyer, merchant, awaiting):
    key = f"it-{data.unique()}"
    body = payment_body(account_of(merchant)["publicId"])
    submit_payment(buyer, awaiting, body, key=None).expect(400, code="IDEMPOTENCY_KEY_REQUIRED")
    first = submit_payment(buyer, awaiting, body, key=key).expect(200)
    replay = submit_payment(buyer, awaiting, body, key=key).expect(200)
    assert replay.headers.get("Idempotent-Replayed") == "true" and replay.body == first.body
    assert len(buyer.get(f"{CUSTOMER_ORDERS}/{awaiting['publicId']}").expect(200).body["payments"]) == 1
    submit_payment(buyer, awaiting, {**body, "referenceNumber": "OTHER1"}, key=key
                   ).expect(422, code="IDEMPOTENCY_KEY_REUSED")


def test_payment_preconditions(client, admin, buyer, merchant, merchant2, item, awaiting):
    cod = factories.order_to_ready(buyer, merchant, item)
    submit_payment(buyer, cod, payment_body(account_of(merchant)["publicId"])).expect(409, code="INVALID_ORDER_STATE")
    placed = factories.place_order(buyer, merchant, item, method="BANK_TRANSFER")
    submit_payment(buyer, placed, payment_body(account_of(merchant)["publicId"])).expect(409, code="INVALID_ORDER_STATE")
    # Another store's account, or an unknown one
    for account in (account_of(merchant2)["publicId"], "BAC-0000-ZZZZZZ"):
        submit_payment(buyer, awaiting, payment_body(account)).expect(400, code="BANK_ACCOUNT_NOT_AVAILABLE")
    other = factories.new_buyer(client, "other-payer")
    submit_payment(other, awaiting, payment_body(account_of(merchant)["publicId"])).expect(404)


@pytest.mark.parametrize("change", [
    lambda b: {k: v for k, v in b.items() if k != "referenceNumber"},
    lambda b: {**b, "referenceNumber": "#bad!"},
    lambda b: {**b, "referenceNumber": "R" * 61},
    lambda b: {**b, "sourceBankCode": "boc"},
    lambda b: {**b, "sourceAccountNumber": "12"},
    lambda b: {**b, "sourceAccountNumber": "12ab3456"},
    lambda b: {**b, "storeBankAccountPublicId": "nope"},
    lambda b: {**b, "attachmentKeys": ["../etc/passwd"]},
    lambda b: {**b, "attachmentKeys": [f"r/{n}.png" for n in range(11)]},
], ids=["no-reference", "bad-reference", "long-reference", "bad-bank", "short-account", "letters-account",
        "bad-account-id", "bad-key", "too-many-keys"])
def test_payment_validation(buyer, merchant, awaiting, change):
    submit_payment(buyer, awaiting, change(payment_body(account_of(merchant)["publicId"]))
                   ).expect(400, code="VALIDATION_FAILED")


def test_payment_decisions_need_payment_verify(client, buyer, merchant, merchant2, awaiting):
    submit_payment(buyer, awaiting, payment_body(account_of(merchant)["publicId"])).expect(200)
    viewer = factories.create_assistant(merchant, client, ["ORDER_VIEW", "ORDER_SHIP"])
    viewer.post(f"{MERCHANT_ORDERS}/{awaiting['publicId']}/payment/verify").expect(403, code="ACCESS_DENIED")
    viewer.post(f"{MERCHANT_ORDERS}/{awaiting['publicId']}/payment/reject", {"reason": "x"}
                ).expect(403, code="ACCESS_DENIED")
    merchant2.post(f"{MERCHANT_ORDERS}/{awaiting['publicId']}/payment/verify").expect(404)
    verifier = factories.create_assistant(merchant, client, ["PAYMENT_VERIFY"])
    assert verifier.post(f"{MERCHANT_ORDERS}/{awaiting['publicId']}/payment/verify").expect(200).body[
        "status"] == "READY_TO_SHIP"


# --- duplicate references ---------------------------------------------------------------------------------------

def test_duplicate_reference_across_stores_is_flagged(client, admin, merchant, merchant2, item, item2):
    """Both stores have an IT_BANK account: the (normalised reference, destination bank) pair is unique platform-wide."""
    first_buyer, second_buyer = factories.new_buyer(client, "payer-a"), factories.new_buyer(client, "payer-b")
    first = factories.bank_order_awaiting_payment(first_buyer, merchant, item)
    second = factories.bank_order_awaiting_payment(second_buyer, merchant2, item2)
    reference = f"ITDUP{data.unique()}".upper()
    submit_payment(first_buyer, first, payment_body(account_of(merchant)["publicId"], reference)).expect(200)

    # Same reference, written differently (case and spaces are normalised away)
    disguised = f"  {reference[:5].lower()} {reference[5:]} "
    resp = submit_payment(second_buyer, second, payment_body(account_of(merchant2)["publicId"], disguised))
    resp.expect(409, code="PAYMENT_REJECTED")
    # Generic: nothing about the other order, store or customer
    assert resp.body["message"] == ("The payment could not be accepted. Check the reference and bank details, "
                                    "or contact support.")
    assert first["publicId"] not in resp.text and reference not in resp.text
    assert second_buyer.get(f"{CUSTOMER_ORDERS}/{second['publicId']}").expect(200).body["status"] == "AWAITING_PAYMENT"

    flags = admin.get(FLAGS, params={"status": "OPEN", "size": 100}).expect(200).body["content"]
    [flag] = [f for f in flags if f["orderPublicId"] == second["publicId"]]
    assert flag["publicId"].startswith("FLG-") and flag["reference"] == reference
    assert flag["bankCode"] == factories.BANK["code"] and flag["originalOrderPublicId"] == first["publicId"]
    assert flag["customerPublicId"] == second_buyer.public_id
    assert flag["storePublicId"] == factories.store_id(merchant2)
    assert admin.get(f"{FLAGS}/{flag['publicId']}").expect(200).body == flag

    # Admin review: a note is required, and a flag is reviewed once
    admin.post(f"{FLAGS}/{flag['publicId']}/review", {"action": "REVIEWED", "note": " "}
               ).expect(400, code="VALIDATION_FAILED")
    reviewed = admin.post(f"{FLAGS}/{flag['publicId']}/review", {"action": "ESCALATED", "note": "same receipt twice"}
                          ).expect(200).body
    assert reviewed["status"] == "ESCALATED" and reviewed["reviewNote"] == "same receipt twice"
    assert reviewed["reviewedBy"] == admin.public_id and reviewed["reviewedAt"]
    admin.post(f"{FLAGS}/{flag['publicId']}/review", {"action": "REVIEWED", "note": "again"}
               ).expect(409, code="FLAG_ALREADY_REVIEWED")

    # The second customer can still pay with a genuine reference
    submit_payment(second_buyer, second, payment_body(account_of(merchant2)["publicId"])).expect(200)


def test_flagged_references_are_admin_only(customer, merchant, assistant, client):
    for actor in (customer, merchant, assistant):
        actor.get(FLAGS).expect(403, code="ACCESS_DENIED")
    client.get(FLAGS, token=None).expect(401)
    admin_get = client.get(f"{FLAGS}/FLG-0000-ZZZZZZ", token=None)
    assert admin_get.status == 401

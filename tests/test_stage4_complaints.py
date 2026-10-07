"""Stage 4, order-service: complaints (file, store response, admin decision and the merchant penalty in
store-service), admin resolution of open orders, and objections to a COD refusal."""
from __future__ import annotations

import pytest

from tests import factories
from tests.waits import wait_until

pytestmark = pytest.mark.stage4

CUSTOMER_COMPLAINTS = "/api/customer/complaints"
MERCHANT_COMPLAINTS = "/api/merchant/orders/complaints"
ADMIN_COMPLAINTS = "/api/admin/complaints"
MERCHANT_ORDERS = "/api/merchant/orders"
CUSTOMER_ORDERS = "/api/customer/orders"


def complaint_body(order: dict, **overrides) -> dict:
    body = {"orderPublicId": order["publicId"], "type": "NOT_AS_DESCRIBED", "text": "The colour is wrong"}
    body.update(overrides)
    return body


# --- complaints -------------------------------------------------------------------------------------------------

@pytest.mark.slow
def test_complaint_upheld_penalises_the_store(client, admin, buyer, super_admin, category, courier, bank):
    shop = factories.new_shop(client, admin, "complaint-shop")
    order = factories.order_to_shipped(buyer, shop, shop.extra["product"])
    penalty = int(next(s["value"] for s in super_admin.get("/api/super-admin/settings").expect(200).body
                       if s["key"] == "score.merchant.complaint-upheld"))

    created = buyer.post(CUSTOMER_COMPLAINTS, complaint_body(order, attachmentKeys=["complaints/photo.png"])
                         ).expect(201).body
    assert created["publicId"].startswith("CMP-") and created["status"] == "OPEN"
    assert created["orderPublicId"] == order["publicId"] and created["storePublicId"] == factories.store_id(shop)
    assert created["type"] == "NOT_AS_DESCRIBED" and len(created["attachmentUrls"]) == 1
    buyer.post(CUSTOMER_COMPLAINTS, complaint_body(order)).expect(409, code="COMPLAINT_NOT_ALLOWED")  # one open
    fetched = buyer.get(f"{CUSTOMER_COMPLAINTS}/{created['publicId']}").expect(200).body
    # createdAt: nanoseconds in the create response, microseconds once read back from Postgres
    assert {k: v for k, v in fetched.items() if k != "createdAt"} == {k: v for k, v in created.items()
                                                                       if k != "createdAt"}
    assert [c["publicId"] for c in buyer.get(CUSTOMER_COMPLAINTS).expect(200).body["content"]] == [created["publicId"]]

    # The store responds once
    assert [c["publicId"] for c in shop.get(MERCHANT_COMPLAINTS).expect(200).body["content"]] == [created["publicId"]]
    url = f"{MERCHANT_COMPLAINTS}/{created['publicId']}/response"
    shop.post(url, {"response": " "}).expect(400)
    responded = shop.post(url, {"response": "We will send the right colour"}).expect(200).body
    assert responded["status"] == "UNDER_REVIEW" and responded["merchantResponse"] == "We will send the right colour"
    shop.post(url, {"response": "again"}).expect(409, code="COMPLAINT_ALREADY_RESPONDED")

    # Admin decision
    under_review = admin.get(ADMIN_COMPLAINTS, params={"status": "UNDER_REVIEW", "size": 100}).expect(200).body
    assert created["publicId"] in [c["publicId"] for c in under_review["content"]]
    decide = f"{ADMIN_COMPLAINTS}/{created['publicId']}/decision"
    admin.post(decide, {"decision": "UPHELD", "notes": " "}).expect(400)
    upheld = admin.post(decide, {"decision": "UPHELD", "notes": "Photos confirm it"}).expect(200).body
    assert upheld["status"] == "UPHELD" and upheld["adminNotes"] == "Photos confirm it" and upheld["decidedAt"]
    admin.post(decide, {"decision": "DISMISSED", "notes": "x"}).expect(409, code="COMPLAINT_ALREADY_DECIDED")
    assert buyer.get(f"{CUSTOMER_COMPLAINTS}/{created['publicId']}").expect(200).body["status"] == "UPHELD"

    # ComplaintDecided -> store-service: merchant penalty and metric
    def penalised():
        m = shop.get("/api/merchant/owner/metrics").expect(200).body
        assert m["complaintsUpheld"] == 1 and m["score"] == penalty, m
        return True
    wait_until(penalised, message="merchant penalty for an upheld complaint")


def test_complaint_dismissed_has_no_penalty(client, admin, buyer, category, courier, bank):
    shop = factories.new_shop(client, admin, "complaint-dismissed-shop")
    order = factories.order_to_ready(buyer, shop, shop.extra["product"])
    created = buyer.post(CUSTOMER_COMPLAINTS, complaint_body(order, type="NOT_SHIPPED", text="Still waiting")
                         ).expect(201).body
    # An admin may decide without a store response
    dismissed = admin.post(f"{ADMIN_COMPLAINTS}/{created['publicId']}/decision",
                           {"decision": "DISMISSED", "notes": "Within the ship-by time"}).expect(200).body
    assert dismissed["status"] == "DISMISSED"
    shop.post(f"{MERCHANT_COMPLAINTS}/{created['publicId']}/response", {"response": "late"}
              ).expect(409, code="COMPLAINT_ALREADY_RESPONDED")
    # After a decision a new complaint may be filed
    buyer.post(CUSTOMER_COMPLAINTS, complaint_body(order, type="OTHER")).expect(201)
    m = shop.get("/api/merchant/owner/metrics").expect(200).body
    assert m["complaintsUpheld"] == 0 and m["score"] == 0


def test_complaint_only_from_ready_to_ship(buyer, merchant, item):
    order = factories.place_order(buyer, merchant, item)
    buyer.post(CUSTOMER_COMPLAINTS, complaint_body(order)).expect(409, code="COMPLAINT_NOT_ALLOWED")
    factories.quote(merchant, order)
    buyer.post(CUSTOMER_COMPLAINTS, complaint_body(order)).expect(409, code="COMPLAINT_NOT_ALLOWED")
    factories.confirm(buyer, order)
    buyer.post(CUSTOMER_COMPLAINTS, complaint_body(order)).expect(201)
    cancelled = factories.place_order(buyer, merchant, item)
    buyer.post(f"{CUSTOMER_ORDERS}/{cancelled['publicId']}/cancel").expect(200)
    buyer.post(CUSTOMER_COMPLAINTS, complaint_body(cancelled)).expect(409, code="COMPLAINT_NOT_ALLOWED")


def test_complaint_after_completion(buyer, merchant, item):
    order = factories.order_to_shipped(buyer, merchant, item)
    buyer.post(f"{CUSTOMER_ORDERS}/{order['publicId']}/received").expect(200)
    buyer.post(CUSTOMER_COMPLAINTS, complaint_body(order, type="WRONG_ITEM")).expect(201)


@pytest.mark.parametrize("change", [
    lambda b: {**b, "type": None},
    lambda b: {**b, "type": "LATE"},
    lambda b: {**b, "text": " "},
    lambda b: {**b, "text": "x" * 4001},
    lambda b: {**b, "orderPublicId": "nope"},
    lambda b: {**b, "attachmentKeys": [f"c/{n}.png" for n in range(11)]},
], ids=["no-type", "unknown-type", "blank-text", "long-text", "bad-order-id", "too-many-keys"])
def test_complaint_validation(buyer, merchant, item, change):
    order = factories.order_to_ready(buyer, merchant, item)
    assert buyer.post(CUSTOMER_COMPLAINTS, change(complaint_body(order))).status == 400


def test_complaint_access(client, buyer, merchant, merchant2, item):
    order = factories.order_to_ready(buyer, merchant, item)
    other = factories.new_buyer(client, "complaint-other")
    other.post(CUSTOMER_COMPLAINTS, complaint_body(order)).expect(404)
    created = buyer.post(CUSTOMER_COMPLAINTS, complaint_body(order)).expect(201).body
    other.get(f"{CUSTOMER_COMPLAINTS}/{created['publicId']}").expect(404)
    merchant2.get(f"{MERCHANT_COMPLAINTS}/{created['publicId']}").expect(404)
    merchant2.post(f"{MERCHANT_COMPLAINTS}/{created['publicId']}/response", {"response": "x"}).expect(404)
    viewer = factories.create_assistant(merchant, client, ["ORDER_VIEW"])
    viewer.get(f"{MERCHANT_COMPLAINTS}/{created['publicId']}").expect(200)
    viewer.post(f"{MERCHANT_COMPLAINTS}/{created['publicId']}/response", {"response": "x"}
                ).expect(403, code="ACCESS_DENIED")
    for actor in (buyer, merchant):
        actor.get(ADMIN_COMPLAINTS).expect(403, code="ACCESS_DENIED")


# --- admin resolution -------------------------------------------------------------------------------------------

def test_admin_force_complete(admin, buyer, merchant, item):
    order = factories.place_order(buyer, merchant, item, quantity=2)
    url = f"/api/admin/orders/{order['publicId']}/resolve"
    admin.post(url, {"action": "FORCE_COMPLETE", "reason": " "}).expect(400)
    done = admin.post(url, {"action": "FORCE_COMPLETE", "reason": "merchant confirmed delivery by phone"}
                      ).expect(200).body
    assert done["status"] == "COMPLETED" and done["reasonCode"] == "ADMIN_FORCE_COMPLETE"
    assert done["reason"] == "merchant confirmed delivery by phone"
    assert factories.stock_of(merchant, item) == (48, 0)  # the hold is committed
    # A resolution is not a customer merit
    assert admin.get(f"/api/admin/customers/{buyer.public_id}/score").expect(200).body["score"] == 0
    admin.post(url, {"action": "CANCEL", "reason": "again"}).expect(409, code="INVALID_ORDER_STATE")


@pytest.mark.parametrize("stage", ["placed", "ready"])
def test_admin_cancel(admin, buyer, merchant, item, stage):
    order = (factories.place_order(buyer, merchant, item) if stage == "placed"
             else factories.order_to_ready(buyer, merchant, item))
    closed = admin.post(f"/api/admin/orders/{order['publicId']}/resolve",
                        {"action": "CANCEL", "reason": "fraud suspected"}).expect(200).body
    assert closed["status"] == "CLOSED_BY_ADMIN" and closed["reasonCode"] == "ADMIN_CANCEL"
    assert buyer.get(f"{CUSTOMER_ORDERS}/{order['publicId']}").expect(200).body["status"] == "CLOSED_BY_ADMIN"
    # Released while held; a confirmed COD order was already sold
    assert factories.stock_of(merchant, item) == ((50, 0) if stage == "placed" else (49, 0))


def test_resolve_is_admin_only(buyer, merchant, assistant, item):
    order = factories.place_order(buyer, merchant, item)
    for actor in (buyer, merchant, assistant):
        actor.post(f"/api/admin/orders/{order['publicId']}/resolve", {"action": "CANCEL", "reason": "x"}
                   ).expect(403, code="ACCESS_DENIED")
    assert buyer.post(f"/api/admin/orders/{order['publicId']}/resolve", {"action": "NUKE", "reason": "x"}).status == 403


# --- COD objection ----------------------------------------------------------------------------------------------

def failed_cod_order(buyer, merchant, item, failure: str = "COD_REFUSED") -> dict:
    order = factories.order_to_shipped(buyer, merchant, item)
    merchant.post(f"{MERCHANT_ORDERS}/{order['publicId']}/delivery-failed", {"type": failure}).expect(200)
    return order


def test_cod_objection_upheld_reverses_the_refusal(admin, buyer, merchant, item):
    order = failed_cod_order(buyer, merchant, item)
    score_url = f"/api/admin/customers/{buyer.public_id}/score"
    assert admin.get(score_url).expect(200).body["codRefusals"] == 1

    url = f"{CUSTOMER_ORDERS}/{order['publicId']}/cod-objection"
    buyer.post(url, {"text": " "}).expect(400)
    objected = buyer.post(url, {"text": "I was home all day"}).expect(200).body
    assert objected["status"] == "DELIVERY_FAILED"
    buyer.post(url, {"text": "again"}).expect(409, code="OBJECTION_ALREADY_EXISTS")

    open_ones = admin.get("/api/admin/orders/cod-objections", params={"status": "OPEN", "size": 100}).expect(200).body
    [objection] = [o for o in open_ones["content"] if o["orderPublicId"] == order["publicId"]]
    assert objection["customerPublicId"] == buyer.public_id and objection["text"] == "I was home all day"

    decide = f"/api/admin/orders/{order['publicId']}/cod-objection/decision"
    admin.post(decide, {"decision": "UPHELD", "note": " "}).expect(400)
    decided = admin.post(decide, {"decision": "UPHELD", "note": "Courier admitted no attempt"}).expect(200).body
    assert decided["status"] == "UPHELD" and decided["adminNote"] == "Courier admitted no attempt"
    assert admin.get(score_url).expect(200).body["codRefusals"] == 0
    admin.post(decide, {"decision": "DISMISSED", "note": "x"}).expect(409, code="OBJECTION_ALREADY_DECIDED")


def test_cod_objection_dismissed_keeps_the_refusal(admin, buyer, merchant, item):
    order = failed_cod_order(buyer, merchant, item, "UNDELIVERABLE")
    buyer.post(f"{CUSTOMER_ORDERS}/{order['publicId']}/cod-objection", {"text": "Address was fine"}).expect(200)
    admin.post(f"/api/admin/orders/{order['publicId']}/cod-objection/decision",
               {"decision": "DISMISSED", "note": "Address incomplete"}).expect(200)
    assert admin.get(f"/api/admin/customers/{buyer.public_id}/score").expect(200).body["codRefusals"] == 1


def test_cod_objection_only_for_failed_cod_delivery(client, buyer, merchant, item):
    shipped = factories.order_to_shipped(buyer, merchant, item)
    buyer.post(f"{CUSTOMER_ORDERS}/{shipped['publicId']}/cod-objection", {"text": "x"}
               ).expect(409, code="OBJECTION_NOT_ALLOWED")
    failed = failed_cod_order(buyer, merchant, item)
    other = factories.new_buyer(client, "objection-other")
    other.post(f"{CUSTOMER_ORDERS}/{failed['publicId']}/cod-objection", {"text": "x"}).expect(404)
    merchant.get("/api/admin/orders/cod-objections").expect(403, code="ACCESS_DENIED")

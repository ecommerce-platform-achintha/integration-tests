"""Stage 4, order-service: the full COD flow end to end (stock in product-service, metrics in store-service, customer
score) and the other transitions (cancel, reject, decline, re-quote, quote rules, delivery failed)."""
from __future__ import annotations

from decimal import Decimal

import pytest

from tests import factories
from tests.factories import COURIER, COURIER_NO_COD, quote_body, store_id
from tests.waits import wait_until

pytestmark = pytest.mark.stage4

MERCHANT_ORDERS = "/api/merchant/orders"
CUSTOMER_ORDERS = "/api/customer/orders"


def dec(value) -> Decimal:
    return Decimal(str(value))


def customer_score(admin, customer) -> dict:
    return admin.get(f"/api/admin/customers/{customer.public_id}/score").expect(200).body


def setting(super_admin, key: str) -> int:
    return int(next(s["value"] for s in super_admin.get("/api/super-admin/settings").expect(200).body
                    if s["key"] == key))


# --- the full COD flow ------------------------------------------------------------------------------------------

@pytest.mark.slow
def test_full_cod_flow(client, admin, buyer, category, courier, bank):
    """Fresh store, so its metrics start from zero: place -> assistant quote -> confirm -> ship -> received."""
    shop = factories.new_shop(client, admin, "cod-flow-shop")
    product = shop.extra["product"]
    quoter = factories.create_assistant(shop, client, ["ORDER_VIEW", "ORDER_QUOTE"])

    order = factories.place_order(buyer, shop, product, quantity=3)
    assert factories.stock_of(shop, product) == (47, 3)  # held, not sold

    store_view = shop.get(f"{MERCHANT_ORDERS}/{order['publicId']}").expect(200).body
    assert store_view["customerPublicId"] == buyer.public_id
    assert store_view["customer"] == {"score": 0, "codRefusals": 0, "codSuspended": False}
    assert [o["publicId"] for o in shop.get(MERCHANT_ORDERS).expect(200).body["content"]] == [order["publicId"]]

    # The assistant quotes: 2 of 3, courier charge, a packing charge and a quote discount
    quoted = quoter.post(f"{MERCHANT_ORDERS}/{order['publicId']}/quote", quote_body(
        order, quantities={factories.variant_id(product): 2}, otherCharges=[{"label": "Packing", "amount": "50.00"}],
        quoteDiscount="100.00")).expect(200).body
    assert quoted["status"] == "AWAITING_CUSTOMER_CONFIRMATION" and quoted["quoteRevision"] == 1
    assert quoted["deadlineType"] == "CUSTOMER_CONFIRMATION"
    amounts = quoted["amounts"]
    assert dec(amounts["itemsTotal"]) == Decimal("2000.00") and dec(amounts["courierCharge"]) == Decimal("350.00")
    assert amounts["courierCode"] == COURIER["code"] and dec(amounts["otherChargesTotal"]) == Decimal("50.00")
    assert dec(amounts["quoteDiscount"]) == Decimal("100.00") and dec(amounts["grandTotal"]) == Decimal("2300.00")
    assert quoted["items"][0]["orderedQuantity"] == 3 and quoted["items"][0]["quantity"] == 2
    assert factories.stock_of(shop, product) == (48, 2)  # the hold follows the quote

    confirmed = factories.confirm(buyer, order)
    assert confirmed["status"] == "READY_TO_SHIP" and confirmed["deadlineType"] == "SHIP_BY"
    assert factories.stock_of(shop, product) == (48, 0)  # committed: sold

    url = f"{MERCHANT_ORDERS}/{order['publicId']}/ship"
    shop.post(url, {"courierCode": COURIER["code"], "trackingNumber": "WRONG123"}
              ).expect(400, code="INVALID_TRACKING_NUMBER")
    shop.post(url, {"courierCode": COURIER_NO_COD["code"], "trackingNumber": "NC123456"}
              ).expect(400, code="COURIER_NOT_AVAILABLE")  # not one of the store's couriers
    shop.post(url, {"courierCode": COURIER["code"], "trackingNumber": "ITX 1234"}
              ).expect(400, code="VALIDATION_FAILED")
    shipped = shop.post(url, {"courierCode": COURIER["code"], "trackingNumber": "ITX12345678"}).expect(200).body
    assert shipped["status"] == "SHIPPED" and shipped["deadlineType"] == "AUTO_COMPLETE"
    assert shipped["shipment"]["trackingNumber"] == "ITX12345678"
    assert shipped["shipment"]["trackingUrl"] == "https://track.example.com/parcels/ITX12345678"
    shop.post(url, {"courierCode": COURIER["code"], "trackingNumber": "ITX12345679"}
              ).expect(409, code="INVALID_ORDER_STATE")

    done = buyer.post(f"{CUSTOMER_ORDERS}/{order['publicId']}/received").expect(200).body
    assert done["status"] == "COMPLETED" and done["timeline"]["completedAt"] and "deadlineAt" not in done
    assert done["shipment"]["trackingUrl"].endswith("ITX12345678")
    assert factories.stock_of(shop, product) == (48, 0)

    wait_until(lambda: customer_score(admin, buyer)["score"] == 1, message="customer score +1")
    assert [e["type"] for e in customer_score(admin, buyer)["recentEvents"]] == ["ORDER_COMPLETED"]

    def metrics_done():
        m = shop.get("/api/merchant/owner/metrics").expect(200).body
        assert m["ordersReceived"] == 1 and m["ordersQuoted"] == 1 and m["completedOrders"] == 1, m
        assert dec(m["revenue"]) == Decimal("2300.00") and dec(m["acceptanceRatePercent"]) == Decimal("100.00"), m
        assert m["cancelledOrders"] == 0 and m["ordersRejected"] == 0 and m["score"] == 1, m
        return True
    wait_until(metrics_done, message="store metrics after OrderCompleted")


# --- other transitions ------------------------------------------------------------------------------------------

def test_customer_cancel_releases_stock(admin, buyer, merchant, item):
    order = factories.place_order(buyer, merchant, item, quantity=2)
    assert factories.stock_of(merchant, item) == (48, 2)
    cancelled = buyer.post(f"{CUSTOMER_ORDERS}/{order['publicId']}/cancel").expect(200).body
    assert cancelled["status"] == "CANCELLED_BY_CUSTOMER" and cancelled["reasonCode"] == "CUSTOMER_CANCELLED"
    assert cancelled["timeline"]["closedAt"] and "deadlineAt" not in cancelled
    assert factories.stock_of(merchant, item) == (50, 0)
    buyer.post(f"{CUSTOMER_ORDERS}/{order['publicId']}/cancel").expect(409, code="INVALID_ORDER_STATE")
    assert customer_score(admin, buyer)["score"] == 0  # free of penalty


def test_cancel_only_before_the_quote(buyer, merchant, item):
    order = factories.place_order(buyer, merchant, item)
    factories.quote(merchant, order)
    buyer.post(f"{CUSTOMER_ORDERS}/{order['publicId']}/cancel").expect(409, code="INVALID_ORDER_STATE")


@pytest.mark.parametrize("reason", ["OUT_OF_STOCK", "LOW_CUSTOMER_SCORE", "COD_HISTORY", "CANNOT_DELIVER_TO_AREA",
                                    "OTHER"])
def test_merchant_reject_with_each_reason(admin, buyer, merchant, item, reason):
    order = factories.place_order(buyer, merchant, item)
    rejected = merchant.post(f"{MERCHANT_ORDERS}/{order['publicId']}/reject",
                             {"reasonCode": reason, "note": "integration-tests"}).expect(200).body
    assert rejected["status"] == "REJECTED_BY_MERCHANT" and rejected["reasonCode"] == reason
    assert rejected["reason"] == "integration-tests"
    assert buyer.get(f"{CUSTOMER_ORDERS}/{order['publicId']}").expect(200).body["status"] == "REJECTED_BY_MERCHANT"
    assert factories.stock_of(merchant, item) == (50, 0)
    assert customer_score(admin, buyer)["score"] == 0  # no penalty for either side


@pytest.mark.parametrize("body", [{}, {"reasonCode": "TOO_BUSY"}, {"reasonCode": None}],
                         ids=["missing", "unknown", "null"])
def test_reject_needs_a_valid_reason(buyer, merchant, item, body):
    order = factories.place_order(buyer, merchant, item)
    resp = merchant.post(f"{MERCHANT_ORDERS}/{order['publicId']}/reject", body)
    assert resp.status == 400, resp.describe()


def test_reject_only_before_the_quote(buyer, merchant, item):
    order = factories.place_order(buyer, merchant, item)
    factories.quote(merchant, order)
    merchant.post(f"{MERCHANT_ORDERS}/{order['publicId']}/reject", {"reasonCode": "OTHER"}
                  ).expect(409, code="INVALID_ORDER_STATE")


def test_customer_decline_costs_a_point(admin, buyer, merchant, item):
    order = factories.place_order(buyer, merchant, item, quantity=2)
    factories.quote(merchant, order)
    declined = buyer.post(f"{CUSTOMER_ORDERS}/{order['publicId']}/decline").expect(200).body
    assert declined["status"] == "DECLINED_BY_CUSTOMER" and declined["reasonCode"] == "QUOTE_DECLINED"
    assert factories.stock_of(merchant, item) == (50, 0)
    score = customer_score(admin, buyer)
    assert score["score"] == -1 and [e["type"] for e in score["recentEvents"]] == ["QUOTE_DECLINED"]
    buyer.post(f"{CUSTOMER_ORDERS}/{order['publicId']}/confirm").expect(409, code="INVALID_ORDER_STATE")


def test_confirm_and_decline_need_a_quote(buyer, merchant, item):
    order = factories.place_order(buyer, merchant, item)
    for action in ("confirm", "decline", "received"):
        buyer.post(f"{CUSTOMER_ORDERS}/{order['publicId']}/{action}").expect(409, code="INVALID_ORDER_STATE")


def test_requote_limit(super_admin, buyer, merchant, item):
    max_revisions = setting(super_admin, "orders.max-quote-revisions")
    order = factories.place_order(buyer, merchant, item, quantity=5)
    vid = factories.variant_id(item)
    # First quote plus `max_revisions` revisions, each reducing the quantity by one
    for revision in range(1, max_revisions + 2):
        quoted = factories.quote(merchant, order, quantities={vid: 5 - revision})
        assert quoted["quoteRevision"] == revision and quoted["items"][0]["quantity"] == 5 - revision
    assert factories.stock_of(merchant, item)[1] == 5 - (max_revisions + 1)
    merchant.post(f"{MERCHANT_ORDERS}/{order['publicId']}/quote", quote_body(order, quantities={vid: 1})
                  ).expect(409, code="QUOTE_REVISION_LIMIT_REACHED")


def test_requote_cannot_raise_a_quantity_again(buyer, merchant, item):
    order = factories.place_order(buyer, merchant, item, quantity=3)
    vid = factories.variant_id(item)
    factories.quote(merchant, order, quantities={vid: 1})
    merchant.post(f"{MERCHANT_ORDERS}/{order['publicId']}/quote", quote_body(order, quantities={vid: 2})
                  ).expect(400, code="INVALID_QUOTE")


@pytest.fixture
def order_of_two(buyer, merchant, item):
    """One line, quantity 2."""
    return factories.place_order(buyer, merchant, item, quantity=2)


@pytest.mark.parametrize("change", [
    lambda o: {"lines": [{"variantPublicId": o["items"][0]["variantPublicId"], "quantity": 3}]},   # raise
    lambda o: {"lines": [{"variantPublicId": "VAR-0000-ZZZZZZ", "quantity": 1}]},                  # add
    lambda o: {"lines": [{"variantPublicId": o["items"][0]["variantPublicId"], "quantity": 0}]},   # remove all
    lambda o: {"lines": [{"variantPublicId": o["items"][0]["variantPublicId"], "quantity": 1}] * 2},
    lambda o: {"quoteDiscount": "999999.00"},
], ids=["raise-quantity", "add-item", "remove-all", "duplicate-line", "discount-exceeds-total"])
def test_quote_rules(merchant, order_of_two, change):
    order = order_of_two
    merchant.post(f"{MERCHANT_ORDERS}/{order['publicId']}/quote", {**quote_body(order), **change(order)}
                  ).expect(400, code="INVALID_QUOTE")


def test_quote_removes_a_line(buyer, merchant, item, category):
    second = factories.create_active_product(merchant, factories.simple_product_payload(category["slug"],
                                                                                        price="200.00"))
    vid1, vid2 = factories.variant_id(item), factories.variant_id(second)
    l1 = factories.cart_line(factories.add_to_cart(buyer, vid1, 1), vid1)["cartItemPublicId"]
    l2 = factories.cart_line(factories.add_to_cart(buyer, vid2, 2), vid2)["cartItemPublicId"]
    placed = factories.checkout(buyer, factories.checkout_body(buyer, [l1, l2], {store_id(merchant): "COD"})
                                ).expect(201).body["results"][0]
    order = buyer.get(f"{CUSTOMER_ORDERS}/{placed['orderPublicId']}").expect(200).body
    assert dec(order["amounts"]["itemsTotal"]) == Decimal("1400.00")
    quoted = factories.quote(merchant, order, quantities={vid2: 0}, courier_charge="0.00")
    lines = {i["variantPublicId"]: i for i in quoted["items"]}
    assert lines[vid2]["quantity"] == 0 and lines[vid2]["orderedQuantity"] == 2
    assert dec(quoted["amounts"]["grandTotal"]) == Decimal("1000.00")
    assert factories.stock_of(merchant, second) == (50, 0)  # the removed line's hold is released


@pytest.mark.parametrize("change", [
    lambda b: {k: v for k, v in b.items() if k != "courierCharge"},
    lambda b: {**b, "courierCharge": "-1.00"},
    lambda b: {**b, "courierCharge": "1.001"},
    lambda b: {**b, "lines": []},
    lambda b: {**b, "otherCharges": [{"label": "", "amount": "10.00"}]},
    lambda b: {**b, "otherCharges": [{"label": "Zero", "amount": "0.00"}]},
    lambda b: {**b, "quoteDiscount": "-5.00"},
    lambda b: {k: v for k, v in b.items() if k != "courierCode"},
], ids=["no-courier-charge", "negative-charge", "three-decimals", "no-lines", "blank-label", "zero-charge",
        "negative-discount", "no-courier"])
def test_quote_validation(buyer, merchant, item, change):
    order = factories.place_order(buyer, merchant, item)
    merchant.post(f"{MERCHANT_ORDERS}/{order['publicId']}/quote", change(quote_body(order))
                  ).expect(400, code="VALIDATION_FAILED")


def test_quote_courier_rules(client, admin, buyer, category, courier, courier_no_cod, bank):
    shop = factories.new_shop(client, admin, "two-courier-shop", couriers=[COURIER["code"], COURIER_NO_COD["code"]])
    order = factories.place_order(buyer, shop, shop.extra["product"])
    url = f"{MERCHANT_ORDERS}/{order['publicId']}/quote"
    shop.post(url, quote_body(order, courier_code="ITC_NOT_MINE")).expect(400, code="COURIER_NOT_AVAILABLE")
    # One of the store's couriers, but it does not collect cash: refused for a COD order
    shop.post(url, quote_body(order, courier_code=COURIER_NO_COD["code"])).expect(400, code="COURIER_NOT_AVAILABLE")
    assert shop.post(url, quote_body(order)).expect(200).body["amounts"]["courierCode"] == COURIER["code"]


def test_ship_rules(buyer, merchant, item):
    order = factories.place_order(buyer, merchant, item)
    url = f"{MERCHANT_ORDERS}/{order['publicId']}/ship"
    shop_body = {"courierCode": COURIER["code"], "trackingNumber": "ITX12345678"}
    merchant.post(url, shop_body).expect(409, code="INVALID_ORDER_STATE")  # not confirmed yet
    factories.quote(merchant, order)
    merchant.post(url, shop_body).expect(409, code="INVALID_ORDER_STATE")
    factories.confirm(buyer, order)
    for tracking in ("ITX1234567", "ITX123456789", "itx12345678", "NC123456"):
        merchant.post(url, {**shop_body, "trackingNumber": tracking}).expect(400, code="INVALID_TRACKING_NUMBER")
    assert merchant.post(url, shop_body).expect(200).body["status"] == "SHIPPED"


@pytest.mark.parametrize("failure", ["COD_REFUSED", "UNDELIVERABLE"])
def test_delivery_failed_counts_as_cod_refusal(admin, buyer, merchant, item, failure):
    order = factories.order_to_shipped(buyer, merchant, item)
    failed = merchant.post(f"{MERCHANT_ORDERS}/{order['publicId']}/delivery-failed",
                           {"type": failure, "note": "nobody home"}).expect(200).body
    assert failed["status"] == "DELIVERY_FAILED" and failed["deliveryFailureType"] == failure
    assert failed["reasonCode"] == failure and failed["timeline"]["deliveryFailedAt"]
    score = customer_score(admin, buyer)
    assert score["codRefusals"] == 1 and score["codRefusalsTowardSuspension"] == 1
    assert score["score"] == 0 and score["codSuspended"] is False  # a refusal never changes the score
    assert factories.stock_of(merchant, item) == (49, 0)  # already sold; not returned to stock

    # The store sees the refusal on the customer's next order
    nxt = factories.place_order(buyer, merchant, item)
    assert merchant.get(f"{MERCHANT_ORDERS}/{nxt['publicId']}").expect(200).body["customer"]["codRefusals"] == 1
    buyer.post(f"{CUSTOMER_ORDERS}/{order['publicId']}/received").expect(409, code="INVALID_ORDER_STATE")


def test_delivery_failed_rules(buyer, merchant, item):
    order = factories.order_to_ready(buyer, merchant, item)
    url = f"{MERCHANT_ORDERS}/{order['publicId']}/delivery-failed"
    merchant.post(url, {"type": "COD_REFUSED"}).expect(409, code="INVALID_ORDER_STATE")  # not shipped yet
    factories.ship(merchant, order)
    assert merchant.post(url, {}).status == 400
    assert merchant.post(url, {"type": "LOST"}).status == 400


def test_order_lists_filter_and_page(buyer, merchant, item):
    first = factories.place_order(buyer, merchant, item)
    second = factories.place_order(buyer, merchant, item)
    buyer.post(f"{CUSTOMER_ORDERS}/{first['publicId']}/cancel").expect(200)
    mine = buyer.get(CUSTOMER_ORDERS, params={"status": "AWAITING_MERCHANT"}).expect(200).body["content"]
    assert [o["publicId"] for o in mine] == [second["publicId"]]
    page = buyer.get(CUSTOMER_ORDERS, params={"size": 1, "sort": "placedAt,asc"}).expect(200).body
    assert page["totalElements"] == 2 and page["totalPages"] == 2 and page["content"][0]["publicId"] == first["publicId"]
    buyer.get(CUSTOMER_ORDERS, params={"sort": "secret,asc"}).expect(400)
    buyer.get(CUSTOMER_ORDERS, params={"page": -1}).expect(400)
    store = merchant.get(MERCHANT_ORDERS, params={"status": "CANCELLED_BY_CUSTOMER", "size": 100}).expect(200).body
    assert first["publicId"] in [o["publicId"] for o in store["content"]]

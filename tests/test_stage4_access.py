"""Stage 4, order-service: who may see and act on an order. Foreign orders are 404 (never 403) to customers and
stores; an assistant without the route's permission gets 403."""
from __future__ import annotations

import pytest

from tests import factories
from tests.factories import COURIER, quote_body

pytestmark = pytest.mark.stage4

MERCHANT_ORDERS = "/api/merchant/orders"
CUSTOMER_ORDERS = "/api/customer/orders"


@pytest.fixture
def order(buyer, merchant, item) -> dict:
    return factories.place_order(buyer, merchant, item)


def test_other_customer_gets_404(client, order):
    other = factories.new_buyer(client, "other-buyer")
    url = f"{CUSTOMER_ORDERS}/{order['publicId']}"
    other.get(url).expect(404, code="NOT_FOUND")
    for action in ("cancel", "confirm", "decline", "received"):
        other.post(f"{url}/{action}").expect(404, code="NOT_FOUND")
    assert other.get(CUSTOMER_ORDERS).expect(200).body["totalElements"] == 0


def test_other_store_gets_404(merchant2, order):
    url = f"{MERCHANT_ORDERS}/{order['publicId']}"
    merchant2.get(url).expect(404, code="NOT_FOUND")
    merchant2.post(f"{url}/reject", {"reasonCode": "OTHER"}).expect(404, code="NOT_FOUND")
    merchant2.post(f"{url}/quote", quote_body(order)).expect(404, code="NOT_FOUND")
    merchant2.post(f"{url}/ship", {"courierCode": COURIER["code"], "trackingNumber": "ITX12345678"}
                   ).expect(404, code="NOT_FOUND")
    merchant2.post(f"{url}/delivery-failed", {"type": "COD_REFUSED"}).expect(404, code="NOT_FOUND")
    assert order["publicId"] not in [o["publicId"] for o in
                                     merchant2.get(MERCHANT_ORDERS, params={"size": 100}).expect(200).body["content"]]


def test_other_stores_assistant_gets_404(client, merchant2, order):
    helper = factories.create_assistant(merchant2, client, ["ORDER_VIEW", "ORDER_QUOTE", "ORDER_SHIP"])
    helper.get(f"{MERCHANT_ORDERS}/{order['publicId']}").expect(404, code="NOT_FOUND")
    helper.post(f"{MERCHANT_ORDERS}/{order['publicId']}/quote", quote_body(order)).expect(404, code="NOT_FOUND")


def test_unknown_order_is_404(buyer, merchant, admin):
    buyer.get(f"{CUSTOMER_ORDERS}/ORD-0000-ZZZZZZ").expect(404)
    merchant.get(f"{MERCHANT_ORDERS}/ORD-0000-ZZZZZZ").expect(404)
    admin.get("/api/admin/orders/ORD-0000-ZZZZZZ").expect(404)


def test_assistant_needs_order_view(client, merchant, order):
    quoter = factories.create_assistant(merchant, client, ["ORDER_QUOTE"])
    quoter.get(MERCHANT_ORDERS).expect(403, code="ACCESS_DENIED")
    quoter.get(f"{MERCHANT_ORDERS}/{order['publicId']}").expect(403, code="ACCESS_DENIED")
    # Quoting needs only ORDER_QUOTE
    assert quoter.post(f"{MERCHANT_ORDERS}/{order['publicId']}/quote", quote_body(order)
                       ).expect(200).body["status"] == "AWAITING_CUSTOMER_CONFIRMATION"


def test_assistant_needs_order_quote(client, merchant, order):
    viewer = factories.create_assistant(merchant, client, ["ORDER_VIEW", "ORDER_SHIP"])
    viewer.get(f"{MERCHANT_ORDERS}/{order['publicId']}").expect(200)
    viewer.post(f"{MERCHANT_ORDERS}/{order['publicId']}/quote", quote_body(order)).expect(403, code="ACCESS_DENIED")
    viewer.post(f"{MERCHANT_ORDERS}/{order['publicId']}/reject", {"reasonCode": "OTHER"}
                ).expect(403, code="ACCESS_DENIED")
    assert viewer.get(f"{MERCHANT_ORDERS}/{order['publicId']}").expect(200).body["status"] == "AWAITING_MERCHANT"


def test_assistant_needs_order_ship(client, buyer, merchant, item):
    ready = factories.order_to_ready(buyer, merchant, item)
    quoter = factories.create_assistant(merchant, client, ["ORDER_VIEW", "ORDER_QUOTE"])
    url = f"{MERCHANT_ORDERS}/{ready['publicId']}"
    quoter.post(f"{url}/ship", {"courierCode": COURIER["code"], "trackingNumber": "ITX12345678"}
                ).expect(403, code="ACCESS_DENIED")
    shipper = factories.create_assistant(merchant, client, ["ORDER_SHIP"])
    shipped = shipper.post(f"{url}/ship", {"courierCode": COURIER["code"], "trackingNumber": "ITX12345678"}
                           ).expect(200).body
    assert shipped["status"] == "SHIPPED"
    quoter.post(f"{url}/delivery-failed", {"type": "COD_REFUSED"}).expect(403, code="ACCESS_DENIED")


def test_customer_actions_are_customer_only(merchant, assistant, admin, order):
    for actor in (merchant, assistant, admin):
        actor.post(f"{CUSTOMER_ORDERS}/{order['publicId']}/cancel").expect(403, code="ACCESS_DENIED")
        actor.get(CUSTOMER_ORDERS).expect(403, code="ACCESS_DENIED")


def test_store_actions_are_store_only(buyer, admin, order):
    for actor in (buyer, admin):
        actor.get(MERCHANT_ORDERS).expect(403, code="ACCESS_DENIED")
        actor.post(f"{MERCHANT_ORDERS}/{order['publicId']}/quote", quote_body(order)).expect(403, code="ACCESS_DENIED")


def test_admin_sees_any_order(admin, buyer, merchant, assistant, order):
    detail = admin.get(f"/api/admin/orders/{order['publicId']}").expect(200).body
    assert detail["customerPublicId"] == buyer.public_id and "customer" in detail
    listed = admin.get("/api/admin/orders", params={"status": "AWAITING_MERCHANT", "size": 100}).expect(200).body
    assert listed["content"] and all(o["status"] == "AWAITING_MERCHANT" for o in listed["content"])
    for actor in (buyer, merchant, assistant):
        actor.get(f"/api/admin/orders/{order['publicId']}").expect(403, code="ACCESS_DENIED")


def test_anonymous_is_401(client, order):
    for path in (CUSTOMER_ORDERS, MERCHANT_ORDERS, "/api/admin/orders", f"{CUSTOMER_ORDERS}/{order['publicId']}"):
        client.get(path, token=None).expect(401)

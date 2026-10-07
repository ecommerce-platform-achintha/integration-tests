"""
Stage 5: one end-to-end scenario through the gateway (8080): register, approve, store, product, cart, COD order
through completion. Registration goes through the gateway (2 of the auth bucket's 10 per minute); tokens come from
logging in directly against user-service.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from tests import factories
from tests.actors import Actor, via_gateway
from tests.waits import wait_until

pytestmark = [pytest.mark.stage5, pytest.mark.gateway, pytest.mark.slow]


def register_through_gateway(gateway, client, kind: str) -> Actor:
    body = factories.customer_payload() if kind == "customer" else factories.merchant_payload()
    resp = gateway.post(f"/api/users/register/{kind}", body, token=None).expect(201)
    assert "x-ratelimit-remaining" in resp.headers  # the auth tier counted it
    actor = Actor(body["email"], body["password"], client, f"gw-{kind}", extra={"registration": body})
    actor.login()  # direct: no auth-bucket token
    return actor


def test_cod_order_end_to_end_through_the_gateway(gateway, client, admin, category, courier, bank):
    # Merchant: register, approve, set up the store, publish a product
    merchant_actor = register_through_gateway(gateway, client, "merchant")
    assert merchant_actor.status == "PENDING_APPROVAL"
    gw_admin = via_gateway(admin)
    # Newest first: pending merchants from earlier runs accumulate (the default sort is createdAt,asc)
    pending = gw_admin.get("/api/admin/merchants/pending", params={"sort": "createdAt,desc", "size": 20}
                           ).expect(200).body["content"]
    assert merchant_actor.public_id in [m["publicId"] for m in pending]
    factories.approve_merchant(gw_admin, merchant_actor)  # re-login is direct
    merchant = via_gateway(merchant_actor)
    factories.setup_merchant_store(merchant)
    product = factories.create_active_product(merchant, factories.simple_product_payload(price="750.00", stock=20))
    factories.wait_publicly_visible(gateway, product)
    listed = gateway.get("/api/public/products", token=None, params={"store": factories.store_id(merchant)}
                         ).expect(200).body["content"]
    assert [p["publicId"] for p in listed] == [product["publicId"]]

    # Customer: register, address, cart, checkout (Idempotency-Key passes through the gateway)
    customer = via_gateway(register_through_gateway(gateway, client, "customer"))
    factories.add_address(customer)
    vid = factories.variant_id(product)
    line = factories.cart_line(factories.add_to_cart(customer, vid, 2), vid)
    body = factories.checkout_body(customer, [line["cartItemPublicId"]], {factories.store_id(merchant): "COD"})
    key = f"it-gw-{product['publicId']}"
    placed = factories.checkout(customer, body, key=key).expect(201)
    assert placed.headers.get("x-ratelimit-remaining") is not None  # sensitive tier
    replay = factories.checkout(customer, body, key=key).expect(201)
    assert replay.headers.get("idempotent-replayed") == "true" and replay.body == placed.body
    [result] = placed.body["results"]
    assert result["status"] == "PLACED" and Decimal(str(result["grandTotal"])) == Decimal("1500.00")
    order = customer.get(f"/api/customer/orders/{result['orderPublicId']}").expect(200).body
    assert factories.stock_of(merchant, product) == (18, 2)

    # Quote, confirm, ship, received
    quoted = factories.quote(merchant, order)
    assert quoted["status"] == "AWAITING_CUSTOMER_CONFIRMATION"
    assert Decimal(str(quoted["amounts"]["grandTotal"])) == Decimal("1850.00")
    assert factories.confirm(customer, order)["status"] == "READY_TO_SHIP"
    shipped = factories.ship(merchant, order)
    assert shipped["status"] == "SHIPPED" and shipped["shipment"]["trackingUrl"].startswith("https://")
    done = customer.post(f"/api/customer/orders/{order['publicId']}/received").expect(200).body
    assert done["status"] == "COMPLETED"
    assert factories.stock_of(merchant, product) == (18, 0)

    # Downstream effects, read through the gateway
    wait_until(lambda: gw_admin.get(f"/api/admin/customers/{customer.public_id}/score").expect(200).body["score"] == 1,
               message="customer score through the gateway")

    def metrics():
        m = merchant.get("/api/merchant/owner/metrics").expect(200).body
        assert m["completedOrders"] == 1 and Decimal(str(m["revenue"])) == Decimal("1850.00"), m
        return True
    wait_until(metrics, message="store metrics through the gateway")

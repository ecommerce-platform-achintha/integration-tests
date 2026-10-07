"""
Stage 4 (destructive): bans, store blocks and COD suspension. Every test creates its own throwaway customer and, where
a merchant is banned, its own throwaway merchant; the shared customer/customer2/merchant/merchant2/assistant fixtures
are never banned or blocked.
"""
from __future__ import annotations

import pytest

from tests import factories
from tests.factories import checkout, checkout_body, store_id, variant_id
from tests.waits import wait_until

pytestmark = pytest.mark.stage4_destructive

MERCHANT_ORDERS = "/api/merchant/orders"
CUSTOMER_ORDERS = "/api/customer/orders"
BLOCKS = "/api/merchant/customer-blocks"


def line_id(customer, product, quantity: int = 1) -> str:
    vid = variant_id(product)
    return factories.cart_line(factories.add_to_cart(customer, vid, quantity), vid)["cartItemPublicId"]


def status_for_store(merchant, order) -> str:
    return merchant.get(f"{MERCHANT_ORDERS}/{order['publicId']}").expect(200).body["status"]


def test_banned_customer_cannot_buy_and_unconfirmed_orders_end(client, admin, merchant, item):
    victim = factories.new_buyer(client, "ban-buyer")
    waiting = factories.place_order(victim, merchant, item)                     # AWAITING_MERCHANT
    quoted = factories.place_order(victim, merchant, item)
    factories.quote(merchant, quoted)                                            # AWAITING_CUSTOMER_CONFIRMATION
    ready = factories.order_to_ready(victim, merchant, item)                     # confirmed: not cancelled
    assert factories.stock_of(merchant, item) == (47, 2)

    admin.post(f"/api/admin/customers/{victim.public_id}/ban", {"reason": "integration-tests"}).expect(200)

    # user-events -> order-service: unconfirmed orders become CANCELLED_BY_SYSTEM, stock released, no penalty
    wait_until(lambda: status_for_store(merchant, waiting) == "CANCELLED_BY_SYSTEM"
               and status_for_store(merchant, quoted) == "CANCELLED_BY_SYSTEM", message="unconfirmed orders cancelled")
    assert merchant.get(f"{MERCHANT_ORDERS}/{waiting['publicId']}").expect(200).body["reasonCode"] == "CUSTOMER_BANNED"
    assert status_for_store(merchant, ready) == "READY_TO_SHIP"
    assert factories.stock_of(merchant, item) == (49, 0)
    assert admin.get(f"/api/admin/customers/{victim.public_id}/score").expect(200).body["score"] == 0

    # A banned customer may still log in and read orders, but not buy
    victim.login()
    assert victim.status == "BANNED"
    victim.get(f"{CUSTOMER_ORDERS}/{ready['publicId']}").expect(200)
    lid = line_id(victim, item)
    checkout(victim, checkout_body(victim, [lid], {store_id(merchant): "COD"})).expect(403, code="CUSTOMER_BANNED")


def test_store_blocked_customer(client, merchant, merchant2, item, item2):
    victim = factories.new_buyer(client, "blocked-buyer")
    factories.place_order(victim, merchant, item)  # a store can only block its own customers
    merchant.post(BLOCKS, {"customerPublicId": victim.public_id, "reason": " "}).expect(400, code="VALIDATION_FAILED")
    block = merchant.post(BLOCKS, {"customerPublicId": victim.public_id, "reason": "abusive messages"}).expect(201).body
    assert block["customerPublicId"] == victim.public_id and block["blockedBy"] == merchant.public_id
    merchant.post(BLOCKS, {"customerPublicId": victim.public_id, "reason": "again"}
                  ).expect(409, code="CUSTOMER_ALREADY_BLOCKED")
    listed = merchant.get(BLOCKS).expect(200).body["content"]
    assert victim.public_id in [b["customerPublicId"] for b in listed]

    # Blocked at this store only
    ids = [line_id(victim, item), line_id(victim, item2)]
    body = checkout(victim, checkout_body(victim, ids, {store_id(merchant): "COD", store_id(merchant2): "COD"})
                    ).expect(201).body
    results = {r["storePublicId"]: r for r in body["results"]}
    assert results[store_id(merchant)]["status"] == "FAILED"
    assert results[store_id(merchant)]["code"] == "CUSTOMER_BLOCKED_BY_STORE"
    assert results[store_id(merchant2)]["status"] == "PLACED"

    merchant.delete(f"{BLOCKS}/{victim.public_id}", params={"reason": "resolved"}).expect(204)
    checkout(victim, checkout_body(victim, [ids[0]], {store_id(merchant): "COD"})).expect(201)


def test_store_cannot_block_a_stranger(client, merchant):
    stranger = factories.new_buyer(client, "stranger")
    merchant.post(BLOCKS, {"customerPublicId": stranger.public_id, "reason": "x"}).expect(404)


def test_block_needs_customer_block_permission(client, merchant, item):
    victim = factories.new_buyer(client, "block-perm-buyer")
    factories.place_order(victim, merchant, item)
    helper = factories.create_assistant(merchant, client, ["ORDER_VIEW"])
    helper.post(BLOCKS, {"customerPublicId": victim.public_id, "reason": "x"}).expect(403, code="ACCESS_DENIED")


@pytest.mark.slow
def test_third_cod_refusal_suspends_cod(client, admin, super_admin, merchant, item):
    limit = int(next(s["value"] for s in super_admin.get("/api/super-admin/settings").expect(200).body
                     if s["key"] == "score.customer.cod-refusal-limit"))
    victim = factories.new_buyer(client, "cod-refuser")
    for n in range(1, limit + 1):
        order = factories.order_to_shipped(victim, merchant, item)
        merchant.post(f"{MERCHANT_ORDERS}/{order['publicId']}/delivery-failed", {"type": "COD_REFUSED"}).expect(200)
        score = admin.get(f"/api/admin/customers/{victim.public_id}/score").expect(200).body
        assert score["codRefusals"] == n and score["codSuspended"] is (n == limit), score
    score = admin.get(f"/api/admin/customers/{victim.public_id}/score").expect(200).body
    assert score["codRefusalsTowardSuspension"] == 0 and score["codSuspendedUntil"] and score["score"] == 0

    lid = line_id(victim, item)
    body = checkout(victim, checkout_body(victim, [lid], {store_id(merchant): "COD"})).expect(422).body
    assert body["results"][0]["code"] == "COD_SUSPENDED"
    # Bank transfer is still possible
    checkout(victim, checkout_body(victim, [lid], {store_id(merchant): "BANK_TRANSFER"})).expect(201)

    nxt = victim.get(CUSTOMER_ORDERS, params={"size": 1}).expect(200).body["content"][0]
    insight = merchant.get(f"{MERCHANT_ORDERS}/{nxt['publicId']}").expect(200).body["customer"]
    assert insight == {"score": 0, "codRefusals": limit, "codSuspended": True}


def test_banned_merchant_store_stops_taking_orders(client, admin, category, courier, bank):
    shop = factories.new_shop(client, admin, "ban-shop")
    product = shop.extra["product"]
    victim = factories.new_buyer(client, "ban-shop-buyer")
    open_order = factories.place_order(victim, shop, product)
    lid = line_id(victim, product)

    admin.post(f"/api/admin/merchants/{shop.public_id}/ban", {"reason": "integration-tests"}).expect(200)
    wait_until(lambda: admin.get(f"/api/admin/stores/{store_id(shop)}").expect(200).body["acceptingOrders"] is False,
               message="store stops accepting orders (BAN_GRACE)")

    body = checkout(victim, checkout_body(victim, [lid], {store_id(shop): "COD"})).expect(422).body
    assert body["results"][0]["code"] == "STORE_NOT_ACCEPTING_ORDERS"
    # BAN_GRACE is silent for the merchant: old tokens are revoked, a new login says ACTIVE (stage 1), and it can
    # still work its open orders (contract section 3.2, D4)
    shop.login()
    assert shop.status == "ACTIVE"
    assert factories.quote(shop, open_order)["status"] == "AWAITING_CUSTOMER_CONFIRMATION"

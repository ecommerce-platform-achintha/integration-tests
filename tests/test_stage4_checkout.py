"""Stage 4, order-service: checkout (Idempotency-Key, one order per store, per-group failures, COD preconditions)."""
from __future__ import annotations

from decimal import Decimal

import pytest

from tests import data, factories
from tests.factories import cart_line, checkout, checkout_body, store_id, variant_id

pytestmark = pytest.mark.stage4

CART = "/api/customer/cart"


def dec(value) -> Decimal:
    return Decimal(str(value))


@pytest.fixture(scope="module")
def no_cod_shop(client, admin, category, courier, bank):
    """A store with cash on delivery switched off."""
    return factories.new_shop(client, admin, "no-cod-shop", cod=False)


def line_id(customer, product, quantity: int = 1) -> str:
    vid = variant_id(product)
    return cart_line(factories.add_to_cart(customer, vid, quantity), vid)["cartItemPublicId"]


def cart_ids(customer) -> set[str]:
    return {l["cartItemPublicId"] for g in customer.get(CART).expect(200).body["stores"] for l in g["lines"]}


def my_orders(customer) -> list[dict]:
    return customer.get("/api/customer/orders", params={"size": 50}).expect(200).body["content"]


# --- happy path and idempotency ---------------------------------------------------------------------------------

def test_checkout_places_order(buyer, merchant, item):
    keep = factories.cart_line(factories.add_to_cart(buyer, variant_id(item), 1), variant_id(item))  # not selected
    other = factories.create_active_product(merchant, factories.simple_product_payload(price="250.00"))
    buy = line_id(buyer, other, 2)

    resp = checkout(buyer, checkout_body(buyer, [buy], {store_id(merchant): "COD"}))
    body = resp.expect(201).body
    assert body["checkoutPublicId"].startswith("CHK-")
    [result] = body["results"]
    assert result["status"] == "PLACED" and result["orderPublicId"].startswith("ORD-")
    assert result["paymentMethod"] == "COD" and dec(result["grandTotal"]) == Decimal("500.00")
    assert result["storePublicId"] == store_id(merchant) and "code" not in result

    # Only the selected line left the cart
    assert cart_ids(buyer) == {keep["cartItemPublicId"]}

    order = buyer.get(f"/api/customer/orders/{result['orderPublicId']}").expect(200).body
    assert order["status"] == "AWAITING_MERCHANT" and order["checkoutPublicId"] == body["checkoutPublicId"]
    assert order["deadlineType"] == "MERCHANT_RESPONSE" and order["deadlineAt"]
    assert order["quoteRevision"] == 0 and "customerPublicId" not in order  # OrderViews: hidden from the customer
    [line] = order["items"]
    assert line["variantPublicId"] == variant_id(other) and line["quantity"] == line["orderedQuantity"] == 2
    assert dec(line["unitPrice"]) == Decimal("250.00") and dec(line["lineTotal"]) == Decimal("500.00")
    assert dec(order["amounts"]["itemsTotal"]) == dec(order["amounts"]["grandTotal"]) == Decimal("500.00")
    assert order["shippingAddress"]["publicId"] == buyer.extra["address"]["publicId"]
    assert order["contact"]["email"] == buyer.email
    assert "customer" not in order  # score/COD insight is for the store and admins only
    assert [o["publicId"] for o in my_orders(buyer)] == [order["publicId"]]


def test_idempotency_key_required(buyer, merchant, item):
    body = checkout_body(buyer, [line_id(buyer, item)], {store_id(merchant): "COD"})
    checkout(buyer, body, key=None).expect(400, code="IDEMPOTENCY_KEY_REQUIRED")
    for bad in ("short", "has space in it", "x" * 101, "semi;colon-key"):
        checkout(buyer, body, key=bad).expect(400, code="IDEMPOTENCY_KEY_REQUIRED")
    assert my_orders(buyer) == []


def test_replay_returns_the_stored_response(buyer, merchant, item):
    key = f"it-{data.unique()}"
    body = checkout_body(buyer, [line_id(buyer, item)], {store_id(merchant): "COD"})
    first = checkout(buyer, body, key=key).expect(201)
    assert "Idempotent-Replayed" not in first.headers
    again = checkout(buyer, body, key=key).expect(201)
    assert again.headers.get("Idempotent-Replayed") == "true"
    assert again.body == first.body
    assert len(my_orders(buyer)) == 1

    # Same key, different request
    other = checkout_body(buyer, [line_id(buyer, item)], {store_id(merchant): "COD"})
    checkout(buyer, other, key=key).expect(422, code="IDEMPOTENCY_KEY_REUSED")
    assert len(my_orders(buyer)) == 1


def test_business_refusal_is_replayed_too(buyer, merchant, item):
    key = f"it-{data.unique()}"
    body = checkout_body(buyer, ["CRT-0000-ZZZZZZ"], {store_id(merchant): "COD"})
    checkout(buyer, body, key=key).expect(404, code="CART_ITEM_NOT_FOUND")
    replay = checkout(buyer, body, key=key).expect(404, code="CART_ITEM_NOT_FOUND")
    assert replay.headers.get("Idempotent-Replayed") == "true"


def test_keys_are_per_customer(buyer, client, merchant, item):
    other = factories.new_buyer(client, "buyer-b")
    key = f"it-{data.unique()}"
    checkout(buyer, checkout_body(buyer, [line_id(buyer, item)], {store_id(merchant): "COD"}), key=key).expect(201)
    resp = checkout(other, checkout_body(other, [line_id(other, item)], {store_id(merchant): "COD"}), key=key)
    assert resp.expect(201).headers.get("Idempotent-Replayed") is None
    assert len(my_orders(other)) == 1


# --- one order per store ----------------------------------------------------------------------------------------

def test_one_order_per_store(buyer, merchant, merchant2, item, item2):
    second = factories.create_active_product(merchant, factories.simple_product_payload(price="300.00"))
    ids = [line_id(buyer, item), line_id(buyer, second, 2), line_id(buyer, item2, 3)]
    body = checkout(buyer, checkout_body(buyer, ids, {store_id(merchant): "COD", store_id(merchant2): "BANK_TRANSFER"})
                    ).expect(201).body
    results = {r["storePublicId"]: r for r in body["results"]}
    assert set(results) == {store_id(merchant), store_id(merchant2)}
    assert all(r["status"] == "PLACED" for r in results.values())
    assert dec(results[store_id(merchant)]["grandTotal"]) == Decimal("1600.00")
    assert dec(results[store_id(merchant2)]["grandTotal"]) == Decimal("1500.00")
    assert results[store_id(merchant2)]["paymentMethod"] == "BANK_TRANSFER"

    orders = {o["storePublicId"]: o for o in my_orders(buyer)}
    assert len(orders) == 2 and orders[store_id(merchant)]["itemCount"] == 2
    for r in results.values():
        detail = buyer.get(f"/api/customer/orders/{r['orderPublicId']}").expect(200).body
        assert detail["checkoutPublicId"] == body["checkoutPublicId"]
    assert cart_ids(buyer) == set()
    # Each store sees only its own order
    merchant.get(f"/api/merchant/orders/{results[store_id(merchant)]['orderPublicId']}").expect(200)
    merchant.get(f"/api/merchant/orders/{results[store_id(merchant2)]['orderPublicId']}").expect(404)


def test_payment_method_needed_for_every_store(buyer, merchant, merchant2, item, item2):
    ids = [line_id(buyer, item), line_id(buyer, item2)]
    checkout(buyer, checkout_body(buyer, ids, {store_id(merchant): "COD"})).expect(400, code="PAYMENT_METHOD_MISSING")
    assert len(cart_ids(buyer)) == 2 and my_orders(buyer) == []


def test_failed_group_does_not_block_the_others(buyer, merchant, item, no_cod_shop):
    ok, failing = line_id(buyer, item), line_id(buyer, no_cod_shop.extra["product"])
    body = checkout(buyer, checkout_body(buyer, [ok, failing], {store_id(merchant): "COD",
                                                                 store_id(no_cod_shop): "COD"})).expect(201).body
    results = {r["storePublicId"]: r for r in body["results"]}
    assert results[store_id(merchant)]["status"] == "PLACED"
    failed = results[store_id(no_cod_shop)]
    assert failed["status"] == "FAILED" and failed["code"] == "COD_NOT_AVAILABLE_FOR_STORE"
    assert "orderPublicId" not in failed and failed["message"]
    assert cart_ids(buyer) == {failing}  # the failed line stays in the cart


def test_all_groups_failing_is_422(buyer, no_cod_shop):
    lid = line_id(buyer, no_cod_shop.extra["product"])
    body = checkout(buyer, checkout_body(buyer, [lid], {store_id(no_cod_shop): "COD"})).expect(422).body
    assert "checkoutPublicId" not in body
    assert body["results"][0]["status"] == "FAILED" and body["results"][0]["code"] == "COD_NOT_AVAILABLE_FOR_STORE"
    assert cart_ids(buyer) == {lid} and my_orders(buyer) == []
    # The same store accepts a bank transfer
    resp = checkout(buyer, checkout_body(buyer, [lid], {store_id(no_cod_shop): "BANK_TRANSFER"}))
    assert resp.expect(201).body["results"][0]["status"] == "PLACED"


# --- preconditions ----------------------------------------------------------------------------------------------

def test_cod_not_allowed_for_product(client, buyer, merchant, category):
    no_cod = factories.create_active_product(merchant, factories.simple_product_payload(category["slug"],
                                                                                        codAllowed=False))
    factories.wait_publicly_visible(client, no_cod)
    lid = line_id(buyer, no_cod)
    cart = buyer.get(CART).expect(200).body
    assert cart_line(cart, variant_id(no_cod))["codAllowed"] is False
    body = checkout(buyer, checkout_body(buyer, [lid], {store_id(merchant): "COD"})).expect(422).body
    assert body["results"][0]["code"] == "COD_NOT_ALLOWED_FOR_PRODUCT"
    checkout(buyer, checkout_body(buyer, [lid], {store_id(merchant): "BANK_TRANSFER"})).expect(201)


def test_foreign_or_unknown_address(buyer, customer2, merchant, item):
    foreign = factories.add_address(customer2)
    lid = line_id(buyer, item)
    for address in (foreign["publicId"], "ADR-0000-ZZZZZZ"):
        checkout(buyer, checkout_body(buyer, [lid], {store_id(merchant): "COD"}, address)
                 ).expect(404, code="ADDRESS_NOT_FOUND")
    assert cart_ids(buyer) == {lid} and my_orders(buyer) == []


def test_unknown_or_foreign_cart_item(buyer, customer2, merchant, item):
    foreign = line_id(customer2, item)
    for lid in ("CRT-0000-ZZZZZZ", foreign):
        checkout(buyer, checkout_body(buyer, [lid], {store_id(merchant): "COD"})).expect(404, code="CART_ITEM_NOT_FOUND")
    customer2.delete(f"{CART}/items/{foreign}").expect(200)


def test_insufficient_stock_group(client, buyer, merchant, category):
    scarce = factories.create_active_product(merchant, factories.simple_product_payload(category["slug"], stock=2))
    factories.wait_publicly_visible(client, scarce)
    lid = line_id(buyer, scarce, 3)
    body = checkout(buyer, checkout_body(buyer, [lid], {store_id(merchant): "COD"})).expect(422).body
    assert body["results"][0]["code"] == "INSUFFICIENT_STOCK"
    assert factories.stock_of(merchant, scarce) == (2, 0)  # nothing held


def test_unavailable_variant_group(buyer, merchant, item):
    lid = line_id(buyer, item)
    factories.set_product_status(merchant, item["publicId"], "DRAFT")
    body = checkout(buyer, checkout_body(buyer, [lid], {store_id(merchant): "COD"})).expect(422).body
    assert body["results"][0]["code"] == "VARIANT_NOT_AVAILABLE"


@pytest.mark.parametrize("change", [
    lambda b: {**b, "cartItemIds": []},
    lambda b: {**b, "payments": []},
    lambda b: {**b, "addressPublicId": "not-an-id"},
    lambda b: {**b, "payments": [{**b["payments"][0], "method": None}]},
    lambda b: {k: v for k, v in b.items() if k != "addressPublicId"},
], ids=["no-lines", "no-payments", "bad-address-id", "no-method", "no-address"])
def test_checkout_validation(buyer, merchant, item, change):
    body = change(checkout_body(buyer, [line_id(buyer, item)], {store_id(merchant): "COD"}))
    checkout(buyer, body).expect(400, code="VALIDATION_FAILED")


def test_unknown_payment_method(buyer, merchant, item):
    body = checkout_body(buyer, [line_id(buyer, item)], {store_id(merchant): "CARD"})
    assert checkout(buyer, body).status == 400


@pytest.mark.slow
def test_open_unconfirmed_order_limit(buyer, super_admin, merchant, item):
    limit = int(next(s["value"] for s in super_admin.get("/api/super-admin/settings").expect(200).body
                     if s["key"] == "orders.max-open-unconfirmed-per-customer"))
    assert limit <= 20, "the shared item has stock 50; raise it before running with a larger limit"
    for _ in range(limit):
        factories.place_order(buyer, merchant, item)
    lid = line_id(buyer, item)
    body = checkout(buyer, checkout_body(buyer, [lid], {store_id(merchant): "COD"})).expect(422).body
    assert body["results"][0]["code"] == "OPEN_ORDER_LIMIT_REACHED"

    # A cancelled order frees one slot
    first = my_orders(buyer)[-1]
    buyer.post(f"/api/customer/orders/{first['publicId']}/cancel").expect(200)
    checkout(buyer, checkout_body(buyer, [lid], {store_id(merchant): "COD"})).expect(201)


def test_checkout_access_control(client, merchant, assistant, admin):
    client.post("/api/customer/checkout", {}, token=None, idempotency_key=True).expect(401)
    for actor in (merchant, assistant, admin):
        actor.post("/api/customer/checkout", {}, idempotency_key=True).expect(403, code="ACCESS_DENIED")

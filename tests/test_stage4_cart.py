"""Stage 4, order-service: the customer's cart (live pricing from product-service, per-line issues, several stores)."""
from __future__ import annotations

from decimal import Decimal

import pytest

from tests import factories
from tests.factories import cart_line, variant_id

pytestmark = pytest.mark.stage4

CART = "/api/customer/cart"


def dec(value) -> Decimal:
    return Decimal(str(value))


def test_empty_cart(buyer):
    assert buyer.get(CART).expect(200).body == {"stores": [], "lineCount": 0, "itemsTotal": 0.00}


def test_add_update_delete_clear(buyer, merchant, item):
    vid = variant_id(item)
    cart = factories.add_to_cart(buyer, vid, 2)
    line = cart_line(cart, vid)
    assert line["cartItemPublicId"].startswith("CRT-") and line["quantity"] == 2
    assert line["available"] is True and "issue" not in line and line["codAllowed"] is True
    assert line["itemPublicId"] == item["publicId"] and line["sku"] == item["variants"][0]["sku"]
    assert dec(line["unitPrice"]) == Decimal("1000.00") and dec(line["lineTotal"]) == Decimal("2000.00")
    group = cart["stores"][0]
    assert group["storePublicId"] == factories.store_id(merchant) and dec(group["subtotal"]) == Decimal("2000.00")
    assert cart["lineCount"] == 1 and dec(cart["itemsTotal"]) == Decimal("2000.00")

    # Adding the same variant again raises the existing line, capped at 100
    cart = factories.add_to_cart(buyer, vid, 3)
    assert cart["lineCount"] == 1 and cart_line(cart, vid)["quantity"] == 5
    cart = factories.add_to_cart(buyer, vid, 100)
    assert cart_line(cart, vid)["quantity"] == 100

    item_id = line["cartItemPublicId"]
    cart = buyer.put(f"{CART}/items/{item_id}", {"quantity": 4}).expect(200).body
    assert cart_line(cart, vid)["quantity"] == 4 and dec(cart["itemsTotal"]) == Decimal("4000.00")
    assert buyer.get(CART).expect(200).body == cart

    cart = buyer.delete(f"{CART}/items/{item_id}").expect(200).body
    assert cart["lineCount"] == 0
    buyer.delete(f"{CART}/items/{item_id}").expect(404, code="CART_ITEM_NOT_FOUND")
    buyer.put(f"{CART}/items/{item_id}", {"quantity": 1}).expect(404, code="CART_ITEM_NOT_FOUND")

    factories.add_to_cart(buyer, vid, 1)
    assert buyer.delete(CART).expect(200).body["lineCount"] == 0
    assert buyer.get(CART).expect(200).body["lineCount"] == 0
    buyer.delete(CART).expect(200)  # clearing an empty cart is fine


@pytest.mark.parametrize("body", [
    {"quantity": 1},
    {"variantPublicId": "not-an-id", "quantity": 1},
    {"variantPublicId": "VAR-0000-ZZZZZZ"},
    {"variantPublicId": "VAR-0000-ZZZZZZ", "quantity": 0},
    {"variantPublicId": "VAR-0000-ZZZZZZ", "quantity": 101},
], ids=["no-variant", "bad-id", "no-quantity", "zero", "too-many"])
def test_add_validation(buyer, body):
    buyer.post(f"{CART}/items", body).expect(400, code="VALIDATION_FAILED")


@pytest.mark.parametrize("quantity", [0, 101, None])
def test_update_validation(buyer, item, quantity):
    vid = variant_id(item)
    line = cart_line(factories.add_to_cart(buyer, vid), vid)
    buyer.put(f"{CART}/items/{line['cartItemPublicId']}", {"quantity": quantity}).expect(400, code="VALIDATION_FAILED")


def test_unknown_or_unavailable_variant_cannot_be_added(buyer, merchant, category):
    buyer.post(f"{CART}/items", {"variantPublicId": "VAR-0000-ZZZZZZ", "quantity": 1}
               ).expect(404, code="VARIANT_NOT_AVAILABLE")
    draft = factories.create_product(merchant, factories.simple_product_payload(category["slug"]))
    buyer.post(f"{CART}/items", {"variantPublicId": variant_id(draft), "quantity": 1}
               ).expect(409, code="VARIANT_NOT_AVAILABLE")


def test_cart_is_repriced_live(buyer, merchant, item):
    vid = variant_id(item)
    factories.add_to_cart(buyer, vid, 2)
    discount = merchant.post("/api/merchant/discounts", {"name": "IT cart 10", "scope": "PRODUCT", "type": "PERCENT",
                                                         "value": "10", "productPublicId": item["publicId"]}
                             ).expect(201).body
    line = cart_line(buyer.get(CART).expect(200).body, vid)
    assert dec(line["listPrice"]) == Decimal("1000.00") and dec(line["discountAmount"]) == Decimal("100.00")
    assert dec(line["unitPrice"]) == Decimal("900.00") and dec(line["lineTotal"]) == Decimal("1800.00")

    merchant.delete(f"/api/merchant/discounts/{discount['publicId']}").expect(204)
    line = cart_line(buyer.get(CART).expect(200).body, vid)
    assert dec(line["unitPrice"]) == Decimal("1000.00") and dec(line["discountAmount"]) == Decimal("0.00")


def test_line_issues(client, buyer, merchant, category):
    in_stock = factories.create_active_product(merchant, factories.simple_product_payload(category["slug"], stock=5))
    goes_draft = factories.create_active_product(merchant, factories.simple_product_payload(category["slug"]))
    for product in (in_stock, goes_draft):
        factories.wait_publicly_visible(client, product)
    factories.add_to_cart(buyer, variant_id(in_stock), 7)  # more than in stock is allowed in the cart
    factories.add_to_cart(buyer, variant_id(goes_draft), 1)
    factories.set_product_status(merchant, goes_draft["publicId"], "DRAFT")

    cart = buyer.get(CART).expect(200).body
    short = cart_line(cart, variant_id(in_stock))
    assert short["available"] is False and short["issue"] == "INSUFFICIENT_STOCK" and short["availableQuantity"] == 5
    gone = cart_line(cart, variant_id(goes_draft))
    assert gone["available"] is False and gone["issue"] == "NOT_AVAILABLE"
    # Unavailable lines do not count towards the subtotal
    assert dec(cart["stores"][0]["subtotal"]) == Decimal("0.00") and dec(cart["itemsTotal"]) == Decimal("0.00")

    buyer.put(f"{CART}/items/{short['cartItemPublicId']}", {"quantity": 5}).expect(200)
    factories.set_product_status(merchant, goes_draft["publicId"], "ACTIVE")
    cart = buyer.get(CART).expect(200).body
    assert all(l["available"] for g in cart["stores"] for l in g["lines"])
    assert dec(cart["itemsTotal"]) == Decimal("6000.00")


def test_multi_store_cart_groups_by_store(buyer, merchant, merchant2, item, item2):
    factories.add_to_cart(buyer, variant_id(item), 1)
    cart = factories.add_to_cart(buyer, variant_id(item2), 3)
    groups = {g["storePublicId"]: g for g in cart["stores"]}
    assert set(groups) == {factories.store_id(merchant), factories.store_id(merchant2)}
    assert dec(groups[factories.store_id(merchant)]["subtotal"]) == Decimal("1000.00")
    assert dec(groups[factories.store_id(merchant2)]["subtotal"]) == Decimal("1500.00")
    assert groups[factories.store_id(merchant2)]["storeName"] == merchant2.extra["store"]["name"]
    assert cart["lineCount"] == 2 and dec(cart["itemsTotal"]) == Decimal("2500.00")


def test_carts_are_private(buyer, customer2, item):
    vid = variant_id(item)
    line = cart_line(factories.add_to_cart(buyer, vid), vid)
    customer2.put(f"{CART}/items/{line['cartItemPublicId']}", {"quantity": 2}).expect(404, code="CART_ITEM_NOT_FOUND")
    customer2.delete(f"{CART}/items/{line['cartItemPublicId']}").expect(404, code="CART_ITEM_NOT_FOUND")
    assert cart_line(buyer.get(CART).expect(200).body, vid)["quantity"] == 1


def test_cart_access_control(client, merchant, assistant, admin):
    client.get(CART, token=None).expect(401)
    for actor in (merchant, assistant, admin):
        actor.get(CART).expect(403, code="ACCESS_DENIED")

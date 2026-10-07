"""Stage 4: reviews after a completed order. OrderCompleted (Kafka) makes the order eligible in store-service (store
review) and product-service (product review): one of each per order, one store reply each, and admin hide/unhide
recomputes the rating from visible reviews only."""
from __future__ import annotations

from decimal import Decimal

import pytest

from tests import factories
from tests.waits import wait_until

pytestmark = [pytest.mark.stage4, pytest.mark.slow]


def dec(value) -> Decimal:
    return Decimal(str(value))


def completed_order(customer, shop) -> dict:
    order = factories.order_to_shipped(customer, shop, shop.extra["product"])
    return customer.post(f"/api/customer/orders/{order['publicId']}/received").expect(200).body


def post_when_eligible(customer, path: str, body: dict) -> dict:
    """Retries while the OrderCompleted event has not reached the service yet (404), then returns the 201 body."""
    created: dict = {}

    def attempt():
        resp = customer.post(path, body)
        if resp.status == 404:
            return False
        created.update(resp.expect(201).body)
        return True
    wait_until(attempt, message=f"review eligibility for {body['orderPublicId']}")
    return created


@pytest.fixture(scope="module")
def reviewed_shop(client, admin, category, courier, bank):
    """A fresh store (exact rating numbers) with two completed orders from two customers."""
    shop = factories.new_shop(client, admin, "review-shop")
    orders = []
    for label in ("reviewer-a", "reviewer-b"):
        customer = factories.new_buyer(client, label)
        orders.append((customer, completed_order(customer, shop)))
    shop.extra["orders"] = orders
    return shop


# --- store reviews (store-service) ------------------------------------------------------------------------------

def test_store_review_lifecycle(client, admin, reviewed_shop):
    shop = reviewed_shop
    store = factories.store_id(shop)
    path = f"/api/customer/stores/{store}/reviews"
    (a, order_a), (b, order_b) = shop.extra["orders"]

    first = post_when_eligible(a, path, {"orderPublicId": order_a["publicId"], "rating": 5, "text": "Great store"})
    assert first["publicId"].startswith("REV-") and first["rating"] == 5 and first["text"] == "Great store"
    a.post(path, {"orderPublicId": order_a["publicId"], "rating": 1}).expect(409, code="REVIEW_ALREADY_EXISTS")
    second = post_when_eligible(b, path, {"orderPublicId": order_b["publicId"], "rating": 2, "text": "Slow"})

    def rating(expected_avg: str, expected_count: int):
        detail = client.get(f"/api/public/stores/{store}", token=None).expect(200).body
        assert dec(detail["ratingAvg"]) == Decimal(expected_avg) and detail["ratingCount"] == expected_count, detail
        return True
    rating("3.5", 2)

    # One reply per review
    reply = shop.post(f"/api/merchant/reviews/{second['publicId']}/reply", {"text": "Sorry, we are faster now"}
                      ).expect(201).body
    assert reply["reply"]["text"] == "Sorry, we are faster now" and reply["orderPublicId"] == order_b["publicId"]
    shop.post(f"/api/merchant/reviews/{second['publicId']}/reply", {"text": "again"}
              ).expect(409, code="REPLY_ALREADY_EXISTS")
    public = client.get(f"/api/public/stores/{store}/reviews", token=None).expect(200).body["content"]
    by_id = {r["publicId"]: r for r in public}
    assert by_id[second["publicId"]]["reply"]["text"] == "Sorry, we are faster now"
    assert "orderPublicId" not in by_id[first["publicId"]]  # public view hides the order

    # Admin hide recomputes from visible reviews only; unhide restores
    hide = f"/api/admin/stores/reviews/{second['publicId']}/hide"
    admin.post(hide, {"reason": " "}).expect(400, code="VALIDATION_FAILED")
    hidden = admin.post(hide, {"reason": "abusive"}).expect(200).body
    assert hidden["hidden"] is True and hidden["hiddenReason"] == "abusive"
    rating("5", 1)
    public = client.get(f"/api/public/stores/{store}/reviews", token=None).expect(200).body["content"]
    assert [r["publicId"] for r in public] == [first["publicId"]]
    managed = admin.get(f"/api/admin/stores/{store}/reviews").expect(200).body["content"]
    assert {r["publicId"]: r["hidden"] for r in managed} == {first["publicId"]: False, second["publicId"]: True}

    admin.post(f"/api/admin/stores/reviews/{second['publicId']}/unhide", {"reason": "appeal accepted"}).expect(200)
    rating("3.5", 2)


def test_store_review_needs_a_completed_order(buyer, merchant, item):
    store_path = f"/api/customer/stores/{factories.store_id(merchant)}/reviews"
    shipped = factories.order_to_shipped(buyer, merchant, item)
    buyer.post(store_path, {"orderPublicId": shipped["publicId"], "rating": 4}).expect(404)


def test_store_review_validation_and_access(client, reviewed_shop):
    store = factories.store_id(reviewed_shop)
    (a, order_a), _ = reviewed_shop.extra["orders"]
    path = f"/api/customer/stores/{store}/reviews"
    for rating in (0, 6, None):
        a.post(path, {"orderPublicId": order_a["publicId"], "rating": rating}).expect(400, code="VALIDATION_FAILED")
    # Someone else's order is not eligible
    stranger = factories.new_buyer(client, "review-stranger")
    stranger.post(path, {"orderPublicId": order_a["publicId"], "rating": 3}).expect(404)
    reviewed_shop.post(path, {"orderPublicId": order_a["publicId"], "rating": 3}).expect(403, code="ACCESS_DENIED")


# --- product reviews (product-service) --------------------------------------------------------------------------

def test_product_review_lifecycle(client, admin, reviewed_shop):
    shop = reviewed_shop
    product = shop.extra["product"]
    path = f"/api/customer/products/{product['publicId']}/reviews"
    (a, order_a), (b, order_b) = shop.extra["orders"]

    first = post_when_eligible(a, path, {"orderPublicId": order_a["publicId"], "rating": 4, "text": "Solid"})
    assert first["rating"] == 4 and first["text"] == "Solid"
    a.post(path, {"orderPublicId": order_a["publicId"], "rating": 1}).expect(409, code="REVIEW_ALREADY_EXISTS")
    second = post_when_eligible(b, path, {"orderPublicId": order_b["publicId"], "rating": 1, "text": "Broke"})

    def rating(expected_avg: str, expected_count: int):
        detail = client.get(f"/api/public/products/{product['publicId']}", token=None).expect(200).body
        assert dec(detail["ratingAvg"]) == Decimal(expected_avg) and detail["ratingCount"] == expected_count, detail
        return True
    rating("2.5", 2)

    reply_url = f"/api/merchant/products/reviews/{second['publicId']}/reply"
    replied = shop.post(reply_url, {"text": "Please contact us for a replacement"}).expect(201).body
    assert replied["reply"]["text"] == "Please contact us for a replacement"
    shop.post(reply_url, {"text": "again"}).expect(409, code="REPLY_ALREADY_EXISTS")
    merchant_list = shop.get("/api/merchant/products/reviews", params={"product": product["publicId"]}
                             ).expect(200).body["content"]
    assert {r["publicId"] for r in merchant_list} == {first["publicId"], second["publicId"]}

    admin.post(f"/api/admin/reviews/{second['publicId']}/hide", {"reason": "not about the product"}).expect(200)
    rating("4", 1)
    public = client.get(f"/api/public/products/{product['publicId']}/reviews", token=None).expect(200).body["content"]
    assert [r["publicId"] for r in public] == [first["publicId"]]
    admin_list = admin.get("/api/admin/reviews", params={"product": product["publicId"]}).expect(200).body["content"]
    assert {r["publicId"] for r in admin_list} == {first["publicId"], second["publicId"]}
    admin.post(f"/api/admin/reviews/{second['publicId']}/unhide", {"reason": "reconsidered"}).expect(200)
    rating("2.5", 2)
    public = client.get(f"/api/public/products/{product['publicId']}/reviews", token=None).expect(200).body["content"]
    assert {r["publicId"] for r in public} == {first["publicId"], second["publicId"]}


def test_product_review_needs_a_completed_order_item(client, buyer, merchant, item, category, reviewed_shop):
    shipped = factories.order_to_shipped(buyer, merchant, item)
    path = f"/api/customer/products/{item['publicId']}/reviews"
    buyer.post(path, {"orderPublicId": shipped["publicId"], "rating": 4}).expect(404, code="REVIEW_NOT_ELIGIBLE")
    # A completed order of a different product does not make this one reviewable
    (a, order_a), _ = reviewed_shop.extra["orders"]
    a.post(path, {"orderPublicId": order_a["publicId"], "rating": 4}).expect(404, code="REVIEW_NOT_ELIGIBLE")


def test_replies_need_review_reply(client, merchant):
    """The permission is checked on the route, before the review is looked up."""
    helper = factories.create_assistant(merchant, client, ["ORDER_VIEW"])
    helper.post("/api/merchant/products/reviews/REV-0000-ZZZZZZ/reply", {"text": "x"}).expect(403, code="ACCESS_DENIED")
    helper.post("/api/merchant/reviews/REV-0000-ZZZZZZ/reply", {"text": "x"}).expect(403, code="ACCESS_DENIED")
    replier = factories.create_assistant(merchant, client, ["REVIEW_REPLY"])
    replier.post("/api/merchant/products/reviews/REV-0000-ZZZZZZ/reply", {"text": "x"}).expect(404)
    replier.post("/api/merchant/reviews/REV-0000-ZZZZZZ/reply", {"text": "x"}).expect(404)

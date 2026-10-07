"""Stage 3, product-service: public search/detail (event-driven visibility), admin product views, reviews."""
from __future__ import annotations

from decimal import Decimal

import pytest

from tests import data, factories
from tests.pagination import MAX_ADMIN_SIZE, assert_paginated
from tests.waits import wait_until

pytestmark = pytest.mark.stage3

PUBLIC = "/api/public/products"
SUMMARY_FIELDS = {"publicId", "name", "category", "store", "imageUrl", "price", "listPrice", "discounted",
                  "inStock", "ratingAvg", "ratingCount"}
DETAIL_FIELDS = {"publicId", "name", "description", "category", "store", "codAllowed", "imageUrls", "options",
                 "variants", "ratingAvg", "ratingCount", "createdAt"}
VARIANT_FIELDS = {"publicId", "label", "attributes", "listPrice", "effectivePrice", "discount", "inStock",
                  "lowStock", "imageUrls"}


def search(client, **params) -> list[dict]:
    return client.get(PUBLIC, params={"size": 50, **params}, token=None).expect(200).body["content"]


def ids(results: list[dict]) -> list[str]:
    return [p["publicId"] for p in results]


def visible(client, product: dict) -> bool:
    detail = client.get(f"{PUBLIC}/{product['publicId']}", token=None)
    return detail.status == 200 and product["publicId"] in ids(search(client, q=product["name"]))


def hidden(client, product: dict) -> bool:
    detail = client.get(f"{PUBLIC}/{product['publicId']}", token=None)
    return detail.status == 404 and product["publicId"] not in ids(search(client, q=product["name"]))


@pytest.fixture
def shop(admin, client, category):
    """Throwaway merchant with a visible store, so search filters can be scoped to its products only."""
    merchant = factories.new_approved_merchant(client, admin, "search-shop")
    return merchant


# --- detail and visibility --------------------------------------------------------------------------------------

def test_public_detail_shape(client, merchant, product):
    wait_until(lambda: visible(client, product), message="product visible")
    resp = client.get(f"{PUBLIC}/{product['publicId']}", token=None).expect(200)
    body = resp.body
    assert set(body) == DETAIL_FIELDS
    assert body["store"] == {"publicId": merchant.extra["store"]["publicId"], "name": merchant.extra["store"]["name"]}
    for variant in body["variants"]:
        assert set(variant) == VARIANT_FIELDS  # no SKU, no stock counts: flags only
    assert product["variants"][0]["sku"] not in resp.text
    [summary] = [p for p in search(client, q=product["name"]) if p["publicId"] == product["publicId"]]
    assert set(summary) == SUMMARY_FIELDS
    assert Decimal(str(summary["price"])) == Decimal("1000.00") and summary["inStock"] is True


def test_draft_and_archived_products_are_not_public(client, merchant, category):
    draft = factories.create_product(merchant, factories.simple_product_payload(category["slug"]))
    client.get(f"{PUBLIC}/{draft['publicId']}", token=None).expect(404)
    active = factories.create_active_product(merchant, factories.simple_product_payload(category["slug"]))
    wait_until(lambda: visible(client, active), message="active product visible")
    merchant.delete(f"/api/merchant/products/{active['publicId']}").expect(200)
    wait_until(lambda: hidden(client, active), message="archived product hidden")


@pytest.mark.slow
def test_product_appears_only_when_its_store_is_visible(admin, client, category):
    merchant = factories.new_approved_merchant(client, admin, "visibility", store="incomplete")
    product = factories.create_active_product(merchant, factories.simple_product_payload(category["slug"]))
    assert hidden(client, product)
    factories.put_store_profile(merchant, complete=True, name=merchant.extra["store"]["name"])
    wait_until(lambda: visible(client, product), message="product visible once the store is published")


@pytest.mark.slow
def test_pending_merchant_products_appear_after_approval(admin, client, category):
    merchant = factories.register_merchant(client, "visibility-pending")
    factories.put_store_profile(merchant, complete=True)
    product = factories.create_product(merchant, factories.simple_product_payload(category["slug"]))
    admin.post(f"/api/admin/merchants/{merchant.public_id}/approve").expect(200)
    merchant.login()
    factories.set_product_status(merchant, product["publicId"], "ACTIVE")
    wait_until(lambda: visible(client, product), message="product visible after approval")


@pytest.mark.slow
def test_banned_owner_products_leave_search(admin, client, category):
    merchant = factories.new_approved_merchant(client, admin, "visibility-ban")
    product = factories.create_active_product(merchant, factories.simple_product_payload(category["slug"]))
    wait_until(lambda: visible(client, product), message="product visible")
    admin.post(f"/api/admin/merchants/{merchant.public_id}/ban", {"reason": "integration-tests"}).expect(200)
    wait_until(lambda: hidden(client, product), message="product hidden during BAN_GRACE")
    # The merchant's own view stays silent (D4)
    merchant.login()
    assert merchant.get(f"/api/merchant/products/{product['publicId']}").expect(200).body["status"] == "ACTIVE"
    admin.post(f"/api/admin/merchants/{merchant.public_id}/unban", {"reason": "integration-tests"}).expect(200)
    wait_until(lambda: visible(client, product), message="product visible after unban")


# --- search filters ---------------------------------------------------------------------------------------------

@pytest.mark.slow
def test_search_filters_and_sorts(client, admin, shop, category):
    parent = admin.post("/api/admin/categories", {"slug": data.slug("itpar"), "name": f"IT Parent {data.unique()}"}
                        ).expect(201).body
    child = admin.post("/api/admin/categories", {"slug": data.slug("itchi"), "name": f"IT Child {data.unique()}",
                                                 "parentSlug": parent["slug"]}).expect(201).body
    cheap = factories.create_active_product(shop, factories.simple_product_payload(parent["slug"], price="100.00"))
    mid = factories.create_active_product(shop, factories.simple_product_payload(child["slug"], price="500.00"))
    dear = factories.create_active_product(shop, factories.simple_product_payload(category["slug"], price="900.00",
                                                                                  stock=0))
    store = shop.extra["store"]["publicId"]
    wait_until(lambda: len(search(client, store=store)) == 3, message="3 products visible in the store")

    assert ids(search(client, store=store, sort="price")) == [cheap["publicId"], mid["publicId"], dear["publicId"]]
    assert ids(search(client, store=store, sort="price,desc")) == [dear["publicId"], mid["publicId"],
                                                                   cheap["publicId"]]
    # category includes sub-categories
    assert set(ids(search(client, category=parent["slug"]))) == {cheap["publicId"], mid["publicId"]}
    assert ids(search(client, category=child["slug"])) == [mid["publicId"]]
    # price range on the effective price
    assert ids(search(client, store=store, minPrice="200", maxPrice="900", sort="price")) == [
        mid["publicId"], dear["publicId"]]
    # inStock: a product with 0 stock disappears from inStock=true results only
    assert dear["publicId"] not in ids(search(client, store=store, inStock="true"))
    assert len(search(client, store=store, inStock="true")) == 2
    # minRating: new products have no rating
    assert search(client, store=store, minRating="1") == []
    # q is a case-insensitive substring of the name
    assert ids(search(client, q=mid["name"].lower())) == [mid["publicId"]]
    for sort in ("relevance", "newest", "rating", "price,asc"):
        client.get(PUBLIC, params={"store": store, "sort": sort}, token=None).expect(200)
    # unknown category or store: empty result, not an error
    assert search(client, category="no-such-category") == []
    assert search(client, store="STR-0000-ZZZZZZ") == []


@pytest.mark.slow
def test_stock_change_moves_product_in_and_out_of_in_stock(client, merchant, category):
    product = factories.create_active_product(merchant, factories.simple_product_payload(category["slug"], stock=2))
    variant = product["variants"][0]["publicId"]
    in_stock = lambda: product["publicId"] in ids(search(client, q=product["name"], inStock="true"))  # noqa: E731
    wait_until(in_stock, message="product in inStock results")
    factories.adjust_stock(merchant, variant, "SET", 0)
    wait_until(lambda: not in_stock(), message="product left inStock results")
    assert product["publicId"] in ids(search(client, q=product["name"]))
    detail = client.get(f"{PUBLIC}/{product['publicId']}", token=None).expect(200).body
    assert detail["variants"][0]["inStock"] is False
    factories.adjust_stock(merchant, variant, "SET", 3)
    wait_until(in_stock, message="product back in inStock results")


def test_search_validation(client):
    client.get(PUBLIC, params={"minPrice": "500", "maxPrice": "100"}, token=None).expect(400, code="VALIDATION_FAILED")
    for sort in ("price,sideways", "name", "relevance,desc", "price,asc,extra"):
        client.get(PUBLIC, params={"sort": sort}, token=None).expect(400, code="INVALID_SORT")


def test_search_is_paged(client, product):
    assert_paginated(client, PUBLIC)


@pytest.mark.parametrize("params", [{"minPrice": "abc"}, {"inStock": "maybe"}, {"page": "x"}],
                         ids=["minPrice", "inStock", "page"])
def test_badly_typed_public_query_is_400(client, params):
    """See FINDINGS S3-1 (same root cause as S1-2 / S2-1)."""
    client.get(PUBLIC, params=params, token=None).expect(400)


# --- admin product views ----------------------------------------------------------------------------------------

def test_admin_product_list_and_detail(admin, merchant, product, category):
    draft = factories.create_product(merchant, factories.simple_product_payload(category["slug"]))
    store = merchant.extra["store"]["publicId"]
    assert_paginated(admin.api, "/api/admin/products", params={"store": store}, cap=MAX_ADMIN_SIZE)
    drafts = admin.get("/api/admin/products", params={"store": store, "status": "DRAFT", "q": draft["name"]}
                       ).expect(200).body["content"]
    assert ids(drafts) == [draft["publicId"]]  # admins see drafts too
    detail = admin.get(f"/api/admin/products/{draft['publicId']}").expect(200).body
    assert detail["status"] == "DRAFT" and detail["variants"][0]["sku"] == draft["variants"][0]["sku"]
    admin.get("/api/admin/products").expect(400)                                  # store is required
    admin.get("/api/admin/products", params={"store": "STR-0000-ZZZZZZ"}).expect(404)
    admin.get("/api/admin/products/ITM-0000-ZZZZZZ").expect(404)


@pytest.mark.parametrize("actor_name", ["customer", "merchant", "assistant"])
def test_admin_product_routes_need_admin(request, actor_name, merchant, product):
    actor = request.getfixturevalue(actor_name)
    actor.get("/api/admin/products", params={"store": merchant.extra["store"]["publicId"]}
              ).expect(403, code="ACCESS_DENIED")
    actor.get(f"/api/admin/products/{product['publicId']}").expect(403, code="ACCESS_DENIED")
    actor.get("/api/admin/reviews", params={"product": product["publicId"]}).expect(403, code="ACCESS_DENIED")


# --- reviews (the endpoints that exist without a completed order; posting is covered in stage 4) -----------------

def test_review_lists_are_paged(admin, client, merchant, product):
    assert_paginated(admin.api, "/api/admin/reviews", params={"product": product["publicId"]}, cap=MAX_ADMIN_SIZE)
    assert_paginated(merchant.api, "/api/merchant/products/reviews")
    wait_until(lambda: visible(client, product), message="product visible")
    assert_paginated(client, f"{PUBLIC}/{product['publicId']}/reviews")
    admin.get("/api/admin/reviews").expect(400)  # product is required


def test_review_moderation_needs_a_reason_and_a_review(admin):
    admin.post("/api/admin/reviews/REV-0000-ZZZZZZ/hide", {"reason": ""}).expect(400, code="VALIDATION_FAILED")
    admin.post("/api/admin/reviews/REV-0000-ZZZZZZ/hide", {"reason": "spam"}).expect(404)
    admin.post("/api/admin/reviews/REV-0000-ZZZZZZ/unhide", {"reason": "ok"}).expect(404)


def test_review_reply_needs_permission(merchant, client):
    viewer = factories.create_assistant(merchant, client, ["ORDER_VIEW"])
    viewer.get("/api/merchant/products/reviews").expect(403, code="ACCESS_DENIED")
    replier = factories.create_assistant(merchant, client, ["REVIEW_REPLY"])
    replier.get("/api/merchant/products/reviews").expect(200)
    replier.post("/api/merchant/products/reviews/REV-0000-ZZZZZZ/reply", {"text": "Thanks"}).expect(404)


def test_customer_cannot_review_without_a_completed_order(customer, product):
    resp = customer.post(f"/api/customer/products/{product['publicId']}/reviews",
                         {"orderPublicId": "ORD-0000-ZZZZZZ", "rating": 5, "text": "Great"})
    resp.expect(404, code="REVIEW_NOT_ELIGIBLE")

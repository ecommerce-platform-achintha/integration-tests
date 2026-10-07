"""Stage 3, product-service: product aggregate (create forms, variants, SKUs, status, ownership, assistants)."""
from __future__ import annotations

import pytest

from tests import data, factories
from tests.pagination import assert_paginated

pytestmark = pytest.mark.stage3

PRODUCTS = "/api/merchant/products"


def stock_of(product: dict, index: int = 0) -> int:
    return product["variants"][index]["stock"]["quantityAvailable"]


# --- create -----------------------------------------------------------------------------------------------------

def test_create_simple_product(merchant, category):
    body = factories.simple_product_payload(category["slug"], price="999.99", stock=7)
    body.pop("codAllowed")
    product = merchant.post(PRODUCTS, body).expect(201).body
    assert product["status"] == "DRAFT" and product["codAllowed"] is True  # codAllowed defaults to true
    assert product["category"]["slug"] == category["slug"] and product["options"] == []
    [variant] = product["variants"]
    assert variant["sku"] == body["sku"] and variant["attributes"] == {} and variant["status"] == "ACTIVE"
    assert float(variant["listPrice"]) == 999.99 and float(variant["effectivePrice"]) == 999.99
    assert variant["discount"] is None
    assert variant["stock"]["quantityAvailable"] == 7 and variant["stock"]["reservedQuantity"] == 0


def test_create_variant_product(merchant, category):
    body = factories.variant_product_payload(category["slug"])
    product = merchant.post(PRODUCTS, body).expect(201).body
    assert product["status"] == "DRAFT"
    assert product["options"] == [{"name": "Size", "values": ["S", "M"]}, {"name": "Colour", "values": ["Red"]}]
    assert [v["attributes"] for v in product["variants"]] == [{"Size": "S", "Colour": "Red"},
                                                             {"Size": "M", "Colour": "Red"}]
    assert [stock_of(product, i) for i in range(2)] == [10, 5]
    assert all(v["publicId"].startswith("VAR-") and v["label"] for v in product["variants"])
    assert product["publicId"].startswith("ITM-")


def test_attributes_match_options_case_insensitively(merchant, category):
    body = factories.variant_product_payload(category["slug"])
    body["variants"][0]["attributes"] = {"size": "s", "COLOUR": "red"}
    product = merchant.post(PRODUCTS, body).expect(201).body
    assert product["variants"][0]["attributes"] == {"Size": "S", "Colour": "Red"}  # the options' spelling


def _variant(sku=None, **attrs):
    return {"sku": sku or data.sku(), "attributes": attrs, "listPrice": "100.00"}


@pytest.mark.parametrize("change", [
    lambda b: b.update(sku=data.sku(), listPrice="10.00"),                             # both forms
    lambda b: (b.pop("variants"), b.pop("options")),                                   # neither
    lambda b: b.pop("variants"),                                                        # options without variants
    lambda b: b.update(variants=[{**b["variants"][0], "attributes": {"Size": "S"}}]),   # missing option value
    lambda b: b.update(variants=[{**b["variants"][0], "attributes": {"Size": "XL", "Colour": "Red"}}]),
    lambda b: b.update(variants=[b["variants"][0], {**b["variants"][1], "attributes": {"Size": "S", "Colour": "Red"}}]),
    lambda b: b.update(variants=[b["variants"][0], {**b["variants"][1], "sku": b["variants"][0]["sku"].lower()}]),
    lambda b: b.update(options=[{"name": "Size", "values": ["S", "s"]}]),               # duplicate values
    lambda b: b.update(options=[{"name": "Size", "values": ["S"]}, {"name": "size", "values": ["M"]}]),
    lambda b: b.update(variants=[{**b["variants"][0], "publicId": "VAR-2610-ABCDEF"}]),  # existing id on create
], ids=["both-forms", "neither", "options-no-variants", "missing-attr", "unknown-value", "same-combination",
        "duplicate-sku", "duplicate-values", "duplicate-option", "publicid-on-create"])
def test_invalid_variants(merchant, category, change):
    body = factories.variant_product_payload(category["slug"])
    change(body)
    merchant.post(PRODUCTS, body).expect(400, code="INVALID_VARIANTS")


def test_variants_without_options_allow_one_default(merchant, category):
    body = factories.simple_product_payload(category["slug"])
    for key in ("sku", "listPrice", "initialStock"):
        body.pop(key)
    merchant.post(PRODUCTS, {**body, "variants": [_variant(), _variant()]}).expect(400, code="INVALID_VARIANTS")
    one = merchant.post(PRODUCTS, {**body, "variants": [_variant()]}).expect(201).body
    assert len(one["variants"]) == 1


@pytest.mark.parametrize("field, value", [
    ("sku", "has space"), ("sku", "-leading"), ("sku", "x" * 65), ("listPrice", "0"), ("listPrice", "1.234"),
    ("initialStock", -1), ("initialStock", 1_000_001), ("name", ""), ("categorySlug", ""),
    ("imageKeys", ["../x.png"]),
])
def test_simple_product_validation(merchant, category, field, value):
    merchant.post(PRODUCTS, factories.simple_product_payload(category["slug"], **{field: value})
                  ).expect(400, code="VALIDATION_FAILED")


def test_sku_unique_per_store_case_insensitive(merchant, merchant2, category):
    sku = data.sku()
    factories.create_product(merchant, factories.simple_product_payload(category["slug"], sku=sku))
    merchant.post(PRODUCTS, factories.simple_product_payload(category["slug"], sku=sku.lower())
                  ).expect(409, code="SKU_TAKEN")
    # Another store may use the same SKU
    factories.create_product(merchant2, factories.simple_product_payload(category["slug"], sku=sku))


# --- status -----------------------------------------------------------------------------------------------------

def test_status_lifecycle(merchant, category):
    product = factories.create_product(merchant, factories.simple_product_payload(category["slug"]))
    pid = product["publicId"]
    assert factories.set_product_status(merchant, pid, "ACTIVE")["status"] == "ACTIVE"
    assert factories.set_product_status(merchant, pid, "ACTIVE")["status"] == "ACTIVE"  # no-op
    assert factories.set_product_status(merchant, pid, "DRAFT")["status"] == "DRAFT"
    archived = merchant.delete(f"{PRODUCTS}/{pid}").expect(200).body
    assert archived["status"] == "ARCHIVED"
    merchant.post(f"{PRODUCTS}/{pid}/status", {"status": "ACTIVE"}).expect(409, code="INVALID_STATUS_TRANSITION")
    merchant.put(f"{PRODUCTS}/{pid}", factories.simple_product_payload(category["slug"])
                 ).expect(409, code="INVALID_STATUS_TRANSITION")
    assert factories.set_product_status(merchant, pid, "DRAFT")["status"] == "DRAFT"  # unarchive to DRAFT
    merchant.post(f"{PRODUCTS}/{pid}/status", {"status": "SOLD"}).expect(400)
    merchant.post(f"{PRODUCTS}/{pid}/status", {}).expect(400, code="VALIDATION_FAILED")


def test_active_needs_an_active_variant(merchant, category):
    body = factories.variant_product_payload(category["slug"])
    for v in body["variants"]:
        v["status"] = "INACTIVE"
    product = factories.create_product(merchant, body)
    merchant.post(f"{PRODUCTS}/{product['publicId']}/status", {"status": "ACTIVE"}
                  ).expect(400, code="PRODUCT_NOT_SELLABLE")

    active = factories.create_active_product(merchant, factories.variant_product_payload(category["slug"]))
    update = factories.variant_product_payload(category["slug"], name=active["name"])
    update["variants"] = [{"publicId": v["publicId"], "sku": v["sku"], "attributes": v["attributes"],
                           "listPrice": v["listPrice"], "status": "INACTIVE"} for v in active["variants"]]
    merchant.put(f"{PRODUCTS}/{active['publicId']}", update).expect(400, code="PRODUCT_NOT_SELLABLE")


def test_pending_merchant_can_draft_but_not_activate(admin, client, category):
    pending = factories.register_merchant(client, "pending-catalog")
    product = factories.create_product(pending, factories.simple_product_payload(category["slug"]))
    url = f"{PRODUCTS}/{product['publicId']}/status"
    pending.post(url, {"status": "ACTIVE"}).expect(403, code="MERCHANT_NOT_APPROVED")

    admin.post(f"/api/admin/merchants/{pending.public_id}/approve").expect(200)
    # The token's status claim is frozen at login: still refused until the merchant logs in again
    pending.post(url, {"status": "ACTIVE"}).expect(403, code="MERCHANT_NOT_APPROVED")
    pending.login()
    pending.post(url, {"status": "ACTIVE"}).expect(200)


# --- update -----------------------------------------------------------------------------------------------------

def test_put_replaces_the_aggregate(merchant, category):
    product = factories.create_product(merchant, factories.variant_product_payload(category["slug"]))
    small, medium = product["variants"]
    body = factories.variant_product_payload(category["slug"], name="Renamed shirt", codAllowed=False)
    body["options"] = [{"name": "Size", "values": ["S", "M", "L"]}, {"name": "Colour", "values": ["Red"]}]
    body["variants"] = [
        {"publicId": small["publicId"], "sku": small["sku"], "attributes": small["attributes"],
         "listPrice": "1499.00", "initialStock": 999},
        {"sku": data.sku(), "attributes": {"Size": "L", "Colour": "Red"}, "listPrice": "1700.00",
         "initialStock": 3},
    ]
    updated = merchant.put(f"{PRODUCTS}/{product['publicId']}", body).expect(200).body
    assert updated["name"] == "Renamed shirt" and updated["codAllowed"] is False
    by_id = {v["publicId"]: v for v in updated["variants"]}
    assert float(by_id[small["publicId"]]["listPrice"]) == 1499.00
    assert by_id[small["publicId"]]["stock"]["quantityAvailable"] == 10  # PUT never changes existing stock
    assert by_id[medium["publicId"]]["status"] == "INACTIVE"            # not listed: retired, not deleted
    new = [v for v in updated["variants"] if v["publicId"] not in (small["publicId"], medium["publicId"])]
    assert len(new) == 1 and new[0]["stock"]["quantityAvailable"] == 3


def test_put_simple_product_keeps_its_default_variant(merchant, category):
    product = factories.create_product(merchant, factories.simple_product_payload(category["slug"]))
    body = factories.simple_product_payload(category["slug"], sku=data.sku(), listPrice="1234.00")
    updated = merchant.put(f"{PRODUCTS}/{product['publicId']}", body).expect(200).body
    [variant] = updated["variants"]
    assert variant["publicId"] == product["variants"][0]["publicId"] and variant["sku"] == body["sku"]
    assert variant["stock"]["quantityAvailable"] == product["variants"][0]["stock"]["quantityAvailable"]


def test_put_rejects_foreign_variant(merchant, category):
    a = factories.create_product(merchant, factories.variant_product_payload(category["slug"]))
    b = factories.create_product(merchant, factories.variant_product_payload(category["slug"]))
    body = factories.variant_product_payload(category["slug"])
    body["variants"][0]["publicId"] = b["variants"][0]["publicId"]
    merchant.put(f"{PRODUCTS}/{a['publicId']}", body).expect(400, code="INVALID_VARIANTS")


# --- reads, ownership, assistants -------------------------------------------------------------------------------

def test_merchant_product_list(merchant, category):
    draft = factories.create_product(merchant, factories.simple_product_payload(category["slug"]))
    factories.create_product(merchant, factories.simple_product_payload(category["slug"]))
    assert_paginated(merchant.api, PRODUCTS)
    drafts = merchant.get(PRODUCTS, params={"status": "DRAFT", "q": draft["name"]}).expect(200).body["content"]
    assert [p["publicId"] for p in drafts] == [draft["publicId"]]
    assert drafts[0]["variantCount"] == 1 and drafts[0]["totalAvailable"] == 50
    merchant.get(PRODUCTS, params={"sort": "price"}).expect(400, code="INVALID_SORT")


def test_other_merchant_gets_404(merchant, merchant2, product):
    pid = product["publicId"]
    merchant2.get(f"{PRODUCTS}/{pid}").expect(404)
    merchant2.put(f"{PRODUCTS}/{pid}", factories.simple_product_payload()).expect(404)
    merchant2.post(f"{PRODUCTS}/{pid}/status", {"status": "DRAFT"}).expect(404)
    merchant2.delete(f"{PRODUCTS}/{pid}").expect(404)
    merchant2.patch(f"/api/merchant/variants/{product['variants'][0]['publicId']}/stock",
                    {"mode": "SET", "quantity": 0, "reason": "x"}).expect(404)
    listed = merchant2.get(PRODUCTS, params={"size": 50, "q": product["name"]}).expect(200).body["content"]
    assert listed == []
    assert merchant.get(f"{PRODUCTS}/{pid}").expect(200).body["status"] == "ACTIVE"  # untouched


def test_assistant_permissions(merchant, client, category):
    editor = factories.create_assistant(merchant, client, ["PRODUCT_EDIT"])
    stocker = factories.create_assistant(merchant, client, ["STOCK_EDIT"])
    viewer = factories.create_assistant(merchant, client, ["ORDER_VIEW"])

    product = factories.create_product(editor, factories.simple_product_payload(category["slug"]))
    factories.set_product_status(editor, product["publicId"], "ACTIVE")
    editor.put(f"{PRODUCTS}/{product['publicId']}",
               factories.simple_product_payload(category["slug"], sku=product["variants"][0]["sku"])).expect(200)
    variant = product["variants"][0]["publicId"]
    editor.patch(f"/api/merchant/variants/{variant}/stock", {"mode": "SET", "quantity": 1, "reason": "x"}
                 ).expect(403, code="ACCESS_DENIED")

    assert factories.adjust_stock(stocker, variant, "SET", 30)["quantityAvailable"] == 30
    stocker.get(PRODUCTS).expect(200)  # any of PRODUCT_EDIT / STOCK_EDIT / DISCOUNT_MANAGE can read
    stocker.post(PRODUCTS, factories.simple_product_payload(category["slug"])).expect(403, code="ACCESS_DENIED")
    stocker.post(f"{PRODUCTS}/{product['publicId']}/status", {"status": "DRAFT"}).expect(403, code="ACCESS_DENIED")

    viewer.get(PRODUCTS).expect(403, code="ACCESS_DENIED")
    viewer.get(f"{PRODUCTS}/{product['publicId']}").expect(403, code="ACCESS_DENIED")
    viewer.post(PRODUCTS, factories.simple_product_payload(category["slug"])).expect(403, code="ACCESS_DENIED")


def test_customers_and_admins_cannot_use_merchant_routes(customer, admin, product):
    for actor in (customer, admin):
        actor.get(PRODUCTS).expect(403, code="ACCESS_DENIED")
        actor.post(PRODUCTS, factories.simple_product_payload()).expect(403, code="ACCESS_DENIED")

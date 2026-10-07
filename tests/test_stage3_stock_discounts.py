"""Stage 3, product-service: merchant stock edits and discounts (effective price in merchant and public views)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from tests import factories
from tests.waits import wait_until

pytestmark = pytest.mark.stage3

DISCOUNTS = "/api/merchant/discounts"


def stock_url(variant_public_id: str) -> str:
    return f"/api/merchant/variants/{variant_public_id}/stock"


def iso(delta: timedelta) -> str:
    return (datetime.now(timezone.utc) + delta).isoformat().replace("+00:00", "Z")


def dec(value) -> Decimal:
    return Decimal(str(value))


@pytest.fixture
def shirt(merchant, category) -> dict:
    """ACTIVE variant product: S/Red 1500.00 (stock 10), M/Red 1600.00 (stock 5)."""
    return factories.create_active_product(merchant, factories.variant_product_payload(category["slug"]))


# --- stock ------------------------------------------------------------------------------------------------------

def test_stock_set_and_delta(merchant, shirt):
    variant = shirt["variants"][0]["publicId"]
    assert factories.adjust_stock(merchant, variant, "SET", 20, "restock")["quantityAvailable"] == 20
    after = factories.adjust_stock(merchant, variant, "DELTA", -5, "damaged")
    assert after["quantityAvailable"] == 15 and after["reservedQuantity"] == 0
    assert factories.adjust_stock(merchant, variant, "DELTA", 3)["quantityAvailable"] == 18
    detail = merchant.get(f"/api/merchant/products/{shirt['publicId']}").expect(200).body
    assert detail["variants"][0]["stock"]["quantityAvailable"] == 18


def test_stock_cannot_go_negative(merchant, shirt):
    variant = shirt["variants"][1]["publicId"]  # stock 5
    merchant.patch(stock_url(variant), {"mode": "DELTA", "quantity": -6, "reason": "x"}
                   ).expect(409, code="INSUFFICIENT_STOCK")
    merchant.patch(stock_url(variant), {"mode": "SET", "quantity": -1, "reason": "x"}
                   ).expect(400, code="VALIDATION_FAILED")
    assert factories.adjust_stock(merchant, variant, "DELTA", -5)["quantityAvailable"] == 0


@pytest.mark.parametrize("body", [
    {"mode": "SET", "quantity": 1},                                  # no reason
    {"mode": "SET", "quantity": 1, "reason": "  "},
    {"quantity": 1, "reason": "x"},                                  # no mode
    {"mode": "ADD", "quantity": 1, "reason": "x"},
    {"mode": "SET", "reason": "x"},                                  # no quantity
    {"mode": "DELTA", "quantity": 1_000_001, "reason": "x"},
], ids=["no-reason", "blank-reason", "no-mode", "bad-mode", "no-quantity", "too-big"])
def test_stock_validation(merchant, shirt, body):
    resp = merchant.patch(stock_url(shirt["variants"][0]["publicId"]), body)
    assert resp.status == 400, resp.describe()


def test_stock_optimistic_version(merchant, shirt):
    variant = shirt["variants"][0]["publicId"]
    version = shirt["variants"][0]["stock"]["version"]
    first = factories.adjust_stock(merchant, variant, "SET", 11, expected_version=version)
    assert first["version"] > version
    merchant.patch(stock_url(variant), {"mode": "SET", "quantity": 12, "reason": "stale", "expectedVersion": version}
                   ).expect(409, code="CONCURRENT_MODIFICATION")
    assert factories.adjust_stock(merchant, variant, "SET", 12, expected_version=first["version"])[
        "quantityAvailable"] == 12


def test_stock_unknown_variant(merchant):
    merchant.patch(stock_url("VAR-0000-ZZZZZZ"), {"mode": "SET", "quantity": 1, "reason": "x"}).expect(404)


# --- discounts --------------------------------------------------------------------------------------------------

def discount(scope: str, type_: str, value: str, product: dict, variant_index: int | None = None, **extra) -> dict:
    body = {"name": f"IT {type_} {value}", "scope": scope, "type": type_, "value": value}
    if scope == "PRODUCT":
        body["productPublicId"] = product["publicId"]
    else:
        body["variantPublicId"] = product["variants"][variant_index or 0]["publicId"]
    body.update(extra)
    return body


def prices(actor, product) -> list[Decimal]:
    detail = actor.get(f"/api/merchant/products/{product['publicId']}").expect(200).body
    return [dec(v["effectivePrice"]) for v in detail["variants"]]


def public_prices(client, product) -> list[Decimal]:
    detail = client.get(f"/api/public/products/{product['publicId']}", token=None).expect(200).body
    return [dec(v["effectivePrice"]) for v in detail["variants"]]


def test_variant_percent_discount(merchant, client, shirt):
    created = merchant.post(DISCOUNTS, discount("VARIANT", "PERCENT", "10", shirt, 0)).expect(201).body
    assert created["live"] is True and created["active"] is True and created["publicId"].startswith("DSC-")
    assert prices(merchant, shirt) == [Decimal("1350.00"), Decimal("1600.00")]
    detail = merchant.get(f"/api/merchant/products/{shirt['publicId']}").expect(200).body
    applied = detail["variants"][0]["discount"]
    assert applied["type"] == "PERCENT" and dec(applied["amount"]) == Decimal("150.00")
    # The public detail shows the same price (the store is visible, so the product is public)
    wait_until(lambda: public_prices(client, shirt) == [Decimal("1350.00"), Decimal("1600.00")],
               message="discounted price in the public detail")


def test_best_single_discount_wins_without_stacking(merchant, shirt):
    merchant.post(DISCOUNTS, discount("VARIANT", "PERCENT", "10", shirt, 0)).expect(201)      # 150 off S
    merchant.post(DISCOUNTS, discount("PRODUCT", "FIXED", "200.00", shirt)).expect(201)       # 200 off both
    assert prices(merchant, shirt) == [Decimal("1300.00"), Decimal("1400.00")]
    merchant.post(DISCOUNTS, discount("VARIANT", "PERCENT", "50", shirt, 1)).expect(201)      # 800 off M
    assert prices(merchant, shirt) == [Decimal("1300.00"), Decimal("800.00")]


def test_discount_window_and_active_flag(merchant, shirt):
    future = merchant.post(DISCOUNTS, discount("PRODUCT", "PERCENT", "20", shirt,
                                               startsAt=iso(timedelta(days=1)), endsAt=iso(timedelta(days=2)))
                           ).expect(201).body
    assert future["live"] is False
    inactive = merchant.post(DISCOUNTS, discount("PRODUCT", "PERCENT", "30", shirt, active=False)).expect(201).body
    assert inactive["live"] is False
    assert prices(merchant, shirt) == [Decimal("1500.00"), Decimal("1600.00")]

    live = merchant.post(DISCOUNTS, discount("PRODUCT", "PERCENT", "5", shirt, startsAt=iso(timedelta(hours=-1)),
                                             endsAt=iso(timedelta(days=1)))).expect(201).body
    assert live["live"] is True
    assert prices(merchant, shirt) == [Decimal("1425.00"), Decimal("1520.00")]

    merchant.delete(f"{DISCOUNTS}/{live['publicId']}").expect(204)
    merchant.get(f"{DISCOUNTS}/{live['publicId']}").expect(404)
    assert prices(merchant, shirt) == [Decimal("1500.00"), Decimal("1600.00")]


def test_discount_update(merchant, shirt):
    created = merchant.post(DISCOUNTS, discount("PRODUCT", "PERCENT", "10", shirt)).expect(201).body
    updated = merchant.put(f"{DISCOUNTS}/{created['publicId']}",
                           discount("PRODUCT", "FIXED", "100.00", shirt)).expect(200).body
    assert updated["type"] == "FIXED" and dec(updated["value"]) == Decimal("100.00")
    assert prices(merchant, shirt) == [Decimal("1400.00"), Decimal("1500.00")]


@pytest.mark.parametrize("make", [
    lambda s: discount("PRODUCT", "PERCENT", "0.5", s),
    lambda s: discount("PRODUCT", "PERCENT", "91", s),
    lambda s: discount("PRODUCT", "FIXED", "1500.00", s),                       # not below the lowest price
    lambda s: discount("VARIANT", "FIXED", "1600.00", s, 1),
    lambda s: discount("PRODUCT", "PERCENT", "10", s, startsAt=iso(timedelta(days=2)),
                       endsAt=iso(timedelta(days=1))),
    lambda s: discount("PRODUCT", "PERCENT", "10", s, endsAt=iso(timedelta(hours=-1))),
    lambda s: {**discount("PRODUCT", "PERCENT", "10", s), "variantPublicId": s["variants"][0]["publicId"]},
    lambda s: {"scope": "VARIANT", "type": "PERCENT", "value": "10"},
], ids=["percent-below-1", "percent-above-90", "fixed-too-big", "fixed-variant-too-big", "ends-before-starts",
        "ends-in-past", "product-with-variant", "variant-without-id"])
def test_invalid_discounts(merchant, shirt, make):
    merchant.post(DISCOUNTS, make(shirt)).expect(400, code="INVALID_DISCOUNT")


def test_discount_variant_must_belong_to_product(merchant, shirt, product):
    body = discount("VARIANT", "PERCENT", "10", shirt, 0, productPublicId=product["publicId"])
    merchant.post(DISCOUNTS, body).expect(400, code="INVALID_DISCOUNT")


def test_discount_list_and_ownership(merchant, merchant2, shirt):
    created = merchant.post(DISCOUNTS, discount("PRODUCT", "PERCENT", "10", shirt)).expect(201).body
    listed = merchant.get(DISCOUNTS, params={"product": shirt["publicId"]}).expect(200).body
    assert created["publicId"] in [d["publicId"] for d in listed["content"]]
    merchant2.get(f"{DISCOUNTS}/{created['publicId']}").expect(404)
    merchant2.delete(f"{DISCOUNTS}/{created['publicId']}").expect(404)
    merchant2.post(DISCOUNTS, discount("PRODUCT", "PERCENT", "10", shirt)).expect(404)
    merchant2.post(DISCOUNTS, discount("VARIANT", "PERCENT", "10", shirt, 0)).expect(404)


def test_discount_permissions(merchant, client, shirt):
    manager = factories.create_assistant(merchant, client, ["DISCOUNT_MANAGE"])
    other = factories.create_assistant(merchant, client, ["PRODUCT_EDIT"])
    manager.post(DISCOUNTS, discount("PRODUCT", "PERCENT", "10", shirt)).expect(201)
    manager.get(f"/api/merchant/products/{shirt['publicId']}").expect(200)
    other.post(DISCOUNTS, discount("PRODUCT", "PERCENT", "10", shirt)).expect(403, code="ACCESS_DENIED")
    other.get(DISCOUNTS).expect(403, code="ACCESS_DENIED")

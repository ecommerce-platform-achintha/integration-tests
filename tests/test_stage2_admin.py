"""Stage 2, store-service: admin store views, merchant metrics, credits and the fee ledger."""
from __future__ import annotations

from decimal import Decimal

import pytest

from tests import factories
from tests.pagination import MAX_ADMIN_SIZE, assert_paginated

pytestmark = pytest.mark.stage2

METRIC_FIELDS = {"score", "ordersReceived", "ordersQuoted", "ordersRejected", "acceptanceRatePercent",
                 "averageResponseMinutes", "cancelledOrders", "cancellationRatePercent", "lateShipments",
                 "lateVerifications", "complaintsUpheld", "completedOrders", "revenue", "updatedAt"}


def test_admin_store_list_and_detail(admin, merchant):
    store = merchant.extra["store"]
    assert_paginated(admin.api, "/api/admin/stores", cap=MAX_ADMIN_SIZE)
    found = admin.get("/api/admin/stores", params={"q": store["name"]}).expect(200).body["content"]
    assert store["publicId"] in [s["publicId"] for s in found]
    assert all(store["name"].lower() in s["name"].lower() for s in found)
    detail = admin.get(f"/api/admin/stores/{store['publicId']}").expect(200).body
    # Admins see contact data and the owner status (the public view does not)
    assert detail["contactEmail"] == store["contactEmail"] and detail["ownerStatus"] == "ACTIVE"
    assert detail["published"] is True and detail["acceptingOrders"] is True
    admin.get("/api/admin/stores/STR-0000-ZZZZZZ").expect(404)
    admin.get("/api/admin/stores", params={"sort": "contactEmail"}).expect(400, code="INVALID_SORT")


def test_admin_sees_hidden_stores(admin, client):
    hidden = factories.new_approved_merchant(client, admin, "admin-hidden", store="incomplete")
    detail = admin.get(f"/api/admin/stores/{hidden.extra['store']['publicId']}").expect(200).body
    assert detail["published"] is False


def test_admin_store_score_and_events(admin, merchant):
    pid = merchant.extra["store"]["publicId"]
    score = admin.get(f"/api/admin/stores/{pid}/score").expect(200).body
    assert set(score) == METRIC_FIELDS and isinstance(score["score"], int)
    assert_paginated(admin.api, f"/api/admin/stores/{pid}/score-events", cap=MAX_ADMIN_SIZE)
    admin.get("/api/admin/stores/STR-0000-ZZZZZZ/score").expect(404)
    admin.get("/api/admin/stores/STR-0000-ZZZZZZ/score-events").expect(404)


def test_at_risk_list_is_paged(admin):
    body = assert_paginated(admin.api, "/api/admin/merchants/at-risk", cap=MAX_ADMIN_SIZE)
    for row in body["content"]:
        assert row["score"] <= row["threshold"]


def test_merchant_metrics(merchant):
    metrics = merchant.get("/api/merchant/owner/metrics").expect(200).body
    assert set(metrics) == METRIC_FIELDS


def test_new_store_metrics_start_empty(admin, client):
    fresh = factories.new_approved_merchant(client, admin, "metrics-fresh")
    metrics = fresh.get("/api/merchant/owner/metrics").expect(200).body
    assert metrics["score"] == 0 and metrics["ordersReceived"] == 0 and metrics["completedOrders"] == 0
    assert metrics["acceptanceRatePercent"] is None and metrics["averageResponseMinutes"] is None


@pytest.mark.parametrize("actor_name", ["customer", "assistant", "admin"])
def test_metrics_are_owner_only(request, actor_name):
    request.getfixturevalue(actor_name).get("/api/merchant/owner/metrics").expect(403, code="ACCESS_DENIED")


@pytest.mark.parametrize("actor_name", ["customer", "merchant", "assistant"])
def test_admin_store_routes_need_admin(request, actor_name, merchant):
    actor = request.getfixturevalue(actor_name)
    actor.get("/api/admin/stores").expect(403, code="ACCESS_DENIED")
    actor.get(f"/api/admin/stores/{merchant.extra['store']['publicId']}/score").expect(403, code="ACCESS_DENIED")
    actor.get("/api/admin/merchants/at-risk").expect(403, code="ACCESS_DENIED")


# --- credits and ledger (super admin) ---------------------------------------------------------------------------

def test_credit_and_ledger(super_admin, admin, client):
    store = factories.new_approved_merchant(client, admin, "credits").extra["store"]
    base = f"/api/super-admin/merchants/{store['publicId']}"
    empty = super_admin.get(f"{base}/ledger").expect(200).body
    assert Decimal(str(empty["balance"])) == 0 and empty["enforced"] is False
    assert empty["entries"]["totalElements"] == 0

    first = super_admin.post(f"{base}/credits", {"amount": "250.50", "reason": "goodwill"}).expect(201).body
    assert first["storePublicId"] == store["publicId"] and Decimal(str(first["amount"])) == Decimal("250.50")
    assert Decimal(str(first["balance"])) == Decimal("-250.50")  # credits are negative ledger lines
    second = super_admin.post(f"{base}/credits", {"amount": "100", "reason": "outage"}).expect(201).body
    assert Decimal(str(second["balance"])) == Decimal("-350.50")

    ledger = super_admin.get(f"{base}/ledger").expect(200).body
    assert ledger["storePublicId"] == store["publicId"] and Decimal(str(ledger["balance"])) == Decimal("-350.50")
    entries = ledger["entries"]["content"]
    assert [e["type"] for e in entries] == ["CREDIT", "CREDIT"]
    assert Decimal(str(entries[0]["amount"])) == Decimal("-100")  # newest first
    assert first["publicId"] in entries[1]["description"]


def test_ledger_is_paged(super_admin, admin, client):
    store = factories.new_approved_merchant(client, admin, "ledger-paged").extra["store"]
    base = f"/api/super-admin/merchants/{store['publicId']}"
    for amount in ("1", "2"):
        super_admin.post(f"{base}/credits", {"amount": amount, "reason": "paging"}).expect(201)

    def check(page: int, size: int) -> dict:
        entries = super_admin.get(f"{base}/ledger", params={"page": page, "size": size}).expect(200).body["entries"]
        assert set(entries) == {"content", "page", "size", "totalElements", "totalPages"}
        return entries
    assert check(0, 1)["size"] == 1 and check(0, 1)["totalElements"] == 2
    assert check(1, 1)["content"] != check(0, 1)["content"]
    assert check(0, MAX_ADMIN_SIZE + 1)["size"] == MAX_ADMIN_SIZE


@pytest.mark.parametrize("body", [
    {"amount": "0", "reason": "x"}, {"amount": "-5", "reason": "x"}, {"amount": "1.234", "reason": "x"},
    {"amount": "1000000.01", "reason": "x"}, {"amount": "10", "reason": " "}, {"reason": "x"},
], ids=["zero", "negative", "3-decimals", "too-big", "blank-reason", "no-amount"])
def test_credit_validation(super_admin, merchant, body):
    pid = merchant.extra["store"]["publicId"]
    super_admin.post(f"/api/super-admin/merchants/{pid}/credits", body).expect(400, code="VALIDATION_FAILED")


def test_credit_unknown_store_and_access(super_admin, admin, merchant):
    super_admin.post("/api/super-admin/merchants/STR-0000-ZZZZZZ/credits", {"amount": "1", "reason": "x"}
                     ).expect(404)
    pid = merchant.extra["store"]["publicId"]
    for actor in (admin, merchant):
        actor.post(f"/api/super-admin/merchants/{pid}/credits", {"amount": "1", "reason": "x"}
                   ).expect(403, code="ACCESS_DENIED")
        actor.get(f"/api/super-admin/merchants/{pid}/ledger").expect(403, code="ACCESS_DENIED")

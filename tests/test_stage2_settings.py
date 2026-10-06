"""Stage 2, store-service: platform settings (restored after each test) and holidays."""
from __future__ import annotations

import random
from datetime import date

import pytest

from tests.pagination import MAX_ADMIN_SIZE, assert_paginated

pytestmark = pytest.mark.stage2

SETTINGS = "/api/super-admin/settings"
HOLIDAYS = "/api/super-admin/holidays"
SETTING_FIELDS = {"key", "value", "type", "defaultValue", "min", "max", "updatedAt", "updatedBy"}
# fees.monthly-lkr is recorded only (nothing bills it, D11), so changing it briefly has no side effects
SAFE_DECIMAL_KEY = "fees.monthly-lkr"
SAFE_INTEGER_KEY = "complaints.window-days"


def _setting(super_admin, key) -> dict:
    return next(s for s in super_admin.get(SETTINGS).expect(200).body if s["key"] == key)


@pytest.fixture
def restore_setting(super_admin, request):
    """Remembers a setting's value and puts it back after the test."""
    def remember(key: str) -> dict:
        original = _setting(super_admin, key)
        request.addfinalizer(lambda: super_admin.put(
            f"{SETTINGS}/{key}", {"value": original["value"], "reason": "integration-tests restore"}).expect(200))
        return original
    return remember


def test_settings_list(super_admin):
    settings = super_admin.get(SETTINGS).expect(200).body
    assert isinstance(settings, list) and len(settings) >= 30
    by_key = {s["key"]: s for s in settings}
    for s in settings:
        assert set(s) <= SETTING_FIELDS and {"key", "value", "type", "defaultValue"} <= set(s), s
        assert s["type"] in ("INTEGER", "DECIMAL", "BOOLEAN")
    assert by_key["orders.max-quote-revisions"]["type"] == "INTEGER"
    assert by_key["fees.enforced"]["type"] == "BOOLEAN" and by_key["fees.enforced"]["value"] == "false"
    assert by_key["timers.ship-by-hours"]["min"] == 1 and by_key["timers.ship-by-hours"]["max"] == 720


def test_update_setting_normalises_and_records_history(super_admin, restore_setting):
    original = restore_setting(SAFE_DECIMAL_KEY)
    new_value = "123.5" if original["value"] != "123.50" else "124.5"
    updated = super_admin.put(f"{SETTINGS}/{SAFE_DECIMAL_KEY}",
                              {"value": new_value, "reason": "integration-tests"}).expect(200).body
    assert updated["value"] == f"{float(new_value):.2f}"  # DECIMAL stored with 2 decimals
    assert updated["updatedBy"] == super_admin.public_id
    assert _setting(super_admin, SAFE_DECIMAL_KEY)["value"] == updated["value"]  # cache invalidated

    history = super_admin.get(f"{SETTINGS}/{SAFE_DECIMAL_KEY}/history").expect(200).body
    latest = history["content"][0]
    assert latest["newValue"] == updated["value"] and latest["oldValue"] == original["value"]
    assert latest["changedBy"] == super_admin.public_id and latest["reason"] == "integration-tests"

    # Same value again: no new history row
    total = history["totalElements"]
    super_admin.put(f"{SETTINGS}/{SAFE_DECIMAL_KEY}", {"value": updated["value"]}).expect(200)
    again = super_admin.get(f"{SETTINGS}/{SAFE_DECIMAL_KEY}/history").expect(200).body
    assert again["totalElements"] == total


def test_integer_setting_accepts_whole_decimal(super_admin, restore_setting):
    original = restore_setting(SAFE_INTEGER_KEY)
    value = 31 if original["value"] != "31" else 32
    updated = super_admin.put(f"{SETTINGS}/{SAFE_INTEGER_KEY}", {"value": f"{value}.0"}).expect(200).body
    assert updated["value"] == str(value)


@pytest.mark.parametrize("key, value", [
    (SAFE_INTEGER_KEY, "abc"),
    (SAFE_INTEGER_KEY, "1.5"),
    (SAFE_INTEGER_KEY, "0"),         # min 1
    (SAFE_INTEGER_KEY, "366"),       # max 365
    (SAFE_DECIMAL_KEY, "1.234"),     # at most 2 decimals
    (SAFE_DECIMAL_KEY, "-1"),
    ("fees.enforced", "yes"),
    ("fees.enforced", "true"),       # enforcement not available (D11)
    ("timers.ship-by-hours", "721"),
])
def test_invalid_setting_values(super_admin, key, value):
    before = _setting(super_admin, key)["value"]
    super_admin.put(f"{SETTINGS}/{key}", {"value": value}).expect(400, code="INVALID_SETTING_VALUE")
    assert _setting(super_admin, key)["value"] == before


def test_setting_request_validation(super_admin):
    super_admin.put(f"{SETTINGS}/{SAFE_INTEGER_KEY}", {"value": ""}).expect(400, code="VALIDATION_FAILED")
    super_admin.put(f"{SETTINGS}/{SAFE_INTEGER_KEY}", {"value": "1" * 51}).expect(400, code="VALIDATION_FAILED")


def test_unknown_setting_is_404(super_admin):
    super_admin.put(f"{SETTINGS}/no.such-setting", {"value": "1"}).expect(404, code="UNKNOWN_SETTING")
    super_admin.get(f"{SETTINGS}/no.such-setting/history").expect(404, code="UNKNOWN_SETTING")


def test_setting_history_is_paged(super_admin):
    assert_paginated(super_admin.api, f"{SETTINGS}/{SAFE_DECIMAL_KEY}/history", cap=MAX_ADMIN_SIZE)


@pytest.mark.parametrize("actor_name", ["admin", "customer", "merchant", "assistant"])
def test_settings_are_super_admin_only(request, actor_name):
    actor = request.getfixturevalue(actor_name)
    actor.get(SETTINGS).expect(403, code="ACCESS_DENIED")
    actor.put(f"{SETTINGS}/{SAFE_INTEGER_KEY}", {"value": "30"}).expect(403, code="ACCESS_DENIED")
    actor.get(HOLIDAYS).expect(403, code="ACCESS_DENIED")


# --- holidays ---------------------------------------------------------------------------------------------------

@pytest.fixture
def holiday_dates(super_admin, request):
    """Free far-future dates (unique per date platform-wide); the holidays are deleted afterwards."""
    created: list[str] = []
    request.addfinalizer(lambda: [super_admin.delete(f"{HOLIDAYS}/{pid}") for pid in created])

    def pick(count: int = 1) -> list[date]:
        days = []
        while len(days) < count:
            day = date(random.randint(2400, 2900), random.randint(1, 12), random.randint(1, 28))
            existing = super_admin.get(HOLIDAYS, params={"from": day.isoformat(), "to": day.isoformat()}).body
            if existing["totalElements"] == 0 and day not in days:
                days.append(day)
        return days
    pick.created = created
    return pick


def test_holiday_crud_and_range(super_admin, holiday_dates):
    d1, d2 = sorted(holiday_dates(2))
    h1 = super_admin.post(HOLIDAYS, {"date": d1.isoformat(), "name": "IT Poya", "type": "POYA"}).expect(201).body
    holiday_dates.created.append(h1["publicId"])
    h2 = super_admin.post(HOLIDAYS, {"date": d2.isoformat(), "name": "IT Day", "type": "PUBLIC"}).expect(201).body
    holiday_dates.created.append(h2["publicId"])
    assert h1["date"] == d1.isoformat() and h1["type"] == "POYA"

    ranged = super_admin.get(HOLIDAYS, params={"from": d1.isoformat(), "to": d1.isoformat()}).expect(200).body
    assert [h["publicId"] for h in ranged["content"]] == [h1["publicId"]]
    both = super_admin.get(HOLIDAYS, params={"from": d1.isoformat(), "to": d2.isoformat(), "size": 100}).body
    assert {h1["publicId"], h2["publicId"]} <= {h["publicId"] for h in both["content"]}

    # Duplicate date: on create and on update
    super_admin.post(HOLIDAYS, {"date": d1.isoformat(), "name": "Dup", "type": "PUBLIC"}
                     ).expect(409, code="HOLIDAY_DATE_TAKEN")
    super_admin.put(f"{HOLIDAYS}/{h2['publicId']}", {"date": d1.isoformat(), "name": "Dup", "type": "PUBLIC"}
                    ).expect(409, code="HOLIDAY_DATE_TAKEN")

    updated = super_admin.put(f"{HOLIDAYS}/{h2['publicId']}",
                              {"date": d2.isoformat(), "name": "IT Day renamed", "type": "POYA"}).expect(200).body
    assert updated["name"] == "IT Day renamed" and updated["type"] == "POYA"

    super_admin.delete(f"{HOLIDAYS}/{h1['publicId']}").expect(204)
    super_admin.delete(f"{HOLIDAYS}/{h1['publicId']}").expect(404)
    super_admin.put(f"{HOLIDAYS}/{h1['publicId']}", {"date": d1.isoformat(), "name": "x", "type": "POYA"}
                    ).expect(404)


@pytest.mark.parametrize("body", [
    {"name": "No date", "type": "PUBLIC"},
    {"date": "2400-02-30", "name": "Bad date", "type": "PUBLIC"},
    {"date": "2400-01-01", "name": "", "type": "PUBLIC"},
    {"date": "2400-01-01", "name": "Bad type", "type": "BANK"},
], ids=["no-date", "bad-date", "blank-name", "bad-type"])
def test_holiday_validation(super_admin, body):
    assert super_admin.post(HOLIDAYS, body).status == 400


def test_holiday_list_is_paged(super_admin, holiday_dates):
    for day in holiday_dates(2):
        h = super_admin.post(HOLIDAYS, {"date": day.isoformat(), "name": "IT page", "type": "PUBLIC"}).expect(201)
        holiday_dates.created.append(h.body["publicId"])
    assert_paginated(super_admin.api, HOLIDAYS, cap=MAX_ADMIN_SIZE)

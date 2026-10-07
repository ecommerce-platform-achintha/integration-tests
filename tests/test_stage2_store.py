"""Stage 2, store-service: store profile, visibility (event driven), public search and detail."""
from __future__ import annotations

import pytest

from tests import data, factories
from tests.pagination import assert_paginated
from tests.waits import wait_until

pytestmark = pytest.mark.stage2

STORE = "/api/merchant/store"
PUBLIC = "/api/public/stores"
PUBLIC_FIELDS = {"publicId", "name", "slug", "description", "city", "district", "logoUrl", "ratingAvg",
                 "ratingCount", "codEnabled"}


def public_ids(client, q: str) -> list[str]:
    body = client.get(PUBLIC, params={"q": q, "size": 50}, token=None).expect(200).body
    return [s["publicId"] for s in body["content"]]


def is_public(client, store: dict) -> bool:
    detail = client.get(f"{PUBLIC}/{store['publicId']}", token=None)
    return detail.status == 200 and store["publicId"] in public_ids(client, store["name"])


def is_hidden(client, store: dict) -> bool:
    detail = client.get(f"{PUBLIC}/{store['publicId']}", token=None)
    return detail.status == 404 and store["publicId"] not in public_ids(client, store["name"])


# --- profile ----------------------------------------------------------------------------------------------------

def test_first_put_creates_the_store(admin, client):
    merchant = factories.new_approved_merchant(client, admin, "store-create", store=None)
    merchant.get(STORE).expect(404)
    body = factories.store_profile_payload(complete=True)
    store = merchant.put(STORE, body).expect(200).body
    assert store["publicId"] and store["slug"]
    assert store["name"] == body["name"] and store["contactEmail"] == body["contactEmail"].lower()
    assert store["profileComplete"] is True and store["codEnabled"] is False
    assert store["ratingCount"] == 0
    assert merchant.get(STORE).expect(200).body["publicId"] == store["publicId"]

    # A second PUT replaces the profile under the same store; a new name gets a new slug
    renamed = merchant.put(STORE, {**body, "name": data.store_name()}).expect(200).body
    assert renamed["publicId"] == store["publicId"] and renamed["slug"] != store["slug"]


def test_store_name_is_unique_case_insensitive(admin, client, merchant):
    other = factories.new_approved_merchant(client, admin, "store-name", store=None)
    taken = merchant.extra["store"]["name"]
    for name in (taken, taken.upper(), f"  {taken.lower()}  "):
        other.put(STORE, factories.store_profile_payload(name=name)).expect(409, code="STORE_NAME_TAKEN")
    # The owner may keep its own name
    merchant.put(STORE, {k: merchant.extra["store"][k] for k in
                         ("name", "description", "contactPhone", "contactEmail", "addressLine", "city",
                          "district")}).expect(200)


@pytest.mark.parametrize("field, value", [
    ("name", "ab"), ("name", "  ab  "), ("name", "x" * 101), ("contactPhone", "123"),
    ("contactEmail", "not-mail"), ("logoKey", "../logo.png"), ("district", "x" * 51),
])
def test_store_profile_validation(merchant, field, value):
    resp = merchant.put(STORE, factories.store_profile_payload(**{field: value}))
    resp.expect(400, code="VALIDATION_FAILED")


@pytest.mark.slow
def test_complete_profile_and_active_owner_make_the_store_visible(admin, client):
    merchant = factories.new_approved_merchant(client, admin, "store-visible", store="incomplete")
    store = merchant.extra["store"]
    assert store["profileComplete"] is False and store["published"] is False
    assert is_hidden(client, store)

    store = factories.put_store_profile(merchant, complete=True, name=store["name"])
    wait_until(lambda: is_public(client, store), message="complete store visible in public search")
    detail = client.get(f"{PUBLIC}/{store['publicId']}", token=None).expect(200).body
    assert set(detail) == PUBLIC_FIELDS, f"public store exposes {set(detail) - PUBLIC_FIELDS}"

    # Removing a required field hides it again
    factories.put_store_profile(merchant, complete=True, name=store["name"], district=None)
    wait_until(lambda: is_hidden(client, store), message="incomplete store hidden again")


@pytest.mark.slow
def test_pending_owner_store_appears_after_approval(admin, client):
    """A pending merchant may build the store, but it is published only once the UserStatusChanged event arrives."""
    merchant = factories.register_merchant(client, "store-pending")
    store = factories.put_store_profile(merchant, complete=True)
    assert store["profileComplete"] is True and store["published"] is False
    assert is_hidden(client, store)
    admin.post(f"/api/admin/merchants/{merchant.public_id}/approve").expect(200)
    wait_until(lambda: is_public(client, store), message="store visible after owner approval")
    assert merchant.get(STORE).expect(200).body["published"] is True


@pytest.mark.slow
def test_banned_owner_store_stops_accepting_orders(admin, client):
    """
    Contract section 3.2: during BAN_GRACE the store stops accepting orders and its listings leave search; only the
    merchant's own view stays silent. Unban restores it.
    """
    merchant = factories.new_approved_merchant(client, admin, "store-ban")
    store = merchant.extra["store"]
    wait_until(lambda: is_public(client, store), message="store visible")
    admin.post(f"/api/admin/merchants/{merchant.public_id}/ban", {"reason": "integration-tests"}).expect(200)
    wait_until(lambda: is_hidden(client, store), message="store hidden after owner ban")
    detail = admin.get(f"/api/admin/stores/{store['publicId']}").expect(200).body
    assert detail["ownerStatus"] == "BAN_GRACE" and detail["published"] is True
    assert detail["acceptingOrders"] is False
    admin.post(f"/api/admin/merchants/{merchant.public_id}/unban", {"reason": "integration-tests"}).expect(200)
    wait_until(lambda: is_public(client, store), message="store visible after unban")


def test_cod_toggle(admin, client):
    merchant = factories.new_approved_merchant(client, admin, "cod")
    assert factories.set_cod(merchant, True)["codEnabled"] is True
    assert factories.set_cod(merchant, False)["codEnabled"] is False
    merchant.put(f"{STORE}/cod", {}).expect(400, code="VALIDATION_FAILED")
    no_store = factories.new_approved_merchant(client, admin, "cod-no-store", store=None)
    no_store.put(f"{STORE}/cod", {"enabled": True}).expect(409, code="STORE_PROFILE_REQUIRED")


def test_store_routes_are_owner_only(assistant, customer, admin):
    for actor in (assistant, customer, admin):
        actor.get(STORE).expect(403, code="ACCESS_DENIED")
        actor.put(STORE, factories.store_profile_payload()).expect(403, code="ACCESS_DENIED")
        actor.put(f"{STORE}/cod", {"enabled": True}).expect(403, code="ACCESS_DENIED")
    for path in ("/api/merchant/owner/bank-accounts", "/api/merchant/owner/couriers",
                 "/api/merchant/owner/shipping-templates", "/api/merchant/owner/metrics"):
        assistant.get(path).expect(403, code="ACCESS_DENIED")


# --- public search ----------------------------------------------------------------------------------------------

@pytest.mark.slow
def test_public_search(client, merchant, merchant2):
    for m in (merchant, merchant2):
        wait_until(lambda m=m: is_public(client, m.extra["store"]), message=f"{m.label} store visible")
    name = merchant.extra["store"]["name"]
    # q matches the name case-insensitively, as a substring; LIKE wildcards are literal
    found = client.get(PUBLIC, params={"q": name.upper(), "size": 50}, token=None).expect(200).body["content"]
    assert merchant.extra["store"]["publicId"] in [s["publicId"] for s in found]
    assert all(name.lower() in s["name"].lower() for s in found)
    both = public_ids(client, f"IT Store {data.RUN_ID}")
    assert {merchant.extra["store"]["publicId"], merchant2.extra["store"]["publicId"]} <= set(both)
    wildcard = client.get(PUBLIC, params={"q": "%", "size": 50}, token=None).expect(200).body["content"]
    assert all("%" in s["name"] for s in wildcard), "'%' must match literally, not as a LIKE wildcard"
    assert public_ids(client, f"no-such-store-{data.RUN_ID}") == []

    for sort in ("name,asc", "name,desc", "rating,desc", "newest", "newest,desc"):
        client.get(PUBLIC, params={"sort": sort}, token=None).expect(200)
    client.get(PUBLIC, params={"sort": "ownerUserId"}, token=None).expect(400, code="INVALID_SORT")
    ordered = client.get(PUBLIC, params={"q": f"IT Store {data.RUN_ID}", "sort": "name,desc", "size": 50},
                         token=None).expect(200).body["content"]
    names = [s["name"] for s in ordered]  # all "IT Store <run> <n>": only the numeric suffix differs
    assert names == sorted(names, reverse=True)
    assert_paginated(client, PUBLIC)


def test_public_detail_exposes_no_contact_or_bank_data(client, merchant):
    store = merchant.extra["store"]
    wait_until(lambda: is_public(client, store), message="store visible")
    resp = client.get(f"{PUBLIC}/{store['publicId']}", token=None).expect(200)
    assert set(resp.body) == PUBLIC_FIELDS
    for secret in (store["contactPhone"], store["contactEmail"], store["addressLine"],
                   merchant.extra["bank_accounts"][0]["_accountNumber"]):
        assert secret not in resp.text
    client.get(f"{PUBLIC}/STR-0000-ZZZZZZ", token=None).expect(404)


@pytest.mark.parametrize("actor_name, path, params", [
    (None, PUBLIC, {"size": "x"}),
    (None, PUBLIC, {"page": "abc"}),
    ("admin", "/api/admin/banks", {"active": "maybe"}),
    ("super_admin", "/api/super-admin/holidays", {"from": "not-a-date"}),
], ids=["public-size", "public-page", "admin-boolean", "holiday-date"])
def test_badly_typed_query_parameter_is_400(request, client, actor_name, path, params):
    """A query parameter of the wrong type is a client error (see FINDINGS S2-1)."""
    api = request.getfixturevalue(actor_name).api if actor_name else client
    api.get(path, params=params, **({} if actor_name else {"token": None})).expect(400)

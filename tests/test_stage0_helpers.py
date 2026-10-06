"""Stage 0: the factories and fixtures produce what later stages rely on."""
from __future__ import annotations

import time

import pytest

from tests.actors import REFRESH_MARGIN_SECONDS
from tests.pagination import MAX_ADMIN_SIZE, assert_paginated
from tests.waits import wait_until

pytestmark = pytest.mark.stage0


def test_customer_fixture(customer):
    assert customer.role == "ROLE_CUSTOMER" and customer.status == "ACTIVE"
    assert customer.public_id and customer.user_id and customer.store_id is None


def test_admin_fixture_has_changed_its_password(admin):
    assert admin.role == "ROLE_ADMIN" and not admin.must_change_password
    admin.get("/api/users/me").expect(200)


def test_actor_relogs_in_when_token_is_about_to_expire(customer2):
    old = customer2.access_token
    customer2.claims["exp"] = time.time() + REFRESH_MARGIN_SECONDS - 1  # pretend it expires soon
    assert customer2.access_token != old
    customer2.get("/api/users/me").expect(200)


def test_pagination_helper_on_admin_user_list(admin, customer, customer2):
    page = assert_paginated(admin.api, "/api/admin/users", params={"role": "ROLE_CUSTOMER"}, cap=MAX_ADMIN_SIZE)
    assert page["totalElements"] >= 2


@pytest.mark.slow
def test_merchant_fixture_is_ready(merchant, courier, bank):
    assert merchant.role == "ROLE_MERCHANT" and merchant.status == "ACTIVE" and merchant.store_id
    store = merchant.extra["store"]
    assert store["profileComplete"] and store["codEnabled"]
    # published needs the owner's ACTIVE status, which store-service may learn through a user event
    wait_until(lambda: merchant.get("/api/merchant/store").expect(200).body["published"],
               message="merchant store published")
    assert [c["code"] for c in merchant.extra["couriers"]] == [courier["code"]]
    assert merchant.extra["bank_accounts"][0]["bankCode"] == bank["code"]
    assert merchant.extra["shipping_templates"][0]["courierCode"] == courier["code"]


def test_assistant_fixture(assistant, merchant):
    assert assistant.role == "ROLE_ASSISTANT" and not assistant.must_change_password
    assert assistant.store_id == merchant.store_id
    assert "PRODUCT_EDIT" in assistant.permissions


def test_category_fixture(category, client):
    assert category["active"] and category["slug"] == "it-general"

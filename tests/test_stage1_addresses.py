"""Stage 1, user-service: the caller's own addresses (/api/users/me/addresses)."""
from __future__ import annotations

import pytest

from tests import data, factories
from tests.pagination import assert_paginated

pytestmark = pytest.mark.stage1

MAX_ADDRESSES = 20  # app.max-addresses-per-user
BASE = "/api/users/me/addresses"


def address_payload(**overrides) -> dict:
    body = {"recipientName": "Ita Receiver", "phone": data.phone(), "line1": "12 Galle Road", "line2": "Apt 3",
            "city": "Colombo", "district": "Colombo", "postalCode": "00300"}
    body.update(overrides)
    return body


def test_address_crud(customer):
    created = customer.post(BASE, address_payload()).expect(201).body
    assert created["publicId"] and created["country"] == "Sri Lanka"  # default country
    pid = created["publicId"]
    assert customer.get(f"{BASE}/{pid}").expect(200).body == created

    updated = customer.put(f"{BASE}/{pid}", address_payload(city="Kandy", district="Kandy", line2=None,
                                                            country="Sri Lanka")).expect(200).body
    assert updated["city"] == "Kandy" and updated["line2"] is None

    customer.delete(f"{BASE}/{pid}").expect(204)
    customer.get(f"{BASE}/{pid}").expect(404)
    customer.delete(f"{BASE}/{pid}").expect(404)


def test_address_list_is_paged(customer):
    for _ in range(2):
        customer.post(BASE, address_payload()).expect(201)
    assert_paginated(customer.api, BASE)


def test_other_users_address_is_404(customer, customer2, merchant):
    pid = customer.post(BASE, address_payload()).expect(201).body["publicId"]
    for other in (customer2, merchant):
        other.get(f"{BASE}/{pid}").expect(404)
        other.put(f"{BASE}/{pid}", address_payload()).expect(404)
        other.delete(f"{BASE}/{pid}").expect(404)
        listed = other.get(BASE, params={"size": 50}).expect(200).body
        assert pid not in [a["publicId"] for a in listed["content"]]
    customer.get(f"{BASE}/{pid}").expect(200)  # untouched


@pytest.mark.parametrize("field, value", [
    ("postalCode", "00300!"), ("phone", "0771234567"), ("line1", ""), ("district", ""),
    ("recipientName", "x" * 151),
])
def test_address_validation(customer, field, value):
    resp = customer.post(BASE, address_payload(**{field: value})).expect(400, code="VALIDATION_FAILED")
    assert field in {e["field"] for e in resp.body["fieldErrors"]}


def test_address_limit(client):
    user = factories.register_customer(client, "address-limit")
    for _ in range(MAX_ADDRESSES):
        user.post(BASE, address_payload()).expect(201)
    user.post(BASE, address_payload()).expect(409, code="ADDRESS_LIMIT_REACHED")

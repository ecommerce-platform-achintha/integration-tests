"""
Stage 5: the only test that empties a gateway rate-limit bucket: the auth tier (login, refresh, register; keyed by
client IP). It is marked `exhausts_rate_limit`, so a conftest hook runs it after every other selected test, and it
waits for the bucket to refill before the module ends, so a following run starts with a full bucket.

The requests are logins with an empty body: the gateway counts them, and user-service rejects them in validation
(400) before its own failed-login throttle (FINDINGS N0-1) sees them.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest
import yaml

from tests.actors import via_gateway
from tests.config import SETTINGS

pytestmark = [pytest.mark.stage5, pytest.mark.gateway, pytest.mark.exhausts_rate_limit, pytest.mark.slow]

SECURITY_HEADERS = {"x-content-type-options": "nosniff", "referrer-policy": "no-referrer", "x-frame-options": "DENY"}


def auth_tier() -> dict:
    config = yaml.safe_load(Path(SETTINGS.gateway_config_path).read_text())
    return config["api-gateway"]["rate-limit"]["tiers"]["auth"]


@pytest.fixture
def refill_afterwards():
    """Holds the Retry-After of the 429; afterwards waits until the whole bucket has refilled."""
    state: dict = {}
    yield state
    if "retry_after" in state:
        # Greedy refill: one token every refill-period/capacity seconds, which is what Retry-After says when the
        # bucket is empty. A full bucket therefore takes capacity * Retry-After seconds.
        time.sleep(state["retry_after"] * state["capacity"])


def test_auth_tier_limit_and_refill(gateway, customer, refill_afterwards):
    tier = auth_tier()
    capacity = tier["capacity"]
    refill_afterwards["capacity"] = capacity

    # Empty the bucket (earlier tests in this run may already have used some of it)
    last = None
    for _ in range(capacity + 1):
        last = gateway.post("/api/auth/login", {}, token=None)
        if last.status == 429:
            break
        assert last.status == 400, last.describe()  # counted by the gateway, refused by validation
        assert int(last.headers["x-ratelimit-remaining"]) >= 0
    assert last is not None and last.status == 429, last.describe()

    assert last.body["code"] == "RATE_LIMITED" and last.body["status"] == 429
    assert set(last.body) == {"timestamp", "status", "error", "code", "message", "path"}
    retry_after = int(last.headers["retry-after"])
    refill_afterwards["retry_after"] = retry_after
    assert 1 <= retry_after <= 60 // capacity + 1
    assert last.headers["x-ratelimit-remaining"] == "0"
    for name, value in SECURITY_HEADERS.items():
        assert last.headers.get(name) == value

    # The whole tier shares one bucket per IP: refresh and register are refused too
    gateway.post("/api/auth/refresh", {"refreshToken": "x"}, token=None).expect(429, code="RATE_LIMITED")
    gateway.post("/api/users/register/customer", {}, token=None).expect(429, code="RATE_LIMITED")

    # Other tiers and direct calls are unaffected
    gateway.get("/api/public/products", token=None).expect(200)
    via_gateway(customer).get("/api/users/me").expect(200)
    customer.login()  # direct to user-service: not behind the gateway's limiter

    # After Retry-After one token is back (and spent again by this request)
    time.sleep(retry_after)
    after = gateway.post("/api/auth/login", {}, token=None)
    assert after.status == 400, after.describe()
    assert after.headers["x-ratelimit-remaining"] == "0"

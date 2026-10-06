"""
Stage 1 (destructive): merchant ban on dedicated throwaway merchants, and a test that briefly changes a global
platform setting. Never touches the shared fixtures. Run with `pytest -m stage1_destructive`.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tests import factories
from tests.waits import wait_until

pytestmark = pytest.mark.stage1_destructive

BAN_GRACE_DAYS = 14  # timers.ban-grace-days (user-service config)


@pytest.fixture
def throwaway_merchant(admin, client):
    merchant = factories.register_merchant(client, "ban-merchant")
    factories.approve_merchant(admin, merchant)
    return merchant


def test_merchant_ban_is_silent_during_grace(admin, client, throwaway_merchant):
    merchant = throwaway_merchant
    assistant = factories.create_assistant(merchant, client, ["ORDER_VIEW"])
    old_access, old_refresh = merchant.tokens["accessToken"], merchant.refresh_token
    url = f"/api/admin/merchants/{merchant.public_id}"

    admin.post(f"{url}/ban", {"reason": ""}).expect(400, code="VALIDATION_FAILED")
    banned = admin.post(f"{url}/ban", {"reason": "counterfeit goods"}).expect(200).body
    assert banned["status"] == "BAN_GRACE" and banned["statusReason"] == "counterfeit goods"
    effective = datetime.fromisoformat(banned["banEffectiveAt"].replace("Z", "+00:00"))
    expected = datetime.now(timezone.utc) + timedelta(days=BAN_GRACE_DAYS)
    assert abs(effective - expected) < timedelta(minutes=5)
    assert banned.get("banAnnouncedAt") is None
    admin.post(f"{url}/ban", {"reason": "again"}).expect(409, code="INVALID_STATUS_TRANSITION")

    # Silent: access tokens re-issued through refresh, merchant still sees ACTIVE and no reason
    client.get("/api/users/me", token=old_access).expect(401, code="TOKEN_REVOKED")
    refreshed = client.post("/api/auth/refresh", {"refreshToken": old_refresh}, token=None).expect(200).body
    assert refreshed["status"] == "ACTIVE" and "statusReason" not in refreshed
    login = merchant.login().body
    assert login["status"] == "ACTIVE" and "statusReason" not in login
    assert merchant.claims["status"] == "ACTIVE"
    me = merchant.get("/api/users/me").expect(200).body
    assert me["status"] == "ACTIVE" and me.get("statusReason") is None
    # Assistants of a merchant in grace can still sign in (only BANNED blocks them)
    assistant.login()

    unbanned = admin.post(f"{url}/unban", {"reason": "appeal accepted"}).expect(200).body
    assert unbanned["status"] == "ACTIVE" and unbanned.get("banEffectiveAt") is None
    admin.post(f"{url}/unban", {"reason": "again"}).expect(409, code="INVALID_STATUS_TRANSITION")


def test_only_active_merchants_can_be_banned(admin, client):
    pending = factories.register_merchant(client, "ban-pending")
    admin.post(f"/api/admin/merchants/{pending.public_id}/ban", {"reason": "x"}
               ).expect(409, code="INVALID_STATUS_TRANSITION")


@pytest.mark.skip_time_based(reason="BAN_GRACE becomes BANNED after timers.ban-grace-days (14 d) via "
                                    "BanEnforcementJob; then merchant login shows BANNED + reason and "
                                    "assistant login is refused with MERCHANT_BANNED")
def test_merchant_ban_enforced_after_grace():
    pass


@pytest.mark.slow
def test_application_limit_follows_platform_setting(super_admin, client, request):
    """
    store-service owns the platform setting merchant.max-application-attempts (SettingKey, design section 7). The
    limit a merchant sees should follow it.
    """
    key = "merchant.max-application-attempts"
    settings = super_admin.get("/api/super-admin/settings").expect(200).body
    original = next(s for s in settings if s["key"] == key)["value"]
    new_value = "2" if original != "2" else "4"

    def restore():
        super_admin.put(f"/api/super-admin/settings/{key}",
                        {"value": original, "reason": "integration-tests restore"}).expect(200)

    request.addfinalizer(restore)
    super_admin.put(f"/api/super-admin/settings/{key}",
                    {"value": new_value, "reason": "integration-tests"}).expect(200)

    merchant = factories.register_merchant(client, "attempts-setting")
    wait_until(lambda: merchant.get("/api/merchant/application").expect(200).body["maxAttempts"] == int(new_value),
               timeout=15, message=f"maxAttempts == {new_value} after changing {key}")

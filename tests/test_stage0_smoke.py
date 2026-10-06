"""Stage 0: the stack is up and the suite's own plumbing (routing, super admin login) works."""
from __future__ import annotations

import httpx
import pytest
import yaml

from tests.routing import ROUTES, service_for, services_for

pytestmark = pytest.mark.stage0

SERVICES = ["user", "product", "order", "store", "gateway"]
EUREKA_APPS = {"USER-SERVICE", "PRODUCT-SERVICE", "ORDER-SERVICE", "STORE-SERVICE", "API-GATEWAY"}


@pytest.mark.parametrize("service", SERVICES)
def test_service_health_is_up(settings, service):
    resp = httpx.get(f"{settings.service_url(service)}/actuator/health", timeout=settings.http_timeout)
    assert resp.status_code == 200, f"{service}: {resp.status_code} {resp.text}"
    assert resp.json()["status"] == "UP"


def test_jwks_is_reachable(client):
    body = client.get("/.well-known/jwks.json", token=None).expect(200).body
    assert body["keys"], "JWKS has no keys"
    for key in body["keys"]:
        assert key["kty"] == "RSA" and key["alg"] == "RS256" and key["kid"], key
        assert "d" not in key, "JWKS must not publish the private exponent"


def test_eureka_lists_the_five_apps(settings):
    resp = httpx.get(f"{settings.eureka_url}/eureka/apps", headers={"Accept": "application/json"},
                     timeout=settings.http_timeout)
    assert resp.status_code == 200, resp.text
    apps = {a["name"]: a["instance"] for a in resp.json()["applications"]["application"]}
    assert EUREKA_APPS <= set(apps), f"missing from Eureka: {EUREKA_APPS - set(apps)}"
    for name in EUREKA_APPS:
        instances = apps[name] if isinstance(apps[name], list) else [apps[name]]
        assert any(i["status"] == "UP" for i in instances), f"{name}: no UP instance"


def test_super_admin_login_works(super_admin):
    assert super_admin.role == "ROLE_SUPER_ADMIN"
    assert not super_admin.must_change_password
    for field in ("accessToken", "tokenType", "expiresIn", "expiresAt", "refreshToken", "refreshTokenExpiresAt",
                  "publicId", "role", "status", "mustChangePassword"):
        assert field in super_admin.tokens, f"TokenResponse lacks {field}"
    me = super_admin.get("/api/users/me").expect(200).body
    assert me["role"] == "ROLE_SUPER_ADMIN" and me["publicId"] == super_admin.public_id


def test_routing_table_matches_gateway_config(settings):
    """tests/routing.py must list exactly the gateway's route patterns (api-gateway application.yml)."""
    path = settings.gateway_config_path
    if not path.exists():
        pytest.skip(f"gateway config not found at {path}")
    config = yaml.safe_load(path.read_text())
    routes = config["spring"]["cloud"]["gateway"]["server"]["webflux"]["routes"]
    actual = {r["id"].removesuffix("-service"): r["predicates"][0]["args"]["patterns"] for r in routes}
    assert actual == ROUTES


@pytest.mark.parametrize("path, service", [
    ("/api/auth/login", "user"),
    ("/api/admin/merchants/MER-0001-ABCDEF/approve", "user"),
    ("/api/admin/merchants/pending", "user"),
    ("/api/admin/merchants/at-risk", "store"),
    ("/api/admin/customers/CUS-0001-ABCDEF/ban", "user"),
    ("/api/admin/customers/CUS-0001-ABCDEF/score", "order"),
    ("/api/merchant/owner/assistants", "user"),
    ("/api/merchant/owner/bank-accounts/x", "store"),
    ("/api/merchant/products/reviews/x", "product"),
    ("/api/merchant/reviews", "store"),
    ("/api/customer/checkout", "order"),
    ("/api/public/categories", "product"),
    ("/.well-known/jwks.json", "user"),
])
def test_routing_examples(path, service):
    assert services_for(path) == [service]
    assert service_for(path) == service


def test_unrouted_path_is_refused():
    with pytest.raises(ValueError):
        service_for("/internal/users/x")

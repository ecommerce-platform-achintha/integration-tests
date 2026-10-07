"""
Stage 5, api-gateway (8080): routing to the owning service, the token check and coarse role rules, /internal/**
and path guards, body limits, security headers, CORS, header spoofing and the rate-limit headers.

Tokens come from logging in directly against user-service (session fixtures), so these tests never spend the
gateway's auth bucket (10/min per IP for login, refresh and register). Only test_stage5_zz_rate_limit.py
empties a bucket, and it always runs last.
"""
from __future__ import annotations

import base64
import http.client
import json
from urllib.parse import urlsplit

import pytest

from tests.actors import via_gateway
from tests.config import SETTINGS

pytestmark = [pytest.mark.stage5, pytest.mark.gateway]

SECURITY_HEADERS = {"x-content-type-options": "nosniff", "referrer-policy": "no-referrer", "x-frame-options": "DENY"}
ERROR_KEYS = {"timestamp", "status", "error", "code", "message", "path"}
ALLOWED_ORIGIN = "http://localhost:5173"


def raw_request(method: str, path: str, headers: dict | None = None, body: bytes | None = None,
                chunked: bool = False) -> tuple[int, dict, str]:
    """http.client sends the path exactly as given (httpx would normalise ./.. segments first)."""
    url = urlsplit(SETTINGS.gateway_url)
    conn = http.client.HTTPConnection(url.hostname, url.port or 80, timeout=SETTINGS.http_timeout)
    try:
        if chunked:
            conn.putrequest(method, path)
            for k, v in (headers or {}).items():
                conn.putheader(k, v)
            conn.putheader("Transfer-Encoding", "chunked")
            conn.endheaders()
            conn.send(f"{len(body):x}\r\n".encode() + body + b"\r\n0\r\n\r\n")
        else:
            conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        return resp.status, {k.lower(): v for k, v in resp.getheaders()}, resp.read().decode("utf-8", "replace")
    finally:
        conn.close()


def assert_security_headers(headers) -> None:
    for name, value in SECURITY_HEADERS.items():
        assert headers.get(name) == value, (name, dict(headers))


def assert_gateway_error(resp, status: int, code: str) -> None:
    resp.expect(status, code=code)
    assert set(resp.body) == ERROR_KEYS, resp.body


# --- routing ----------------------------------------------------------------------------------------------------

def test_each_service_is_reachable_through_the_gateway(gateway, admin, customer, merchant):
    gw_admin, gw_customer, gw_merchant = via_gateway(admin), via_gateway(customer), via_gateway(merchant)
    assert gw_customer.get("/api/users/me").expect(200).body["email"] == customer.email            # user
    assert gw_merchant.get("/api/merchant/store").expect(200).body["publicId"]                      # store
    assert isinstance(gateway.get("/api/public/categories", token=None).expect(200).body, list)     # product
    assert "content" in gw_customer.get("/api/customer/orders").expect(200).body                    # order
    assert "content" in gw_admin.get("/api/admin/orders").expect(200).body


def test_split_prefixes_go_to_the_right_service(admin, customer, buyer, merchant, item):
    """/api/admin/customers/*/score is order-service; /ban and /unban are user-service."""
    from tests import factories
    factories.place_order(buyer, merchant, item)  # order-service knows only customers who ordered
    gw_admin = via_gateway(admin)
    score = gw_admin.get(f"/api/admin/customers/{buyer.public_id}/score").expect(200).body
    assert score["customerPublicId"] == buyer.public_id and "codRefusals" in score
    # user-service validates the ban body: an empty reason is its 400, not a routing 404
    gw_admin.post(f"/api/admin/customers/{customer.public_id}/ban", {"reason": ""}).expect(400, code="VALIDATION_FAILED")
    assert isinstance(gw_admin.get("/api/admin/merchants/at-risk").expect(200).body, (list, dict))     # store
    assert "content" in gw_admin.get("/api/admin/merchants/pending").expect(200).body                  # user


def test_unrouted_path_is_404(admin):
    via_gateway(admin).get("/api/nothing-here").expect(404)


def test_jwks_is_public(gateway):
    keys = gateway.get("/.well-known/jwks.json", token=None).expect(200).body["keys"]
    assert keys and all(k["kty"] == "RSA" and "d" not in k for k in keys)  # public parts only


# --- token check and role rules ---------------------------------------------------------------------------------

def test_missing_or_invalid_token_is_401(gateway, customer):
    token = customer.access_token
    header, payload, signature = token.split(".")
    tampered_payload = base64.urlsafe_b64encode(json.dumps(
        {**customer.claims, "role": "ROLE_SUPER_ADMIN"}).encode()).rstrip(b"=").decode()
    none_alg = (base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode() + "." + payload + ".")
    cases = {
        "missing": {},
        "garbage": {"Authorization": "Bearer not-a-jwt"},
        "basic-scheme": {"Authorization": f"Basic {token}"},
        "bearer-without-token": {"Authorization": "Bearer"},
        "tampered": {"Authorization": f"Bearer {header}.{tampered_payload}.{signature}"},
        "alg-none": {"Authorization": f"Bearer {none_alg}"},
    }
    for name, headers in cases.items():
        resp = gateway.get("/api/users/me", token=None, headers=headers)
        assert resp.status == 401, (name, resp.describe())
        assert resp.body["code"] == "UNAUTHORIZED" and set(resp.body) == ERROR_KEYS, name
        assert resp.headers.get("www-authenticate") == "Bearer", name
        assert_security_headers(resp.headers)


def test_public_routes_are_method_specific(gateway):
    gateway.get("/api/public/products", token=None).expect(200)
    assert_gateway_error(gateway.post("/api/public/products", {}, token=None), 401, "UNAUTHORIZED")
    assert_gateway_error(gateway.delete("/api/public/categories", token=None), 401, "UNAUTHORIZED")


@pytest.mark.parametrize("actor_name, method, path", [
    ("customer", "GET", "/api/admin/users"),
    ("customer", "GET", "/api/merchant/orders"),
    ("customer", "GET", "/api/super-admin/settings"),
    ("merchant", "GET", "/api/customer/cart"),
    ("merchant", "GET", "/api/admin/orders"),
    ("assistant", "GET", "/api/merchant/owner/bank-accounts"),   # owner only: never reaches store-service
    ("assistant", "GET", "/api/customer/orders"),
    ("admin", "GET", "/api/super-admin/settings"),
    ("admin", "GET", "/api/customer/cart"),
])
def test_role_rules(request, actor_name, method, path):
    actor = via_gateway(request.getfixturevalue(actor_name))
    resp = actor.api.request(method, path)
    assert_gateway_error(resp, 403, "ACCESS_DENIED")
    assert resp.body["message"] == "Access denied"  # the gateway's own 403, not a service's


def test_roles_allowed_by_the_rules_pass(super_admin, assistant):
    via_gateway(super_admin).get("/api/admin/orders").expect(200)          # super admin on /api/admin/**
    via_gateway(assistant).get("/api/merchant/orders").expect(200)         # assistant with ORDER_VIEW


# --- /internal/**, /fallback/** and path tricks -----------------------------------------------------------------

@pytest.mark.parametrize("path", ["/internal/settings", "/INTERNAL/settings", "/%69nternal/settings",
                                  "/internal/auth/service-token", "//internal/settings", "/internal;x=1/settings",
                                  "/fallback/user-service", "/fallback/order-service"])
def test_internal_and_fallback_paths_are_404(super_admin, path):
    status, headers, body = raw_request("GET", path, {"Authorization": f"Bearer {super_admin.access_token}"})
    assert status == 404, (path, body)
    assert json.loads(body)["code"] == "NOT_FOUND" and SECURITY_HEADERS.items() <= headers.items()


def test_internal_post_is_404(gateway):
    gateway.post("/internal/auth/service-token", {"clientId": "order-service", "clientSecret": "x"}, token=None
                 ).expect(404, code="NOT_FOUND")


@pytest.mark.parametrize("path", ["/api/public/../../internal/settings", "/api/public/products/./x",
                                  "/api/public/products%2F..%2F..%2Finternal", "/api/public/products%5C..",
                                  "/api/public/%2e%2e/internal/settings"])
def test_ambiguous_paths_are_400(path):
    status, _, body = raw_request("GET", path)
    assert status == 400, (path, body)
    assert json.loads(body)["code"] == "BAD_REQUEST"


@pytest.mark.parametrize("path", ["/USER-SERVICE/internal/users/00000000-0000-0000-0000-000000000000/security-state",
                                  "/STORE-SERVICE/internal/settings", "/PRODUCT-SERVICE/internal/variants/quote",
                                  "/ORDER-SERVICE/internal/reservations/ORD-0000-ZZZZZZ",
                                  "/STORE-SERVICE/%69nternal/settings", "/STORE-SERVICE//internal/settings"])
def test_internal_unreachable_through_discovery_routes(super_admin, path):
    """Local profile only: /<SERVICE-ID>/** debug routes. The only assertion: /internal/** stays unreachable."""
    status, _, body = raw_request("GET", path, {"Authorization": f"Bearer {super_admin.access_token}"})
    assert status == 404, (path, body)


# --- body limits ------------------------------------------------------------------------------------------------

def test_body_over_1mb_is_413():
    body = b"x" * (1024 * 1024 + 1)
    status, headers, text = raw_request("POST", "/api/users/register/customer",
                                        {"Content-Type": "application/json", "Content-Length": str(len(body))}, body)
    assert status == 413 and json.loads(text)["code"] == "PAYLOAD_TOO_LARGE"
    assert SECURITY_HEADERS.items() <= headers.items()


def test_chunked_body_is_411(customer):
    status, _, text = raw_request("POST", "/api/customer/cart/items",
                                  {"Content-Type": "application/json",
                                   "Authorization": f"Bearer {customer.access_token}"},
                                  b'{"variantPublicId":"VAR-0000-ZZZZZZ","quantity":1}', chunked=True)
    assert status == 411 and json.loads(text)["code"] == "LENGTH_REQUIRED"


# --- security headers -------------------------------------------------------------------------------------------

def test_security_headers_on_every_kind_of_response(gateway, customer):
    gw = via_gateway(customer)
    for resp in (gw.get("/api/users/me"),                                 # proxied 200
                 gateway.get("/api/public/products", token=None),         # public 200
                 gateway.get("/api/users/me", token=None),                # gateway 401
                 gw.get("/api/admin/users"),                              # gateway 403
                 gateway.get("/internal/settings", token=None),           # guard 404
                 gw.get("/api/customer/orders/ORD-0000-ZZZZZZ")):         # service 404
        assert_security_headers(resp.headers)


def test_auth_responses_are_not_cached(customer):
    """Logout is under /api/auth/** but not in the auth rate-limit tier, so this costs no auth-bucket token."""
    gw = via_gateway(customer)
    resp = gw.post("/api/auth/logout", {"refreshToken": customer.refresh_token})
    resp.expect(204)
    assert resp.headers.get("cache-control") == "no-store" and resp.headers.get("pragma") == "no-cache"
    customer.login()  # direct: new tokens for later tests


# --- CORS -------------------------------------------------------------------------------------------------------

def preflight(gateway, origin: str, method: str = "POST", headers: str = "authorization,content-type,idempotency-key",
              path: str = "/api/customer/checkout"):
    return gateway.request("OPTIONS", path, token=None, headers={
        "Origin": origin, "Access-Control-Request-Method": method, "Access-Control-Request-Headers": headers})


def test_cors_preflight_allows_idempotency_key(gateway):
    resp = preflight(gateway, ALLOWED_ORIGIN)
    assert resp.status == 200, resp.describe()
    h = resp.headers
    assert h.get("access-control-allow-origin") == ALLOWED_ORIGIN
    assert h.get("access-control-allow-credentials") == "true"
    allowed = {x.strip().lower() for x in h.get("access-control-allow-headers", "").split(",")}
    assert {"authorization", "content-type", "idempotency-key"} <= allowed
    assert "POST" in h.get("access-control-allow-methods", "") and h.get("access-control-max-age") == "3600"
    assert_security_headers(h)


def test_cors_preflight_refuses_unlisted_origin(gateway):
    for origin in ("http://evil.example", "http://localhost:3001", "https://localhost:5173", "null"):
        resp = preflight(gateway, origin)
        assert resp.status == 403, (origin, resp.describe())
        assert "access-control-allow-origin" not in resp.headers, origin


def test_cors_preflight_refuses_unlisted_header(gateway):
    for headers in ("x-custom-header", "x-user-roles,x-user-id"):
        resp = preflight(gateway, ALLOWED_ORIGIN, headers=headers)
        assert resp.status == 403, (headers, resp.describe())
        assert "access-control-allow-origin" not in resp.headers
    # Mixed with allowed ones, Spring answers 200 with only the allowed subset (FINDINGS N5-1): the unlisted header
    # is never listed, so the browser refuses to send the real request
    for headers, allowed in (("authorization,x-user-roles", {"authorization"}),
                             ("content-type,x-user-id", {"content-type"})):
        resp = preflight(gateway, ALLOWED_ORIGIN, headers=headers)
        listed = {x.strip().lower() for x in resp.headers.get("access-control-allow-headers", "").split(",") if x}
        assert listed == allowed, (headers, resp.describe())


def test_cors_preflight_refuses_unlisted_method(gateway):
    assert preflight(gateway, ALLOWED_ORIGIN, method="TRACE").status == 403


def test_cors_actual_request(gateway, customer):
    resp = via_gateway(customer).get("/api/users/me", headers={"Origin": ALLOWED_ORIGIN}).expect(200)
    assert resp.headers.get_list("access-control-allow-origin") == [ALLOWED_ORIGIN]  # deduplicated: one value
    exposed = {x.strip().lower() for x in resp.headers.get("access-control-expose-headers", "").split(",")}
    assert {"retry-after", "x-ratelimit-remaining"} <= exposed
    foreign = gateway.get("/api/public/products", token=None, headers={"Origin": "http://evil.example"})
    assert foreign.status == 403 and "access-control-allow-origin" not in foreign.headers


# --- spoofed identity headers -----------------------------------------------------------------------------------

def test_spoofed_identity_headers_have_no_effect(gateway, customer, customer2, admin):
    spoof = {"X-User-Id": customer2.user_id, "X-User-Roles": "ROLE_SUPER_ADMIN,ROLE_ADMIN"}
    gw = via_gateway(customer)
    me = gw.get("/api/users/me", headers=spoof).expect(200).body
    assert me["email"] == customer.email  # identity comes from the token only
    assert_gateway_error(gw.get("/api/admin/users", headers=spoof), 403, "ACCESS_DENIED")
    assert_gateway_error(gateway.get("/api/admin/users", token=None, headers=spoof), 401, "UNAUTHORIZED")
    # Another service, and the services themselves when called directly: they read the JWT, not these headers
    orders = gw.get("/api/customer/orders", headers=spoof).expect(200).body["content"]
    assert all(o.get("customerPublicId") in (None, customer.public_id) for o in orders)
    assert customer.get("/api/users/me", headers=spoof).expect(200).body["email"] == customer.email
    customer.get("/api/admin/users", headers=spoof).expect(403, code="ACCESS_DENIED")


# --- rate-limit headers (no bucket is emptied here) -------------------------------------------------------------

def test_rate_limit_headers(gateway, customer):
    gw = via_gateway(customer)
    first = int(gw.get("/api/users/me").expect(200).headers["x-ratelimit-remaining"])
    second = int(gw.get("/api/users/me").expect(200).headers["x-ratelimit-remaining"])
    assert second < first  # the default tier, keyed by user
    # Other tiers have their own buckets: public browsing (300/min) is not the user's default bucket
    public = int(gateway.get("/api/public/products", token=None).expect(200).headers["x-ratelimit-remaining"])
    assert public > second
    # Requests rejected before routing (guards) or by the token check are not routed, but still answered
    assert gateway.get("/internal/settings", token=None).status == 404

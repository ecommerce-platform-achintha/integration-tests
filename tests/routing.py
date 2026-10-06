"""Path -> owning service, copied from api-gateway's route predicates (application.yml).

In direct mode the ApiClient sends each request to the owning service's own port; test_stage0_smoke checks that
this table still equals the gateway's configuration.
"""
from __future__ import annotations

import re
from functools import lru_cache

# Same order and patterns as spring.cloud.gateway.server.webflux.routes in api-gateway/application.yml
ROUTES: dict[str, list[str]] = {
    "user": [
        "/api/auth/**",
        "/api/users/**",
        "/api/merchant/application/**",
        "/api/merchant/owner/assistants/**",
        "/api/admin/users/**",
        "/api/admin/merchants/pending",
        "/api/admin/merchants/*/{action:approve|reject|ban|unban}",
        "/api/admin/customers/*/{action:ban|unban}",
        "/api/super-admin/admins/**",
        "/.well-known/jwks.json",
    ],
    "store": [
        "/api/public/stores/**",
        "/api/merchant/store/**",
        "/api/merchant/owner/bank-accounts/**",
        "/api/merchant/owner/couriers/**",
        "/api/merchant/owner/shipping-templates/**",
        "/api/merchant/owner/metrics/**",
        "/api/merchant/reviews/**",
        "/api/customer/stores/**",
        "/api/admin/stores/**",
        "/api/admin/merchants/at-risk",
        "/api/admin/banks/**",
        "/api/admin/couriers/**",
        "/api/super-admin/settings/**",
        "/api/super-admin/holidays/**",
        "/api/super-admin/merchants/**",
    ],
    "product": [
        "/api/public/products/**",
        "/api/public/categories/**",
        "/api/merchant/products/**",
        "/api/merchant/variants/**",
        "/api/merchant/discounts/**",
        "/api/customer/products/**",
        "/api/admin/products/**",
        "/api/admin/categories/**",
        "/api/admin/reviews/**",
    ],
    "order": [
        "/api/customer/cart/**",
        "/api/customer/checkout",
        "/api/customer/orders/**",
        "/api/customer/complaints/**",
        "/api/merchant/orders/**",
        "/api/merchant/customer-blocks/**",
        "/api/admin/orders/**",
        "/api/admin/complaints/**",
        "/api/admin/flagged-references/**",
        "/api/admin/customers/*/score",
    ],
}


def _to_regex(pattern: str) -> re.Pattern[str]:
    """Spring PathPattern subset used by the gateway: '/**' tail, '*' segment, '{name:regex}' segment."""
    out = ""
    rest = pattern
    tail = ""
    if rest.endswith("/**"):
        rest, tail = rest[:-3], "(?:/.*)?"
    for part in re.split(r"(\{[^}]*\}|\*)", rest):
        if part == "*":
            out += "[^/]+"
        elif part.startswith("{"):
            body = part[1:-1]
            out += "(?:" + body.split(":", 1)[1] + ")" if ":" in body else "[^/]+"
        else:
            out += re.escape(part)
    return re.compile("^" + out + tail + "$")


@lru_cache(maxsize=None)
def _compiled() -> list[tuple[str, re.Pattern[str]]]:
    return [(service, _to_regex(p)) for service, patterns in ROUTES.items() for p in patterns]


def services_for(path: str) -> list[str]:
    """Every service whose patterns match (the gateway guarantees at most one)."""
    path = path.split("?", 1)[0]
    return [service for service, regex in _compiled() if regex.match(path)]


def service_for(path: str) -> str:
    matches = services_for(path)
    if not matches:
        raise ValueError(f"No gateway route matches {path!r}; add it to tests/routing.py if the gateway routes it")
    return matches[0]

"""Signed-in users. Claims are read from the JWT payload without verifying it (the services verify)."""
from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass, field
from typing import Any

from tests.api import ApiClient, ApiResponse

# Re-login when the 10-minute access token has less than this left
REFRESH_MARGIN_SECONDS = 60


def decode_jwt(token: str) -> dict[str, Any]:
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


@dataclass
class Actor:
    email: str
    password: str
    client: ApiClient
    label: str = ""
    tokens: dict[str, Any] = field(default_factory=dict)
    claims: dict[str, Any] = field(default_factory=dict)
    # Free-form data a factory attaches (registration payload, store, bank account, ...)
    extra: dict[str, Any] = field(default_factory=dict)

    # --- tokens -------------------------------------------------------------------------------------------------

    def login(self, expect_status: int = 200) -> ApiResponse:
        """POST /api/auth/login; on 200 the new tokens replace the old ones."""
        resp = self.client.post("/api/auth/login", {"email": self.email, "password": self.password}, token=None)
        resp.expect(expect_status)
        if resp.status == 200:
            self.set_tokens(resp.body)
        return resp

    def refresh(self, expect_status: int = 200) -> ApiResponse:
        resp = self.client.post("/api/auth/refresh", {"refreshToken": self.refresh_token}, token=None)
        resp.expect(expect_status)
        if resp.status == 200:
            self.set_tokens(resp.body)
        return resp

    def set_tokens(self, token_response: dict[str, Any]) -> None:
        self.tokens = dict(token_response)
        self.claims = decode_jwt(token_response["accessToken"])

    @property
    def access_token(self) -> str:
        """The current access token, re-logging in first when it expires within REFRESH_MARGIN_SECONDS."""
        if not self.tokens or self.claims.get("exp", 0) - time.time() < REFRESH_MARGIN_SECONDS:
            self.login()
        return self.tokens["accessToken"]

    @property
    def refresh_token(self) -> str:
        return self.tokens["refreshToken"]

    # --- claims -------------------------------------------------------------------------------------------------

    @property
    def user_id(self) -> str:
        return self.claims["sub"]

    @property
    def public_id(self) -> str:
        return self.claims["pid"]

    @property
    def role(self) -> str:
        return self.claims["role"]

    @property
    def status(self) -> str:
        return self.claims.get("status")

    @property
    def store_id(self) -> str | None:
        return self.claims.get("storeId")

    @property
    def permissions(self) -> list[str]:
        return self.claims.get("perms", [])

    @property
    def must_change_password(self) -> bool:
        return bool(self.tokens.get("mustChangePassword"))

    # --- requests as this actor ---------------------------------------------------------------------------------

    @property
    def api(self) -> ApiClient:
        return self.client.with_token(lambda: self.access_token)

    def get(self, path: str, **kw: Any) -> ApiResponse:
        return self.api.get(path, **kw)

    def post(self, path: str, json: Any = None, **kw: Any) -> ApiResponse:
        return self.api.post(path, json, **kw)

    def put(self, path: str, json: Any = None, **kw: Any) -> ApiResponse:
        return self.api.put(path, json, **kw)

    def patch(self, path: str, json: Any = None, **kw: Any) -> ApiResponse:
        return self.api.patch(path, json, **kw)

    def delete(self, path: str, **kw: Any) -> ApiResponse:
        return self.api.delete(path, **kw)

    def __repr__(self) -> str:
        pid = self.claims.get("pid", "?")
        return f"<Actor {self.label or self.email} {self.claims.get('role', '?')} {pid}>"


class Via:
    """
    An actor whose requests go through another base mode (e.g. the gateway) while logins and token refresh stay on
    the actor's own client (direct to user-service), so they never spend the gateway's auth bucket. Everything else
    (claims, extra, login) is the wrapped actor's, so factories accept it in place of an Actor.
    """

    def __init__(self, actor: Actor, mode: str = "gateway"):
        self._actor = actor
        self._mode = mode

    def __getattr__(self, name: str) -> Any:
        return getattr(self._actor, name)

    @property
    def api(self) -> ApiClient:
        return self._actor.api.in_mode(self._mode)

    def get(self, path: str, **kw: Any) -> ApiResponse:
        return self.api.get(path, **kw)

    def post(self, path: str, json: Any = None, **kw: Any) -> ApiResponse:
        return self.api.post(path, json, **kw)

    def put(self, path: str, json: Any = None, **kw: Any) -> ApiResponse:
        return self.api.put(path, json, **kw)

    def patch(self, path: str, json: Any = None, **kw: Any) -> ApiResponse:
        return self.api.patch(path, json, **kw)

    def delete(self, path: str, **kw: Any) -> ApiResponse:
        return self.api.delete(path, **kw)

    def __repr__(self) -> str:
        return f"<Via {self._mode} {self._actor!r}>"


def via_gateway(actor: Actor) -> Via:
    return Via(actor, "gateway")

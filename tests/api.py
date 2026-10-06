"""Thin httpx wrapper: base-URL routing, bearer tokens, Idempotency-Key, and readable status assertions."""
from __future__ import annotations

import json as jsonlib
import uuid
from typing import Any, Callable

import httpx

from tests.config import SETTINGS, Settings
from tests.routing import service_for

# Masked in failure messages (request and response bodies)
SECRET_KEYS = {
    "password", "temporaryPassword", "currentPassword", "newPassword", "clientSecret",
    "accessToken", "refreshToken",
}

_UNSET = object()


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: ("****" if k in SECRET_KEYS and v is not None else redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


def new_idempotency_key() -> str:
    return str(uuid.uuid4())


class ApiResponse:
    """An httpx.Response plus the request that produced it, for assertion messages."""

    def __init__(self, response: httpx.Response, request_body: Any):
        self.raw = response
        self.request_body = request_body

    @property
    def status(self) -> int:
        return self.raw.status_code

    @property
    def headers(self) -> httpx.Headers:
        return self.raw.headers

    @property
    def text(self) -> str:
        return self.raw.text

    @property
    def body(self) -> Any:
        """Parsed JSON, or None for an empty / non-JSON body."""
        if not self.raw.content:
            return None
        try:
            return self.raw.json()
        except ValueError:
            return None

    def json(self) -> Any:
        return self.body

    def __getitem__(self, key: str) -> Any:
        return self.body[key]

    @property
    def code(self) -> str | None:
        """The standard error body's machine-readable code."""
        body = self.body
        return body.get("code") if isinstance(body, dict) else None

    def describe(self) -> str:
        req = self.raw.request
        body = self.body
        shown = jsonlib.dumps(redact(body), indent=2, default=str) if body is not None else self.text[:4000]
        req_body = (jsonlib.dumps(redact(self.request_body), indent=2, default=str)
                    if self.request_body is not None else "<none>")
        return (f"{req.method} {req.url}\n"
                f"request body: {req_body}\n"
                f"response: {self.status}\n{shown}")

    def expect(self, *statuses: int, code: str | None = None) -> "ApiResponse":
        """Asserts the status (any of `statuses`) and optionally the error `code`; returns self for chaining."""
        if self.status not in statuses:
            raise AssertionError(f"expected HTTP {' or '.join(map(str, statuses))}\n{self.describe()}")
        if code is not None and self.code != code:
            raise AssertionError(f"expected error code {code!r}, got {self.code!r}\n{self.describe()}")
        return self

    def __repr__(self) -> str:
        return f"<ApiResponse {self.raw.request.method} {self.raw.request.url} -> {self.status}>"


class ApiClient:
    """
    Sends requests to the owning service (BASE_MODE=direct) or to the gateway (BASE_MODE=gateway).

    `token_provider` returns the bearer token to use (an Actor passes its auto-refreshing access token); a call
    can override it with `token="..."` or send none with `token=None`.
    """

    def __init__(self, settings: Settings = SETTINGS, token_provider: Callable[[], str | None] | None = None,
                 mode: str | None = None, http: httpx.Client | None = None):
        self.settings = settings
        self.mode = mode or settings.base_mode
        self.token_provider = token_provider
        self._http = http or httpx.Client(timeout=settings.http_timeout)

    def with_token(self, token_provider: Callable[[], str | None] | None) -> "ApiClient":
        return ApiClient(self.settings, token_provider, self.mode, self._http)

    def in_mode(self, mode: str) -> "ApiClient":
        return ApiClient(self.settings, self.token_provider, mode, self._http)

    def base_url(self, path: str) -> str:
        if self.mode == "gateway":
            return self.settings.gateway_url
        return self.settings.service_url(service_for(path))

    def url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return self.base_url(path) + path

    def request(self, method: str, path: str, *, json: Any = None, params: dict | None = None,
                headers: dict | None = None, token: Any = _UNSET, idempotency_key: str | bool | None = None,
                content: bytes | str | None = None) -> ApiResponse:
        """
        idempotency_key: True generates a fresh UUID, a string is sent as-is, None/False sends no header.
        """
        hdrs: dict[str, str] = {"Accept": "application/json"}
        bearer = (self.token_provider() if self.token_provider else None) if token is _UNSET else token
        if bearer:
            hdrs["Authorization"] = f"Bearer {bearer}"
        if idempotency_key is True:
            hdrs["Idempotency-Key"] = new_idempotency_key()
        elif idempotency_key:
            hdrs["Idempotency-Key"] = str(idempotency_key)
        if headers:
            hdrs.update(headers)
        if params:
            params = {k: v for k, v in params.items() if v is not None}
        response = self._http.request(method, self.url(path), json=json, params=params, headers=hdrs,
                                      content=content)
        return ApiResponse(response, json if json is not None else content)

    def get(self, path: str, **kw: Any) -> ApiResponse:
        return self.request("GET", path, **kw)

    def post(self, path: str, json: Any = None, **kw: Any) -> ApiResponse:
        return self.request("POST", path, json=json, **kw)

    def put(self, path: str, json: Any = None, **kw: Any) -> ApiResponse:
        return self.request("PUT", path, json=json, **kw)

    def patch(self, path: str, json: Any = None, **kw: Any) -> ApiResponse:
        return self.request("PATCH", path, json=json, **kw)

    def delete(self, path: str, **kw: Any) -> ApiResponse:
        return self.request("DELETE", path, **kw)

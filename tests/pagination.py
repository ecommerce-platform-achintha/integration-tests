"""Checks the PageResponse shape every list endpoint returns: {content, page, size, totalElements, totalPages}."""
from __future__ import annotations

import math
from typing import Any

from tests.api import ApiClient

PAGE_KEYS = {"content", "page", "size", "totalElements", "totalPages"}
MAX_SIZE = 50        # Pagination.MAX_SIZE (user-facing lists)
MAX_ADMIN_SIZE = 100  # Pagination.MAX_ADMIN_SIZE (admin lists)


def assert_page_shape(body: Any, page: int | None = None, size: int | None = None) -> None:
    assert isinstance(body, dict), f"expected a page object, got {type(body).__name__}: {body!r}"
    assert set(body) == PAGE_KEYS, f"page keys {sorted(body)} != {sorted(PAGE_KEYS)}"
    assert isinstance(body["content"], list)
    for key in ("page", "size", "totalElements", "totalPages"):
        assert isinstance(body[key], int) and body[key] >= 0, f"{key}={body[key]!r}"
    if page is not None:
        assert body["page"] == page, f"page {body['page']} != {page}"
    if size is not None:
        assert body["size"] == size, f"size {body['size']} != {size}"
        assert len(body["content"]) <= size
        assert body["totalPages"] == math.ceil(body["totalElements"] / size), body
    if body["page"] < body["totalPages"] - 1 and size is not None:
        assert len(body["content"]) == size, "a page before the last must be full"


def assert_paginated(client: ApiClient, path: str, params: dict | None = None, cap: int = MAX_SIZE,
                     check_negative_page: bool = True) -> dict:
    """
    Verifies shape, that `page`/`size` are honoured, that `size` above the cap is clamped to it, and that a
    negative page is a 400. Returns the first page (size=cap).
    """
    params = dict(params or {})

    first = client.get(path, params={**params, "page": 0, "size": 1}).expect(200).body
    assert_page_shape(first, page=0, size=1)

    capped = client.get(path, params={**params, "page": 0, "size": cap + 1}).expect(200).body
    assert_page_shape(capped, page=0, size=cap)

    if first["totalElements"] >= 2:
        second = client.get(path, params={**params, "page": 1, "size": 1}).expect(200).body
        assert_page_shape(second, page=1, size=1)
        assert len(second["content"]) == 1
        assert second["content"][0] != first["content"][0], "page 1 repeats page 0"

    if check_negative_page:
        client.get(path, params={**params, "page": -1, "size": 1}).expect(400)
    return capped

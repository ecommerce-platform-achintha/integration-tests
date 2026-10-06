"""Unique test data per run. Values match the services' validation rules (see the comments for the source)."""
from __future__ import annotations

import itertools
import random
import secrets
import string
import time

# Short, sortable run id: base-36 seconds since epoch + 3 random chars, e.g. "t1m2k9x4q"
RUN_ID = (format(int(time.time()), "x")[-6:] + "".join(random.choices(string.ascii_lowercase + string.digits, k=3)))
_counter = itertools.count(1)


def _n() -> int:
    return next(_counter)


def unique(prefix: str = "") -> str:
    return f"{prefix}{RUN_ID}{_n()}"


def email(kind: str = "user") -> str:
    # @Email + max 254; stored lower-cased
    return f"it.{kind}.{RUN_ID}.{_n()}@example.com"


def password() -> str:
    # PasswordPolicy: 12-128 chars, >= 4 distinct, not on the deny list, no leading/trailing whitespace
    return f"It-{secrets.token_urlsafe(12)}-Pw9"


def nic(old_format: bool = False) -> str:
    # Nic.REGEX ^(\d{9}[VvXx]|\d{12})$ ; assistants' NICs are unique across stores (ux_users_assistant_nic)
    if old_format:
        return "".join(random.choices(string.digits, k=9)) + random.choice("VX")
    return "".join(random.choices(string.digits, k=12))


def phone() -> str:
    # Validation.PHONE_REGEX ^\+?[1-9]\d{7,14}$
    return "+9477" + "".join(random.choices(string.digits, k=7))


def store_name() -> str:
    # 3-100 chars, unique case-insensitively. Zero-padded so one name is never a substring of another (q search)
    return f"IT Store {RUN_ID} {_n():05d}"


def sku() -> str:
    return f"IT-{RUN_ID}-{_n()}".upper()


def slug(prefix: str = "it") -> str:
    # product-service Validation.SLUG_REGEX ^[a-z0-9]+(-[a-z0-9]+)*$
    return f"{prefix}-{RUN_ID}-{_n()}".lower()


def code(prefix: str = "IT") -> str:
    # store-service Validation.CODE_REGEX ^[A-Z][A-Z0-9_]{1,19}$
    return f"{prefix}_{RUN_ID}{_n()}".upper()[:20]


def account_number() -> str:
    # Validation.ACCOUNT_NUMBER_REGEX ^\d{6,20}$
    return "".join(random.choices(string.digits, k=12))


def document_keys(count: int = 1) -> list[str]:
    # Validation.STORAGE_KEY_REGEX ^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$
    return [f"docs/{RUN_ID}/{_n()}.pdf" for _ in range(count)]

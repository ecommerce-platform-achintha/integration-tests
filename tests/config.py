"""Suite configuration, read from the environment (and a git-ignored .env file) with local defaults."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env", override=False)


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


@dataclass(frozen=True)
class Settings:
    user_url: str
    product_url: str
    order_url: str
    store_url: str
    gateway_url: str
    eureka_url: str
    base_mode: str
    super_admin_email: str
    super_admin_initial_password: str
    super_admin_new_password: str
    gateway_config_path: Path
    http_timeout: float

    def service_url(self, service: str) -> str:
        return {
            "user": self.user_url,
            "product": self.product_url,
            "order": self.order_url,
            "store": self.store_url,
            "gateway": self.gateway_url,
        }[service]


def load_settings() -> Settings:
    mode = _env("BASE_MODE", "direct").lower()
    if mode not in ("direct", "gateway"):
        raise ValueError(f"BASE_MODE must be 'direct' or 'gateway', got {mode!r}")
    return Settings(
        user_url=_env("USER_URL", "http://localhost:8081").rstrip("/"),
        product_url=_env("PRODUCT_URL", "http://localhost:8082").rstrip("/"),
        order_url=_env("ORDER_URL", "http://localhost:8083").rstrip("/"),
        store_url=_env("STORE_URL", "http://localhost:8084").rstrip("/"),
        gateway_url=_env("GATEWAY_URL", "http://localhost:8080").rstrip("/"),
        eureka_url=_env("EUREKA_URL", "http://localhost:8761").rstrip("/"),
        base_mode=mode,
        # LOCAL-DEV VALUES ONLY (the user-service local bootstrap). Never point this suite at a shared or real
        # environment: it creates accounts, changes the super admin password and changes platform settings.
        super_admin_email=_env("SUPER_ADMIN_EMAIL", "superadmin@example.com"),
        super_admin_initial_password=_env("SUPER_ADMIN_INITIAL_PASSWORD", "SuperAdmin#Local2026"),
        super_admin_new_password=_env("SUPER_ADMIN_NEW_PASSWORD", "SuperAdmin#Local2027"),
        gateway_config_path=(ROOT / _env("GATEWAY_CONFIG_PATH",
                                         "../api-gateway/src/main/resources/application.yml")).resolve(),
        http_timeout=float(_env("HTTP_TIMEOUT_SECONDS", "15")),
    )


SETTINGS = load_settings()

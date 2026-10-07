"""Session-scoped actors and master data. Nothing here starts or stops services."""
from __future__ import annotations

import pytest

from tests import factories
from tests.actors import Actor
from tests.api import ApiClient
from tests.config import SETTINGS, Settings


def pytest_collection_modifyitems(config, items):
    for item in items:
        marker = item.get_closest_marker("skip_time_based")
        if marker is not None:
            reason = marker.kwargs.get("reason") or (marker.args[0] if marker.args else "time-based")
            item.add_marker(pytest.mark.skip(reason=f"time-based: {reason}"))
    # Tests that exhaust a gateway rate-limit bucket run after everything else, whatever was selected
    items.sort(key=lambda item: item.get_closest_marker("exhausts_rate_limit") is not None)


def pytest_report_header(config):
    return [f"BASE_MODE={SETTINGS.base_mode}  gateway={SETTINGS.gateway_url}  user={SETTINGS.user_url}  "
            f"product={SETTINGS.product_url}  order={SETTINGS.order_url}  store={SETTINGS.store_url}"]


@pytest.fixture(scope="session")
def settings() -> Settings:
    return SETTINGS


@pytest.fixture(scope="session")
def client(settings) -> ApiClient:
    """Unauthenticated client in the configured BASE_MODE."""
    return ApiClient(settings)


def login_super_admin(client: ApiClient, settings: Settings) -> Actor:
    """
    Handles both states without risking the lockout (5 failed passwords per 15 min, LoginThrottleService):
    the NEW password is tried first, so once the password has been changed a run costs no failed attempt. Only
    the very first run costs one (and the successful login right after resets the per-user counter;
    the per-IP counter, 20 per 15 min, is not reset by a success).
    """
    actor = Actor(settings.super_admin_email, settings.super_admin_new_password, client, "super_admin")
    resp = client.post("/api/auth/login", {"email": actor.email, "password": actor.password}, token=None)
    if resp.status == 200:
        actor.set_tokens(resp.body)
        assert not actor.must_change_password, "super admin logged in with the NEW password but must change it"
        return actor
    resp.expect(401, code="INVALID_CREDENTIALS")

    initial = client.post("/api/auth/login",
                          {"email": actor.email, "password": settings.super_admin_initial_password}, token=None)
    initial.expect(200)  # 401 here: neither password works; fix SUPER_ADMIN_* (do not retry, lockout after 5)
    actor.password = settings.super_admin_initial_password
    actor.set_tokens(initial.body)
    factories.change_password(actor, settings.super_admin_new_password)
    return actor


@pytest.fixture(scope="session")
def super_admin(client, settings) -> Actor:
    actor = login_super_admin(client, settings)
    assert actor.role == "ROLE_SUPER_ADMIN"
    return actor


@pytest.fixture(scope="session")
def admin(super_admin, client) -> Actor:
    return factories.create_admin(super_admin, client)


@pytest.fixture(scope="session")
def customer(client) -> Actor:
    return factories.register_customer(client, "customer")


@pytest.fixture(scope="session")
def customer2(client) -> Actor:
    return factories.register_customer(client, "customer2")


@pytest.fixture(scope="session")
def bank(super_admin) -> dict:
    return factories.ensure_bank(super_admin)


@pytest.fixture(scope="session")
def courier(super_admin) -> dict:
    return factories.ensure_courier(super_admin)


@pytest.fixture(scope="session")
def courier_no_cod(super_admin) -> dict:
    return factories.ensure_courier(super_admin, factories.COURIER_NO_COD)


@pytest.fixture(scope="session")
def category(admin) -> dict:
    return factories.ensure_category(admin)


def _ready_merchant(client: ApiClient, admin: Actor, label: str, courier: dict, bank: dict) -> Actor:
    merchant = factories.register_merchant(client, label)
    factories.approve_merchant(admin, merchant)
    return factories.setup_merchant_store(merchant, courier_code=courier["code"], bank_code=bank["code"])


@pytest.fixture(scope="session")
def merchant(client, admin, courier, bank) -> Actor:
    """Approved, re-logged-in, with a published store, bank account, courier, FLAT template and COD on."""
    return _ready_merchant(client, admin, "merchant", courier, bank)


@pytest.fixture(scope="session")
def merchant2(client, admin, courier, bank) -> Actor:
    return _ready_merchant(client, admin, "merchant2", courier, bank)


@pytest.fixture(scope="session")
def assistant(merchant, client) -> Actor:
    """Assistant of `merchant` with every permission, password already changed."""
    return factories.create_assistant(merchant, client)


@pytest.fixture(scope="session")
def product(merchant, category) -> dict:
    """An ACTIVE simple product of `merchant` (list price 1000.00, stock 50, COD allowed)."""
    return factories.create_active_product(merchant, factories.simple_product_payload(category["slug"]))


# --- stage 4: per-test actors and stock, so order tests never drain shared state --------------------------------

@pytest.fixture
def buyer(client) -> Actor:
    """A fresh customer with one address (own cart, open-order budget and score)."""
    return factories.new_buyer(client, "buyer")


@pytest.fixture
def item(client, merchant, category) -> dict:
    """A fresh ACTIVE simple product of `merchant` (1000.00, stock 50, COD allowed). Orders never touch `product`.
    Waits until it is public: a just-created store reaches product-service through Kafka."""
    product = factories.create_active_product(merchant, factories.simple_product_payload(category["slug"]))
    factories.wait_publicly_visible(client, product)
    return product


@pytest.fixture
def item2(client, merchant2, category) -> dict:
    """The same in `merchant2`'s store (500.00), for multi-store carts."""
    product = factories.create_active_product(merchant2,
                                              factories.simple_product_payload(category["slug"], price="500.00"))
    factories.wait_publicly_visible(client, product)
    return product


# --- stage 5: the gateway ---------------------------------------------------------------------------------------

@pytest.fixture(scope="session")
def gateway(client) -> ApiClient:
    """Anonymous client that always goes through the gateway (actors: tests.actors.via_gateway)."""
    return client.in_mode("gateway")

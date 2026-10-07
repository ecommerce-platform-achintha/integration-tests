"""
Stage 4, order-service: cases that depend on a running timer. Black-box tests cannot move the services' clock, and
the timers are hours or days (platform settings, counted in business hours around the holiday calendar), so these
are always skipped. Each docstring says what would be asserted. Covering them needs a test clock in order-service,
or settings that are seconds rather than hours.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.stage4


@pytest.mark.skip_time_based(reason="timers.merchant-response-hours (24 h)")
def test_merchant_response_timeout():
    """AWAITING_MERCHANT past the deadline -> EXPIRED_MERCHANT by the scheduler, stock released, merchant penalty
    RESPONSE_TIMEOUT (score.merchant.response-timeout) in store-service; no customer penalty."""


@pytest.mark.skip_time_based(reason="timers.customer-confirmation-hours (24 h)")
def test_customer_confirmation_timeout():
    """AWAITING_CUSTOMER_CONFIRMATION past the deadline -> EXPIRED_CUSTOMER, stock released, customer score
    score.customer.expired (-1, QUOTE_EXPIRED)."""


@pytest.mark.skip_time_based(reason="any user deadline (24 h)")
def test_user_action_after_deadline_is_refused():
    """A quote/confirm/cancel after the running deadline but before the scheduler ran -> 409 DEADLINE_PASSED."""


@pytest.mark.skip_time_based(reason="timers.ship-by-hours (72 h)")
def test_late_shipment_flag():
    """READY_TO_SHIP past ship-by -> stays open, flags.lateShipment=true, OrderShipmentOverdue, merchant
    LATE_SHIPMENT penalty and lateShipments metric in store-service."""


@pytest.mark.skip_time_based(reason="timers.auto-complete-days (7 days)")
def test_auto_complete():
    """SHIPPED past auto-complete -> COMPLETED without the customer, customer score +1 (ORDER_COMPLETED), store
    completedOrders/revenue updated."""


@pytest.mark.skip_time_based(reason="app.orders.idempotency-ttl (24 h)")
def test_idempotency_key_expires():
    """The same Idempotency-Key after 24 h runs the request again instead of replaying it."""

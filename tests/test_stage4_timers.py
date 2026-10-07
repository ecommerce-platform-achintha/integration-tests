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


@pytest.mark.skip_time_based(reason="timers.payment-submission-hours (24 h)")
def test_payment_submission_timeout():
    """AWAITING_PAYMENT past the payment deadline -> EXPIRED_PAYMENT, stock released, customer score -1
    (PAYMENT_EXPIRED). A rejected payment does not extend the deadline."""


@pytest.mark.skip_time_based(reason="timers.payment-verification-hours (24 h)")
def test_late_payment_verification_flag():
    """PAYMENT_SUBMITTED past verification -> stays open, flags.lateVerification=true, hold extended, merchant
    LATE_VERIFICATION penalty in store-service."""


@pytest.mark.skip_time_based(reason="complaints.window-days (30 days)")
def test_complaint_window_closes():
    """A complaint more than 30 days after completion -> 409 COMPLAINT_NOT_ALLOWED."""


@pytest.mark.skip_time_based(reason="cod.objection-window-days (7 days)")
def test_cod_objection_window_closes():
    """An objection more than 7 days after the failed delivery -> 409 OBJECTION_NOT_ALLOWED."""


@pytest.mark.skip_time_based(reason="timers.ban-grace-days (14 days)")
def test_banned_merchant_open_orders_need_admin_resolution():
    """At the end of BAN_GRACE (merchant BANNED) open orders get flags.needsAdminResolution and appear in
    GET /api/admin/orders?needsResolution=true; the merchant can no longer act (403)."""

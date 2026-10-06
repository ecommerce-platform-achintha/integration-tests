"""Polling for eventually consistent state (Kafka events, outbox relay every 1 s). Never sleep in tests."""
from __future__ import annotations

import time
from typing import Callable, TypeVar

T = TypeVar("T")


def wait_until(predicate: Callable[[], T], timeout: float = 30, interval: float = 1,
               message: str = "condition") -> T:
    """
    Calls `predicate` until it returns a truthy value and returns that value. Assertion errors raised by the
    predicate count as "not yet". On timeout, fails with the last value or error.
    """
    deadline = time.monotonic() + timeout
    last: object = None
    while True:
        try:
            result = predicate()
            if result:
                return result
            last = result
        except AssertionError as e:
            last = e
        if time.monotonic() >= deadline:
            raise AssertionError(f"timed out after {timeout}s waiting for {message}; last result: {last}")
        time.sleep(interval)

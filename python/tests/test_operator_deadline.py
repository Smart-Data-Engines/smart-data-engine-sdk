"""The dedicated-process watchdog does not steal application alarms or thread ownership."""

from __future__ import annotations

import signal
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from sde import CutoverReceipt, MigrationRefused
from sde._operator_deadline import DeadlineInterrupt, OperatorDeadline


def test_watchdog_interrupts_and_restores_the_previous_signal_handler() -> None:
    original = signal.getsignal(signal.SIGALRM)
    with pytest.raises(DeadlineInterrupt), OperatorDeadline() as deadline:
        deadline.arm(40)
        time.sleep(1)
    assert signal.getsignal(signal.SIGALRM) == original
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)


def test_an_existing_alarm_is_preserved_on_refusal() -> None:
    signal.setitimer(signal.ITIMER_REAL, 30)
    try:
        with pytest.raises(MigrationRefused, match="existing process alarm"), OperatorDeadline():
            pytest.fail("the caller's alarm was taken over")
        assert signal.getitimer(signal.ITIMER_REAL)[0] > 25
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)


def test_worker_thread_cannot_start_a_local_operator_deadline() -> None:
    def worker() -> None:
        with OperatorDeadline():
            pytest.fail("operator entered a worker thread")

    with (
        ThreadPoolExecutor(max_workers=1) as pool,
        pytest.raises(MigrationRefused, match="main thread"),
    ):
        pool.submit(worker).result(timeout=5)
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)


@pytest.mark.parametrize("value", [0, -1, True, 1.5])
def test_invalid_deadline_cannot_silently_disable_the_watchdog(value: Any) -> None:
    with OperatorDeadline() as deadline, pytest.raises(MigrationRefused, match="positive integer"):
        deadline.arm(value)


def test_receipt_is_a_snapshot_of_nested_results() -> None:
    raw = {"verification": {"matched": True}, "outcome": "success"}
    receipt = CutoverReceipt(raw)
    raw["verification"]["matched"] = False
    returned = receipt.as_record()
    returned["verification"]["matched"] = False
    assert receipt.as_record()["verification"]["matched"] is True

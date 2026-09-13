"""Wall-clock watchdog for the dedicated POSIX operator process, independent of driver progress."""

from __future__ import annotations

import signal
import threading
from types import FrameType
from typing import Any

from .errors import MigrationRefused


class DeadlineInterrupt(BaseException):
    """Interrupt the operator without being swallowed by adapter exception translation."""


class OperatorDeadline:
    """Own SIGALRM only in a dedicated main thread with no pre-existing alarm."""

    def __init__(self) -> None:
        self.previous: Any = None
        self.installed = False

    def __enter__(self) -> OperatorDeadline:
        if threading.current_thread() is not threading.main_thread():
            raise MigrationRefused("local cutover runs in a dedicated process main thread")
        if signal.getitimer(signal.ITIMER_REAL) != (0.0, 0.0):
            raise MigrationRefused("local cutover cannot share an existing process alarm")
        self.previous = signal.getsignal(signal.SIGALRM)
        signal.signal(signal.SIGALRM, self._interrupt)
        self.installed = True
        return self

    @staticmethod
    def _interrupt(_signum: int, _frame: FrameType | None) -> None:
        raise DeadlineInterrupt("local operator wall-clock deadline reached")

    def arm(self, milliseconds: int) -> None:
        if type(milliseconds) is not int or milliseconds < 1:
            raise MigrationRefused("operator deadline must be a positive integer in milliseconds")
        signal.setitimer(signal.ITIMER_REAL, milliseconds / 1000)

    def cancel(self) -> None:
        if self.installed:
            signal.setitimer(signal.ITIMER_REAL, 0)

    def __exit__(self, *_args: Any) -> None:
        self.cancel()
        signal.signal(signal.SIGALRM, self.previous)
        self.installed = False

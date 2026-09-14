"""Local operation and transaction ownership; no database or process-global client state."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
from threading import Lock
from typing import Concatenate, ParamSpec, Protocol, TypeVar

from .errors import ResourceBusy, ResourceClosed

_session_owner: ContextVar[object | None] = ContextVar("sde_session_owner", default=None)
_operation: ContextVar[object | None] = ContextVar("sde_operation", default=None)
_transaction: ContextVar[TransactionScope | None] = ContextVar("sde_transaction", default=None)


@dataclass(eq=False)
class TransactionScope:
    gate: UsageGate
    parent: TransactionScope | None
    session: object | None
    active: bool = True


def check_scope() -> None:
    scope = _transaction.get()
    while scope is not None:
        if not scope.active:
            raise ResourceClosed("the inherited transaction scope has already ended")
        scope = scope.parent


@contextmanager
def session_owner(owner: object) -> Iterator[None]:
    check_scope()
    logical = _session_scope.get()
    if logical is not None and logical.active and logical.usage.owner is not owner:
        raise ResourceBusy("another session owns the current transaction scope")
    scope = _transaction.get()
    if scope is not None and scope.session is not owner:
        raise ResourceBusy("another session owns the current transaction scope")
    marker = _session_owner.set(owner)
    try:
        yield
    finally:
        _session_owner.reset(marker)


class UsageGate:
    """Synchronize admission with transaction claims; reject foreign use before native I/O."""

    def __init__(self) -> None:
        self.lock = Lock()
        self.pid = os.getpid()
        self.operation_owner: object | None = None
        self.operation_depth = 0
        self.transaction_owner: TransactionScope | None = None

    def _process(self) -> None:
        if self.pid != os.getpid():
            raise ResourceClosed(
                "create a fresh adapter after fork; the inherited connection is not usable"
            )

    def _check_transaction(self) -> None:
        check_scope()
        owned = self.transaction_owner
        if owned is None:
            return
        scope = _transaction.get()
        while scope is not None and scope.gate is not self:
            scope = scope.parent
        if scope is not owned or owned.session is not _session_owner.get():
            raise ResourceBusy("the connection is owned by another active transaction scope")

    @contextmanager
    def operation(self) -> Iterator[None]:
        self._process()
        owner = _operation.get()
        marker = None
        if owner is None:
            owner = object()
            marker = _operation.set(owner)
        admitted = False
        failed = False
        try:
            with self.lock:
                self._check_transaction()
                if self.operation_owner is not None and self.operation_owner is not owner:
                    raise ResourceBusy("the connection already has an operation in progress")
                self.operation_owner = owner
                self.operation_depth += 1
                admitted = True
            try:
                yield
            except BaseException:
                failed = True
                raise
            finally:
                if admitted:
                    with self.lock:
                        self.operation_depth -= 1
                        if self.operation_depth == 0:
                            self.operation_owner = None
                if not failed:
                    check_scope()
        finally:
            if marker is not None:
                _operation.reset(marker)

    @contextmanager
    def transaction(self) -> Iterator[TransactionScope]:
        self._process()
        with self.lock:
            self._check_transaction()
            if self.operation_owner is not None:
                raise ResourceBusy("a transaction cannot begin while an operation is in progress")
            previous = self.transaction_owner
            scope = TransactionScope(self, _transaction.get(), _session_owner.get())
            self.transaction_owner = scope
        marker = _transaction.set(scope)
        try:
            yield scope
        finally:
            with self.lock:
                scope.active = False
                self.transaction_owner = previous
            _transaction.reset(marker)

    def idle(self) -> None:
        with self.lock:
            if self.operation_owner is not None:
                if self.transaction_owner is not None:
                    self.transaction_owner.active = False
                raise ResourceBusy("finish all operations before leaving the transaction")


class Guarded(Protocol):
    @property
    def _usage(self) -> UsageGate: ...


G = TypeVar("G", bound=Guarded)
P = ParamSpec("P")
R = TypeVar("R")


def guarded(method: Callable[Concatenate[G, P], R]) -> Callable[Concatenate[G, P], R]:
    @wraps(method)
    def call(self: G, /, *args: P.args, **kwargs: P.kwargs) -> R:
        with self._usage.operation():
            return method(self, *args, **kwargs)

    return call


@dataclass(eq=False)
class SessionScope:
    usage: SessionUsage
    group: str
    active: bool = True


_session_scope: ContextVar[SessionScope | None] = ContextVar("sde_session_scope", default=None)


class SessionUsage:
    def __init__(self, owner: object) -> None:
        self.owner = owner
        self.pid = os.getpid()
        self.lock = Lock()
        self.closed = False
        self.busy = False
        self.transaction_scope: SessionScope | None = None

    def check(self) -> None:
        if self.pid != os.getpid():
            raise ResourceClosed("create a fresh session and adapters after fork")
        check_scope()
        context = _session_scope.get()
        if self.closed or (context is not None and not context.active):
            raise ResourceClosed("the session or inherited transaction scope has ended")
        if self.transaction_scope is not None and context is not self.transaction_scope:
            raise ResourceBusy("the session belongs to another active transaction context")

    def group(self, name: str) -> None:
        from .errors import ModelPlanningError

        self.check()
        if self.transaction_scope is not None and self.transaction_scope.group != name:
            raise ModelPlanningError(
                "the operation is outside the active transaction's colocation group"
            )

    @contextmanager
    def operation(self) -> Iterator[None]:
        self.check()  # Refuse inherited locks before acquiring one in a child.
        with self.lock:
            self.check()
            if self.busy:
                raise ResourceBusy("the session already has an operation in progress")
            self.busy = True
        failed = False
        try:
            with session_owner(self.owner):
                try:
                    yield
                except BaseException:
                    failed = True
                    raise
                finally:
                    if not failed:
                        self.check()
        finally:
            with self.lock:
                self.busy = False

    @contextmanager
    def transaction(self, group: str) -> Iterator[None]:
        self.check()
        with self.lock:
            self.group(group)
            if self.busy:
                raise ResourceBusy("finish the session operation before beginning a transaction")
            previous = self.transaction_scope
            scope = SessionScope(self, group)
            self.transaction_scope = scope
        marker = _session_scope.set(scope)
        try:
            with session_owner(self.owner):
                yield
        finally:
            with self.lock:
                scope.active = False
                self.transaction_scope = previous
            _session_scope.reset(marker)

    def idle(self) -> None:
        with self.lock:
            if self.busy:
                if self.transaction_scope is not None:
                    self.transaction_scope.active = False
                raise ResourceBusy("finish all session operations before leaving the transaction")

    def seal(self) -> None:
        with self.lock:
            if self.transaction_scope is not None:
                self.transaction_scope.active = False

    def close(self) -> None:
        if self.pid != os.getpid():
            raise ResourceClosed(
                "create a fresh session after fork; do not close inherited adapters"
            )
        with self.lock:
            if self.busy or self.transaction_scope is not None:
                raise ResourceBusy(
                    "cannot close a session while its operation or transaction is active"
                )
            self.closed = True


class SessionGuarded(Protocol):
    @property
    def _session_usage(self) -> SessionUsage: ...


S = TypeVar("S", bound=SessionGuarded)


def session_call(method: Callable[Concatenate[S, P], R]) -> Callable[Concatenate[S, P], R]:
    @wraps(method)
    def call(self: S, /, *args: P.args, **kwargs: P.kwargs) -> R:
        with self._session_usage.operation():
            return method(self, *args, **kwargs)

    return call


def _after_fork() -> None:
    _session_owner.set(None)
    _operation.set(None)
    _transaction.set(None)
    _session_scope.set(None)


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork)


Reader = TypeVar("Reader")


def session_function(
    method: Callable[Concatenate[Reader, P], R],
) -> Callable[Concatenate[Reader, P], R]:
    """Keep free migration helpers inside a real Session lease; inspection contexts are separate."""

    @wraps(method)
    def call(reader: Reader, /, *args: P.args, **kwargs: P.kwargs) -> R:
        usage = getattr(reader, "_session_usage", None)
        if isinstance(usage, SessionUsage):
            usage.check()
            if usage.transaction_scope is not None:
                raise ResourceBusy("run migration helpers outside application transactions")
            with usage.operation():
                return method(reader, *args, **kwargs)
        return method(reader, *args, **kwargs)

    return call

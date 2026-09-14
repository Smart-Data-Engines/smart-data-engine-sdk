"""Private implementation of durable local executor files on a POSIX filesystem.

A local executor project has one writer at a time. The lock covers the read/decide/write
operation, not just the final syscall; a per-process reentrant mutex and flock cover threads
and independent processes.
All state files are published only after their complete contents have been fsynced. Immutable
files use link, so even a writer outside the lock cannot overwrite an existing artifact.

Readers opening a file see an old or new complete inode. Multi-file readers must hold transaction(),
as the local executor does for the entire operation. This is not a cross-host/NFS locking protocol.
"""

from __future__ import annotations

import errno
import fcntl
import os
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from threading import Lock, RLock
from typing import Concatenate, ParamSpec, Protocol, TypeVar


class DurabilityUncertain(OSError):
    """Publication happened, but its durability could not be confirmed. Inspect before retrying."""


class _LockState:
    def __init__(self) -> None:
        self.mutex = RLock()
        self.fd: int | None = None


_locks: dict[Path, _LockState] = {}
_guard = Lock()


def _after_fork() -> None:
    # Close inherited descriptions, without LOCK_UN (which would unlock the parent's flock).
    # A child must neither inherit a reentrancy decision nor keep a dead parent's lock alive.
    global _guard
    for _, state in sorted(_locks.items()):
        if state.fd is not None:
            os.close(state.fd)
    _locks.clear()
    _guard = Lock()


def _before_fork() -> None:
    # Fork must not fall between opening/closing a descriptor and updating its registration.
    _guard.acquire()


def _after_parent_fork() -> None:
    _guard.release()


os.register_at_fork(
    before=_before_fork, after_in_parent=_after_parent_fork, after_in_child=_after_fork
)


def sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def confirm_file(path: Path) -> None:
    """Confirm an already visible result before treating an uncertain publication as complete."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    sync_directory(path.parent)


def ensure_directory(path: Path) -> None:
    """Create parents and persist the directory entries, not only the files placed in them."""
    if path.is_dir():
        return
    ensure_directory(path.parent)
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        if not path.is_dir():
            raise
    sync_directory(path.parent)


@contextmanager
def transaction(directory: Path) -> Iterator[None]:
    root = directory.resolve()
    ensure_directory(root)
    with _guard:
        state = _locks.setdefault(root, _LockState())
    with state.mutex:
        if state.fd is not None:
            yield
            return
        with _guard:
            fd = os.open(root / ".state.lock", os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
            state.fd = fd
        owner_pid = os.getpid()
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            # Closing releases flock even after an exception. The lock file is never unlinked:
            # replacing it could give a new process a different inode from an existing waiter.
            if os.getpid() == owner_pid:
                with _guard:
                    state.fd = None
                    os.close(fd)


class _Store(Protocol):
    @property
    def root(self) -> Path: ...


S = TypeVar("S", bound=_Store)
P = ParamSpec("P")
R = TypeVar("R")


def serialized(method: Callable[Concatenate[S, P], R]) -> Callable[Concatenate[S, P], R]:
    @wraps(method)
    def wrapped(self: S, /, *args: P.args, **kwargs: P.kwargs) -> R:
        with transaction(self.root):
            return method(self, *args, **kwargs)

    return wrapped


def _write_all(fd: int, payload: bytes) -> None:
    remaining = memoryview(payload)
    while remaining:
        written = os.write(fd, remaining)
        if written == 0:
            raise OSError(errno.EIO, "state write made no progress")
        remaining = remaining[written:]


def write_bytes(path: Path, payload: bytes, *, replace: bool = True) -> None:
    """Publish a complete, durable file, with mode 0600 before any bytes are written.

    Before publication, failure leaves the prior file untouched (or the name absent). After
    publication an fsync failure is an explicitly uncertain result, never a claimed rollback.
    """
    with transaction(path.parent):
        ensure_directory(path.parent)
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary = Path(temporary_name)
        published = False
        try:
            try:
                _write_all(fd, payload)
                os.fsync(fd)
            finally:
                os.close(fd)
            if replace:
                os.replace(temporary, path)
            else:
                os.link(temporary, path)
            published = True
            sync_directory(path.parent)
        except OSError as exc:
            if published:
                raise DurabilityUncertain(
                    f"{path.name} was published, but directory durability could not be confirmed. "
                    "Inspect the stored state before retrying; the previous value was not restored."
                ) from exc
            raise
        finally:
            temporary.unlink(missing_ok=True)


def write_text(path: Path, text: str, *, replace: bool = True) -> None:
    write_bytes(path, text.encode("utf-8"), replace=replace)

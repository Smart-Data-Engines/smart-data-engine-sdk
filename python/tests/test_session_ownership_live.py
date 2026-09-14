"""No successful independent operation may disappear inside another session's rollback."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from typing import Any

import pytest
from test_runtime_privileges_live import runtime_roles

import sde
from sde.testing.loader import model_from_neutral


class Abort(Exception):
    pass


@pytest.fixture
def shared() -> Iterator[tuple[sde.Session, sde.Session, Any]]:
    model = model_from_neutral(
        {
            "entities": [
                {
                    "name": "Event",
                    "fields": [
                        {"name": "id", "type": "int64"},
                        {"name": "value", "type": "int32"},
                    ],
                    "key": ["id"],
                }
            ]
        }
    )
    placement = sde.load_map(
        {
            "contract": 3,
            "model_version": model.version,
            "map_version": 1,
            "groups": {
                "Event": {
                    "source": {
                        "id": "source",
                        "engine": "pg",
                        "layout": {
                            "tables": {"Event": "events"},
                            "columns": {"Event": {"id": "bigint", "value": "integer"}},
                        },
                    }
                }
            },
        },
        model=model,
    )
    with runtime_roles("postgres") as role:
        role.operator.ensure_schema(placement.groups["Event"].source.layout, keys={"Event": ["id"]})
        role.grant("events")
        yield (
            sde.Session(model, placement, {"pg": role.runtime}),
            sde.Session(model, placement, {"pg": role.runtime}),
            role,
        )


@pytest.mark.parametrize("same_session", [False, True])
def test_foreign_thread_cannot_join_a_shared_connections_transaction(
    shared: Any, same_session: bool
) -> None:
    first, other, role = shared
    other = first if same_session else other
    entered, release = threading.Event(), threading.Event()
    unexpected: list[str] = []

    def work() -> None:
        try:
            with first.transaction("Event"):
                first.save("Event", {"id": 1, "value": 11})
                entered.set()
                if not release.wait(5):
                    raise AssertionError("competing operation did not return promptly")
                raise Abort()
        except Abort:
            pass
        except BaseException as exc:
            unexpected.append(type(exc).__name__)

    worker = threading.Thread(target=work)
    worker.start()
    refused = False
    try:
        assert entered.wait(5)
        try:
            other.save("Event", {"id": 2, "value": 22})
        except sde.EngineError:
            refused = True
    finally:
        release.set()
        worker.join(timeout=5)
    assert not worker.is_alive() and not unexpected
    assert refused, "the independent write was acknowledged inside a foreign transaction"
    assert role.operator.get("events", {"id": 1}) is None
    other.save("Event", {"id": 2, "value": 22})
    assert role.operator.get("events", {"id": 2}) == {"id": 2, "value": 22}


def test_another_session_in_the_same_thread_cannot_inherit_transaction_ownership(
    shared: Any,
) -> None:
    first, other, role = shared
    refused = False
    with pytest.raises(Abort), first.transaction("Event"):
        first.save("Event", {"id": 1, "value": 11})
        try:
            other.save("Event", {"id": 2, "value": 22})
        except sde.EngineError:
            refused = True
        raise Abort()
    assert refused, "the other session inherited a transaction and could publish fan-out too early"
    assert role.operator.get("events", {"id": 2}) is None
    other.save("Event", {"id": 2, "value": 22})
    assert role.operator.get("events", {"id": 2}) is not None


def test_inner_success_never_commits_the_outer_transaction(shared: Any) -> None:
    session, _other, role = shared
    with pytest.raises(Abort), session.transaction("Event"):
        session.save("Event", {"id": 1, "value": 11})
        with session.transaction("Event"):
            session.save("Event", {"id": 2, "value": 22})
        raise Abort()
    assert role.operator.count("events") == 0
    assert role.runtime._cx.autocommit is True


def test_inner_rollback_keeps_outer_work_and_returns_an_idle_connection(shared: Any) -> None:
    session, _other, role = shared
    with session.transaction("Event"):
        session.save("Event", {"id": 1, "value": 11})
        with pytest.raises(Abort), session.transaction("Event"):
            session.save("Event", {"id": 2, "value": 22})
            raise Abort()
        session.save("Event", {"id": 3, "value": 33})
    assert role.operator.get("events", {"id": 1}) is not None
    assert role.operator.get("events", {"id": 2}) is None
    assert role.operator.get("events", {"id": 3}) is not None
    assert role.runtime._cx.autocommit is True


def test_base_exception_rolls_back_without_masking_it_or_leaving_transaction_open(
    shared: Any,
) -> None:
    session, _other, role = shared

    class Interrupted(BaseException):
        pass

    with pytest.raises(Interrupted), session.transaction("Event"):
        session.save("Event", {"id": 1, "value": 11})
        raise Interrupted()
    assert role.operator.count("events") == 0
    assert role.runtime._cx.autocommit is True
    session.save("Event", {"id": 2, "value": 22})
    assert role.operator.get("events", {"id": 2}) is not None


def test_catching_a_statement_error_cannot_make_an_aborted_transaction_succeed(shared: Any) -> None:
    session, _other, role = shared
    rejected = False
    try:
        with session.transaction("Event"):
            session.save("Event", {"id": 1, "value": 11})
            with pytest.raises(sde.EngineError):
                session.save("Event", {"id": 1, "value": 22})
    except sde.EngineError:
        rejected = True
    assert rejected, "COMMIT of an aborted native transaction was reported as success"
    assert role.operator.get("events", {"id": 1}) is None
    session.save("Event", {"id": 2, "value": 22})
    assert role.operator.get("events", {"id": 2}) is not None


def test_closed_session_refuses_data_and_migration_but_retains_borrowed_adapter(
    shared: Any,
) -> None:
    session, other, role = shared
    session.close()
    session.close()
    with pytest.raises(sde.ResourceClosed):
        session.save("Event", {"id": 1, "value": 11})
    with pytest.raises(sde.ResourceClosed):
        session.get("Event", {"id": 1})
    with pytest.raises(sde.ResourceClosed):
        sde.backfill(session, "Event")
    with pytest.raises(sde.ResourceClosed), session.transaction("Event"):
        pass
    other.save("Event", {"id": 2, "value": 22})
    assert role.operator.get("events", {"id": 2}) is not None


def test_session_context_manager_does_not_close_the_callers_connection(shared: Any) -> None:
    session, other, role = shared
    with session:
        session.save("Event", {"id": 1, "value": 11})
    with pytest.raises(sde.ResourceClosed):
        session.get("Event", {"id": 1})
    assert other.get("Event", {"id": 1}) == {"id": 1, "value": 11}
    assert not role.runtime._cx.closed


def test_an_async_task_cannot_escape_its_python_transaction(shared: Any) -> None:
    import asyncio

    session, _other, role = shared

    async def run() -> None:
        release = asyncio.Event()

        async def escaped() -> None:
            await release.wait()
            session.save("Event", {"id": 1, "value": 11})

        with session.transaction("Event"):
            task = asyncio.create_task(escaped())
            await asyncio.sleep(0)
        release.set()
        with pytest.raises(sde.ResourceClosed):
            await task
        session.save("Event", {"id": 2, "value": 22})

    asyncio.run(run())
    assert role.operator.get("events", {"id": 1}) is None
    assert role.operator.get("events", {"id": 2}) is not None


def test_a_forwarding_adapter_does_not_hide_the_native_transaction_owner(shared: Any) -> None:
    original, _other, role = shared

    class Proxy:
        def __getattr__(self, name: str) -> Any:
            return getattr(role.runtime, name)

    first = sde.Session(original.model, original.placement, {"pg": Proxy()})
    other = sde.Session(original.model, original.placement, {"pg": Proxy()})
    with first.transaction("Event"), pytest.raises(sde.ResourceBusy):
        other.save("Event", {"id": 1, "value": 11})
    other.save("Event", {"id": 2, "value": 22})
    assert role.operator.get("events", {"id": 2}) is not None


def test_forked_session_refuses_before_touching_an_inherited_lock(shared: Any) -> None:
    import os

    session, _other, role = shared
    read_fd, write_fd = os.pipe()
    session._session_usage.lock.acquire()
    try:
        pid = os.fork()
        if pid == 0:
            os.close(read_fd)
            try:
                session.save("Event", {"id": 1, "value": 11})
            except sde.ResourceClosed:
                os.write(write_fd, b"refused")
                os._exit(0)
            os._exit(1)
    finally:
        session._session_usage.lock.release()
    os.close(write_fd)
    reaped = False
    try:
        import select

        assert select.select([read_fd], [], [], 5)[0], "child blocked on a parent's inherited lock"
        assert os.read(read_fd, 100) == b"refused"
        status = os.waitpid(pid, 0)[1]
        reaped = True
        assert status == 0
    finally:
        os.close(read_fd)
        if not reaped:
            import signal
            from contextlib import suppress

            with suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
    session.save("Event", {"id": 2, "value": 22})
    assert role.operator.get("events", {"id": 2}) is not None


def test_lost_commit_response_is_uncertain_and_requires_explicit_reconnect(shared: Any) -> None:
    from contextlib import contextmanager

    session, _other, role = shared
    actual = role.runtime._conn

    class CommitResponseLost:
        def __getattr__(self, name: str) -> Any:
            return getattr(actual, name)

        @contextmanager
        def transaction(self) -> Iterator[Any]:
            with actual.transaction() as native:
                yield native
            raise OSError("controlled lost commit response")

    role.runtime._conn = CommitResponseLost()
    with pytest.raises(sde.EngineError, match="uncertain"), session.transaction("Event"):
        session.save("Event", {"id": 1, "value": 11})
    assert role.operator.get("events", {"id": 1}) == {"id": 1, "value": 11}
    with pytest.raises(sde.EngineError, match=r"close.*connect"):
        session.save("Event", {"id": 2, "value": 22})
    with pytest.raises(sde.EngineError, match=r"close.*connect"):
        role.runtime.connect()
    role.runtime.close()
    role.runtime.connect()
    session.save("Event", {"id": 2, "value": 22})
    assert role.operator.get("events", {"id": 2}) is not None


def test_begin_cannot_race_a_native_operation_already_waiting_on_the_server(shared: Any) -> None:
    import time

    first, other, role = shared
    pid = role.runtime._cx.info.backend_pid
    # A deliberately removed admission guard must fail this test, not wait forever behind
    # the fixture's table lock while the fixture waits for BEGIN to return.
    role.runtime._cx.execute("SET lock_timeout = '2s'")
    results: list[Any] = []

    def read() -> None:
        try:
            results.append(first.get("Event", {"id": 1}))
        except BaseException as exc:
            results.append(exc)

    worker = threading.Thread(target=read)
    refused = False
    try:
        with role.operator._cx.transaction():
            role.operator._cx.execute("LOCK TABLE events IN ACCESS EXCLUSIVE MODE")
            worker.start()
            for _ in range(200):
                row = role.operator._cx.execute(
                    "SELECT wait_event_type FROM pg_stat_activity WHERE pid=%s", [pid]
                ).fetchone()
                if row is not None and row[0] == "Lock":
                    break
                time.sleep(0.01)
            else:
                raise AssertionError("the native read did not reach the lock boundary")
            try:
                with other.transaction("Event"):
                    pass
            except sde.ResourceBusy:
                refused = True
    finally:
        worker.join(timeout=5)
    assert refused, "BEGIN was admitted while another call already owned the connection"
    assert not worker.is_alive() and results == [None]
    with other.transaction("Event"):
        other.save("Event", {"id": 2, "value": 22})
    assert role.operator.get("events", {"id": 2}) is not None

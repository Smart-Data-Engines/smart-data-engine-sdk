"""Owned session startup and cleanup keep failure semantics and borrowed resources distinct."""

from __future__ import annotations

from typing import Any

import pytest

import sde
from sde.testing.loader import model_from_neutral
from sde.testing.memory import MemoryEngine


class Engine(MemoryEngine):
    def __init__(self, *, fail_connect: bool = False, fail_close: bool = False) -> None:
        super().__init__()
        self.opened = self.closed = 0
        self.fail_connect, self.fail_close = fail_connect, fail_close

    def connect(self) -> None:
        self.opened += 1
        if self.fail_connect:
            raise RuntimeError("controlled connect failure")

    def close(self) -> None:
        self.closed += 1
        if self.fail_close:
            raise RuntimeError("controlled close failure")


@pytest.fixture
def model_map() -> tuple[Any, Any]:
    model = model_from_neutral(
        {
            "entities": [
                {"name": "Event", "fields": [{"name": "id", "type": "int64"}], "key": ["id"]}
            ]
        }
    )
    placement = sde.load_map(
        {
            "contract": 3,
            "model_version": model.version,
            "map_version": 1,
            "groups": {
                "Event": {"source": {"engine": "a", "id": "source", "layout": {"auto": True}}}
            },
        },
        model=model,
    )
    return model, placement


def test_owned_session_closes_every_created_adapter(model_map: Any) -> None:
    first, second = Engine(), Engine()
    with sde.Session.connect(*model_map, {"a": lambda: first, "b": lambda: second}) as session:
        assert (first.opened, second.opened) == (1, 1)
        session.save("Event", {"id": 1})
    assert (first.closed, second.closed) == (1, 1)
    session.close()
    assert (first.closed, second.closed) == (1, 1)
    with pytest.raises(sde.ResourceClosed):
        session.get("Event", {"id": 1})


def test_failed_second_connect_closes_it_and_the_already_opened_adapter(model_map: Any) -> None:
    first, second = Engine(), Engine(fail_connect=True)
    with pytest.raises(RuntimeError, match="connect failure"):
        sde.Session.connect(*model_map, {"a": lambda: first, "b": lambda: second})
    assert (first.closed, second.closed) == (1, 1)


def test_invalid_map_bindings_close_created_resources(model_map: Any) -> None:
    engine = Engine()
    with pytest.raises(sde.EngineError, match="not supplied"):
        sde.Session.connect(*model_map, {"missing": lambda: engine})
    assert engine.opened == engine.closed == 1


def test_duplicate_factory_resource_is_rejected_and_closed_once(model_map: Any) -> None:
    engine = Engine()
    with pytest.raises(sde.EngineError, match="distinct"):
        sde.Session.connect(*model_map, {"a": lambda: engine, "b": lambda: engine})
    assert engine.opened == engine.closed == 1


def test_one_failed_close_does_not_skip_other_resources_and_can_be_retried(model_map: Any) -> None:
    first, second = Engine(), Engine(fail_close=True)
    session = sde.Session.connect(*model_map, {"a": lambda: first, "b": lambda: second})
    with pytest.raises(RuntimeError, match="close failure"):
        session.close()
    assert first.closed == second.closed == 1
    second.fail_close = False
    session.close()
    assert first.closed == 1 and second.closed == 2


def test_cleanup_does_not_replace_the_startup_failure(model_map: Any) -> None:
    first, second = Engine(fail_close=True), Engine(fail_connect=True)
    with pytest.raises(RuntimeError, match="connect failure") as failed:
        sde.Session.connect(*model_map, {"a": lambda: first, "b": lambda: second})
    assert first.closed == second.closed == 1
    assert failed.value.__notes__


def test_cleanup_does_not_replace_an_exception_from_the_session_body(model_map: Any) -> None:
    engine = Engine(fail_close=True)
    with (
        pytest.raises(ValueError, match="body failure") as failed,
        sde.Session.connect(*model_map, {"a": lambda: engine}),
    ):
        raise ValueError("body failure")
    assert engine.closed == 1
    assert failed.value.__notes__


def test_concurrent_cleanup_is_refused_without_closing_an_adapter_twice(model_map: Any) -> None:
    import threading

    entered, release = threading.Event(), threading.Event()

    class SlowClose(Engine):
        def close(self) -> None:
            self.closed += 1
            entered.set()
            assert release.wait(5)

    engine = SlowClose()
    session = sde.Session.connect(*model_map, {"a": lambda: engine})
    failures = []

    def close() -> None:
        try:
            session.close()
        except BaseException as exc:
            failures.append(type(exc).__name__)

    worker = threading.Thread(target=close)
    worker.start()
    try:
        assert entered.wait(5)
        with pytest.raises(sde.ResourceBusy):
            session.close()
    finally:
        release.set()
        worker.join(timeout=5)
    assert not failures and not worker.is_alive()
    assert engine.closed == 1


def test_transaction_rejects_an_operation_outside_its_declared_group() -> None:
    model = model_from_neutral(
        {
            "entities": [
                {"name": name, "fields": [{"name": "id", "type": "int64"}], "key": ["id"]}
                for name in ("Event", "Audit")
            ]
        }
    )
    placement = sde.load_map(
        {
            "contract": 3,
            "model_version": model.version,
            "map_version": 1,
            "groups": {
                name: {"source": {"engine": "a", "id": name, "layout": {"auto": True}}}
                for name in ("Event", "Audit")
            },
        },
        model=model,
    )
    engine = Engine()
    session = sde.Session(model, placement, {"a": engine})
    with session.transaction("Event"):
        with pytest.raises(sde.ModelPlanningError, match="outside"):
            session.save("Audit", {"id": 1})
        session.save("Event", {"id": 2})
    assert engine.get("audit", {"id": 1}) is None
    assert engine.get("event", {"id": 2}) is not None

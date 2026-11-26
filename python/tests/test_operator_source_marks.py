"""The operator's write of a source's mark, without an engine: what it reads again before it
writes, and the cause it names when the engine does not take the mark (``mark-sources``)."""

from __future__ import annotations

from typing import Any

import pytest

import sde
from sde.engines._operator import NIL_UUID, MarkNotWritten, NativeOperator, mark_cause
from sde.ownership import source_marker

MARK = source_marker("1" * 32, "Event", "event")


def _operator(dialect: str, rows: list[Any], fails: BaseException | None = None) -> Any:
    """A NativeOperator whose statements go to ``rows`` and ``fails`` instead of an engine."""
    native: Any = object.__new__(NativeOperator)
    native.dialect = dialect
    native.quote = lambda name: f'"{name}"'
    native.sent = []
    native.rows = lambda sql, params=None, engine=None: rows

    def command(sql: str) -> None:
        native.sent.append(sql)
        if fails is not None:
            raise sde.EngineError(f"local operator command failed: {fails}") from fails

    native.command = command
    return native


def test_a_clickhouse_table_without_a_uuid_is_not_marked() -> None:
    """A database that is not Atomic gives its tables the nil uuid: a name is all there is to read
    again, and a table renamed in between would be marked, so nothing is written."""
    native = _operator("clickhouse", [(NIL_UUID, "db")])
    with pytest.raises(sde.MigrationRefused, match="not Atomic") as refused:
        native.write_source_mark("event", NIL_UUID, MARK)
    assert MARK in str(refused.value)
    assert native.sent == []


def test_another_object_under_the_name_is_not_marked() -> None:
    native = _operator("postgres", [("41", "public")])
    with pytest.raises(sde.MigrationRefused, match="changed while its mark was read"):
        native.write_source_mark("event", "40", MARK)
    assert native.sent == []


def test_the_object_read_again_is_marked() -> None:
    native = _operator("postgres", [("40", "public")])
    native.write_source_mark("event", "40", MARK)
    assert native.sent == [f"COMMENT ON TABLE \"public\".\"event\" IS '{MARK}'"]


class _PgError(Exception):
    def __init__(self, sqlstate: str) -> None:
        super().__init__("a driver's text that names host=db.internal password=secret")
        self.sqlstate = sqlstate


@pytest.mark.parametrize(
    ("error", "cause"),
    [
        (_PgError("42501"), "privilege"),
        (_PgError("42809"), "kind"),
        (_PgError("57P01"), None),
        (Exception("Code: 497. DB::Exception: user lacks ALTER"), "privilege"),
        (Exception("Code: 4970. DB::Exception: other"), None),
        (None, None),
    ],
)
def test_the_cause_of_a_mark_not_written_is_read_off_the_driver(
    error: BaseException | None, cause: str | None
) -> None:
    assert mark_cause(error) == cause


def test_a_mark_the_engine_refused_carries_its_cause() -> None:
    native = _operator("postgres", [("40", "public")], fails=_PgError("42501"))
    with pytest.raises(MarkNotWritten) as refused:
        native.write_source_mark("event", "40", MARK)
    assert refused.value.cause == "privilege"
    # Still an engine error for a cutover, which writes the same mark through the same code.
    assert isinstance(refused.value, sde.EngineError)


@pytest.mark.parametrize(("write_epoch", "unexamined"), [(None, []), (1, ["orderbook"])])
def test_mark_sources_reads_the_groups_a_session_reads(
    write_epoch: int | None, unexamined: list[str]
) -> None:
    """A session reads the marks of a group's tables only where its writes carry a generation, so
    a group without one - the orderbook engine's, which no operator is bound to - is not a source
    the operator failed to read; one with a generation on an engine it is not bound to is."""
    import os
    from contextlib import nullcontext
    from types import SimpleNamespace

    from sde.local_cutover import LocalCutover

    executor: Any = object.__new__(LocalCutover)
    executor._pid = os.getpid()
    executor.native = {}
    executor.project_id = "1" * 32
    executor.store = SimpleNamespace(
        lock=nullcontext, read=lambda: {"execution": None, "stages": {}, "completed": {}}
    )
    source = SimpleNamespace(engine="orderbook", layout=SimpleNamespace(tables={"Book": "book"}))
    executor.active_map = lambda: SimpleNamespace(
        groups={"Book": SimpleNamespace(write_epoch=write_epoch, source=source)}
    )
    assert executor.mark_sources() == {"marked": [], "left": [], "unexamined": unexamined}


def test_an_engine_that_does_not_answer_leaves_the_source_and_keeps_its_text_out() -> None:
    """The driver's text can carry what a connection is: a source the engine did not answer for is
    left with a fixed reason, and the next table is read."""
    import os
    from contextlib import nullcontext
    from types import SimpleNamespace

    from sde.local_cutover import LocalCutover

    def rows(*_: Any, **__: Any) -> list[Any]:
        raise sde.EngineError("local operator metadata query failed: host=db.internal password=x")

    executor: Any = object.__new__(LocalCutover)
    executor._pid = os.getpid()
    executor.native = {
        "pg": SimpleNamespace(dialect="postgres", engine=None, quote=lambda n: n, rows=rows)
    }
    executor.project_id = "1" * 32
    executor.store = SimpleNamespace(
        lock=nullcontext, read=lambda: {"execution": None, "stages": {}, "completed": {}}
    )
    source = SimpleNamespace(engine="pg", layout=SimpleNamespace(tables={"A": "a", "B": "b"}))
    executor.active_map = lambda: SimpleNamespace(
        groups={"G": SimpleNamespace(write_epoch=1, source=source)}
    )
    outcome = executor.mark_sources()
    assert outcome["marked"] == [] and outcome["unexamined"] == []
    assert [entry["table"] for entry in outcome["left"]] == ["a", "b"]
    for entry in outcome["left"]:
        assert "did not answer" in entry["reason"] and "password" not in entry["reason"]

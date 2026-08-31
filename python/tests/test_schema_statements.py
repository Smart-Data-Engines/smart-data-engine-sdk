"""DDL as a value: the statements, without a database anywhere.

This file exists because of what the control plane needs. "Here is the schema we chose for you" is
one of the things it hands a client, and the only honest way to produce that text is to ask the
library that would apply it. Rebuilding the DDL at the other end would be a second implementation of
the type mapping, the quoting and the key handling - and two implementations of one thing is the
failure the byte contract exists to prevent, made worse by both sides having tests and neither
comparing against the other.

The last test in this file is the one that matters. Rendering the statements is useless if
`ensure_schema` runs something else, and nothing about the two being in the same repository makes
them agree.
"""

from __future__ import annotations

import datetime as dt
import decimal
import subprocess
import sys
import uuid
from typing import Annotated

import pytest

import sde
from sde.errors import EngineError
from sde.schema import schema_statements


def _model() -> sde.LogicalModel:
    sde.clear_registry()

    @sde.entity
    class Reading:
        station: uuid.UUID
        at: dt.datetime
        celsius: Annotated[decimal.Decimal, sde.precision(12, 2)]
        note: str

        class Meta:
            key = ["station", "at"]

    return sde.build_model(Reading)


def _layout(dialect: str) -> sde.PhysicalLayout:
    model = _model()
    (group,) = sde.colocation_groups(model)
    return sde.default_layout(model, group, dialect=dialect)


def test_postgres_ddl_has_a_primary_key_and_ansi_quoting() -> None:
    statements = schema_statements(
        _layout("postgres"), keys={"Reading": ("station", "at")}, dialect="postgres"
    )
    create = next(s for s in statements if "CREATE TABLE" in s)
    assert '"reading"' in create
    assert 'PRIMARY KEY ("station", "at")' in create
    assert "numeric(12,2)" in create


def test_clickhouse_ddl_orders_by_the_declared_key_in_declared_order() -> None:
    """The order is positional and decides which prefixes can prune granules.

    Sorting it would change the physical performance of the table while leaving the map looking
    identical - which is why the key travels as a sequence and not as a set.
    """
    statements = schema_statements(
        _layout("clickhouse"), keys={"Reading": ("station", "at")}, dialect="clickhouse"
    )
    create = next(s for s in statements if "CREATE TABLE" in s)
    assert "`reading`" in create
    assert "ENGINE = ReplacingMergeTree ORDER BY (`station`, `at`)" in create

    reversed_key = schema_statements(
        _layout("clickhouse"), keys={"Reading": ("at", "station")}, dialect="clickhouse"
    )
    assert reversed_key != statements, "the key order does not reach the statement"


def test_a_key_column_the_layout_does_not_have_is_refused_for_clickhouse() -> None:
    with pytest.raises(EngineError, match="names columns the layout does not have"):
        schema_statements(
            _layout("clickhouse"), keys={"Reading": ("station", "missing")}, dialect="clickhouse"
        )


def test_an_unknown_dialect_is_refused_rather_than_falling_back_to_ansi() -> None:
    with pytest.raises(EngineError, match="no DDL for dialect"):
        schema_statements(_layout("postgres"), keys={"Reading": ("station",)}, dialect="mysql")


def test_a_table_with_no_key_is_refused() -> None:
    with pytest.raises(EngineError, match="a table without one cannot be addressed"):
        schema_statements(_layout("postgres"), keys={}, dialect="postgres")


def test_rendering_ddl_does_not_import_a_driver() -> None:
    """The reason this module is not inside ``sde.engines``.

    That package is the part of the library that opens connections, and the control plane's import
    allowlist refuses it by name for exactly that reason. Rendering DDL needs a driver about as much
    as printing a receipt needs a bank - and a module that pulls one in anyway makes the boundary
    test a matter of which import happens to come first.

    Run in a subprocess because this process has already imported plenty.
    """
    source = (
        "import sys; import sde.schema; "
        "loaded = [m for m in sys.modules if m.split('.')[0] in "
        "('psycopg', 'clickhouse_connect', 'clickhouse_driver')]; "
        "print(loaded)"
    )
    out = subprocess.run(
        [sys.executable, "-c", source], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert out == "[]", f"importing sde.schema pulled in {out}"


def test_the_rendered_statements_are_the_statements_that_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rendering is useless if ``ensure_schema`` applies something else.

    Nothing about the two living in one repository makes them agree, and a report showing DDL the
    engine never ran is worse than no report: the client reads it, checks their database, and finds
    a different schema.

    Proven by substitution rather than by inspection. ``schema_statements`` is replaced with one
    that returns a statement nobody would write by accident, and the adapter has to fail on exactly
    that statement - which it can only do if it ran what the function returned.
    """
    import sde.engines.postgres as adapter

    sentinel = "CREATE TABLE proof_of_delegation ((("

    def fake(*args: object, **kwargs: object) -> tuple[str, ...]:
        return (sentinel,)

    monkeypatch.setattr(adapter, "schema_statements", fake)

    engine = adapter.PostgresEngine.__new__(adapter.PostgresEngine)

    class Cursor:
        def __enter__(self) -> Cursor:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def execute(self, statement: str) -> None:
            raise RuntimeError(f"executed: {statement}")

    class Connection:
        def cursor(self) -> Cursor:
            return Cursor()

    monkeypatch.setattr(type(engine), "_cx", property(lambda self: Connection()))

    with pytest.raises(EngineError, match=r"proof_of_delegation"):
        engine.ensure_schema(_layout("postgres"), keys={"Reading": ("station",)})

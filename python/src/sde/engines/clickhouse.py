"""ClickHouse adapter.

The second engine, and the reason there is a product at all: between PostgreSQL and ClickHouse lies
the decision clients get wrong once, at the start, and never revisit. With one adapter the planner
chooses from a set of size one.

Three things here are genuinely different from the PostgreSQL adapter, and none of them is a detail.
Each is a place where "the same operation" means something else because the engine is different, and
the product's position is that such a difference must be *stated* rather than smoothed over.

**A naive datetime is UTC. Always, explicitly, here.**

A fix for a divergence that was measured rather than imagined. `datetime(2026, 8, 27, 12, 0)` with
no tzinfo, written through this library:

- into PostgreSQL `timestamptz`, it arrives as ``12:00:00+00:00``
- into ClickHouse ``DateTime64(3, 'UTC')`` with the driver left to its own devices, it arrived as
  ``10:00:00`` - because `clickhouse-connect` reads a naive datetime as *local* time and converts

Two hours apart, from one call, in one library, with no error anywhere. After a migration from one
engine to the other every timestamp in the client's analytics would shift by the offset of whichever
machine happened to write the row. So naive datetimes are given `timezone.utc` before they reach the
driver, which is what the PostgreSQL path already does in effect. If you mean a different zone, pass
an aware datetime and it is respected.

**There are no transactions, and this adapter says so instead of pretending.**

`transaction()` raises. Not a no-op context manager: a caller who believes they are in a transaction
and is not has been lied to at the worst possible moment. A client who needs two entities to commit
together declares that, and the planner then places them in the same group and therefore the same
engine - the requirement becomes a placement constraint. This method existing and refusing is how
that constraint is discovered at the point of use rather than inferred from a rollback that never
happened.

**Keys are not enforced, so the table is a `ReplacingMergeTree` and point reads use `FINAL`.**

A MergeTree does not enforce uniqueness on its `ORDER BY`. So a second `save()` of the same key does
not raise the way PostgreSQL's primary key does - and that divergence cannot be removed, only
chosen.
Plain `MergeTree` would leave two rows and make every aggregate over that group quietly wrong;
`ReplacingMergeTree` keeps the newest and collapses the rest at merge time. Of the two, "the newest
row for a key wins" is something a client can reason about. `FINAL` on point reads and counts is
what makes the collapse visible immediately rather than eventually, and it is not free - which is
the trade, stated.

The residue is worth naming plainly, because a client will meet it: in PostgreSQL, saving the same
key
twice is an error; here it is an overwrite. That belongs in a placement decision rather than in an
adapter, and there is no declaration for it yet.
"""

from __future__ import annotations

import datetime as _dt
from collections.abc import Iterator, Mapping, Sequence
from typing import Any

from ..errors import EngineError
from ..logging import log
from ..placement import PhysicalLayout
from ..schema import QUOTE, schema_statements

__all__ = ["ClickHouseEngine"]


# Bound from the one definition in sde.schema, so that DDL and DML cannot disagree about
# how an identifier is escaped.
_quote = QUOTE["clickhouse"]


def _as_utc(value: Any) -> Any:
    """A naive datetime is UTC. See the module docstring for the measurement behind this."""
    if isinstance(value, _dt.datetime) and value.tzinfo is None:
        return value.replace(tzinfo=_dt.UTC)
    return value


def _row(names: Sequence[str], types: Sequence[Any], row: Sequence[Any]) -> dict[str, Any]:
    """One result row as a dict, with the timezone put back on values that had one in the schema.

    The write side of this module makes a naive datetime UTC. Without this, the read side would not
    give it back: `clickhouse-connect` returns a *naive* datetime for a `DateTime64(3, 'UTC')`
    column, while psycopg returns an aware one for a `timestamptz`. So the same entity read from
    the two engines produces two datetimes that Python refuses to compare -
    ``can't compare offset-naive and offset-aware datetimes`` - which is a `TypeError` in a client's
    code that appears on the day a group is moved and not before.

    The column type carries the zone, so it is read from there rather than assumed: a column
    declared with a timezone yields aware values, one declared without yields naive, which is
    exactly the distinction the neutral vocabulary makes between `timestamptz` and `timestamp`.
    """
    out: dict[str, Any] = {}
    for name, column_type, value in zip(names, types, row, strict=True):
        zone = getattr(column_type, "tzinfo", None)
        if zone is not None and isinstance(value, _dt.datetime) and value.tzinfo is None:
            value = value.replace(tzinfo=zone)
        out[name] = value
    return out


class ClickHouseEngine:
    """A thin adapter over clickhouse-connect. Executes decisions, makes none."""

    dialect = "clickhouse"

    def __init__(self, dsn: str) -> None:
        try:
            import clickhouse_connect
        except ImportError as exc:  # pragma: no cover - depends on the install extra
            raise EngineError(
                "the ClickHouse adapter needs the 'clickhouse' extra: "
                "pip install 'smart-data-engine[clickhouse]'. "
                "The core library has no dependencies, because it goes into your application and "
                "every dependency here would be one you inherit."
            ) from exc
        self._module = clickhouse_connect
        self._dsn = dsn
        self._client: Any = None

    # --- connection ------------------------------------------------------------------------

    def connect(self) -> None:
        if self._client is None:
            try:
                self._client = self._module.get_client(dsn=self._dsn)
            except Exception as exc:
                raise EngineError(f"could not connect to ClickHouse: {exc}") from exc

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self) -> ClickHouseEngine:
        self.connect()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @property
    def _cx(self) -> Any:
        if self._client is None:
            raise EngineError("not connected; call connect() first")
        return self._client

    # --- schema ----------------------------------------------------------------------------

    def ensure_schema(self, layout: PhysicalLayout, *, keys: Mapping[str, Sequence[str]]) -> None:
        """Create what is missing, change nothing that exists.

        `ORDER BY` is the declared key, in declared order. That order is positional and carries
        meaning: it decides which prefixes of the key can prune granules, so sorting it would change
        the physical performance of the table while leaving the map looking identical.

        No indexes are created. `layout.indexes` is empty for this dialect by construction - see
        `sde.layout` - because ClickHouse's `CREATE INDEX` builds a data-skipping index that needs a
        type and a granularity, and choosing those is a planner decision with a cost attached.
        """
        statements = schema_statements(layout, keys=keys, dialect=self.dialect)

        for statement in statements:
            try:
                self._cx.command(statement)
            except Exception as exc:
                raise EngineError(f"schema statement failed: {statement}: {exc}") from exc
        log("sde.schema.applied", engine=self.dialect, statements=len(statements))

    # --- data ------------------------------------------------------------------------------

    def insert(self, table: str, values: Mapping[str, Any]) -> None:
        if not values:
            raise EngineError("nothing to insert")
        cols = sorted(values)
        row = [_as_utc(values[c]) for c in cols]
        try:
            self._cx.insert(table, [row], column_names=cols)
        except Exception as exc:
            # Surfaced, not swallowed and not rerouted, as in the PostgreSQL adapter. A write that
            # did not happen is not our internal problem to absorb.
            log("sde.write.failed", table=table, error=type(exc).__name__)
            raise EngineError(f"insert into {table} failed: {exc}") from exc

    def get(self, table: str, key: Mapping[str, Any]) -> dict[str, Any] | None:
        """One row by key, with `FINAL` so a superseded row is never returned.

        Without `FINAL` a key that has been saved twice returns whichever duplicate the scan reaches
        first until a merge happens - which is to say, nondeterministically the old value. Paying
        for `FINAL` on a point read is the cheaper half of that trade.
        """
        columns = sorted(key)
        where = " AND ".join(f"{_quote(c)} = %({c})s" for c in columns)
        sql = f"SELECT * FROM {_quote(table)} FINAL WHERE {where} LIMIT 1"
        try:
            result = self._cx.query(sql, parameters={c: _as_utc(key[c]) for c in columns})
        except Exception as exc:
            raise EngineError(f"select from {table} failed: {exc}") from exc
        if not result.result_rows:
            return None
        return _row(result.column_names, result.column_types, result.result_rows[0])

    def range(
        self,
        table: str,
        column: str,
        *,
        low: Any = None,
        high: Any = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        parameters: dict[str, Any] = {}
        if low is not None:
            clauses.append(f"{_quote(column)} >= %(low)s")
            parameters["low"] = _as_utc(low)
        if high is not None:
            clauses.append(f"{_quote(column)} < %(high)s")
            parameters["high"] = _as_utc(high)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        cap = ""
        if limit is not None:
            cap = " LIMIT %(limit)s"
            parameters["limit"] = int(limit)
        sql = f"SELECT * FROM {_quote(table)} FINAL{where} ORDER BY {_quote(column)}{cap}"
        try:
            result = self._cx.query(sql, parameters=parameters)
        except Exception as exc:
            raise EngineError(f"range select from {table} failed: {exc}") from exc
        return [
            _row(result.column_names, result.column_types, row)
            for row in result.result_rows
        ]

    def count(self, table: str) -> int:
        """`FINAL` here too, so this counts entities rather than stored rows.

        The two differ between a save and the next merge. A count that drifts and then settles is
        worse than a slower count, because it makes a test flaky and a dashboard untrustworthy in
        the same way.
        """
        try:
            result = self._cx.query(f"SELECT count() FROM {_quote(table)} FINAL")
        except Exception as exc:
            raise EngineError(f"count on {table} failed: {exc}") from exc
        return int(result.result_rows[0][0]) if result.result_rows else 0

    # --- transactions ----------------------------------------------------------------------

    def transaction(self) -> Iterator[ClickHouseEngine]:
        """Refuses. There is no transaction here to give you.

        A no-op context manager would be the friendlier signature and the worse library: the caller
        would believe a group of writes was atomic, and would find out otherwise from the state of
        the data rather than from an exception.

        The way out is not a flag. Declare the atomicity - `atomic_with` on the entity - and the
        planner is then obliged to place those entities in one group, and one group is one engine,
        so it will not be this one.

        Not decorated with `@contextmanager`, unlike the PostgreSQL one. A decorated generator would
        need a `yield` after the `raise` to keep the type honest, and that statement is unreachable
        - `mypy --strict` says so, correctly. A plain method that raises fails at the call, which is
        one frame earlier and reads better in a traceback.
        """
        raise EngineError(
            "ClickHouse has no multi-statement transactions, so this adapter will not pretend to "
            "start one. If these writes have to commit together, declare it: `atomic_with` on the "
            "entities makes them one colocation group, one group is one engine, and the planner is "
            "then not permitted to put them here. A silent no-op context manager would let the "
            "writes proceed and let you believe they were atomic."
        )

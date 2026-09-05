"""ClickHouse adapter.

The second engine, and the reason there is a product at all: between PostgreSQL and ClickHouse
lies the decision clients get wrong once, at the start, and never revisit. With one adapter the
planner chooses from a set of size one.

Three things here are genuinely different from the PostgreSQL adapter, and none of them is a
detail. Each is a place where "the same operation" means something else because the engine is
different, and the product's position is that such a difference must be *stated* rather than
smoothed over.

**A naive datetime is UTC. Always, explicitly, here.**

A fix for a divergence that was measured rather than imagined. `datetime(2026, 8, 27, 12, 0)` with
no tzinfo, written through this library:

- into PostgreSQL `timestamptz`, it arrives as ``12:00:00+00:00`` - into ClickHouse
``DateTime64(3, 'UTC')`` with the driver left to its own devices, it arrived as
  ``10:00:00`` - because `clickhouse-connect` reads a naive datetime as *local* time and converts

Two hours apart, from one call, in one library, with no error anywhere. After a migration from one
engine to the other every timestamp in the client's analytics would shift by the offset of
whichever machine happened to write the row. So naive datetimes are given `timezone.utc` before
they reach the driver, which is what the PostgreSQL path already does in effect. If you mean a
different zone, pass an aware datetime and it is respected.

**There are no transactions, and this adapter says so instead of pretending.**

`transaction()` raises. Not a no-op context manager: a caller who believes they are in a
transaction and is not has been lied to at the worst possible moment. A client who needs two
entities to commit together declares that, and the planner then places them in the same group and
therefore the same engine - the requirement becomes a placement constraint. This method existing
and refusing is how that constraint is discovered at the point of use rather than inferred from a
rollback that never happened.

**Keys are not enforced, so the table is a `ReplacingMergeTree` and point reads use `FINAL`.**

A MergeTree does not enforce uniqueness on its `ORDER BY`. So a second `save()` of the same key
does not raise the way PostgreSQL's primary key does - and that divergence cannot be removed, only
chosen. Plain `MergeTree` would leave two rows and make every aggregate over that group quietly
wrong; `ReplacingMergeTree` keeps the newest and collapses the rest at merge time. Of the two,
"the newest row for a key wins" is something a client can reason about. `FINAL` on point reads and
counts is what makes the collapse visible immediately rather than eventually, and it is not free -
which is the trade, stated.

The residue is worth naming plainly, because a client will meet it: in PostgreSQL, saving the same
key twice is an error; here it is an overwrite. That belongs in a placement decision rather than
in an adapter, and there is no declaration for it yet.
"""

from __future__ import annotations

import datetime as _dt
from collections.abc import Iterator, Mapping, Sequence
from typing import Any

from ..errors import EngineError
from ..explain import (
    Cost,
    QueryPlan,
    QueryPlanRefused,
    replacing_merge_tree_finding,
)
from ..logging import log
from ..migration import key_columns, same_width
from ..placement import BACKFILL_TABLE, WATERMARK_TABLE, PhysicalLayout
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

    The write side of this module makes a naive datetime UTC. Without this, the read side would
    not give it back: `clickhouse-connect` returns a *naive* datetime for a `DateTime64(3, 'UTC')`
    column, while psycopg returns an aware one for a `timestamptz`. So the same entity read from
    the two engines produces two datetimes that Python refuses to compare - ``can't compare
    offset-naive and offset-aware datetimes`` - which is a `TypeError` in a client's code that
    appears on the day a group is moved and not before.

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
        meaning: it decides which prefixes of the key can prune granules, so sorting it would
        change the physical performance of the table while leaving the map looking identical.

        No indexes are created. `layout.indexes` is empty for this dialect by construction - see
        `sde.layout` - because ClickHouse's `CREATE INDEX` builds a data-skipping index that needs
        a type and a granularity, and choosing those is a planner decision with a cost attached.
        """
        statements = schema_statements(layout, keys=keys, dialect=self.dialect)

        for statement in statements:
            try:
                self._cx.command(statement)
            except Exception as exc:
                raise EngineError(f"schema statement failed: {statement}: {exc}") from exc
        log("sde.schema.applied", engine=self.dialect, statements=len(statements))
        self._verify_schema(layout)

    def _verify_schema(self, layout: PhysicalLayout) -> None:
        """The same check as the PostgreSQL adapter, for the same reason.

        `CREATE TABLE IF NOT EXISTS` keeps whatever table is already there, so a leftover from an
        older map is accepted in silence and the first insert fails naming a column rather than
        the cause. Missing columns are refused, extra ones are logged: a client may have added one
        outside SDE and the map has no opinion about it.
        """
        expected = {
            table: set(layout.columns.get(entity, {}))
            for entity, table in sorted(layout.tables.items())
        }
        if not expected:
            return

        result = self._cx.query(
            "SELECT table, name FROM system.columns "
            "WHERE database = currentDatabase() AND table IN %(tables)s",
            parameters={"tables": sorted(expected)},
        )
        found: dict[str, set[str]] = {}
        for table_name, column_name in result.result_rows:
            found.setdefault(str(table_name), set()).add(str(column_name))

        for table, columns in sorted(expected.items()):
            actual = found.get(table)
            if actual is None:
                raise EngineError(
                    f"{table!r} does not exist after applying the schema. The statement reported "
                    f"success, so this is a permissions or database-selection problem rather "
                    f"than a bad map."
                )
            missing = sorted(columns - actual)
            if missing:
                raise EngineError(
                    f"{table!r} already existed with a different shape: the map needs "
                    f"{missing} and the table has {sorted(actual)}. `CREATE TABLE IF NOT "
                    f"EXISTS` keeps whatever "
                    f"is there, so this table came from somewhere else. Refusing here rather than "
                    f"at the first insert, which would fail in your request path with an error "
                    f"naming a column and not the cause."
                )
            extra = sorted(actual - columns)
            if extra:
                log("sde.schema.extra_columns", table=table, columns=extra)

    # --- data ------------------------------------------------------------------------------

    def explain_plan(self, sql: str) -> QueryPlan:
        """Plan an analyst's query without running it, with ``readonly=1`` on every statement.

        Requirement 19.4. Four questions to the server, all under ``readonly=1`` - which ClickHouse
        refuses a mutation under (code 164) and which leaves the rest working, both halves
        measured, because a write protection that also blocked the measurement would be one nobody
        would keep switched on.

        The plan tree, ``EXPLAIN ESTIMATE`` for the numbers, ``EXPLAIN QUERY TREE`` for the table
        names, and ``system.tables`` for each table's engine.

        **The third question exists because of a measurement that broke the obvious design.** The
        table names came from ``EXPLAIN ESTIMATE`` first, and for ``SELECT count() FROM t`` that
        returns **no rows at all** - ClickHouse answers a trivial count from part metadata and
        reads nothing, so there is no table in the estimate. That is precisely the query where
        9.7's hazard is worst: a count over a ``ReplacingMergeTree`` without ``FINAL`` returns the
        uncollapsed number. So the one query that most needs the warning was the one the estimate
        could not see. ``EXPLAIN QUERY TREE`` names every table in the join tree whatever the read
        optimisation does.
        """
        settings = {"readonly": 1}
        # Read it back. Same reason as the PostgreSQL side: without this, dropping the setting
        # changes nothing observable - nothing EXPLAIN does would write - so the guard would be
        # unverifiable and its removal silent.
        confirmed = self._cx.query("SELECT getSetting('readonly')", settings=settings)
        answer = confirmed.result_rows[0][0] if confirmed.result_rows else None
        if int(answer or 0) < 1:
            raise EngineError(
                f"this server would not accept readonly=1 for planning: getSetting('readonly') "
                f"came back {answer!r}. Refusing to plan anything: requirement 19.2 keeps this "
                f"product out of the data path, and readonly=1 is the whole of what stops a "
                f"statement here from writing - measured, it refuses a mutation with code 164 and "
                f"still answers EXPLAIN ESTIMATE."
            )
        try:
            tree = self._cx.query(f"EXPLAIN {sql}", settings=settings)
            plan = tuple(str(row[0]) for row in tree.result_rows)
            estimate = self._cx.query(f"EXPLAIN ESTIMATE {sql}", settings=settings)
        except Exception as exc:
            raise QueryPlanRefused(
                f"{self.dialect} would not plan this query: {exc} Nothing was executed. This is "
                f"what validating against a live engine means: the control plane checked the query "
                f"against the schema it authored, and this is the schema that exists."
            ) from exc

        columns = list(estimate.column_names)
        rows = [dict(zip(columns, row, strict=False)) for row in estimate.result_rows]
        values: dict[str, str] = {}
        for name in ("parts", "rows", "marks"):
            if name in columns:
                values[name] = str(sum(int(row.get(name) or 0) for row in rows))
        values["tables"] = str(len(rows))

        named = self._tables_in(sql, settings=settings) or [
            # The estimate's own names, as a fallback for a server whose analyzer does not answer
            # EXPLAIN QUERY TREE. Both parts come from the answer rather than from this
            # connection's default database: a query can read a table elsewhere, and the default
            # would name the wrong one quietly, because system.tables would simply find nothing.
            (str(row.get("table", "")), str(row.get("database", "")))
            for row in rows
            if row.get("table") and row.get("database")
        ]
        engines: list[tuple[str, str]] = []
        for table, database in named:
            try:
                answer = self._cx.query(
                    "SELECT engine FROM system.tables WHERE database = {db:String} "
                    "AND name = {tb:String}",
                    parameters={"db": database, "tb": table},
                    settings=settings,
                )
            except Exception:  # pragma: no cover - a client without system.tables access
                # Not fatal and not silent: the plan is still worth having, and the finding this
                # would have produced is a property of the table rather than of the query, so its
                # absence costs a warning rather than a guarantee.
                continue
            for row in answer.result_rows:
                engines.append((table, str(row[0])))

        return QueryPlan(
            engine=self.dialect,
            dialect=self.dialect,
            plan=plan or ("(the engine returned an empty plan)",),
            cost=Cost(
                units="parts, rows and marks to read",
                basis=(
                    "real counts rather than arbitrary units, and coarse below one granule: a "
                    "mark covers 8192 rows by default, so a small table reports every row and one "
                    "mark whether or not the key filter prunes anything - measured on a "
                    "thousand-row table. The figures are what the primary key can rule out before "
                    "reading, so they move when the query filters on a key prefix and not when it "
                    "filters on anything else. Zero across the board means the engine reads "
                    "nothing at all: a trivial count() is answered from part metadata, and the "
                    "estimate correctly reports no rows read - which is not the same as a query "
                    "that returns nothing."
                ),
                values=values,
            ),
            findings=replacing_merge_tree_finding(engines),
            read_only_enforced=(
                "every statement sent with readonly=1. ClickHouse refuses a mutation under it "
                "(code 164, READONLY) and still answers EXPLAIN ESTIMATE - both measured. There "
                "is no transaction here to roll back, which is the same absence transaction() "
                "refuses rather than pretends about."
            ),
        )

    def _tables_in(self, sql: str, *, settings: Mapping[str, Any]) -> list[tuple[str, str]]:
        """Every table the query reads, from the analyzer's own tree. ``(table, database)`` pairs.

        A regex over ``EXPLAIN QUERY TREE`` output, which is the engine's own rendering and could
        change between versions. The cost of that is a **missing** warning rather than a wrong one:
        the finding this feeds is a property of a table, so losing it costs an analyst a sentence
        and costs no guarantee. Worth having anyway, because the alternative loses it for the one
        query where it matters most - see :meth:`explain_plan`.
        """
        import re

        try:
            tree = self._cx.query(f"EXPLAIN QUERY TREE {sql}", settings=dict(settings))
        except Exception as exc:
            # No pragma here any more, and that is the point. The marker that used to say "no test
            # comes here" sat one line above a log() call whose event name was missing from the
            # vocabulary, and log() raised on an unknown name - so the graceful path was the one
            # that crashed, in the client's process, on an older server.
            log("sde.explain.no_query_tree", reason=str(exc)[:120])
            return []
        found: list[tuple[str, str]] = []
        for row in tree.result_rows:
            for match in re.finditer(r"table_name:\s*(\S+)", str(row[0])):
                qualified = match.group(1)
                database, _, table = qualified.rpartition(".")
                if table and (table, database) not in found:
                    found.append((table, database))
        return found

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

        Without `FINAL` a key that has been saved twice returns whichever duplicate the scan
        reaches first until a merge happens - which is to say, nondeterministically the old value.
        Paying for `FINAL` on a point read is the cheaper half of that trade.
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

    # --- rollback protection ------------------------------------------------------------------
    #
    # Append-only and `max()`, which is what makes this identical in both engines: no key to
    # enforce, no row to update, nothing for this engine's lack of a unique constraint to spoil.

    def map_watermark(self) -> int | None:
        """The highest map version applied against this engine, creating the table if missing.

        A plain `MergeTree` is right here, and it is the one place in this adapter where that is
        true without qualification: the table is append-only by design and the answer is an
        aggregate, so there are no duplicates to collapse and no reason to pay for `FINAL`.
        """
        try:
            self._cx.command(
                f"CREATE TABLE IF NOT EXISTS {_quote(WATERMARK_TABLE)} ("
                f"{_quote('map_version')} Int64, "
                f"{_quote('model_version')} String, "
                f"{_quote('seen_at')} DateTime64(3, 'UTC') DEFAULT now64(3, 'UTC')) "
                f"ENGINE = MergeTree ORDER BY ({_quote('map_version')})"
            )
            result = self._cx.query(
                f"SELECT max({_quote('map_version')}) FROM {_quote(WATERMARK_TABLE)}"
            )
        except Exception as exc:
            raise EngineError(f"reading {WATERMARK_TABLE} failed: {exc}") from exc
        if not result.result_rows or result.result_rows[0][0] is None:
            return None
        highest = int(result.result_rows[0][0])
        # An empty MergeTree answers max() with 0 rather than with null, so zero here means either
        # "no map has been applied" or "map version 0 has been". Map version 0 is what this library
        # reads when a hand-written map omits the field, and a hand-written map is unsigned - so it
        # never reaches this path. Reported as absent, which is the honest reading of the two.
        return highest if highest > 0 else None

    def record_map_version(self, version: int, *, model_version: str) -> None:
        try:
            self._cx.insert(
                WATERMARK_TABLE,
                [[version, model_version]],
                column_names=["map_version", "model_version"],
            )
        except Exception as exc:
            raise EngineError(
                f"recording a map version in {WATERMARK_TABLE} failed: {exc}"
            ) from exc

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

    # --- migration ------------------------------------------------------------------------------
    #
    # `sde.migration.Migratable`, the same optional protocol the PostgreSQL adapter satisfies. Two
    # things are different here and neither is smoothed over: reads take `FINAL`, because a key
    # saved twice is an overwrite in this engine rather than an error, and `copy_in` has no
    # conflict clause to write - the collapse *is* the idempotence.

    def key_range(
        self,
        table: str,
        order: Sequence[str],
        *,
        after: Sequence[Any] | None = None,
        upto: Sequence[Any] | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Rows in key order, strictly after one key and up to another inclusive, with `FINAL`.

        `FINAL` is what makes keyset pagination correct here rather than merely fast enough. Without
        it a key saved twice returns two rows until a merge happens, and the next page starts
        strictly after that key - so one of the duplicates is read and the other is not, which for a
        backfill means copying a row this engine considers superseded.
        """
        cols = key_columns(order, table)
        clauses: list[str] = []
        parameters: dict[str, Any] = {}
        tuple_expr = f"({', '.join(_quote(c) for c in cols)})"
        if after is not None:
            same_width(after, cols, "after")
            names = [f"after_{i}" for i in range(len(cols))]
            clauses.append(f"{tuple_expr} > ({', '.join(f'%({n})s' for n in names)})")
            parameters.update(zip(names, (_as_utc(v) for v in after), strict=True))
        if upto is not None:
            same_width(upto, cols, "upto")
            names = [f"upto_{i}" for i in range(len(cols))]
            clauses.append(f"{tuple_expr} <= ({', '.join(f'%({n})s' for n in names)})")
            parameters.update(zip(names, (_as_utc(v) for v in upto), strict=True))
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        cap = ""
        if limit is not None:
            cap = " LIMIT %(row_limit)s"
            parameters["row_limit"] = int(limit)
        sql = f"SELECT * FROM {_quote(table)} FINAL{where} ORDER BY {tuple_expr}{cap}"
        try:
            result = self._cx.query(sql, parameters=parameters)
        except Exception as exc:
            raise EngineError(f"key range select from {table} failed: {exc}") from exc
        return [_row(result.column_names, result.column_types, row) for row in result.result_rows]

    def nth_key(
        self, table: str, order: Sequence[str], *, position: int
    ) -> tuple[Any, ...] | None:
        """The key of the ``position``-th row in key order, one-based, or None if there is none."""
        cols = key_columns(order, table)
        if position < 1:
            raise EngineError(f"position is one-based; {position} is not a row")
        projection = ", ".join(_quote(c) for c in cols)
        sql = (
            f"SELECT {projection} FROM {_quote(table)} FINAL ORDER BY ({projection}) "
            f"LIMIT 1 OFFSET %(skip)s"
        )
        try:
            result = self._cx.query(sql, parameters={"skip": position - 1})
        except Exception as exc:
            raise EngineError(f"reading row {position} of {table} failed: {exc}") from exc
        if not result.result_rows:
            return None
        row = _row(result.column_names, result.column_types, result.result_rows[0])
        return tuple(row[column] for column in cols)

    def copy_in(self, table: str, rows: Sequence[Mapping[str, Any]]) -> None:
        """Insert rows. Duplicates are collapsed by the table rather than rejected by it.

        There is no `ON CONFLICT` to write and none is needed: the tables this library creates here
        are `ReplacingMergeTree` ordered by the key, so a row copied twice leaves two parts that
        collapse to the newest at merge time and read as one under `FINAL`. That is the same
        idempotence the PostgreSQL path gets from a conflict clause, arrived at from the opposite
        direction - and it is why a recopied chunk is free in both engines.
        """
        if not rows:
            return
        cols = sorted(rows[0])
        for row in rows:
            if sorted(row) != cols:
                raise EngineError(
                    f"copy_in into {table} was given rows with different columns "
                    f"({cols} and {sorted(row)}). A chunk comes from one table, so this is a "
                    f"caller assembling it from two."
                )
        data = [[_as_utc(row[c]) for c in cols] for row in rows]
        try:
            self._cx.insert(table, data, column_names=cols)
        except Exception as exc:
            log("sde.write.failed", table=table, error=type(exc).__name__)
            raise EngineError(f"copying {len(rows)} rows into {table} failed: {exc}") from exc

    def backfill_marker(self, *, materialization: str, entity: str) -> int:
        """How many rows of this entity have been copied into this engine. Zero if none.

        A plain `MergeTree` and `max()`, as the map watermark is - append-only by design, so there
        are no duplicates to collapse and no reason to pay for `FINAL`. The quirk that needed a
        comment there is harmless here: an empty aggregate answers 0 rather than null, and 0 is
        exactly what "nothing has been copied" means, so the two readings coincide.
        """
        try:
            self._cx.command(
                f"CREATE TABLE IF NOT EXISTS {_quote(BACKFILL_TABLE)} ("
                f"{_quote('materialization')} String, "
                f"{_quote('entity')} String, "
                f"{_quote('rows_copied')} Int64, "
                f"{_quote('at')} DateTime64(3, 'UTC') DEFAULT now64(3, 'UTC')) "
                f"ENGINE = MergeTree ORDER BY ({_quote('materialization')}, {_quote('entity')})"
            )
            result = self._cx.query(
                f"SELECT max({_quote('rows_copied')}) FROM {_quote(BACKFILL_TABLE)} "
                f"WHERE {_quote('materialization')} = %(m)s AND {_quote('entity')} = %(e)s",
                parameters={"m": materialization, "e": entity},
            )
        except Exception as exc:
            raise EngineError(f"reading {BACKFILL_TABLE} failed: {exc}") from exc
        if not result.result_rows or result.result_rows[0][0] is None:
            return 0
        return int(result.result_rows[0][0])

    def record_backfill_marker(self, *, materialization: str, entity: str, rows: int) -> None:
        """Append the new marker. Never update, so an interrupted run leaves a readable trail."""
        try:
            self._cx.insert(
                BACKFILL_TABLE,
                [[materialization, entity, int(rows)]],
                column_names=["materialization", "entity", "rows_copied"],
            )
        except Exception as exc:
            raise EngineError(
                f"recording backfill progress in {BACKFILL_TABLE} failed: {exc}"
            ) from exc

    # --- transactions ----------------------------------------------------------------------

    def transaction(self) -> Iterator[ClickHouseEngine]:
        """Refuses. There is no transaction here to give you.

        A no-op context manager would be the friendlier signature and the worse library: the
        caller would believe a group of writes was atomic, and would find out otherwise from the
        state of the data rather than from an exception.

        The way out is not a flag. Declare the atomicity - `atomic_with` on the entity - and the
        planner is then obliged to place those entities in one group, and one group is one engine,
        so it will not be this one.

        Not decorated with `@contextmanager`, unlike the PostgreSQL one. A decorated generator
        would need a `yield` after the `raise` to keep the type honest, and that statement is
        unreachable - `mypy --strict` says so, correctly. A plain method that raises fails at the
        call, which is one frame earlier and reads better in a traceback.
        """
        raise EngineError(
            "ClickHouse has no multi-statement transactions, so this adapter will not pretend to "
            "start one. If these writes have to commit together, declare it: `atomic_with` on the "
            "entities makes them one colocation group, one group is one engine, and the planner is "
            "then not permitted to put them here. A silent no-op context manager would let the "
            "writes proceed and let you believe they were atomic."
        )

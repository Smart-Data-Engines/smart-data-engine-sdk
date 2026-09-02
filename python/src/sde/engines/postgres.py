"""PostgreSQL adapter.

Two rules run through everything here, and both come straight from the requirements rather than
from taste.

**A failed write is reported, never worked around.** No retry into another engine, no swallowing,
no "eventually consistent" story invented on the spot. If the source engine for a group will not
take the write, the client's code finds out (:class:`~sde.errors.EngineError`). The library
swallows its *own* internal problems - routing, telemetry - because a bug of ours must not take
down someone's application, but a write that did not happen is not our internal problem and
reporting success for it would be the single worst thing this library could do.

**Identifiers are quoted, always.** Not for injection - identifiers come from the placement map,
not from user input - but because entity names may contain non-ASCII characters, and an unquoted
identifier in PostgreSQL is folded to lower case in a way that is lossy for some of them.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any

from ..errors import EngineError
from ..logging import log
from ..placement import WATERMARK_TABLE, PhysicalLayout
from ..schema import QUOTE, schema_statements

__all__ = ["PostgresEngine"]


# Bound from the one definition in sde.schema, so that DDL and DML cannot disagree about
# how an identifier is escaped.
_quote = QUOTE["postgres"]


class PostgresEngine:
    """A thin adapter over psycopg. Deliberately thin: it executes decisions, it makes none."""

    dialect = "postgres"

    def __init__(self, dsn: str) -> None:
        try:
            import psycopg
        except ImportError as exc:  # pragma: no cover - depends on the install extra
            raise EngineError(
                "the PostgreSQL adapter needs the 'postgres' extra: "
                "pip install 'smart-data-engine[postgres]'. "
                "The core library has no dependencies, because it goes into your application and "
                "every dependency here would be one you inherit."
            ) from exc
        self._psycopg = psycopg
        self._dsn = dsn
        self._conn: Any = None

    # --- connection ------------------------------------------------------------------------

    def connect(self) -> None:
        if self._conn is None:
            try:
                self._conn = self._psycopg.connect(self._dsn, autocommit=True)
            except Exception as exc:
                raise EngineError(f"could not connect to PostgreSQL: {exc}") from exc

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> PostgresEngine:
        self.connect()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @property
    def _cx(self) -> Any:
        if self._conn is None:
            raise EngineError("not connected; call connect() first")
        return self._conn

    # --- schema ----------------------------------------------------------------------------

    def ensure_schema(self, layout: PhysicalLayout, *, keys: Mapping[str, Sequence[str]]) -> None:
        """Create what is missing, change nothing that exists.

        Idempotent on purpose: an application restarting must not reapply DDL, and two instances
        starting at once must not race. Anything beyond creation - altering a column, dropping an
        index - is a migration, which is the orchestrator's job and carries a safety
        classification. A library that quietly altered a live column would be doing the one thing
        this product promises never to do without a rollback path.
        """
        statements = schema_statements(layout, keys=keys, dialect=self.dialect)

        with self._cx.cursor() as cur:
            for statement in statements:
                try:
                    cur.execute(statement)
                except Exception as exc:
                    raise EngineError(f"schema statement failed: {statement}: {exc}") from exc
        log("sde.schema.applied", engine=self.dialect, statements=len(statements))
        self._verify_schema(layout)

    def _verify_schema(self, layout: PhysicalLayout) -> None:
        """Check that what exists is what the map describes, because IF NOT EXISTS does not.

        `CREATE TABLE IF NOT EXISTS` accepts a table of that name whatever shape it is in, so a
        table left over from something else - an older map, another application, a hand-run
        migration - is silently kept and the first insert fails with `column "at" does not exist`.
        That error names a column and not the cause, and it arrives in the client's request path
        rather than at startup.

        A **missing** column is refused: writes through this map cannot work. An **extra** column
        is logged and allowed - a client may have added one outside SDE, the map does not name it,
        writes are unaffected, and refusing would make this library an obstacle to work it has no
        opinion about.

        Names only. Comparing declared types to `information_schema.data_type` means matching
        `numeric(8,2)` against `numeric`, and a check that has to normalise dialect spellings
        would report differences that are not differences.
        """
        expected = {
            table: set(layout.columns.get(entity, {}))
            for entity, table in sorted(layout.tables.items())
        }
        if not expected:
            return

        with self._cx.cursor() as cur:
            cur.execute(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = ANY(%s)",
                [sorted(expected)],
            )
            found: dict[str, set[str]] = {}
            for table_name, column_name in cur.fetchall():
                found.setdefault(str(table_name), set()).add(str(column_name))

        for table, columns in sorted(expected.items()):
            actual = found.get(table)
            if actual is None:
                raise EngineError(
                    f"{table!r} does not exist after applying the schema. The statement reported "
                    f"success, so this is a permissions or search_path problem rather than a bad "
                    f"map."
                )
            missing = sorted(columns - actual)
            if missing:
                raise EngineError(
                    f"{table!r} already existed with a different shape: the map needs {missing} "
                    f"and the table has {sorted(actual)}. `CREATE TABLE IF NOT EXISTS` keeps "
                    f"whatever is there, so this table came from somewhere else - an older map, "
                    f"another application, a migration run by hand. Refusing here rather than at "
                    f"the first insert, which would fail in your request path with an error naming "
                    f"a column and not the cause."
                )
            extra = sorted(actual - columns)
            if extra:
                log("sde.schema.extra_columns", table=table, columns=extra)

    # --- data ------------------------------------------------------------------------------

    def insert(self, table: str, values: Mapping[str, Any]) -> None:
        if not values:
            raise EngineError("nothing to insert")
        cols = sorted(values)
        placeholders = ", ".join(["%s"] * len(cols))
        sql = (
            f"INSERT INTO {_quote(table)} ({', '.join(_quote(c) for c in cols)}) "
            f"VALUES ({placeholders})"
        )
        try:
            with self._cx.cursor() as cur:
                cur.execute(sql, [values[c] for c in cols])
        except Exception as exc:
            # Surfaced, not swallowed and not rerouted. See the module docstring.
            log("sde.write.failed", table=table, error=type(exc).__name__)
            raise EngineError(f"insert into {table} failed: {exc}") from exc

    def get(self, table: str, key: Mapping[str, Any]) -> dict[str, Any] | None:
        where = " AND ".join(f"{_quote(c)} = %s" for c in sorted(key))
        sql = f"SELECT * FROM {_quote(table)} WHERE {where}"
        try:
            with self._cx.cursor() as cur:
                cur.execute(sql, [key[c] for c in sorted(key)])
                row = cur.fetchone()
                if row is None:
                    return None
                names = [d.name for d in cur.description or ()]
                return dict(zip(names, row, strict=True))
        except Exception as exc:
            raise EngineError(f"select from {table} failed: {exc}") from exc

    # --- rollback protection ------------------------------------------------------------------
    #
    # Two methods that satisfy `sde.watermark.WatermarkStore`, which is a separate optional
    # protocol rather than part of `Engine`: adding these to `Engine` would break every adapter
    # anybody has written, for a capability our own orderbook engine cannot provide.

    def map_watermark(self) -> int | None:
        """The highest map version applied against this engine, creating the table if missing.

        Creating on read rather than on write, and that closes a real gap: with the table appearing
        only on the first write, the first load of a signed map has nothing to compare against and a
        file swapped immediately after a deployment goes unnoticed. Reading is also the cheaper of
        the two paths to make idempotent - `CREATE TABLE IF NOT EXISTS` here costs one statement per
        session, once per process.
        """
        try:
            with self._cx.cursor() as cur:
                cur.execute(
                    f"CREATE TABLE IF NOT EXISTS {_quote(WATERMARK_TABLE)} ("
                    f"{_quote('map_version')} bigint NOT NULL, "
                    f"{_quote('model_version')} text NOT NULL, "
                    f"{_quote('seen_at')} timestamptz NOT NULL DEFAULT now())"
                )
                cur.execute(f"SELECT max({_quote('map_version')}) FROM {_quote(WATERMARK_TABLE)}")
                row = cur.fetchone()
        except Exception as exc:
            raise EngineError(f"reading {WATERMARK_TABLE} failed: {exc}") from exc
        if row is None or row[0] is None:
            return None
        return int(row[0])

    def record_map_version(self, version: int, *, model_version: str) -> None:
        """Append. Never update, so there is nothing to contend over and nothing to lose.

        The timestamp comes from the engine's own `now()` rather than from this process: an audit
        column wants the clock of the thing being audited, and this library reading a clock is a
        thing its tests would then have to work around.
        """
        try:
            with self._cx.cursor() as cur:
                cur.execute(
                    f"INSERT INTO {_quote(WATERMARK_TABLE)} "
                    f"({_quote('map_version')}, {_quote('model_version')}) VALUES (%s, %s)",
                    [version, model_version],
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
        params: list[Any] = []
        if low is not None:
            clauses.append(f"{_quote(column)} >= %s")
            params.append(low)
        if high is not None:
            clauses.append(f"{_quote(column)} < %s")
            params.append(high)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        order = f" ORDER BY {_quote(column)}"
        cap = ""
        if limit is not None:
            cap = " LIMIT %s"
            params.append(limit)
        sql = f"SELECT * FROM {_quote(table)}{where}{order}{cap}"
        try:
            with self._cx.cursor() as cur:
                cur.execute(sql, params)
                names = [d.name for d in cur.description or ()]
                return [dict(zip(names, row, strict=True)) for row in cur.fetchall()]
        except Exception as exc:
            raise EngineError(f"range select from {table} failed: {exc}") from exc

    def count(self, table: str) -> int:
        try:
            with self._cx.cursor() as cur:
                cur.execute(f"SELECT count(*) FROM {_quote(table)}")
                row = cur.fetchone()
                return int(row[0]) if row else 0
        except Exception as exc:
            raise EngineError(f"count on {table} failed: {exc}") from exc

    # --- transactions ----------------------------------------------------------------------

    @contextmanager
    def transaction(self) -> Iterator[PostgresEngine]:
        """One engine, one transaction, that engine's semantics.

        There is no distributed transaction here and there will not be one. A client needing two
        entities to commit together declares that, and the planner puts them in the same group and
        therefore the same engine - so the requirement turns into a placement constraint instead
        of a two-phase commit. That is the trade this product makes, and it is why this method is
        four lines rather than a subsystem.
        """
        cx = self._cx
        previous = cx.autocommit
        cx.autocommit = False
        try:
            yield self
            cx.commit()
        except Exception:
            cx.rollback()
            raise
        finally:
            cx.autocommit = previous

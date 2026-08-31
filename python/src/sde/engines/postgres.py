"""PostgreSQL adapter.

Two rules run through everything here, and both come straight from the requirements rather than from
taste.

**A failed write is reported, never worked around.** No retry into another engine, no swallowing, no
"eventually consistent" story invented on the spot. If the source engine for a group will not take
the write, the client's code finds out (:class:`~sde.errors.EngineError`). The library swallows its
*own* internal problems - routing, telemetry - because a bug of ours must not take down someone's
application, but a write that did not happen is not our internal problem and reporting success for
it would be the single worst thing this library could do.

**Identifiers are quoted, always.** Not for injection - identifiers come from the placement map, not
from user input - but because entity names may contain non-ASCII characters, and an unquoted
identifier in PostgreSQL is folded to lower case in a way that is lossy for some of them.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any

from ..errors import EngineError
from ..logging import log
from ..placement import PhysicalLayout
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
        index - is a migration, which is the orchestrator's job and carries a safety classification.
        A library that quietly altered a live column would be doing the one thing this product
        promises never to do without a rollback path.
        """
        statements = schema_statements(layout, keys=keys, dialect=self.dialect)

        with self._cx.cursor() as cur:
            for statement in statements:
                try:
                    cur.execute(statement)
                except Exception as exc:
                    raise EngineError(f"schema statement failed: {statement}: {exc}") from exc
        log("sde.schema.applied", engine=self.dialect, statements=len(statements))

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
        therefore the same engine - so the requirement turns into a placement constraint instead of
        a two-phase commit. That is the trade this product makes, and it is why this method is four
        lines rather than a subsystem.
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

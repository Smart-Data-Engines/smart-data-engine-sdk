"""Build, confirm and drop one index on a live table, through a dedicated operator connection.

Nothing here trusts ``IF NOT EXISTS``, because both engines were measured keeping the wrong object
under it (PostgreSQL 15.19, ClickHouse 24.8.14.39, 24 September 2026):

- a failed ``CREATE INDEX CONCURRENTLY`` leaves its index in the catalogue, neither valid nor ready,
  and ``CREATE INDEX CONCURRENTLY IF NOT EXISTS`` over it succeeds with only a notice;
- ``ALTER TABLE ... ADD INDEX IF NOT EXISTS`` with another type keeps the index that is there.

So every step reads the catalogue first and acts on what it found: an index of this name on this
table and of this shape is ours; anything else under the name is somebody else's object. A build
refuses it and an abandonment leaves it alone - neither adopts it or removes it.
"""

from __future__ import annotations

import re
import time
from collections.abc import Mapping
from typing import Any, Literal

from .._operator_deadline import DeadlineInterrupt
from ..errors import MigrationRefused
from ..physical import CLICKHOUSE_METHODS, POSTGRES_METHODS, index_method
from ..schema import _skip_index_type, clickhouse_index_clause, postgres_index_target
from ._operator import NativeOperator, TableIdentity

Status = Literal["absent", "unfinished", "ready", "foreign"]

_NAME = re.compile(r"sde_i_[0-9a-f]{32}_[0-9]{6}")
"""The only names an in-place build creates. Plain identifiers, so ClickHouse records a mutation
over one as ``MATERIALIZE INDEX <name>`` with the name unquoted - measured, and relied on to find a
materialization again instead of starting a second one."""

POLL_SECONDS = 0.2


class NativeIndexBuild:
    def __init__(self, native: NativeOperator) -> None:
        self.native = native
        self.dialect = native.dialect
        self.quote = native.quote

    # --- shared ------------------------------------------------------------------------------

    def _check(self, table: TableIdentity, index: Mapping[str, Any]) -> str:
        name = str(index["name"])
        if _NAME.fullmatch(name) is None:
            raise MigrationRefused("an in-place build creates only its own bound index names")
        allowed = POSTGRES_METHODS if self.dialect == "postgres" else CLICKHOUSE_METHODS
        if index_method(index) not in allowed:
            raise MigrationRefused(
                f"{self.dialect} cannot build a {index_method(index)} index in place"
            )
        if self.native.identity(table.name).physical_key != table.physical_key:
            raise MigrationRefused("the table an index build names was replaced")
        return name

    def inspect(self, table: TableIdentity, index: Mapping[str, Any]) -> tuple[Status, str]:
        """Where the build of this index stands, and for a foreign object, whose it seems to be."""
        name = self._check(table, index)
        if self.dialect == "postgres":
            return self._pg_status(table, index, name)
        return self._ch_status(table, index, name)

    def status(self, table: TableIdentity, index: Mapping[str, Any]) -> Status:
        return self.inspect(table, index)[0]

    def build(self, table: TableIdentity, index: Mapping[str, Any]) -> None:
        """Bring the index to ``ready`` without pausing the table's writers."""
        status, reason = self.inspect(table, index)
        if status == "foreign":
            raise MigrationRefused(reason)
        if self.dialect == "postgres":
            self._pg_build(table, index, status)
        else:
            self._ch_build(table, index, status)
        if self.status(table, index) != "ready":
            raise MigrationRefused("the index build did not leave a ready index")

    def declared(self, table: TableIdentity, index: Mapping[str, Any]) -> Status:
        """Where an index the map in force declares stands on its table, whatever its name.

        The indexes an index change removes were named by a design or by an earlier build, so the
        bound-name rule of :meth:`inspect` does not apply; the table and the shape still do.
        """
        if self.native.identity(table.name).physical_key != table.physical_key:
            raise MigrationRefused("the table an index change names was replaced")
        name = str(index["name"])
        if self.dialect == "postgres":
            return self._pg_status(table, index, name)[0]
        return self._ch_status(table, index, name)[0]

    def remove(self, table: TableIdentity, index: Mapping[str, Any]) -> None:
        """Remove an index the map in force declares; run after the decision, so resumably.

        An index of the declared shape goes, finished or not - a PostgreSQL drop that was stopped
        leaves it invalid yet still maintained (measured), and dropping again removes it. Absent,
        or another object under the name, means ours is already gone; the other object stays.
        """
        status = self.declared(table, index)
        if status in ("absent", "foreign"):
            return
        name = str(index["name"])
        if self.dialect == "postgres":
            # IF EXISTS only for an index that goes while this drop waits for its lock: an earlier
            # drop whose client the budget closed runs on in the server until the transaction it
            # waits for ends. What is left afterwards is read back below, as always.
            self.native.command(f"DROP INDEX CONCURRENTLY IF EXISTS {self.quote(name)}")
        else:
            for mutation_id, is_done, _failure in self._ch_mutations(table, name):
                if not is_done:
                    self.native.command(
                        "KILL MUTATION WHERE database = currentDatabase() "
                        f"AND table = '{self._literal(table.name)}' "
                        f"AND mutation_id = '{self._literal(mutation_id)}'"
                    )
            self.native.command(
                f"ALTER TABLE {self.quote(table.name)} DROP INDEX {self.quote(name)} "
                "SETTINGS alter_sync = 0"
            )
        if self.declared(table, index) in ("unfinished", "ready"):
            raise MigrationRefused("a removed index is still in the catalogue")

    def drop(self, table: TableIdentity, index: Mapping[str, Any]) -> None:
        """Remove this build's own index, finished or not, and anything still materializing it.

        A foreign object under the name means ours is not there: it is left as it is.
        """
        status, _ = self.inspect(table, index)  # checks the name and the table first
        if status == "foreign":
            return
        name = str(index["name"])
        if self.dialect == "postgres":
            if status != "absent":
                self.native.command(f"DROP INDEX CONCURRENTLY {self.quote(name)}")
        else:
            for mutation_id, is_done, _failure in self._ch_mutations(table, name):
                if not is_done:
                    self.native.command(
                        "KILL MUTATION WHERE database = currentDatabase() "
                        f"AND table = '{self._literal(table.name)}' "
                        f"AND mutation_id = '{self._literal(mutation_id)}'"
                    )
            if status != "absent":
                # DROP INDEX is itself a mutation, and by default the ALTER waits for it on the
                # merge pool - measured, it waits forever while merges are stopped, which is the
                # very case an abandoned build is likely to be in. With alter_sync=0 the index
                # leaves the catalogue at once and the pool removes its files later.
                self.native.command(
                    f"ALTER TABLE {self.quote(table.name)} DROP INDEX {self.quote(name)} "
                    "SETTINGS alter_sync = 0"
                )
        if self.status(table, index) in ("unfinished", "ready"):
            raise MigrationRefused("an abandoned index is still in the catalogue")

    @staticmethod
    def _literal(value: str) -> str:
        if re.fullmatch(r"[A-Za-z0-9_.\-]+", value) is None:
            raise MigrationRefused("an index build refuses an identifier it cannot quote as data")
        return value

    # --- PostgreSQL --------------------------------------------------------------------------

    def _pg_status(
        self, table: TableIdentity, index: Mapping[str, Any], name: str
    ) -> tuple[Status, str]:
        rows = self.native.rows(
            "SELECT i.indrelid::text,i.indisunique,i.indisvalid AND i.indisready,"
            "i.indpred IS NULL AND i.indexprs IS NULL AND i.indnkeyatts=i.indnatts,a.amname,"
            "ARRAY(SELECT p.attname FROM unnest(i.indkey) WITH ORDINALITY k(num,pos) "
            "JOIN pg_attribute p ON p.attrelid=i.indrelid AND p.attnum=k.num ORDER BY k.pos),"
            "i.indoption::smallint[] FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid "
            "JOIN pg_am a ON a.oid=c.relam WHERE c.oid=to_regclass(%s)",
            [self.quote(name)],
        )
        if not rows:
            # Index names share PostgreSQL's relation namespace: a table or view of this name
            # would make CREATE INDEX fail, and it is not ours to remove.
            if self.native.rows("SELECT to_regclass(%s)", [self.quote(name)])[0][0] is not None:
                return "foreign", "another relation holds this index build's name"
            return "absent", ""
        target, unique, usable, simple, method, columns, options = rows[0]
        if (
            str(target) != table.object
            or unique
            or not simple
            or method != index_method(index)
            or list(columns) != [str(column) for column in index["columns"]]
            or any(options)
        ):
            return (
                "foreign",
                "an index of this build's name exists with another table or shape; it is not ours",
            )
        return ("ready" if usable else "unfinished"), ""

    def _pg_build(self, table: TableIdentity, index: Mapping[str, Any], status: Status) -> None:
        if status == "ready":
            return
        if status == "unfinished":
            # Our own leftover of an interrupted build: never valid again by itself, and kept by
            # IF NOT EXISTS. Removed without blocking writers, then built again.
            self.native.command(f"DROP INDEX CONCURRENTLY {self.quote(str(index['name']))}")
        self.native.command(f"CREATE INDEX CONCURRENTLY {postgres_index_target(index, table.name)}")

    # --- ClickHouse --------------------------------------------------------------------------

    def _ch_index(self, table: TableIdentity, name: str) -> tuple[str, str, int] | None:
        rows = self.native.rows(
            "SELECT type_full, expr, granularity FROM system.data_skipping_indices "
            "WHERE database = currentDatabase() AND table = {table:String} "
            "AND name = {name:String}",
            {"table": table.name, "name": name},
        )
        if not rows:
            return None
        kind, expr, granularity = rows[0]
        return str(kind), str(expr), int(granularity)

    def _ch_mutations(self, table: TableIdentity, name: str) -> list[tuple[str, bool, str]]:
        # KILL MUTATION removes the mutation from this table - measured - so what is listed here
        # is either done or still to be done.
        rows = self.native.rows(
            "SELECT mutation_id, is_done, latest_fail_reason FROM system.mutations "
            "WHERE database = currentDatabase() AND table = {table:String} "
            "AND command = {command:String} AND NOT is_killed ORDER BY create_time",
            {"table": table.name, "command": f"MATERIALIZE INDEX {name}"},
        )
        return [(str(mutation_id), bool(done), str(failure)) for mutation_id, done, failure in rows]

    def _ch_status(
        self, table: TableIdentity, index: Mapping[str, Any], name: str
    ) -> tuple[Status, str]:
        from ..physical import parse_identifier_list

        found = self._ch_index(table, name)
        if found is None:
            return "absent", ""
        kind, expr, granularity = found
        try:
            columns = parse_identifier_list(expr)
        except ValueError:
            columns = ()
        if (
            kind != _skip_index_type(index)
            or list(columns) != [str(column) for column in index["columns"]]
            or granularity != index["granularity"]
        ):
            return (
                "foreign",
                "a data-skipping index of this build's name has another shape; it is not ours",
            )
        # Parts written after ADD INDEX carry the index; parts before it only once the
        # materialization is done. The catalogue lists the index from the ADD on, so it alone
        # does not say the index covers the table. A finished mutation is the evidence - and when
        # the server has forgotten it (it keeps the last finished_mutations_to_keep), the index
        # reads as unfinished and is materialized once more: slower, never falsely ready.
        done = any(is_done for _, is_done, _ in self._ch_mutations(table, name))
        return ("ready" if done else "unfinished"), ""

    def _ch_build(self, table: TableIdentity, index: Mapping[str, Any], status: Status) -> None:
        name = str(index["name"])
        if status == "ready":
            return
        if status == "absent":
            self.native.command(
                f"ALTER TABLE {self.quote(table.name)} ADD {clickhouse_index_clause(index)}"
            )
            added, reason = self._ch_status(table, index, name)
            if added == "foreign":
                raise MigrationRefused(reason)
        if not self._ch_mutations(table, name):
            # Asynchronous on purpose: mutations_sync would hold one HTTP request for the whole
            # rewrite. Found again by its recorded command after a restart, not started twice.
            self.native.command(
                f"ALTER TABLE {self.quote(table.name)} MATERIALIZE INDEX {self.quote(name)}"
            )
        failure = ""
        try:
            while True:
                mutations = self._ch_mutations(table, name)
                if any(is_done for _, is_done, _ in mutations):
                    return
                # The server retries a failed mutation by itself; the reason is kept for the one
                # thing that ends this wait without success, the signed build budget.
                failure = next((reason for _, _, reason in mutations if reason), failure)
                time.sleep(POLL_SECONDS)
        except DeadlineInterrupt as exc:
            if not failure:
                raise
            raise DeadlineInterrupt(
                f"the index materialization kept failing until the build budget: {failure}"
            ) from exc

"""Native DDL and drain operations for the public write-fence protocol.

These objects need dedicated provisioning connections. They do not run on the application's write
path. Metadata is checked as well as created: an identically named, unrelated table is not ours.
"""

from __future__ import annotations

import re
from typing import Any

from ..errors import EngineError, MigrationRefused
from ..schema import QUOTE
from ..write_fence import DRAIN_TABLE, EPOCH_COLUMN, FenceMetadata, fence_state


class PostgresFences:
    def __init__(self, connection: Any) -> None:
        if not connection.autocommit:
            raise MigrationRefused("write-fence DDL needs a dedicated autocommit connection")
        self._cx = connection
        self._quote = QUOTE["postgres"]

    def _query(self, query: str, values: list[Any] | None = None) -> list[Any]:
        try:
            with self._cx.cursor() as cursor:
                cursor.execute(query, values)
                return list(cursor.fetchall()) if cursor.description else []
        except Exception as exc:
            raise EngineError(
                f"write-fence operation failed; inspect or resume its state: {exc}"
            ) from exc

    def _idle(self) -> None:
        if int(self._cx.info.transaction_status) != 0:
            raise MigrationRefused("write-fence DDL cannot run inside an application transaction")

    def metadata(self, table: str) -> FenceMetadata:
        quoted = self._quote(table)
        rows = self._query(
            "SELECT c.oid::text, c.relkind, EXISTS (SELECT 1 FROM pg_inherits i "
            "WHERE i.inhrelid=c.oid OR i.inhparent=c.oid) FROM pg_class c "
            "WHERE c.oid=to_regclass(%s)",
            [quoted],
        )
        if len(rows) != 1:
            raise EngineError("write-fence table does not exist")
        identity, kind, inherits = rows[0]
        if kind != "r" or inherits:
            raise MigrationRefused(
                "write fences support ordinary PostgreSQL tables without inheritance"
            )
        columns = self._query(
            "SELECT a.atttypid='bigint'::regtype, a.attnotnull, a.attgenerated, "
            "pg_get_expr(d.adbin,d.adrelid) FROM pg_attribute a "
            "LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum "
            "WHERE a.attrelid=to_regclass(%s) AND a.attname=%s AND NOT a.attisdropped",
            [quoted, EPOCH_COLUMN],
        )
        valid = (
            bool(columns)
            and columns[0][:3] == (True, True, "")
            and columns[0][3]
            in (
                "0",
                "'0'::bigint",
            )
        )
        constraints = self._query(
            "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid=to_regclass(%s) AND contype='c'",
            [quoted],
        )
        return FenceMetadata(
            str(identity),
            "valid" if valid else "conflict" if columns else "absent",
            {str(name): str(expression) for name, expression in constraints},
        )

    def add_column(self, table: str) -> None:
        self._idle()
        self._query(
            f"ALTER TABLE {self._quote(table)} ADD COLUMN IF NOT EXISTS "
            f"{self._quote(EPOCH_COLUMN)} bigint NOT NULL DEFAULT 0"
        )

    def add_constraint(self, table: str, name: str, expression: str) -> None:
        self._idle()
        if name in self.metadata(table).constraints:
            return
        expression = {"1": "true", "0": "false"}.get(expression, expression)
        self._query(
            f"ALTER TABLE {self._quote(table)} ADD CONSTRAINT {self._quote(name)} "
            f"CHECK ({expression}) NOT VALID"
        )

    def drop_constraint(self, table: str, name: str) -> None:
        self._idle()
        self._query(
            f"ALTER TABLE {self._quote(table)} DROP CONSTRAINT IF EXISTS {self._quote(name)}"
        )

    def drain(self, table: str, *, project_id: str, hold: str) -> None:
        self._idle()
        # Even when a hold already existed, returning from this transaction proves that every
        # writer which held a conflicting table lock has completed. A retry repeats this proof.
        with self._cx.transaction():
            self._query(f"LOCK TABLE {self._quote(table)} IN SHARE ROW EXCLUSIVE MODE")
            _held(self.metadata(table), project_id, hold)

    def restore(self, table: str, *, project_id: str, hold: str) -> None:
        _held(self.metadata(table), project_id, hold)


class ClickHouseFences:
    def __init__(self, client: Any) -> None:
        self._cx = client
        self._quote = QUOTE["clickhouse"]

    def _query(self, query: str, parameters: dict[str, Any] | None = None) -> list[Any]:
        try:
            return list(self._cx.query(query, parameters=parameters or {}).result_rows)
        except Exception as exc:
            raise EngineError(f"write-fence metadata query failed: {exc}") from exc

    def _command(self, query: str) -> None:
        try:
            self._cx.command(query)
        except Exception as exc:
            raise EngineError(
                f"write-fence DDL failed; the table may remain closed or detached; "
                f"resume the same operation: {exc}"
            ) from exc

    def _table(self, table: str) -> list[Any]:
        return self._query(
            "SELECT toString(uuid), engine, formatQuery(create_table_query) FROM system.tables "
            "WHERE database=currentDatabase() AND name={table:String}",
            {"table": table},
        )

    def metadata(self, table: str) -> FenceMetadata:
        database = self._query("SELECT engine FROM system.databases WHERE name=currentDatabase()")
        if database != [("Atomic",)]:
            raise MigrationRefused("write fences require a local Atomic ClickHouse database")
        rows = self._table(table)
        if len(rows) != 1:
            raise EngineError(
                "write-fence table does not exist or is detached; resume the same operation"
            )
        identity, kind, create = rows[0]
        if kind not in ("MergeTree", "ReplacingMergeTree"):
            raise MigrationRefused(
                "write fences support local MergeTree and ReplacingMergeTree tables"
            )
        columns = self._query(
            "SELECT type, default_kind, default_expression FROM system.columns "
            "WHERE database=currentDatabase() AND table={table:String} AND name={column:String}",
            {"table": table, "column": EPOCH_COLUMN},
        )
        valid = columns == [("Int64", "DEFAULT", "0")]
        constraints = {
            name: expression.rstrip().removesuffix(",")
            for name, expression in re.findall(
                r'^\s*CONSTRAINT\s+[`"]?(__sde_f_[A-Za-z0-9_]+)[`"]?\s+CHECK\s+([^\n]+)',
                str(create),
                re.MULTILINE,
            )
        }
        return FenceMetadata(
            str(identity),
            "valid" if valid else "conflict" if columns else "absent",
            constraints,
        )

    def add_column(self, table: str) -> None:
        self._command(
            f"ALTER TABLE {self._quote(table)} ADD COLUMN IF NOT EXISTS "
            f"{self._quote(EPOCH_COLUMN)} Int64 DEFAULT 0"
        )

    def add_constraint(self, table: str, name: str, expression: str) -> None:
        self._command(
            f"ALTER TABLE {self._quote(table)} ADD CONSTRAINT IF NOT EXISTS "
            f"{self._quote(name)} CHECK {expression}"
        )

    def drop_constraint(self, table: str, name: str) -> None:
        self._command(
            f"ALTER TABLE {self._quote(table)} DROP CONSTRAINT IF EXISTS {self._quote(name)}"
        )

    def _drain_log(self) -> None:
        self._command(
            f"CREATE TABLE IF NOT EXISTS {self._quote(DRAIN_TABLE)} ("
            "table_name String, table_uuid UUID, project_id FixedString(32), hold String) "
            "ENGINE=MergeTree ORDER BY (table_uuid, project_id, hold)"
        )
        columns = self._query(
            "SELECT name,type FROM system.columns WHERE database=currentDatabase() "
            "AND table={table:String} ORDER BY name",
            {"table": DRAIN_TABLE},
        )
        if columns != [
            ("hold", "String"),
            ("project_id", "FixedString(32)"),
            ("table_name", "String"),
            ("table_uuid", "UUID"),
        ]:
            raise MigrationRefused("the reserved write-fence drain log has an incompatible schema")
        kinds = self._query(
            "SELECT engine FROM system.tables WHERE database=currentDatabase() "
            "AND name={table:String}",
            {"table": DRAIN_TABLE},
        )
        if kinds != [("MergeTree",)]:
            raise MigrationRefused("the reserved write-fence drain log has an incompatible engine")

    def drain(self, table: str, *, project_id: str, hold: str) -> None:
        metadata = self.metadata(table)
        _held(metadata, project_id, hold)
        self._drain_log()
        # Store the exact Atomic UUID before DETACH. A failed response can leave a permanently
        # detached table; resumption must not attach an unrelated table just because its name fits.
        try:
            self._cx.insert(
                DRAIN_TABLE,
                [[table, metadata.identity, project_id, hold]],
                column_names=["table_name", "table_uuid", "project_id", "hold"],
            )
        except Exception as exc:
            raise EngineError(f"write-fence drain intent was not confirmed: {exc}") from exc
        self._command(f"DETACH TABLE {self._quote(table)} PERMANENTLY SYNC")
        self._command(f"ATTACH TABLE {self._quote(table)}")
        after = self.metadata(table)
        if after.identity != metadata.identity:
            raise EngineError("the write-fence table identity changed while draining it")
        _held(after, project_id, hold)

    def restore(self, table: str, *, project_id: str, hold: str) -> None:
        if self._table(table):
            _held(self.metadata(table), project_id, hold)
            return
        # Reading a missing log fails closed. This path does not create one and must not guess
        # ownership from a detached table's name or from the supplied request alone.
        records = self._query(
            f"SELECT DISTINCT toString(table_uuid) FROM {self._quote(DRAIN_TABLE)} "
            "WHERE table_name={table:String} AND project_id={project:String} "
            "AND hold={hold:String}",
            {"table": table, "project": project_id, "hold": hold},
        )
        detached = self._query(
            "SELECT toString(uuid) FROM system.detached_tables "
            "WHERE database=currentDatabase() AND table={table:String} AND is_permanently=1",
            {"table": table},
        )
        if len(records) != 1 or detached != records:
            raise MigrationRefused("no matching durable intent for this detached write-fence table")
        self._command(f"ATTACH TABLE {self._quote(table)}")
        metadata = self.metadata(table)
        if metadata.identity != records[0][0]:
            raise EngineError("the restored write-fence table has another identity")
        _held(metadata, project_id, hold)


def _held(metadata: FenceMetadata, project_id: str, hold: str) -> None:
    state = fence_state(metadata)
    if state.project_id != project_id or hold not in state.holds:
        raise MigrationRefused("the requested project's write barrier is not installed")

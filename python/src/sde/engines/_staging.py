"""Create and recognize stage-owned objects through dedicated native operator connections."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from typing import Any

from ..errors import EngineError, MigrationRefused
from ..physical import POSTGRES_METHODS, index_method
from ..placement import PhysicalLayout
from ..schema import schema_statements
from ._operator import NativeOperator, TableIdentity


def creation_marker(project_id: str, stage_id: str, table: str) -> str:
    suffix = hashlib.sha256(table.encode("utf-8")).hexdigest()[:16]
    return f"sde.stage1.{project_id}.{stage_id}.{suffix}"


class NativeStaging:
    def __init__(self, native: NativeOperator) -> None:
        self.native = native
        self.engine = native.engine
        self.dialect = native.dialect
        self.quote = native.quote

    def marker(self, table: str) -> tuple[str, str | None] | None:
        if self.dialect == "postgres":
            rows = self.native.rows(
                "SELECT c.oid::text,obj_description(c.oid,'pg_class') FROM pg_class c "
                "WHERE c.oid=to_regclass(%s)",
                [self.quote(table)],
            )
        else:
            rows = self.native.rows(
                "SELECT toString(uuid),comment FROM system.tables "
                "WHERE database=currentDatabase() AND name={table:String}",
                {"table": table},
            )
        if not rows:
            return None
        if len(rows) != 1:
            raise MigrationRefused("staging table name has an ambiguous native identity")
        return str(rows[0][0]), None if rows[0][1] is None else str(rows[0][1])

    def preflight(self, layout: PhysicalLayout, keys: Mapping[str, Sequence[str]]) -> None:
        schema_statements(layout, keys=keys, dialect=self.dialect)
        for table in layout.tables.values():
            if self.marker(table) is not None:
                raise MigrationRefused("a fresh staging name already exists in the target")
        if self.dialect == "postgres":
            identifiers = [
                *layout.tables.values(),
                *(name for columns in layout.columns.values() for name in columns),
                *(str(index["name"]) for index in layout.indexes),
            ]
            if any(len(name.encode("utf-8")) > 63 for name in identifiers):
                raise MigrationRefused("staging identifiers cannot be truncated by PostgreSQL")
            for index in layout.indexes:
                found = self.native.rows("SELECT to_regclass(%s)", [self.quote(str(index["name"]))])
                if found[0][0] is not None:
                    raise MigrationRefused(
                        "a staging index name already exists in the target namespace"
                    )

    def create_table(
        self, *, entity: str, layout: PhysicalLayout, keys: Mapping[str, Sequence[str]], marker: str
    ) -> TableIdentity:
        if re.fullmatch(r"sde\.stage1\.[0-9a-f]{32}\.[0-9a-f]{32}\.[0-9a-f]{16}", marker) is None:
            raise MigrationRefused("staging creation marker is not valid protocol metadata")
        table = layout.tables[entity]
        present = self.marker(table)
        if present is not None:
            if present[1] != marker:
                raise MigrationRefused("staging refuses a table without its exact creation marker")
            return self.native.identity(table)
        # The whole physical design of this one table: key order, partition and - in ClickHouse,
        # where `CREATE TABLE` is the only moment an index can be declared without a mutation over
        # every part - its data-skipping indexes. An earlier version passed the partition alone,
        # so a staged copy would have silently lost the key order and the indexes its signed map
        # authorized. PostgreSQL indexes follow separately in `create_indexes`.
        single = PhysicalLayout(
            tables={entity: table},
            columns={entity: layout.columns[entity]},
            partition_by={entity: layout.partition_by[entity]}
            if entity in layout.partition_by
            else {},
            key_order={entity: layout.key_order[entity]} if entity in layout.key_order else {},
            indexes=tuple(
                index
                for index in layout.indexes
                if self.dialect == "clickhouse" and str(index["entity"]) == entity
            ),
        )
        statements = schema_statements(single, keys=keys, dialect=self.dialect)
        if len(statements) != 1 or not statements[0].startswith("CREATE TABLE IF NOT EXISTS "):
            raise MigrationRefused("staging requires the SDK's ordinary table creation statement")
        statement = statements[0].replace("CREATE TABLE IF NOT EXISTS ", "CREATE TABLE ", 1)
        try:
            if self.dialect == "postgres":
                from psycopg import sql

                with self.engine._cx.transaction():
                    self.engine._cx.execute(statement)
                    self.engine._cx.execute(
                        sql.SQL("COMMENT ON TABLE {} IS {}").format(
                            sql.Identifier(table), sql.Literal(marker)
                        )
                    )
            else:
                # The marker is fixed ASCII protocol metadata, never a credential or a row value.
                self.engine._cx.command(statement + " COMMENT '" + marker + "'")
        except Exception as exc:
            raise EngineError(
                "staging table creation has an uncertain outcome; resume its intent"
            ) from exc
        found = self.marker(table)
        if found is None or found[1] != marker:
            raise MigrationRefused("staging creation marker was not established with the table")
        return self.native.identity(table)

    def owned(
        self, table: str, marker: str, identity: TableIdentity | None
    ) -> TableIdentity | None:
        """This staging's own table under ``table``, or ``None`` when it is absent or not ours.

        Ours means the exact creation marker and, once the identity was recorded, that native
        object. Anything else under the name - another marker, none, another object - belongs to
        somebody else and is left alone by an abandonment.
        """
        present = self.marker(table)
        if present is None or present[1] != marker:
            return None
        if identity is not None:
            return identity if present[0] == identity.object else None
        return self.native.identity(table)

    def drop_owned(self, table: TableIdentity, marker: str) -> None:
        """Drop this staging's own table; its indexes and generation constraints go with it."""
        present = self.marker(table.name)
        if present is None:
            return  # dropped before a crash; nothing is left to remove
        if present[1] != marker or present[0] != table.object:
            raise MigrationRefused("the table to abandon is no longer this staging's own")
        # SYNC: in an Atomic database a dropped table otherwise lingers until the server removes
        # it, and its name and data with it.
        suffix = " SYNC" if self.dialect == "clickhouse" else ""
        self.native.command(f"DROP TABLE {self.quote(table.name)}{suffix}")
        if self.marker(table.name) is not None:
            raise MigrationRefused("an abandoned staging table is still in the catalogue")

    def revoke_runtime(self, tables: Sequence[TableIdentity], principals: Sequence[str]) -> None:
        """Take back the runtime grants on dropped tables; ClickHouse keeps them after DROP TABLE.

        Measured on 24.8.14.39: ``system.grants`` still lists a table's grants once the table is
        gone, and ``REVOKE`` on the dropped table is accepted and removes them. PostgreSQL removes a
        table's privileges with the table, so there is nothing to take back there.
        """
        if self.dialect != "clickhouse":
            return
        for table in tables:
            qualified = self.quote(table.namespace) + "." + self.quote(table.name)
            for name in principals:
                self.native.command(f"REVOKE SELECT, INSERT ON {qualified} FROM {self.quote(name)}")
        for table in tables:
            for name in principals:
                rows = self.native.rows(
                    "SELECT count() FROM system.grants WHERE user_name={user:String} "
                    "AND database={database:String} AND table={table:String}",
                    {"user": name, "database": table.namespace, "table": table.name},
                )
                if rows[0][0]:
                    raise MigrationRefused("a runtime grant on an abandoned staging table remains")

    def create_indexes(self, layout: PhysicalLayout) -> None:
        if self.dialect != "postgres":
            # ClickHouse data-skipping indexes were declared inside `CREATE TABLE`; qualification
            # reads them back from `system.data_skipping_indices` before the stage is accepted.
            return
        for index in layout.indexes:
            name, entity = str(index["name"]), str(index["entity"])
            declared_method = index_method(index)
            if declared_method not in POSTGRES_METHODS:
                raise MigrationRefused(f"PostgreSQL cannot create a {declared_method} index")
            table = self.native.identity(layout.tables[entity])
            columns = [str(column) for column in index["columns"]]
            rows = self._index(name)
            if not rows:
                column_sql = ", ".join(self.quote(column) for column in columns)
                using = "" if declared_method == "btree" else f"USING {declared_method} "
                self.native.command(
                    f"CREATE INDEX {self.quote(name)} ON {self.quote(table.name)} "
                    f"{using}({column_sql})"
                )
                rows = self._index(name)
            if len(rows) != 1:
                raise MigrationRefused("staging index identity could not be established")
            target, unique, valid, ready, simple, method, actual, options = rows[0]
            if (
                str(target) != table.object
                or unique
                or not valid
                or not ready
                or not simple
                or method != declared_method
                or list(actual) != columns
                or any(options)
            ):
                raise MigrationRefused("staging index does not match its owned table and columns")

    def _index(self, name: str) -> list[Any]:
        return self.native.rows(
            "SELECT i.indrelid::text,i.indisunique,i.indisvalid,i.indisready,"
            "i.indpred IS NULL AND i.indexprs IS NULL AND i.indnkeyatts=i.indnatts,a.amname,"
            "ARRAY(SELECT p.attname FROM unnest(i.indkey) WITH ORDINALITY k(num,pos) "
            "JOIN pg_attribute p ON p.attrelid=i.indrelid AND p.attnum=k.num ORDER BY k.pos),"
            "i.indoption::smallint[] FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid "
            "JOIN pg_am a ON a.oid=c.relam WHERE c.oid=to_regclass(%s)",
            [self.quote(name)],
        )

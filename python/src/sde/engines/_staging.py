"""Create and recognize stage-owned objects through dedicated native operator connections."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from typing import Any

from ..errors import EngineError, MigrationRefused
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
        single = PhysicalLayout(
            tables={entity: table},
            columns={entity: layout.columns[entity]},
            partition_by={entity: layout.partition_by[entity]}
            if entity in layout.partition_by
            else {},
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

    def create_indexes(self, layout: PhysicalLayout) -> None:
        if self.dialect != "postgres":
            if layout.indexes:
                raise MigrationRefused("this staging target cannot create the requested indexes")
            return
        for index in layout.indexes:
            name, entity = str(index["name"]), str(index["entity"])
            table = self.native.identity(layout.tables[entity])
            columns = [str(column) for column in index["columns"]]
            rows = self._index(name)
            if not rows:
                column_sql = ", ".join(self.quote(column) for column in columns)
                self.native.command(
                    f"CREATE INDEX {self.quote(name)} ON {self.quote(table.name)} ({column_sql})"
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
                or method != "btree"
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

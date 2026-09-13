"""Native operations for the local cutover executor. Credentials and all rows stay local."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from ..errors import EngineError, MigrationRefused
from ..schema import QUOTE


@dataclass(frozen=True)
class TableIdentity:
    dialect: str
    server: str
    database: str
    object: str
    namespace: str
    name: str

    @property
    def physical_key(self) -> tuple[str, str, str, str]:
        return self.dialect, self.server, self.database, self.object

    def as_record(self) -> dict[str, str]:
        return {
            "dialect": self.dialect,
            "server": self.server,
            "database": self.database,
            "object": self.object,
            "namespace": self.namespace,
            "name": self.name,
        }


class NativeOperator:
    """A dedicated stock adapter plus dedicated read-only probes for declared runtime logins."""

    def __init__(self, engine: Any, runtime: Sequence[Any]) -> None:
        from .clickhouse import ClickHouseEngine
        from .postgres import PostgresEngine

        if not isinstance(engine, (PostgresEngine, ClickHouseEngine)) or not runtime:
            raise MigrationRefused(
                "local cutover requires stock operator adapters and runtime probes"
            )
        if any(
            type(probe) is not type(engine) or probe is engine or probe._cx is engine._cx
            for probe in runtime
        ):
            raise MigrationRefused(
                "runtime probes must be separate connections of the same dialect"
            )
        self.engine = engine
        self.runtime = tuple(runtime)
        self.dialect = engine.dialect
        self.quote = QUOTE[self.dialect]
        self.principals: tuple[str, ...] = ()
        self.principal_ids: dict[str, str] = {}
        if self.dialect == "postgres" and int(engine._cx.info.transaction_status) != 0:
            raise MigrationRefused("operator connection must not have an open transaction")
        if self.dialect == "clickhouse":
            # Every acknowledged repair/metadata INSERT must have reached the engine.
            engine._cx.set_client_setting("async_insert", 0)
            engine._cx.set_client_setting("wait_for_async_insert", 1)

    def rows(self, sql: str, params: Any = None, *, engine: Any = None) -> list[Any]:
        adapter = self.engine if engine is None else engine
        try:
            if self.dialect == "postgres":
                with adapter._cx.cursor() as cursor:
                    cursor.execute(sql, params)
                    return list(cursor.fetchall()) if cursor.description else []
            return list(adapter._cx.query(sql, parameters=params).result_rows)
        except Exception as exc:
            raise EngineError(f"local operator metadata query failed: {exc}") from exc

    def command(self, sql: str) -> None:
        if self.dialect == "postgres":
            self.rows(sql)
        else:
            try:
                self.engine._cx.command(sql)
            except Exception as exc:
                raise EngineError(
                    f"local operator command has an uncertain outcome: {exc}"
                ) from exc

    def endpoint(self, engine: Any = None) -> tuple[str, str, str]:
        if self.dialect == "postgres":
            rows = self.rows(
                "SELECT c.system_identifier::text, d.oid::text, current_database() "
                "FROM pg_control_system() c CROSS JOIN pg_database d "
                "WHERE d.datname=current_database()",
                engine=engine,
            )
        else:
            rows = self.rows(
                "SELECT toString(serverUUID()), toString(uuid), name "
                "FROM system.databases WHERE name=currentDatabase()",
                engine=engine,
            )
        if len(rows) != 1 or any(not str(value) for value in rows[0]):
            raise MigrationRefused("native endpoint identity is unavailable")
        return tuple(str(value) for value in rows[0])  # type: ignore[return-value]

    def identity(self, table: str, *, engine: Any = None) -> TableIdentity:
        server, database, database_name = self.endpoint(engine)
        if self.dialect == "postgres":
            rows = self.rows(
                "SELECT c.oid::text,n.nspname,c.relname,c.relkind,c.relpersistence,"
                "c.relrowsecurity,c.relforcerowsecurity,"
                "EXISTS(SELECT 1 FROM pg_inherits i WHERE i.inhrelid=c.oid OR i.inhparent=c.oid),"
                "EXISTS(SELECT 1 FROM pg_trigger t WHERE t.tgrelid=c.oid AND NOT t.tgisinternal),"
                "EXISTS(SELECT 1 FROM pg_rewrite r WHERE r.ev_class=c.oid) "
                "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE c.oid=to_regclass(%s)",
                [self.quote(table)],
                engine=engine,
            )
            if len(rows) != 1:
                raise MigrationRefused("cutover table is missing or invisible")
            oid, namespace, actual, kind, persistence, *unsupported = rows[0]
            if kind != "r" or persistence != "p" or any(unsupported):
                raise MigrationRefused(
                    "cutover requires persistent ordinary tables "
                    "without RLS, triggers, rules or inheritance"
                )
        else:
            rows = self.rows(
                "SELECT toString(uuid),database,name,engine,length(dependencies_table) "
                "FROM system.tables WHERE database=currentDatabase() AND name={table:String}",
                {"table": table},
                engine=engine,
            )
            if len(rows) != 1:
                raise MigrationRefused("cutover table is missing or invisible")
            oid, namespace, actual, kind, dependencies = rows[0]
            if kind not in ("MergeTree", "ReplacingMergeTree") or dependencies:
                raise MigrationRefused(
                    "cutover requires local MergeTree tables without dependent views"
                )
            if self.rows(
                "SELECT count() FROM system.row_policies WHERE database={database:String} "
                "AND table={table:String}",
                {"database": database_name, "table": table},
            )[0][0]:
                raise MigrationRefused("cutover does not transfer row policies")
        return TableIdentity(self.dialect, server, database, str(oid), str(namespace), str(actual))

    def qualify(
        self, tables: Sequence[str], allowed_tables: Sequence[str], *, required_access: bool = True
    ) -> tuple[str, ...]:
        """Require direct restricted logins whose table grants can be removed independently."""
        endpoint = self.endpoint()
        principals: list[str] = []
        for probe in self.runtime:
            if self.endpoint(probe) != endpoint:
                raise MigrationRefused(
                    "runtime and operator connections name different native databases"
                )
            if self.dialect == "postgres":
                rows = self.rows(
                    "SELECT current_user,session_user,r.oid::text,r.rolsuper,r.rolcreaterole,"
                    "r.rolcreatedb,r.rolreplication,r.rolbypassrls,"
                    "EXISTS(SELECT 1 FROM pg_auth_members m WHERE m.member=r.oid) "
                    "FROM pg_roles r WHERE r.rolname=current_user",
                    engine=probe,
                )
                user, session_user, oid, *powers = rows[0]
                if user != session_user or any(powers):
                    raise MigrationRefused(
                        "cutover runtime must be an unprivileged direct login "
                        "without role memberships"
                    )
                for table in tables:
                    if (
                        self.identity(table, engine=probe).physical_key
                        != self.identity(table).physical_key
                    ):
                        raise MigrationRefused("runtime resolves a different physical table")
                    rights = self.rows(
                        "SELECT c.relowner=%s::oid,has_schema_privilege(%s,n.oid,'CREATE'),"
                        "has_table_privilege(%s,c.oid,'SELECT'),has_table_privilege(%s,c.oid,'INSERT'),"
                        "has_table_privilege(%s,c.oid,'UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER') "
                        "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                        "WHERE c.oid=to_regclass(%s)",
                        [oid, user, user, user, user, self.quote(table)],
                    )[0]
                    if (
                        rights[0]
                        or rights[1]
                        or rights[4]
                        or (required_access and (not rights[2] or not rights[3]))
                    ):
                        raise MigrationRefused(
                            "runtime needs only SELECT/INSERT table rights "
                            "and no ownership or schema CREATE"
                        )
                    shared = self.rows(
                        "SELECT count(*) FROM pg_class c, LATERAL aclexplode(c.relacl) a "
                        "WHERE c.oid=to_regclass(%s) AND (a.grantee=0 OR "
                        "(a.grantee=%s::oid AND a.is_grantable))",
                        [self.quote(table), oid],
                    )[0][0]
                    columns = self.rows(
                        "SELECT count(*) FROM pg_attribute c, LATERAL aclexplode(c.attacl) a "
                        "WHERE c.attrelid=to_regclass(%s) AND a.grantee IN (0,%s::oid)",
                        [self.quote(table), oid],
                    )[0][0]
                    if shared or columns:
                        raise MigrationRefused(
                            "PUBLIC, grant-option or column grants "
                            "prevent isolated runtime revocation"
                        )
            else:
                user = str(self.rows("SELECT currentUser()", engine=probe)[0][0])
                if self.rows(
                    "SELECT count() FROM system.role_grants WHERE user_name={user:String}",
                    {"user": user},
                )[0][0]:
                    raise MigrationRefused(
                        "cutover runtime must use direct grants without role memberships"
                    )
                rows = self.rows(
                    "SELECT storage,toString(id) FROM system.users WHERE name={user:String}",
                    {"user": user},
                )
                if len(rows) != 1 or rows[0][0] != "local_directory":
                    raise MigrationRefused("cutover runtime must be a local SQL-managed user")
                oid = str(rows[0][1])
                grants = self.rows(
                    "SELECT access_type,database,table,column,is_partial_revoke,grant_option "
                    "FROM system.grants WHERE user_name={user:String}",
                    {"user": user},
                )
                for access, database, table, column, partial, option in grants:
                    ordinary = (
                        database == endpoint[2]
                        and table in allowed_tables
                        and access in ("SELECT", "INSERT")
                    )
                    settings = database == "system" and table == "settings" and access == "SELECT"
                    if not (ordinary or settings) or column is not None or partial or option:
                        raise MigrationRefused(
                            "runtime grants must be direct SELECT/INSERT on declared tables"
                        )
                for table in tables:
                    if required_access and (
                        self.identity(table, engine=probe).physical_key
                        != self.identity(table).physical_key
                    ):
                        raise MigrationRefused("runtime resolves a different physical table")
                    rights = {(str(row[0]), row[1], row[2]) for row in grants}
                    if required_access and any(
                        (kind, endpoint[2], table) not in rights for kind in ("SELECT", "INSERT")
                    ):
                        raise MigrationRefused("runtime is missing direct SELECT/INSERT grants")
            principals.append(str(user))
            self.principal_ids[str(user)] = str(oid)
        if len(set(principals)) != len(principals):
            raise MigrationRefused("runtime probes repeat the same login")
        self._qualify_readers(tables, principals)
        self.principals = tuple(sorted(principals))
        return self.principals

    def _qualify_readers(self, tables: Sequence[str], principals: Sequence[str]) -> None:
        """A missing runtime principal would keep reading the retired authority after cutover."""
        if self.dialect == "postgres":
            operator = str(self.rows("SELECT current_user")[0][0])
            allowed = {*principals, operator}
            for table in tables:
                grants = self.rows(
                    "SELECT pg_get_userbyid(a.grantee) FROM pg_class c, "
                    "LATERAL aclexplode(c.relacl) a WHERE c.oid=to_regclass(%s) "
                    "AND a.grantee <> c.relowner UNION "
                    "SELECT pg_get_userbyid(a.grantee) FROM pg_attribute p "
                    "JOIN pg_class c ON c.oid=p.attrelid, LATERAL aclexplode(p.attacl) a "
                    "WHERE c.oid=to_regclass(%s) AND a.grantee <> c.relowner",
                    [self.quote(table), self.quote(table)],
                )
                if any(str(row[0]) not in allowed for row in grants):
                    raise MigrationRefused("cutover table has an undeclared runtime grantee")
        else:
            operator = str(self.rows("SELECT currentUser()")[0][0])
            database = self.endpoint()[2]
            allowed = {*principals, operator}
            for table in tables:
                grants = self.rows(
                    "SELECT user_name,role_name FROM system.grants "
                    "WHERE (database IS NULL OR database='' OR database={database:String}) "
                    "AND (table IS NULL OR table='' OR table={table:String})",
                    {"database": database, "table": table},
                )
                if any(role is not None or user not in allowed for user, role in grants):
                    raise MigrationRefused("cutover table has an undeclared runtime grantee")

    def access(self, tables: Sequence[TableIdentity], *, enabled: bool) -> None:
        if not self.principals:
            raise MigrationRefused("runtime principals were not qualified")
        for table in tables:
            if self.identity(table.name).physical_key != table.physical_key:
                raise MigrationRefused("physical table identity changed before access transition")
            qualified = self.quote(table.namespace) + "." + self.quote(table.name)
            for name in self.principals:
                action, direction = ("GRANT", "TO") if enabled else ("REVOKE", "FROM")
                self.command(
                    f"{action} SELECT, INSERT ON {qualified} {direction} {self.quote(name)}"
                )
        for table in tables:
            qualified = self.quote(table.namespace) + "." + self.quote(table.name)
            for name in self.principals:
                if self.dialect == "postgres":
                    rights = self.rows(
                        "SELECT has_table_privilege(%s,to_regclass(%s),'SELECT'),"
                        "has_table_privilege(%s,to_regclass(%s),'INSERT')",
                        [name, qualified, name, qualified],
                    )[0]
                else:
                    memberships = self.rows(
                        "SELECT count() FROM system.role_grants WHERE user_name={user:String}",
                        {"user": name},
                    )[0][0]
                    if memberships:
                        raise MigrationRefused(
                            "runtime memberships changed during access transition"
                        )
                    grants = self.rows(
                        "SELECT access_type FROM system.grants WHERE user_name={user:String} "
                        "AND database={database:String} AND table={table:String} "
                        "AND column IS NULL AND is_partial_revoke=0",
                        {"user": name, "database": table.namespace, "table": table.name},
                    )
                    present = {str(row[0]) for row in grants}
                    rights = [kind in present for kind in ("SELECT", "INSERT")]
                if list(rights) != [enabled, enabled]:
                    raise MigrationRefused(
                        "runtime SELECT/INSERT grants did not reach the expected state"
                    )
        for probe in self.runtime:
            for table in tables:
                query = f"SELECT 1 FROM {self.quote(table.name)} LIMIT 0"
                try:
                    if self.dialect == "postgres":
                        probe._cx.execute(query)
                    else:
                        probe._cx.query(query)
                except Exception as exc:
                    denied = (
                        getattr(exc, "sqlstate", None) == "42501"
                        if self.dialect == "postgres"
                        else getattr(exc, "code", None) == 497
                    )
                    if enabled or not denied:
                        raise EngineError(
                            "runtime access probe did not establish the expected permissions"
                        ) from exc
                else:
                    if not enabled:
                        raise MigrationRefused(
                            "runtime still reads a table after its grants were revoked"
                        )

    def truncate(self, tables: Sequence[TableIdentity]) -> None:
        for table in tables:
            if self.identity(table.name).physical_key != table.physical_key:
                raise MigrationRefused("physical target identity changed before repair")
        names = [self.quote(table.namespace) + "." + self.quote(table.name) for table in tables]
        if self.dialect == "postgres":
            self.command("TRUNCATE TABLE " + ", ".join(names))
        else:
            for name in names:
                self.command("TRUNCATE TABLE " + name + " SYNC")

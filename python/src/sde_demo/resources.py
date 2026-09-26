"""Durable ownership of disposable loopback demo namespaces and runtime logins.

This is a local demo facility, not a general database administrator. Its directory lock is shared
with the starter. External schema/access administration must not race these local operations.
Passwords and DSNs live only in the two private credential files, never in the manifest or errors.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import ipaddress
import json
import re
import secrets
import stat
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit

from sde import _local_state
from sde.engines._storage import STORAGE_COLUMNS

FILES = {"operator": "operator-credentials.json", "runtime": "runtime-credentials.json"}
_FIELDS = {
    "protocol",
    "allocation_id",
    "status",
    "credential_files",
    "credential_hashes",
    "engines",
    "digest",
}
_ENTRY_FIELDS = {
    "dialect",
    "namespace",
    "runtime_user",
    "owner_marker",
    "endpoint",
    "expected_uuid",
    "namespace_identity",
    "runtime_identity",
    "phase",
    "tables",
}
_PHASES = {
    "planned",
    "namespace_created",
    "created",
    "ready",
    "drop_namespace",
    "namespace_dropped",
    "user_dropped",
    "drop_user",
    "reset",
}


class ResourceRefused(RuntimeError):
    """The demo cannot prove that a native resource or local file still belongs to this run."""


def _after_step(_step: str) -> None:
    """Fault-injection seam, including after native CREATE/DROP and before identity publication."""


def _encode(value: Any) -> bytes:
    return (
        json.dumps(
            value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        )
        + "\n"
    ).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_encode(value)).hexdigest()


def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, value in items:
        if name in result:
            raise ResourceRefused("demo state contains duplicate JSON keys")
        result[name] = value
    return result


def _read(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ResourceRefused("demo state or credentials are missing; restore verified state")
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise ResourceRefused("demo state and credential files must have mode 0600")
    value = json.loads(path.read_bytes(), object_pairs_hook=_pairs)
    if not isinstance(value, dict):
        raise ResourceRefused("demo state must be an object")
    return value


def _write(root: Path, record: dict[str, Any], *, replace: bool = True) -> None:
    record["digest"] = _digest({key: value for key, value in record.items() if key != "digest"})
    _local_state.write_bytes(root / "resources.json", _encode(record), replace=replace)


def _engines(engines: Mapping[str, str]) -> dict[str, str]:
    if not isinstance(engines, Mapping) or not 1 <= len(engines) <= 2:
        raise ResourceRefused("demo needs one or two named engines")
    if any(
        not isinstance(name, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", name) is None
        or dialect not in ("postgres", "clickhouse")
        for name, dialect in engines.items()
    ):
        raise ResourceRefused("demo engine names must be ASCII and dialects PostgreSQL/ClickHouse")
    if len(set(engines.values())) != len(engines):
        raise ResourceRefused("demo permits only one binding per dialect")
    return dict(sorted(engines.items()))


def _load(root: Path) -> dict[str, Any] | None:
    path = root / "resources.json"
    if not path.exists() and not path.is_symlink():
        if any((root / name).exists() or (root / name).is_symlink() for name in FILES.values()):
            raise ResourceRefused("demo credential files have lost their ownership manifest")
        return None
    record = _read(path)
    if (
        set(record) != _FIELDS
        or type(record["protocol"]) is not int
        or record["protocol"] != 1
        or record["digest"] != _digest({k: v for k, v in record.items() if k != "digest"})
        or record["credential_files"] != FILES
        or record["status"] not in ("allocating", "ready", "resetting", "reset")
        or re.fullmatch(r"[0-9a-f]{32}", record["allocation_id"]) is None
    ):
        raise ResourceRefused("demo resource manifest is corrupt or unsupported")
    entries = record["engines"]
    _engines({name: entry["dialect"] for name, entry in entries.items()})
    for entry in entries.values():
        if (
            set(entry) != _ENTRY_FIELDS
            or entry["phase"] not in _PHASES
            or re.fullmatch(r"sde_demo_[0-9a-f]{32}_[0-9]+", entry["namespace"]) is None
            or entry["runtime_user"] != entry["namespace"] + "_app"
            or entry["owner_marker"] != "sde-demo-v1:" + record["allocation_id"]
            or not isinstance(entry["tables"], dict)
        ):
            raise ResourceRefused("demo resource entry has invalid ownership metadata")
        if entry["phase"] in {"created", "ready"} and (
            entry["namespace_identity"] is None or entry["runtime_identity"] is None
        ):
            raise ResourceRefused("demo resource history has lost a native identity")
        if record["status"] == "ready" and entry["phase"] != "ready":
            raise ResourceRefused("ready demo has incomplete resource history")
        if record["status"] == "reset" and entry["phase"] != "reset":
            raise ResourceRefused("reset demo has incomplete resource history")
    _local_state.confirm_file(path)
    return record


def _host(value: str | None) -> str:
    if value == "localhost":
        return "127.0.0.1"
    try:
        address = ipaddress.ip_address(value or "")
    except ValueError:
        raise ResourceRefused("demo bindings must use an explicit loopback address") from None
    if not address.is_loopback:
        raise ResourceRefused("demo refuses non-loopback bindings")
    return str(address)


def _pg_uri(parts: Mapping[str, str | int | None]) -> str:
    values = {name: str(value) for name, value in parts.items() if value is not None}
    host = _host(values.pop("host", None))
    authority = "[" + host + "]" if ":" in host else host
    port = values.pop("port", "5432")
    if not port.isdigit() or not 1 <= int(port) <= 65535:
        raise ResourceRefused("demo PostgreSQL port must be one TCP port")
    user, password, database = (values.pop(name, "") for name in ("user", "password", "dbname"))
    if not user or not database:
        raise ResourceRefused("demo PostgreSQL binding requires explicit user and database")
    return (
        f"postgresql://{quote(user, safe='')}:{quote(password, safe='')}@{authority}:{port}/"
        f"{quote(database, safe='')}?{urlencode(sorted(values.items()))}"
    )


def _normalize(dialect: str, dsn: str) -> str:
    if not isinstance(dsn, str) or not dsn:
        raise ResourceRefused("demo administrator binding is absent")
    if dialect == "postgres":
        from psycopg.conninfo import conninfo_to_dict

        parts = conninfo_to_dict(dsn)
        if set(parts) - {
            "host",
            "port",
            "user",
            "password",
            "dbname",
            "options",
            "sslmode",
            "connect_timeout",
            "application_name",
        }:
            raise ResourceRefused("demo PostgreSQL binding contains unsupported routing options")
        return _pg_uri(parts)
    url = urlsplit(dsn)
    if url.scheme not in ("clickhouse", "clickhouses", "http", "https") or url.fragment:
        raise ResourceRefused("demo ClickHouse binding must be an explicit HTTP(S) DSN")
    host = _host(url.hostname)
    secure = url.scheme in ("clickhouses", "https")
    port = url.port or (8443 if secure else 8123)
    query = dict(parse_qsl(url.query, strict_parsing=True))
    if set(query) - {"connect_timeout", "send_receive_timeout"}:
        raise ResourceRefused("demo ClickHouse binding contains unsupported routing options")
    if not url.username or not url.path.removeprefix("/"):
        raise ResourceRefused("demo ClickHouse binding requires explicit user and database")
    authority = "[" + host + "]" if ":" in host else host
    authority = (
        f"{quote(unquote(url.username), safe='')}:"
        f"{quote(unquote(url.password or ''), safe='')}@{authority}:{port}"
    )
    return urlunsplit(
        (
            "clickhouses" if secure else "clickhouse",
            authority,
            url.path,
            urlencode(sorted(query.items())),
            "",
        )
    )


def _scoped(
    dialect: str,
    admin: str,
    namespace: str,
    *,
    user: str | None = None,
    password: str | None = None,
) -> str:
    if dialect == "postgres":
        from psycopg.conninfo import conninfo_to_dict

        parts = conninfo_to_dict(admin)
        parts["options"] = "-csearch_path=" + namespace
        if user is not None:
            parts.update(user=user, password=password or "")
        return _pg_uri(parts)
    url = urlsplit(admin)
    authority = url.netloc
    if user is not None:
        host = "[" + str(url.hostname) + "]" if ":" in str(url.hostname) else str(url.hostname)
        authority = f"{quote(user, safe='')}:{quote(password or '', safe='')}@{host}:{url.port}"
    return urlunsplit((url.scheme, authority, "/" + quote(namespace, safe=""), url.query, ""))


@contextmanager
def _errors() -> Iterator[None]:
    try:
        yield
    except (ResourceRefused, _local_state.DurabilityUncertain):
        raise
    except Exception:
        # Driver errors can contain the complete DSN or a truncated CREATE USER password.
        raise ResourceRefused(
            "local demo operation failed; preserve its files and retry or restore"
        ) from None


@contextmanager
def _connections(
    bindings: Mapping[str, str], admin_dsns: Mapping[str, str]
) -> Iterator[dict[str, Any]]:
    from sde.engines.clickhouse import ClickHouseEngine
    from sde.engines.postgres import PostgresEngine

    if not isinstance(admin_dsns, Mapping) or set(bindings.values()) - set(admin_dsns):
        raise ResourceRefused("demo needs administrator bindings for its dialects")
    normalized = {
        dialect: _normalize(dialect, admin_dsns[dialect]) for dialect in set(bindings.values())
    }
    with ExitStack() as stack:
        result = {}
        for name, dialect in sorted(bindings.items()):
            engine = (PostgresEngine if dialect == "postgres" else ClickHouseEngine)(
                normalized[dialect]
            )
            stack.callback(engine.close)
            engine.connect()
            result[name] = engine
        yield result


def _endpoint(engine: Any, *, database_name: str | None = None) -> dict[str, str]:
    if engine.dialect == "postgres":
        row = engine._cx.execute(
            "SELECT c.system_identifier::text,d.oid::text,pg_catalog.current_database() "
            "FROM pg_catalog.pg_control_system() c CROSS JOIN pg_catalog.pg_database d "
            "WHERE d.datname=pg_catalog.current_database()"
        ).fetchone()
    else:
        condition = "name=currentDatabase()" if database_name is None else "name={database:String}"
        rows = engine._cx.query(
            "SELECT toString(serverUUID()),toString(uuid),name FROM system.databases WHERE "
            + condition,
            parameters={} if database_name is None else {"database": database_name},
        ).result_rows
        row = rows[0] if len(rows) == 1 else None
    if row is None or len(row) != 3 or any(not str(value) for value in row):
        raise ResourceRefused("demo native endpoint identity is unavailable")
    return dict(zip(("server", "database", "database_name"), map(str, row), strict=True))


def _namespace(engine: Any, entry: Mapping[str, Any]) -> dict[str, str] | None:
    if engine.dialect == "postgres":
        row = engine._cx.execute(
            "SELECT oid::text,nspowner::text,pg_catalog.obj_description(oid,'pg_namespace') "
            "FROM pg_catalog.pg_namespace WHERE nspname=%s",
            (entry["namespace"],),
        ).fetchone()
        return None if row is None else {"id": row[0], "owner": row[1], "marker": row[2]}
    rows = engine._cx.query(
        "SELECT toString(uuid),engine,comment FROM system.databases WHERE name={name:String}",
        parameters={"name": entry["namespace"]},
    ).result_rows
    return None if not rows else {"id": rows[0][0], "engine": rows[0][1], "marker": rows[0][2]}


def _principal(engine: Any, entry: Mapping[str, Any]) -> dict[str, str] | None:
    if engine.dialect == "postgres":
        row = engine._cx.execute(
            "SELECT oid::text,pg_catalog.shobj_description(oid,'pg_authid') "
            "FROM pg_catalog.pg_roles WHERE rolname=%s",
            (entry["runtime_user"],),
        ).fetchone()
        return None if row is None else {"id": row[0], "marker": row[1]}
    rows = engine._cx.query(
        "SELECT toString(id),storage,default_database FROM system.users WHERE name={name:String}",
        parameters={"name": entry["runtime_user"]},
    ).result_rows
    return (
        None
        if not rows
        else {"id": rows[0][0], "storage": rows[0][1], "default_database": rows[0][2]}
    )


def _authentication(admin: str, user: str, password: str) -> tuple[int, str | None, list[str]]:
    url = urlsplit(admin)
    connection = (
        http.client.HTTPSConnection if url.scheme == "clickhouses" else http.client.HTTPConnection
    )(
        str(url.hostname),
        url.port,
        timeout=10,
    )
    try:
        authorization = base64.b64encode((user + ":" + password).encode()).decode()
        connection.request(
            "POST",
            "/?" + urlencode({"database": unquote(url.path.removeprefix("/"))}),
            body="SELECT currentUser(), toString(serverUUID()) FORMAT TabSeparated",
            headers={"Authorization": "Basic " + authorization},
        )
        response = connection.getresponse()
        data = response.read(65_537)
        identity = data.decode("utf-8").strip().split("\t") if response.status == 200 else []
        return response.status, response.getheader("X-ClickHouse-Exception-Code"), identity
    finally:
        connection.close()


def _prove_ch_user(engine: Any, entry: Mapping[str, Any], runtime_dsn: str) -> None:
    password = unquote(urlsplit(runtime_dsn).password or "")
    correct = _authentication(engine._dsn, entry["runtime_user"], password)
    wrong = _authentication(engine._dsn, entry["runtime_user"], password + "-wrong")
    if (
        correct[0] != 200
        or correct[2] != [entry["runtime_user"], entry["endpoint"]["server"]]
        or wrong[0] != 403
        or wrong[1] != "516"
    ):
        raise ResourceRefused("demo runtime login does not prove ownership of its saved credential")


def _table(engine: Any, entry: Mapping[str, Any], name: str) -> dict[str, str] | None:
    if engine.dialect == "postgres":
        row = engine._cx.execute(
            "SELECT c.oid::text,c.relowner::text,c.relkind FROM pg_catalog.pg_class c "
            "JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname=%s AND c.relname=%s",
            (entry["namespace"], name),
        ).fetchone()
        if row is None:
            return None
        if row[2] != "r" or row[1] != entry["namespace_identity"]["owner"]:
            raise ResourceRefused("demo can grant/reset only its ordinary owned tables")
        return {"id": row[0], "owner": row[1]}
    rows = engine._cx.query(
        "SELECT toString(uuid),engine FROM system.tables "
        "WHERE database={database:String} AND name={table:String}",
        parameters={"database": entry["namespace"], "table": name},
    ).result_rows
    if not rows:
        return None
    if rows[0][1] not in ("MergeTree", "ReplacingMergeTree"):
        raise ResourceRefused("demo can grant only local MergeTree tables")
    return {"id": rows[0][0], "engine": rows[0][1]}


def _observe(engine: Any, entry: Mapping[str, Any], runtime_dsn: str | None) -> tuple[Any, Any]:
    if _endpoint(engine) != entry["endpoint"]:
        raise ResourceRefused("demo administrator binding now resolves to another native endpoint")
    namespace, principal = _namespace(engine, entry), _principal(engine, entry)
    phase = entry["phase"]
    resetting = {"drop_namespace", "namespace_dropped", "drop_user", "user_dropped", "reset"}
    namespace_gone = {"namespace_dropped", "reset"}
    user_gone = {"user_dropped", "reset"}
    if engine.dialect == "postgres":
        namespace_gone.update({"drop_user", "user_dropped"})
    else:
        user_gone.update({"drop_namespace", "namespace_dropped"})
    if namespace is not None:
        if phase in namespace_gone:
            raise ResourceRefused("a dropped demo namespace has reappeared")
        if phase in resetting and entry["namespace_identity"] is None:
            raise ResourceRefused("an absent demo namespace appeared after reset intent")
        if namespace["marker"] != entry["owner_marker"] or (
            engine.dialect == "clickhouse"
            and (namespace["id"] != entry["expected_uuid"] or namespace["engine"] != "Atomic")
        ):
            raise ResourceRefused("demo namespace ownership marker or UUID differs")
        if entry["namespace_identity"] is not None and namespace != entry["namespace_identity"]:
            raise ResourceRefused("demo namespace native identity changed")
    elif entry["namespace_identity"] is not None and phase not in namespace_gone | {
        "drop_namespace"
    }:
        raise ResourceRefused("a recorded demo namespace is missing")
    if principal is not None:
        if phase in user_gone:
            raise ResourceRefused("a dropped demo runtime has reappeared")
        if phase in resetting and entry["runtime_identity"] is None:
            raise ResourceRefused("an absent demo runtime appeared after reset intent")
        if entry["runtime_identity"] is not None and principal != entry["runtime_identity"]:
            raise ResourceRefused("demo runtime native identity changed")
        if engine.dialect == "postgres":
            if principal["marker"] != entry["owner_marker"]:
                raise ResourceRefused("demo runtime ownership marker differs")
        else:
            if (
                principal["storage"] != "local_directory"
                or principal["default_database"] != entry["namespace"]
                or runtime_dsn is None
            ):
                raise ResourceRefused("demo runtime ownership metadata differs")
            _prove_ch_user(engine, entry, runtime_dsn)
    elif entry["runtime_identity"] is not None and phase not in user_gone | {"drop_user"}:
        raise ResourceRefused("a recorded demo runtime login is missing")
    if phase == "planned" and (
        (engine.dialect == "postgres" and (namespace is None) != (principal is None))
        or (engine.dialect == "clickhouse" and principal is not None)
    ):
        raise ResourceRefused("demo creation history contradicts native resources")
    if namespace is not None:
        for name, granted in entry["tables"].items():
            actual = _table(engine, entry, name)
            if actual != granted["identity"] and not (actual is None and phase == "drop_namespace"):
                raise ResourceRefused("a recorded demo table was removed or replaced")
    return namespace, principal


def _credential_values(
    root: Path, record: dict[str, Any], connections: Mapping[str, Any]
) -> dict[str, dict[str, str]]:
    hashes = record["credential_hashes"]
    if hashes is None:
        if any(
            _namespace(engine, record["engines"][name]) is not None
            or _principal(engine, record["engines"][name]) is not None
            for name, engine in connections.items()
        ):
            raise ResourceRefused("native demo resources exist without durable credential history")
        values: dict[str, dict[str, str]] = {"operator": {}, "runtime": {}}
        for name, engine in connections.items():
            entry = record["engines"][name]
            values["operator"][name] = _scoped(engine.dialect, engine._dsn, entry["namespace"])
        for kind in FILES:
            path = root / FILES[kind]
            if path.exists() or path.is_symlink():
                stored = _read(path)
                if kind == "operator" and stored != values[kind]:
                    raise ResourceRefused("unfinished demo credentials conflict with its bindings")
                values[kind] = stored
            elif kind == "runtime":
                values[kind] = {
                    name: _scoped(
                        engine.dialect,
                        engine._dsn,
                        record["engines"][name]["namespace"],
                        user=record["engines"][name]["runtime_user"],
                        password=secrets.token_hex(32),
                    )
                    for name, engine in connections.items()
                }
            if not path.exists():
                _local_state.write_bytes(path, _encode(values[kind]), replace=False)
                _after_step("credential_written:" + kind)
        _validate_credentials(values, record, connections)
        record["credential_hashes"] = {
            kind: hashlib.sha256((root / file).read_bytes()).hexdigest()
            for kind, file in FILES.items()
        }
        for file in FILES.values():
            _local_state.confirm_file(root / file)
        _write(root, record)
        _after_step("credentials_ready")
        return values
    if not isinstance(hashes, dict) or set(hashes) != set(FILES):
        raise ResourceRefused("demo credential fingerprint history is incomplete")
    values = {}
    for kind, file in FILES.items():
        path = root / file
        values[kind] = _read(path)
        if hashlib.sha256(path.read_bytes()).hexdigest() != hashes[kind]:
            raise ResourceRefused("demo credentials changed; restore the original files")
        _local_state.confirm_file(path)
    _validate_credentials(values, record, connections)
    return values


def _validate_credentials(
    values: Mapping[str, Any], record: Mapping[str, Any], connections: Mapping[str, Any]
) -> None:
    for kind in FILES:
        if set(values[kind]) != set(record["engines"]):
            raise ResourceRefused("demo credentials do not cover the exact bindings")
        for name, engine in connections.items():
            entry = record["engines"][name]
            dsn = values[kind][name]
            normalized = _normalize(engine.dialect, dsn)
            url, admin = urlsplit(normalized), urlsplit(engine._dsn)
            if (url.hostname, url.port) != (admin.hostname, admin.port):
                raise ResourceRefused("demo credential binding points to another endpoint")
            if engine.dialect == "postgres":
                from psycopg.conninfo import conninfo_to_dict

                options = conninfo_to_dict(normalized)
                if (
                    options["dbname"] != entry["endpoint"]["database_name"]
                    or options.get("options") != "-csearch_path=" + entry["namespace"]
                ):
                    raise ResourceRefused("demo PostgreSQL credentials name another namespace")
            elif unquote(url.path.removeprefix("/")) != entry["namespace"]:
                raise ResourceRefused("demo ClickHouse credentials name another namespace")
            if kind == "runtime" and (
                unquote(url.username or "") != entry["runtime_user"]
                or re.fullmatch(r"[0-9a-f]{64}", unquote(url.password or "")) is None
            ):
                raise ResourceRefused("demo runtime credential ownership is invalid")


def _new_record(bindings: Mapping[str, str], connections: Mapping[str, Any]) -> dict[str, Any]:
    identity = uuid.uuid4().hex
    entries = {}
    for index, (name, dialect) in enumerate(bindings.items()):
        namespace = f"sde_demo_{identity}_{index}"
        entries[name] = {
            "dialect": dialect,
            "namespace": namespace,
            "runtime_user": namespace + "_app",
            "owner_marker": "sde-demo-v1:" + identity,
            "endpoint": _endpoint(connections[name]),
            "expected_uuid": str(uuid.uuid4()) if dialect == "clickhouse" else None,
            "namespace_identity": None,
            "runtime_identity": None,
            "phase": "planned",
            "tables": {},
        }
    return {
        "protocol": 1,
        "allocation_id": identity,
        "status": "allocating",
        "credential_files": dict(FILES),
        "credential_hashes": None,
        "engines": entries,
    }


def _create_pg(engine: Any, entry: Mapping[str, Any], password: str) -> None:
    from psycopg import sql

    with engine._cx.transaction():
        engine._cx.execute(
            sql.SQL("CREATE ROLE {} LOGIN NOINHERIT PASSWORD {}").format(
                sql.Identifier(entry["runtime_user"]), sql.Literal(password)
            )
        )
        engine._cx.execute(
            sql.SQL("COMMENT ON ROLE {} IS {}").format(
                sql.Identifier(entry["runtime_user"]), sql.Literal(entry["owner_marker"])
            )
        )
        engine._cx.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(entry["namespace"])))
        engine._cx.execute(
            sql.SQL("COMMENT ON SCHEMA {} IS {}").format(
                sql.Identifier(entry["namespace"]), sql.Literal(entry["owner_marker"])
            )
        )
        engine._cx.execute(
            sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                sql.Identifier(entry["namespace"]), sql.Identifier(entry["runtime_user"])
            )
        )


def allocate(
    root: Path, engines: Mapping[str, str], admin_dsns: Mapping[str, str]
) -> dict[str, Any]:
    """Allocate or verify this run's resources; ready allocations never restore grants."""
    bindings = _engines(engines)
    with (
        _errors(),
        _connections(bindings, admin_dsns) as connections,
        _local_state.transaction(root),
    ):
        record = _load(root)
        if record is not None:
            if {name: entry["dialect"] for name, entry in record["engines"].items()} != bindings:
                raise ResourceRefused("demo engine bindings changed; use the original allocation")
            if record["status"] == "resetting":
                raise ResourceRefused("finish the demo reset before allocating again")
            if record["status"] == "reset":
                for name, entry in record["engines"].items():
                    _observe(connections[name], entry, None)
                _remove_credentials(root, record)
                record = None
        if record is None:
            record = _new_record(bindings, connections)
            _write(root, record, replace=(root / "resources.json").exists())
            _after_step("intent_written")
        for name, entry in record["engines"].items():
            if _endpoint(connections[name]) != entry["endpoint"]:
                raise ResourceRefused("demo native endpoint changed")
        credentials = _credential_values(root, record, connections)
        for name, entry in record["engines"].items():
            _observe(connections[name], entry, credentials["runtime"][name])
        if record["status"] == "ready":
            return deepcopy(record)
        for name, entry in record["engines"].items():
            engine = connections[name]
            runtime = credentials["runtime"][name]
            namespace, principal = _observe(engine, entry, runtime)
            password = unquote(urlsplit(runtime).password or "")
            if entry["phase"] == "planned":
                if namespace is None:
                    if engine.dialect == "postgres":
                        _create_pg(engine, entry, password)
                    else:
                        engine._cx.command(
                            f"CREATE DATABASE `{entry['namespace']}` UUID "
                            f"'{entry['expected_uuid']}' ENGINE=Atomic "
                            f"COMMENT '{entry['owner_marker']}'"
                        )
                    _after_step("namespace_created:" + name)
                namespace, principal = _observe(engine, entry, runtime)
                entry["namespace_identity"] = namespace
                entry["runtime_identity"] = principal
                entry["phase"] = "created" if engine.dialect == "postgres" else "namespace_created"
                _write(root, record)
                _after_step("namespace_recorded:" + name)
            if entry["phase"] == "namespace_created":
                if principal is None:
                    engine._cx.command(
                        f"CREATE USER `{entry['runtime_user']}` IDENTIFIED WITH "
                        f"sha256_password BY '{password}' DEFAULT DATABASE `{entry['namespace']}`"
                    )
                    _after_step("runtime_created:" + name)
                _, principal = _observe(engine, entry, runtime)
                entry["runtime_identity"] = principal
                entry["phase"] = "created"
                _write(root, record)
                _after_step("runtime_recorded:" + name)
            if entry["phase"] == "created":
                _observe(engine, entry, runtime)
                if engine.dialect == "clickhouse":
                    engine._cx.command(
                        f"GRANT SELECT ON system.settings TO `{entry['runtime_user']}`"
                    )
                    # What a storage measurement reads, and nothing more: ClickHouse refuses
                    # system.parts to a login with table grants alone, and this column grant shows
                    # it the parts of its own tables only. The operator's qualification admits it.
                    engine._cx.command(
                        f"GRANT SELECT({', '.join(STORAGE_COLUMNS)}) ON system.parts "
                        f"TO `{entry['runtime_user']}`"
                    )
                entry["phase"] = "ready"
                _write(root, record)
                _after_step("engine_ready:" + name)
        record["status"] = "ready"
        _write(root, record)
        _after_step("ready")
        return deepcopy(record)


def verify(root: Path) -> dict[str, Any]:
    """Verify a ready allocation using its saved local operator credentials, without provisioning.

    This proves the saved native incarnations and ownership markers. It deliberately does not
    restore or qualify access grants: cutover can have revoked them since initial provisioning.
    """
    with _errors(), _local_state.transaction(root):
        record = _load(root)
        if record is None or record["status"] != "ready":
            raise ResourceRefused("demo must be fully allocated before verifying its resources")
        hashes = record["credential_hashes"]
        if not isinstance(hashes, dict) or set(hashes) != set(FILES):
            raise ResourceRefused("demo credential fingerprint history is incomplete")
        operator_path = root / FILES["operator"]
        operators = _read(operator_path)
        if (
            set(operators) != set(record["engines"])
            or hashlib.sha256(operator_path.read_bytes()).hexdigest() != hashes["operator"]
        ):
            raise ResourceRefused("demo operator credentials changed; restore the original file")
        bindings = {name: entry["dialect"] for name, entry in record["engines"].items()}
        admin = {dialect: operators[name] for name, dialect in bindings.items()}
        with _connections(bindings, admin) as connections:
            credentials = _credential_values(root, record, connections)
            for name, entry in record["engines"].items():
                observed = entry
                if entry["dialect"] == "clickhouse":
                    # Allocation began through the administrator's base database. The saved
                    # operator URI now opens our own database; check both native identities.
                    if (
                        _endpoint(
                            connections[name], database_name=entry["endpoint"]["database_name"]
                        )
                        != entry["endpoint"]
                    ):
                        raise ResourceRefused("demo native endpoint changed")
                    observed = {
                        **entry,
                        "endpoint": {
                            "server": entry["endpoint"]["server"],
                            "database": entry["namespace_identity"]["id"],
                            "database_name": entry["namespace"],
                        },
                    }
                _observe(connections[name], observed, credentials["runtime"][name])
        return deepcopy(record)


def _table_name(name: Any) -> str:
    if (
        not isinstance(name, str)
        or not name
        or len(name.encode("utf-8")) > 255
        or any(ord(char) < 32 for char in name)
        or name in ("*", "ALL TABLES")
    ):
        raise ResourceRefused("demo grants require explicit table names")
    return name


def _grant(engine: Any, entry: Mapping[str, Any], table: str) -> None:
    if engine.dialect == "postgres":
        from psycopg import sql

        engine._cx.execute(
            sql.SQL("GRANT SELECT, INSERT ON {}.{} TO {}").format(
                sql.Identifier(entry["namespace"]),
                sql.Identifier(table),
                sql.Identifier(entry["runtime_user"]),
            )
        )
    else:
        from sde.schema import QUOTE

        engine._cx.command(
            f"GRANT SELECT, INSERT ON `{entry['namespace']}`.{QUOTE['clickhouse'](table)} "
            f"TO `{entry['runtime_user']}`"
        )


def grant_tables(
    root: Path, tables: Mapping[str, Sequence[str]], admin_dsns: Mapping[str, str]
) -> None:
    """Grant only listed existing tables, binding each grant to its native table identity."""
    with _errors(), _local_state.transaction(root):
        record = _load(root)
        if record is None or record["status"] != "ready":
            raise ResourceRefused("demo must be fully allocated before granting tables")
        if not isinstance(tables, Mapping) or set(tables) - set(record["engines"]):
            raise ResourceRefused("demo grants refer to unknown engines")
        requested = {}
        for name, names in sorted(tables.items()):
            if isinstance(names, (str, bytes)) or not isinstance(names, Sequence):
                raise ResourceRefused("demo grants require a sequence of table names")
            requested[name] = sorted({_table_name(table) for table in names})
        bindings = {name: entry["dialect"] for name, entry in record["engines"].items()}
        with _connections(bindings, admin_dsns) as connections:
            credentials = _credential_values(root, record, connections)
            for name, entry in record["engines"].items():
                _observe(connections[name], entry, credentials["runtime"][name])
            identities = {}
            for name, names in requested.items():
                for table in names:
                    identity = _table(connections[name], record["engines"][name], table)
                    if identity is None:
                        raise ResourceRefused("a requested demo table does not exist")
                    identities[name, table] = identity
            for (name, table), identity in identities.items():
                entry = record["engines"][name]
                prior = entry["tables"].get(table)
                if prior is not None and prior["identity"] != identity:
                    raise ResourceRefused("a demo grant names a replacement table")
                entry["tables"][table] = {"identity": identity, "phase": "granting"}
                _write(root, record)
                _after_step("grant_intent:" + name + ":" + table)
                _observe(connections[name], entry, credentials["runtime"][name])
                _grant(connections[name], entry, table)
                _after_step("grant_applied:" + name + ":" + table)
                _observe(connections[name], entry, credentials["runtime"][name])
                entry["tables"][table]["phase"] = "granted"
                _write(root, record)


def _drop_namespace(engine: Any, entry: Mapping[str, Any]) -> None:
    if engine.dialect == "postgres":
        from psycopg import sql

        with engine._cx.transaction():
            rows = engine._cx.execute(
                "SELECT c.relname,c.relkind,c.relowner::text FROM pg_catalog.pg_class c "
                "JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
                "WHERE n.nspname=%s AND c.relkind NOT IN ('i','I') ORDER BY c.relname",
                (entry["namespace"],),
            ).fetchall()
            if any(
                kind != "r" or owner != entry["namespace_identity"]["owner"]
                for _, kind, owner in rows
            ):
                raise ResourceRefused(
                    "demo namespace contains unsupported or foreign-owned objects"
                )
            for table, _, _ in rows:
                engine._cx.execute(
                    sql.SQL("DROP TABLE {}.{}").format(
                        sql.Identifier(entry["namespace"]), sql.Identifier(table)
                    )
                )
            engine._cx.execute(sql.SQL("DROP SCHEMA {}").format(sql.Identifier(entry["namespace"])))
    else:
        dependencies = engine._cx.query(
            "SELECT dependencies_database FROM system.tables WHERE database={database:String}",
            parameters={"database": entry["namespace"]},
        ).result_rows
        if any(database != entry["namespace"] for row in dependencies for database in row[0]):
            raise ResourceRefused("demo tables have dependents outside their namespace")
        engine._cx.command(f"DROP DATABASE `{entry['namespace']}` SYNC")


def _drop_user(engine: Any, entry: Mapping[str, Any]) -> None:
    if engine.dialect == "postgres":
        from psycopg import sql

        engine._cx.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(entry["runtime_user"])))
    else:
        engine._cx.command(f"DROP USER `{entry['runtime_user']}`")


def _remove_credentials(root: Path, record: Mapping[str, Any]) -> None:
    for kind, file in FILES.items():
        path = root / file
        if path.exists() or path.is_symlink():
            _read(path)
            if (
                record["credential_hashes"] is not None
                and hashlib.sha256(path.read_bytes()).hexdigest()
                != record["credential_hashes"][kind]
            ):
                raise ResourceRefused("reset refuses changed credential files")
            path.unlink()
            _local_state.sync_directory(root)


def reset(root: Path, admin_dsns: Mapping[str, str]) -> dict[str, Any]:
    """Remove only this manifest's native incarnations; retain an idempotent reset tombstone.

    PostgreSQL removes its namespace before its role, because grants depend on the role. In
    ClickHouse the user goes first: dropping its DEFAULT DATABASE prevents the password proof.
    """
    with _errors(), _local_state.transaction(root):
        record = _load(root)
        if record is None:
            raise ResourceRefused("no demo resource manifest exists")
        bindings = {name: entry["dialect"] for name, entry in record["engines"].items()}
        with _connections(bindings, admin_dsns) as connections:
            if record["status"] == "reset":
                for name, entry in record["engines"].items():
                    _observe(connections[name], entry, None)
                _remove_credentials(root, record)
                return deepcopy(record)
            credentials = _credential_values(root, record, connections)
            for name, entry in record["engines"].items():
                _observe(connections[name], entry, credentials["runtime"][name])
            record["status"] = "resetting"
            _write(root, record)
            _after_step("reset_intent")
            for name, entry in record["engines"].items():
                engine, runtime = connections[name], credentials["runtime"][name]
                order = (
                    ("namespace", "user") if engine.dialect == "postgres" else ("user", "namespace")
                )
                namespace, principal = _observe(engine, entry, runtime)
                if entry["phase"] == "reset":
                    continue
                if entry["phase"] not in {
                    "drop_namespace",
                    "namespace_dropped",
                    "drop_user",
                    "user_dropped",
                }:
                    entry["namespace_identity"], entry["runtime_identity"] = namespace, principal
                    entry["phase"] = "drop_" + order[0]
                    _write(root, record)
                    if order[0] == "user":
                        _after_step("user_drop_intent:" + name)
                for index, resource in enumerate(order):
                    intended, finished = "drop_" + resource, resource + "_dropped"
                    if entry["phase"] == intended:
                        namespace, principal = _observe(engine, entry, runtime)
                        exists = namespace if resource == "namespace" else principal
                        if exists is not None:
                            if resource == "namespace":
                                _drop_namespace(engine, entry)
                                _after_step("namespace_dropped:" + name)
                            else:
                                _drop_user(engine, entry)
                                _after_step("runtime_dropped:" + name)
                        remaining = (
                            _namespace(engine, entry)
                            if resource == "namespace"
                            else _principal(engine, entry)
                        )
                        if remaining is not None:
                            raise ResourceRefused("demo resource remains after DROP")
                        entry["phase"] = finished
                        _write(root, record)
                    if entry["phase"] == finished:
                        entry["phase"] = (
                            "drop_" + order[index + 1] if index + 1 < len(order) else "reset"
                        )
                        _write(root, record)
                        if entry["phase"] == "drop_user":
                            _after_step("user_drop_intent:" + name)
            record["status"] = "reset"
            _write(root, record)
            _after_step("reset_complete")
            _remove_credentials(root, record)
            return deepcopy(record)

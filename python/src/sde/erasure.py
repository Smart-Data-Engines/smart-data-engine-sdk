"""A person's rows erased by the local operator, from every table of the map in force holding them.

The library never deletes a row: there is no ``Session.delete``, and the write fences guard INSERT,
not DELETE. A client who has to honour a request to erase a person's data (GDPR, article 17) ran
SQL by hand and had to know where the copies were: the group's source, every copy the map keeps
beside it, the copy a migration is filling. This is that request as an operation of the operator
the client already runs, on its own login and on the map it already holds - so it is never ours to
run, and the controller still has no connection to make it with.

**It holds no write barrier.** A ClickHouse mutation on a large table takes minutes, and a barrier
closes writes to the whole group, so one person's erasure would be minutes of refused writes and an
interruption would leave the group closed. It runs under the project's lock instead, beside no
staging, cutover or index build of the project, deletes from the copies before the source, and then
counts what is left. A row written again meanwhile - by an application still writing that person -
is reported as ``incomplete``, not hidden. Every step is idempotent, so running the same erasure
again is how an interrupted one finishes.

**Physically, not only from reads.** PostgreSQL deletes the row, and VACUUM reclaims its space.
ClickHouse runs ``ALTER TABLE ... DELETE`` with ``mutations_sync = 2``, a mutation that rewrites the
parts holding the row; a lightweight ``DELETE FROM`` only masks it until the next merge. Every
version a ReplacingMergeTree kept of the key goes with it. Elasticsearch deletes the documents by
``_id``, which removes them from search at once and from the segments when they merge. Backups,
replicas outside the map and the engines' own logs are the client's retention, and out of reach.

**Tables outside the map.** A table a completed move retired, or a copy an aborted cutover left,
still holds the rows it had then. The plan lists every such table of this project that has the
entity's columns, with the rows that match; it does not erase them. Those tables are no longer the
entity's - another entity of the same shape could be in one - so removing their rows is a decision
the plan informs and the client takes, with the statements ``sde-operator erase`` prints for it.

**The receipt names no key.** Each key becomes an HMAC-SHA-256 under a secret kept in the project
directory (``erasure.key``, 32 random bytes, mode 0600), of the canonical bytes of
``{"entity": <entity>, "key": {<field>: <value as text>}}``. A plain hash of a small key - a number,
an e-mail address - is reversed by trying them all, and a receipt is meant to be handed to whoever
asks for proof. The client, holding the secret, can show years later that a given key was erased.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import secrets
import uuid
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from .canonical import canonical_bytes
from .errors import EngineError, MigrationRefused
from .model import LogicalModel
from .placement import Materialization, PlacementMap

if TYPE_CHECKING:
    from .local_cutover import LocalCutover

__all__ = [
    "ErasurePlan",
    "ErasureReceipt",
    "ErasureRequest",
    "ErasureTable",
    "erasure_secret",
]

KEY_TYPES = ("bool", "int32", "int64", "string", "uuid", "date", "timestamp", "timestamptz")
"""The neutral types a key can have here. A float key addresses no row exactly, and a key of bytes
or JSON is not one any layout declares."""

BATCH = 500
"""Keys per statement: one request is usually one person, and a list of thousands stays bounded."""

SECRET_FILE = "erasure.key"
RECEIPTS = "erasures"

Role = Literal["source", "copy", "outside"]


def _typed(kind: str, value: Any, where: str) -> Any:
    """One key value as the engines compare it, from Python or from JSON text."""
    if kind not in KEY_TYPES:
        raise MigrationRefused(f"{where}: a key of type {kind} cannot address rows to erase")
    if kind == "bool" and isinstance(value, bool):
        return value
    if kind in ("int32", "int64") and isinstance(value, int) and not isinstance(value, bool):
        bits = 31 if kind == "int32" else 63
        if -(2**bits) <= value < 2**bits:
            return value
    if kind == "string" and isinstance(value, str):
        return value
    if kind == "uuid":
        if isinstance(value, uuid.UUID):
            return value
        if isinstance(value, str):
            try:
                return uuid.UUID(value)
            except ValueError:
                pass
    if kind == "date":
        if isinstance(value, dt.date) and not isinstance(value, dt.datetime):
            return value
        if isinstance(value, str):
            try:
                return dt.date.fromisoformat(value)
            except ValueError:
                pass
    if kind in ("timestamp", "timestamptz"):
        moment = value
        if isinstance(value, str):
            try:
                moment = dt.datetime.fromisoformat(value)
            except ValueError:
                moment = None
        if isinstance(moment, dt.datetime):
            aware = moment.tzinfo is not None
            if kind == "timestamptz" and aware:
                return moment.astimezone(dt.UTC)
            if kind == "timestamp" and not aware:
                return moment
    raise MigrationRefused(f"{where}: {value!r} is not a {kind} value")


def _text(value: Any) -> Any:
    """A typed key value as canonical bytes carry it: the digest's input, spelled one way."""
    if isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, dt.datetime):
        return value.isoformat(timespec="microseconds")
    if isinstance(value, dt.date):
        return value.isoformat()
    raise MigrationRefused(f"a key value of type {type(value).__name__} has no text form here")


@dataclass(frozen=True)
class ErasureRequest:
    """One entity and the keys of the rows to erase, typed, distinct and in canonical order."""

    entity: str
    fields: tuple[str, ...]
    kinds: tuple[str, ...]
    keys: tuple[tuple[Any, ...], ...]

    @classmethod
    def build(
        cls, model: LogicalModel, entity: str, keys: Sequence[Mapping[str, Any]]
    ) -> ErasureRequest:
        try:
            spec = model.entity(entity)
        except Exception:
            raise MigrationRefused(f"the model has no entity {entity!r}") from None
        if not spec.key:
            raise MigrationRefused(f"{entity} has no key, so no row of it can be named to erase")
        if not keys:
            raise MigrationRefused("an erasure names at least one key")
        kinds = {field.name: field.type for field in spec.fields}
        distinct: dict[bytes, tuple[Any, ...]] = {}
        for index, key in enumerate(keys):
            if not isinstance(key, Mapping) or set(key) != set(spec.key):
                raise MigrationRefused(
                    f"key {index}: a key of {entity} has exactly the fields {list(spec.key)}"
                )
            typed = tuple(
                _typed(kinds[name], key[name], f"key {index}, field {name}") for name in spec.key
            )
            distinct[canonical_bytes([_text(value) for value in typed])] = typed
        return cls(
            entity,
            tuple(spec.key),
            tuple(kinds[name] for name in spec.key),
            tuple(distinct[spelled] for spelled in sorted(distinct)),
        )

    def digests(self, secret: bytes) -> tuple[str, ...]:
        """Each key as ``HMAC-SHA-256(secret, canonical {"entity", "key"})``, in hexadecimal."""
        return tuple(
            hmac.new(
                secret,
                canonical_bytes(
                    {
                        "entity": self.entity,
                        "key": {
                            name: _text(value)
                            for name, value in zip(self.fields, key, strict=True)
                        },
                    }
                ),
                hashlib.sha256,
            ).hexdigest()
            for key in self.keys
        )


@dataclass(frozen=True)
class ErasureTable:
    """One table that holds, or held, the entity: where it is, and the rows that match."""

    engine: str
    dialect: str
    table: str
    role: Role
    matching: int
    deleted: int | None = None
    remaining: int | None = None
    statements: tuple[str, ...] = ()

    def as_record(self) -> dict[str, Any]:
        record: dict[str, Any] = {
            "engine": self.engine,
            "dialect": self.dialect,
            "table": self.table,
            "role": self.role,
            "matching": self.matching,
        }
        if self.deleted is not None:
            record["deleted"] = self.deleted
        if self.remaining is not None:
            record["remaining"] = self.remaining
        if self.statements:
            record["statements"] = list(self.statements)
        return record


@dataclass(frozen=True)
class ErasurePlan:
    """What an erasure would delete, read now and changing nothing. Names no key."""

    request: ErasureRequest
    group: str
    map_version: int
    tables: tuple[ErasureTable, ...]
    outside: tuple[ErasureTable, ...]

    def as_record(self) -> dict[str, Any]:
        return {
            "entity": self.request.entity,
            "keys": len(self.request.keys),
            "group": self.group,
            "map_version": self.map_version,
            "tables": [table.as_record() for table in self.tables],
            "outside": [table.as_record() for table in self.outside],
        }


@dataclass(frozen=True)
class ErasureReceipt:
    """What an erasure did, with its keys as digests only. An observation, not a signature."""

    record: Mapping[str, Any]

    @property
    def outcome(self) -> str:
        return str(self.record["outcome"])

    def as_record(self) -> dict[str, Any]:
        copied: dict[str, Any] = json.loads(json.dumps(self.record))
        return copied


# --- the engines --------------------------------------------------------------------------------


class _Access:
    """Counting and deleting rows by key in one engine, through the operator's own connection."""

    def __init__(self, native: Any) -> None:
        self.native = native
        self.dialect: str = native.dialect

    def count(self, table: str, request: ErasureRequest, kinds: Mapping[str, str]) -> int:
        raise NotImplementedError

    def delete(self, table: str, request: ErasureRequest, kinds: Mapping[str, str]) -> int:
        raise NotImplementedError

    def columns(self, table: str) -> frozenset[str]:
        raise NotImplementedError

    def target(self, table: str) -> str:
        """The table as a statement names it."""
        return table

    def key_columns(self, request: ErasureRequest) -> str:
        return ", ".join(request.fields)


def _batches(keys: Sequence[tuple[Any, ...]]) -> list[Sequence[tuple[Any, ...]]]:
    return [keys[start : start + BATCH] for start in range(0, len(keys), BATCH)]


class _SqlAccess(_Access):
    def _name(self, table: str) -> str:
        identity = self.native.identity(table)
        return f"{self.native.quote(identity.namespace)}.{self.native.quote(identity.name)}"

    def _columns(self, request: ErasureRequest) -> str:
        return "(" + ", ".join(self.native.quote(name) for name in request.fields) + ")"

    def target(self, table: str) -> str:
        return self._name(table)

    def key_columns(self, request: ErasureRequest) -> str:
        return self._columns(request)


class _PostgresAccess(_SqlAccess):
    def _where(
        self, request: ErasureRequest, keys: Sequence[tuple[Any, ...]]
    ) -> tuple[str, list[Any]]:
        row = "(" + ", ".join(["%s"] * len(request.fields)) + ")"
        marks = ", ".join(row for _ in keys)
        return f"{self._columns(request)} IN ({marks})", [value for key in keys for value in key]

    def count(self, table: str, request: ErasureRequest, kinds: Mapping[str, str]) -> int:
        name, total = self._name(table), 0
        for keys in _batches(request.keys):
            where, values = self._where(request, keys)
            answer = self.native.rows(f"SELECT count(*) FROM {name} WHERE {where}", values)
            total += int(answer[0][0])
        return total

    def delete(self, table: str, request: ErasureRequest, kinds: Mapping[str, str]) -> int:
        name, total = self._name(table), 0
        for keys in _batches(request.keys):
            where, values = self._where(request, keys)
            gone = self.native.rows(
                f"WITH gone AS (DELETE FROM {name} WHERE {where} RETURNING 1) "
                f"SELECT count(*) FROM gone",
                values,
            )
            total += int(gone[0][0])
        return total

    def columns(self, table: str) -> frozenset[str]:
        identity = self.native.identity(table)
        rows = self.native.rows(
            "SELECT a.attname FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = %s AND c.relname = %s AND a.attnum > 0 AND NOT a.attisdropped",
            [identity.namespace, identity.name],
        )
        return frozenset(str(row[0]) for row in rows)


class _ClickHouseAccess(_SqlAccess):
    def _where(
        self, request: ErasureRequest, keys: Sequence[tuple[Any, ...]]
    ) -> tuple[str, dict[str, Any]]:
        from .engines.clickhouse import _query_parameter

        parameters: dict[str, Any] = {}
        rows = []
        for index, key in enumerate(keys):
            bound = [
                _query_parameter(f"k{index}_{position}", _clickhouse_value(value), parameters)
                for position, value in enumerate(key)
            ]
            rows.append("(" + ", ".join(bound) + ")")
        return f"{self._columns(request)} IN ({', '.join(rows)})", parameters

    def count(self, table: str, request: ErasureRequest, kinds: Mapping[str, str]) -> int:
        name, total = self._name(table), 0
        for keys in _batches(request.keys):
            where, parameters = self._where(request, keys)
            answer = self.native.rows(f"SELECT count() FROM {name} FINAL WHERE {where}", parameters)
            total += int(answer[0][0])
        return total

    def delete(self, table: str, request: ErasureRequest, kinds: Mapping[str, str]) -> int:
        # Every version a ReplacingMergeTree kept of a key is a row here, and FINAL counts one:
        # the number deleted is the keys' rows as the engine answers for them, before and after.
        before = self.count(table, request, kinds)
        name = self._name(table)
        for keys in _batches(request.keys):
            where, parameters = self._where(request, keys)
            try:
                self.native.engine._cx.command(
                    f"ALTER TABLE {name} DELETE WHERE {where}",
                    parameters=parameters,
                    settings={"mutations_sync": 2},
                )
            except Exception as exc:
                raise EngineError(f"erasing from {table} has an uncertain outcome: {exc}") from exc
        return before - self.count(table, request, kinds)

    def columns(self, table: str) -> frozenset[str]:
        identity = self.native.identity(table)
        rows = self.native.rows(
            "SELECT name FROM system.columns WHERE database = %(database)s AND table = %(table)s",
            {"database": identity.namespace, "table": identity.name},
        )
        return frozenset(str(row[0]) for row in rows)


def _clickhouse_value(value: Any) -> Any:
    # The driver formats a UUID and a date itself; a datetime goes through _query_parameter.
    return str(value) if isinstance(value, uuid.UUID) else value


class _ElasticsearchAccess(_Access):
    """Documents by ``_id``: the key's own address there (format contract 7m), exact by design."""

    def _ids(
        self, table: str, request: ErasureRequest, kinds: Mapping[str, str]
    ) -> list[list[str]]:
        from .engines.elasticsearch import document_id, stored

        return [
            [
                document_id(
                    [
                        stored(kinds[name], value)
                        for name, value in zip(request.fields, key, strict=True)
                    ]
                )
                for key in keys
            ]
            for keys in _batches(request.keys)
        ]

    def count(self, table: str, request: ErasureRequest, kinds: Mapping[str, str]) -> int:
        engine, total = self.native.engine, 0
        for ids in self._ids(table, request, kinds):
            status, answer = self.native._call(
                engine, "POST", engine._path(table, "_count"), {"query": {"ids": {"values": ids}}}
            )
            if status != 200 or not isinstance(answer, Mapping):
                raise EngineError(f"counting in {table} failed ({status})")
            total += int(answer["count"])
        return total

    def delete(self, table: str, request: ErasureRequest, kinds: Mapping[str, str]) -> int:
        engine, total = self.native.engine, 0
        for ids in self._ids(table, request, kinds):
            status, answer = self.native._call(
                engine,
                "POST",
                engine._path(table, "_delete_by_query") + "?refresh=true&conflicts=abort",
                {"query": {"ids": {"values": ids}}},
            )
            if (
                status != 200
                or not isinstance(answer, Mapping)
                or answer.get("timed_out") is not False
                or answer.get("failures")
            ):
                raise EngineError(f"erasing from {table} has an uncertain outcome ({status})")
            total += int(answer["deleted"])
        return total

    def columns(self, table: str) -> frozenset[str]:
        engine = self.native.engine
        status, answer = self.native._call(engine, "GET", engine._path(table, "_mapping"))
        if status != 200 or not isinstance(answer, Mapping):
            raise EngineError(f"reading the mapping of {table} failed ({status})")
        found: set[str] = set()
        for index in answer.values():
            properties = index.get("mappings", {}).get("properties", {})
            found.update(str(name) for name in properties)
        return frozenset(found)


def rendered_deletion(dialect: str, table: str, columns: str, arity: int) -> str:
    """The deletion of the keys' rows from a table outside the map, as text for the client.

    The operator sends none of these: a table outside the map is no longer the entity's, so its rows
    are the client's to delete or keep. ``…`` stands for each key value, which the text never holds.
    """
    row = "(" + ", ".join(["…"] * arity) + ")"
    if dialect == "postgres":
        return f"DELETE FROM {table} WHERE {columns} IN ({row}, …);"
    if dialect == "clickhouse":
        return (
            f"ALTER TABLE {table} DELETE WHERE {columns} IN ({row}, …) SETTINGS mutations_sync = 2;"
        )
    return (
        f'POST /{table}/_delete_by_query?refresh=true {{"query": {{"ids": {{"values": ["…"]}}}}}}'
        f" - the index's write block refuses it until it is lifted"
    )


def _access(native: Any) -> _Access:
    if native.dialect == "postgres":
        return _PostgresAccess(native)
    if native.dialect == "clickhouse":
        return _ClickHouseAccess(native)
    if native.dialect == "elasticsearch":
        return _ElasticsearchAccess(native)
    raise MigrationRefused(f"{native.dialect} has no erasure here")  # pragma: no cover


# --- the plan and its execution -----------------------------------------------------------------


def _group(
    placement: PlacementMap, entity: str
) -> tuple[str, list[tuple[Role, Materialization]]]:
    for name, group in sorted(placement.groups.items()):
        if entity in group.source.layout.tables:
            members: list[tuple[Role, Materialization]] = [("source", group.source)]
            members.extend(("copy", material) for material in group.derived)
            return name, members
    raise MigrationRefused(f"the map in force places no table of {entity}")


def plan_erasure(operator: LocalCutover, request: ErasureRequest) -> ErasurePlan:
    """Every table of the map in force that holds the entity, and the project's tables outside it
    that have its columns, each with the rows that match. Reads only. Call under the project lock.
    """
    from .departure import departure

    placement = operator.active_map()
    group, materials = _group(placement, request.entity)
    tables: list[ErasureTable] = []
    in_force: dict[str, set[str]] = {}
    for role, material in materials:
        if material.engine not in operator.native:
            raise MigrationRefused(
                f"{request.entity} is in {material.engine}, which this operator is not bound "
                f"to. An orderbook engine deletes nothing and is never bound; any other engine "
                f"of the map needs its binding before an erasure can reach every copy"
            )
        native = operator.native[material.engine]
        access = _access(native)
        table = material.layout.tables[request.entity]
        kinds = dict(material.layout.columns.get(request.entity, {}))
        in_force.setdefault(material.engine, set()).add(table)
        tables.append(
            ErasureTable(
                material.engine,
                access.dialect,
                table,
                role,
                access.count(table, request, kinds),
            )
        )
    entity = operator.model.entity(request.entity)
    wanted = {field.name for field in entity.fields}
    outside: list[ErasureTable] = []
    found = departure(placement, operator.engines, project_id=operator.project_id)
    for engine in found.engines:
        if engine.engine not in operator.native:
            continue
        access = _access(operator.native[engine.engine])
        for finding in engine.findings:
            if finding.kind != "outside" or finding.name in in_force.get(engine.engine, set()):
                continue
            columns = access.columns(finding.name)
            if not wanted <= columns:
                continue
            kinds = _kinds_like(materials, engine.engine, request.entity)
            outside.append(
                ErasureTable(
                    engine.engine,
                    access.dialect,
                    finding.name,
                    "outside",
                    access.count(finding.name, request, kinds),
                    statements=(
                        rendered_deletion(
                            access.dialect,
                            access.target(finding.name),
                            access.key_columns(request),
                            len(request.fields),
                        ),
                    ),
                )
            )
    return ErasurePlan(request, group, placement.map_version, tuple(tables), tuple(outside))


def _kinds_like(
    materials: Sequence[tuple[Role, Materialization]], engine: str, entity: str
) -> dict[str, str]:
    """The column kinds of the entity in ``engine``, from a material of the map, else none."""
    for _role, material in materials:
        if material.engine == engine:
            return dict(material.layout.columns.get(entity, {}))
    return {}


def execute_erasure(
    operator: LocalCutover, plan: ErasurePlan, secret: bytes, *, at: str | None = None
) -> ErasureReceipt:
    """Delete from the copies, then the source, then count every table again.

    The receipt says when, and that is the wall clock's one use here: a receipt is proof that a
    request was honoured, and proof without a time does not say whether it was honoured in time.
    The time is written down and never compared with anything.
    """
    moment = at or dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
    placement = operator.active_map()
    if placement.map_version != plan.map_version:
        raise MigrationRefused("the map in force changed after the erasure was planned")
    _group_name, materials = _group(placement, plan.request.entity)
    kinds_of = {
        (material.engine, material.layout.tables[plan.request.entity]): dict(
            material.layout.columns.get(plan.request.entity, {})
        )
        for _role, material in materials
    }
    order = [table for table in plan.tables if table.role == "copy"] + [
        table for table in plan.tables if table.role == "source"
    ]
    deleted: dict[tuple[str, str], int] = {}
    for table in order:
        access = _access(operator.native[table.engine])
        kinds = kinds_of[(table.engine, table.table)]
        deleted[(table.engine, table.table)] = access.delete(table.table, plan.request, kinds)
    done: list[ErasureTable] = []
    for table in plan.tables:
        access = _access(operator.native[table.engine])
        kinds = kinds_of[(table.engine, table.table)]
        done.append(
            ErasureTable(
                table.engine,
                table.dialect,
                table.table,
                table.role,
                table.matching,
                deleted[(table.engine, table.table)],
                access.count(table.table, plan.request, kinds),
            )
        )
    outside = []
    for table in plan.outside:
        access = _access(operator.native[table.engine])
        kinds = _kinds_like(materials, table.engine, plan.request.entity)
        outside.append(
            ErasureTable(
                table.engine,
                table.dialect,
                table.table,
                "outside",
                table.matching,
                None,
                access.count(table.table, plan.request, kinds),
            )
        )
    complete = all(table.remaining == 0 for table in done)
    record = {
        "protocol": 1,
        "id": secrets.token_hex(16),
        "project_id": operator.project_id,
        "entity": plan.request.entity,
        "group": plan.group,
        "map_version": plan.map_version,
        "keys": len(plan.request.keys),
        "key_digests": list(plan.request.digests(secret)),
        "tables": [table.as_record() for table in done],
        "outside": [table.as_record() for table in outside],
        "at": moment,
        "outcome": "erased" if complete else "incomplete",
    }
    return ErasureReceipt(record)


def erasure_secret(directory: Path) -> bytes:
    """The project's digest secret: created once, 32 random bytes, readable by its owner only."""
    from ._local_state import write_bytes

    path = Path(directory) / SECRET_FILE
    if not path.exists():
        # Another operator process may have created it first; then read theirs.
        with suppress(FileExistsError):
            write_bytes(path, secrets.token_bytes(32), replace=False)
    mode = path.stat().st_mode & 0o7777
    if mode & 0o077:
        raise MigrationRefused(
            f"{path} is readable beyond its owner (mode {mode:o}); a digest secret others can read "
            f"lets them test a guessed key against a receipt. chmod 600 it"
        )
    secret = path.read_bytes()
    if len(secret) != 32:
        raise MigrationRefused(f"{path} is not a 32-byte erasure secret")
    return secret


def write_receipt(directory: Path, receipt: ErasureReceipt) -> Path:
    """Publish the receipt as ``erasures/<id>.json``, durably and never over another one."""
    from ._local_state import write_bytes

    path = Path(directory) / RECEIPTS / f"{receipt.record['id']}.json"
    write_bytes(path, canonical_bytes(dict(receipt.record)) + b"\n", replace=False)
    return path

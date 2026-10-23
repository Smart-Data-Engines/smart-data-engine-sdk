"""Native DDL and drain operations for the public write-fence protocol.

These objects need dedicated provisioning connections. They do not run on the application's write
path. Metadata is checked as well as created: an identically named, unrelated table is not ours.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any
from uuid import UUID

from ..errors import EngineError, MigrationRefused
from ..layout import index_name_refusal
from ..schema import QUOTE
from ..write_fence import (
    DRAIN_TABLE,
    EPOCH_COLUMN,
    FENCE_PREFIX,
    ColumnState,
    FenceMetadata,
    fence_state,
)


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
                [[table, UUID(metadata.identity), project_id, hold]],
                column_names=["table_name", "table_uuid", "project_id", "hold"],
                settings={"async_insert": 0, "wait_for_async_insert": 1},
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


ElasticsearchRequest = Callable[..., tuple[int, Any]]
"""``request(method, path, body=None)``: the adapter's own transport, one request, no replay."""

PIPELINE_CONTRACT = 1
_HOLDS = re.compile(r"(?:setup|hold_[0-9a-f]{32})")
_BOUND = re.compile(r"(min|max)_([1-9][0-9]*)")


def fence_pipeline(index: str) -> str:
    """The ingest pipeline holding ``index``'s write fence: one per index, named after it."""
    return FENCE_PREFIX + _index_name(index)


def _index_name(index: str) -> str:
    refusal = index_name_refusal(index)
    if refusal is not None:
        raise MigrationRefused(f"{index!r} is not an Elasticsearch index name: {refusal}")
    return index


def fence_processor(constraints: Mapping[str, str]) -> dict[str, Any]:
    """The one processor of a fence pipeline, rendered from its constraints and nothing else.

    Elasticsearch checks no constraint of its own on a document, so the constraints the protocol
    names are kept in the pipeline's ``_meta`` and this processor is what enforces them: it fails
    a document a held fence, or a bound, refuses. Rendered deterministically, so that a reader can
    render it again from ``_meta`` and refuse a pipeline whose processor says something else - a
    name alone does not certify its predicate.
    """
    names = sorted(name for name in constraints if name.startswith(FENCE_PREFIX))
    held = any(_HOLDS.fullmatch(name[len(FENCE_PREFIX):]) for name in names)
    bounds = [match for name in names if (match := _BOUND.fullmatch(name[len(FENCE_PREFIX):]))]
    lower = [int(match.group(2)) for match in bounds if match.group(1) == "min"]
    upper = [int(match.group(2)) for match in bounds if match.group(1) == "max"]
    compare = [f"v < {value}L" for value in sorted(lower)]
    compare += [f"v > {value}L" for value in sorted(upper)]
    # No constant conditions inside a script: Painless refuses `if (true)` as an extraneous block,
    # so a held fence and an unbounded one are each a script of one constant.
    if held:
        script = "true"
    elif not bounds:
        script = "false"
    else:
        script = (
            f"def e = ctx['{EPOCH_COLUMN}']; "
            "if (!(e instanceof Integer || e instanceof Long)) { return true; } "
            "long v = ((Number) e).longValue(); "
            f"return {' || '.join(compare)};"
        )
    if held:
        message = "a write barrier holds this index: no write is admitted until it is released"
    else:
        admits = (
            f"from {max(lower)} to {min(upper)}" if lower and upper else "none recorded"
        )
        message = (
            f"the write generation {{{{{EPOCH_COLUMN}}}}} is not one this index admits "
            f"(generations {admits})"
        )
    return {"fail": {"if": script, "message": message}}


class ElasticsearchFences:
    """The write-fence protocol on Elasticsearch: a pipeline holds the constraints, a block drains.

    - Constraints live in the ``_meta`` of the index's own ingest pipeline, set as its
      ``index.final_pipeline``, which no request can bypass. The pipeline's processor enforces
      them when a document is accepted.
    - ``drain`` is ``PUT /<index>/_block/write``, which waits for the operations already running on
      the index's shards and refuses the rest, measured on 9.5.5. The block stays while any hold is
      recorded and is lifted with the last one: the pipeline checks a write when it arrives, the
      block when it reaches the shard, and a write that passed an older pipeline on its way is
      refused at the shard while the barrier holds.
    - An index write-blocked with a fence of ours and no hold recorded is an interrupted change
      (the block outlived its hold, or a setup stopped half-way). It reads as an interrupted setup,
      which ``resume_prepare`` completes. One blocked without a fence of ours is somebody else's
      block, and is refused rather than lifted.
    """

    def __init__(self, request: ElasticsearchRequest) -> None:
        self._request = request

    def _call(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        try:
            return self._request(method, path, body)
        except EngineError as exc:
            raise EngineError(
                f"write-fence operation failed; inspect or resume its state: {exc}"
            ) from exc

    def _index(self, table: str) -> tuple[str, Mapping[str, Any]]:
        # Every operation starts here or in add_column and drain, which check the same: the name
        # goes into a path, so it is refused unless it is an index name ("a/../_cluster").
        _index_name(table)
        status, body = self._call("GET", f"/{table}/_settings")
        settings = body.get(table, {}).get("settings", {}).get("index") if status == 200 else None
        if not isinstance(settings, Mapping) or not isinstance(settings.get("uuid"), str):
            raise EngineError(f"write-fence index {table} does not exist")
        return settings["uuid"], settings

    def _pipeline(self, table: str) -> tuple[dict[str, Any] | None, int | None]:
        status, body = self._call("GET", f"/_ingest/pipeline/{fence_pipeline(table)}")
        if status == 404:
            return None, None
        document = body.get(fence_pipeline(table)) if status == 200 else None
        if not isinstance(document, dict):
            raise EngineError(f"the write-fence pipeline of {table} could not be read ({status})")
        version = document.get("version")
        return document, version if type(version) is int else None

    def _constraints(
        self, table: str, uuid: str, document: Mapping[str, Any] | None
    ) -> dict[str, str]:
        if document is None:
            return {}
        outer = document.get("_meta")
        meta = outer.get("sde") if isinstance(outer, Mapping) else None
        if (
            not isinstance(meta, Mapping)
            or meta.get("contract") != PIPELINE_CONTRACT
            or meta.get("index") != table
        ):
            raise MigrationRefused(
                f"the pipeline {fence_pipeline(table)} is not a write fence of this library"
            )
        if meta.get("uuid") != uuid:
            # The fence of an earlier index of the same name: nothing of it applies to this one.
            return {}
        constraints = meta.get("constraints")
        if not isinstance(constraints, Mapping) or not all(
            isinstance(name, str) and isinstance(value, str) for name, value in constraints.items()
        ):
            raise MigrationRefused(
                f"the write-fence pipeline of {table} has unreadable constraints"
            )
        if document.get("processors") != [fence_processor(constraints)]:
            raise MigrationRefused(
                f"the write-fence pipeline of {table} was changed outside this library: its "
                "processor is not the one its constraints render"
            )
        return dict(constraints)

    def metadata(self, table: str) -> FenceMetadata:
        uuid, settings = self._index(table)
        final = settings.get("final_pipeline")
        if final not in (None, "_none", fence_pipeline(table)):
            raise MigrationRefused(
                f"index {table} runs another final pipeline ({final}); a write fence cannot be "
                "installed beside it"
            )
        status, body = self._call("GET", f"/{table}/_mapping")
        properties = (
            body.get(table, {}).get("mappings", {}).get("properties", {}) if status == 200 else None
        )
        if not isinstance(properties, Mapping):
            raise EngineError(f"the mapping of {table} could not be read ({status})")
        field = properties.get(EPOCH_COLUMN)
        column: ColumnState = (
            "absent" if field is None else "valid" if field == {"type": "long"} else "conflict"
        )
        document, _ = self._pipeline(table)
        constraints = self._constraints(table, uuid, document)
        blocked = str(settings.get("blocks", {}).get("write", "false")).lower() == "true"
        held = any(_HOLDS.fullmatch(name[len(FENCE_PREFIX):]) for name in constraints)
        if blocked and not held:
            owned = any(name.startswith(FENCE_PREFIX + "owner_") for name in constraints)
            if not owned:
                raise MigrationRefused(
                    f"index {table} is write-blocked and carries no write fence of this library: "
                    "somebody else blocked it, and this library does not lift that"
                )
            constraints[FENCE_PREFIX + "setup"] = "0"
        return FenceMetadata(uuid, column, constraints)

    def add_column(self, table: str) -> None:
        _index_name(table)
        status, _ = self._call(
            "PUT", f"/{table}/_mapping", {"properties": {EPOCH_COLUMN: {"type": "long"}}}
        )
        if status != 200:
            raise EngineError(f"adding {EPOCH_COLUMN} to {table} failed ({status})")

    def _write(
        self, table: str, change: Callable[[dict[str, str]], dict[str, str]]
    ) -> dict[str, str]:
        uuid, settings = self._index(table)
        document, version = self._pipeline(table)
        before = self._constraints(table, uuid, document)
        after = change(dict(before))
        recorded = None if document is None else document.get("_meta", {}).get("sde", {})
        if after != before or not isinstance(recorded, Mapping) or recorded.get("uuid") != uuid:
            body = {
                "description": f"Smart Data Engine write fence of {table}",
                "version": (version or 0) + 1,
                "_meta": {"sde": {"contract": PIPELINE_CONTRACT, "index": table, "uuid": uuid,
                                  "constraints": dict(sorted(after.items()))}},
                "processors": [fence_processor(after)],
            }
            path = f"/_ingest/pipeline/{fence_pipeline(table)}"
            if document is not None:
                path += f"?if_version={version if version is not None else 0}"
            status, _ = self._call("PUT", path, body)
            if status == 409:
                raise MigrationRefused(
                    f"the write fence of {table} changed while this command ran: another executor "
                    "is provisioning it. Serialize commands in one project state directory."
                )
            if status != 200:
                raise EngineError(f"writing the write fence of {table} failed ({status})")
        if settings.get("final_pipeline") != fence_pipeline(table):
            status, _ = self._call(
                "PUT", f"/{table}/_settings", {"index.final_pipeline": fence_pipeline(table)}
            )
            if status != 200:
                raise EngineError(f"installing the write fence on {table} failed ({status})")
        return after

    def add_constraint(self, table: str, name: str, expression: str) -> None:
        def change(constraints: dict[str, str]) -> dict[str, str]:
            if constraints.get(name, expression) != expression:
                raise MigrationRefused(f"write fence constraint {name} has an unexpected predicate")
            return {**constraints, name: expression}

        self._write(table, change)

    def drop_constraint(self, table: str, name: str) -> None:
        after = self._write(table, lambda constraints: {
            key: value for key, value in constraints.items() if key != name
        })
        if not any(_HOLDS.fullmatch(key[len(FENCE_PREFIX):]) for key in after):
            # The last hold: the shard-level block goes with it.
            _, settings = self._index(table)
            if str(settings.get("blocks", {}).get("write", "false")).lower() == "true":
                status, _ = self._call("PUT", f"/{table}/_settings", {"index.blocks.write": False})
                if status != 200:
                    raise EngineError(f"lifting the write block of {table} failed ({status})")

    def drain(self, table: str, *, project_id: str, hold: str) -> None:
        # Repeated on a retry: the block proves that every operation already on the shards has
        # finished, and that the rest are refused there.
        _index_name(table)
        status, body = self._call("PUT", f"/{table}/_block/write")
        indices = body.get("indices", []) if isinstance(body, Mapping) else []
        if status == 200 and isinstance(body, Mapping) and body.get("acknowledged") and not indices:
            # Already blocked, and a block added again proves nothing (measured: no shard
            # acknowledges it). Lifted and added again: in between, the hold already in the
            # pipeline refuses every new write at its arrival, and the new block waits for
            # anything still on its way.
            lifted, _ = self._call("PUT", f"/{table}/_settings", {"index.blocks.write": False})
            if lifted != 200:
                raise EngineError(f"renewing the write barrier on {table} failed ({lifted})")
            status, body = self._call("PUT", f"/{table}/_block/write")
            indices = body.get("indices", []) if isinstance(body, Mapping) else []
        if (
            status != 200
            or body.get("acknowledged") is not True
            or body.get("shards_acknowledged") is not True
            or not any(entry.get("name") == table and entry.get("blocked") for entry in indices)
        ):
            raise EngineError(
                f"the write barrier on {table} was not confirmed by every shard ({status}); the "
                "index may be closed to writes - inspect or resume"
            )
        _held(self.metadata(table), project_id, hold)

    def restore(self, table: str, *, project_id: str, hold: str) -> None:
        # Nothing is detached on Elasticsearch: the block is the drain, and the hold its record.
        _held(self.metadata(table), project_id, hold)


def _held(metadata: FenceMetadata, project_id: str, hold: str) -> None:
    state = fence_state(metadata)
    if state.project_id != project_id or hold not in state.holds:
        raise MigrationRefused("the requested project's write barrier is not installed")

"""Leaving: what this library and its local operator left in a client's engines, and its removal.

A client who stops using the library keeps their data where it always was, in ordinary tables of
their own engines. Two kinds of thing of ours can be there beside their rows:

- **write fences** ([write-fences.md](../../../docs/write-fences.md)). On every table of a group
  that carries a write generation: the column ``__sde_write_epoch`` and CHECK constraints in the
  ``__sde_f_`` namespace; on Elasticsearch, the index's final pipeline ``__sde_f_<index>``. They
  refuse every write that does not carry the group's generation, and a client's own code, writing
  without this library, carries none - so it is refused until the fences are released;
- **bookkeeping**: ``sde_map_state``, ``sde_backfill_state`` and, on ClickHouse,
  ``__sde_fence_drains``. Version numbers, counts and table identities, never a row.

:func:`departure` lists both on the client's own provisioning connections and renders the
statements that remove them, as text a database administrator can read and run by hand.
:meth:`Departure.execute` runs exactly that text. **Nothing here ever renders a statement that
removes a row.** A table that carries this project's fence but is not in the map in force - a
source a completed move retired, or a copy an abandoned one left - holds rows, so it is listed and
never released or dropped: whether to keep it is the client's decision, not this tool's.

A table held by an operation in progress - a barrier, an interrupted setup or generation change, a
table a drain left detached - refuses the whole engine's release: the local operator finishes or
abandons the operation first, because releasing under it would open a table its recovery expects
closed.

:func:`departure_script` renders the release and cleanup from a map alone, without connecting, for
an export written where the engines are out of reach. It names the constraints the map's own
generation implies; anything else in the namespace is a completed barrier's marker, ``CHECK
(true)``, inert for a write, which :func:`departure` finds and removes as well.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from .errors import EngineError, MigrationRefused
from .generation import DRAIN_TABLE, EPOCH_COLUMN, check_map_project, fenced_groups
from .placement import RESERVED_TABLES, PlacementMap
from .schema import QUOTE
from .write_fence import FENCE_PREFIX, FenceMetadata, fence_state

DIALECTS = ("clickhouse", "elasticsearch", "postgres")
"""The engines that can hold a write fence or bookkeeping of this library. The orderbook engine
fixes its own schema and takes neither (placement map contract 6), so nothing of ours is in it."""

FindingKind = Literal[
    "fence", "column", "held", "outside", "foreign", "bookkeeping", "pipeline", "field",
    "detached", "unreadable",
]

_OWNER = FENCE_PREFIX + "owner_"
_STAGE_MARKER = re.compile(r"sde\.stage1\.[0-9a-f]{32}\.[0-9a-f]{32}\.[0-9a-f]{16}")


@dataclass(frozen=True)
class Finding:
    """One object of ours, or one table that carries something of ours, and what it is."""

    kind: FindingKind
    name: str
    detail: str

    def as_record(self) -> dict[str, str]:
        return {"kind": self.kind, "name": self.name, "detail": self.detail}


@dataclass(frozen=True)
class EngineDeparture:
    """One engine of the map: what is in it, and the statements that remove it, in order.

    ``release`` is what a write without this library needs: after it, the tables of the map in
    force accept an ordinary INSERT. ``cleanup`` removes the rest of ours - the generation column
    and the bookkeeping - and is optional. ``refusals`` are why the release cannot be run now.
    """

    engine: str
    dialect: str
    findings: tuple[Finding, ...]
    release: tuple[str, ...]
    cleanup: tuple[str, ...]
    refusals: tuple[str, ...]

    def as_record(self) -> dict[str, Any]:
        return {
            "engine": self.engine,
            "dialect": self.dialect,
            "findings": [finding.as_record() for finding in self.findings],
            "release": list(self.release),
            "cleanup": list(self.cleanup),
            "refusals": list(self.refusals),
        }


@dataclass(frozen=True)
class Departure:
    """Everything of ours in the engines of one map, read now, with its removal."""

    project_id: str | None
    map_version: int
    engines: tuple[EngineDeparture, ...]
    placement: PlacementMap = field(repr=False, compare=False)

    @property
    def ready(self) -> bool:
        """No engine refuses its release: no operation of the operator is in progress."""
        return not any(engine.refusals for engine in self.engines)

    def as_record(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "map_version": self.map_version,
            "ready": self.ready,
            "engines": [engine.as_record() for engine in self.engines],
        }

    def execute(self, engines: Mapping[str, Any], *, cleanup: bool = False) -> Departure:
        """Run the release - and the cleanup when asked - exactly as listed, then list again.

        The engines are read again first, and the statements run only if they are the ones this
        departure listed: what a person read and approved is what runs, and a table that changed
        in between - an operation started, a fence added - is listed again rather than acted on.
        """
        fresh = departure(self.placement, engines, project_id=self.project_id)
        if not fresh.ready:
            raise MigrationRefused(
                "departure refuses while an operation holds a table: "
                + "; ".join(reason for engine in fresh.engines for reason in engine.refusals)
            )
        if [(e.engine, e.release, e.cleanup) for e in fresh.engines] != [
            (e.engine, e.release, e.cleanup) for e in self.engines
        ]:
            raise MigrationRefused(
                "the engines changed since this departure was listed; list it again and read it"
            )
        for planned in self.engines:
            statements = planned.release + (planned.cleanup if cleanup else ())
            for statement in statements:
                _run(planned.dialect, engines[planned.engine], statement)
        return departure(self.placement, engines, project_id=self.project_id)


@dataclass(frozen=True)
class DepartureScript:
    """One engine's release and cleanup, rendered from a map alone (:func:`departure_script`)."""

    engine: str
    dialect: str
    release: tuple[str, ...]
    cleanup: tuple[str, ...]

    def as_record(self) -> dict[str, Any]:
        return {
            "engine": self.engine,
            "dialect": self.dialect,
            "release": list(self.release),
            "cleanup": list(self.cleanup),
        }


# --- rendering ------------------------------------------------------------------------------------


def release_statements(dialect: str, table: str, constraints: Sequence[str]) -> tuple[str, ...]:
    """What lets an ordinary write into ``table``: its fence constraints dropped, ownership last.

    Ownership goes last so that an interrupted release still reads as this project's, and a second
    run finds and finishes it. On Elasticsearch the constraints are one pipeline: it stops being
    the index's final pipeline first, then it is deleted.
    """
    _dialect(dialect)
    if dialect == "elasticsearch":
        _index(table)
        return (
            f"PUT /{table}/_settings " + _json({"index": {"final_pipeline": None}}),
            f"DELETE /_ingest/pipeline/{FENCE_PREFIX}{table}",
        )
    quote = QUOTE[dialect]
    names = sorted({name for name in constraints if name.startswith(FENCE_PREFIX)})
    ordered = [name for name in names if not name.startswith(_OWNER)] + [
        name for name in names if name.startswith(_OWNER)
    ]
    return tuple(
        f"ALTER TABLE {quote(table)} DROP CONSTRAINT IF EXISTS {quote(name)}" for name in ordered
    )


def column_statement(dialect: str, table: str) -> str | None:
    """The generation column dropped, or ``None`` where the engine cannot drop a mapped field."""
    _dialect(dialect)
    if dialect == "elasticsearch":
        return None
    quote = QUOTE[dialect]
    return f"ALTER TABLE {quote(table)} DROP COLUMN IF EXISTS {quote(EPOCH_COLUMN)}"


def bookkeeping_statement(dialect: str, table: str) -> str:
    """One bookkeeping table of ours dropped; on Elasticsearch, its index deleted."""
    _dialect(dialect)
    if table not in (*RESERVED_TABLES, DRAIN_TABLE):
        raise MigrationRefused(f"{table!r} is not bookkeeping of this library")
    if dialect == "elasticsearch":
        return f"DELETE /{table}?ignore_unavailable=true"
    suffix = " SYNC" if dialect == "clickhouse" else ""
    return f"DROP TABLE IF EXISTS {QUOTE[dialect](table)}{suffix}"


def bookkeeping_tables(dialect: str) -> tuple[str, ...]:
    """The bookkeeping this library may create in an engine of ``dialect``.

    ``__sde_fence_drains`` is ClickHouse's alone: its drain detaches a table, and the log is what
    lets a resumption attach the right one. PostgreSQL drains with a lock, Elasticsearch with a
    write block, and neither needs a record.
    """
    _dialect(dialect)
    base = tuple(RESERVED_TABLES)
    return (*base, DRAIN_TABLE) if dialect == "clickhouse" else base


def departure_script(
    placement: PlacementMap, dialects: Mapping[str, str]
) -> tuple[DepartureScript, ...]:
    """The release and cleanup of every engine of ``placement``, from the map alone.

    For an export written where the engines cannot be reached. Every table of a group that carries
    a generation gets its fence released by the names the map implies - ``min`` and ``max`` of its
    generation and the project's ``owner`` - with ``IF EXISTS``, so a statement for a constraint
    that is not there does nothing. Other names in the namespace mark completed barriers and accept
    every write; :func:`departure` finds them too. ``dialects`` maps each engine of the map to its
    dialect; an engine of another dialect gets nothing, as nothing of ours is in it.
    """
    scripts: list[DepartureScript] = []
    fenced = fenced_groups(placement)
    engines = sorted(
        {material.engine for spot in placement.groups.values() for material in spot.all()}
    )
    for name in engines:
        dialect = dialects.get(name, "")
        if dialect not in DIALECTS:
            continue
        release: list[str] = []
        columns: list[str] = []
        for _, spot in sorted(fenced.items()):
            assert spot.write_epoch is not None
            for material in spot.all():
                if material.engine != name:
                    continue
                for table in sorted(material.layout.tables.values()):
                    implied = [f"{FENCE_PREFIX}min_{spot.write_epoch}",
                               f"{FENCE_PREFIX}max_{spot.write_epoch}"]
                    if placement.project_id is not None:
                        implied.append(_OWNER + placement.project_id)
                    release.extend(release_statements(dialect, table, implied))
                    statement = column_statement(dialect, table)
                    if statement is not None:
                        columns.append(statement)
        cleanup = columns + [
            bookkeeping_statement(dialect, table) for table in bookkeeping_tables(dialect)
        ]
        scripts.append(DepartureScript(name, dialect, tuple(release), tuple(cleanup)))
    return tuple(scripts)


# --- reading the engines --------------------------------------------------------------------------


def departure(
    placement: PlacementMap,
    engines: Mapping[str, Any],
    *,
    project_id: str | None = None,
) -> Departure:
    """Everything of ours in the engines of ``placement``, read now on ``engines``.

    ``placement`` is the map in force; its tables are the ones released. ``engines`` are the
    client's provisioning connections by the map's engine names, as ``prepare_schema`` takes them:
    reading a fence and removing it need the rights a runtime login does not have. A
    generation-bearing map needs its locally configured ``project_id``, never one learned from the
    map, since it decides which fences are this project's.
    """
    project = check_map_project(placement, project_id) if placement.contract >= 4 else project_id
    fenced = fenced_groups(placement)
    required = sorted({material.engine for spot in fenced.values() for material in spot.all()})
    missing = [name for name in required if name not in engines]
    if missing:
        raise MigrationRefused(
            f"departure needs a provisioning connection to every engine of a group that carries a "
            f"write generation, or their fences stay: {missing}"
        )
    result: list[EngineDeparture] = []
    # Every engine supplied, in the map or not: preparing a signed map keeps its bookkeeping on
    # every capable engine it was given, including one no group of the map is in.
    for name in sorted(engines):
        dialect = getattr(engines[name], "dialect", None)
        if dialect not in DIALECTS:
            continue
        live = sorted(
            {
                table
                for spot in fenced.values()
                for material in spot.all()
                if material.engine == name
                for table in material.layout.tables.values()
            }
        )
        result.append(_engine(name, str(dialect), engines[name], live, project))
    return Departure(project, placement.map_version, tuple(result), placement)


def _engine(
    name: str, dialect: str, engine: Any, live: Sequence[str], project: str | None
) -> EngineDeparture:
    findings: list[Finding] = []
    release: list[str] = []
    columns: list[str] = []
    refusals: list[str] = []
    reader = _Reader.for_engine(dialect, engine, project)
    for table in sorted(set(live) | set(reader.fenced())):
        try:
            metadata = reader.metadata(table)
        except EngineError:
            if table in live:
                findings.append(Finding("unreadable", table, "named by the map, not in the engine"))
            continue
        except MigrationRefused as exc:
            findings.append(Finding("unreadable", table, str(exc)))
            if table in live:
                refusals.append(f"{table}: {exc}")
            continue
        ours = sorted(key for key in metadata.constraints if key.startswith(FENCE_PREFIX))
        if not ours:
            if metadata.column != "absent" and dialect != "elasticsearch" and table in live:
                findings.append(Finding("column", table, "the generation column, with no fence"))
                statement = column_statement(dialect, table)
                if statement is not None:
                    columns.append(statement)
            elif metadata.column != "absent" and table in live:
                findings.append(Finding("field", table, _FIELD))
            elif metadata.column != "absent":
                findings.append(Finding("column", table, _COLUMN_OUTSIDE))
            continue
        try:
            state = fence_state(metadata)
        except MigrationRefused as exc:
            findings.append(Finding("unreadable", table, str(exc)))
            if table in live:
                refusals.append(f"{table}: {exc}")
            continue
        if project is None or state.project_id != project:
            # Not this project's, or no project to tell by: listed, and acted on by nobody here.
            findings.append(Finding("foreign", table, f"fenced by project {state.project_id}"))
            if table in live:
                refusals.append(f"{table}: its fence belongs to project {state.project_id}")
            continue
        if table not in live:
            findings.append(Finding("outside", table, _OUTSIDE))
            continue
        if state.holds or state.closed:
            findings.append(Finding("held", table, _held(state.holds)))
            refusals.append(f"{table}: {_held(state.holds)}")
            continue
        findings.append(
            Finding("fence", table, f"write generation {state.epoch}: {', '.join(ours)}")
        )
        release.extend(release_statements(dialect, table, ours))
        statement = column_statement(dialect, table)
        if statement is not None:
            columns.append(statement)
        elif metadata.column != "absent":
            findings.append(Finding("field", table, _FIELD))
    for pipeline, statements in reader.orphan_pipelines():
        findings.append(Finding("pipeline", pipeline, _ORPHAN))
        columns.extend(statements)
    for pipeline in reader.foreign_pipelines():
        findings.append(Finding("foreign", pipeline, _FOREIGN_PIPELINE))
    detached = reader.detached()
    for table in detached:
        findings.append(Finding("detached", table, _DETACHED))
        refusals.append(f"{table}: {_DETACHED}")
    # Bookkeeping is one per namespace - per database or schema, and on Elasticsearch per cluster
    # - whichever projects write there. With another project's fence beside ours it is theirs as
    # much as ours, and dropping it would reset their rollback protection: it is listed and kept.
    shared = any(finding.kind == "foreign" for finding in findings)
    bookkeeping = []
    for table in reader.bookkeeping():
        if shared:
            findings.append(Finding("bookkeeping", table, _BOOKKEEPING[table] + _SHARED))
            continue
        findings.append(Finding("bookkeeping", table, _BOOKKEEPING[table]))
        bookkeeping.append(bookkeeping_statement(dialect, table))
    return EngineDeparture(
        name, dialect, tuple(findings), tuple(release), tuple(columns + bookkeeping),
        tuple(refusals),
    )


_OUTSIDE = (
    "carries this project's write fence but is not in the map in force: a source a completed move "
    "retired, or a copy an abandoned one left. It holds rows, so departure neither releases nor "
    "drops it; keep it or drop it yourself"
)
_FIELD = (
    "__sde_write_epoch, and __sde_tokens where the index has a text field, stay in the index "
    "mapping: Elasticsearch removes a mapped field only by a reindex, and a document written "
    "without them is accepted"
)
_COLUMN_OUTSIDE = (
    "the generation column, with no fence, on a table outside the map in force: left as it is"
)
_FOREIGN_PIPELINE = (
    "a fence pipeline of an index that is gone, and of another project: left as it is"
)
_SHARED = (
    "; kept, because another project's fences are in this namespace and this bookkeeping is "
    "shared by every project in it"
)
_ORPHAN = (
    "a fence pipeline of an index that is gone or was created again under the same name: it fences "
    "nothing of the index there now"
)
_DETACHED = (
    "detached by a drain that did not finish; resume the operator's operation, which attaches it"
)
_BOOKKEEPING = {
    **{name: f"bookkeeping, no row of yours: {holds}" for name, holds in RESERVED_TABLES.items()},
    DRAIN_TABLE: (
        "bookkeeping, no row of yours: the identity of each table a ClickHouse drain detached, "
        "which is what lets a resumption attach the right one"
    ),
}


def _held(holds: Sequence[str]) -> str:
    named = ", ".join(holds) if holds else "an interrupted generation change"
    return (
        f"an operation of the local operator holds this table ({named}); finish it, or abandon "
        "it, with sde-operator first"
    )


class _Reader:
    """What one engine holds of ours, read through its own catalogue."""

    def __init__(self, engine: Any, project: str | None) -> None:
        self.engine = engine
        self.project = project

    @staticmethod
    def for_engine(dialect: str, engine: Any, project: str | None) -> _Reader:
        if dialect == "postgres":
            return _PostgresReader(engine, project)
        if dialect == "clickhouse":
            return _ClickHouseReader(engine, project)
        return _ElasticsearchReader(engine, project)

    def fenced(self) -> list[str]:
        raise NotImplementedError

    def metadata(self, table: str) -> FenceMetadata:
        raise NotImplementedError

    def bookkeeping(self) -> list[str]:
        raise NotImplementedError

    def orphan_pipelines(self) -> list[tuple[str, tuple[str, ...]]]:
        return []

    def foreign_pipelines(self) -> list[str]:
        return []

    def detached(self) -> list[str]:
        return []


class _PostgresReader(_Reader):
    def _rows(self, query: str, values: Sequence[Any] = ()) -> list[Any]:
        try:
            with self.engine._cx.cursor() as cursor:
                cursor.execute(query, list(values))
                return list(cursor.fetchall())
        except Exception as exc:
            raise EngineError(f"reading the catalogue for departure failed: {exc}") from exc

    def fenced(self) -> list[str]:
        rows = self._rows(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname=current_schema() AND c.relkind='r' AND ("
            "EXISTS (SELECT 1 FROM pg_constraint k WHERE k.conrelid=c.oid AND k.contype='c' "
            "AND starts_with(k.conname, %s)) OR EXISTS (SELECT 1 FROM pg_attribute a "
            "WHERE a.attrelid=c.oid AND a.attname=%s AND NOT a.attisdropped)) ORDER BY 1",
            [FENCE_PREFIX, EPOCH_COLUMN],
        )
        return [str(row[0]) for row in rows]

    def metadata(self, table: str) -> FenceMetadata:
        from .engines._write_fences import PostgresFences

        return PostgresFences(self.engine._cx).metadata(table)

    def bookkeeping(self) -> list[str]:
        found = []
        for table in bookkeeping_tables("postgres"):
            rows = self._rows("SELECT to_regclass(%s) IS NOT NULL", [QUOTE["postgres"](table)])
            if rows and rows[0][0]:
                found.append(table)
        return found


class _ClickHouseReader(_Reader):
    def _rows(self, query: str, parameters: Mapping[str, Any] | None = None) -> list[Any]:
        try:
            return list(self.engine._cx.query(query, parameters=dict(parameters or {})).result_rows)
        except Exception as exc:
            raise EngineError(f"reading the catalogue for departure failed: {exc}") from exc

    def fenced(self) -> list[str]:
        rows = self._rows(
            "SELECT name FROM system.tables WHERE database=currentDatabase() AND "
            "(position(create_table_query, {prefix:String}) > 0 OR name IN (SELECT table FROM "
            "system.columns WHERE database=currentDatabase() AND name={column:String})) "
            "ORDER BY name",
            {"prefix": FENCE_PREFIX, "column": EPOCH_COLUMN},
        )
        return [str(row[0]) for row in rows if str(row[0]) not in bookkeeping_tables("clickhouse")]

    def metadata(self, table: str) -> FenceMetadata:
        from .engines._write_fences import ClickHouseFences

        return ClickHouseFences(self.engine._cx).metadata(table)

    def bookkeeping(self) -> list[str]:
        rows = self._rows(
            "SELECT name FROM system.tables WHERE database=currentDatabase() "
            "AND has({names:Array(String)}, name) ORDER BY name",
            {"names": list(bookkeeping_tables("clickhouse"))},
        )
        present = {str(row[0]) for row in rows}
        return [table for table in bookkeeping_tables("clickhouse") if table in present]

    def detached(self) -> list[str]:
        """Tables a drain detached and did not attach again: logged, and still detached."""
        if DRAIN_TABLE not in self.bookkeeping():
            return []
        rows = self._rows(
            "SELECT DISTINCT d.table FROM system.detached_tables AS d WHERE "
            "d.database=currentDatabase() AND d.is_permanently=1 AND toString(d.uuid) IN "
            f"(SELECT toString(table_uuid) FROM {QUOTE['clickhouse'](DRAIN_TABLE)}) ORDER BY 1"
        )
        return [str(row[0]) for row in rows]


class _ElasticsearchReader(_Reader):
    def __init__(self, engine: Any, project: str | None) -> None:
        super().__init__(engine, project)
        self._read: (
            tuple[list[str], list[tuple[str, tuple[str, ...]]], list[str]] | None
        ) = None

    def _call(self, method: str, path: str) -> tuple[int, Any]:
        status, body = self.engine._request(method, path)
        return int(status), body

    def _pipelines(self) -> dict[str, Any]:
        status, body = self._call("GET", f"/_ingest/pipeline/{FENCE_PREFIX}*")
        if status == 404:
            return {}
        if status != 200 or not isinstance(body, Mapping):
            raise EngineError(f"reading the fence pipelines for departure failed ({status})")
        return {str(key): value for key, value in body.items() if str(key).startswith(FENCE_PREFIX)}

    def _settings(self, index: str) -> Mapping[str, Any] | None:
        status, body = self._call("GET", f"/{index}/_settings")
        if status == 404:
            return None
        settings = body.get(index, {}).get("settings", {}).get("index") if status == 200 else None
        if not isinstance(settings, Mapping):
            raise EngineError(f"reading the settings of {index} for departure failed ({status})")
        return settings

    def _owned(self) -> tuple[list[str], list[tuple[str, tuple[str, ...]]], list[str]]:
        """Fenced indexes; this project's pipelines that fence nothing now; and another's."""
        if self._read is not None:
            return self._read
        fenced: list[str] = []
        orphans: list[tuple[str, tuple[str, ...]]] = []
        foreign: list[str] = []
        for pipeline, document in sorted(self._pipelines().items()):
            index = pipeline[len(FENCE_PREFIX):]
            outer = document.get("_meta") if isinstance(document, Mapping) else None
            meta = outer.get("sde") if isinstance(outer, Mapping) else None
            if not isinstance(meta, Mapping) or meta.get("index") != index:
                continue  # a pipeline in our namespace that is not a fence of this library
            settings = self._settings(index) if _index_or_none(index) else None
            if settings is not None and meta.get("uuid") == settings.get("uuid"):
                fenced.append(index)
                continue
            constraints = meta.get("constraints")
            if (
                self.project is None
                or not isinstance(constraints, Mapping)
                or _OWNER + self.project not in constraints
            ):
                foreign.append(pipeline)
                continue
            statements: list[str] = []
            if settings is not None and settings.get("final_pipeline") == pipeline:
                # An index made again under the name runs the old fence: unset it first, as
                # Elasticsearch refuses to delete a pipeline an index runs.
                statements.append(
                    f"PUT /{index}/_settings " + _json({"index": {"final_pipeline": None}})
                )
            statements.append(f"DELETE /_ingest/pipeline/{pipeline}")
            orphans.append((pipeline, tuple(statements)))
        self._read = (fenced, orphans, foreign)
        return self._read

    def fenced(self) -> list[str]:
        return self._owned()[0]

    def metadata(self, table: str) -> FenceMetadata:
        from .engines._write_fences import ElasticsearchFences

        return ElasticsearchFences(self.engine._request).metadata(table)

    def orphan_pipelines(self) -> list[tuple[str, tuple[str, ...]]]:
        return self._owned()[1]

    def foreign_pipelines(self) -> list[str]:
        return self._owned()[2]

    def bookkeeping(self) -> list[str]:
        return [
            table
            for table in bookkeeping_tables("elasticsearch")
            if self._settings(table) is not None
        ]


def _index_or_none(index: str) -> str | None:
    try:
        return _index(index)
    except MigrationRefused:
        return None


def _index(index: str) -> str:
    from .layout import index_name_refusal

    refusal = index_name_refusal(index)
    if refusal is not None:
        raise MigrationRefused(f"{index!r} is not an Elasticsearch index name: {refusal}")
    return index


def _dialect(dialect: str) -> None:
    if dialect not in DIALECTS:
        raise MigrationRefused(f"nothing of this library is in an engine of dialect {dialect!r}")


def _json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


# --- running the text -----------------------------------------------------------------------------

_REQUEST = re.compile(r"(PUT|DELETE) (/[^ ]+)(?: (\{.*\}))?")


def _run(dialect: str, engine: Any, statement: str) -> None:
    """One listed statement, run as written: SQL on the connection, a console line as a request."""
    if dialect == "elasticsearch":
        matched = _REQUEST.fullmatch(statement)
        if matched is None:
            raise MigrationRefused(f"not a request departure renders: {statement}")
        method, path, body = matched.group(1), matched.group(2), matched.group(3)
        status, answer = engine._request(method, path, None if body is None else json.loads(body))
        if not 200 <= int(status) < 300:
            raise EngineError(f"{statement} failed ({status}): {answer}")
        return
    try:
        if dialect == "postgres":
            with engine._cx.cursor() as cursor:
                cursor.execute(statement)
        else:
            engine._cx.command(statement)
    except Exception as exc:
        raise EngineError(f"{statement} failed: {exc}") from exc

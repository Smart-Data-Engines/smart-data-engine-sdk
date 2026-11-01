"""A session: a model, a placement and the engines it points at, tied together.

Everything below this is plumbing that was proved separately - canonical models, groups, shapes,
maps, routing, an engine adapter. This is where they become something an application calls, and
where the one guarantee that cannot be delivered by any of them individually gets enforced.

That guarantee is the transaction boundary. One group, one engine, that engine's transaction
semantics, and nothing wider. A client asking for a transaction across two groups is asking for a
distributed transaction, and the answer is not a two-phase commit - it is that they should declare
the atomicity they need, so the planner colocates the entities and the requirement becomes a
placement constraint instead. The error says exactly that, and it says it when the transaction is
*opened* rather than when the commit fails, because the second one is a production incident and the
first one is a test failure.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import replace
from threading import Lock
from time import perf_counter_ns
from types import MappingProxyType
from typing import Any, Protocol

from ._usage import SessionUsage, session_call, session_owner
from .admission import Check, Misfit, check_for
from .books import BookStream, Resync
from .bulk import batch_columns, bulk_writer, snapshot_rows
from .errors import (
    BulkWriteRefused,
    DeclarationError,
    EngineError,
    MapError,
    MigrationRefused,
    ModelPlanningError,
    ResourceBusy,
    ResourceClosed,
)
from .generation import (
    EPOCH_COLUMN,
    GENERATIONS_SINCE,
    logical_row,
    stamp_values,
    validate_generations,
)
from .groups import Group, colocation_groups
from .hashing import NameMap
from .layout import group_columns, zone_refusal
from .logging import log
from .migration import precision_refusal
from .model import LogicalModel
from .physical import PhysicalFinding
from .placement import Materialization, PlacementMap
from .query import (
    MAX_PAGE_ROWS,
    NumericSummary,
    QueryRefused,
    Range,
    ReadColumn,
    ReadPlan,
    ScanPage,
    count_engine,
    numeric_summary,
    plan_read,
    query_engine,
    query_value,
    summary_engine,
    summary_scale,
)
from .routing import Router
from .search import (
    DEFAULT_MAX_EXAMINED,
    MAX_EXAMINED,
    MAX_SEARCH_LIMIT,
    SearchPage,
    SearchRefused,
    matches,
    query_tokens,
    search_engine,
    text_index,
)
from .shapes import OperationShape, enumerate_shapes
from .telemetry import Recorder, StorageMeasurement, StorageSize
from .temporal_range import TEMPORAL_KINDS, kind_noun
from .verification import check_project_id
from .watermark import WatermarkCheck, enforce_forward_only

__all__ = ["Engine", "ManagedEngine", "Session"]

_WriteValues = dict[str, Any] | tuple[dict[str, Any], ...]


class Engine(Protocol):
    """What a session needs from an engine adapter, and nothing more.

    A protocol rather than a base class so that an adapter for another engine - or a fake, in
    somebody else's test suite - does not have to import anything from us to satisfy it.
    """

    dialect: str

    def ensure_schema(self, layout: Any, *, keys: Mapping[str, Any]) -> Any: ...
    def insert(self, table: str, values: Mapping[str, Any]) -> None: ...
    def get(self, table: str, key: Mapping[str, Any]) -> dict[str, Any] | None: ...

    @contextmanager
    def transaction(self) -> Iterator[Any]: ...


class ManagedEngine(Engine, Protocol):
    def connect(self) -> None: ...
    def close(self) -> None: ...


def _storage_refusal(exc: BaseException) -> str:
    """``refused`` when the catalogue denied the login, ``failed`` for any other engine error.

    Read from the error beneath the adapter's: PostgreSQL's SQLSTATE 42501, ClickHouse's access
    error 497 - what a login without the ``system.parts`` grant receives (measured on 24.8) - and
    Elasticsearch's 403, what a key without the ``monitor`` privilege receives (measured on 9.5.5).
    """
    cause = exc.__cause__ if exc.__cause__ is not None else exc
    if getattr(cause, "sqlstate", None) == "42501" or "Code: 497" in str(cause):
        return "refused"
    if getattr(cause, "status", None) == 403:
        return "refused"
    return "failed"


class _BookSink:
    """A book stream's sink: each update one write of the entity, through the session that opened
    the stream, so placement, ownership and telemetry are the session's own."""

    def __init__(self, session: Session, target: str, shape: OperationShape, symbol: str,
                 exchange: str) -> None:
        self._session = session
        self._target = target
        self._shape = shape
        self._symbol = symbol
        self._exchange = exchange

    def send_whole(self, side: str, levels: list[tuple[int, int, int]], timestamp_ns: int) -> None:
        self._session._book_write(self._target, self._shape, symbol=self._symbol,
                                  exchange=self._exchange, side=side, levels=levels,
                                  timestamp_ns=timestamp_ns, whole=True)

    def send_changes(
        self, side: str, levels: list[tuple[int, int, int]], timestamp_ns: int
    ) -> None:
        self._session._book_write(self._target, self._shape, symbol=self._symbol,
                                  exchange=self._exchange, side=side, levels=levels,
                                  timestamp_ns=timestamp_ns, whole=False)


def _no_entity(entity: str) -> ModelPlanningError:
    """A write or a point read of an entity the model does not declare (format contract 8d): one
    refusal, in every library, before any operation shape is looked up or any engine is called."""
    return ModelPlanningError(f"this model has no entity '{entity}'")


class Session:
    """Routes operations for one model against one placement.

    Holds no connection state of its own: engines do that. It exists to answer "where does this
    operation go" and to refuse the operations that cannot be answered - and both of those are pure
    functions of the model and the map, which is why this class has no configuration.
    """

    def __init__(
        self,
        model: LogicalModel,
        placement: PlacementMap,
        engines: Mapping[str, Engine],
        *,
        recorder: Recorder | None = None,
        names: NameMap | None = None,
        project_id: str | None = None,
    ) -> None:
        # Telemetry is optional and off by default. A library that starts measuring the moment it is
        # imported is a library people are right to be suspicious of; measurement begins when a
        # recorder is handed in, which is a visible line in the client's code.
        self._session_usage = SessionUsage(self)
        self._owned_engines: list[ManagedEngine] = []
        self._cleanup_lock = Lock()
        check_project_id(project_id)
        self._project_id = project_id
        self._recorder = recorder

        # The one place where the client's names and the hashed ones meet. When a model has been
        # hashed, everything downstream of here - the map, the shapes, the tables, the telemetry -
        # speaks digests only, and the application keeps saying `save("User", {"email": ...})`.
        # Without this the mode is unusable: a client would have to write `save("e_546526714dc9",
        # {"f_b1b4a0ec9efb": ...})` in their own code, which nobody will do and which would put the
        # digests in their source anyway.
        self._names = names
        self._reverse_fields: dict[str, dict[str, str]] = {}
        self._input_fields: dict[str, dict[str, str]] = {}
        self._declared: tuple[str, ...] = ()
        if names is not None:
            original_entities = {hashed: original for original, hashed in names.entities.items()}
            for entity, declared_fields in names.fields.items():
                mapping = dict(declared_fields)
                for original_relation, hashed_relation in names.relations.get(entity, {}).items():
                    relation = next(
                        r for r in model.relations
                        if r.source == names.entity(entity) and r.name == hashed_relation
                    )
                    target = model.entity(relation.target)
                    target_fields = names.fields[original_entities[target.name]]
                    for original_key, hashed_key in target_fields.items():
                        if hashed_key in target.key:
                            mapping[f"{original_relation}_{original_key}"] = (
                                f"{hashed_relation}_{hashed_key}"
                            )
                self._input_fields[entity] = mapping
                self._reverse_fields[names.entity(entity)] = {
                    hashed: original for original, hashed in mapping.items()
                }
            self._declared = tuple(sorted(names.entities))
        self._model = model
        self._placement = placement
        self._engines = dict(engines)
        self._router = Router(placement)
        self._groups: tuple[Group, ...] = colocation_groups(model)
        self._shapes = {(s.entity, s.kind, s.fields): s for s in enumerate_shapes(model)}
        self._admission = _admission(model, placement, self._groups)
        self._in_write_transaction = False
        self._deferred: list[tuple[str, str, str, str, int, _WriteValues]] = []
        """Rows waiting for their transaction to commit before reaching a copy.

        Six-wide rather than three because requirement 5.2 has us report how far behind a copy
        runs, and the honest start of that interval is when the row was **queued** - not when the
        replay ran. Carrying the group and the materialisation id too means the measurement is
        attributed without looking anything up on a path that must not do work.
        """

        missing = sorted(
            {m.engine for p in placement.groups.values() for m in p.all()} - set(self._engines)
        )
        if missing:
            raise EngineError(
                f"the placement map refers to engines that were not supplied: {missing}. A session "
                "cannot route an operation to an engine it has no adapter for, and guessing at a "
                "connection is not something a library should do."
            )

        # A temporal column keeps its field's zone (format contract 7a): groups in name order,
        # materialisations in document order. Here, as the fan-out rule below, because only an
        # engine says which dialect's spelling a layout is written in.
        for group in sorted(self._groups, key=lambda item: item.name):
            spot = placement.groups.get(group.name)
            if spot is None:
                continue
            for material in spot.all():
                zone = zone_refusal(
                    model, group.name, material.id, material.layout,
                    self._engines[material.engine].dialect,
                )
                if zone is not None:
                    raise MapError(zone)

        # A copy that silently holds different values from its source is refused before the first
        # write rather than discovered by `verify` at the end of one. The rule is `backfill`'s and
        # it lives in one function, because this is the second door asking it and for a long time
        # only the first one did: measured against live servers, a `timestamptz` written through a
        # fan-out map came back `09:30:15.123456` from PostgreSQL and `09:30:15.123` from
        # ClickHouse, with no error anywhere and `backfill` refusing the very same copy a phase
        # later. Here rather than at `load_map`, and that is forced: a map names engines by name
        # and carries no dialect, deliberately, so the earliest moment this is answerable is the
        # one where the adapters are in hand.
        for group in self._groups:
            spot = placement.groups.get(group.name)
            if spot is None or not spot.also_write:
                continue
            columns = group_columns(model, group)
            source_dialect = self._engines[spot.source.engine].dialect
            for copy in spot.also_write:
                for entity in sorted(columns):
                    refusal = precision_refusal(
                        group=group.name,
                        entity=entity,
                        columns=columns[entity],
                        source_dialect=source_dialect,
                        target_dialect=self._engines[copy.engine].dialect,
                    )
                    if refusal is not None:
                        raise MigrationRefused(
                            f"{refusal} This map fans writes out to {copy.engine}, so it would "
                            f"happen on every write rather than once during a copy, and nothing "
                            f"would report it."
                        )

        # Here rather than in a method somebody has to remember to call, and here rather than in
        # `ensure_schema`, which a deployment past its first release skips. A rolled-back map file
        # is read at process start, so the check has to be on the path every start takes - and this
        # constructor already refuses a map it cannot route, which is the same kind of refusal in
        # the same place. It costs one statement per participating engine, once per process, and
        # nothing at all for an unsigned map.
        with session_owner(self):
            self._physical = validate_generations(model, placement, self._engines, project_id)
            self._report_physical()
            self._forward_only = enforce_forward_only(placement, self._engines)

    @classmethod
    def connect(
        cls,
        model: LogicalModel,
        placement: PlacementMap,
        factories: Mapping[str, Callable[[], ManagedEngine]],
        *,
        recorder: Recorder | None = None,
        names: NameMap | None = None,
        project_id: str | None = None,
    ) -> Session:
        """Create, connect and own fresh adapters; clean up partial startup on any failure."""
        created: list[ManagedEngine] = []
        with session_owner(object()):
            try:
                engines: dict[str, ManagedEngine] = {}
                for name, factory in sorted(factories.items()):
                    engine = factory()
                    if any(engine is previous for previous in created):
                        raise EngineError(
                            "owned session factories must provide distinct fresh adapters"
                        )
                    created.append(engine)
                    engines[name] = engine
                    engine.connect()
                session = cls(
                    model, placement, engines, recorder=recorder, names=names, project_id=project_id
                )
                session._owned_engines = created
                return session
            except BaseException as exc:
                for engine in reversed(created):
                    try:
                        engine.close()
                    except BaseException:
                        exc.add_note(
                            "An owned adapter also failed to close during startup cleanup."
                        )
                raise

    def close(self) -> None:
        """End this unit of work and close only the adapters created by Session.connect."""
        self._session_usage.close()
        if not self._cleanup_lock.acquire(blocking=False):
            raise ResourceBusy("session cleanup is already in progress")
        try:
            failed: list[ManagedEngine] = []
            failures: list[BaseException] = []
            for engine in reversed(self._owned_engines):
                try:
                    engine.close()
                except BaseException as exc:
                    failed.append(engine)
                    failures.append(exc)
            self._owned_engines = failed
            if failures:
                raise failures[0]
        finally:
            self._cleanup_lock.release()

    def __enter__(self) -> Session:
        self._session_usage.check()
        return self

    def __exit__(self, _kind: object, error: BaseException | None, _traceback: object) -> None:
        try:
            self.close()
        except BaseException:
            if error is None:
                raise
            error.add_note("Owned adapter cleanup also failed; retry Session.close().")

    # --- what a session is -----------------------------------------------------------------
    #
    # The three things a session was handed, readable. Exposed for `sde.migration`, which needs all
    # three and is deliberately a set of free functions rather than methods here: a call that copies
    # a table for an hour has no business sitting in autocomplete next to `save()`. Reaching into
    # private attributes from a sibling module would have worked and would have made this class a
    # friend of that one, which is a worse arrangement than admitting what a session holds.

    @property
    def project_id(self) -> str | None:
        """The project from local enrollment configuration, never learned from a request."""
        return self._project_id

    @property
    def model(self) -> LogicalModel:
        """The model this session routes. Digests rather than the client's names when hashing is on,
        like everything else downstream of that boundary."""
        return self._model

    @property
    def placement(self) -> PlacementMap:
        """The map in force."""
        return self._placement

    @property
    def engines(self) -> Mapping[str, Engine]:
        """The adapters, by the names the map uses. A read-only view; the session keeps its own."""
        self._session_usage.check()
        return MappingProxyType(self._engines)

    @property
    def physical(self) -> tuple[PhysicalFinding, ...]:
        """How existing tables differ from the physical design the map declares, if at all.

        Reported, never refused, because the difference is performance: a table whose sort key,
        partition or index is not the one declared still stores and returns exactly the same rows,
        and turning that into an outage would make this library the thing that broke production
        (requirement 3.6). Provisioning refuses the same differences, which is where a person can
        act on them. Empty until a check has run and when everything matches.
        """
        return self._physical

    def _report_physical(self) -> None:
        if self._physical:
            log(
                "sde.schema.physical_mismatch",
                findings=len(self._physical),
                tables=sorted({finding.table for finding in self._physical}),
            )

    @property
    def rollback_protection(self) -> WatermarkCheck:
        """Whether an older map could be loaded over this one, and why.

        Public because a protection whose state cannot be read is a protection taken on trust. It
        has three values and the middle one matters: `enforced`, `unavailable` - no engine in this
        map can keep the bookkeeping, which is the case for an engine whose schema is fixed in its
        own source - and `not_applicable` for an unsigned map, which is the client's own document.
        """
        return self._forward_only

    # --- the hashing boundary --------------------------------------------------------------
    #
    # Four one-line helpers, each guarded by `is None`, because with hashing off the cost of this
    # whole mechanism has to be a single attribute check on the hot path - the overhead test gates
    # the library at one percent of a round trip and routing already spends 0.4% of it.

    def _entity(self, entity: str) -> str:
        """The client's entity name, as the model knows it."""
        if self._names is None:
            return entity
        return self._names.entity(entity)

    def _fields_in(self, entity: str, values: Mapping[str, Any]) -> Mapping[str, Any]:
        if self._names is None:
            return values
        mapping = self._input_fields[entity]
        try:
            return {mapping[field]: value for field, value in values.items()}
        except KeyError as exc:
            raise ModelPlanningError(
                f"{entity} declares no field {exc.args[0]}. With hashed identifiers a field the "
                "model does not declare cannot be translated, so it is refused here rather than "
                "sent to an engine under a name nothing will recognise."
            ) from None

    def _fields_out(self, entity: str, row: Any) -> Any:
        """Translate a row back, so a client can read their own data.

        Skipping this would hand back `{"f_b1b4a0ec9efb": "a@b.c"}`. The row would be correct and
        useless: the application would have to know the digests to index it, which is the one thing
        hashing exists to keep out of their code.
        """
        if self._names is None or not isinstance(row, Mapping):
            return row
        reverse = self._reverse_fields.get(self._names.entity(entity), {})
        return {reverse.get(field, field): value for field, value in row.items()}

    def _client_names(self, entity: str, fields: Iterable[str]) -> list[str]:
        """Hashed field names, back in the client's vocabulary, for an error message.

        An error that says `['f_7d1d3e8368c8', 'f_d186ef62ff24']` is an error that sends somebody to
        read our source. It costs one dict lookup on a path that is already raising.
        """
        if self._names is None:
            return sorted(fields)
        reverse = self._reverse_fields.get(self._names.entity(entity), {})
        return sorted(reverse.get(field, field) for field in fields)

    def _admit(self, entity: str, target: str, values: Mapping[str, Any]) -> Mapping[str, Any]:
        """Refuse, before any engine is called, a row the model does not allow; or return it as
        engines receive it.

        A field declared without ``nullable`` and every key field must be present and not ``None``,
        and a field the entity does not declare is refused by name. The engines would not agree on
        any of it, which is why the library decides: measured on 2 October 2026, PostgreSQL stored
        NULL in a required field and ClickHouse stored a value nobody wrote - ``0`` for an integer
        the row left out, ``""`` for a string - and neither raised. The same model meant two things,
        and moving a group would have changed which. Shared vectors: ``errors/078``-``082``.

        Then every value must be one its field's type holds (:meth:`_typed`).

        Two comparisons of key views, one pass over the required fields and one over the typed
        ones, from tables built once per session: this is on every write.
        """
        admission = self._admission[target]
        names = values.keys()
        if names <= admission.declared and admission.needed <= names:
            name = _null_required(values, admission.required)
            if name is None:
                return self._typed(entity, admission, values)
            raise ModelPlanningError(
                f"{entity}.{self._client_names(entity, (name,))[0]} is required and this row "
                f"gives it null"
            )
        unknown = sorted(name for name in names if name not in admission.declared)
        if unknown:
            raise ModelPlanningError(
                f"{entity} declares no field {self._client_names(entity, unknown[:1])[0]}"
            )
        absent = next(name for name in admission.required if name not in names)
        raise ModelPlanningError(
            f"{entity}.{self._client_names(entity, (absent,))[0]} is required and this row leaves "
            f"it out"
        )

    def _typed(
        self, entity: str, admission: _Admission, values: Mapping[str, Any], *, noun: str = "row"
    ) -> Mapping[str, Any]:
        """Refuse a value its field's type does not hold; return the row as engines receive it.

        Section 8b, point 4: a value takes a form a filter of its type takes, and the type holds
        it (``sde.admission``). Each driver and engine used to convert what it was given in its own
        way, measured on 7 October 2026: a decimal rounded by PostgreSQL was truncated by
        ClickHouse, an integer outside int32 wrapped to the opposite sign in ClickHouse from
        TypeScript, a date that does not exist moved two days. A row now reaches every engine with
        each value in the form a filter gets, so every adapter sees one representation per type.

        Fields in code-point order, the order a refusal names; the row is copied only when a value
        changes form.
        """
        admitted: dict[str, Any] | None = None
        for name, kind, check in admission.checks:
            value = values.get(name)
            if value is None:
                continue
            try:
                given = check(value)
            except Misfit as misfit:
                raise ModelPlanningError(
                    f"{entity}.{self._client_names(entity, (name,))[0]} is {kind} and this "
                    f"{noun} gives it {misfit}"
                ) from None
            if given is not value:
                if admitted is None:
                    admitted = dict(values)
                admitted[name] = given
        return values if admitted is None else admitted

    def _held(
        self,
        entity: str,
        target: str,
        group: str,
        engine: str,
        rows: Sequence[Mapping[str, Any]],
        *,
        batch: bool,
    ) -> None:
        """Refuse a row an engine its write goes to cannot hold, before any engine is written to.

        Format contract section 8b. ClickHouse 24.8 holds a date and a timestamp from 1900-01-01
        to 2299-12-31 and moved a value outside to the nearer end without an error (measured on
        1 November 2026: 1800-01-01 stored as 1900-01-01); 26.9 holds the contract's whole range.
        The range is the server's (``temporal_range``), and every engine the row goes to is asked:
        the one the write is routed to and each ``also_write`` copy, so that no copy is written
        before another refuses. An engine without a range holds them all.
        """
        fields = self._admission[target].temporal
        if not fields:
            return
        spot = self._placement.placement_of(group)
        for name in (engine, *(copy.engine for copy in spot.also_write)):
            span = getattr(self._engines[name], "temporal_range", None)
            if span is None:
                continue
            for index, row in enumerate(rows):
                for field, kind in fields:
                    value = row.get(field)
                    if value is None or span.holds(kind, value):
                        continue
                    client = self._client_names(entity, (field,))[0]
                    raise EngineError(
                        f"{f'row {index}: ' if batch else ''}{name} holds {kind_noun(kind)} "
                        f"{span.describe()}, and this row gives {entity}.{client} one outside it; "
                        f"nothing was sent"
                    )

    def _key(
        self, entity: str, target: str, key: Mapping[str, Any], fields: tuple[str, ...]
    ) -> Mapping[str, Any]:
        """A point read's key held to a written row's rules (format contract 8b, a point read's
        key): no value null, then every value one its field's type holds, each refusal naming the
        first offending field in code-point order. Returns the key as engines receive it.

        A key went to the engine as given until 31 October 2026, and each engine answered for it:
        `1.234` for a `decimal(12,2)` found no row on PostgreSQL and the row stored as `1.23` on
        ClickHouse, and a boolean for an `int64` was an engine error on one and no row on the
        other. Shared vectors: ``errors/131``-``136``.
        """
        name = _null_required(key, fields)
        if name is not None:
            raise ModelPlanningError(
                f"{entity}.{self._client_names(entity, (name,))[0]} is required and this key "
                f"gives it null"
            )
        return self._typed(entity, self._admission[target], key, noun="key")

    # --- structure -------------------------------------------------------------------------

    def group_of(self, entity: str) -> Group:
        target = self._entity(entity)
        for group in self._groups:
            if target in group:
                return group
        raise ModelPlanningError(
            f"{entity!r} is not in this model. A session routes what the model declares; an entity "
            "that is not declared has no group, no placement and no table."
        )

    def _shape(self, entity: str, kind: str, fields: tuple[str, ...] = ()) -> OperationShape:
        try:
            return self._shapes[(entity, kind, fields)]
        except KeyError:
            # Asked only once the lookup has failed: an entity typed wrong is not a shape the
            # model forgot, and the caller is told which (format contract 8d).
            if not any(spec.name == entity for spec in self._model.entities):
                raise _no_entity(entity) from None
            raise ModelPlanningError(
                f"the model admits no {kind} on {entity} over {list(fields)}. Shapes are "
                "enumerated from the model, so an operation with no shape is one the planner never "
                "saw and therefore never routed, which makes it a modelling gap rather than a "
                "runtime error."
            ) from None

    def _target(self, shape: OperationShape, *, fresh: bool) -> tuple[Engine, Materialization]:
        self._session_usage.group(shape.group)
        materialization = self._router.resolve(
            shape, in_write_transaction=self._in_write_transaction, fresh=fresh
        )
        return self._engines[materialization.engine], materialization

    # --- schema ----------------------------------------------------------------------------

    @session_call
    def ensure_schema(self) -> None:
        """Create what each engine is missing for the groups placed in it."""
        if self._placement.contract >= 4:
            self._physical = validate_generations(
                self._model, self._placement, self._engines, self._project_id
            )
            self._report_physical()
            return
        findings: list[PhysicalFinding] = []
        for group in self._groups:
            placement = self._placement.placement_of(group.name)
            keys = {name: self._model.entity(name).key for name in group.members}
            for materialization in placement.all():
                engine = self._engines[materialization.engine]
                # `or ()`: an adapter written before this returned findings returns None.
                findings.extend(engine.ensure_schema(materialization.layout, keys=keys) or ())
        self._physical = tuple(findings)
        self._report_physical()
        log("sde.schema.applied", groups=len(self._groups))

    # --- data ------------------------------------------------------------------------------

    @session_call
    def save(self, entity: str, values: Mapping[str, Any]) -> None:
        try:
            target = self._entity(entity)
        except DeclarationError:
            # With hashed identifiers a name with no hash is one the model does not declare (8d).
            raise _no_entity(entity) from None
        values = self._fields_in(entity, values)
        shape = self._shape(target, "write")
        values = self._admit(entity, target, values)
        engine, materialization = self._target(shape, fresh=False)
        self._held(entity, target, shape.group, materialization.engine, (values,), batch=False)
        table = materialization.layout.table_for(target)
        started = perf_counter_ns() if self._recorder else 0
        failed = False
        try:
            engine.insert(table, stamp_values(self._placement, shape.group, values))
            self._session_usage.check()
            self._fan_out(target, shape.group, values)
        except BaseException:
            failed = True
            raise
        finally:
            # In a finally block on purpose: a failed write is exactly the operation whose
            # latency and error count matter most to a placement decision, and it is the one an
            # early return would silently omit.
            #
            # The fan-out is inside the timed region, which is a decision. It makes the client's
            # write slower and the telemetry has to say so, or a placement would be scored against
            # a latency the application is not experiencing - and the whole point of the number is
            # that it is what they pay. One consequence is worth knowing: during a migration this
            # inflates write latency against a baseline written before it, so the drift detector
            # sees a real degradation with a known cause.
            self._observe(shape, started, rows=1, failed=failed)

    @session_call
    def save_many(self, entity: str, rows: Sequence[Mapping[str, Any]]) -> None:
        """Insert one bounded batch. No splitting or retry; see docs/bulk-writes.md."""
        try:
            target = self._entity(entity)
        except DeclarationError:
            # With hashed identifiers a name with no hash is one the model does not declare (8d).
            raise _no_entity(entity) from None
        shape = self._shape(target, "bulk_write")
        engine, materialization = self._target(shape, fresh=False)
        spot = self._placement.placement_of(shape.group)
        columns = batch_columns(rows, extra_columns=int(spot.write_epoch is not None))
        if not columns:
            return
        try:
            translated = tuple(self._fields_in(entity, row) for row in rows)
        except ModelPlanningError as exc:
            raise BulkWriteRefused(str(exc)) from exc
        fields = set(translated[0])
        spec = self._model.entity(target)
        declared = set(group_columns(self._model, self.group_of(entity))[target])
        required = {field.name for field in spec.fields if not field.nullable} | set(spec.key)
        if not fields <= declared or not required <= fields:
            raise BulkWriteRefused(
                "batch fields must be declared and include the key and all non-nullable fields"
            )
        admission = self._admission[target]
        typed = []
        for index, row in enumerate(translated):
            name = _null_required(row, admission.required)
            if name is not None:
                raise BulkWriteRefused(
                    f"row {index}: {entity}.{self._client_names(entity, (name,))[0]} is required "
                    f"and this row gives it null"
                )
            try:
                typed.append(self._typed(entity, admission, row))
            except ModelPlanningError as exc:
                raise BulkWriteRefused(f"row {index}: {exc}") from None
        translated = tuple(typed)
        self._held(entity, target, shape.group, materialization.engine, translated, batch=True)
        writer = bulk_writer(engine)
        for copy in spot.also_write:
            bulk_writer(self._engines[copy.engine])
        snapshot = snapshot_rows(translated)
        stamped = tuple(stamp_values(self._placement, shape.group, row) for row in snapshot)
        table = materialization.layout.table_for(target)
        started = perf_counter_ns() if self._recorder else 0
        failed = False
        try:
            writer.insert_many(table, stamped)
            self._session_usage.check()
            self._fan_out(target, shape.group, snapshot)
        except BaseException:
            failed = True
            raise
        finally:
            self._observe(shape, started, rows=len(snapshot), failed=failed)

    @session_call
    def book_stream(
        self,
        entity: str,
        *,
        symbol: str,
        exchange: str,
        mode: str = "incremental",
        resync: Resync | None = None,
    ) -> BookStream:
        """A stream of one book of ``entity`` - one symbol on one exchange - to the orderbook
        engine, sent whole or as its changes (:mod:`sde.books`).

        The entity's group must be placed on the orderbook engine, which reads an update by price
        and replaces a side whole (``MREPLACE``): refused otherwise, and refused for a server that
        does not announce ``replace_side``, before anything is sent. Each update the stream sends
        is one write of the entity, under this session's rules and timed like ``save_many``'s.
        """
        target = self._entity(entity)
        shape = self._shape(target, "bulk_write")
        engine, _ = self._target(shape, fresh=False)
        supports = getattr(engine, "supports_replace_side", None)
        if supports is None:
            raise EngineError(
                f"{entity} is placed on an engine that does not take a book by price: a book "
                f"stream writes to the orderbook engine, which applies an update by price and "
                f"replaces a side whole"
            )
        if not supports():
            raise EngineError(
                "this orderbook server does not replace a side of a book (capability "
                "replace_side), which a book stream needs: in full mode for every update, in "
                "incremental mode for each resync. Upgrade the server; nothing was sent."
            )
        check = getattr(engine, "check_book", None)
        if check is not None:
            check(symbol, exchange)
        sink = _BookSink(self, target, shape, symbol, exchange)
        return BookStream(sink, mode=mode, resync=resync)

    @session_call
    def _book_write(
        self,
        target: str,
        shape: OperationShape,
        *,
        symbol: str,
        exchange: str,
        side: str,
        levels: list[tuple[int, int, int]],
        timestamp_ns: int,
        whole: bool,
    ) -> None:
        """One update of a book stream: a whole side, or changes by price."""
        engine, materialization = self._target(shape, fresh=False)
        if self._placement.placement_of(shape.group).also_write:
            # Unreachable while a map refuses copies of a group without generations, which every
            # group on the orderbook engine is. Refused rather than skipped if that ever changes:
            # a copy that misses the stream's updates is a copy that is silently wrong.
            raise EngineError(
                f"{target} has copies being written, and a book stream writes only the source"
            )
        name = "replace_levels" if whole else "apply_levels"
        write = getattr(engine, name, None)
        if write is None:
            raise EngineError(f"{target} is placed on an engine that does not take a book by price")
        table = materialization.layout.table_for(target)
        started = perf_counter_ns() if self._recorder else 0
        failed = False
        try:
            write(table, symbol=symbol, exchange=exchange, side=side, timestamp_ns=timestamp_ns,
                  levels=levels)
            self._session_usage.check()
        except BaseException:
            failed = True
            raise
        finally:
            self._observe(shape, started, rows=len(levels), failed=failed)

    # --- dual write ----------------------------------------------------------------------------

    def _fan_out(
        self, entity: str, group: str, values: Mapping[str, Any] | tuple[dict[str, Any], ...]
    ) -> None:
        """Write the row to every ``also_write`` copy of the group. Additionally, never
        authoritatively.

        Requirement 9.1 and task 12.3: **a failure here does not interrupt the client's
        operation.** The row is in the source, which is the copy that counts, and turning a
        migration into an application outage would make the safest thing this product does the
        most dangerous. So the divergence is recorded and `VERIFY` is the gate that refuses to
        switch reads while any of them remain.

        Inside a write transaction the fan-out is **deferred to commit** rather than skipped or
        done inline, and each of those three was considered. Inline is wrong: the target is a
        different engine, so it is outside the source's transaction, and a rolled-back row would
        exist in the copy - which after the switch is a row the client explicitly undid, readable.
        Skipping is wrong for a quieter reason: those rows are above the backfill marker, so
        nothing else copies them, and `VERIFY`'s tail check would refuse the migration of every
        group that uses a transaction. Deferring keeps both properties: the copy only ever receives
        committed rows, and it receives all of them.
        """
        placement = self._placement.placement_of(group)
        if not placement.also_write:
            return
        for copy in placement.also_write:
            table = copy.layout.table_for(entity)
            body = dict(values) if isinstance(values, Mapping) else values
            if self._in_write_transaction:
                self._deferred.append(
                    (copy.engine, table, group, copy.id, perf_counter_ns(), body)
                )
                continue
            self._replay_one(copy.engine, table, group, copy.id, perf_counter_ns(), body)

    def _replay_one(
        self,
        engine_name: str,
        table: str,
        group: str,
        materialization: str,
        queued_ns: int,
        values: _WriteValues,
    ) -> None:
        """Write one row to one copy, measure how long the copy was behind, and never raise.

        ``queued_ns`` is when the row was handed to the fan-out, so the interval measured is the
        whole time the copy did not have a row the source did. Outside a transaction that is the
        duration of this write; inside one it also includes the rest of the transaction, which
        **overstates** the staleness - the safe direction for a bound somebody checks a budget
        against.
        """
        failed = False
        try:
            engine = self._engines[engine_name]
            if isinstance(values, tuple):
                bulk_writer(engine).insert_many(
                    table, tuple(stamp_values(self._placement, group, row) for row in values)
                )
            else:
                engine.insert(table, stamp_values(self._placement, group, values))
        except Exception as exc:
            # Deliberately swallowed, and the only place in this library that swallows a write
            # failure. The narrow `Exception` rather than `BaseException` matters: a
            # KeyboardInterrupt or a SystemExit during a fan-out is not a divergence, it is a
            # process being told to stop, and treating it as one would log a lie and continue.
            failed = True
            log(
                "sde.migration.divergence",
                engine=engine_name,
                table=table,
                error=type(exc).__name__,
            )
        # After the `except` rather than in a `finally`, and the difference is one case. A failed
        # fan-out must be counted - a copy missing a thousand rows with an excellent p99 is the
        # report this measurement exists to make impossible - and `except Exception` above already
        # falls through to here, so both paths record. What a `finally` would add is the
        # **BaseException** path: a KeyboardInterrupt or SystemExit while the copy is being written
        # would record a fan-out that completed, with `failed=False`, for a row that never landed.
        # The comment above says an interrupt is not a divergence; recording it as a success would
        # be worse than not recording it, so it is not recorded at all.
        if self._recorder is not None:
            self._recorder.record_fan_out(
                group=group,
                materialization=materialization,
                nanoseconds=perf_counter_ns() - queued_ns,
                failed=failed,
            )

    @session_call
    def get(self, entity: str, key: Mapping[str, Any], *, fresh: bool = False) -> Any:
        try:
            target = self._entity(entity)
        except DeclarationError:
            # With hashed identifiers a name with no hash is one the model does not declare (8d).
            raise _no_entity(entity) from None
        given = self._fields_in(entity, key)
        try:
            spec = self._model.entity(target)
        except KeyError:
            raise _no_entity(entity) from None
        expected = tuple(sorted(spec.key))
        if tuple(sorted(given)) != expected:
            # Both lists are put back into the client's vocabulary: with hashing on, an error naming
            # digests tells them nothing about their own code.
            raise ModelPlanningError(
                f"a point read of {entity} needs exactly its key "
                f"{self._client_names(entity, expected)}, and was given "
                f"{self._client_names(entity, given)}. A partial key is a range read, which is a "
                "different shape and may well be routed somewhere else."
            )
        key = self._key(entity, target, given, expected)
        shape = self._shape(target, "point_read", expected)
        engine, materialization = self._target(shape, fresh=fresh)
        table = materialization.layout.table_for(target)
        started = perf_counter_ns() if self._recorder else 0
        failed = False
        row: Any = None
        try:
            row = engine.get(table, key)
        except BaseException:
            failed = True
            raise
        finally:
            self._observe(shape, started, rows=0 if row is None else 1, failed=failed)
        return self._fields_out(entity, logical_row(self._placement, row))

    def _prepare_read(
        self, entity: str, *, where: Mapping[str, Any] | None, bounds: Range | None,
        order_by: str | None = None, descending: bool = False,
        after: Mapping[str, Any] | None = None, limit: int = 100, paginate: bool = True,
    ) -> tuple[str, ReadPlan]:
        if not isinstance(entity, str):
            raise QueryRefused("a query needs a logical entity name")
        try:
            target = self._entity(entity)
            spec = self._model.entity(target)
        except (KeyError, ModelPlanningError):
            raise QueryRefused("query refers to an entity this model does not declare") from None

        def translated(values: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
            if values is None:
                return None
            if not isinstance(values, Mapping) or any(not isinstance(name, str) for name in values):
                raise QueryRefused("query arguments must map logical field names to values")
            try:
                return self._fields_in(entity, values)
            except ModelPlanningError as exc:
                raise QueryRefused(str(exc)) from exc

        def field(name: str) -> str:
            if not isinstance(name, str):
                raise QueryRefused("query fields must be logical field names")
            mapped = translated({name: None})
            assert mapped is not None
            return next(iter(mapped))

        if bounds is not None:
            if not isinstance(bounds, Range):
                raise QueryRefused("bounds must be a Range")
            bounds = Range(field(bounds.field), bounds.low, bounds.high)
        columns = group_columns(self._model, self.group_of(entity))[target]
        plan = plan_read(
            tuple(ReadColumn(name, kind) for name, kind in sorted(columns.items())), spec.key,
            where=translated(where), bounds=bounds,
            order_by=None if order_by is None else field(order_by), descending=descending,
            after=translated(after), limit=limit, paginate=paginate,
        )
        return target, plan

    @staticmethod
    def _read_projection(
        material: Materialization, entity: str, plan: ReadPlan, *, count: bool,
    ) -> None:
        required = {predicate.column.name for predicate in plan.filters}
        if not count:
            required.update(column.name for column in plan.columns)
        available = set(material.layout.columns.get(entity, {}))
        if not required <= available:
            raise QueryRefused(
                "the routed materialization does not contain every field this query needs; "
                "request fresh=True to read the source or have the placement revised"
            )

    @session_call
    def scan(
        self, entity: str, *, where: Mapping[str, Any] | None = None,
        bounds: Range | None = None, order_by: str | None = None, descending: bool = False,
        after: Mapping[str, Any] | None = None, limit: int = 100, fresh: bool = False,
    ) -> ScanPage:
        """Read one bounded page in logical order, independently of migration checkpoints."""
        if not isinstance(fresh, bool):
            raise QueryRefused("fresh must be a bool")
        target, plan = self._prepare_read(
            entity, where=where, bounds=bounds, order_by=order_by, descending=descending,
            after=after, limit=limit,
        )
        if bounds is None:
            shape = self._shape(target, "full_scan")
        else:
            ranged = next(item for item in plan.filters if item.operation in ("ge", "lt"))
            shape = self._shape(target, "range_read", (ranged.column.name,))
        engine, material = self._target(shape, fresh=fresh)
        self._read_projection(material, target, plan, count=False)
        reader = query_engine(engine)
        table = material.layout.table_for(target)
        started = perf_counter_ns() if self._recorder else 0
        failed, returned = False, 0
        try:
            rows = reader.select_rows(table, plan)
            self._session_usage.check()
            visible = rows[:plan.limit]
            position = None
            if len(rows) > plan.limit:
                position = self._fields_out(
                    entity, {column.name: visible[-1][column.name] for column in plan.order}
                )
                position = MappingProxyType(snapshot_rows([position])[0])
            result = tuple(self._fields_out(entity, row) for row in visible)
            returned = len(result)
            return ScanPage(result, position)
        except BaseException:
            failed = True
            raise
        finally:
            self._observe(shape, started, rows=returned, failed=failed, plan=plan)

    @session_call
    def book_at(
        self,
        entity: str,
        *,
        symbol: str,
        exchange: str,
        at_ns: int,
        depth: int | None = None,
        fresh: bool = False,
    ) -> dict[str, list[tuple[int, int, int]]]:
        """The book of ``entity`` for one symbol on one exchange as of ``at_ns``, composed by the
        orderbook engine from what was written - by price, in time order: ``{"bid": [...],
        "ask": [...]}``, each side best first as (price, quantity, order count), at most ``depth``
        levels a side.

        The read side of :meth:`book_stream`: a stream that sends only changes stores only
        changes, and this answers the book they make. Recorded as a range read of the entity's
        time, which is what it is: a book is its history up to ``at_ns``.
        """
        if not isinstance(fresh, bool):
            raise QueryRefused("fresh must be a bool")
        target = self._entity(entity)

        def field(name: str) -> str:
            try:
                return next(iter(self._fields_in(entity, {name: None})))
            except ModelPlanningError as exc:
                raise QueryRefused(
                    f"{entity} is not a book: it has no field {name!r} ({exc})"
                ) from exc

        moment = field("timestamp_ns")
        equal = sorted((field("symbol"), field("exchange")))
        shape = self._shape(target, "range_read", (moment,))
        engine, material = self._target(shape, fresh=fresh)
        read = getattr(engine, "book_at", None)
        if read is None:
            raise QueryRefused(
                f"{entity} is placed on an engine that does not compose a book: the orderbook "
                f"engine does"
            )
        table = material.layout.table_for(target)
        started = perf_counter_ns() if self._recorder else 0
        failed, returned = False, 0
        try:
            book: dict[str, list[tuple[int, int, int]]] = read(
                table, symbol=symbol, exchange=exchange, at_ns=at_ns, depth=depth
            )
            self._session_usage.check()
            returned = sum(len(levels) for levels in book.values())
            return book
        except BaseException:
            failed = True
            raise
        finally:
            self._observe(shape, started, rows=returned, failed=failed,
                          predicates={"equal": equal, "ranged": moment})

    @session_call
    def search(
        self, entity: str, text: str, *, fields: Sequence[str] | None = None,
        after: Mapping[str, Any] | None = None, limit: int = 100,
        max_examined: int = DEFAULT_MAX_EXAMINED,
    ) -> SearchPage:
        """The rows of ``entity`` whose chosen text fields hold every token of ``text``.

        At most ``limit`` of them, in key order, with the position to continue from - a page, as
        :meth:`scan` reads one. ``fields`` are the entity's ``string`` fields to look in, all of
        them when omitted. What a token is and when a row matches is the format contract's rule
        (section 7l), applied by :mod:`sde.search` to every row before it is returned: the engine
        supplies candidates and never decides, so the answer is the same in every engine.

        A search always reads the group's source, the one materialisation known to hold every
        row. An engine with a text index supplies only rows that hold the tokens; one without
        supplies rows in key order and the library examines them, at most ``max_examined`` in one
        call. A page cut short by that bound keeps the position of the last row examined, so
        ``next_after`` is ``None`` only when nothing is left - a short page is not the last page.
        """
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise SearchRefused(f"limit must be an integer between 1 and {MAX_SEARCH_LIMIT}")
        if not 1 <= limit <= MAX_SEARCH_LIMIT:
            raise SearchRefused(f"limit must be an integer between 1 and {MAX_SEARCH_LIMIT}")
        if isinstance(max_examined, bool) or not isinstance(max_examined, int):
            raise SearchRefused(f"max_examined must be an integer between 1 and {MAX_EXAMINED}")
        if not 1 <= max_examined <= MAX_EXAMINED:
            raise SearchRefused(f"max_examined must be an integer between 1 and {MAX_EXAMINED}")
        query = query_tokens(text)
        target, plan = self._prepare_read(
            entity, where=None, bounds=None, after=after, limit=MAX_PAGE_ROWS
        )
        chosen = self._search_fields(entity, target, fields)
        shape = self._shape(target, "search")
        engine, material = self._target(shape, fresh=False)
        self._read_projection(material, target, plan, count=False)
        table = material.layout.table_for(target)
        search_engine(engine)
        indexed = text_index(engine)
        reader = None if indexed is not None else query_engine(engine)
        started = perf_counter_ns() if self._recorder else 0
        failed, examined = False, 0
        found: list[dict[str, Any]] = []
        try:
            position: Mapping[str, Any] | None = None
            while True:
                # An index supplies rows that hold the tokens, so it is asked for what the page
                # still needs; a scan supplies everything, so it reads as far as the bound allows.
                wanted = max_examined - examined
                if indexed is not None:
                    wanted = min(wanted, limit - len(found))
                plan = replace(plan, limit=min(MAX_PAGE_ROWS, wanted))
                if indexed is not None:
                    rows = indexed.search_rows(table, plan, query, chosen)
                else:
                    assert reader is not None
                    rows = reader.select_rows(table, plan)
                self._session_usage.check()
                candidates = rows[:plan.limit]
                more = len(rows) > plan.limit
                stopped = None
                for index, row in enumerate(candidates):
                    examined += 1
                    if matches(row, query, chosen):
                        found.append(row)
                    if len(found) == limit or examined == max_examined:
                        stopped = index
                        break
                if stopped is not None:
                    if more or stopped < len(candidates) - 1:
                        position = candidates[stopped]
                    break
                if not more or not candidates:
                    break
                last = candidates[-1]
                plan = replace(
                    plan,
                    after=tuple(query_value(column, last[column.name]) for column in plan.order),
                )
            next_after = None
            if position is not None:
                key = {column.name: position[column.name] for column in plan.order}
                next_after = MappingProxyType(snapshot_rows([self._fields_out(entity, key)])[0])
            return SearchPage(
                tuple(self._fields_out(entity, row) for row in found), next_after, examined
            )
        except BaseException:
            failed = True
            raise
        finally:
            self._observe(
                shape, started, rows=0 if failed else len(found), failed=failed,
                examined=examined, searched=chosen,
            )

    def _search_fields(
        self, entity: str, target: str, fields: Sequence[str] | None
    ) -> tuple[str, ...]:
        """The model's names of the text fields a search looks in, sorted."""
        declared = {f.name: f.type for f in self._model.entity(target).fields}
        text = sorted(name for name, kind in declared.items() if kind == "string")
        if not text:
            raise SearchRefused(f"{entity} has no field of type string, so nothing to search")
        if fields is None:
            return tuple(text)
        refusal = "fields must be a non-empty list of the entity's text field names"
        if isinstance(fields, str) or not isinstance(fields, Sequence) or not fields:
            raise SearchRefused(refusal)
        chosen: set[str] = set()
        for name in fields:
            if not isinstance(name, str):
                raise SearchRefused(refusal)
            try:
                chosen.update(self._fields_in(entity, {name: None}))
            except ModelPlanningError as exc:
                raise SearchRefused(str(exc)) from exc
        unknown = sorted(chosen - set(declared))
        if unknown:
            raise SearchRefused(f"{entity} declares no field named {unknown}")
        other = chosen - set(text)
        if other:
            named = sorted(self._fields_out(entity, dict.fromkeys(other)))
            raise SearchRefused(f"a search looks in fields of type string only, not in {named}")
        return tuple(sorted(chosen))

    @session_call
    def count(
        self, entity: str, *, where: Mapping[str, Any] | None = None,
        bounds: Range | None = None, fresh: bool = False,
    ) -> int:
        """Count matching rows in the engine. The count value itself is never telemetry."""
        if not isinstance(fresh, bool):
            raise QueryRefused("fresh must be a bool")
        target, plan = self._prepare_read(entity, where=where, bounds=bounds, paginate=False)
        shape = self._shape(target, "aggregate")
        engine, material = self._target(shape, fresh=fresh)
        self._read_projection(material, target, plan, count=True)
        reader = count_engine(engine)
        table = material.layout.table_for(target)
        started = perf_counter_ns() if self._recorder else 0
        failed = False
        try:
            result = reader.count_rows(table, plan)
            self._session_usage.check()
            return result
        except BaseException:
            failed = True
            raise
        finally:
            self._observe(shape, started, rows=0 if failed else 1, failed=failed, plan=plan)

    @session_call
    def measure_storage(self) -> StorageMeasurement:
        """Measure each group's size on its source materialisation, from the engine's catalogue.

        One catalogue statement per engine, for every table of every group whose source is on it.
        Only the source counts: a copy a staging is still building is not the group's size. Each
        size is recorded into this session's recorder, when it has one, for the window's
        ``total_bytes``, ``index_to_table_ratio`` and ``daily_growth_bytes``.

        **It never raises because an engine could not answer.** An adapter with no catalogue, a
        refused read, a failed one or a table the map names that does not exist leaves that group's
        size unknown - in ``unavailable`` and in an ``sde.telemetry.storage_unavailable`` line with
        the engine and the class of the reason, never the engine's message. Using a closed session
        or another owner's still raises, as every method of a session does.
        """
        by_engine: dict[str, list[tuple[str, Materialization]]] = {}
        for name in sorted(self._placement.groups):
            source = self._placement.groups[name].source
            by_engine.setdefault(source.engine, []).append((name, source))
        sizes: list[StorageSize] = []
        unavailable: dict[str, str] = {}
        for engine_name in sorted(by_engine):
            members = by_engine[engine_name]
            reader = getattr(self._engines[engine_name], "storage_sizes", None)
            if not callable(reader):
                unavailable.update((name, "unsupported") for name, _ in members)
                continue
            tables = sorted(
                {table for _, source in members for table in source.layout.tables.values()}
            )
            try:
                found: Mapping[str, tuple[int, int]] = reader(tables)
            except (ResourceBusy, ResourceClosed):
                raise  # misuse of an adapter is the caller's to see, not an unknown size
            except Exception as exc:
                reason = _storage_refusal(exc)
                unavailable.update((name, reason) for name, _ in members)
                continue
            self._session_usage.check()
            for name, source in members:
                named = list(source.layout.tables.values())
                if any(table not in found for table in named):
                    unavailable[name] = "missing_table"
                    continue
                size = StorageSize(
                    group=name,
                    materialization=source.id,
                    engine=engine_name,
                    total_bytes=sum(found[table][0] for table in named),
                    secondary_index_bytes=sum(found[table][1] for table in named),
                )
                sizes.append(size)
                if self._recorder is not None:
                    self._recorder.record_storage(
                        group=name,
                        total_bytes=size.total_bytes,
                        secondary_index_bytes=size.secondary_index_bytes,
                    )
        for name in sorted(unavailable):
            source = self._placement.groups[name].source
            log(
                "sde.telemetry.storage_unavailable",
                engine=source.engine,
                reason=unavailable[name],
            )
        return StorageMeasurement(
            tuple(sorted(sizes, key=lambda size: size.group)),
            MappingProxyType(dict(sorted(unavailable.items()))),
        )

    @session_call
    def summarize(
        self, entity: str, field: str, *, where: Mapping[str, Any] | None = None,
        bounds: Range | None = None, mean_scale: int = 6, fresh: bool = False,
    ) -> NumericSummary:
        """Exact integer/decimal statistics; half-even mean at an explicit decimal scale."""
        if not isinstance(fresh, bool):
            raise QueryRefused("fresh must be a bool")
        target, plan = self._prepare_read(entity, where=where, bounds=bounds, paginate=False)
        if not isinstance(field, str):
            raise QueryRefused("a summary needs a logical field name")
        try:
            name = next(iter(self._fields_in(entity, {field: None})))
        except ModelPlanningError as exc:
            raise QueryRefused(str(exc)) from exc
        column = next((item for item in plan.columns if item.name == name), None)
        if column is None:
            raise QueryRefused("summary refers to a field this entity does not declare")
        summary_scale(column, mean_scale)
        shape = self._shape(target, "aggregate")
        engine, material = self._target(shape, fresh=fresh)
        self._read_projection(material, target, plan, count=True)
        if name not in material.layout.columns.get(target, {}):
            raise QueryRefused("the routed materialization does not contain the summary field")
        reader = summary_engine(engine)
        table = material.layout.table_for(target)
        started = perf_counter_ns() if self._recorder else 0
        failed = False
        try:
            raw = reader.summarize_rows(table, plan, column)
            self._session_usage.check()
            return numeric_summary(raw, column, mean_scale)
        except BaseException:
            failed = True
            raise
        finally:
            self._observe(shape, started, rows=0 if failed else 1, failed=failed, plan=plan)

    def _observe(
        self,
        shape: OperationShape,
        started: int,
        *,
        rows: int,
        failed: bool,
        plan: ReadPlan | None = None,
        predicates: Mapping[str, Any] | None = None,
        examined: int = 0,
        searched: Sequence[str] | None = None,
    ) -> None:
        """Hand one observation to the recorder, if there is one.

        The timing call is skipped entirely when telemetry is off, which is why `started` is
        passed in rather than measured here: with no recorder this method is one attribute check.
        A read without a plan - a book's - names what it filtered on as ``predicates``.
        """
        recorder = self._recorder
        if recorder is None:
            return
        recorder.record(
            shape_id=shape.id,
            group=shape.group,
            entity=shape.entity,
            kind=shape.kind,
            nanoseconds=perf_counter_ns() - started,
            rows=rows,
            failed=failed,
            **(_predicates(plan) if plan is not None else dict(predicates or {})),
            examined=examined,
            searched=searched,
        )

    # --- transactions ----------------------------------------------------------------------

    @contextmanager
    def transaction(self, *entities: str) -> Iterator[Session]:
        """Open a transaction covering the given entities.

        They must share a colocation group, because a transaction is one engine's transaction. If
        they do not, this raises before anything is opened and names the fix: declare the atomicity,
        and the planner will colocate them.

        Called with no entities it covers the whole model, which is only legal when the model has
        one group. That is not a convenience for small models so much as a refusal to let a
        two-group model quietly get a transaction that only covers half of what the caller meant.
        """
        # Entity names in the *client's* vocabulary, so that an error message and the entities they
        # passed are in the same language. With hashing on, `self._model.entities` are digests, and
        # feeding those back through `group_of` would try to hash a hash.
        declared = self._declared or tuple(e.name for e in self._model.entities)
        names = list(entities) if entities else list(declared)
        groups = {self.group_of(name).name for name in names}
        if len(groups) > 1:
            by_group: dict[str, list[str]] = {}
            for name in sorted(names):
                by_group.setdefault(self.group_of(name).name, []).append(name)
            layout = "; ".join(
                f"{g}: {', '.join(members)}" for g, members in sorted(by_group.items())
            )
            raise ModelPlanningError(
                f"a transaction cannot span colocation groups ({layout}). One group is one "
                "engine and one engine's transaction; there is no distributed transaction here and "
                "there will not be one. If these entities have to commit together, declare it with "
                "`atomic_with` on either side, and the planner will place them in the same engine "
                "- which turns the requirement into a placement constraint instead of a two-phase "
                "commit."
            )

        group = self.group_of(names[0])
        with self._session_usage.transaction(group.name):
            engine = self._engines[self._placement.placement_of(group.name).source.engine]
            previous = self._in_write_transaction
            outer = len(self._deferred)
            self._in_write_transaction = True
            committed = False
            try:
                with engine.transaction():
                    yield self
                    self._session_usage.idle()
                    self._session_usage.seal()
                committed = True
            finally:
                self._in_write_transaction = previous
                pending = self._deferred[outer:]
                del self._deferred[outer:]
                if committed and not previous:
                    # Replayed after the source transaction has committed, and only by the outermost
                    # one: a nested block that returns to a still-open transaction has not committed
                    # anything yet, so its rows go back on the queue rather than to the copy.
                    for engine_name, table, group_name, copy_id, queued_ns, values in pending:
                        self._replay_one(engine_name, table, group_name, copy_id, queued_ns, values)
                elif not committed:
                    # Rolled back. The rows never existed in the source, so they must never exist in
                    # the copy - dropping them is the whole reason the fan-out was deferred.
                    pass
                else:
                    self._deferred.extend(pending)


def _predicates(plan: ReadPlan) -> dict[str, Any]:
    """What a read filtered on, for telemetry: field names only, never a value.

    The names are the model's own - digests when the client hashes them, like the shape's fields.
    """
    equal = sorted({item.column.name for item in plan.filters if item.operation == "eq"})
    bounded = sorted({item.column.name for item in plan.filters if item.operation != "eq"})
    return {"equal": equal, "ranged": bounded[0] if bounded else None}


class _Admission:
    """What a row of one entity must and may carry, computed once per session."""

    __slots__ = ("checks", "declared", "needed", "required", "temporal")

    def __init__(
        self,
        declared: frozenset[str],
        required: tuple[str, ...],
        checks: tuple[tuple[str, str, Check], ...],
    ) -> None:
        self.declared = declared
        self.required = required
        """The fields a row must give a value, in code-point order: the order a refusal names."""
        self.needed = frozenset(required)
        self.checks = checks
        """Each typed field, its neutral type and its check, in code-point order."""
        self.temporal = tuple((name, kind) for name, kind, _ in checks if kind in TEMPORAL_KINDS)
        """The date and timestamp fields, which an engine's range may not hold (section 8b)."""


def _admission(
    model: LogicalModel, placement: PlacementMap, groups: Sequence[Group]
) -> dict[str, _Admission]:
    # The write-generation column is left to `stamp_values`, which refuses it in a map that has
    # generations with its own reason: it is reserved, not merely undeclared.
    reserved = frozenset({EPOCH_COLUMN}) if placement.contract >= GENERATIONS_SINCE else frozenset()
    admission: dict[str, _Admission] = {}
    for group in groups:
        columns = group_columns(model, group)
        for entity in group.members:
            spec = model.entity(entity)
            required = tuple(
                sorted(f.name for f in spec.fields if not f.nullable or f.name in spec.key)
            )
            checks = tuple(
                (name, kind, check)
                for name, kind in sorted(columns[entity].items())
                if (check := check_for(kind)) is not None
            )
            admission[entity] = _Admission(frozenset(columns[entity]) | reserved, required, checks)
    return admission


def _null_required(values: Mapping[str, Any], required: tuple[str, ...]) -> str | None:
    """The first required field the row gives ``None``, or ``None`` if there is none."""
    for name in required:
        if values[name] is None:
            return name
    return None

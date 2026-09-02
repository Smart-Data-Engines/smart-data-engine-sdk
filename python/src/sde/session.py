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

from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from time import perf_counter_ns
from typing import Any, Protocol

from .errors import EngineError, ModelPlanningError
from .groups import Group, colocation_groups
from .hashing import NameMap
from .logging import log
from .model import LogicalModel
from .placement import Materialization, PlacementMap
from .routing import Router
from .shapes import OperationShape, enumerate_shapes
from .telemetry import Recorder
from .watermark import WatermarkCheck, enforce_forward_only

__all__ = ["Engine", "Session"]


class Engine(Protocol):
    """What a session needs from an engine adapter, and nothing more.

    A protocol rather than a base class so that an adapter for another engine - or a fake, in
    somebody else's test suite - does not have to import anything from us to satisfy it.
    """

    dialect: str

    def ensure_schema(self, layout: Any, *, keys: Mapping[str, Any]) -> None: ...
    def insert(self, table: str, values: Mapping[str, Any]) -> None: ...
    def get(self, table: str, key: Mapping[str, Any]) -> dict[str, Any] | None: ...

    @contextmanager
    def transaction(self) -> Iterator[Any]: ...


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
    ) -> None:
        # Telemetry is optional and off by default. A library that starts measuring the moment it is
        # imported is a library people are right to be suspicious of; measurement begins when a
        # recorder is handed in, which is a visible line in the client's code.
        self._recorder = recorder

        # The one place where the client's names and the hashed ones meet. When a model has been
        # hashed, everything downstream of here - the map, the shapes, the tables, the telemetry -
        # speaks digests only, and the application keeps saying `save("User", {"email": ...})`.
        # Without this the mode is unusable: a client would have to write `save("e_546526714dc9",
        # {"f_b1b4a0ec9efb": ...})` in their own code, which nobody will do and which would put the
        # digests in their source anyway.
        self._names = names
        self._reverse_fields: dict[str, dict[str, str]] = {}
        self._declared: tuple[str, ...] = ()
        if names is not None:
            for entity, mapping in names.fields.items():
                self._reverse_fields[names.entity(entity)] = {
                    hashed: original for original, hashed in mapping.items()
                }
            self._declared = tuple(sorted(names.entities))
        self._model = model
        self._placement = placement
        self._engines = dict(engines)
        self._router = Router(placement)
        self._groups: tuple[Group, ...] = colocation_groups(model)
        self._shapes = {
            (s.entity, s.kind, s.fields): s for s in enumerate_shapes(model)
        }
        self._in_write_transaction = False

        missing = sorted(
            {m.engine for p in placement.groups.values() for m in p.all()} - set(self._engines)
        )
        if missing:
            raise EngineError(
                f"the placement map refers to engines that were not supplied: {missing}. A session "
                "cannot route an operation to an engine it has no adapter for, and guessing at a "
                "connection is not something a library should do."
            )

        # Here rather than in a method somebody has to remember to call, and here rather than in
        # `ensure_schema`, which a deployment past its first release skips. A rolled-back map file
        # is read at process start, so the check has to be on the path every start takes - and this
        # constructor already refuses a map it cannot route, which is the same kind of refusal in
        # the same place. It costs one statement per participating engine, once per process, and
        # nothing at all for an unsigned map.
        self._forward_only = enforce_forward_only(placement, self._engines)

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
        mapping = self._names.fields[entity]
        try:
            return {mapping[field]: value for field, value in values.items()}
        except KeyError as exc:
            raise ModelPlanningError(
                f"{entity} has no field {exc.args[0]!r}. With hashed identifiers a field the model "
                "does not declare cannot be translated, so it is refused here rather than sent to "
                "an engine under a name nothing will recognise."
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
            raise ModelPlanningError(
                f"the model admits no {kind} on {entity} over {list(fields)}. Shapes are "
                "enumerated from the model, so an operation with no shape is one the planner never "
                "saw and therefore never routed, which makes it a modelling gap rather than a "
                "runtime error."
            ) from None

    def _target(self, shape: OperationShape, *, fresh: bool) -> tuple[Engine, Materialization]:
        materialization = self._router.resolve(
            shape, in_write_transaction=self._in_write_transaction, fresh=fresh
        )
        return self._engines[materialization.engine], materialization

    # --- schema ----------------------------------------------------------------------------

    def ensure_schema(self) -> None:
        """Create what each engine is missing for the groups placed in it."""
        for group in self._groups:
            placement = self._placement.placement_of(group.name)
            keys = {name: self._model.entity(name).key for name in group.members}
            for materialization in placement.all():
                engine = self._engines[materialization.engine]
                engine.ensure_schema(materialization.layout, keys=keys)
        log("sde.schema.applied", groups=len(self._groups))

    # --- data ------------------------------------------------------------------------------

    def save(self, entity: str, values: Mapping[str, Any]) -> None:
        target = self._entity(entity)
        values = self._fields_in(entity, values)
        shape = self._shape(target, "write")
        engine, materialization = self._target(shape, fresh=False)
        table = materialization.layout.table_for(target)
        started = perf_counter_ns() if self._recorder else 0
        failed = False
        try:
            engine.insert(table, values)
        except BaseException:
            failed = True
            raise
        finally:
            # In a finally block on purpose: a failed write is exactly the operation whose
            # latency and error count matter most to a placement decision, and it is the one an
            # early return would silently omit.
            self._observe(shape, started, rows=1, failed=failed)

    def get(self, entity: str, key: Mapping[str, Any], *, fresh: bool = False) -> Any:
        target = self._entity(entity)
        given = self._fields_in(entity, key)
        spec = self._model.entity(target)
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
        shape = self._shape(target, "point_read", expected)
        engine, materialization = self._target(shape, fresh=fresh)
        table = materialization.layout.table_for(target)
        key = given
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
        return self._fields_out(entity, row)

    def _observe(self, shape: OperationShape, started: int, *, rows: int, failed: bool) -> None:
        """Hand one observation to the recorder, if there is one.

        The timing call is skipped entirely when telemetry is off, which is why `started` is
        passed in rather than measured here: with no recorder this method is one attribute check.
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
        engine = self._engines[self._placement.placement_of(group.name).source.engine]
        previous = self._in_write_transaction
        self._in_write_transaction = True
        try:
            with engine.transaction():
                yield self
        finally:
            self._in_write_transaction = previous

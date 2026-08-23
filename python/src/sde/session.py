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

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any, Protocol

from .errors import EngineError, ModelPlanningError
from .groups import Group, colocation_groups
from .logging import log
from .model import LogicalModel
from .placement import Materialization, PlacementMap
from .routing import Router
from .shapes import OperationShape, enumerate_shapes

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
    ) -> None:
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

    # --- structure -------------------------------------------------------------------------

    def group_of(self, entity: str) -> Group:
        for group in self._groups:
            if entity in group:
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
        shape = self._shape(entity, "write")
        engine, materialization = self._target(shape, fresh=False)
        engine.insert(materialization.layout.table_for(entity), values)

    def get(self, entity: str, key: Mapping[str, Any], *, fresh: bool = False) -> Any:
        spec = self._model.entity(entity)
        expected = tuple(sorted(spec.key))
        if tuple(sorted(key)) != expected:
            raise ModelPlanningError(
                f"a point read of {entity} needs exactly its key {list(expected)}, and was given "
                f"{sorted(key)}. A partial key is a range read, which is a different shape and may "
                "well be routed somewhere else."
            )
        shape = self._shape(entity, "point_read", expected)
        engine, materialization = self._target(shape, fresh=fresh)
        return engine.get(materialization.layout.table_for(entity), key)

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
        names = list(entities) if entities else [e.name for e in self._model.entities]
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

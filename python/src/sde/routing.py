"""Routing: a lookup and three conditions, and deliberately nothing more.

The library does not decide where an operation goes. The planner decided, ahead of time, for every
shape the model admits, and put the answers in the placement map. This module reads them.

That division is the reason it is affordable to have four libraries. Decisions need telemetry,
history, a cost model and an explanation, and they need to be reproducible and testable - all of
which live on our side, once. If the library decided anything, that judgement would have to be
reimplemented in Python, TypeScript, Java and Rust, and kept identical in all four forever. So it
looks things up.

The three conditions that are *not* lookups exist because they are correctness, not judgement:

1. Writes go to the source materialisation. There is exactly one, and derived copies are derived.
2. An operation inside a transaction that has already written goes to the source, because a derived
   copy is behind by design and would not show the write the caller just made.
3. An operation asking for no staleness goes to the source, for the same reason.

Anything else follows the routing table, and if the table has nothing to say the answer is the
source. That fallback is safe by construction, since the source is always correct and merely
sometimes slower, and it is what makes a hand-written map a two-line affair rather than a table of
hashes.
"""

from __future__ import annotations

from dataclasses import dataclass

from .logging import log
from .placement import Materialization, PlacementMap
from .shapes import OperationShape

__all__ = ["Router", "resolve"]

_WRITE_KINDS = frozenset({"write", "bulk_write"})


@dataclass(frozen=True)
class Router:
    """Resolves shapes against one placement map."""

    placement: PlacementMap

    def resolve(
        self,
        shape: OperationShape,
        *,
        in_write_transaction: bool = False,
        fresh: bool = False,
    ) -> Materialization:
        group = self.placement.placement_of(shape.group)

        if shape.kind in _WRITE_KINDS:
            return group.source
        if in_write_transaction or fresh:
            return group.source

        target_id = self.placement.routing.get(shape.id)
        if target_id is None:
            # Not an error. A map with no routing table is the normal shape of a hand-written one,
            # and the source is always a correct answer.
            log("sde.route.fallback", group=shape.group, shape=shape.id, kind=shape.kind)
            return group.source

        materialization = group.by_id(target_id)
        log(
            "sde.route.resolved",
            group=shape.group,
            shape=shape.id,
            kind=shape.kind,
            materialization=materialization.id,
            engine=materialization.engine,
        )
        return materialization


def resolve(
    placement: PlacementMap,
    shape: OperationShape,
    *,
    in_write_transaction: bool = False,
    fresh: bool = False,
) -> Materialization:
    """Functional form, for the conformance vectors and for one-off resolution."""
    return Router(placement).resolve(
        shape, in_write_transaction=in_write_transaction, fresh=fresh
    )

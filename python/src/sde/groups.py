"""Colocation groups: the unit of placement.

Entities that are queried together, or that must change together, live in the same engine. The graph
has an edge for every relation and for every declared atomicity, and a group is a connected
component of it.

This looks like a limitation and is the opposite. A join across two engines means pulling both sides
over the network and joining in the client's process: slow, memory-hungry, and the single easiest
way for a young product to embarrass itself. Making colocation a constraint turns that problem into
something the planner simply respects, and the consistency contract falls straight out of it - one
group, one engine, that engine's transaction semantics, and no distributed transactions anywhere.

The obvious worry is that every relation being an edge collapses a normalised model into one group,
leaving nothing to place. In practice it does not, and the reason is worth understanding because it
is the product's whole thesis. Take a typical application: ``User``, ``Order``, ``OrderLine``,
``Product``, ``Event``. The first four are related and become one group. ``Event`` references
nothing and becomes its own. That is exactly the split that matters: the transactional core belongs
in a row store, the event stream belongs in a column store, and the entities nobody joins are
precisely the ones that were sitting in the wrong engine all along.

A client who wants two related entities in different engines can have that, and finds out about the
cost honestly: the relation stops being traversable, and the error at model-planning time says which
entities would have to share a group for the query to be possible.
"""

from __future__ import annotations

from dataclasses import dataclass

from .model import LogicalModel

__all__ = ["Group", "colocation_groups", "group_of"]


@dataclass(frozen=True)
class Group:
    """A set of entities placed together.

    ``name`` is the alphabetically first member. It exists so that logs, proposals and error
    messages can say ``group "order"`` instead of a hash, and it is only meaningful within one model
    version - change the membership and you have changed the model, which changes its version.
    """

    name: str
    members: tuple[str, ...]

    def __contains__(self, entity: str) -> bool:
        return entity in self.members


def colocation_groups(model: LogicalModel) -> tuple[Group, ...]:
    """Connected components of the colocation graph, deterministically ordered.

    Determinism here is not a nicety. The group name reaches the placement map, the telemetry and
    the planner's decisions, so two runs over the same model have to produce the same names or the
    control plane sees a model whose groups keep being renamed.
    """
    names = sorted(e.name for e in model.entities)
    parent: dict[str, str] = {n: n for n in names}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            # Always attach to the alphabetically smaller root, so the representative of a component
            # does not depend on the order edges were visited in.
            parent[max(ra, rb)] = min(ra, rb)

    for relation in sorted(model.relations, key=lambda r: (r.source, r.name, r.target)):
        union(relation.source, relation.target)
    for atomic in model.atomic:
        first = atomic[0]
        for other in atomic[1:]:
            union(first, other)

    buckets: dict[str, list[str]] = {}
    for name in names:
        buckets.setdefault(find(name), []).append(name)

    groups = [
        Group(name=min(members), members=tuple(sorted(members)))
        for members in buckets.values()
    ]
    groups.sort(key=lambda g: g.name)
    return tuple(groups)


def group_of(groups: tuple[Group, ...], entity: str) -> Group:
    for group in groups:
        if entity in group:
            return group
    raise KeyError(f"{entity} is not in any group, which means it is not in the model")

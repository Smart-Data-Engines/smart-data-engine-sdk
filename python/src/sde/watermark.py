"""Refusing a placement map that goes backwards, and the durable state that makes it possible.

A signed map for version 3 verifies correctly forever - that is what a signature is. So replacing
the client's map file with an older signed one loads cleanly, routes writes to the previous
placement, and **nothing protests**. Today that costs a client a stale schema. Once the migration
state travels in the map, it costs them writes: a library reverted from dual-write to single-write
in the middle of a migration drops exactly the rows the migration exists not to drop.

Refusing it needs one thing the library has never had: **memory**. Everything else here is a pure
function of a document, a model and a key, which is why it can be verified by reading it. This
module is the exception, and each of the three obvious places to keep that memory is worse than the
one chosen:

- **in the process** protects until the first restart, and a restart is when a swapped file is
  read. A protection that lapses exactly when it is needed;
- **in a file** needs a configured path, and in a container that path is usually ephemeral - so it
  degrades to the first option while continuing to look like the third. The worst property
  available: a guarantee that is present in the code and absent in production;
- **with us** would mean the library asking our service whether it may start, which is the one
  thing this product promises it will never need to do. Our outage would become the client's.

So it lives **in the client's own engines**, in a table this library owns. The library already
creates tables there; this is one more, it holds no client data, and we still never see a row of it.

Four properties, and the first two are what make it safe rather than merely present.

**Append-only, and the watermark is `max(map_version)`.** No update, no key enforcement, no
row-level contention - and therefore identical semantics in PostgreSQL and in ClickHouse, which is
the engine that has no unique constraint to offer. A stale row can never lower the bar. It also
leaves an audit trail for free: which map versions this deployment has seen, and when.

**Every participating engine is written, and the watermark is the maximum over all of them.**
Losing an engine cannot lose the protection, and one engine lagging cannot weaken it.

**An engine that cannot store it does not participate, and that is reported rather than hidden.**
The orderbook engine has a schema fixed in its own source and no room for bookkeeping, so a client
whose only engine is that one has no rollback protection and cannot have any. The honest maximum is
to say so - :class:`WatermarkCheck` carries it and ``Session`` exposes it, because a protection
whose state cannot be read is a protection taken on trust.

**Only signed maps are checked.** An unsigned map is the client's own document: hand-writing one and
pointing a library at it is the no-account mode, and their business what they replace it with. A
signed map is one we issued, which is precisely when we are the authority on what the newest version
is. In pure no-account mode this module does nothing at all - no table, no query, no cost.

The escape hatch is deliberately not a parameter. A legitimate rollback - we issued a bad map -
means clearing the bookkeeping, and the refusal says how. A parameter called ``allow_rollback``
would be set once during an incident and left set.

**A limitation worth stating.** The watermark is per engine and the format has no field naming which
stream of maps a document belongs to, so an engine shared by two independent map streams would have
the higher one refusing the lower. The fix is a separate database per stream, which a shared engine
wants regardless; inventing a stream identifier would mean a new key in a signed document, which is
a loosening of the format and a contract bump in every language at once.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

from .errors import MapRolledBack
from .logging import log
from .placement import WATERMARK_TABLE, PlacementMap

__all__ = [
    "WATERMARK_TABLE",
    "Protection",
    "WatermarkCheck",
    "WatermarkStore",
    "enforce_forward_only",
]

Protection = Literal["enforced", "unavailable", "not_applicable"]


@runtime_checkable
class WatermarkStore(Protocol):
    """What an engine adapter needs to offer to take part.

    A separate protocol from :class:`~sde.session.Engine`, and optional. Adding two methods to
    ``Engine`` would break every adapter anybody has written against it - including the fakes in
    somebody else's test suite - for a capability one of our own three engines cannot provide
    anyway. So participation is discovered rather than required, and non-participation is a
    reportable state instead of a crash.
    """

    def map_watermark(self) -> int | None: ...
    def record_map_version(self, version: int, *, model_version: str) -> None: ...


@dataclass(frozen=True)
class WatermarkCheck:
    """What the check did, in a form a client can assert on.

    Exposed rather than kept private on purpose. A protection whose state cannot be read is a
    protection taken on trust, and this product's whole argument is that its guarantees are
    checkable by reading the code and now by reading this.
    """

    protection: Protection
    map_version: int
    highest_seen: int | None
    participating: tuple[str, ...]
    unable: tuple[str, ...]
    why: str

    def as_record(self) -> dict[str, Any]:
        return {
            "protection": self.protection,
            "map_version": self.map_version,
            "highest_seen": self.highest_seen,
            "participating": list(self.participating),
            "unable": list(self.unable),
            "why": self.why,
        }


def _split(engines: Mapping[str, Any]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Which engines can keep the bookkeeping and which cannot, both sorted."""
    able = sorted(name for name, engine in engines.items() if isinstance(engine, WatermarkStore))
    unable = sorted(set(engines) - set(able))
    return tuple(able), tuple(unable)


def enforce_forward_only(
    placement: PlacementMap, engines: Mapping[str, Any]
) -> WatermarkCheck:
    """Refuse a signed map older than the newest one these engines have seen.

    Raises :class:`~sde.errors.MapRolledBack`. Equal is allowed - restarting a process against the
    same map is the ordinary case - and only strictly lower is refused.
    """
    if not placement.signed:
        return WatermarkCheck(
            protection="not_applicable",
            map_version=placement.map_version,
            highest_seen=None,
            participating=(),
            unable=tuple(sorted(engines)),
            why=(
                "this map is unsigned, so it is your own document rather than one we issued. "
                "Replacing it with another is the no-account mode working as documented, and there "
                "is no newest version for us to be the authority on."
            ),
        )

    able, unable = _split(engines)
    if not able:
        check = WatermarkCheck(
            protection="unavailable",
            map_version=placement.map_version,
            highest_seen=None,
            participating=(),
            unable=unable,
            why=(
                f"none of the engines in this map can keep bookkeeping ({list(unable)}), so a map "
                f"that goes backwards cannot be recognised. An engine whose schema is fixed in its "
                f"own source - ours is - has nowhere to put it. Nothing is wrong with your "
                f"configuration; this protection simply does not exist for it."
            ),
        )
        log(
            "sde.map.rollback_unprotected",
            map_version=placement.map_version,
            engines=len(unable),
        )
        return check

    seen = [store.map_watermark() for store in (engines[name] for name in able)]
    known = [value for value in seen if value is not None]
    highest = max(known) if known else None

    if highest is not None and placement.map_version < highest:
        raise MapRolledBack(
            f"this map is version {placement.map_version} and version {highest} has already been "
            f"applied against these engines. Refusing to go backwards: an older signed map "
            f"verifies perfectly - that is what a signature is - so nothing else here would notice "
            f"that the file was replaced, and the writes would go to the previous placement. If "
            f"this is deliberate, because the newer map was wrong, clear the bookkeeping: "
            f"DELETE FROM {WATERMARK_TABLE} WHERE map_version > {placement.map_version}; in "
            f"{list(able)}, and on an engine that deletes asynchronously, let the deletion finish "
            f"before restarting. That is a deliberate act with a stated consequence, which is why "
            f"it is not a flag."
        )

    if highest is None or placement.map_version > highest:
        # Written only when it moves. Recording every start would grow the table by one row per
        # process restart, and the watermark would say nothing more than it does now.
        for name in able:
            store: WatermarkStore = engines[name]
            store.record_map_version(
                placement.map_version, model_version=placement.model_version
            )

    log(
        "sde.map.forward_only",
        map_version=placement.map_version,
        highest_seen=highest,
        engines=len(able),
    )
    return WatermarkCheck(
        protection="enforced",
        map_version=placement.map_version,
        highest_seen=highest,
        participating=able,
        unable=unable,
        why=(
            f"the highest map version applied against these engines is "
            f"{highest if highest is not None else placement.map_version}, kept in "
            f"{WATERMARK_TABLE} in {list(able)}. A map older than that is refused."
        ),
    )

"""Asking an engine adapter whether it will answer a call, which is not the same as its type.

Two optional protocols decide whether an engine takes part in something: :class:`
sde.watermark.WatermarkStore` for the forward-only map check, and :class:`sde.migration.Migratable`
for a migration. Both were originally asked with ``isinstance(engine, Protocol)``, which is the
obvious spelling and the wrong question.

Since Python 3.12 a ``runtime_checkable`` protocol resolves members with
:func:`inspect.getattr_static`, which deliberately ignores ``__getattr__``. So an object that
forwards to a wrapped adapter answers ``hasattr`` for every member of the protocol and still fails
``isinstance``. Wrapping an engine adapter is an ordinary thing for a client to do - metrics,
logging, a retry, a connection pool - and the consequence was two wrong diagnoses shipped as
helpful messages: "this engine has nowhere to keep the bookkeeping, so you have no rollback
protection", and "this engine cannot take part in a migration". Both would have named the client's
*engine* for a property of their own wrapper, and the second one refuses a migration outright.

Found from a test, not from review: a two-line proxy over the real ClickHouse adapter, written to
make one write fail, was refused as an engine with no row-level operations.

So the question asked here is the one that matters - **will this object respond to these calls** -
and it is asked with ordinary attribute access, which honours every way Python has of providing an
attribute.
"""

from __future__ import annotations

from typing import Any

__all__ = ["members_of", "satisfies"]


def members_of(protocol: type) -> tuple[str, ...]:
    """The public members a protocol names: its methods and its annotated data attributes.

    Both halves are needed and neither is enough. Methods have class attributes, so ``dir`` finds
    them; an annotated attribute with no value - ``dialect: str`` - exists only in
    ``__annotations__``. Assembled from two public sources rather than from
    ``__protocol_attrs__``, which is an implementation detail that did not exist in every version
    this library supports.
    """
    named = {name for name in dir(protocol) if not name.startswith("_")}
    named.update(
        name
        for name in getattr(protocol, "__annotations__", {})
        if not name.startswith("_")
    )
    return tuple(sorted(named))


def satisfies(obj: Any, protocol: type) -> bool:
    """Whether every member the protocol names can be reached on this object.

    Presence rather than callability, deliberately. A member that exists and is not callable fails
    at the call with a message naming it, which is a better failure than a capability check that
    quietly answers "no" and sends the reader to look at their engine.
    """
    return all(hasattr(obj, name) for name in members_of(protocol))

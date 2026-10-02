"""What each engine this library speaks to can and cannot do, as data, for whoever places groups.

The control plane hands these facts to the model that decides placement, because a model can only
weigh what it is told. Offered our orderbook engine as "dialect orderbook, cost unknown, no physical
design", a fresh agent put L2 depth on ClickHouse and called the choice a guess by name (measured on
2 October 2026). It did not know that the engine has one fixed shape, no transactions, no key
enforcement, and no write generations - so a group placed there can never be moved.

Every fact here is a statement about an adapter in this package, and the tests hold each one against
that adapter's behaviour: ``tests/test_engine_facts.py`` against the adapters' code, and the live
slices against the engines. A fact that drifts from the code fails the suite instead of misleading a
model. What the engines render as a physical design is a separate document,
:func:`sde.physical.capabilities`, and is not repeated here.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Final

from .layout import DIALECTS, ORDERBOOK_KEY, ORDERBOOK_NULLABLE, ORDERBOOK_SHAPE, ORDERBOOK_TABLE

__all__ = ["FACTS_VERSION", "engine_facts"]

FACTS_VERSION: Final = 1
"""Raised when a key changes meaning, so a request carrying facts says which meaning it used."""

_ALL_READS: Final[dict[str, bool]] = {"get": True, "scan": True, "count": True, "summarize": True}

_FACTS: Final[dict[str, dict[str, Any]]] = {
    "postgres": {
        "schema": "derived",
        "transactions": True,
        # A primary key: a second save under one key raises.
        "key": "enforced",
        "write_generations": True,
        "write_unit": "row",
        "reads": dict(_ALL_READS),
    },
    "clickhouse": {
        "schema": "derived",
        "transactions": False,
        # A ReplacingMergeTree read with FINAL: a second save under one key replaces the first.
        "key": "newest_wins",
        "write_generations": True,
        "write_unit": "row",
        "reads": dict(_ALL_READS),
    },
    "orderbook": {
        "schema": "fixed",
        "fixed_shape": {
            "table": ORDERBOOK_TABLE,
            "entities": 1,
            "fields": dict(ORDERBOOK_SHAPE),
            "key": list(ORDERBOOK_KEY),
            # Declared nullable by a model, these and no others: the engine stores no null anywhere
            # else, and a model that requires one of these could not write over TCP.
            "nullable": sorted(ORDERBOOK_NULLABLE),
            # Over TCP, which is how a registry's engine is reached, the server numbers every
            # update: a row is written without it and read back with the server's number. In
            # process the caller chooses it.
            "assigned_by_server": ["sequence_number"],
        },
        "transactions": False,
        # Both rows are stored, and a read that meets two rows with one key refuses.
        "key": "not_enforced",
        # No write fences: a group placed here carries no generation (map contract 6) and cannot
        # be staged, cut over or have an index built.
        "write_generations": False,
        # One side of one book at one instant, levels 0 to n-1; a single row is level 0.
        "write_unit": "level_update",
        # Its aggregates read the live book, not the history, so there is nothing to count.
        "reads": {"get": True, "scan": True, "count": False, "summarize": False},
        "scan_requires": {
            "equal": ["symbol", "exchange"],
            "ranges": ["timestamp_ns", "price"],
            "order": ["timestamp_ns", "side", "level"],
        },
    },
}


def engine_facts(dialect: str) -> dict[str, Any]:
    """The facts about one dialect, as a fresh document the caller may keep or change.

    A dialect this library has no adapter for is refused rather than answered with defaults: an
    empty answer would read as "nothing is known to be missing", which is the opposite of the truth.
    """
    if dialect not in _FACTS:
        raise ValueError(
            f"no engine facts for dialect {dialect!r}: this library has adapters for "
            f"{list(DIALECTS)}"
        )
    return {"facts_version": FACTS_VERSION, "dialect": dialect, **deepcopy(_FACTS[dialect])}

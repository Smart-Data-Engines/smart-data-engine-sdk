"""Refusing a signed map that goes backwards, and the memory that makes it possible.

An older signed map verifies perfectly - that is what a signature is - so swapping the client's map
file for a previous one loads cleanly, routes writes to the previous placement, and nothing
protests. Today that costs a stale schema. Once the migration state travels in the map, it costs
writes: a library reverted from dual-write to single-write mid-migration drops exactly the rows the
migration exists not to drop.

The tests below are mostly about the *shape* of the memory rather than the comparison, because the
comparison is one `<`. The shape is where this could go wrong quietly: bookkeeping that lives in one
engine and is lost with it, an engine that cannot keep it and says nothing, or a check that runs on
a path a deployment past its first release does not take.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

import pytest

import sde


class Bookkeeping:
    """A fake engine that can keep the watermark, and counts every touch.

    Counting is the point of the fake: several of the claims here are about what did *not* happen -
    no write when the version has not moved, nothing at all for an unsigned map - and a fake that
    only records values cannot express those.
    """

    dialect = "fake"

    def __init__(self, watermark: int | None = None) -> None:
        self.watermark = watermark
        self.reads = 0
        self.writes: list[tuple[int, str]] = []

    # -- Engine
    def ensure_schema(self, layout: Any, *, keys: Mapping[str, Any]) -> None:
        return None

    def insert(self, table: str, values: Mapping[str, Any]) -> None:
        return None

    def get(self, table: str, key: Mapping[str, Any]) -> dict[str, Any] | None:
        return None

    # -- WatermarkStore
    def map_watermark(self) -> int | None:
        self.reads += 1
        return self.watermark

    def record_map_version(self, version: int, *, model_version: str) -> None:
        self.writes.append((version, model_version))
        self.watermark = version if self.watermark is None else max(self.watermark, version)


class Forgetful:
    """An engine that cannot keep bookkeeping - like ours, whose schema is fixed in its source."""

    dialect = "fake-fixed"

    def ensure_schema(self, layout: Any, *, keys: Mapping[str, Any]) -> None:
        return None

    def insert(self, table: str, values: Mapping[str, Any]) -> None:
        return None

    def get(self, table: str, key: Mapping[str, Any]) -> dict[str, Any] | None:
        return None


@pytest.fixture(autouse=True)
def _isolate() -> None:
    sde.clear_registry()


def _model() -> sde.LogicalModel:
    @sde.entity
    class Reading:
        id: uuid.UUID
        station: str

    return sde.build_model(Reading)


def _map(
    model: sde.LogicalModel, *, version: int, signed: bool, engines: tuple[str, ...] = ("a",)
) -> sde.PlacementMap:
    raw: dict[str, Any] = {
        "contract": sde.CONTRACT,
        "model_version": model.version,
        "map_version": version,
        "groups": {
            group.name: {
                "source": {
                    "id": f"{group.name}@{engines[0]}",
                    "engine": engines[0],
                    "layout": {"auto": True},
                }
            }
            for group in sde.colocation_groups(model)
        },
    }
    parsed = sde.load_map(raw, model=model)
    # `signed` is what the check keys on and signing needs a private key, which lives in the
    # control plane rather than here. Setting the flag is the honest way to test this side of it:
    # the signature has its own tests, and what this file is about is what happens after one
    # verifies.
    return replace(parsed, signed=signed)


# ── the comparison ───────────────────────────────────────────────────────────────────────────────


def test_a_signed_map_older_than_the_watermark_is_refused() -> None:
    """And the message says how to proceed deliberately, because sometimes a rollback is right.

    The escape is clearing the bookkeeping rather than a parameter. A parameter called
    `allow_rollback` gets set once during an incident and stays set.
    """
    model = _model()
    engine = Bookkeeping(watermark=7)
    with pytest.raises(sde.MapRolledBack) as raised:
        sde.Session(model, _map(model, version=3, signed=True), {"a": engine})
    message = str(raised.value)
    assert "version 3" in message
    assert "version 7 has already been applied" in message
    assert f"DELETE FROM {sde.WATERMARK_TABLE} WHERE map_version > 3" in message
    assert "not a flag" in message
    assert "let the deletion finish before restarting" in message
    assert engine.writes == []


def test_the_same_version_is_allowed_because_restarting_is_ordinary() -> None:
    model = _model()
    engine = Bookkeeping(watermark=7)
    session = sde.Session(model, _map(model, version=7, signed=True), {"a": engine})
    assert session.rollback_protection.protection == "enforced"
    assert session.rollback_protection.highest_seen == 7
    # And nothing was written: the watermark has not moved, so a row per process start would say
    # nothing the watermark does not already say.
    assert engine.writes == []


def test_a_newer_version_advances_the_watermark_once() -> None:
    model = _model()
    engine = Bookkeeping(watermark=7)
    sde.Session(model, _map(model, version=8, signed=True), {"a": engine})
    assert engine.writes == [(8, model.version)]
    assert engine.watermark == 8
    # A second session against the same map writes nothing further.
    sde.Session(model, _map(model, version=8, signed=True), {"a": engine})
    assert len(engine.writes) == 1


def test_a_first_map_establishes_the_watermark() -> None:
    """With nothing recorded, the first signed map is accepted and remembered.

    The alternative - wait for the first write to establish it - leaves the window that matters
    open: a file swapped immediately after a deployment, before any traffic, goes unnoticed.
    """
    model = _model()
    engine = Bookkeeping(watermark=None)
    session = sde.Session(model, _map(model, version=4, signed=True), {"a": engine})
    assert session.rollback_protection.highest_seen is None
    assert engine.writes == [(4, model.version)]
    with pytest.raises(sde.MapRolledBack):
        sde.Session(model, _map(model, version=3, signed=True), {"a": engine})


# ── the shape of the memory ──────────────────────────────────────────────────────────────────────


def test_every_participating_engine_is_written_and_the_highest_wins() -> None:
    """Losing an engine must not lose the protection, and a lagging one must not weaken it.

    The watermark is the maximum across engines, so a stale row somewhere can never lower the bar -
    which is also why nothing here ever updates a row.
    """
    model = _model()
    ahead = Bookkeeping(watermark=9)
    behind = Bookkeeping(watermark=2)
    engines: dict[str, Any] = {"a": ahead, "b": behind}
    with pytest.raises(sde.MapRolledBack, match="version 9 has already been applied"):
        sde.Session(model, _map(model, version=5, signed=True), engines)

    session = sde.Session(model, _map(model, version=10, signed=True), engines)
    assert session.rollback_protection.participating == ("a", "b")
    assert ahead.writes == [(10, model.version)]
    assert behind.writes == [(10, model.version)]
    assert behind.watermark == 10


def test_an_engine_that_cannot_keep_bookkeeping_is_reported_not_refused() -> None:
    """A client whose only engine has a fixed schema has no rollback protection and cannot have one.

    Refusing would make that configuration unusable; pretending would be worse. The honest maximum
    is to say so where it can be read - and `unavailable` is the middle value of the three for
    exactly this reason.
    """
    model = _model()
    session = sde.Session(model, _map(model, version=4, signed=True), {"a": Forgetful()})
    protection = session.rollback_protection
    assert protection.protection == "unavailable"
    assert protection.participating == ()
    assert protection.unable == ("a",)
    assert "can keep bookkeeping" in protection.why
    assert "Nothing is wrong with your configuration" in protection.why


def test_one_capable_engine_is_enough_and_the_others_are_named() -> None:
    model = _model()
    keeper = Bookkeeping(watermark=None)
    engines: dict[str, Any] = {"a": keeper, "b": Forgetful()}
    session = sde.Session(model, _map(model, version=4, signed=True), engines)
    protection = session.rollback_protection
    assert protection.protection == "enforced"
    assert protection.participating == ("a",)
    assert protection.unable == ("b",)
    assert keeper.writes == [(4, model.version)]


def test_an_unsigned_map_is_not_checked_and_costs_nothing() -> None:
    """The no-account mode, and it has to be free rather than merely permitted.

    An unsigned map is the client's own document; replacing it with another is that mode working as
    documented, and there is no newest version for us to be the authority on. So: no query, no
    table, no cost - asserted by a fake that counts reads.
    """
    model = _model()
    engine = Bookkeeping(watermark=7)
    session = sde.Session(model, _map(model, version=1, signed=False), {"a": engine})
    protection = session.rollback_protection
    assert protection.protection == "not_applicable"
    assert engine.reads == 0
    assert engine.writes == []
    assert "no-account mode working as documented" in protection.why


def test_the_check_state_is_readable_and_serialisable() -> None:
    """A protection whose state cannot be read is a protection taken on trust."""
    model = _model()
    session = sde.Session(model, _map(model, version=4, signed=True), {"a": Bookkeeping(3)})
    record = session.rollback_protection.as_record()
    assert record["protection"] == "enforced"
    assert record["map_version"] == 4
    assert record["highest_seen"] == 3
    assert record["participating"] == ["a"]
    assert sde.canonical_bytes(record)


# ── the reserved table name ──────────────────────────────────────────────────────────────────────


def test_a_layout_naming_the_bookkeeping_table_is_refused_when_the_map_loads() -> None:
    """A client table under that name would be read as bookkeeping and written to as bookkeeping.

    Refused at load, which for a map we issue means at issuance, since the control plane parses its
    own output with this parser. The message names the fix rather than only the problem.
    """
    model = _model()
    raw: dict[str, Any] = {
        "contract": sde.CONTRACT,
        "model_version": model.version,
        "map_version": 1,
        "groups": {
            group.name: {
                "source": {
                    "id": f"{group.name}@a",
                    "engine": "a",
                    "layout": {
                        "tables": {member: sde.WATERMARK_TABLE for member in group.members},
                        "columns": {},
                    },
                }
            }
            for group in sde.colocation_groups(model)
        },
    }
    with pytest.raises(sde.MapError) as raised:
        sde.load_map(raw, model=model)
    message = str(raised.value)
    assert sde.WATERMARK_TABLE in message
    assert "stops an older map from being loaded over a newer one" in message
    assert "Rename the table" in message


def test_the_error_is_a_map_error_a_client_can_tell_apart() -> None:
    """It says the document is authentic and out of date, not that it cannot be trusted.

    Every other refusal in that hierarchy says the map is untrustworthy. This one says it can be
    trusted and that trusting it would undo something, which is the one map refusal a client may
    reasonably want to handle.
    """
    assert issubclass(sde.MapRolledBack, sde.MapError)
    model = _model()
    with pytest.raises(sde.MapError):
        sde.Session(model, _map(model, version=1, signed=True), {"a": Bookkeeping(5)})

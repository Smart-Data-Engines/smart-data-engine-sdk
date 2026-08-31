"""Loading placement maps, and resolving operations against them.

The map decides where data is written, so almost every test here is a refusal. That asymmetry is the
design: a map that is wrong in a way we tolerate is worse than no map at all.
"""

from __future__ import annotations

import base64
import copy
import uuid
from typing import Any

import pytest

import sde
from sde.canonical import canonical_bytes
from sde.errors import MapError


@pytest.fixture(autouse=True)
def _isolate() -> None:
    sde.clear_registry()


def _model() -> sde.LogicalModel:
    @sde.entity
    class Event:
        id: uuid.UUID
        name: str

    return sde.build_model(Event)


def _two_group_model() -> sde.LogicalModel:
    """Two entities, no relation between them, so colocation puts them in two groups.

    Needed because materialisation ids are unique only *within* a group: a one-group model cannot
    express an id that exists in the map and not in the shape's own group, which is the case the
    model-dependent routing check exists for.
    """

    @sde.entity
    class Event:
        id: uuid.UUID
        name: str

    @sde.entity
    class Ledger:
        id: uuid.UUID
        note: str

    return sde.build_model(Event, Ledger)


def _two_group_map(model: sde.LogicalModel) -> dict[str, Any]:
    return {
        "contract": sde.CONTRACT,
        "model_version": model.version,
        "map_version": 1,
        "groups": {
            "Event": {
                "source": {
                    "id": "event@pg",
                    "engine": "pg-main",
                    "layout": {
                        "tables": {"Event": "event"},
                        "columns": {"Event": {"id": "uuid", "name": "text"}},
                    },
                }
            },
            "Ledger": {
                "source": {
                    "id": "ledger@pg",
                    "engine": "pg-main",
                    "layout": {
                        "tables": {"Ledger": "ledger"},
                        "columns": {"Ledger": {"id": "uuid", "note": "text"}},
                    },
                }
            },
        },
    }


def _layout() -> dict[str, Any]:
    return {"tables": {"Event": "event"}, "columns": {"Event": {"id": "uuid", "name": "text"}}}


def _map(model: sde.LogicalModel, **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "contract": sde.CONTRACT,
        "model_version": model.version,
        "map_version": 1,
        "groups": {
            "Event": {
                "source": {"id": "event@pg", "engine": "pg-main", "layout": _layout()},
                "derived": [
                    {
                        "id": "event@ch",
                        "engine": "ch-1",
                        "layout": _layout(),
                        "lag_budget_ms": 30_000,
                    }
                ],
            }
        },
    }
    base.update(overrides)
    return base


def test_a_map_with_no_signature_is_valid() -> None:
    # The no-account mode. Not a loophole: hand-write a map, run with no key and no network.
    model = _model()
    placement = sde.load_map(_map(model), model=model)
    assert placement.signed is False
    assert placement.placement_of("Event").source.id == "event@pg"


def test_a_signature_with_no_key_to_check_it_is_refused() -> None:
    # The middle case, and the only one that is refused: a signature is a claim that the map came
    # from us, and an unverifiable claim is worse than no claim.
    model = _model()
    raw = _map(model, signature={"alg": "ed25519", "key_id": "k1", "value": "AAAA"})
    with pytest.raises(MapError, match="no public key was provided"):
        sde.load_map(raw, model=model)


def test_signature_round_trip() -> None:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)

    model = _model()
    raw = _map(model)
    signature = private.sign(canonical_bytes(raw))
    raw["signature"] = {
        "alg": "ed25519",
        "key_id": "k1",
        "value": base64.b64encode(signature).decode(),
    }

    placement = sde.load_map(raw, model=model, public_key=public)
    assert placement.signed is True

    # Tamper with anything at all and it stops verifying. Engine name chosen deliberately: this is
    # the field an attacker would change to redirect a client's writes.
    tampered = copy.deepcopy(raw)
    tampered["groups"]["Event"]["source"]["engine"] = "attacker-db"
    with pytest.raises(MapError, match="does not verify"):
        sde.load_map(tampered, model=model, public_key=public)


def test_a_required_signature_that_is_absent_is_refused() -> None:
    model = _model()
    with pytest.raises(MapError, match="signature was required"):
        sde.load_map(_map(model), model=model, require_signature=True)


def test_contract_mismatch_is_refused_not_guessed() -> None:
    model = _model()
    with pytest.raises(MapError, match="format contract"):
        sde.load_map(_map(model, contract=99), model=model)


def test_the_contract_mismatch_message_names_both_versions() -> None:
    """Matching on a prefix let a broken message ship, so this matches on the numbers.

    The earlier version of this message was an implicitly concatenated group where one continuation
    line had lost its ``f``, so the text a client saw was "this library implements {CONTRACT}". The
    TypeScript port of the same message interpolated correctly, which makes this a Python/TypeScript
    divergence of the class the conformance vectors cannot see: they compare encodings, not
    diagnostics. A refusal is the library's only output on this path, so the text *is* the
    behaviour.
    """
    model = _model()
    with pytest.raises(MapError) as raised:
        sde.load_map(_map(model, contract=99), model=model)

    message = str(raised.value)
    assert "99" in message, "the map's own version has to be in there"
    assert f"implements {sde.CONTRACT}" in message
    assert "{" not in message, f"an unrendered placeholder survived: {message}"


def test_model_version_mismatch_is_refused() -> None:
    model = _model()
    with pytest.raises(MapError, match="is for model version"):
        sde.load_map(_map(model, model_version="0" * 16), model=model)


def test_a_group_left_unplaced_is_refused() -> None:
    model = _model()
    raw = _map(model)
    raw["groups"] = {
        "Nonexistent": {"source": {"id": "x", "engine": "e", "layout": _layout()}}
    }
    with pytest.raises(MapError, match="does not place these groups"):
        sde.load_map(raw, model=model)


def test_a_source_cannot_have_a_lag_budget() -> None:
    model = _model()
    raw = _map(model)
    raw["groups"]["Event"]["source"]["lag_budget_ms"] = 1000
    with pytest.raises(MapError, match="cannot have a lag budget"):
        sde.load_map(raw, model=model)


def test_a_derived_copy_must_declare_its_lag_budget() -> None:
    model = _model()
    raw = _map(model)
    del raw["groups"]["Event"]["derived"][0]["lag_budget_ms"]
    with pytest.raises(MapError, match="needs lag_budget_ms"):
        sde.load_map(raw, model=model)


def test_two_materialisations_cannot_share_an_id() -> None:
    model = _model()
    raw = _map(model)
    raw["groups"]["Event"]["derived"][0]["id"] = "event@pg"
    with pytest.raises(MapError, match="share an id"):
        sde.load_map(raw, model=model)


# --- routing ----------------------------------------------------------------------------------


def _placement_and_shapes() -> tuple[sde.PlacementMap, dict[str, sde.OperationShape]]:
    model = _model()
    shapes = {s.kind: s for s in sde.enumerate_shapes(model)}
    raw = _map(model)
    raw["routing"] = {shapes["full_scan"].id: "event@ch", shapes["point_read"].id: "event@ch"}
    return sde.load_map(raw, model=model), shapes


def test_writes_always_go_to_the_source() -> None:
    placement, shapes = _placement_and_shapes()
    for kind in ("write", "bulk_write"):
        assert sde.resolve(placement, shapes[kind]).id == "event@pg"


def test_a_routed_read_goes_where_the_planner_said() -> None:
    placement, shapes = _placement_and_shapes()
    assert sde.resolve(placement, shapes["full_scan"]).id == "event@ch"


def test_a_read_inside_a_write_transaction_goes_to_the_source() -> None:
    # A derived copy is behind by design, so it cannot show the write the caller just made. This is
    # correctness, not a policy, which is why it is one of the three conditions the library applies
    # itself rather than looking up.
    placement, shapes = _placement_and_shapes()
    assert sde.resolve(placement, shapes["point_read"], in_write_transaction=True).id == "event@pg"


def test_asking_for_freshness_goes_to_the_source() -> None:
    placement, shapes = _placement_and_shapes()
    assert sde.resolve(placement, shapes["point_read"], fresh=True).id == "event@pg"


def test_an_unrouted_shape_falls_back_to_the_source() -> None:
    # What makes a hand-written map two lines rather than a table of hashes. The source is always a
    # correct answer, merely sometimes a slower one.
    placement, shapes = _placement_and_shapes()
    assert sde.resolve(placement, shapes["aggregate"]).id == "event@pg"


def test_routing_at_a_materialisation_that_does_not_exist_is_refused_at_load() -> None:
    """It used to load fine and fail at the first read that routed through the entry.

    That is the worse of the two places for it. A map is a document handed over and applied; an
    inconsistency in it that only surfaces when a particular shape is issued fails inside the
    client's request path at a time nobody can predict, and a staging run that never issues that
    operation is green. Nothing here needs runtime information, so nothing here waits for runtime.
    """
    model = _model()
    shapes = {s.kind: s for s in sde.enumerate_shapes(model)}
    raw = _map(model)
    raw["routing"] = {shapes["full_scan"].id: "event@nowhere"}
    with pytest.raises(MapError, match="no group in this map declares one with that id"):
        sde.load_map(raw, model=model)


def test_routing_across_groups_is_refused_because_ids_are_only_unique_within_one() -> None:
    """The check that needs the model, and the reason the weaker one is not enough.

    Materialisation ids are unique within a group, so an id that exists in *some* group is not
    evidence it exists in the right one. Routing a shape at another group's copy would read the
    entity out of a table that does not hold it - a wrong answer rather than an error.
    """
    model = _two_group_model()
    event_scan = next(
        s for s in sde.enumerate_shapes(model) if s.group == "Event" and s.kind == "full_scan"
    )
    raw = _two_group_map(model)
    # `ledger@pg` is declared - in the other group. The weaker check passes; this one must not.
    raw["routing"] = {event_scan.id: "ledger@pg"}
    with pytest.raises(MapError, match="which that group does not declare"):
        sde.load_map(raw, model=model)


def test_a_map_placing_a_group_the_model_does_not_have_is_refused_not_a_keyerror() -> None:
    """The other direction of the same check, and it used to be a bare ``KeyError``.

    Only the model-to-map direction was checked, so a map placing a group the model does not have
    fell through to a dict lookup on the model's groups and came out as ``KeyError: 'Other'`` - an
    exception with no explanation, raised by the function whose whole job on this path is to
    explain. One-directional checks read as complete; this is the same shape as a required status
    check nobody produces.
    """
    model = _model()
    raw = _map(model)
    raw["groups"]["Other"] = {
        "source": {"id": "other@pg", "engine": "pg-main", "layout": _layout()}
    }
    with pytest.raises(MapError, match="places groups this model does not have"):
        sde.load_map(raw, model=model)


def test_a_routing_entry_for_a_shape_this_model_does_not_produce_is_refused() -> None:
    """Same model version on both sides and a different shape enumeration is a real divergence.

    The model version covers the declaration, so if it matches and a routing key names a shape this
    library does not enumerate, the two sides disagree about what operations exist. That is the
    failure the byte contract exists for: one library's write lands where another never looks.
    """
    model = _model()
    raw = _map(model)
    raw["routing"] = {"0" * 16: raw["groups"]["Event"]["source"]["id"]}
    with pytest.raises(MapError, match="does not produce that shape"):
        sde.load_map(raw, model=model)


def test_by_id_still_guards_the_path_that_load_now_makes_unreachable() -> None:
    """Kept deliberately, and this test is the reason it stays.

    Now that ``load_map`` refuses a dangling routing entry, ``by_id`` cannot be reached with a bad
    id through the public path - which is exactly the argument somebody would use to delete it. A
    guard whose reachability depends on a check somewhere else is worth keeping and worth testing
    directly, because the day the other check moves is the day this one matters again.
    """
    placement = sde.GroupPlacement(
        group="Event",
        source=sde.Materialization(id="event@pg", engine="pg", layout=None),
    )
    with pytest.raises(MapError, match="has no materialisation"):
        placement.by_id("event@nowhere")

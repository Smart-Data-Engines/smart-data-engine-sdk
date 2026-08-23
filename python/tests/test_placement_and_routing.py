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


def test_routing_at_a_materialisation_that_does_not_exist_is_refused() -> None:
    model = _model()
    shapes = {s.kind: s for s in sde.enumerate_shapes(model)}
    raw = _map(model)
    raw["routing"] = {shapes["full_scan"].id: "event@nowhere"}
    placement = sde.load_map(raw, model=model)
    with pytest.raises(MapError, match="no materialisation"):
        sde.resolve(placement, shapes["full_scan"])

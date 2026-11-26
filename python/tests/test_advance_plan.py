"""A generation-advance authorization is a snapshot, binds the signed map in force, and its
refusals name an advance.

The shared vectors pin the packet's rules by their words (``migration/201`` and after). These are
what a vector cannot hold: what a loaded plan is as an object, and the refusals a packet signed by
the vectors' key cannot reach - the packet here is a vector's, signed again with a key made for
the test, so that a change reaches the rule it is about and not a signature that stopped verifying.
"""

from __future__ import annotations

import base64
import copy
import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

import sde
from sde.testing.loader import model_from_neutral

CASE = (
    Path(__file__).resolve().parents[2]
    / "conformance/vectors/migration/201-advance-raises-one-group-by-one-generation"
)
PROJECT = "1" * 32
MAX_EPOCH = 9_007_199_254_740_991


def fixture() -> tuple[dict[str, Any], sde.LogicalModel, dict[str, bytes], sde.AdvancePlan]:
    raw = json.loads((CASE / "plan.json").read_bytes())
    model = model_from_neutral(json.loads((CASE / "model.json").read_bytes()))
    keys = {
        name: base64.b64decode(value)
        for name, value in json.loads((CASE / "keys.json").read_bytes()).items()
    }
    plan = sde.load_advance_plan(raw, model=model, project_id=PROJECT, public_key=keys)
    return raw, model, keys, plan


class Signer:
    """Signs as the control plane signs: over the canonical bytes without the block."""

    def __init__(self) -> None:
        self._key = Ed25519PrivateKey.generate()
        self.keys = {"k": self._key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)}

    def sign(self, document: dict[str, Any]) -> None:
        document.pop("signature", None)
        value = base64.b64encode(self._key.sign(sde.canonical_bytes(document))).decode()
        document["signature"] = {"alg": "ed25519", "key_id": "k", "value": value}

    def resigned(self, packet: dict[str, Any]) -> dict[str, Any]:
        """The packet with both of its maps and its envelope signed again."""
        packet = copy.deepcopy(packet)
        for name in ("before", "after"):
            if isinstance(packet.get(name), dict):
                self.sign(packet[name])
        self.sign(packet)
        return packet


def in_both_maps(change: Callable[[dict[str, Any]], Any]) -> Callable[[dict[str, Any]], None]:
    def apply(packet: dict[str, Any]) -> None:
        for name in ("before", "after"):
            change(packet[name])

    return apply


def refusal_of(change: Callable[[dict[str, Any]], Any]) -> str:
    """What the loader says to the vector's packet after ``change``, signed again."""
    raw, model, _, _ = fixture()
    signer = Signer()
    change(raw)
    with pytest.raises(sde.MigrationRefused) as raised:
        sde.load_advance_plan(
            signer.resigned(raw), model=model, project_id=PROJECT, public_key=signer.keys
        )
    return str(raised.value)


def test_the_packet_signed_again_loads() -> None:
    """The premise of every refusal below: signing again changes nothing the loader checks."""
    raw, model, _, plan = fixture()
    signer = Signer()
    again = sde.load_advance_plan(
        signer.resigned(raw), model=model, project_id=PROJECT, public_key=signer.keys
    )
    assert again.verified_with == "k"
    assert (again.group, again.source_epoch, again.next_epoch) == (plan.group, 1, 2)
    # A map's fingerprint is of the map without its signature, so it is the vector's. The
    # packet's covers its maps as they were signed, so it is another packet's.
    assert again.before.fingerprint == plan.before.fingerprint
    assert again.after.fingerprint == plan.after.fingerprint
    assert again.fingerprint != plan.fingerprint


def test_caller_and_returned_records_cannot_replace_the_next_map() -> None:
    raw, _, _, plan = fixture()
    expected = plan.after_payload()
    raw["after"]["groups"]["Event"]["write_epoch"] = 9
    returned = plan.as_record()
    returned["after"]["groups"]["Event"]["write_epoch"] = 9
    assert plan.after_payload() == expected
    assert plan.as_record()["after"]["groups"]["Event"]["write_epoch"] == 2
    assert plan.next_epoch == 2


def test_what_is_published_is_the_signed_next_map_byte_for_byte() -> None:
    raw, model, keys, plan = fixture()
    assert plan.after_payload() == sde.canonical_bytes(raw["after"])
    published = sde.load_map(
        json.loads(plan.after_payload()), model=model, public_key=keys, require_signature=True
    )
    assert published.fingerprint == plan.after.fingerprint
    assert published.groups["Event"].write_epoch == plan.next_epoch


def test_the_generations_are_the_two_maps() -> None:
    _, _, _, plan = fixture()
    assert plan.source_epoch == plan.before.groups[plan.group].write_epoch == 1
    assert plan.next_epoch == plan.after.groups[plan.group].write_epoch == 2
    # Nothing else in the group moved, and the other group moved not at all.
    assert plan.before.groups["Event"].source == plan.after.groups["Event"].source
    assert plan.before.groups["Event"].derived == plan.after.groups["Event"].derived
    assert plan.before.groups["Order"] == plan.after.groups["Order"]


def test_reconstructed_plan_cannot_inherit_loaded_provenance() -> None:
    _, _, _, plan = fixture()
    with pytest.raises(sde.MigrationRefused, match="immutable loaded"):
        replace(plan).as_record()
    with pytest.raises(sde.MigrationRefused, match="immutable loaded"):
        replace(plan).after_payload()
    with pytest.raises(sde.MigrationRefused, match="immutable loaded"):
        _ = replace(plan).source_epoch
    with pytest.raises(sde.MigrationRefused, match="immutable loaded"):
        _ = replace(plan).next_epoch
    with pytest.raises(sde.MigrationRefused, match="immutable loaded"):
        replace(plan, group="Order").check_current(plan.before)


def test_the_advance_checks_the_actual_signed_current_map() -> None:
    raw, model, _keys, plan = fixture()
    plan.check_current(plan.before)
    with pytest.raises(sde.MigrationRefused, match="signed current map"):
        plan.check_current(plan.after)
    with pytest.raises(sde.MigrationRefused):
        plan.check_current(replace(plan.before))
    del raw["before"]["signature"]
    unsigned = sde.load_map(raw["before"], model=model)
    assert unsigned.fingerprint == plan.before.fingerprint
    with pytest.raises(sde.MigrationRefused, match="signed current map"):
        plan.check_current(unsigned)


def test_the_advance_checks_the_local_project_of_the_current_map() -> None:
    """A plan loaded for one project does not start from another project's map, the same map
    otherwise: the check is the session's own, before the fingerprints are compared."""
    raw, model, _, _ = fixture()
    signer = Signer()
    other = "9" * 32

    def of_another_project(packet: dict[str, Any]) -> dict[str, Any]:
        packet = copy.deepcopy(packet)
        packet["project_id"] = other
        for name in ("before", "after"):
            packet[name]["project_id"] = other
        return signer.resigned(packet)

    ours = sde.load_advance_plan(
        signer.resigned(raw), model=model, project_id=PROJECT, public_key=signer.keys
    )
    theirs = sde.load_advance_plan(
        of_another_project(raw), model=model, project_id=other, public_key=signer.keys
    )
    with pytest.raises(sde.MigrationRefused):
        ours.check_current(theirs.before)


def test_integral_json_numbers_preserve_packet_identity() -> None:
    raw, model, keys, plan = fixture()
    raw["protocol"], raw["pause_budget_ms"] = 1.0, float(raw["pause_budget_ms"])
    for name in ("before", "after"):
        raw[name]["contract"] = float(raw[name]["contract"])
        raw[name]["map_version"] = float(raw[name]["map_version"])
        for group in raw[name]["groups"].values():
            group["write_epoch"] = float(group["write_epoch"])
    loaded = sde.load_advance_plan(raw, model=model, project_id=PROJECT, public_key=keys)
    assert loaded.fingerprint == plan.fingerprint
    assert type(loaded.pause_budget_ms) is int
    assert type(loaded.next_epoch) is int
    assert loaded.as_record() == plan.as_record()


@pytest.mark.parametrize(
    ("change", "words"),
    [
        (lambda raw: [], "advance authorization must be an object"),
        (lambda raw: {**raw, "advance_id": "x"}, "advance advance_id must be 32 lowercase"),
        (lambda raw: {**raw, "advance_id": "A" * 32}, "advance advance_id must be 32 lowercase"),
        (lambda raw: {**raw, "advance_id": 7}, "advance advance_id must be 32 lowercase"),
        (lambda raw: {**raw, "project_id": "x"}, "advance project_id must be 32 lowercase"),
        (lambda raw: {**raw, "group": ""}, "advance group must be a nonempty string"),
        (lambda raw: {**raw, "group": 5}, "advance group must be a nonempty string"),
        (lambda raw: {**raw, "signature": "x"}, "advance signature must be an object"),
        (
            lambda raw: {**raw, "signature": {"alg": "ed25519"}},
            "advance signatures have missing or unknown fields",
        ),
        (
            lambda raw: {**raw, "signature": {**raw["signature"], "alg": "rsa"}},
            "advance signatures must use ed25519 or es256 and canonical base64",
        ),
        (
            lambda raw: {**raw, "signature": {**raw["signature"], "key_id": 5}},
            "advance signature key_id must be a string",
        ),
        (
            lambda raw: {key: value for key, value in raw.items() if key != "pause_budget_ms"},
            "advance authorization has missing or unknown fields",
        ),
    ],
)
def test_an_advance_packet_is_refused_as_an_advance(change: Any, words: str) -> None:
    """The loader shares its helpers with the cutover packet's, which name a cutover unless told
    otherwise. These are the refusals before the envelope's signature is verified, which no signed
    vector reaches."""
    raw, model, keys, _ = fixture()
    with pytest.raises(sde.MigrationRefused, match=words) as raised:
        sde.load_advance_plan(change(raw), model=model, project_id=PROJECT, public_key=keys)
    assert "cutover" not in str(raised.value)
    assert "staging" not in str(raised.value)


@pytest.mark.parametrize("budget", [0, -1, True, 1.5, "30000", None, MAX_EPOCH + 1])
def test_the_pause_budget_is_a_positive_safe_integer(budget: Any) -> None:
    raw, model, keys, _ = fixture()
    raw["pause_budget_ms"] = budget
    with pytest.raises(sde.MigrationRefused, match="advance pause_budget_ms must be a positive"):
        sde.load_advance_plan(raw, model=model, project_id=PROJECT, public_key=keys)


def test_the_longest_pause_budget_a_number_holds_is_taken() -> None:
    raw, model, _, _ = fixture()
    signer = Signer()
    raw["pause_budget_ms"] = MAX_EPOCH
    plan = sde.load_advance_plan(
        signer.resigned(raw), model=model, project_id=PROJECT, public_key=signer.keys
    )
    assert plan.pause_budget_ms == MAX_EPOCH


@pytest.mark.parametrize("protocol", [0, 2, True, "1", None])
def test_one_protocol_is_read_and_a_boolean_is_not_a_number(protocol: Any) -> None:
    raw, model, keys, _ = fixture()
    raw["protocol"] = protocol
    with pytest.raises(sde.MigrationRefused, match="unsupported advance authorization kind or"):
        sde.load_advance_plan(raw, model=model, project_id=PROJECT, public_key=keys)


def as_contract_3(document: dict[str, Any]) -> None:
    """A map rewritten to contract 3: no project and no generation. It loads as a map."""
    document["contract"] = 3
    document.pop("project_id")
    for group in document["groups"].values():
        group.pop("write_epoch")


@pytest.mark.parametrize(
    ("change", "words"),
    [
        (
            in_both_maps(as_contract_3),
            "an advance requires map contract 4 or later: it changes a write generation, and an "
            "earlier map has none",
        ),
        (lambda p: p.update(before=[]), "advance before must be an object"),
        (lambda p: p.update(after="map"), "advance after must be an object"),
        (lambda p: p["after"].update(groups=[]), "advance groups must be an object"),
        (lambda p: p["after"]["groups"].update(Event=[]), "advance group must be an object"),
        (
            in_both_maps(lambda m: m["groups"]["Event"].update(derived="Event@ch")),
            "advance derived copies must be an array",
        ),
        (
            in_both_maps(lambda m: m["groups"]["Event"].update(source=[])),
            "advance materialization must be an object",
        ),
        (
            in_both_maps(lambda m: m["groups"]["Event"]["derived"].append("Event@ch")),
            "advance materialization must be an object",
        ),
        (
            in_both_maps(lambda m: m["groups"]["Event"]["source"].update(layout=[])),
            "advance layout must be an object",
        ),
        # A layout written out with no column in it is not one anybody can fence a table by.
        (
            in_both_maps(lambda m: m["groups"]["Event"]["source"]["layout"].update(columns={})),
            "advance maps need explicit physical layouts",
        ),
        # Of a copy as of the source.
        (
            in_both_maps(lambda m: m["groups"]["Event"]["derived"][0].update(layout={"auto": 1})),
            "advance maps need explicit physical layouts",
        ),
    ],
)
def test_rules_no_vector_reaches_refuse_by_their_own_words(change: Any, words: str) -> None:
    assert refusal_of(change) == words


def test_a_map_or_encoding_refusal_inside_it_is_the_advances() -> None:
    said = refusal_of(lambda p: p["after"]["groups"]["Event"].update(write_epoch=True))
    assert said.startswith("advance authorization refused: group 'Event'")
    # A group the model has and the next map does not place: the map loader's rule, so neither map
    # of a packet can hold another set of groups than the model's.
    said = refusal_of(lambda p: p["after"]["groups"].pop("Order"))
    assert said.startswith("advance authorization refused: the map does not place these groups")
    raw, model, _, _ = fixture()
    signer = Signer()
    packet = signer.resigned(raw)
    packet["after"]["groups"]["Event"]["write_epoch"] = 2.5  # not canonical, so not signed
    with pytest.raises(sde.MigrationRefused, match="advance authorization refused: "):
        sde.load_advance_plan(packet, model=model, project_id=PROJECT, public_key=signer.keys)


def test_the_envelope_is_verified_before_the_maps_are_read() -> None:
    """An envelope nobody signed says nothing about its maps: the refusal is the signature's, and
    not a rule of a map inside a packet that may be anybody's."""
    raw, model, keys, _ = fixture()
    raw["before"] = "not a map"
    with pytest.raises(sde.MigrationRefused, match="advance authorization refused: ") as raised:
        sde.load_advance_plan(raw, model=model, project_id=PROJECT, public_key=keys)
    assert "must be an object" not in str(raised.value)


def test_a_key_the_caller_did_not_configure_verifies_nothing() -> None:
    raw, model, keys, _ = fixture()
    stranger = Signer()
    with pytest.raises(sde.MigrationRefused, match="advance authorization refused: "):
        sde.load_advance_plan(
            stranger.resigned(raw), model=model, project_id=PROJECT, public_key=keys
        )


def test_the_package_exports_the_loader() -> None:
    assert sde.ADVANCE_PROTOCOL == 1
    assert {"ADVANCE_PROTOCOL", "AdvancePlan", "load_advance_plan"} <= set(sde.__all__)

"""The packet is immutable authorization, including the signed mode of its current map."""

from __future__ import annotations

import base64
import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import sde
from sde.testing.loader import model_from_neutral

CASE = (
    Path(__file__).resolve().parents[2]
    / "conformance/vectors/migration/079-cutover-packet-authorizes-one-group"
)


def fixture() -> tuple[dict[str, Any], sde.LogicalModel, dict[str, bytes]]:
    raw = json.loads((CASE / "plan.json").read_text())
    model = model_from_neutral(json.loads((CASE / "model.json").read_text()))
    keys = {
        name: base64.b64decode(value)
        for name, value in json.loads((CASE / "keys.json").read_text()).items()
    }
    return raw, model, keys


def loaded() -> tuple[sde.CutoverPlan, dict[str, Any], sde.LogicalModel, dict[str, bytes]]:
    raw, model, keys = fixture()
    return (
        sde.load_cutover_plan(raw, model=model, project_id="1" * 32, public_key=keys),
        raw,
        model,
        keys,
    )


def test_input_and_returned_records_cannot_change_authorized_candidates() -> None:
    plan, raw, _, _ = loaded()
    expected = plan.candidate_payload("success")
    raw["success"]["groups"]["Event"]["source"]["layout"]["tables"]["Event"] = "other"
    returned = plan.as_record()
    returned["success"]["groups"]["Event"]["source"]["layout"]["tables"]["Event"] = "other"
    assert plan.candidate_payload("success") == expected
    assert plan.success.groups["Event"].source.layout.table_for("Event") == "event_copy"


def test_copied_plan_does_not_inherit_loader_provenance() -> None:
    plan, _, _, _ = loaded()
    for copied in (replace(plan), replace(plan, group="Order")):
        with pytest.raises(sde.MigrationRefused, match="immutable loaded plan"):
            copied.check_current(plan.before)
        with pytest.raises(sde.MigrationRefused, match="immutable loaded plan"):
            copied.as_record()


def test_current_map_must_match_the_before_instruction() -> None:
    plan, _, _, _ = loaded()
    plan.check_current(plan.before)
    with pytest.raises(sde.MigrationRefused, match="current placement map"):
        plan.check_current(plan.success)
    with pytest.raises(sde.MigrationRefused, match="immutable loaded placement"):
        plan.check_current(replace(plan.before))


def test_removing_current_signature_does_not_preserve_admission_mode() -> None:
    plan, raw, model, _ = loaded()
    current = deepcopy(raw["before"])
    del current["signature"]
    unsigned = sde.load_map(current, model=model)
    assert unsigned.fingerprint == plan.before.fingerprint
    with pytest.raises(sde.MigrationRefused, match="current placement map"):
        plan.check_current(unsigned)


def test_integral_json_numbers_are_checked_before_packet_signature() -> None:
    plan, raw, model, keys = loaded()
    raw["protocol"] = 1.0
    raw["pause_budget_ms"] = 5000.0
    for name in ("before", "success", "abort"):
        raw[name]["contract"] = 4.0
        raw[name]["map_version"] = float(raw[name]["map_version"])
    parsed = sde.load_cutover_plan(raw, model=model, project_id="1" * 32, public_key=keys)
    assert parsed.fingerprint == plan.fingerprint
    assert parsed.as_record() == plan.as_record()


@pytest.mark.parametrize("bad", [None, [], {"alg": "ed25519", "value": "AA=="}])
def test_bad_envelope_signature_is_a_named_refusal(bad: Any) -> None:
    raw, model, keys = fixture()
    raw["signature"] = bad
    with pytest.raises(sde.MigrationRefused, match="cutover signature"):
        sde.load_cutover_plan(raw, model=model, project_id="1" * 32, public_key=keys)


def test_noncanonical_base64_padding_is_refused_even_when_it_decodes_the_same() -> None:
    raw, model, keys = fixture()
    signature = raw["signature"]["value"]
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
    replacement = alphabet[alphabet.index(signature[85]) + 1]
    altered = signature[:85] + replacement + signature[86:]
    assert base64.b64decode(altered, validate=True) == base64.b64decode(signature, validate=True)
    raw["signature"]["value"] = altered
    with pytest.raises(sde.MigrationRefused, match="canonical base64"):
        sde.load_cutover_plan(raw, model=model, project_id="1" * 32, public_key=keys)

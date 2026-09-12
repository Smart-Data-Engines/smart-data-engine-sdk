"""Evidence must describe the session that actually reads, not merely carry plausible counts."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest

import sde
from sde.testing.loader import model_from_neutral
from sde.testing.memory import MemoryEngine, Recorded

PROJECT = "1" * 32
REQUEST = "2" * 32
AT = "2000-01-01T00:00:00Z"


def fixture() -> tuple[sde.Session, sde.VerificationRequest, Recorded, dict[str, Any]]:
    model = model_from_neutral(
        {
            "entities": [
                {
                    "name": "Event",
                    "fields": [
                        {"name": "id", "type": "int32"},
                        {"name": "value", "type": "string"},
                    ],
                    "key": ["id"],
                }
            ]
        }
    )

    def materialization(identity: str, table: str) -> dict[str, Any]:
        return {
            "id": identity,
            "engine": identity,
            "layout": {
                "tables": {"Event": table},
                "columns": {"Event": {"id": "integer", "value": "text"}},
            },
        }

    document = {
        "contract": 3,
        "model_version": model.version,
        "map_version": 1,
        "groups": {
            "Event": {
                "source": materialization("source", "events"),
                "derived": [{**materialization("copy", "events_copy"), "lag_budget_ms": 1000}],
                "also_write": ["copy"],
            }
        },
    }
    placement = sde.load_map(document, model=model)
    journal = Recorded()
    source = MemoryEngine(
        name="source",
        journal=journal,
        tables={"events": [{"id": 729381, "value": "private-row-value"}]},
    )
    target = MemoryEngine(
        name="copy",
        journal=journal,
        tables={"events_copy": [{"id": 729381, "value": "different-private-value"}]},
    )
    session = sde.Session(model, placement, {"source": source, "copy": target}, project_id=PROJECT)
    request = sde.verification_request(
        placement, group="Event", project_id=PROJECT, request_id=REQUEST, requested_at=AT
    )
    return session, request, journal, document


def test_bound_record_carries_the_request_and_no_row_values() -> None:
    session, request, _, _ = fixture()
    result = sde.verify(session, "Event", request=request)
    assert not result.matched
    assert result.request == request
    assert result.as_record()["request"] == request.as_record()
    assert result.differences[0].key == {"id": 729381}
    encoded = json.dumps(result.as_record())
    assert "private-row-value" not in encoded
    assert "different-private-value" not in encoded
    assert '"key"' not in encoded
    assert sde.VerificationRequest.from_record(request.as_record()) == request


@pytest.mark.parametrize(
    "changed",
    [
        {"project_id": "3" * 32},
        {"model_version": "0" * 16},
        {"map_version": 2},
        {"map_fingerprint": "0" * 64},
        {"group": "Other"},
        {"source_engine": "other"},
        {"source_id": "other-id"},
        {"targets": (("other", "other-id"),)},
        {"requires_signature": True},
    ],
)
def test_wrong_binding_refuses_before_the_first_comparison_call(changed: dict[str, Any]) -> None:
    session, request, journal, _ = fixture()
    request = replace(request, **changed)
    assert journal.as_list() == []
    with pytest.raises(sde.MigrationRefused):
        sde.verify(session, "Event", request=request)
    assert journal.as_list() == []


def test_project_identity_cannot_be_learned_from_the_request() -> None:
    session, request, journal, _ = fixture()
    unconfigured = sde.Session(session.model, session.placement, session.engines)
    with pytest.raises(sde.MigrationRefused, match="no project_id"):
        sde.verify(unconfigured, "Event", request=request)
    assert journal.as_list() == []


@pytest.mark.parametrize(
    "key,value",
    [
        ("protocol", True),
        ("protocol", 2),
        ("map_version", True),
        ("map_version", 2**53),
        ("project_id", ""),
        ("map_fingerprint", "invalid"),
        ("requires_signature", 1),
        ("targets", [{"engine": "copy", "id": []}]),
        ("source", {"engine": "source"}),
        ("requested_at", "2026-02-29T00:00:00Z"),
        ("requested_at", "2026-01-01T00:00:00"),
    ],
)
def test_malformed_request_has_a_named_refusal(key: str, value: Any) -> None:
    _, request, _, _ = fixture()
    body = request.as_record()
    body[key] = value
    with pytest.raises(sde.MigrationRefused):
        sde.VerificationRequest.from_record(body)


def test_unknown_fields_cannot_carry_client_rows() -> None:
    _, request, _, _ = fixture()
    with pytest.raises(sde.MigrationRefused, match="unknown fields"):
        sde.VerificationRequest.from_record({**request.as_record(), "rows": [{"id": 729381}]})


def test_same_number_different_layout_is_a_different_map() -> None:
    session, request, _, document = fixture()
    changed = json.loads(json.dumps(document))
    changed["groups"]["Event"]["source"]["layout"]["tables"]["Event"] = "other_events"
    newer = sde.load_map(changed, model=session.model)
    assert newer.map_version == session.placement.map_version
    assert newer.fingerprint != session.placement.fingerprint
    with pytest.raises(sde.MigrationRefused, match="does not match"):
        request.check_session(newer, project_id=PROJECT, group="Event")


def test_legacy_unsigned_annotations_do_not_change_the_old_load_behavior() -> None:
    session, _, _, document = fixture()
    annotated = sde.load_map({**document, "annotation": 1.25}, model=session.model)
    assert annotated.fingerprint is None
    with pytest.raises(sde.MigrationRefused, match="canonically encodable"):
        sde.verification_request(
            annotated, group="Event", project_id=PROJECT, request_id=REQUEST, requested_at=AT
        )


def test_a_result_cannot_predate_its_request() -> None:
    _, request, _, _ = fixture()
    with pytest.raises(sde.MigrationRefused, match="predates"):
        request.check_time("1999-12-31T23:59:59.999999Z")
    request.check_time(AT)


def test_loaded_layout_cannot_change_after_its_fingerprint_is_verified() -> None:
    session, request, _, document = fixture()
    with pytest.raises(TypeError, match="immutable"):
        session.placement.groups["Event"].source.layout.tables["Event"] = "different_table"  # type: ignore[index]
    document["groups"]["Event"]["derived"][0]["layout"]["columns"]["Event"]["value"] = "integer"
    assert session.placement.groups["Event"].derived[0].layout.columns["Event"]["value"] == "text"
    request.check_session(session.placement, project_id=PROJECT, group="Event")
    copied = replace(session.placement, routing={})
    assert copied.fingerprint is None
    with pytest.raises(sde.MigrationRefused, match="canonically encodable"):
        request.check_session(copied, project_id=PROJECT, group="Event")

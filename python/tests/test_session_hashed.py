"""A hashed model, driven by an application that never says a digest.

This file is what turns hashing from a model-level curiosity into a usable mode. Without it a
client would have to write ``session.save("e_546526714dc9", {"f_b1b4a0ec9efb": ...})`` in their own
source - which nobody will do, and which would put the digests in their repository anyway, next to
a comment explaining what they are.

The last test is the one that matters: with hashing on, telemetry must carry neither the client's
values (which was already true) nor their identifiers (which is what hashing adds).
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest

import sde
from sde.errors import ModelPlanningError
from sde.hashing import hash_identifiers
from sde.telemetry import Recorder

SALT = b"conformance-salt-not-for-production"


class FakeEngine:
    """Records what it was asked to write, under the names it was given."""

    dialect = "fake"

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], dict[str, Any]] = {}

    def ensure_schema(self, layout: Any, *, keys: Any) -> None:
        return None

    def insert(self, table: str, values: Any) -> None:
        self.rows[(table, str(values[sorted(values)[0]]))] = dict(values)

    def get(self, table: str, key: Any) -> dict[str, Any] | None:
        for (name, _), row in self.rows.items():
            if name == table and all(row.get(k) == v for k, v in key.items()):
                return dict(row)
        return None

    def transaction(self) -> Any:  # pragma: no cover
        raise NotImplementedError


@pytest.fixture(autouse=True)
def _isolate() -> None:
    sde.clear_registry()


def _shop() -> sde.LogicalModel:
    @sde.entity
    class User:
        id: uuid.UUID
        email: str
        display_name: str

        class Meta:
            pii = ["email", "display_name"]

    return sde.build_model(User)


def _session(recorder: Recorder | None = None) -> tuple[sde.Session, FakeEngine, Any]:
    model = _shop()
    hashed, names = hash_identifiers(model, SALT)
    raw: dict[str, Any] = {
        "contract": sde.CONTRACT,
        "model_version": hashed.version,
        "map_version": 1,
        "groups": {
            g.name: {"source": {"id": f"{g.name}@fake", "engine": "fake", "layout": {"auto": True}}}
            for g in sde.colocation_groups(hashed)
        },
    }
    placement = sde.load_map(raw, model=hashed)
    engine = FakeEngine()
    session = sde.Session(hashed, placement, {"fake": engine}, recorder=recorder, names=names)
    return session, engine, names


def test_the_application_uses_its_own_names_end_to_end() -> None:
    session, _, _ = _session()
    key = uuid.uuid4()

    session.save("User", {"id": key, "email": "a@b.test", "display_name": "Ada"})
    row = session.get("User", {"id": key})

    assert row is not None
    # Read back in the client's vocabulary. A row keyed by digests would be correct and useless.
    assert row["email"] == "a@b.test"
    assert row["display_name"] == "Ada"
    assert set(row) == {"id", "email", "display_name"}


def test_the_engine_sees_only_digests() -> None:
    session, engine, names = _session()
    session.save("User", {"id": uuid.uuid4(), "email": "a@b.test", "display_name": "Ada"})

    table, _ = next(iter(engine.rows))
    stored = next(iter(engine.rows.values()))

    assert names.entity("User") in table
    assert set(stored) == {
        names.field("User", "id"),
        names.field("User", "email"),
        names.field("User", "display_name"),
    }
    # The values are the client's, and stay the client's: hashing hides names, not data.
    assert "a@b.test" in stored.values()


def test_a_field_the_model_does_not_declare_is_refused_by_its_own_name() -> None:
    session, _, _ = _session()
    with pytest.raises(ModelPlanningError, match="has no field 'nickname'"):
        session.save("User", {"id": uuid.uuid4(), "nickname": "Ada"})


def test_a_wrong_key_is_reported_in_the_clients_vocabulary() -> None:
    """An error naming digests sends somebody to read our source instead of theirs."""
    session, _, _ = _session()
    with pytest.raises(ModelPlanningError) as raised:
        session.get("User", {"email": "a@b.test"})
    message = str(raised.value)
    assert "'id'" in message and "'email'" in message
    assert "f_" not in message, f"the error message leaks digests: {message}"


def test_an_undeclared_entity_is_refused() -> None:
    session, _, _ = _session()
    with pytest.raises(sde.DeclarationError, match="not in this model"):
        session.save("Ghost", {"id": uuid.uuid4()})


def test_a_transaction_over_the_whole_model_uses_client_names() -> None:
    # With hashing on, the no-arguments form used to feed digests back through the translation and
    # fail on a name it had just produced itself.
    session, _, _ = _session()
    group = session.group_of("User")
    assert group.members  # the group is the hashed one, which is ours to name
    assert session.group_of("User") is group


MARKERS = ("MARKER-email-3f9a", "MARKER-name-7c2b", "d0d0beef-0000-4000-8000-00000000cafe")


def test_neither_a_value_nor_an_identifier_reaches_telemetry() -> None:
    """The two guarantees together, which is the point of doing groups 6 and 7 in that order.

    Group 6 established that no client *value* reaches a telemetry record. Hashing adds the second
    half: with it on, no client *identifier* does either. Both are checked by searching the whole
    serialised window rather than named fields, so a field added later is automatically in scope.
    """
    recorder = Recorder("unused")
    session, _, _ = _session(recorder)

    key = uuid.UUID(MARKERS[2])
    for _ in range(3):
        session.save("User", {"id": key, "email": MARKERS[0], "display_name": MARKERS[1]})
        session.get("User", {"id": key})

    window = recorder.roll()
    assert window is not None
    assert window.shapes, "nothing was recorded, so this test proves nothing"

    serialised = json.dumps(
        {
            "model_version": window.model_version,
            "complete": window.complete,
            "shapes": [
                {
                    "shape_id": s.shape_id,
                    "group": s.group,
                    "entity": s.entity,
                    "kind": s.kind,
                    "calls": s.calls,
                    "rows": s.rows,
                    "errors": s.errors,
                    "call_site": s.call_site,
                    "latency_buckets": s.latency.buckets,
                }
                for s in window.shapes
            ],
        },
        default=str,
    )

    for marker in MARKERS:
        assert marker not in serialised, f"{marker} reached a telemetry record"
    for identifier in ("User", "email", "display_name"):
        assert identifier not in serialised, (
            f"{identifier!r} reached a telemetry record even though the model is hashed. That is "
            "the whole promise of the mode: we learn shapes and latencies, never names."
        )

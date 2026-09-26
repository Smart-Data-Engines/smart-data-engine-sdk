"""`Session.measure_storage` with a least-privilege runtime login, on both real engines.

The session a client's application opens measures its group's size from the engine's catalogue and
the window carries it. On ClickHouse the runtime login first lacks the ``system.parts`` grant - the
size is then an unknown with the reason ``refused``, and the application sees no exception - and
with the grant the size is what the administrator reads.
"""

from __future__ import annotations

import pytest
from test_runtime_privileges_live import PROJECT, Roles, document, roles  # noqa: F401 - fixture

import sde
from sde.engines.clickhouse import STORAGE_COLUMNS
from sde.watermark import WATERMARK_TABLE


def test_a_runtime_session_measures_its_group_and_the_window_carries_it(roles: Roles) -> None:
    model, placement = document(roles)
    sde.prepare_schema(model, placement, {"db": roles.operator}, project_id=PROJECT)
    roles.grant("events")
    roles.grant(WATERMARK_TABLE)
    recorder = sde.Recorder(model.version)
    session = sde.Session(model, placement, {"db": roles.runtime}, recorder=recorder,
                          project_id=PROJECT)
    for start in (0, 1_000):
        session.save_many("Event", [{"id": start + index} for index in range(1_000)])
    if roles.operator.dialect == "clickhouse":
        refused = session.measure_storage()
        assert refused.sizes == () and refused.unavailable == {"Event": "refused"}
        roles.command(
            f"GRANT SELECT({', '.join(STORAGE_COLUMNS)}) ON system.parts TO `{roles.username}`"
        )
    measured = session.measure_storage()
    assert measured.unavailable == {}
    (size,) = measured.sizes
    admin_total, admin_secondary = roles.operator.storage_sizes(["events"])["events"]
    assert (size.group, size.engine, size.materialization) == ("Event", "db", "source")
    assert (size.total_bytes, size.secondary_index_bytes) == (admin_total, admin_secondary)
    assert size.total_bytes > 0
    window = recorder.roll()
    assert window is not None
    body = window.as_record(model)["groups"]["Event"]
    assert body["total_bytes"] == size.total_bytes
    assert "total_bytes" not in body["missing"]
    assert body["write_burstiness"] >= 1.0
    session.close()


@pytest.mark.parametrize("dialect", ["postgres", "clickhouse"])
def test_measuring_a_closed_session_is_refused_like_any_other_call(dialect: str) -> None:
    from test_runtime_privileges_live import runtime_roles

    with runtime_roles(dialect) as held:
        model, placement = document(held)
        sde.prepare_schema(model, placement, {"db": held.operator}, project_id=PROJECT)
        held.grant("events")
        held.grant(WATERMARK_TABLE)
        session = sde.Session(model, placement, {"db": held.runtime}, project_id=PROJECT)
        session.close()
        with pytest.raises(sde.ResourceClosed):
            session.measure_storage()

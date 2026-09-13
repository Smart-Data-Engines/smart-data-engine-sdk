"""The final comparison must close the native boundary, not infer it from matching live counts."""

from __future__ import annotations

from typing import Any

import pytest
from test_generation_migration_live import engines as engines
from test_generation_migration_live import fixture

import sde

PROJECT = "1" * 32
HOLD = "6" * 32


def setup(
    engines: dict[str, Any],
) -> tuple[sde.InspectionContext, sde.Session, sde.VerificationRequest]:
    model, placement, _target = fixture("postgres", 1)
    sde.prepare_schema(model, placement, engines, project_id=PROJECT)
    session = sde.Session(model, placement, engines, project_id=PROJECT)
    session.save("Event", {"id": 1, "value": 11})
    request = sde.verification_request(
        placement,
        group="Event",
        project_id=PROJECT,
        request_id="7" * 32,
        requested_at="2026-09-12T12:00:00Z",
    )
    return sde.InspectionContext(model, placement, engines, PROJECT), session, request


def test_a_target_only_row_cannot_pass_the_frozen_gate(engines: dict[str, Any]) -> None:
    context, session, request = setup(engines)
    engines["clickhouse"].insert("copy_events", {"id": 99, "value": 99, sde.WRITE_EPOCH_COLUMN: 1})
    assert sde.verify(session, "Event").matched, "the live comparison checks source containment"
    report = sde.verify_frozen(
        context, "Event", request=request, hold_id=HOLD, epochs={"source": 1, "copy": 1}
    )
    assert not report.matched
    assert report.comparison.rows_source == 1
    assert report.comparison.rows_target == 2
    assert len(report.barriers) == 2
    record = report.as_record()
    assert record["comparison"]["request"] == request.as_record()
    assert "differences" not in record["comparison"]
    for engine, table in (("postgres", "source_events"), ("clickhouse", "copy_events")):
        assert HOLD in engines[engine].write_fence(table, project_id=PROJECT).state().holds
        with pytest.raises(sde.EngineError):
            engines[engine].insert(table, {"id": 2, "value": 22, sde.WRITE_EPOCH_COLUMN: 1})


def test_operator_can_compare_different_native_epochs_without_adopting_a_runtime_map(
    engines: dict[str, Any],
) -> None:
    context, _session, request = setup(engines)
    target = engines["clickhouse"].write_fence("copy_events", project_id=PROJECT)
    target.freeze("8" * 32)
    target.advance(2)
    target.release("8" * 32)
    engines["clickhouse"].insert("copy_events", {"id": 1, "value": 11, sde.WRITE_EPOCH_COLUMN: 2})
    with pytest.raises(sde.MigrationRefused, match="write generation"):
        sde.Session(context.model, context.placement, engines, project_id=PROJECT)
    report = sde.verify_frozen(
        context, "Event", request=request, hold_id=HOLD, epochs={"source": 1, "copy": 2}
    )
    assert report.matched
    assert {barrier.materialization: barrier.epoch for barrier in report.barriers} == {
        "source": 1,
        "copy": 2,
    }
    assert engines["postgres"].map_watermark() is None
    assert engines["clickhouse"].map_watermark() is None


def test_releasing_a_barrier_during_comparison_invalidates_the_result(
    engines: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from sde import frozen_verification as module

    context, _session, request = setup(engines)
    original = module.verify

    def interrupted(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        engines["postgres"].write_fence("source_events", project_id=PROJECT).release(HOLD)
        return result

    monkeypatch.setattr(module, "verify", interrupted)
    with pytest.raises(sde.MigrationRefused, match="lost its named barrier"):
        sde.verify_frozen(
            context, "Event", request=request, hold_id=HOLD, epochs={"source": 1, "copy": 1}
        )


def test_bad_epoch_is_refused_before_any_table_is_closed(engines: dict[str, Any]) -> None:
    context, _session, request = setup(engines)
    with pytest.raises(sde.MigrationRefused, match="expected write generation"):
        sde.verify_frozen(
            context, "Event", request=request, hold_id=HOLD, epochs={"source": 1, "copy": 2}
        )
    assert not engines["postgres"].write_fence("source_events", project_id=PROJECT).state().holds
    assert not engines["clickhouse"].write_fence("copy_events", project_id=PROJECT).state().holds


def test_equal_counts_cannot_hide_a_different_target_value(engines: dict[str, Any]) -> None:
    context, _session, request = setup(engines)
    engines["clickhouse"].insert("copy_events", {"id": 1, "value": 99, sde.WRITE_EPOCH_COLUMN: 1})
    report = sde.verify_frozen(
        context, "Event", request=request, hold_id=HOLD, epochs={"source": 1, "copy": 1}
    )
    assert report.comparison.rows_source == report.comparison.rows_target == 1
    assert not report.comparison.matched
    assert not report.matched


def test_retired_hold_is_refused_before_closing_another_table(engines: dict[str, Any]) -> None:
    context, _session, request = setup(engines)
    source = engines["postgres"].write_fence("source_events", project_id=PROJECT)
    source.freeze(HOLD)
    source.release(HOLD)
    with pytest.raises(sde.MigrationRefused, match="retired barrier id"):
        sde.verify_frozen(
            context, "Event", request=request, hold_id=HOLD, epochs={"source": 1, "copy": 1}
        )
    assert not source.state().holds
    assert not engines["clickhouse"].write_fence("copy_events", project_id=PROJECT).state().holds


def test_a_changed_table_identity_invalidates_a_matching_comparison(
    engines: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    from sde import frozen_verification as module

    context, _session, request = setup(engines)
    actual_verify = module.verify
    actual_state = sde.WriteFence.state
    compared = False

    def compare(*args: Any, **kwargs: Any) -> Any:
        nonlocal compared
        report = actual_verify(*args, **kwargs)
        compared = True
        return report

    def read_state(fence: sde.WriteFence) -> sde.FenceState:
        state = actual_state(fence)
        return (
            replace(state, identity="replacement-table")
            if compared and fence.table == "source_events"
            else state
        )

    monkeypatch.setattr(module, "verify", compare)
    monkeypatch.setattr(sde.WriteFence, "state", read_state)
    with pytest.raises(sde.MigrationRefused, match="changed identity"):
        sde.verify_frozen(
            context, "Event", request=request, hold_id=HOLD, epochs={"source": 1, "copy": 1}
        )

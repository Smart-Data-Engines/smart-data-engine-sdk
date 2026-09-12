"""Native generation bounds must remain conservative at every interrupted DDL boundary."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from _write_fence import MemoryFences

import sde
from sde.write_fence import EPOCH_COLUMN, FENCE_PREFIX, WriteFence, fence_state

PROJECT = "1" * 32
HOLD = "2" * 32


def fixture() -> tuple[MemoryFences, WriteFence]:
    backend = MemoryFences()
    return backend, WriteFence(backend, "events", project_id=PROJECT)


def test_preparation_and_generation_change_have_no_unbounded_release() -> None:
    backend, fence = fixture()
    assert fence.prepare(1).epoch == 1
    assert not backend.accepts(0)
    assert backend.accepts(1)
    assert not backend.accepts(2)
    assert fence.freeze(HOLD).closed
    assert not backend.accepts(1)
    assert fence.advance(2).closed
    assert fence.release(HOLD).epoch == 2
    assert not backend.accepts(1)
    assert backend.accepts(2)
    assert not backend.accepts(3)
    fence.freeze("3" * 32)
    with pytest.raises(sde.MigrationRefused, match="backwards"):
        fence.advance(1)


def test_a_retry_drains_again_and_releasing_one_hold_keeps_the_other() -> None:
    backend, fence = fixture()
    fence.prepare(1)
    fence.freeze(HOLD)
    before = sum(call[0] == "drain" for call in backend.calls)
    fence.freeze(HOLD)
    assert sum(call[0] == "drain" for call in backend.calls) == before + 1
    other = "3" * 32
    fence.freeze(other)
    assert fence.release(HOLD).holds == (other,)
    assert not backend.accepts(1)
    assert not fence.release(other).closed
    assert not fence.release(other).closed


@pytest.mark.parametrize("boundary", range(1, 8))
def test_preparation_resumes_after_every_side_effect(boundary: int) -> None:
    backend, fence = fixture()
    backend.fail_after = boundary
    with pytest.raises(OSError):
        fence.prepare(1)
    backend.fail_after = None
    assert fence.prepare(1).epoch == 1
    assert backend.accepts(1)
    assert not backend.accepts(0)
    assert not backend.accepts(2)


@pytest.mark.parametrize("boundary", range(1, 5))
def test_epoch_change_stays_closed_after_every_interruption(boundary: int) -> None:
    backend, fence = fixture()
    fence.prepare(1)
    fence.freeze(HOLD)
    backend.fail_after = len(backend.calls) + boundary
    with pytest.raises(OSError):
        fence.advance(2)
    assert not backend.accepts(1)
    assert not backend.accepts(2)
    backend.fail_after = None
    assert fence.advance(2).closed
    fence.release(HOLD)
    assert backend.accepts(2)
    assert not backend.accepts(1)


@pytest.mark.parametrize("epoch", [0, -1, True, 1.5, 2**53])
def test_bad_epoch_causes_no_metadata_change(epoch: Any) -> None:
    backend, fence = fixture()
    with pytest.raises(sde.MigrationRefused, match="safe integer"):
        fence.prepare(epoch)
    assert not backend.calls


def test_wrong_project_and_unowned_reserved_column_are_refused() -> None:
    backend, fence = fixture()
    fence.prepare(1)
    before = dict(backend.constraints)
    with pytest.raises(sde.MigrationRefused, match="another project"):
        WriteFence(backend, "events", project_id="4" * 32).prepare(1)
    assert backend.constraints == before
    backend, fence = fixture()
    backend.column = "valid"
    with pytest.raises(sde.MigrationRefused, match="not owned"):
        fence.prepare(1)
    assert not backend.calls


def test_a_future_generation_is_not_an_idempotent_prepare() -> None:
    backend, fence = fixture()
    fence.prepare(1)
    count = len(backend.calls)
    with pytest.raises(sde.MigrationRefused, match="cannot change"):
        fence.prepare(2)
    with pytest.raises(sde.MigrationRefused, match="named write barrier"):
        fence.advance(2)
    assert len(backend.calls) == count


@pytest.mark.parametrize(
    "predicate",
    [
        f"CHECK (({EPOCH_COLUMN} <= 1)) NOT VALID",
        f"CHECK (({EPOCH_COLUMN} >= 0)) NOT VALID",
        'CHECK (("__sde_write_ epoch" >= 1)) NOT VALID',
        'CHECK (("other" >= 1)) NOT VALID',
        f"CHECK (({EPOCH_COLUMN} >= 1) OR true) NOT VALID",
        "CHECK (true) NOT VALID",
    ],
)
def test_a_constraint_name_does_not_vouch_for_its_predicate(predicate: str) -> None:
    backend, fence = fixture()
    fence.prepare(1)
    backend.constraints[FENCE_PREFIX + "min_1"] = predicate
    with pytest.raises(sde.MigrationRefused, match="unexpected predicate"):
        fence.state()


def test_large_epochs_and_native_predicate_spellings_agree() -> None:
    backend, fence = fixture()
    epoch = 2**53 - 1
    fence.prepare(epoch)
    backend.constraints[FENCE_PREFIX + "min_" + str(epoch)] = (
        f"CHECK ((\"{EPOCH_COLUMN}\" >= '{epoch}'::bigint)) NOT VALID"
    )
    assert fence.state().epoch == epoch
    metadata = backend.metadata("events")
    assert (
        fence_state(
            replace(metadata, constraints={**metadata.constraints, "unrelated": "false"})
        ).epoch
        == epoch
    )


def test_integral_json_numbers_use_the_same_constraint_names() -> None:
    backend, fence = fixture()
    assert fence.prepare(1.0).epoch == 1  # type: ignore[arg-type]
    assert FENCE_PREFIX + "min_1" in backend.constraints
    assert FENCE_PREFIX + "max_1" in backend.constraints


def test_a_completed_barrier_id_cannot_be_reopened_or_reused() -> None:
    backend, fence = fixture()
    fence.prepare(1)
    fence.freeze(HOLD)
    fence.advance(2)
    fence.release(HOLD)
    before = list(backend.calls)
    with pytest.raises(sde.MigrationRefused, match="cannot be reused"):
        fence.freeze(HOLD)
    assert backend.calls == before
    assert backend.accepts(2)

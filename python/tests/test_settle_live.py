"""A settle before a cutover, and a cutover whose pause reads only the rows since the advance.

Against PostgreSQL and ClickHouse, each as the source and the other as the copy. The order the
operator documents: a copy filled, a generation advance, a settle while the application writes,
and the cutover. What is held here:

- the cutover after a settle succeeds, its copy is the source's equal value for value, and its
  pause read no range of either side: it compared the rows that carry the generation, and those
  only;
- a row from before the advance that the copy lacks or holds otherwise is the settle's to write,
  and one written since is the pause's, whichever way the application wrote it - a new key, or on
  ClickHouse a new version of one - and whether its fan-out arrived or not;
- the settle runs beside an application that keeps writing, and what it settled still holds at the
  decision;
- what voids a settle - another advance, an erasure, a table replaced - sends the cutover back to
  the comparison of the whole group, and nothing else does;
- the record of a settle, its state and its command.
"""

from __future__ import annotations

import json
import threading
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from test_advance_operator_live import PROJECT, TABLE, Group, crash_at, group, quiet

import sde
from sde.erasure import ErasureRequest
from sde.local_cutover import LocalCutover


def lose_the_fanout_of(made: Group, session: Any, key: int, value: int) -> None:
    """A write the source takes and the copy does not: the fan-out is the library's own, and
    its failure is counted, never the caller's."""
    runtime = made.roles[made.target].runtime
    actual = runtime.insert

    def failed_copy(_table: str, _values: Any) -> None:
        raise sde.EngineError("controlled missing copy")

    runtime.insert = failed_copy
    try:
        session.save("Event", {"id": key, "value": value})
    finally:
        runtime.insert = actual


def cutover_of(made: Group, *, budget_ms: int = 30000) -> Any:
    """A signed cutover from the map in force to its copy: before, success and abort, as a
    control plane issues it after the advance."""
    before = made.state()["active_map"]
    epoch = before["groups"]["Event"]["write_epoch"]
    copy = deepcopy(before["groups"]["Event"]["derived"][0])
    del copy["lag_budget_ms"]
    success, abort = deepcopy(before), deepcopy(before)
    success["map_version"] = before["map_version"] + 1
    abort["map_version"] = before["map_version"] + 2
    success["groups"]["Event"] = {"write_epoch": epoch + 2, "source": copy}
    abort["groups"]["Event"] = {
        "write_epoch": epoch + 1,
        "source": deepcopy(before["groups"]["Event"]["source"]),
    }
    parsed = sde.load_map(before, model=made.model, public_key=made.public)
    request = sde.verification_request(
        parsed,
        group="Event",
        project_id=PROJECT,
        request_id=uuid4().hex,
        requested_at=datetime.now(UTC).isoformat(),
    )
    packet = made.signed(
        {
            "kind": "sde-cutover",
            "protocol": 1,
            "plan_id": uuid4().hex,
            "project_id": PROJECT,
            "group": "Event",
            "pause_budget_ms": budget_ms,
            "query_impact_digest": "a" * 64,
            "before": before,
            "success": made.signed(success),
            "abort": made.signed(abort),
            "verification": request.as_record(),
        }
    )
    return sde.load_cutover_plan(
        packet, model=made.model, project_id=PROJECT, public_key=made.public
    )


def advance_again(made: Group) -> None:
    """The next advance, from the map in force: as the first, one generation up."""
    before = made.state()["active_map"]
    after = deepcopy(before)
    after["map_version"] += 1
    after["groups"]["Event"]["write_epoch"] += 1
    packet = made.signed(
        {
            "kind": "sde-advance",
            "protocol": 1,
            "advance_id": uuid4().hex,
            "project_id": PROJECT,
            "group": "Event",
            "pause_budget_ms": 30000,
            "before": before,
            "after": made.signed(after),
        }
    )
    plan = sde.load_advance_plan(
        packet, model=made.model, project_id=PROJECT, public_key=made.public
    )
    assert made.operator.advance(plan).as_record()["outcome"] == "advanced"


def repair_of(made: Group, plan: Any) -> dict[str, Any]:
    """Which way the completed cutover repaired its copy: local, numbers only."""
    record: dict[str, Any] = made.state()["completed"][plan.plan_id]["repair"]
    return record


def values(made: Group, role: str) -> dict[int, int]:
    return {key: value for key, (value, _) in made.stamps(role).items()}


class Reads:
    """Every range and stream an engine is asked for while it is watched."""

    def __init__(self, made: Group) -> None:
        self.seen: list[str] = []
        self._made = made
        self._kept: list[tuple[Any, str, Any]] = []

    def __enter__(self) -> Reads:
        for name, role in self._made.roles.items():
            for method in ("key_range", "key_stream", "earlier_range"):
                original = getattr(role.operator, method, None)
                if original is None:
                    continue
                self._kept.append((role.operator, method, original))

                def watched(*args: Any, _n: str = f"{name}.{method}", _f: Any = original,
                            **kwargs: Any) -> Any:
                    self.seen.append(_n)
                    return _f(*args, **kwargs)

                setattr(role.operator, method, watched)
        return self

    def __exit__(self, *_: Any) -> None:
        for engine, method, original in self._kept:
            setattr(engine, method, original)


def settled_then_written(made: Group) -> None:
    """Rows on both sides of an advance, with what each side can get wrong.

    Before it: a write whose fan-out was lost, a copy row holding another value, and rows whose
    fan-out arrived. After it: a write whose fan-out was lost, and on a ClickHouse source a row
    written again with another value - a new version of its key.
    """
    lose_the_fanout_of(made, made.old, 3, 33)
    made.old.save("Event", {"id": 5, "value": 55})
    made.old.save("Event", {"id": 6, "value": 66})
    made.roles[made.target].operator.upsert_in(
        TABLE["copy"], [{"id": 2, "value": 999, sde.WRITE_EPOCH_COLUMN: 1}], key=["id"]
    )
    made.operator.advance(made.plan)
    session = made.session()
    lose_the_fanout_of(made, session, 4, 44)
    if made.source == "clickhouse":
        session.save("Event", {"id": 1, "value": 111})


EXPECTED = {
    "postgres": {1: 11, 2: 22, 3: 33, 4: 44, 5: 55, 6: 66},
    "clickhouse": {1: 111, 2: 22, 3: 33, 4: 44, 5: 55, 6: 66},
}


@pytest.mark.parametrize("source", ["postgres", "clickhouse"])
def test_a_cutover_after_a_settle_compares_only_the_rows_written_since_the_advance(
    source: str, tmp_path: Path
) -> None:
    with group(source, tmp_path) as made:
        settled_then_written(made)
        (record,) = made.operator.settle()
        # Rows below the map's generation: 2, 3, 5, 6 and on PostgreSQL 1. Two of them written:
        # 2 over another value, 3 where the copy had none.
        earlier = 4 if source == "clickhouse" else 5
        assert (record["group"], record["settled"]) == ("Event", True)
        assert (record["write_epoch"], record["map_version"]) == (2, 2)
        assert (record["earlier"], record["written"], record["unconfirmed"]) == (earlier, 2, 0)
        assert [(row["role"], row["engine"]) for row in record["tables"]] == [
            ("source", made.source),
            ("target", made.target),
        ]
        assert made.state()["settled"]["Event"]["map_fingerprint"] == made.plan.after.fingerprint
        assert json.loads(made.operator.store.path.read_bytes())["storage_contract"] == 7
        plan = cutover_of(made)
        with Reads(made) as reads:
            receipt = made.operator.execute(plan).as_record()
        assert receipt["outcome"] == "success" and receipt["verification"]["matched"] is True
        # No range of either side and no stream: the rows that carry the generation, by it.
        assert reads.seen == []
        repair = repair_of(made, plan)
        # Row 4, whose fan-out was lost; on ClickHouse also row 1, written again with another
        # value, whose fan-out PostgreSQL refused as a key it holds.
        assert repair["mode"] == "since"
        assert repair["written"] == (2 if source == "clickhouse" else 1)
        assert values(made, "copy") == values(made, "source") == EXPECTED[source]
        # Settled under the map this cutover replaced: void, and kept no longer.
        assert "Event" not in made.state()["settled"]


@pytest.mark.parametrize("source", ["postgres", "clickhouse"])
def test_a_settle_beside_an_application_that_keeps_writing_still_holds_at_the_decision(
    source: str, tmp_path: Path
) -> None:
    """A thousand rows from before the advance, a tenth of them missing from the copy and a tenth
    holding another value there; a writer adding rows - and on ClickHouse writing over old ones -
    for as long as the settle runs, in chunks of fifty."""
    with group(source, tmp_path) as made:
        made.old.save_many("Event", [{"id": key, "value": key} for key in range(10, 1010)])
        copy = made.roles[made.target].operator
        copy.upsert_in(
            TABLE["copy"],
            [{"id": key, "value": -key, sde.WRITE_EPOCH_COLUMN: 1} for key in range(10, 1010, 10)],
            key=["id"],
        )
        if made.target == "postgres":
            copy._cx.execute(f'DELETE FROM "{TABLE["copy"]}" WHERE id % 10 = 5')
        else:
            copy._cx.command(f"DELETE FROM `{TABLE['copy']}` WHERE id % 10 = 5")
        made.operator.advance(made.plan)
        stop = threading.Event()
        written: list[int] = []

        def writer() -> None:
            # Connections of its own: an adapter serves one thread at a time.
            from test_index_operator_live import fresh_runtime

            runtime = fresh_runtime(made.roles)
            session = sde.Session(
                made.model, made.operator.active_map(), runtime, project_id=PROJECT
            )
            key = 2000
            while not stop.is_set():
                session.save("Event", {"id": key, "value": key})
                written.append(key)
                if made.source == "clickhouse":
                    old = 10 + (key * 7) % 1000
                    session.save("Event", {"id": old, "value": old + 1})
                key += 1

        settling = LocalCutover(
            made.root,
            model=made.model,
            project_id=PROJECT,
            public_key=made.public,
            operators={name: role.operator for name, role in made.roles.items()},
            runtime={name: [role.runtime] for name, role in made.roles.items()},
            chunk_rows=50,
        )
        thread = threading.Thread(target=writer)
        thread.start()
        try:
            (record,) = settling.settle()
        finally:
            stop.set()
            thread.join(timeout=60)
        assert record["settled"] and written, (record, len(written))
        receipt = made.operator.execute(cutover_of(made)).as_record()
        assert receipt["outcome"] == "success" and receipt["verification"]["matched"] is True
        assert values(made, "copy") == values(made, "source")
        assert set(range(10, 1010)) | set(written) <= set(values(made, "copy"))


def test_another_advance_after_the_settle_voids_it(tmp_path: Path) -> None:
    """The rows written between the two advances carry the first one's generation, which the
    second cutover's pause would not read: a settle is for the map it was made under."""
    with group("postgres", tmp_path) as made:
        settled_then_written(made)
        made.operator.settle()
        advance_again(made)
        lose_the_fanout_of(made, made.session(), 7, 77)
        plan = cutover_of(made)
        receipt = made.operator.execute(plan).as_record()
        assert receipt["outcome"] == "success"
        assert repair_of(made, plan)["mode"] == "diff"
        assert values(made, "copy") == values(made, "source")


def test_an_erasure_voids_the_settle(tmp_path: Path) -> None:
    with group("postgres", tmp_path) as made:
        settled_then_written(made)
        made.operator.settle()
        assert made.state()["settled"]
        request = ErasureRequest.build(made.model, "Event", [{"id": 5}])
        assert made.operator.erase(request, execute=True).as_record()["outcome"] == "erased"
        assert made.state()["settled"] == {}
        plan = cutover_of(made)
        assert made.operator.execute(plan).as_record()["outcome"] == "success"
        assert repair_of(made, plan)["mode"] == "diff"
        assert 5 not in values(made, "copy") and 5 not in values(made, "source")


@pytest.mark.parametrize(
    "tamper",
    [
        lambda record: record["tables"][0]["identity"].update(object="0"),
        lambda record: record["tables"][1]["identity"].update(object="0"),
        lambda record: record.update(map_fingerprint="0" * 64),
        lambda record: record.update(write_epoch=1),
    ],
    ids=["the source replaced", "the copy replaced", "another map", "another generation"],
)
def test_a_record_that_is_not_of_this_cutover_is_not_stood_on(
    tamper: Any, tmp_path: Path
) -> None:
    """Each thing the record binds, alone: the tables by their native identities, the map it was
    made under, and the generation. A cutover that finds another does not stand on it."""
    with group("postgres", tmp_path) as made:
        settled_then_written(made)
        made.operator.settle()
        with made.operator.store.lock():
            state = made.operator.store.read()
            tamper(state["settled"]["Event"])
            made.operator.store.write(state)
        plan = cutover_of(made)
        assert made.operator.execute(plan).as_record()["outcome"] == "success"
        assert repair_of(made, plan)["mode"] == "diff"
        assert values(made, "copy") == values(made, "source")


@pytest.mark.parametrize("source", ["postgres", "clickhouse"])
def test_a_key_the_copy_holds_since_and_the_source_lacks_is_refilled_away(
    source: str, tmp_path: Path
) -> None:
    """Only emptying removes it, so the pause refills the copy whole and compares it whole."""
    with group(source, tmp_path) as made:
        settled_then_written(made)
        made.operator.settle()
        made.roles[made.target].operator.upsert_in(
            TABLE["copy"], [{"id": 99, "value": 9, sde.WRITE_EPOCH_COLUMN: 2}], key=["id"]
        )
        plan = cutover_of(made)
        receipt = made.operator.execute(plan).as_record()
        assert receipt["outcome"] == "success" and receipt["verification"]["matched"] is True
        repair = repair_of(made, plan)
        assert repair["mode"] == "refill" and repair["after"]["surplus"] is True
        assert values(made, "copy") == values(made, "source") == EXPECTED[source]


def test_a_settle_that_could_not_read_back_a_write_keeps_no_record(tmp_path: Path) -> None:
    with group("clickhouse", tmp_path) as made:
        settled_then_written(made)
        made.operator.settle()
        assert "Event" in made.state()["settled"]
        copy = made.roles[made.target].operator
        # A row from before the advance gone from the copy, and a copy that answers a write and
        # keeps nothing: the settle copies the row, reads it back, and does not find it.
        copy._cx.execute(f'DELETE FROM "{TABLE["copy"]}" WHERE id = 6')
        copy.copy_in = lambda *args, **kwargs: None
        (record,) = made.operator.settle()
        assert record["settled"] is False and record["unconfirmed"] == 1
        assert "Event" not in made.state()["settled"]


def test_a_settle_waits_for_no_unfinished_operation(tmp_path: Path) -> None:
    with group("postgres", tmp_path) as made:
        made.operator._after_step = crash_at("advance_freeze_copy:done")
        with pytest.raises(BaseException, match="advance_freeze_copy:done"):
            made.operator.advance(made.plan)
        made.operator._after_step = quiet
        with pytest.raises(sde.MigrationRefused, match="before a settle"):
            made.operator.settle()
        made.operator.resume()


def test_a_settle_needs_a_copy(tmp_path: Path) -> None:
    with (
        group("postgres", tmp_path, copied=False) as made,
        pytest.raises(sde.MigrationRefused, match="names no copy to settle"),
    ):
        made.operator.settle()


def test_a_cutover_whose_pause_ran_out_after_a_settle_aborts_and_keeps_nothing_settled(
    tmp_path: Path,
) -> None:
    """The settle changes what the pause reads, not what its budget means."""
    with group("postgres", tmp_path) as made:
        settled_then_written(made)
        made.operator.settle()
        plan = cutover_of(made)

        def spend(step: str) -> None:
            if step == "repair:done":
                made.operator._started -= (plan.pause_budget_ms + 1) * 1_000_000

        made.operator._after_step = spend
        receipt = made.operator.execute(plan).as_record()
        assert (receipt["outcome"], receipt["reason"]) == ("abort", "budget_exceeded")
        assert repair_of(made, plan)["mode"] == "since"
        assert "Event" not in made.state()["settled"]


def test_the_operator_cli_settles_and_says_what_it_did(tmp_path: Path) -> None:
    from test_advance_operator_live import operator_cli, run_cli

    with group("clickhouse", tmp_path / "state") as made:
        settled_then_written(made)
        base, environment = operator_cli(made, tmp_path)
        code, records = run_cli([*base, "settle"], environment)
        assert code == 0
        (record,) = records
        assert (record["group"], record["settled"], record["written"]) == ("Event", True, 2)
        assert made.state()["settled"]["Event"]["map_version"] == 2
        stored = made.operator.store.path.read_text() + json.dumps(records)
        for role in made.roles.values():
            assert role.runtime._dsn not in stored

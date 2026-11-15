"""A generation advance raises one number on a group's source and its copies under a barrier.

What is held here, against PostgreSQL and ClickHouse in both roles: the two states an advance can
leave and no third - the map in force with every table open at its generation, or the next map
with every table open at the next one; that it reaches one of them from every checkpoint, after a
crash, a kill, a deadline and an exhausted budget; that a row written before it keeps the
generation it was written in, which is the whole use of it; and the order the barrier closes and
opens in.
"""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from test_runtime_privileges_live import Roles, runtime_roles

import sde
from sde.local_cutover import CutoverRecoveryRequired, LocalCutover, load_local_map
from sde.placement import WATERMARK_TABLE

PROJECT = "1" * 32
TABLE = {"source": "source_events", "copy": "copy_events"}

BEFORE_DECISION = [
    "advance_prepared",
    "advance_freeze_source:intent",
    "advance_freeze_source:done",
    "advance_freeze_copy:intent",
    "advance_freeze_copy:done",
    "advance_held:intent",
    "advance_held:done",
]
AFTER_DECISION = [
    "advance_decision:advanced",
    *(
        f"advance_{step}:{part}"
        for step in ("source", "copy", "watermarks", "publish", "release_copy", "release_source")
        for part in ("intent", "done")
    ),
]


@dataclass
class Group:
    operator: LocalCutover
    plan: Any
    old: Any
    roles: dict[str, Roles]
    source: str
    target: str
    copied: bool
    model: Any
    public: bytes
    signed: Callable[[dict[str, Any]], dict[str, Any]]
    root: Path

    @property
    def tables(self) -> list[tuple[str, str]]:
        """The group's tables as (engine, table), the source's first."""
        found = [(self.source, TABLE["source"])]
        if self.copied:
            found.append((self.target, TABLE["copy"]))
        return found

    def session(self) -> Any:
        """A process that loads the active map now, on the fixture's runtime logins."""
        runtime = {name: role.runtime for name, role in self.roles.items()}
        return sde.Session(self.model, self.operator.active_map(), runtime, project_id=PROJECT)

    def fence(self, role: str) -> Any:
        engine = self.source if role == "source" else self.target
        return self.roles[engine].operator.write_fence(TABLE[role], project_id=PROJECT).state()

    def stamps(self, role: str) -> dict[int, tuple[int, int]]:
        """A table as it is stored: each key's value and the generation that wrote it."""
        engine = self.source if role == "source" else self.target
        return {
            row["id"]: (row["value"], row[sde.WRITE_EPOCH_COLUMN])
            for row in self.roles[engine].operator.key_range(TABLE[role], ["id"])
        }

    def everywhere(self) -> list[dict[int, tuple[int, int]]]:
        roles = ("source", "copy")[: len(self.tables)]
        return [self.stamps(role) for role in roles]

    def state(self) -> dict[str, Any]:
        with self.operator.store.lock():
            return self.operator.store.read()

    def reconnect(self) -> LocalCutover:
        """Fresh dedicated connections after a deadline or a kill, and a fresh operator."""
        for name, role in self.roles.items():
            role.operator.close()
            role.operator.connect()
            if name == "postgres":
                role.operator._cx.execute('SET search_path TO "' + role.namespace + '"')
            role.runtime.close()
            role.runtime.connect()
        self.operator = LocalCutover(
            self.root,
            model=self.model,
            project_id=PROJECT,
            public_key=self.public,
            operators={name: role.operator for name, role in self.roles.items()},
            runtime={name: [role.runtime] for name, role in self.roles.items()},
        )
        return self.operator


@contextmanager
def group(
    source: str, root: Path, *, copied: bool = True, budget_ms: int = 30000
) -> Iterator[Group]:
    """A map in force with one group - a source and, unless told otherwise, a maintained copy in
    the other engine - two rows written under it, an operator, and a signed advance of the group."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    with runtime_roles("postgres") as pg, runtime_roles("clickhouse") as ch:
        roles = {"postgres": pg, "clickhouse": ch}
        operators = {name: value.operator for name, value in roles.items()}
        runtime = {name: value.runtime for name, value in roles.items()}
        target = "clickhouse" if source == "postgres" else "postgres"
        sde.clear_registry()

        @sde.entity
        class Event:
            id: int
            value: sde.Int32

        model = sde.build_model(Event)
        key = Ed25519PrivateKey.generate()
        public = key.public_key().public_bytes_raw()

        def signed(body: dict[str, Any]) -> dict[str, Any]:
            body = deepcopy(body)
            body.pop("signature", None)
            body["signature"] = {
                "alg": "ed25519",
                "value": base64.b64encode(key.sign(sde.canonical_bytes(body))).decode(),
            }
            return body

        def material(name: str, role: str) -> dict[str, Any]:
            return {
                "engine": name,
                "id": role,
                "layout": {
                    "tables": {"Event": TABLE[role]},
                    "columns": {
                        "Event": {
                            "id": "bigint" if name == "postgres" else "Int64",
                            "value": "integer" if name == "postgres" else "Int32",
                        }
                    },
                },
            }

        placed: dict[str, Any] = {"write_epoch": 1, "source": material(source, "source")}
        if copied:
            placed["derived"] = [{**material(target, "copy"), "lag_budget_ms": 60000}]
            placed["also_write"] = ["copy"]
        before = signed(
            {
                "contract": 4,
                "project_id": PROJECT,
                "model_version": model.version,
                "map_version": 1,
                "groups": {"Event": placed},
            }
        )
        after = deepcopy(before)
        after["map_version"] = 2
        after["groups"]["Event"]["write_epoch"] = 2
        packet = signed(
            {
                "kind": "sde-advance",
                "protocol": 1,
                "advance_id": uuid4().hex,
                "project_id": PROJECT,
                "group": "Event",
                "pause_budget_ms": budget_ms,
                "before": before,
                "after": signed(after),
            }
        )
        plan = sde.load_advance_plan(packet, model=model, project_id=PROJECT, public_key=public)
        parsed = sde.load_map(before, model=model, public_key=public)
        sde.prepare_schema(model, parsed, operators, project_id=PROJECT)
        roles[source].grant(TABLE["source"])
        if copied:
            roles[target].grant(TABLE["copy"])
        for value in roles.values():
            value.grant(WATERMARK_TABLE)
        old = sde.Session(model, parsed, runtime, project_id=PROJECT)
        old.save("Event", {"id": 1, "value": 11})
        old.save("Event", {"id": 2, "value": 22})
        executor = LocalCutover(
            root,
            model=model,
            project_id=PROJECT,
            public_key=public,
            operators=operators,
            runtime={name: [value] for name, value in runtime.items()},
        )
        executor.enroll(before)
        try:
            yield Group(
                executor, plan, old, roles, source, target, copied, model, public, signed, root
            )
        finally:
            # A kill can leave a permanently detached table. Restore only the test's own objects
            # before the enclosing namespace fixture removes them.
            for value in roles.values():
                if value.operator.dialect == "clickhouse":
                    with_detached = value.operator._cx.query(
                        "SELECT table FROM system.detached_tables WHERE database=currentDatabase()"
                    ).result_rows
                    for (table,) in with_detached:
                        value.operator._cx.command(
                            "ATTACH TABLE `" + table.replace("`", "``") + "`"
                        )


class Crash(BaseException):
    pass


def crash_at(checkpoint: str) -> Callable[[str], None]:
    def hook(step: str) -> None:
        if step == checkpoint:
            raise Crash(step)

    return hook


def quiet(_step: str) -> None:
    return None


WRITTEN_BEFORE = {1: (11, 1), 2: (22, 1)}


def assert_advanced(made: Group, receipt: dict[str, Any], *, recovered: bool = False) -> None:
    """The next map is in force, every table of the group is open at the next generation, and
    what was written before it still says so."""
    plan = made.plan
    assert (receipt["outcome"], receipt["reason"]) == ("advanced", "completed")
    assert receipt["recovered"] is recovered
    assert (receipt["map_version"], receipt["map_fingerprint"], receipt["write_epoch"]) == (
        plan.after.map_version,
        plan.after.fingerprint,
        plan.next_epoch,
    )
    assert receipt["advance_id"] == plan.advance_id
    assert receipt["advance_fingerprint"] == plan.fingerprint
    assert [(row["role"], row["engine"], row["table"]["name"]) for row in receipt["tables"]] == [
        ("source" if table == TABLE["source"] else "copy", engine, table)
        for engine, table in made.tables
    ]
    hold = made.state()["advances"][plan.advance_id]["receipt"]
    assert hold == receipt
    for role in ("source", "copy")[: len(made.tables)]:
        observed = made.fence(role)
        assert (observed.epoch, observed.closed, tuple(observed.holds)) == (2, False, ())
        assert len(observed.retired) == 1  # this advance's barrier, spent
    assert made.operator.active_map().fingerprint == plan.after.fingerprint
    local = load_local_map(made.root, model=made.model, project_id=PROJECT, public_key=made.public)
    assert local.fingerprint == plan.after.fingerprint
    for role_ in made.roles.values():
        assert role_.operator.map_watermark() == plan.after.map_version
    state = made.state()
    assert state["execution"] is None
    assert state["active_map"]["map_version"] == plan.after.map_version
    # A process still on the earlier map is refused by the engine and nothing of it is stored.
    with pytest.raises(sde.EngineError):
        made.old.save("Event", {"id": 90, "value": 9})
    assert all(90 not in stored for stored in made.everywhere())
    # A process on the next map writes under the next generation, to the source and the copy,
    # and the rows from before it carry the generation they were written in.
    made.session().save("Event", {"id": 3, "value": 33})
    assert made.everywhere() == [{**WRITTEN_BEFORE, 3: (33, 2)}] * len(made.tables)


def assert_abandoned(made: Group, receipt: dict[str, Any], reason: str) -> None:
    """The map in force is the one from before, and every table is open at its generation."""
    plan = made.plan
    assert (receipt["outcome"], receipt["reason"]) == ("abandoned", reason)
    assert receipt["recovered"] is True and receipt["within_budget"] is False
    assert (receipt["map_version"], receipt["map_fingerprint"], receipt["write_epoch"]) == (
        plan.before.map_version,
        plan.before.fingerprint,
        plan.source_epoch,
    )
    for role in ("source", "copy")[: len(made.tables)]:
        observed = made.fence(role)
        assert (observed.epoch, observed.closed, tuple(observed.holds)) == (1, False, ())
    assert made.operator.active_map().fingerprint == plan.before.fingerprint
    for role_ in made.roles.values():
        assert role_.operator.map_watermark() in (None, plan.before.map_version)
    state = made.state()
    assert state["execution"] is None
    assert state["advances"][plan.advance_id]["receipt"] == receipt
    assert state["active_map"]["map_version"] == plan.before.map_version
    # The application never stopped holding a map that is true: it writes at once.
    made.old.save("Event", {"id": 3, "value": 33})
    assert made.everywhere() == [{**WRITTEN_BEFORE, 3: (33, 1)}] * len(made.tables)


# --- the advance -----------------------------------------------------------------------------


@pytest.mark.parametrize("source", ["postgres", "clickhouse"])
def test_an_advance_raises_the_generation_of_the_source_and_its_copy(
    source: str, tmp_path: Path
) -> None:
    with group(source, tmp_path) as made:
        assert made.everywhere() == [WRITTEN_BEFORE, WRITTEN_BEFORE]
        receipt = made.operator.advance(made.plan).as_record()
        assert receipt["within_budget"] is True and receipt["protocol"] == 1
        assert receipt["project_id"] == PROJECT and receipt["group"] == "Event"
        assert_advanced(made, receipt)
        # The same packet again changes nothing and answers with the same receipt.
        before = (made.everywhere(), made.fence("source"), made.fence("copy"))
        assert made.operator.advance(made.plan).as_record() == receipt
        assert (made.everywhere(), made.fence("source"), made.fence("copy")) == before
        stored = made.operator.store.path.read_text()
        for role in made.roles.values():
            assert role.runtime._dsn not in stored and role.operator._dsn not in stored


@pytest.mark.parametrize("source", ["postgres", "clickhouse"])
def test_a_group_without_a_copy_is_advanced_on_its_source(source: str, tmp_path: Path) -> None:
    with group(source, tmp_path, copied=False) as made:
        receipt = made.operator.advance(made.plan).as_record()
        assert [row["role"] for row in receipt["tables"]] == ["source"]
        assert_advanced(made, receipt)


def test_the_state_of_an_advance_is_one_an_earlier_operator_refuses(tmp_path: Path) -> None:
    """The history of advances is part of the state, under a storage contract of its own: an
    operator that predates advances reads no such number and refuses the state whole."""
    with group("postgres", tmp_path) as made:
        envelope = json.loads(made.operator.store.path.read_text())
        assert envelope["storage_contract"] == 1 and "advances" not in envelope["payload"]
        made.operator._after_step = crash_at("advance_freeze_source:intent")
        with pytest.raises(Crash):
            made.operator.advance(made.plan)
        envelope = json.loads(made.operator.store.path.read_text())
        assert envelope["storage_contract"] == 6
        assert envelope["payload"]["advances"] == {}
        assert envelope["payload"]["execution"]["kind"] == "advance"
        made.operator._after_step = quiet
        made.operator.resume()
        envelope = json.loads(made.operator.store.path.read_text())
        assert envelope["storage_contract"] == 6
        assert list(envelope["payload"]["advances"]) == [made.plan.advance_id]


# --- recovery ----------------------------------------------------------------------------------


@pytest.mark.parametrize("checkpoint", [*BEFORE_DECISION, *AFTER_DECISION])
@pytest.mark.parametrize("source", ["postgres", "clickhouse"])
def test_the_advance_reaches_one_of_its_two_ends_from_every_checkpoint(
    source: str, checkpoint: str, tmp_path: Path
) -> None:
    with group(source, tmp_path) as made:
        made.operator._after_step = crash_at(checkpoint)
        with pytest.raises(Crash):
            made.operator.advance(made.plan)
        status = made.operator.status()
        assert status["plan_id"] == made.plan.advance_id
        assert status["decision"] == (None if checkpoint in BEFORE_DECISION else "advanced")
        # A second operation does not start beside it, and the packet itself is not run again.
        with pytest.raises(CutoverRecoveryRequired, match="requires resume"):
            made.operator.advance(made.plan)
        made.operator._after_step = quiet
        receipt = made.operator.resume().as_record()
        if checkpoint in BEFORE_DECISION:
            assert_abandoned(made, receipt, "recovered_before_decision")
        else:
            assert_advanced(made, receipt, recovered=True)
            assert receipt["within_budget"] is False
        # Either way the packet is spent: it answers with the receipt and changes nothing.
        assert made.operator.advance(made.plan).as_record() == receipt
        with pytest.raises(sde.MigrationRefused, match="no unfinished local operation"):
            made.operator.resume()


def test_a_crash_after_completion_leaves_only_the_receipt(tmp_path: Path) -> None:
    with group("postgres", tmp_path) as made:
        made.operator._after_step = crash_at("advance_complete")
        with pytest.raises(Crash):
            made.operator.advance(made.plan)
        made.operator._after_step = quiet
        with pytest.raises(sde.MigrationRefused, match="no unfinished local operation"):
            made.operator.resume()
        receipt = made.operator.advance(made.plan).as_record()
        assert_advanced(made, receipt)


def test_an_unfinished_advance_is_not_abandoned_by_hand(tmp_path: Path) -> None:
    with group("postgres", tmp_path) as made:
        made.operator._after_step = crash_at("advance_freeze_copy:done")
        with pytest.raises(Crash):
            made.operator.advance(made.plan)
        made.operator._after_step = quiet
        with pytest.raises(sde.MigrationRefused, match="not abandoned by hand: resume lifts"):
            made.operator.abandon()
        assert made.fence("source").closed and made.fence("copy").closed
        assert_abandoned(made, made.operator.resume().as_record(), "recovered_before_decision")


def test_a_recovery_checks_the_packet_its_state_holds(tmp_path: Path) -> None:
    """The state names its packet twice, as a record and as a fingerprint. A record that is
    another signed packet's, under the same id, is not resumed."""
    with group("postgres", tmp_path) as made:
        made.operator._after_step = crash_at("advance_freeze_copy:done")
        with pytest.raises(Crash):
            made.operator.advance(made.plan)
        made.operator._after_step = quiet

        def stored(plan: dict[str, Any]) -> None:
            with made.operator.store.lock():
                state = made.operator.store.read()
                state["execution"]["plan"] = plan
                made.operator.store.write(state)

        other = made.plan.as_record()
        other["pause_budget_ms"] += 1
        stored(made.signed(other))
        with pytest.raises(sde.MigrationRefused, match="names another signed authorization"):
            made.operator.resume()
        # Nothing was lifted or raised on its word.
        assert made.fence("source").closed and made.fence("copy").closed
        stored(made.plan.as_record())
        assert_abandoned(made, made.operator.resume().as_record(), "recovered_before_decision")


def test_another_packet_under_a_spent_id_is_refused(tmp_path: Path) -> None:
    with group("postgres", tmp_path) as made:
        made.operator.advance(made.plan)
        record = made.plan.as_record()
        record["pause_budget_ms"] += 1
        other = sde.load_advance_plan(
            made.signed(record), model=made.model, project_id=PROJECT, public_key=made.public
        )
        assert other.advance_id == made.plan.advance_id
        with pytest.raises(sde.MigrationRefused, match="names another authorization"):
            made.operator.advance(other)


# --- the pause ---------------------------------------------------------------------------------


@pytest.mark.parametrize("source", ["postgres", "clickhouse"])
def test_the_source_closes_first_and_opens_last(source: str, tmp_path: Path) -> None:
    """A write is the source's first and the copy's additionally. With the source closed first
    nothing new reaches the copy; with it opened last no write is taken while the copy refuses."""
    with group(source, tmp_path) as made:
        seen: dict[str, Any] = {}

        def probe(step: str) -> None:
            if step == "advance_freeze_source:done":
                seen["closed"] = (made.fence("source").closed, made.fence("copy").closed)
                with pytest.raises(sde.EngineError):
                    made.old.save("Event", {"id": 50, "value": 5})
                seen["after the first barrier"] = [50 in stored for stored in made.everywhere()]
            if step == "advance_release_copy:done":
                seen["opened"] = (made.fence("source").closed, made.fence("copy").closed)
                with pytest.raises(sde.EngineError):
                    made.session().save("Event", {"id": 60, "value": 6})
                seen["before the last release"] = [60 in stored for stored in made.everywhere()]

        made.operator._after_step = probe
        receipt = made.operator.advance(made.plan).as_record()
        assert seen == {
            "closed": (True, False),
            "after the first barrier": [False, False],
            "opened": (True, False),
            "before the last release": [False, False],
        }
        made.operator._after_step = quiet
        assert_advanced(made, receipt)


@pytest.mark.parametrize("source", ["postgres", "clickhouse"])
def test_no_write_is_taken_between_the_barrier_and_its_release(source: str, tmp_path: Path) -> None:
    """Under the barrier neither generation is admitted, so the mark is exact: nothing is written
    at the earlier generation after the advance began, nor at the next before its map is out."""
    with group(source, tmp_path) as made:
        refused: list[str] = []

        def probe(step: str) -> None:
            if step in ("advance_held:done", "advance_copy:done", "advance_publish:intent"):
                with pytest.raises(sde.EngineError):
                    made.old.save("Event", {"id": 70, "value": 7})
                refused.append(step)

        made.operator._after_step = probe
        receipt = made.operator.advance(made.plan).as_record()
        assert refused == ["advance_held:done", "advance_copy:done", "advance_publish:intent"]
        made.operator._after_step = quiet
        assert all(70 not in stored for stored in made.everywhere())
        assert_advanced(made, receipt)


@pytest.mark.parametrize(
    ("checkpoint", "end"),
    [
        ("advance_freeze_source:intent", "abandoned"),
        ("advance_freeze_copy:intent", "abandoned"),
        ("advance_held:done", "abandoned"),
        ("advance_source:intent", "advanced"),
        ("advance_publish:intent", "advanced"),
    ],
)
def test_a_spent_pause_budget_lifts_the_barrier_before_the_decision_and_never_after(
    checkpoint: str, end: str, tmp_path: Path
) -> None:
    """The budget is what the packet signs for the pause. Spent before the decision, the advance
    lifts its own barrier and keeps the map in force; a decision once kept is completed."""
    with group("postgres", tmp_path) as made:

        def spend(step: str) -> None:
            if step == checkpoint:
                made.operator._started -= (made.plan.pause_budget_ms + 1) * 1_000_000

        made.operator._after_step = spend
        receipt = made.operator.advance(made.plan).as_record()
        made.operator._after_step = quiet
        if end == "abandoned":
            assert_abandoned(made, receipt, "budget_exceeded")
        else:
            assert receipt["within_budget"] is False
            assert_advanced(made, receipt)


@pytest.mark.parametrize(
    ("checkpoint", "end"),
    [("advance_freeze_copy:intent", "abandoned"), ("advance_copy:intent", "advanced")],
)
def test_the_deadline_interrupts_a_server_that_stopped_answering(
    checkpoint: str, end: str, tmp_path: Path
) -> None:
    with group("postgres", tmp_path, budget_ms=3000) as made:
        reached: list[float] = []

        def stall(step: str) -> None:
            if step == checkpoint:
                reached.append(time.monotonic())
                made.roles["postgres"].operator._cx.execute("SELECT pg_sleep(30)")

        made.operator._after_step = stall
        with pytest.raises(CutoverRecoveryRequired, match="deadline"):
            made.operator.advance(made.plan)
        assert len(reached) == 1
        assert time.monotonic() - reached[0] < 20, "the deadline did not interrupt the statement"
        receipt = made.reconnect().resume().as_record()
        if end == "abandoned":
            assert_abandoned(made, receipt, "recovered_before_decision")
        else:
            assert_advanced(made, receipt, recovered=True)


# --- what it refuses before it touches anything ------------------------------------------------


def untouched(made: Group) -> None:
    assert made.state()["execution"] is None and "advances" not in made.state()
    for role in ("source", "copy"):
        observed = made.fence(role)
        assert (observed.epoch, observed.closed, observed.retired) == (1, False, ())
    assert made.operator.active_map().fingerprint == made.plan.before.fingerprint


@pytest.mark.parametrize("held", ["source", "copy"])
@pytest.mark.parametrize("source", ["postgres", "clickhouse"])
def test_another_operations_barrier_refuses_the_advance_until_it_is_released(
    source: str, held: str, tmp_path: Path
) -> None:
    with group(source, tmp_path) as made:
        engine = made.source if held == "source" else made.target
        fence = made.roles[engine].operator.write_fence(TABLE[held], project_id=PROJECT)
        other = uuid4().hex
        fence.freeze(other)
        with pytest.raises(sde.MigrationRefused, match="without another barrier"):
            made.operator.advance(made.plan)
        assert made.state()["execution"] is None
        fence.release(other)
        assert_advanced_beside_a_spent_barrier(made, made.operator.advance(made.plan).as_record())


def assert_advanced_beside_a_spent_barrier(made: Group, receipt: dict[str, Any]) -> None:
    assert (receipt["outcome"], receipt["write_epoch"]) == ("advanced", 2)
    for role in ("source", "copy"):
        observed = made.fence(role)
        assert (observed.epoch, observed.closed, tuple(observed.holds)) == (2, False, ())


def test_a_table_at_another_generation_refuses_the_advance(tmp_path: Path) -> None:
    with group("postgres", tmp_path) as made:
        fence = made.roles[made.target].operator.write_fence(TABLE["copy"], project_id=PROJECT)
        hold = uuid4().hex
        fence.freeze(hold)
        fence.advance(2)
        fence.release(hold)
        with pytest.raises(sde.MigrationRefused, match="the group's current generation"):
            made.operator.advance(made.plan)
        assert made.state()["execution"] is None
        assert made.fence("source").epoch == 1


def test_a_map_that_is_not_the_one_in_force_refuses_the_advance(tmp_path: Path) -> None:
    with group("postgres", tmp_path) as made:
        record = made.plan.as_record()
        for name, version in (("before", 5), ("after", 6)):
            record[name]["map_version"] = version
            record[name] = made.signed(record[name])
        record["advance_id"] = uuid4().hex
        other = sde.load_advance_plan(
            made.signed(record), model=made.model, project_id=PROJECT, public_key=made.public
        )
        with pytest.raises(sde.MigrationRefused, match="does not name the signed current map"):
            made.operator.advance(other)
        untouched(made)


@pytest.mark.parametrize("when", ["before", "while"])
def test_a_newer_map_that_overtakes_an_advance_is_named_with_both_versions(
    when: str, tmp_path: Path
) -> None:
    with group("postgres", tmp_path) as made:
        newer = made.plan.after.map_version + 5

        def adopt() -> None:
            made.roles["clickhouse"].operator.record_map_version(
                newer, model_version=made.model.version
            )

        if when == "before":
            adopt()
            expected = (
                f"a newer map was adopted before this advance: clickhouse records map version "
                f"{newer}, and the map this operation was prepared from is version "
                f"{made.plan.before.map_version}."
            )
            with pytest.raises(sde.MigrationRefused) as refused:
                made.operator.advance(made.plan)
            assert str(refused.value) == expected
            untouched(made)
        else:
            made.operator._after_step = (
                lambda step: adopt() if step == "advance_watermarks:intent" else None
            )
            expected = (
                f"a newer map appeared while completing the advance: clickhouse records map "
                f"version {newer}, and the map this operation was prepared from is version "
                f"{made.plan.after.map_version}."
            )
            # After its durable decision the operation is resumed, not refused: the reason it
            # stopped is beneath the error that says so.
            with pytest.raises(CutoverRecoveryRequired) as stopped:
                made.operator.advance(made.plan)
            reasons, error = [], stopped.value.__cause__
            while error is not None:
                reasons.append(str(error))
                error = error.__cause__
            assert expected in reasons
            # Decided and not finished: the barrier stays until a resume can publish.
            assert made.operator.status()["decision"] == "advanced"
            assert made.fence("source").closed and made.fence("copy").closed


def test_a_binding_the_group_needs_and_the_operator_lacks_refuses_the_advance(
    tmp_path: Path,
) -> None:
    with group("postgres", tmp_path) as made:
        lacking = LocalCutover(
            made.root,
            model=made.model,
            project_id=PROJECT,
            public_key=made.public,
            operators={"postgres": made.roles["postgres"].operator},
            runtime={"postgres": [made.roles["postgres"].runtime]},
        )
        with pytest.raises(sde.MigrationRefused, match="missing an operator binding"):
            lacking.advance(made.plan)
        untouched(made)


def test_a_grantee_the_operator_does_not_know_refuses_the_advance(tmp_path: Path) -> None:
    """As a cutover qualifies its tables: the generation an advance leaves is a mark a later
    comparison stands on, and a principal nobody declared could change a row under it."""
    with group("postgres", tmp_path) as made:
        role = made.roles["postgres"]
        stranger = "sde_stranger_" + uuid4().hex[:12]
        role.command(f'CREATE ROLE "{stranger}"')
        try:
            role.command(f'GRANT SELECT ON "{role.namespace}"."{TABLE["source"]}" TO "{stranger}"')
            with pytest.raises(sde.MigrationRefused):
                made.operator.advance(made.plan)
            untouched(made)
            role.command(
                f'REVOKE SELECT ON "{role.namespace}"."{TABLE["source"]}" FROM "{stranger}"'
            )
            assert made.operator.advance(made.plan).as_record()["outcome"] == "advanced"
        finally:
            role.command(f'DROP OWNED BY "{stranger}"')
            role.command(f'DROP ROLE "{stranger}"')


# --- a killed operator -------------------------------------------------------------------------


def spawn(made: Group, checkpoint: str) -> Any:
    import subprocess
    import sys

    from psycopg.conninfo import make_conninfo

    payload = {
        "directory": str(made.root),
        "project_id": PROJECT,
        "model": sde.neutral_declaration(made.model),
        "public_key": made.public.hex(),
        "plan": made.plan.as_record(),
        "checkpoint": checkpoint,
        "operators": {
            name: (
                make_conninfo(role.operator._dsn, options="-csearch_path=" + role.namespace)
                if name == "postgres"
                else role.operator._dsn
            )
            for name, role in made.roles.items()
        },
        "runtime": {name: role.runtime._dsn for name, role in made.roles.items()},
    }
    worker = subprocess.Popen(
        [sys.executable, str(Path(__file__).with_name("_advance_worker.py"))],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert worker.stdin is not None
    worker.stdin.write(json.dumps(payload))
    worker.stdin.close()
    return worker


def killed_at(made: Group, checkpoint: str) -> None:
    import os
    import select
    import signal

    worker = spawn(made, checkpoint)
    try:
        assert worker.stdout is not None and worker.stderr is not None
        assert select.select([worker.stdout], [], [], 60)[0], "the worker never stopped"
        line = worker.stdout.readline()
        assert line == "READY\n", line + worker.stderr.read()
    finally:
        if worker.poll() is None:
            os.kill(worker.pid, signal.SIGKILL)
        worker.wait(timeout=10)


@pytest.mark.parametrize(
    ("checkpoint", "end"),
    [
        ("advance_freeze_source:done", "abandoned"),
        ("native:detach", "abandoned"),
        ("advance_held:done", "abandoned"),
        ("advance_source:done", "advanced"),
        ("advance_publish:done", "advanced"),
    ],
)
@pytest.mark.parametrize("source", ["postgres", "clickhouse"])
def test_a_killed_operator_leaves_an_advance_that_resume_ends(
    source: str, checkpoint: str, end: str, tmp_path: Path
) -> None:
    """The process dies with its connections: locks go, the project lock goes, and what the
    engines hold is whatever its last statement left - a detached ClickHouse table included."""
    with group(source, tmp_path) as made:
        killed_at(made, checkpoint)
        receipt = made.reconnect().resume().as_record()
        if end == "abandoned":
            assert_abandoned(made, receipt, "recovered_before_decision")
        else:
            assert_advanced(made, receipt, recovered=True)


# --- the command line --------------------------------------------------------------------------


def operator_cli(made: Group, directory: Path) -> tuple[list[str], dict[str, str]]:
    import os
    import sys

    from psycopg.conninfo import make_conninfo

    config: dict[str, Any] = {
        "protocol": 1,
        "project_id": PROJECT,
        "model": sde.neutral_declaration(made.model),
        "public_keys": {"primary": base64.b64encode(made.public).decode()},
        "engines": {},
    }
    environment = dict(os.environ)
    for name, role in made.roles.items():
        op_name, run_name = "SDE_ADVANCE_" + name.upper(), "SDE_ADVANCE_APP_" + name.upper()
        operator_dsn = role.operator._dsn
        if name == "postgres":
            operator_dsn = make_conninfo(operator_dsn, options="-csearch_path=" + role.namespace)
        environment[op_name], environment[run_name] = operator_dsn, role.runtime._dsn
        config["engines"][name] = {
            "dialect": name,
            "operator_dsn_env": op_name,
            "runtime_dsn_envs": [run_name],
        }
    config_path = directory / "config.json"
    config_path.write_text(json.dumps(config))
    base = [
        sys.executable,
        "-m",
        "sde_operator",
        "--project-dir",
        str(made.operator.store.root),
        "--config",
        str(config_path),
    ]
    return base, environment


def run_cli(command: list[str], environment: dict[str, str]) -> tuple[int, Any]:
    import subprocess

    done = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=90)
    return done.returncode, json.loads(done.stdout if done.returncode == 0 else done.stderr)


@pytest.mark.parametrize("source", ["postgres", "clickhouse"])
def test_the_operator_cli_advances_and_repeats_the_same_receipt(
    source: str, tmp_path: Path
) -> None:
    with group(source, tmp_path / "state") as made:
        base, environment = operator_cli(made, tmp_path)
        packet = tmp_path / "advance.json"
        packet.write_text(json.dumps(made.plan.as_record()))
        results = [
            run_cli([*base, "advance", "--plan", str(packet)], environment) for _ in range(2)
        ]
        assert [code for code, _ in results] == [0, 0]
        assert results[0][1] == results[1][1]
        code, status = run_cli([*base, "status"], environment)
        assert code == 0 and status["active_map_version"] == made.plan.after.map_version
        assert status["plan_id"] is None
        assert_advanced(made, results[0][1])
        stored = made.operator.store.path.read_text() + json.dumps(results)
        for role in made.roles.values():
            assert role.runtime._dsn not in stored


def test_the_operator_cli_resumes_an_unfinished_advance_and_does_not_abandon_it(
    tmp_path: Path,
) -> None:
    with group("clickhouse", tmp_path / "state") as made:
        made.operator._after_step = crash_at("advance_copy:done")
        with pytest.raises(Crash):
            made.operator.advance(made.plan)
        made.operator._after_step = quiet
        base, environment = operator_cli(made, tmp_path)
        code, refusal = run_cli([*base, "abandon"], environment)
        assert code == 2 and refusal["error"] == "refused"
        assert "not abandoned by hand" in refusal["message"]
        code, status = run_cli([*base, "status"], environment)
        assert (code, status["plan_id"], status["decision"]) == (
            0,
            made.plan.advance_id,
            "advanced",
        )
        code, receipt = run_cli([*base, "resume"], environment)
        assert code == 0
        assert_advanced(made, receipt, recovered=True)


# --- more than one advance, and what is not this advance's ---------------------------------------


def following(made: Group) -> Any:
    """The advance that comes next: from the map in force to the generation after its own."""
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
    return sde.load_advance_plan(
        packet, model=made.model, project_id=PROJECT, public_key=made.public
    )


@pytest.mark.parametrize("source", ["postgres", "clickhouse"])
def test_a_second_advance_follows_the_first(source: str, tmp_path: Path) -> None:
    """Each advance has a barrier of its own, spent with it, so the next one starts clean; and
    each generation stays on the rows written under it."""
    with group(source, tmp_path) as made:
        made.operator.advance(made.plan)
        made.session().save("Event", {"id": 3, "value": 33})
        second = following(made)
        assert (second.source_epoch, second.next_epoch) == (2, 3)
        receipt = made.operator.advance(second).as_record()
        assert (receipt["outcome"], receipt["write_epoch"], receipt["map_version"]) == (
            "advanced",
            3,
            3,
        )
        made.session().save("Event", {"id": 4, "value": 44})
        assert made.everywhere() == [{1: (11, 1), 2: (22, 1), 3: (33, 2), 4: (44, 3)}] * 2
        for role in ("source", "copy"):
            observed = made.fence(role)
            assert (observed.epoch, observed.closed, len(observed.retired)) == (3, False, 2)
        assert set(made.state()["advances"]) == {made.plan.advance_id, second.advance_id}
        # The first packet is spent and still answers; it does not run against the newer map.
        assert made.operator.advance(made.plan).as_record()["write_epoch"] == 2
        assert made.fence("source").epoch == 3


@pytest.mark.parametrize("source", ["postgres", "clickhouse"])
def test_a_barrier_that_appears_mid_advance_stops_the_decision_and_the_ending(
    source: str, tmp_path: Path
) -> None:
    with group(source, tmp_path) as made:
        other = uuid4().hex
        fence = made.roles[made.target].operator.write_fence(TABLE["copy"], project_id=PROJECT)

        def intrude(step: str) -> None:
            if step == "advance_freeze_copy:done":
                fence.freeze(other)

        made.operator._after_step = intrude
        with pytest.raises(CutoverRecoveryRequired):
            made.operator.advance(made.plan)
        made.operator._after_step = quiet
        assert made.operator.status()["decision"] is None
        assert made.fence("source").epoch == made.fence("copy").epoch == 1
        # The recovery lifts this advance's own barrier. The other one is not its to lift, and
        # the advance reports no end while a table of the group is still closed.
        with pytest.raises(CutoverRecoveryRequired):
            made.operator.resume()
        assert tuple(made.fence("copy").holds) == (other,)
        assert not made.fence("source").closed
        assert made.operator.status()["decision"] == "abandoned"
        fence.release(other)
        assert_abandoned(made, made.operator.resume().as_record(), "recovered_before_decision")


def test_two_names_of_one_physical_table_refuse_the_advance(tmp_path: Path) -> None:
    """Two bindings of one database, and a map whose source and copy name the same table in
    them: one table would be fenced twice and counted as a source and its own copy."""
    with group("postgres", tmp_path / "state") as made:
        role = made.roles["postgres"]
        before = deepcopy(made.plan.as_record()["before"])
        for material in (
            before["groups"]["Event"]["source"],
            before["groups"]["Event"]["derived"][0],
        ):
            material["layout"] = deepcopy(before["groups"]["Event"]["source"]["layout"])
        before = made.signed(before)
        after = deepcopy(before)
        after["map_version"], after["groups"]["Event"]["write_epoch"] = 2, 2
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
        aliased = LocalCutover(
            tmp_path / "aliased",
            model=made.model,
            project_id=PROJECT,
            public_key=made.public,
            operators={"postgres": role.operator, "clickhouse": role.operator},
            runtime={"postgres": [role.runtime], "clickhouse": [role.runtime]},
        )
        aliased.enroll(before)
        with pytest.raises(sde.MigrationRefused, match="resolve to the same physical table"):
            aliased.advance(plan)
        observed = made.fence("source")
        assert (observed.epoch, observed.closed, observed.retired) == (1, False, ())
        with aliased.store.lock():
            assert aliased.store.read()["execution"] is None


def test_a_table_another_group_also_names_refuses_the_advance(tmp_path: Path) -> None:
    """Raising its generation would close it to the other group, whose map says the earlier one."""
    with group("postgres", tmp_path / "state", copied=False) as made:
        role = made.roles["postgres"]
        sde.clear_registry()

        @sde.entity
        class Event:
            id: int
            value: sde.Int32

        @sde.entity
        class Other:
            id: int
            value: sde.Int32

        model = sde.build_model(Event, Other)
        layout = made.plan.as_record()["before"]["groups"]["Event"]["source"]["layout"]

        def placed(entity: str, engine: str) -> dict[str, Any]:
            return {
                "write_epoch": 1,
                "source": {
                    "engine": engine,
                    "id": entity.lower(),
                    "layout": {
                        "tables": {entity: TABLE["source"]},
                        "columns": {entity: dict(layout["columns"]["Event"])},
                    },
                },
            }

        before = made.signed(
            {
                "contract": 4,
                "project_id": PROJECT,
                "model_version": model.version,
                "map_version": 1,
                "groups": {
                    "Event": placed("Event", "postgres"),
                    "Other": placed("Other", "clickhouse"),
                },
            }
        )
        after = deepcopy(before)
        after["map_version"], after["groups"]["Event"]["write_epoch"] = 2, 2
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
            packet, model=model, project_id=PROJECT, public_key=made.public
        )
        aliased = LocalCutover(
            tmp_path / "aliased",
            model=model,
            project_id=PROJECT,
            public_key=made.public,
            operators={"postgres": role.operator, "clickhouse": role.operator},
            runtime={"postgres": [role.runtime], "clickhouse": [role.runtime]},
        )
        aliased.enroll(before)
        with pytest.raises(sde.MigrationRefused, match="another group's physical table"):
            aliased.advance(plan)
        observed = made.fence("source")
        assert (observed.epoch, observed.closed, observed.retired) == (1, False, ())

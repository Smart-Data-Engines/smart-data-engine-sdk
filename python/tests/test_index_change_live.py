"""An index change removes indexes in force - alone or beside new ones - on the live tables.

Protocol 2 of the ``sde-index`` authorization. The operator builds and qualifies what it adds as
protocol 1 does, records its decision, publishes the next map, and only then removes each index the
next map no longer declares, one resumable step each. Measured before the design (SDK
``docs/qualification/in-place-index-drop/``): ``DROP INDEX CONCURRENTLY`` pauses no write; an open
transaction that has read the table holds it, while an older snapshot alone does not; and a drop
that is stopped leaves its index invalid yet still maintained - which a second drop removes.
ClickHouse drops a data-skipping index at once with ``alter_sync = 0``.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from test_index_operator_live import (
    CHECKPOINTS,
    Build,
    Crash,
    admin,
    assert_built,
    ch_indexes,
    ch_materializations,
    crash_at,
    expect_line,
    initial,
    kill,
    pg_indexes,
    quiet,
    spawn,
    wait_for,
)

import sde
from sde.local_cutover import CutoverRecoveryRequired

REMOVED = "sde_i_kept_000001"
AFTER_THE_DECISION = [
    *CHECKPOINTS[5:],
    "index_remove:intent",
    "index_remove:done",
]


def present(build: Build) -> set[str]:
    """The table's own indexes, by name, as the engine's catalogue lists them."""
    if build.engine == "postgres":
        return set(pg_indexes(build.role))
    return set(ch_indexes(build.role))


def assert_changed(build: Build, receipt: dict[str, Any]) -> None:
    assert receipt["protocol"] == 2
    assert receipt["outcome"] == "built"
    assert [row["name"] for row in receipt["removed"]] == [REMOVED]
    assert receipt["removed"][0]["table"]["name"] == "initial_events"
    assert REMOVED not in present(build)
    if build.plan.added:
        assert_built(build)
    source = build.plan.prepared.groups["Event"].source
    assert build.role.operator.validate_schema(source.layout, keys={"Event": ["id"]}) == ()
    assert build.operator.active_map().fingerprint == build.plan.prepared.fingerprint


@contextmanager
def open_reader(build: Build) -> Iterator[None]:
    """A transaction that has read the table and stays open: what a concurrent drop waits for.

    It holds a lock on the table until it ends. An older snapshot alone would not hold the drop
    (measured), unlike a concurrent build.
    """
    import psycopg
    from psycopg.conninfo import make_conninfo

    holder = psycopg.connect(
        make_conninfo(build.role.operator._dsn, options="-csearch_path=" + build.role.namespace)
    )
    holder.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
    holder.execute("SELECT count(*) FROM initial_events")
    try:
        yield
    finally:
        holder.rollback()
        holder.close()


@pytest.mark.parametrize("engine", ["postgres", "clickhouse"])
def test_an_index_in_force_is_removed_while_the_application_writes(
    engine: str, tmp_path: Path
) -> None:
    with initial(engine, tmp_path, kept=True, added=0, remove=True) as build:
        assert REMOVED in present(build)
        assert [index["name"] for index in build.plan.removed] == [REMOVED]
        assert build.plan.added == ()
        build.old.save_many("Event", [{"id": n, "value": n % 7} for n in range(2, 202)])
        receipt = build.operator.index(build.plan).as_record()
        assert_changed(build, receipt)
        assert receipt["indexes"] == []
        # A process still on the map in force keeps writing and reading, and a process on the
        # next map finds its table exactly as that map says.
        build.old.save("Event", {"id": 5000, "value": 5})
        assert build.old.get("Event", {"id": 5000}) == {"id": 5000, "value": 5}
        fresh = build.session()
        assert fresh.get("Event", {"id": 200}) == {"id": 200, "value": 200 % 7}
        if engine == "postgres":
            assert fresh.physical == ()
        # The state that holds a change is contract 5, which an older operator refuses.
        envelope = json.loads((tmp_path / "project.json").read_bytes())
        assert envelope["storage_contract"] == 5
        # Retrying the completed change returns the same receipt.
        assert build.operator.index(build.plan).as_record() == receipt


@pytest.mark.parametrize("engine", ["postgres", "clickhouse"])
def test_an_index_is_replaced_in_place(engine: str, tmp_path: Path) -> None:
    with initial(engine, tmp_path, kept=True, added=1, remove=True) as build:
        receipt = build.operator.index(build.plan).as_record()
        assert_changed(build, receipt)
        assert [row["name"] for row in receipt["indexes"]] == build.names
        assert set(build.names) <= present(build)


@pytest.mark.parametrize("checkpoint", AFTER_THE_DECISION)
@pytest.mark.parametrize("engine", ["postgres", "clickhouse"])
def test_a_change_resumes_after_every_checkpoint_after_its_decision(
    engine: str, checkpoint: str, tmp_path: Path
) -> None:
    with initial(engine, tmp_path, kept=True, added=1, remove=True) as build:
        build.operator._after_step = crash_at(checkpoint)
        with pytest.raises(Crash):
            build.operator.index(build.plan)
        build.operator._after_step = quiet
        if checkpoint != "index_remove:done":
            # Nothing of the map in force is touched before the next map is published.
            assert REMOVED in present(build)
        build.old.save("Event", {"id": 2, "value": 22})  # nothing is paused between attempts
        with pytest.raises(sde.MigrationRefused, match="cannot be abandoned"):
            build.operator.abandon()
        receipt = build.operator.resume().as_record()
        assert receipt["recovered"] is True
        assert_changed(build, receipt)
        assert build.operator.index(build.plan).as_record() == receipt


@pytest.mark.parametrize("engine", ["postgres", "clickhouse"])
def test_abandoning_a_change_before_its_decision_removes_nothing_in_force(
    engine: str, tmp_path: Path
) -> None:
    with initial(engine, tmp_path, kept=True, added=1, remove=True) as build:
        build.operator._after_step = crash_at("index_build:done")
        with pytest.raises(Crash):
            build.operator.index(build.plan)
        build.operator._after_step = quiet
        receipt = build.operator.abandon().as_record()
        assert (receipt["protocol"], receipt["outcome"]) == (2, "abandoned")
        assert receipt["removed"] == []
        assert REMOVED in present(build)  # the map in force is untouched
        assert not set(build.names) & present(build)  # and our own addition is gone
        assert build.operator.active_map().fingerprint == build.plan.current.fingerprint
        current = build.plan.current.groups["Event"].source
        assert build.role.operator.validate_schema(current.layout, keys={"Event": ["id"]}) == ()


@pytest.mark.parametrize("change", ["absent", "another_shape"])
@pytest.mark.parametrize("engine", ["postgres", "clickhouse"])
def test_an_index_in_force_that_is_not_as_declared_refuses_before_any_ddl(
    engine: str, change: str, tmp_path: Path
) -> None:
    """The design in force is read back before any DDL, and an index a change removes is in it."""
    with (
        initial(engine, tmp_path, kept=True, added=1, remove=True) as build,
        admin(build.role) as run,
    ):
        if engine == "postgres":
            run(f'DROP INDEX "{REMOVED}"')
            if change == "another_shape":
                run(f'CREATE INDEX "{REMOVED}" ON initial_events (id)')
        else:
            run(f"ALTER TABLE initial_events DROP INDEX `{REMOVED}` SETTINGS alter_sync = 0")
            if change == "another_shape":
                run(
                    f"ALTER TABLE initial_events ADD INDEX `{REMOVED}` value TYPE minmax "
                    "GRANULARITY 1"
                )
        before = present(build)
        with pytest.raises(sde.MigrationRefused, match="differ from the physical design"):
            build.operator.index(build.plan)
        assert present(build) == before  # nothing was built and nothing removed
        assert build.operator.store.read()["execution"] is None


def test_an_index_in_force_with_another_sort_order_refuses_before_any_ddl(
    tmp_path: Path,
) -> None:
    """The one shape the physical read-back does not compare: the removal's own check sees it."""
    with (
        initial("postgres", tmp_path, kept=True, added=1, remove=True) as build,
        admin(build.role) as run,
    ):
        run(f'DROP INDEX "{REMOVED}"')
        run(f'CREATE INDEX "{REMOVED}" ON initial_events (value DESC, id)')
        before = pg_indexes(build.role)[REMOVED]
        with pytest.raises(sde.MigrationRefused, match="not on its table as declared"):
            build.operator.index(build.plan)
        assert pg_indexes(build.role)[REMOVED] == before
        assert not set(build.names) & present(build)
        assert build.operator.store.read()["execution"] is None


def test_a_removal_held_by_an_open_reader_is_bounded_and_resumed(tmp_path: Path) -> None:
    with initial("postgres", tmp_path, kept=True, added=0, remove=True, budget_ms=6000) as build:
        with open_reader(build):
            started = time.monotonic()
            with pytest.raises(CutoverRecoveryRequired, match="build budget"):
                build.operator.index(build.plan)
            assert time.monotonic() - started < 20
            operator = build.reconnect()
            # Decided and published: the next map is in force, and the index stays - invalid, so
            # no query uses it, yet maintained - while the drop waits for the reader.
            assert operator.active_map().fingerprint == build.plan.prepared.fingerprint
            assert pg_indexes(build.role)[REMOVED][0] is False
            build.old.save("Event", {"id": 7000, "value": 7})  # writes go on meanwhile
            with pytest.raises(sde.MigrationRefused, match="cannot be abandoned"):
                operator.abandon()
        receipt = operator.resume().as_record()
        assert receipt["recovered"] is True
        assert_changed(build, receipt)
        assert build.session().get("Event", {"id": 7000}) == {"id": 7000, "value": 7}


def test_a_killed_operator_mid_removal_resumes_it(tmp_path: Path) -> None:
    with (
        initial("postgres", tmp_path, kept=True, added=0, remove=True) as build,
        admin(build.role) as run,
    ):
        with open_reader(build):
            worker = spawn(build, "native")
            try:
                expect_line(worker, "STARTED")
                rows = wait_for(
                    lambda: run(
                        "SELECT pid FROM pg_stat_activity "
                        "WHERE query LIKE 'DROP INDEX CONCURRENTLY%' AND wait_event_type = 'Lock'"
                    ),
                    "the removal to wait for the open reader",
                )
                kill(worker)
                pid = int(rows[0][0])
                # The server notices a vanished client only when it next talks to it; stand in
                # for that moment, which is when the drop stops server-side.
                run("SELECT pg_terminate_backend(%s)", [pid])
                wait_for(
                    lambda: not run("SELECT 1 FROM pg_stat_activity WHERE pid = %s", [pid]),
                    "the killed removal's backend to exit",
                )
            finally:
                kill(worker)
        assert pg_indexes(build.role)[REMOVED][0] is False  # left invalid, still maintained
        receipt = build.reconnect().resume().as_record()
        assert receipt["recovered"] is True
        assert_changed(build, receipt)


@pytest.mark.parametrize("engine", ["postgres", "clickhouse"])
def test_a_foreign_object_under_a_removed_name_after_the_decision_is_left_alone(
    engine: str, tmp_path: Path
) -> None:
    with (
        initial(engine, tmp_path, kept=True, added=0, remove=True) as build,
        admin(build.role) as run,
    ):
        build.operator._after_step = crash_at("index_publish:done")
        with pytest.raises(Crash):
            build.operator.index(build.plan)
        build.operator._after_step = quiet
        # Between the publication and the removal, somebody else's index takes the name.
        if engine == "postgres":
            run(f'DROP INDEX "{REMOVED}"')
            run(f'CREATE INDEX "{REMOVED}" ON initial_events (id)')
            foreign: Any = pg_indexes(build.role)[REMOVED]
        else:
            run(f"ALTER TABLE initial_events DROP INDEX `{REMOVED}` SETTINGS alter_sync = 0")
            run(f"ALTER TABLE initial_events ADD INDEX `{REMOVED}` value TYPE minmax GRANULARITY 1")
            foreign = ch_indexes(build.role)[REMOVED]
        receipt = build.operator.resume().as_record()
        assert receipt["outcome"] == "built"
        assert [row["name"] for row in receipt["removed"]] == [REMOVED]  # ours is gone
        found = pg_indexes(build.role) if engine == "postgres" else ch_indexes(build.role)
        assert found[REMOVED] == foreign  # theirs stays as it was


def test_a_removal_that_leaves_its_index_is_not_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The catalogue is read back after a drop: a drop that left the index is not a removal."""
    with initial("postgres", tmp_path, kept=True, added=0, remove=True) as build:
        native = build.operator.native["postgres"]
        command = native.command

        def no_drop(sql: str) -> None:
            if not sql.startswith("DROP INDEX"):
                command(sql)

        monkeypatch.setattr(native, "command", no_drop)
        with pytest.raises(CutoverRecoveryRequired):
            build.operator.index(build.plan)
        assert REMOVED in present(build)
        monkeypatch.setattr(native, "command", command)
        receipt = build.operator.resume().as_record()
        assert receipt["recovered"] is True
        assert_changed(build, receipt)


def test_a_pending_materialization_of_a_removed_index_is_killed(tmp_path: Path) -> None:
    """Left behind, the mutation would fail on every retry once its index is gone."""
    with (
        initial("clickhouse", tmp_path, kept=True, added=0, remove=True) as build,
        admin(build.role) as run,
    ):
        where = f"`{build.role.namespace}`.`initial_events`"
        run(f"SYSTEM STOP MERGES {where}")
        try:
            run(f"ALTER TABLE initial_events MATERIALIZE INDEX `{REMOVED}`")
            wait_for(
                lambda: ch_materializations(build.role, REMOVED) == [False],
                "the materialization to be pending",
            )
            receipt = build.operator.index(build.plan).as_record()
            assert_changed(build, receipt)
            assert ch_materializations(build.role, REMOVED) == []
        finally:
            run(f"SYSTEM START MERGES {where}")


def test_a_resumed_removal_waits_for_the_stopped_drop_and_finishes(tmp_path: Path) -> None:
    """The budget closed the operator's connection, not its drop: that runs on in the server.

    Resumed while the stopped drop still waits for the reader, the new drop waits behind it for
    the table's lock; when the reader ends, the stopped drop removes the index first. The resumed
    step must then finish, not fail on an index that is already gone.
    """
    import threading

    import psycopg
    from psycopg.conninfo import make_conninfo

    with (
        initial("postgres", tmp_path, kept=True, added=0, remove=True, budget_ms=6000) as build,
        admin(build.role) as run,
    ):
        holder = psycopg.connect(
            make_conninfo(build.role.operator._dsn, options="-csearch_path=" + build.role.namespace)
        )
        holder.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
        holder.execute("SELECT count(*) FROM initial_events")
        seen: list[Any] = []

        def release_once_both_drops_wait() -> None:
            try:
                seen.append(
                    wait_for(
                        lambda: (
                            len(
                                run(
                                    "SELECT pid FROM pg_stat_activity WHERE query LIKE "
                                    "'DROP INDEX CONCURRENTLY%' AND wait_event_type = 'Lock'"
                                )
                            )
                            == 2
                        ),
                        "the resumed drop to wait behind the stopped one",
                    )
                )
            except AssertionError as exc:
                seen.append(exc)
            finally:
                holder.rollback()
                holder.close()

        try:
            with pytest.raises(CutoverRecoveryRequired, match="build budget"):
                build.operator.index(build.plan)
            operator = build.reconnect()
        except BaseException:
            holder.close()
            raise
        helper = threading.Thread(target=release_once_both_drops_wait)
        helper.start()
        try:
            receipt = operator.resume().as_record()
        finally:
            helper.join(60)
        assert seen == [True]  # the race this is about did happen
        assert receipt["recovered"] is True
        assert_changed(build, receipt)

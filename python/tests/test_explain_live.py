"""What ``EXPLAIN`` actually does, measured, because the safety of requirement 19.4 rests on it.

Task 9.4 puts validation against a live engine on the library side, because only the library has a
connection. That means asking somebody else's database to plan an analyst's query - and the reason
this file exists rather than a docstring is that "``EXPLAIN`` does not execute the query" is a claim
about software we did not write.

Four things get measured here and two of them are surprises:

* plain ``EXPLAIN`` on ``WITH x AS (DELETE FROM t RETURNING *) SELECT * FROM x`` **does not delete
  the rows** - which matters because that exact shape is the one the control plane's read-only gate
  was built for, and it parses as a ``SELECT``;
* running that CTE **for real** inside the read-only transaction ``explain_plan`` uses fails with
  *"cannot execute SELECT in a read-only transaction"* - so the protection does not depend on the
  first measurement, and PostgreSQL's own wording is the same evidence the gate was built on;
* ``EXPLAIN SELECT pg_sleep(3)`` returns immediately, so planning does not call volatile functions;
* ``EXPLAIN SELECT 1/0`` **raises at plan time**, so planning *does* fold constants - which is why
  the read-only transaction is load-bearing rather than decorative. An immutable function that lies
  about being immutable is a thing a client's own database can contain.

And the one that requirement 19.4 exists for: a column the control plane's map says exists and this
database says does not. That is the only thing in this product that can catch a schema which drifted
from our map, and it can only be caught here.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator

import pytest

import sde
from sde.engines.postgres import PostgresEngine
from sde.errors import EngineError
from sde.explain import QueryPlanRefused

PG_DSN = os.environ.get("SDE_POSTGRES_DSN")
CH_DSN = os.environ.get("SDE_CLICKHOUSE_DSN")
TABLE = "explain_slice"


@pytest.fixture
def pg() -> Iterator[PostgresEngine]:
    assert PG_DSN
    with PostgresEngine(PG_DSN) as engine:
        with engine._cx.cursor() as cursor:
            cursor.execute(f'DROP TABLE IF EXISTS "{TABLE}" CASCADE')
            cursor.execute(f'CREATE TABLE "{TABLE}" (id int primary key, station text)')
            cursor.executemany(
                f'INSERT INTO "{TABLE}" VALUES (%s, %s)',
                [(n, f"s{n}") for n in range(200)],
            )
        engine._cx.commit()
        yield engine
        with engine._cx.cursor() as cursor:
            cursor.execute(f'DROP TABLE IF EXISTS "{TABLE}" CASCADE')
        engine._cx.commit()


def _rows(engine: PostgresEngine) -> int:
    with engine._cx.cursor() as cursor:
        cursor.execute(f'SELECT count(*) FROM "{TABLE}"')
        row = cursor.fetchone()
        return int(row[0]) if row else -1


# --- PostgreSQL: the safety measurements ----------------------------------------------------------


postgres = pytest.mark.skipif(
    not PG_DSN,
    reason="needs a live PostgreSQL: every claim in this file is about what a real engine does, "
    "and a fake would agree with whatever this library believes",
)


@postgres
def test_planning_a_data_modifying_cte_deletes_nothing(pg: PostgresEngine) -> None:
    """The shape the control plane's read-only gate exists for, put through ``explain``.

    It parses as a ``SELECT`` - that is the whole finding behind requirement 19.3's gate - so if
    ``EXPLAIN`` executed statements, this is the query that would empty the table without anything
    looking wrong.
    """
    before = _rows(pg)
    assert before == 200
    plan = sde.explain(pg, f'WITH x AS (DELETE FROM "{TABLE}" RETURNING *) SELECT * FROM x')
    assert _rows(pg) == before
    # And the plan says out loud what it planned, which is worth having in front of a reader.
    assert any("Delete" in line for line in plan.plan)


@postgres
def test_the_read_only_transaction_would_stop_it_if_planning_did_not(pg: PostgresEngine) -> None:
    """Why the protection does not rest on the measurement above.

    Planning folds constants, so it evaluates expressions; an immutable function that lies about
    being immutable is reachable from a plan. This is the mechanism that makes that irrelevant, and
    PostgreSQL's own message names *SELECT* - the same evidence the control plane's gate was built
    on.
    """
    import psycopg

    assert PG_DSN
    with psycopg.connect(PG_DSN) as connection, connection.cursor() as cursor:
        cursor.execute("SET TRANSACTION READ ONLY")
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction, match="read-only transaction"):
            cursor.execute(f'WITH x AS (DELETE FROM "{TABLE}" RETURNING *) SELECT * FROM x')
        connection.rollback()
    assert _rows(pg) == 200


@postgres
def test_planning_does_not_call_a_volatile_function(pg: PostgresEngine) -> None:
    """``pg_sleep(3)`` in the target list costs nothing, so the plan is not the query."""
    started = time.perf_counter()
    sde.explain(pg, "SELECT pg_sleep(3)")
    assert time.perf_counter() - started < 1.0


@postgres
def test_planning_does_fold_constants_and_that_is_a_refusal_not_a_plan(pg: PostgresEngine) -> None:
    """The measurement that makes the read-only transaction load-bearing.

    ``EXPLAIN SELECT 1/0`` raises at plan time, so planning evaluates immutable expressions. It is
    also the case that shows why "the engine would not plan this" has to be its own error: nothing
    here is wrong with the *plan*, and reporting it as an expensive query would be a lie in an
    unhelpful direction.
    """
    with pytest.raises(QueryPlanRefused, match="division by zero"):
        sde.explain(pg, "SELECT 1/0")
    with pytest.raises(QueryPlanRefused, match="Nothing was executed"):
        sde.explain(pg, "SELECT 1/0")


@postgres
def test_a_column_the_map_says_exists_and_the_database_does_not(pg: PostgresEngine) -> None:
    """Requirement 19.4's own case, and the only thing in this product that can catch it.

    The control plane validates a query against the schema **it authored**. That is a different
    schema from the one that exists the moment somebody runs DDL by hand - and 19.8 says
    hand-written work outside SDE is the client's, so hand-run DDL is too. The engine names the
    column, which is why the refusal carries its wording rather than a summary.
    """
    with pytest.raises(QueryPlanRefused, match="celsius"):
        sde.explain(pg, f'SELECT celsius FROM "{TABLE}"')

    with pg._cx.cursor() as cursor:
        cursor.execute(f'ALTER TABLE "{TABLE}" DROP COLUMN station')
    pg._cx.commit()
    with pytest.raises(QueryPlanRefused, match="station"):
        sde.explain(pg, f'SELECT station FROM "{TABLE}"')


# --- PostgreSQL: the plan and the cost ------------------------------------------------------------


@postgres
def test_the_plan_carries_the_engines_own_numbers_with_their_units(pg: PostgresEngine) -> None:
    plan = sde.explain(pg, f'SELECT * FROM "{TABLE}" WHERE station = \'s3\'')
    assert plan.dialect == "postgres"
    assert plan.cost is not None
    assert plan.cost.units == "PostgreSQL planner cost units"
    assert float(plan.cost.values["total_cost"]) > 0
    assert int(plan.cost.values["plan_rows"]) >= 1
    assert plan.cost.values["root_node"] == "Seq Scan"


@postgres
def test_the_cost_says_what_it_cannot_be_compared_to(pg: PostgresEngine) -> None:
    """The ``price_basis`` rule, applied to somebody else's number.

    PostgreSQL's total cost is unitless and depends on the machine and on planner settings, so a
    figure with no basis is not wrong - it is unfalsifiable, which is worse, because nothing can
    argue with it.
    """
    plan = sde.explain(pg, f'SELECT * FROM "{TABLE}"')
    assert plan.cost is not None
    assert "arbitrary units" in plan.cost.basis
    assert "seq_page_cost" in plan.cost.basis
    assert "meaningless against a number from anywhere else" in plan.cost.basis


@postgres
def test_a_sequential_scan_under_a_filter_is_reported_as_a_shape(pg: PostgresEngine) -> None:
    """One finding, and it is true at any size - unlike a threshold in arbitrary units."""
    plan = sde.explain(pg, f'SELECT * FROM "{TABLE}" WHERE station = \'s7\'')
    kinds = {finding.kind for finding in plan.findings}
    assert "full_scan_under_filter" in kinds
    detail = next(f.detail for f in plan.findings if f.kind == "full_scan_under_filter")
    assert TABLE in detail
    assert "An index on the filtered column is what changes this" in detail
    assert "ours to own" in detail


@postgres
def test_a_primary_key_lookup_produces_no_finding(pg: PostgresEngine) -> None:
    """The half that makes the finding above worth reading.

    A check that fires on every query is a check people stop reading, so the negative case is
    pinned: an index lookup is not reported as anything.
    """
    plan = sde.explain(pg, f'SELECT * FROM "{TABLE}" WHERE id = 7')
    assert plan.findings == ()


@postgres
def test_the_plan_reports_how_writing_was_prevented(pg: PostgresEngine) -> None:
    """A guarantee whose state cannot be read is a guarantee taken on trust.

    And the wording matters: this library has no SQL parser, so it cannot say "we checked the
    query". What it can say is what the engine was told.
    """
    plan = sde.explain(pg, f'SELECT count(*) FROM "{TABLE}"')
    assert "SET TRANSACTION READ ONLY" in plan.read_only_enforced
    assert "rolled back" in plan.read_only_enforced
    text = plan.for_a_human()
    assert "Not executed." in text
    assert "Write protection:" in text


@postgres
def test_the_human_rendering_puts_the_findings_above_the_plan(pg: PostgresEngine) -> None:
    """Same ordering rule the issued query follows: a caveat under forty lines is unread."""
    text = sde.explain(pg, f'SELECT * FROM "{TABLE}" WHERE station = \'s1\'').for_a_human()
    assert text.index("Worth reading before you run it:") < text.index("Plan, as the engine")
    assert text.index("Estimate, in") < text.index("Plan, as the engine")


# --- ClickHouse -----------------------------------------------------------------------------------


clickhouse = pytest.mark.skipif(
    not CH_DSN,
    reason="needs a live ClickHouse: EXPLAIN ESTIMATE and readonly=1 are both measured here",
)

CH_TABLE = "explain_slice_ch"


@pytest.fixture
def ch() -> Iterator[object]:
    assert CH_DSN
    from sde.engines.clickhouse import ClickHouseEngine

    with ClickHouseEngine(CH_DSN) as engine:
        engine._cx.command(f"DROP TABLE IF EXISTS `{CH_TABLE}`")
        engine._cx.command(
            f"CREATE TABLE `{CH_TABLE}` (id Int64, station String) "
            f"ENGINE = ReplacingMergeTree ORDER BY (id)"
        )
        engine._cx.insert(
            CH_TABLE, [[n, f"s{n}"] for n in range(500)], column_names=["id", "station"]
        )
        yield engine
        engine._cx.command(f"DROP TABLE IF EXISTS `{CH_TABLE}`")


@clickhouse
def test_clickhouse_gives_parts_rows_and_marks(ch: object) -> None:
    plan = sde.explain(ch, f"SELECT * FROM `{CH_TABLE}` WHERE station = 's3'")
    assert plan.dialect == "clickhouse"
    assert plan.cost is not None
    assert plan.cost.units == "parts, rows and marks to read"
    assert int(plan.cost.values["rows"]) == 500
    assert int(plan.cost.values["marks"]) >= 1
    assert int(plan.cost.values["tables"]) == 1
    assert any("ReadFromMergeTree" in line for line in plan.plan)


@clickhouse
def test_the_clickhouse_basis_admits_the_estimate_is_coarse(ch: object) -> None:
    """Measured: a small table reports every row and one mark whether the key filter prunes or not.

    A granule is 8192 rows by default, so below that the estimate cannot distinguish a key lookup
    from a scan. Saying so is the difference between a number and a number somebody can use.
    """
    by_key = sde.explain(ch, f"SELECT * FROM `{CH_TABLE}` WHERE id = 3")
    by_value = sde.explain(ch, f"SELECT * FROM `{CH_TABLE}` WHERE station = 's3'")
    assert by_key.cost is not None
    assert by_value.cost is not None
    assert by_key.cost.values["rows"] == by_value.cost.values["rows"]
    assert "coarse below one granule" in by_key.cost.basis
    assert "8192" in by_key.cost.basis


@clickhouse
def test_the_final_hazard_is_reported_from_the_live_engine(ch: object) -> None:
    """9.7 measured this and could only warn about it in prose. Now the engine says it.

    A ``ReplacingMergeTree`` keeps both rows written under one key until a merge collapses them,
    so a read without ``FINAL`` counts both - measured with merges stopped, two rows against one.
    The finding is a fact about the **table**, phrased that way on purpose: there is no SQL parser
    here, so whether the query says ``FINAL`` is not something this check can see.
    """
    plan = sde.explain(ch, f"SELECT count() FROM `{CH_TABLE}`")
    kinds = {finding.kind for finding in plan.findings}
    assert "replacing_merge_tree" in kinds
    detail = next(f.detail for f in plan.findings if f.kind == "replacing_merge_tree")
    assert CH_TABLE in detail
    assert "two rows against one" in detail
    assert "a fact about the table rather than a claim about your query" in detail


@clickhouse
def test_readonly_refuses_a_mutation_and_still_answers_explain(ch: object) -> None:
    """Both halves, because a protection that blocked the measurement would be switched off.

    Code 164 is ClickHouse's own ``READONLY``. ``EXPLAIN ESTIMATE`` works under it, which is what
    makes this the setting to use rather than a compromise.
    """
    with pytest.raises(Exception, match=r"readonly|READONLY"):
        ch._cx.command(  # type: ignore[attr-defined]
            f"ALTER TABLE `{CH_TABLE}` DELETE WHERE id < 10", settings={"readonly": 1}
        )
    plan = sde.explain(ch, f"SELECT * FROM `{CH_TABLE}`")
    assert plan.cost is not None
    assert int(plan.cost.values["rows"]) == 500
    assert "readonly=1" in plan.read_only_enforced
    assert "code 164" in plan.read_only_enforced


@clickhouse
def test_a_trivial_count_reads_nothing_and_the_estimate_says_so(ch: object) -> None:
    """The measurement that broke the obvious design, and it is worth pinning on its own.

    ClickHouse answers ``SELECT count() FROM t`` from part metadata, so ``EXPLAIN ESTIMATE``
    returns **no rows at all** - not "0 rows read for this table", nothing. The first version of
    the adapter took the table names from that answer, which meant the one query where 9.7's
    hazard is worst - an uncollapsed count over a ReplacingMergeTree - was the one query whose
    table could not be named. The names now come from ``EXPLAIN QUERY TREE``.
    """
    plan = sde.explain(ch, f"SELECT count() FROM `{CH_TABLE}`")
    assert plan.cost is not None
    assert plan.cost.values["rows"] == "0"
    assert plan.cost.values["tables"] == "0"
    assert "answered from part metadata" in plan.cost.basis
    # And the warning survives the table being invisible to the estimate.
    assert {f.kind for f in plan.findings} == {"replacing_merge_tree"}


@clickhouse
def test_the_table_names_come_from_the_analyzer_and_not_from_the_estimate(ch: object) -> None:
    """A join names both tables even when only one of them is read.

    Which is the right answer for this finding: a ReplacingMergeTree the query touches is a
    ReplacingMergeTree whether or not the plan ends up reading granules from it.
    """
    plan = sde.explain(
        ch,
        f"SELECT a.id FROM `{CH_TABLE}` AS a "
        f"INNER JOIN `{CH_TABLE}` AS b ON a.id = b.id WHERE a.id = 3",
    )
    details = " ".join(f.detail for f in plan.findings)
    assert CH_TABLE in details


@clickhouse
def test_a_server_without_the_new_analyzer_still_gets_a_plan(ch: object) -> None:
    """The fallback path, which shipped raising a ValueError at the client.

    ``EXPLAIN QUERY TREE`` needs the new analyzer, so the branch behind it is a real deployment
    condition and it carried a ``pragma: no cover``. Behind that marker sat a ``log()`` call with
    an event name absent from the vocabulary, and ``log()`` raised on an unknown name - so a client
    on an older ClickHouse asked for a query plan and got our note to ourselves, "add it to
    EVENTS", instead of one. The marker saying no test comes here was one line above the call that
    needed a test.

    Two things changed and this pins both: the name is in the vocabulary, and an unknown name is
    counted rather than raised. The path is exercised by making the analyzer query fail, which is
    what an older server does.
    """
    engine = ch
    real_query = engine._cx.query  # type: ignore[attr-defined]

    def older_server(sql: str, *args: object, **kwargs: object) -> object:
        if sql.startswith("EXPLAIN QUERY TREE"):
            raise RuntimeError("Code: 46. Unknown expression identifier (older analyzer)")
        return real_query(sql, *args, **kwargs)

    engine._cx.query = older_server  # type: ignore[attr-defined]
    try:
        plan = sde.explain(engine, f"SELECT id FROM `{CH_TABLE}` WHERE id = 3")
    finally:
        engine._cx.query = real_query  # type: ignore[attr-defined]

    assert plan.plan, "a plan is still returned; only the table names lose their source"
    assert plan.cost is not None
    # The estimate's own names take over, so the ReplacingMergeTree finding survives for a query
    # whose plan reads granules. It is the trivial count() that loses it - see the test above.
    assert any(CH_TABLE in finding.detail for finding in plan.findings)


@clickhouse
def test_clickhouse_refuses_to_plan_a_mutation_as_a_syntax_error(ch: object) -> None:
    """Measured, and it is a stronger position than PostgreSQL's.

    ``EXPLAIN`` there accepts only ``SELECT``-shaped statements, so a mutation cannot even be
    expressed as a plan request. Worth pinning, because it is the reason the ClickHouse path needs
    no equivalent of the data-modifying-CTE measurement.
    """
    with pytest.raises(QueryPlanRefused, match="Nothing was executed"):
        sde.explain(ch, f"ALTER TABLE `{CH_TABLE}` DELETE WHERE id < 10")


@clickhouse
def test_a_column_that_does_not_exist_is_refused_by_clickhouse_too(ch: object) -> None:
    with pytest.raises(QueryPlanRefused, match="celsius"):
        sde.explain(ch, f"SELECT celsius FROM `{CH_TABLE}`")


# --- the adapter that cannot ----------------------------------------------------------------------


def test_an_adapter_with_no_planner_is_refused_by_name() -> None:
    """And the refusal separates the two reasons, because they have different fixes."""

    class Bare:
        dialect = "something"

    with pytest.raises(EngineError, match="cannot plan a query"):
        sde.explain(Bare(), "SELECT 1")
    with pytest.raises(EngineError, match="keeps this product out of the data path"):
        sde.explain(Bare(), "SELECT 1")


def test_the_orderbook_adapter_refuses_and_says_why() -> None:
    """The fourth named difference in that adapter, named for the same reason as the other three."""
    from sde.engines.orderbook import OrderbookEngine

    engine = OrderbookEngine(host="127.0.0.1", port=1)
    with pytest.raises(EngineError, match="no query planner"):
        engine.explain_plan("SELECT 1")
    with pytest.raises(EngineError, match="reads as 'nothing to worry about'"):
        engine.explain_plan("SELECT 1")


def test_an_empty_query_is_refused_before_any_engine_is_touched() -> None:
    class Exploding:
        dialect = "x"

        def explain_plan(self, sql: str) -> object:
            raise AssertionError("should not have been reached")

    with pytest.raises(EngineError, match="no query here to plan"):
        sde.explain(Exploding(), "   ")


# --- guards that survived their first mutation, and one correction --------------------------------


@postgres
def test_the_read_only_setting_is_read_back_and_a_failure_refuses_to_plan(
    pg: PostgresEngine,
) -> None:
    """The guard is verified rather than asserted, and that is what makes it testable at all.

    Three measurements got this here. Planning cannot write - PostgreSQL refuses an ``INSERT``
    inside a non-volatile function outright, and a ``VOLATILE`` function is not folded - so the
    docstring's first version, which claimed an immutable function could sneak a write into a
    plan, was wrong. ``EXPLAIN INSERT`` is planned identically in a read-only and a read-write
    transaction. So the guard has **no observable effect** on anything this method does, and
    deleting it changed no output: it survived its first mutation, correctly.

    What it does protect against is one keyword: ``EXPLAIN (ANALYZE)`` of a write **runs** in a
    read-write transaction and is **refused** in a read-only one - measured in the test below. A
    guard whose present hazard is zero and whose future hazard is an accidental keyword is worth
    keeping, and the way to keep it honestly is to read the setting back. Which is a lesson this
    project already owns from three GitHub API endpoints that returned 200 and changed nothing.
    """
    import psycopg

    # The read-back happens, and it says on.
    with psycopg.connect(str(PG_DSN)) as connection, connection.cursor() as cursor:
        cursor.execute("SET TRANSACTION READ ONLY")
        cursor.execute("SHOW transaction_read_only")
        row = cursor.fetchone()
        assert row is not None
        assert str(row[0]) == "on"
        connection.rollback()

    # And when it comes back off, nothing is planned. Simulated by making the server refuse the
    # SET - the shape a pooler that rewrites session state would produce.
    original = PostgresEngine.explain_plan

    def with_the_set_swallowed(self: PostgresEngine, sql: str) -> object:
        real = self._cx.cursor

        class Cursor:
            def __init__(self, inner: object) -> None:
                self._inner = inner

            def execute(self, statement: str, *args: object) -> object:
                if statement == "SET TRANSACTION READ ONLY":
                    return None  # the pooler ate it
                return self._inner.execute(statement, *args)  # type: ignore[attr-defined]

            def __getattr__(self, name: str) -> object:
                return getattr(self._inner, name)

        class Context:
            def __enter__(self) -> object:
                self._handle = real()
                return Cursor(self._handle.__enter__())

            def __exit__(self, *exc: object) -> object:
                return self._handle.__exit__(*exc)

        self._cx.cursor = Context  # type: ignore[method-assign]
        try:
            return original(self, sql)
        finally:
            del self._cx.cursor

    with pytest.raises(EngineError, match="would not go read-only"):
        with_the_set_swallowed(pg, f'SELECT count(*) FROM "{TABLE}"')
    assert _rows(pg) == 200


@postgres
def test_explain_analyze_of_a_write_runs_unless_the_transaction_is_read_only() -> None:
    """The measurement that justifies keeping the guard, and it is somebody else's behaviour.

    ``EXPLAIN (ANALYZE)`` starts the executor. In a read-write transaction it inserts the row - the
    ``(actual ...)`` in the plan is the proof. In a read-only one PostgreSQL refuses with *"cannot
    execute INSERT in a read-only transaction"*. So the distance between planning an analyst's
    query and running it is one keyword, and this is what that keyword hits.
    """
    import psycopg

    assert PG_DSN
    with psycopg.connect(PG_DSN, autocommit=True) as connection, connection.cursor() as cursor:
        cursor.execute("DROP TABLE IF EXISTS explain_analyze_probe")
        cursor.execute("CREATE TABLE explain_analyze_probe (n int)")

    with psycopg.connect(PG_DSN) as connection, connection.cursor() as cursor:
        cursor.execute("SET TRANSACTION READ ONLY")
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction, match="read-only transaction"):
            cursor.execute("EXPLAIN (ANALYZE) INSERT INTO explain_analyze_probe VALUES (1)")
        connection.rollback()

    with psycopg.connect(PG_DSN) as connection, connection.cursor() as cursor:
        cursor.execute("EXPLAIN (ANALYZE) INSERT INTO explain_analyze_probe VALUES (2)")
        rendered = " ".join(str(row[0]) for row in cursor.fetchall())
        assert "actual" in rendered, "ANALYZE did not start the executor, so this test proves less"
        connection.rollback()

    with psycopg.connect(PG_DSN, autocommit=True) as connection, connection.cursor() as cursor:
        cursor.execute("DROP TABLE explain_analyze_probe")


@postgres
def test_a_scan_below_the_root_of_the_plan_is_still_found(pg: PostgresEngine) -> None:
    """The walk has to descend, and a root-only check would pass every test above.

    A sort puts the sequential scan one level down, which is where it lives in almost every real
    plan - the root of a plan is an aggregate or a sort far more often than it is a scan.
    """
    plan = sde.explain(
        pg, f'SELECT * FROM "{TABLE}" WHERE station = \'s5\' ORDER BY id DESC LIMIT 5'
    )
    assert plan.cost is not None
    assert plan.cost.values["root_node"] != "Seq Scan"
    assert {finding.kind for finding in plan.findings} == {"full_scan_under_filter"}


@clickhouse
def test_a_table_that_is_not_a_replacing_merge_tree_produces_no_finding(ch: object) -> None:
    """The negative half. A finding that fires for every table is a finding nobody reads."""
    plain = "explain_slice_plain"
    ch._cx.command(f"DROP TABLE IF EXISTS `{plain}`")  # type: ignore[attr-defined]
    ch._cx.command(  # type: ignore[attr-defined]
        f"CREATE TABLE `{plain}` (id Int64, station String) ENGINE = MergeTree ORDER BY (id)"
    )
    try:
        ch._cx.insert(  # type: ignore[attr-defined]
            plain, [[n, f"s{n}"] for n in range(100)], column_names=["id", "station"]
        )
        plan = sde.explain(ch, f"SELECT * FROM `{plain}` WHERE station = 's3'")
        assert plan.findings == ()
    finally:
        ch._cx.command(f"DROP TABLE IF EXISTS `{plain}`")  # type: ignore[attr-defined]


@clickhouse
def test_every_statement_carries_readonly_one(ch: object) -> None:
    """The safety claim, tested as a claim about what is sent rather than about what came back.

    The live tests around this one show that ``readonly=1`` refuses a mutation and still answers
    ``EXPLAIN ESTIMATE``. Neither of them notices if the setting is simply not sent, because
    nothing ``explain_plan`` runs would write - the same shape as the read-only transaction on the
    PostgreSQL side. So this records the settings of every call.
    """
    real = ch._cx  # type: ignore[attr-defined]
    seen: list[dict[str, object]] = []

    class Recording:
        def query(self, sql: str, **kwargs: object) -> object:
            seen.append(dict(kwargs.get("settings") or {}))
            return real.query(sql, **kwargs)

        def __getattr__(self, name: str) -> object:
            return getattr(real, name)

    ch._client = Recording()  # type: ignore[attr-defined]
    try:
        sde.explain(ch, f"SELECT * FROM `{CH_TABLE}` WHERE station = 's1'")
    finally:
        ch._client = real  # type: ignore[attr-defined]

    assert len(seen) >= 3, seen
    for settings in seen:
        assert settings.get("readonly") == 1, seen


def test_a_cost_with_no_units_or_no_basis_is_not_constructible() -> None:
    """A figure whose meaning is not stated cannot be argued with.

    The ``price_basis`` rule, and the type is where it is enforced rather than a convention: a
    documented rule is followed until somebody is in a hurry.
    """
    from sde.explain import Cost

    with pytest.raises(EngineError, match="units and its basis"):
        Cost(units="", basis="something", values={})
    with pytest.raises(EngineError, match="units and its basis"):
        Cost(units="something", basis="   ", values={})
    assert Cost(units="a", basis="b", values={}).as_record()["units"] == "a"


def test_a_plan_with_no_plan_in_it_is_not_constructible() -> None:
    """An engine that answered nothing is a refusal, and a refusal is not an empty result."""
    from sde.explain import QueryPlan

    with pytest.raises(EngineError, match="not a plan"):
        QueryPlan(
            engine="x", dialect="x", plan=(), cost=None, findings=(), read_only_enforced="y"
        )
    with pytest.raises(EngineError, match="how the engine was stopped from writing"):
        QueryPlan(
            engine="x", dialect="x", plan=("a",), cost=None, findings=(), read_only_enforced=" "
        )


@postgres
def test_a_full_scan_with_no_filter_is_not_a_finding(pg: PostgresEngine) -> None:
    """Because it is not one. The query asked for the whole table.

    The finding's text says "every row is fetched to discard most of them", and with no filter
    nothing is discarded - so emitting it here would be a false statement, not a noisy one. This
    is the case that distinguishes ``Seq Scan`` from ``Seq Scan under a filter``, and the negative
    test above uses an index lookup, which does not.
    """
    plan = sde.explain(pg, f'SELECT * FROM "{TABLE}"')
    assert plan.cost is not None
    assert plan.cost.values["root_node"] == "Seq Scan"
    assert plan.findings == ()

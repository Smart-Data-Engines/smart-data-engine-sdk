"""Validation against a **live** engine, which is the only kind this library can do.

Requirement 19.4 splits the work in two and the split is the interesting part. The control plane
validates a query against the schema **it authored** - only tables and columns its own map defines,
and read-only, proved by walking a syntax tree. That is real and it is not enough, because the
schema it authored and the schema that *exists* are two different things the moment somebody runs
DDL by hand. Only the engine knows the second one, and only this library has a connection to the
engine. So this module is the half the control plane deliberately does not pretend to do.

**What it does not do is execute the query.** Requirement 19.2: executing an analyst's query would
put this product in the data path for analytics. ``EXPLAIN`` plans; it does not run. That is a claim
about somebody else's software, so it is measured rather than believed - see
``python/tests/test_explain_live.py``, which puts ``WITH x AS (DELETE FROM t RETURNING *) SELECT *
FROM x`` through ``explain`` and counts the rows afterwards. They are all there.

**And the safety does not rest on that measurement.** This library has **no runtime dependencies**
and therefore no SQL parser, so it cannot tell a read from a write by looking - the control plane's
gate needs ``sqlglot`` for exactly that reason, and the case it was built for parses as a
``SELECT``. What this module does instead is make the *engine* refuse:

* PostgreSQL: the plan is taken inside ``SET TRANSACTION READ ONLY``, and the transaction is rolled
  back. Measured: running that data-modifying CTE for real in such a transaction fails with
  ``cannot execute SELECT in a read-only transaction`` - and the word *SELECT* in PostgreSQL's own
  message is the same evidence the control plane's gate was built on.
* ClickHouse: every statement is sent with ``readonly=1``, which refuses a mutation (code 164) and
  leaves ``EXPLAIN ESTIMATE`` working. Measured, both halves.
* The orderbook engine has no query language to plan and refuses by name.

That ordering matters: **planning is safe, and if it were not, the transaction would still stop
it.** Constant folding is the reason the second sentence is not decoration - ``EXPLAIN SELECT 1/0``
raises ``DivisionByZero`` at plan time, so planning *does* evaluate immutable expressions, and an
immutable function that lies about being immutable is a thing a client's own database can contain.

**The cost travels with its units and what it cannot be compared to.** PostgreSQL's planner cost is
in arbitrary units that depend on the machine and on ``seq_page_cost``; ClickHouse reports parts,
rows and marks, which are real units and coarse at small scale - measured, a thousand-row table
reports the whole thousand and one mark whether or not the key filter applies, because a granule
is 8192 rows. A number with no statement of what it means is not wrong, it is unfalsifiable, which
is worse: the same rule that put ``price_basis`` beside every engine price.

**There is no timestamp on a plan.** Not an omission: this library reads the wall clock exactly
once, in ``verify()``, and ``python/tests/test_no_expiry.py`` pins that count - because "what time
is it" is the first thing an expiry check needs. A caller who wants a plan dated can date it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from .errors import EngineError

__all__ = [
    "Cost",
    "Explains",
    "PlanFinding",
    "QueryPlan",
    "QueryPlanRefused",
    "explain",
]


class QueryPlanRefused(EngineError):
    """The engine would not plan this query, and that refusal **is** the validation.

    Carries the engine's own message rather than a summary of it. The commonest cause is the one
    requirement 19.4 exists for - a column the control plane's map says exists and the engine says
    does not, because somebody ran DDL by hand - and the engine's wording names the column.
    """


@dataclass(frozen=True)
class Cost:
    """The engine's own numbers, with their units and what they cannot be compared to.

    ``values`` are strings, like every other number this library stores: a fixed-precision string
    is the same value in every process, and a plan may be written down and read back.

    ``basis`` is not padding and it is not the same sentence for two engines. PostgreSQL's total
    cost is unitless and depends on the machine and on planner settings, so comparing it to a
    number from another database is meaningless; ClickHouse's parts, rows and marks are real
    counts and are coarse below one granule. A cost with no basis is unfalsifiable, which is worse
    than being wrong.
    """

    units: str
    basis: str
    values: Mapping[str, str]

    def __post_init__(self) -> None:
        if not self.units.strip() or not self.basis.strip():
            raise EngineError(
                "a cost estimate needs both its units and its basis. A figure whose meaning is "
                "not stated cannot be argued with, and a decision resting on one is not auditable."
            )

    def as_record(self) -> dict[str, Any]:
        return {"units": self.units, "basis": self.basis, "values": dict(self.values)}


@dataclass(frozen=True)
class PlanFinding:
    """One thing the plan says that is worth reading, with what would change it.

    Deliberately few, and every one is a **shape** fact rather than a threshold. A threshold on
    PostgreSQL's cost would be taste in arbitrary units; a sequential scan under a filter is a
    statement about what the engine will do, and it is true at any size.
    """

    kind: str
    detail: str

    def as_record(self) -> dict[str, Any]:
        return {"kind": self.kind, "detail": self.detail}


@dataclass(frozen=True)
class QueryPlan:
    """What a live engine says about a query it has not run.

    ``read_only_enforced`` is reported rather than assumed, for the reason
    ``session.rollback_protection`` is: a guarantee whose state cannot be read is a guarantee taken
    on trust. It says *how* the engine was stopped from writing, so a reader can check the claim
    against their own engine's documentation.
    """

    engine: str
    dialect: str
    plan: tuple[str, ...]
    cost: Cost | None
    findings: tuple[PlanFinding, ...]
    read_only_enforced: str

    def __post_init__(self) -> None:
        if not self.plan:
            raise EngineError(
                "a query plan with no plan in it is not a plan. An engine that answered nothing "
                "is a refusal, and a refusal is QueryPlanRefused rather than an empty result."
            )
        if not self.read_only_enforced.strip():
            raise EngineError(
                "a plan has to say how the engine was stopped from writing. This library has no "
                "SQL parser, so 'we checked the query' is not available to it - what is available "
                "is what the engine was told, and that is the thing worth recording."
            )

    def as_record(self) -> dict[str, Any]:
        return {
            "engine": self.engine,
            "dialect": self.dialect,
            "plan": list(self.plan),
            "cost": None if self.cost is None else self.cost.as_record(),
            "findings": [finding.as_record() for finding in self.findings],
            "read_only_enforced": self.read_only_enforced,
        }

    def for_a_human(self) -> str:
        """The findings before the plan, and the plan before the numbers.

        Same ordering rule the issued query follows: the caveat goes above the thing it is about,
        because a caveat printed under forty lines of plan is a caveat nobody read.
        """
        lines = [f"Planned by {self.engine} ({self.dialect}). Not executed."]
        lines.append(f"  Write protection: {self.read_only_enforced}")
        if self.findings:
            lines.append("")
            lines.append("Worth reading before you run it:")
            for finding in self.findings:
                lines.append(f"  [{finding.kind}] {finding.detail}")
        if self.cost is not None:
            lines.append("")
            lines.append(f"Estimate, in {self.cost.units}:")
            for name in sorted(self.cost.values):
                lines.append(f"  {name}: {self.cost.values[name]}")
            lines.append(f"  What this number is: {self.cost.basis}")
        else:
            lines.append("")
            lines.append(
                "No estimate: this engine gave a plan and no figure. Reported as absent rather "
                "than as zero - a query with no cost estimate and a free query are not the same "
                "thing."
            )
        lines.append("")
        lines.append("Plan, as the engine rendered it:")
        lines.extend(f"  {line}" for line in self.plan)
        return "\n".join(lines)


@runtime_checkable
class Explains(Protocol):
    """An adapter that can ask its engine to plan a query without running it.

    A separate protocol rather than a method on :class:`~sde.session.Engine`, and that is a
    compatibility decision. ``Engine`` is what a third party implements to plug their own database
    in; growing it would break every such adapter on upgrade, for a capability a session never
    uses. So this is asked for by name, and an adapter without it gets a refusal that says which
    of the two reasons applies.

    ``runtime_checkable`` here checks only that the attribute exists. Worth remembering why that is
    not enough on its own: ``isinstance`` against a runtime-checkable protocol resolves members
    through ``hasattr`` up to Python 3.11 and through ``inspect.getattr_static`` from 3.12, and the
    second ignores ``__getattr__`` - so a proxy that forwards calls passes on one interpreter and
    fails on another. :func:`explain` therefore looks the attribute up directly.
    """

    dialect: str

    def explain_plan(self, sql: str) -> QueryPlan: ...


def explain(engine: object, sql: str) -> QueryPlan:
    """Ask a live engine to plan this query without running it. Requirement 19.4.

    Takes an adapter rather than a :class:`~sde.session.Session`, because an analyst's query is not
    an operation shape and there is nothing for a session to route. The engine to use is in the
    query the control plane issued - it names the materialisation and its staleness - so the caller
    already knows which adapter to hand over.

    Looked up with ``getattr`` rather than ``isinstance``: see :class:`Explains` for the version
    difference that makes the second one answer differently on 3.11 and 3.12.
    """
    if not sql.strip():
        raise EngineError("there is no query here to plan")
    method = getattr(engine, "explain_plan", None)
    if method is None or not callable(method):
        raise EngineError(
            f"{type(engine).__name__} cannot plan a query. That is one of two different things and "
            f"the difference matters: either this engine has no query planner to ask - the "
            f"orderbook engine has a fixed access path and nothing to choose between - or it has "
            f"one and this adapter does not expose it yet. Neither is a reason to run the query "
            f"instead: requirement 19.2 keeps this product out of the data path for analytics, and "
            f"'we could not check it, so we ran it' is the exact opposite of a validation."
        )
    plan: QueryPlan = method(sql)
    return plan


def postgres_findings(node: Mapping[str, Any]) -> tuple[PlanFinding, ...]:
    """Shape facts from a PostgreSQL JSON plan. Walks the whole tree, not just the root.

    One finding, and the restraint is deliberate. A sequential scan carrying a filter means the
    engine reads every row to throw most of them away, which is true at any size and is the case
    an index addresses - the same reasoning that makes ``latency`` drift point at an index rather
    than at an engine. Everything else the plan offers is either a number in arbitrary units or
    needs ``ANALYZE``, which would execute the query.
    """
    found: list[PlanFinding] = []

    def walk(current: Mapping[str, Any]) -> None:
        if str(current.get("Node Type")) == "Seq Scan" and current.get("Filter"):
            relation = current.get("Relation Name", "a table")
            found.append(
                PlanFinding(
                    kind="full_scan_under_filter",
                    detail=(
                        f"{relation} is read in full and then filtered on "
                        f"{current.get('Filter')}, so every row is fetched to discard most of "
                        f"them. The engine estimates {current.get('Plan Rows')} row(s) will "
                        f"survive. An index on the filtered column is what changes this; ask for "
                        f"one rather than adding it, because a physical schema is ours to own."
                    ),
                )
            )
        for child in current.get("Plans") or ():
            if isinstance(child, dict):
                walk(child)

    walk(node)
    return tuple(found)


def replacing_merge_tree_finding(tables: Sequence[tuple[str, str]]) -> tuple[PlanFinding, ...]:
    """The measured hazard from 9.7, now sayable from a live engine rather than only in prose.

    A ``ReplacingMergeTree`` keeps both rows written under one key until a merge collapses them, so
    a read without ``FINAL`` counts the row twice. Measured in this product with merges stopped:
    two rows against one. The library's own reads use ``FINAL``; a query an analyst runs by hand
    does not unless it says so.

    **This is a property of the table, not a defect in the query, and it is phrased that way** -
    because this library has no SQL parser and therefore cannot tell whether the query says
    ``FINAL``. Guessing with a substring search would be worse than not looking: an alias called
    ``final`` would suppress a real warning, which is a check that fails open. So the finding
    states the fact and "I already wrote FINAL" is a satisfying answer to it.
    """
    return tuple(
        PlanFinding(
            kind="replacing_merge_tree",
            detail=(
                f"{table} is a {kind}: two rows written under one key both stay until a merge "
                f"collapses them, and a read without FINAL counts both. Measured here with merges "
                f"stopped: two rows against one. This library's own reads use FINAL. Whether "
                f"yours does is something this check cannot see - there is no SQL parser here, so "
                f"this is a fact about the table rather than a claim about your query."
            ),
        )
        for table, kind in tables
        if kind.startswith("Replacing")
    )

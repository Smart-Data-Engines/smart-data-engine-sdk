"""Requirement 6.4 and task 4.10: a retry only where the operation is known to be idempotent, and
only against the same engine.

The task states its own test - "a non-idempotent operation is not retried even once" - and this file
is that, plus the two halves it implies. A write that failed must not be attempted again, and it
must not become a write somewhere *else*, which is the more interesting half: the library holds a
map with more than one materialisation per group during a migration, so "somewhere else" is a real
address rather than a hypothetical one.

There is a third half that a behavioural test cannot reach, and it is the reason this file also
reads the source. A retry only shows up in a test that fails the right call the right number of
times, so the absence of a retry on the paths a fixture happens to touch says nothing about the
paths it does not. The static half asks the question the fixtures cannot: does any `except` handler
in this library call an engine method again? Its method set is derived from the `Engine` protocol's
own annotations rather than from a list written here, because a list written here would be the
thing that goes stale.

`docs/failure-semantics.md` states this rule to clients, so it is now load-bearing for a public
document rather than only for a requirement.
"""

from __future__ import annotations

import ast
import uuid
from pathlib import Path
from typing import Any

import pytest

import sde

SOURCE = Path(sde.__file__).parent


@pytest.fixture(autouse=True)
def _isolate() -> None:
    sde.clear_registry()


class Counting:
    """An engine that counts what it was asked to do, and can be told to refuse.

    Not a mock of a database: a mock would answer questions about a database, and the question here
    is about this library. What it records is attempts, because "not retried" is a claim about a
    number of attempts and nothing else.
    """

    dialect = "postgres"

    def __init__(self, *, fail: str | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.rows: dict[Any, dict[str, Any]] = {}
        self._fail = fail

    def ensure_schema(self, layout: Any, *, keys: Any) -> None:
        self.calls.append(("ensure_schema", ""))

    def insert(self, table: str, values: Any) -> None:
        self.calls.append(("insert", table))
        if self._fail == "insert":
            raise sde.EngineError("the engine refused this row")
        self.rows[tuple(sorted(values.items()))] = dict(values)

    def get(self, table: str, key: Any) -> dict[str, Any] | None:
        self.calls.append(("get", table))
        if self._fail == "get":
            raise sde.EngineError("the engine refused this read")
        return None

    def attempts(self, what: str) -> int:
        return sum(1 for call, _ in self.calls if call == what)


def _model() -> sde.LogicalModel:
    @sde.entity
    class Reading:
        id: uuid.UUID
        sensor: str

    return sde.build_model(Reading)


def _map(model: sde.LogicalModel, *, also_write: bool = False) -> sde.PlacementMap:
    group = sde.colocation_groups(model)[0].name
    placement: dict[str, Any] = {
        "source": {"id": "r@pg", "engine": "pg", "layout": {"auto": True}},
    }
    if also_write:
        placement["derived"] = [
            {
                "id": "r@ch",
                "engine": "ch",
                "lag_budget_ms": 30_000,
                "layout": {"tables": {"Reading": "reading"}, "columns": {}},
            }
        ]
        placement["also_write"] = ["r@ch"]
    raw = {
        "contract": sde.MAP_CONTRACT if also_write else sde.CONTRACT,
        "model_version": model.version,
        "map_version": 1,
        "groups": {group: placement},
    }
    return sde.load_map(raw, model=model)


def test_a_write_the_engine_refused_is_attempted_exactly_once() -> None:
    """The task's own test. Once, not twice, and not zero times either - a library that swallowed
    the operation would also pass an assertion that only counted retries."""
    model = _model()
    engine = Counting(fail="insert")
    session = sde.Session(model=model, placement=_map(model), engines={"pg": engine})

    with pytest.raises(sde.EngineError, match="refused this row"):
        session.save("Reading", {"id": uuid.uuid4(), "sensor": "s"})

    assert engine.attempts("insert") == 1, engine.calls


def test_a_read_the_engine_refused_is_attempted_exactly_once() -> None:
    """A read *is* idempotent, so requirement 6.4 would permit retrying it. This library does not,
    and that is worth pinning rather than leaving to be discovered: a client sizing a timeout needs
    to know whether one call can become three."""
    model = _model()
    engine = Counting(fail="get")
    session = sde.Session(model=model, placement=_map(model), engines={"pg": engine})

    with pytest.raises(sde.EngineError, match="refused this read"):
        session.get("Reading", {"id": uuid.uuid4()})

    assert engine.attempts("get") == 1, engine.calls


def test_a_failed_write_does_not_become_a_write_to_another_engine() -> None:
    """"Only within the same engine", in the one configuration where another engine is reachable.

    During a migration the map names a fan-out target, so a library that treated a failed write as
    something to place elsewhere has an address to place it at. The copy must see nothing: the
    source is authoritative, and a row that exists only in the copy is a row the client was told
    failed.
    """
    model = _model()
    source = Counting(fail="insert")
    copy = Counting()
    session = sde.Session(
        model=model, placement=_map(model, also_write=True), engines={"pg": source, "ch": copy}
    )

    with pytest.raises(sde.EngineError, match="refused this row"):
        session.save("Reading", {"id": uuid.uuid4(), "sensor": "s"})

    assert source.attempts("insert") == 1
    assert copy.calls == [], "the copy was written after the source refused the row"


def test_a_write_that_succeeded_reaches_the_copy_exactly_once() -> None:
    """The other half, and it is what makes the test above evidence rather than an accident: the
    fan-out has to happen when the source accepts, or the assertion above would also pass against a
    library that never fans out at all."""
    model = _model()
    source = Counting()
    copy = Counting()
    session = sde.Session(
        model=model, placement=_map(model, also_write=True), engines={"pg": source, "ch": copy}
    )

    session.save("Reading", {"id": uuid.uuid4(), "sensor": "s"})

    assert source.attempts("insert") == 1
    assert copy.attempts("insert") == 1


def test_a_copy_that_refuses_does_not_make_the_callers_write_fail_or_repeat() -> None:
    """The fan-out swallows its own failure by design (requirement 9.1). What it must not do is
    retry: a copy that refuses one row and accepts it on the second attempt would leave the two
    engines agreeing for the wrong reason, and the migration's verification is what is supposed to
    find the gap."""
    model = _model()
    source = Counting()
    copy = Counting(fail="insert")
    recorder = sde.Recorder(model.version)
    session = sde.Session(
        model=model,
        placement=_map(model, also_write=True),
        engines={"pg": source, "ch": copy},
        recorder=recorder,
    )

    session.save("Reading", {"id": uuid.uuid4(), "sensor": "s"})

    assert source.attempts("insert") == 1
    assert copy.attempts("insert") == 1, "the fan-out tried the copy more than once"

    # And it is recorded. A swallowed failure that is not counted is a lost failure, and the
    # counter it lands in is deliberately not `internal_failures()`: that one is for this
    # library's own problems, while a copy that refused a row is a fact about the client's
    # migration and belongs where the copy-freshness report can read it.
    window = recorder.roll()
    assert window.fanned, "the fan-out was not recorded at all"
    copies = window.copies(sde.colocation_groups(model)[0].name)
    assert [(copy.writes, copy.failures) for copy in copies] == [(1, 1)], copies


# --- the half a fixture cannot reach -----------------------------------------------------------


def _engine_facing() -> list[Path]:
    """The modules that can hold an engine, which is where a retry could be written.

    Narrowed by *reading* rather than by choosing: a module that never mentions an engine protocol
    has no engine to call twice. That matters because the method names collide with builtins -
    `dict.get` and `list.insert` both exist - so a rule over names alone reported `internal.py`
    retrying an engine while it was incrementing a counter.

    The first fix for that was to drop the colliding names from the method set, and it was worse
    than the false positive it removed: `get` and `insert` are the two methods a retry would
    actually use, so the rule went on asserting that it checked something while checking
    `ensure_schema` and `transaction`. A mutation planted in the write path survived it. Hence this
    direction instead - the full method set over fewer files - and the limitation is
    self-correcting, because a module that starts holding an engine starts mentioning a protocol.
    """
    facing = [
        path for path in sorted(SOURCE.rglob("*.py")) if "Protocol" in path.read_text("utf-8")
    ]
    assert len(facing) >= 4, facing
    return facing


def _engine_methods() -> frozenset[str]:
    """Every method any engine protocol in this library declares, read out of the source.

    Four protocols describe an engine here and each covers a different slice: `Engine` is what a
    session needs, `Migratable` what a backfill needs, `WatermarkStore` the bookkeeping and
    `Explains` a query plan. Deriving from one would leave the others unchecked, and it did: the
    first version read only `Engine`, so a retry of `key_range()` in the migration path would have
    passed - and `key_range` is the call a resumable backfill is built out of.

    Derived rather than listed for the reason this project has paid for twice: a hand-written flag
    list in the engine, and a hand-written key-argument list here that produced eighteen false
    positives and missed the real one.
    """
    names: set[str] = set()
    for path in _engine_facing():
        tree = ast.parse(path.read_text("utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            if not any(
                isinstance(base, ast.Name) and base.id == "Protocol" for base in node.bases
            ):
                continue
            names |= {
                item.name
                for item in node.body
                if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef)
                and not item.name.startswith("_")
            }
    assert len(names) >= 6, f"only {sorted(names)} - the protocols moved"
    return frozenset(names)


def test_no_exception_handler_in_this_library_calls_an_engine_again() -> None:
    """A retry is an engine call inside an `except`. There is no other shape it can take.

    This is the assertion a behavioural test cannot make, because a retry only appears when the
    right call fails the right number of times, and no fixture reaches every call site. Catching an
    engine failure is fine and happens deliberately in two places - the fan-out counts it, the
    telemetry drops it - so what is refused is narrower than a catch: calling the engine *again*
    from the handler.
    """
    methods = _engine_methods()
    assert {"insert", "get"} <= methods, "the two methods a retry would use are not being checked"
    offenders: list[str] = []
    for path in _engine_facing():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            for handler in node.handlers:
                for inner in ast.walk(handler):
                    if (
                        isinstance(inner, ast.Call)
                        and isinstance(inner.func, ast.Attribute)
                        and inner.func.attr in methods
                    ):
                        offenders.append(
                            f"{path.relative_to(SOURCE)}:{inner.lineno} calls {inner.func.attr}()"
                        )
    assert offenders == [], (
        "an exception handler calls an engine method again, which is a retry: "
        f"{offenders}. Requirement 6.4 allows one only for an operation known to be idempotent, "
        "and the caller cannot know it happened."
    )


def test_the_rule_this_file_pins_is_the_one_the_client_is_told() -> None:
    """The public document states this rule. If the rule moves, the page has to move with it, and
    a page that describes a library's behaviour from memory is the failure this pairing prevents."""
    page = (SOURCE.parents[2] / "docs" / "failure-semantics.md").read_text(encoding="utf-8")
    flat = " ".join(page.split())
    assert "only where the operation is known to be idempotent, and only against the same" in flat
    assert "No retry of a write" in flat

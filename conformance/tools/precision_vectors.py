"""Vectors for the rule that refuses a fan-out which would silently change values.

The rule itself is `sde.precision_refusal`, and until 7 September 2026 it had one caller: `backfill`
refused a copy that would truncate, and the write fan-out in `Session` did exactly that truncation
one phase earlier, on every write. Measured against live servers before it was fixed: a
`timestamptz` written as `09:30:15.123456` came back unchanged from PostgreSQL and as
`09:30:15.123` from ClickHouse, with no error on either side.

It was fixed in both languages the same day, and held by a test in each - which is the state this
suite exists to refuse. A rule enforced in one runtime and not the other is one map with two
meanings, and per-language tests are exactly how that state looks from the inside: green, twice.

**The stage is new and that is the point.** `errors/` knew `model` and `map`; this rule can be
answered at neither, because a placement map names engines by name and deliberately carries no
dialect (§7). The earliest door that can answer is the one where the adapters are in hand, so the
family gains a `session` stage in both runners.

Three cases, and the two positives are what make the refusal a proof rather than a mood:

- `errors/038` - PostgreSQL to ClickHouse, refused, and **zero calls**. A map that can never work
  must not create a table or issue a query first. That is the same promise `migration/001` pins for
  the no-account mode, and it broke there once in exactly this way: the right answer, arrived at
  after the work.
- `migration/020` - the same shape between two engines of one dialect, which opens and fans out. A
  library that refused every map carrying a timestamp would pass the case above.
- `migration/021` - ClickHouse to PostgreSQL, which opens. The rule is about *losing* digits, not
  about the two dialects differing, and a library that refused any mismatch would pass both of the
  others.

    python conformance/tools/precision_vectors.py --i-am-changing-the-contract
"""

from __future__ import annotations

import argparse
import base64
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "python" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import sde  # noqa: E402
from migration_vectors import sign  # noqa: E402
from sde.errors import MigrationRefused  # noqa: E402
from sde.testing.loader import model_from_neutral  # noqa: E402
from sde.testing.memory import engines_from as _engines  # noqa: E402

VECTORS = ROOT / "conformance" / "vectors"

AT = "2026-11-09T09:30:15.123456+00:00"
"""The value the defect was measured with, kept as the value the vectors write.

A string rather than an instant because a vector is a document, and the engine under it is the
in-memory one, which stores what it is handed. What the case is about is whether the session opens
at all - the truncation itself is a property of a real server and is measured in the live tests.
"""


def _model() -> sde.LogicalModel:
    """One entity carrying a `timestamptz`, which is what any table with a time column declares.

    Deliberately ordinary. The defect this family is about does not need an exotic type: it needs
    the type almost every table has, which is why it reached live servers.
    """
    sde.clear_registry()
    return model_from_neutral(
        {
            "entities": [
                {
                    "name": "Reading",
                    "fields": [
                        {"name": "at", "type": "timestamptz"},
                        {"name": "celsius", "type": "int32"},
                        {"name": "id", "type": "int64"},
                    ],
                    "key": ["id"],
                }
            ],
            "relations": [],
            "atomic": [],
        }
    )


PG_LAYOUT = {
    "tables": {"Reading": "reading"},
    "columns": {"Reading": {"at": "timestamptz", "celsius": "integer", "id": "bigint"}},
}
CH_LAYOUT = {
    "tables": {"Reading": "reading"},
    "columns": {"Reading": {"at": "DateTime64(3)", "celsius": "Int32", "id": "Int64"}},
}
"""Written with each dialect's real spelling rather than one set of types twice.

The layout is not what the rule reads - it reads the neutral types from the model - but a vector
whose ClickHouse copy claimed `timestamptz` columns would be a document nobody could deploy, and a
reader checking our honesty would notice that before they noticed the rule.
"""


def _map(*, source_engine: str, source_layout: Any, copy_engine: str, copy_layout: Any) -> dict:
    return {
        "contract": sde.MAP_CONTRACT,
        "model_version": _model().version,
        "map_version": 1,
        "groups": {
            "Reading": {
                "source": {"id": "Reading@src", "engine": source_engine, "layout": source_layout},
                "derived": [
                    {
                        "id": "Reading@copy",
                        "engine": copy_engine,
                        "layout": copy_layout,
                        "lag_budget_ms": 30000,
                    }
                ],
                "also_write": ["Reading@copy"],
            }
        },
    }


def _write(family: str, name: str, files: dict[str, Any]) -> None:
    out = VECTORS / family / name
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    for filename, body in files.items():
        (out / filename).write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
    print(f"  wrote {family}/{name}")


def _refusal_case() -> None:
    model = _model()
    unsigned = _map(
        source_engine="pg-main",
        source_layout=PG_LAYOUT,
        copy_engine="other-1",
        copy_layout=CH_LAYOUT,
    )
    # **Signed, and that is what makes the empty call list a constraint rather than a decoration.**
    # The forward-only check does nothing at all for an unsigned map - the no-account mode promises
    # no table and no query - so against one, "no calls before the refusal" is satisfied by a
    # library that runs the check first and would stay satisfied by every mutation of the ordering.
    # A signed map is the case where that check costs two `map_watermark` calls, so the empty list
    # says something: refuse before you spend them.
    with tempfile.TemporaryDirectory() as home:
        document, public_key = sign(unsigned, Path(home))
    spec = {"other-1": {"dialect": "mysql"}, "pg-main": {"dialect": "postgres"}}
    placement = sde.load_map(
        document, model=model, public_key=base64.b64decode(public_key),
        require_signature=True,
    )
    engines = _engines(spec)
    try:
        sde.Session(model, placement, engines)  # type: ignore[arg-type]
    except MigrationRefused as refused:
        message = str(refused)
    else:  # pragma: no cover - the generator refuses to write a case that does not refuse
        raise SystemExit("the reference implementation accepted the map this case is about")

    calls = next(iter(engines.values())).recorded.as_list()
    if calls:
        raise SystemExit(f"the refusal cost {len(calls)} engine calls; the case asserts none")

    _write(
        "errors",
        "038-a-fan-out-into-a-dialect-we-have-no-precision-facts-for",
        {
            "model.json": sde.neutral_declaration(model),
            "map.json": document,
            "engines.json": spec,
            "calls.json": calls,
            "expected.json": {
                "error": "MigrationRefused",
                "stage": "session",
                "match": "does not know whether postgres and mysql store it to the same",
                "load": {"require_signature": True, "public_key": public_key},
                "why": (
                    "**No two dialects this library ships truncate each other any more**, and "
                    "that is why this case reads the way it does. Until 7 September 2026 it was "
                    "PostgreSQL to ClickHouse with a `timestamptz`: measured on live servers, one "
                    "row written once came back `09:30:15.123456` from the first and "
                    "`09:30:15.123` from the second, silently, on every write a fan-out made. The "
                    "fix was not to go on refusing it. It was to stop rendering a ClickHouse "
                    "timestamp to three digits - a choice originally made against a comparison "
                    "with plain `DateTime`, which is seconds, rather than with the engine "
                    "standing beside it in the same product. Both keep six now, the refusal has "
                    "no reachable pair among shipped dialects, and a case built on it would have "
                    "become one that cannot fail. "
                    "What is left is the branch that will actually fire in the field: a dialect "
                    "this library holds no precision facts about, which is what the fourth engine "
                    "adapter anybody writes will be on its first day. A type nobody classified is "
                    "a type nobody checked, and guessing returns a value changed with no error "
                    "anywhere. "
                    "The stage is `session` and it is forced there: a map names engines by name "
                    "and carries no dialect, so neither `model` nor `map` can answer this, and "
                    "the earliest door that can is the one holding the adapters. `calls.json` is "
                    "empty and is the load-bearing half: a library that gathered the watermarks "
                    "first and refused afterwards would give this same answer with the map's cost "
                    "already paid, which is how the no-account promise broke in `migration/001`. "
                    "The map is **signed** for exactly that reason - the forward-only check does "
                    "nothing at all against an unsigned one, so an unsigned case would satisfy "
                    "the empty list however the two were ordered."
                ),
            },

        },
    )
    print(f"    refusal: {message[:90]}...")


def _accepting_case(name: str, *, source: str, copy: str, why: str) -> None:
    """A map of the same shape that opens, and a row that reaches both engines.

    Both positives drive a write rather than stopping at "the session opened". Opening is what the
    refusal is about, but a session that opens and then does not fan out would satisfy an assertion
    on the constructor and lose exactly the rows this rule protects.
    """
    dialects = {"pg": "postgres", "ch": "clickhouse"}
    model = _model()
    document = _map(
        source_engine=f"{source}-src",
        source_layout=PG_LAYOUT if source == "pg" else CH_LAYOUT,
        copy_engine=f"{copy}-copy",
        copy_layout=PG_LAYOUT if copy == "pg" else CH_LAYOUT,
    )
    spec = {
        f"{copy}-copy": {"dialect": dialects[copy]},
        f"{source}-src": {"dialect": dialects[source]},
    }
    placement = sde.load_map(document, model=model)
    engines = _engines(spec)
    recorder = sde.Recorder(model.version)
    session = sde.Session(model, placement, engines, recorder=recorder)  # type: ignore[arg-type]
    operations = [{"op": "save", "entity": "Reading", "values": {"id": 1, "at": AT, "celsius": 20}}]
    for step in operations:
        session.save(step["entity"], step["values"])

    window = recorder.roll()
    copies = [
        {**record.as_record(), "lag_p50_ms": "<any>", "lag_p99_ms": "<any>"}
        for record in (() if window is None else window.copies("Reading"))
    ]
    _write(
        "migration",
        name,
        {
            "model.json": sde.neutral_declaration(model),
            "map.json": document,
            "engines.json": spec,
            "operations.json": operations,
            "tables.json": {
                engine: {
                    table: sorted(
                        (dict(row) for row in rows),
                        key=lambda row: json.dumps(row, sort_keys=True),
                    )
                    for table, rows in sorted(built.tables.items())
                }
                for engine, built in sorted(engines.items())
            },
            "copies.json": copies,
            "calls.json": next(iter(engines.values())).recorded.as_list(),
            "why.json": {"why": why},
        },
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--i-am-changing-the-contract", action="store_true")
    args = parser.parse_args()
    if not args.i_am_changing_the_contract:
        print(__doc__)
        print("refusing to run without --i-am-changing-the-contract")
        return 2

    _refusal_case()
    _accepting_case(
        "020-a-timestamp-fans-out-between-engines-of-one-dialect",
        source="pg",
        copy="pg",
        why=(
            "The half that makes `errors/038` a proof rather than a mood. Same model, same map "
            "shape, same `timestamptz` - and both engines keep six sub-second digits, so nothing "
            "is lost and the session opens. A library that refused every map carrying a timestamp "
            "would satisfy the refusal case and fail this one, and a client whose two PostgreSQL "
            "instances are mid-migration is the ordinary use of `also_write`."
        ),
    )
    _accepting_case(
        "021-the-central-shape-of-this-product-opens",
        source="pg",
        copy="ch",
        why=(
            "A transactional group in PostgreSQL with an analytical copy in ClickHouse, fanned to "
            "on every write, with a timestamp in it. That is the shape this product is sold on, "
            "and until 7 September 2026 it was **not expressible**: we rendered the ClickHouse "
            "timestamp to three sub-second digits against PostgreSQL's six, so the copy held "
            "different values from the source and the library correctly refused to open the "
            "session. An order without a time column is not an order, so the refusal was not the "
            "thing to keep - the rendering was the thing to fix. This case exists so that nobody "
            "can quietly render three again: it opens, and both engines receive the row."
        ),
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

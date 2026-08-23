"""Generate conformance vectors from the Python reference implementation.

Read this before running it.

A vector is frozen once committed. Regenerating one is changing the contract, and a contract change
means bumping ``contract-version.txt`` and every library declaring which version it implements. So
this script refuses to run without ``--i-am-changing-the-contract``, and even then it will not touch
``model/001-single-entity``, whose expected values were written by hand from the format document and
are the only independent check that the document is sufficient to implement from.

The honest limitation of everything else in here: it was produced by the implementation it is meant
to verify, so a bug in the reference would have been frozen in. That is why 001 exists, and why new
vectors should be added for the cases a bug taught you rather than the cases that were easy.

    python conformance/tools/generate.py --i-am-changing-the-contract
"""

from __future__ import annotations

import argparse
import datetime as dt
import decimal
import io
import json
import shutil
import sys
import uuid
from pathlib import Path
from typing import Annotated, Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "python" / "src"))

import sde  # noqa: E402
from sde.canonical import canonical_bytes  # noqa: E402

VECTORS = ROOT / "conformance" / "vectors"
HAND_WRITTEN = {"model/001-single-entity"}


def _neutral(model: sde.LogicalModel) -> dict[str, Any]:
    """The neutral declaration for a model, i.e. what a vector's ``model.json`` holds.

    Keys are written as a plain list here, not in the positioned form the IR uses. That asymmetry is
    deliberate: if the vector carried the positioned form, a library could pass by copying it.
    """
    return {
        "entities": [
            {
                "name": e.name,
                "fields": [
                    {"name": f.name, "type": f.type, **({"nullable": True} if f.nullable else {})}
                    for f in e.fields
                ],
                "key": list(e.key),
                **({"pii": list(e.pii)} if e.pii else {}),
                **({"residency": e.residency} if e.residency else {}),
            }
            for e in model.entities
        ],
        "relations": [
            {"name": r.name, "from": r.source, "to": r.target} for r in model.relations
        ],
        **({"atomic": [list(g) for g in model.atomic]} if model.atomic else {}),
        **({"cost_ceiling": dict(model.cost_ceiling)} if model.cost_ceiling else {}),
    }


def _write_model_vector(name: str, model: sde.LogicalModel) -> None:
    out = VECTORS / "model" / name
    if f"model/{name}" in HAND_WRITTEN:
        print(f"  skipping {name}: hand-written")
        return
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    (out / "model.json").write_text(json.dumps(_neutral(model), indent=2) + "\n", encoding="utf-8")
    (out / "ir.json").write_bytes(canonical_bytes(model.ir))
    (out / "version.txt").write_text(model.version + "\n", encoding="utf-8")
    (out / "groups.json").write_text(
        json.dumps(
            [{"name": g.name, "members": list(g.members)} for g in sde.colocation_groups(model)],
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (out / "shapes.json").write_text(
        json.dumps(
            [{**s.as_ir(), "id": s.id} for s in sde.enumerate_shapes(model)],
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"  wrote model/{name}")


def _shop() -> sde.LogicalModel:
    sde.clear_registry()

    @sde.entity
    class User:
        id: uuid.UUID
        email: str

        class Meta:
            pii = ["email"]

    @sde.entity
    class Order:
        tenant: uuid.UUID
        id: uuid.UUID
        user: sde.Ref[User]
        total: Annotated[decimal.Decimal, sde.precision(12, 2)]
        placed_at: dt.datetime

        class Meta:
            key = ["tenant", "id"]
            residency = "EU"

    @sde.entity
    class Payment:
        id: uuid.UUID
        amount: Annotated[decimal.Decimal, sde.precision(12, 2)]

        class Meta:
            atomic_with = ["Order"]

    @sde.entity
    class Event:
        id: uuid.UUID
        name: str
        at: dt.datetime

    return sde.build_model(
        User, Order, Payment, Event, cost_ceiling={"amount": "750.00", "currency": "EUR"}
    )


def _everything() -> sde.LogicalModel:
    sde.clear_registry()

    @sde.entity
    class Wszystko:
        # A non-ASCII identifier on purpose. Field names reach the IR, so normalisation and code
        # point ordering have to be exercised by something other than a unit test in one language.
        id: uuid.UUID
        zażółć: str
        flag: bool
        small: sde.Int32
        big: int
        approx: float
        narrow: sde.Float32
        money: Annotated[decimal.Decimal, sde.precision(19, 4)]
        blob: bytes
        day: dt.date
        moment: dt.datetime
        naive: sde.Timestamp
        doc: sde.Json
        maybe: str | None

    return sde.build_model(Wszystko)


def _astral() -> sde.LogicalModel:
    """A model whose field names expose UTF-16 order against code point order.

    This vector exists because the TypeScript implementation found the divergence. JavaScript compares
    strings by UTF-16 code unit; the contract requires code point order. For anything in the Basic
    Multilingual Plane the two agree, so no test written with Latin or even CJK names can see the
    difference. Above U+FFFF they disagree: an astral character is a surrogate pair starting at
    0xD800, so UTF-16 order puts every emoji *before* U+E000 while code point order puts it after.

    Expected order of these three field names: "a", U+E000, U+1F600. A library that used a naive
    sort would produce "a", U+1F600, U+E000, hash differently from every other library, and nothing
    would fail until the control plane was looking at two models where there should be one.
    """
    sde.clear_registry()

    @sde.entity
    class Astral:
        id: uuid.UUID
        a: str
        # U+E000, private use area, inside the BMP
        locals()["\ue000"] = str
        # U+1F600, astral: stored as a surrogate pair in UTF-16
        locals()["\U0001f600"] = str

    # Annotations added after the fact, because the names are not valid Python identifiers.
    Astral.__annotations__["\ue000"] = str
    Astral.__annotations__["\U0001f600"] = str
    return sde.build_model(Astral)


def _routing_vector() -> None:
    model = _shop()
    shapes = sde.enumerate_shapes(model)
    by_kind = {(s.entity, s.kind): s for s in shapes}

    layout = {"tables": {e.name: e.name.lower() for e in model.entities}}
    raw: dict[str, Any] = {
        "contract": sde.CONTRACT,
        "model_version": model.version,
        "map_version": 1,
        "groups": {
            g.name: {
                "source": {"id": f"{g.name}@pg", "engine": "pg-main", "layout": layout},
                **(
                    {
                        "derived": [
                            {
                                "id": f"{g.name}@ch",
                                "engine": "ch-1",
                                "layout": layout,
                                "lag_budget_ms": 30_000,
                            }
                        ]
                    }
                    if g.name == "Event"
                    else {}
                ),
            }
            for g in sde.colocation_groups(model)
        },
        "routing": {
            by_kind[("Event", "full_scan")].id: "Event@ch",
            by_kind[("Event", "aggregate")].id: "Event@ch",
            by_kind[("Event", "point_read")].id: "Event@ch",
        },
    }

    cases = [
        # A routed read goes where the planner said.
        {"shape": by_kind[("Event", "full_scan")].id, "expect": "Event@ch"},
        # A write never does.
        {"shape": by_kind[("Event", "write")].id, "expect": "Event@pg"},
        # Freshness and write transactions override the table, because a derived copy is behind.
        {"shape": by_kind[("Event", "point_read")].id, "fresh": True, "expect": "Event@pg"},
        {
            "shape": by_kind[("Event", "point_read")].id,
            "in_write_transaction": True,
            "expect": "Event@pg",
        },
        # An unrouted shape falls back to the source rather than failing.
        {"shape": by_kind[("Order", "point_read")].id, "expect": "Order@pg"},
    ]

    out = VECTORS / "routing" / "001-derived-materialisation"
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    (out / "model.json").write_text(json.dumps(_neutral(model), indent=2) + "\n", encoding="utf-8")
    (out / "map.json").write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
    (out / "cases.json").write_text(json.dumps(cases, indent=2) + "\n", encoding="utf-8")
    print("  wrote routing/001-derived-materialisation")


def _error_vectors() -> None:
    cases = [
        (
            "001-decimal-without-precision",
            {
                "entities": [
                    {
                        "name": "Bad",
                        "fields": [
                            {"name": "id", "type": "uuid"},
                            {"name": "amount", "type": "decimal"},
                        ],
                    }
                ]
            },
            {
                "error": "DeclarationError",
                "stage": "model",
                "match": "well-formed decimal",
                "why": "a decimal without precision is not a storable type in any target engine",
            },
        ),
        (
            "002-unknown-type",
            {
                "entities": [
                    {
                        "name": "Bad",
                        "fields": [
                            {"name": "id", "type": "uuid"},
                            {"name": "thing", "type": "money"},
                        ],
                    }
                ]
            },
            {
                "error": "DeclarationError",
                "stage": "model",
                "match": "neutral type vocabulary",
                "why": "the vocabulary is closed; a name outside it must not be guessed at",
            },
        ),
        (
            "003-decimal-with-spaces",
            {
                "entities": [
                    {
                        "name": "Bad",
                        "fields": [
                            {"name": "id", "type": "uuid"},
                            {"name": "amount", "type": "decimal(12, 2)"},
                        ],
                    }
                ]
            },
            {
                "error": "DeclarationError",
                "stage": "model",
                "match": "well-formed decimal",
                "why": "whitespace inside a type name is exactly the kind of thing two libraries "
                "would disagree about, so the written form is fixed",
            },
        ),
        (
            "004-key-names-a-missing-field",
            {
                "entities": [
                    {
                        "name": "Bad",
                        "fields": [{"name": "id", "type": "uuid"}],
                        "key": ["tenant"],
                    }
                ]
            },
            {
                "error": "DeclarationError",
                "stage": "model",
                "match": "not fields",
                "why": "a key that is not stored in the entity cannot address, migrate or verify a row",
            },
        ),
        (
            "005-relation-to-unknown-entity",
            {
                "entities": [
                    {"name": "Order", "fields": [{"name": "id", "type": "uuid"}]}
                ],
                "relations": [{"name": "user", "from": "Order", "to": "User"}],
            },
            {
                "error": "DeclarationError",
                "stage": "model",
                "match": "unknown entity",
                "why": "a dangling relation would silently drop a colocation edge, and the group it "
                "should have merged with would be placed separately",
            },
        ),
    ]
    for name, model_json, expected in cases:
        out = VECTORS / "errors" / name
        if out.exists():
            shutil.rmtree(out)
        out.mkdir(parents=True)
        (out / "model.json").write_text(json.dumps(model_json, indent=2) + "\n", encoding="utf-8")
        (out / "expected.json").write_text(json.dumps(expected, indent=2) + "\n", encoding="utf-8")
        print(f"  wrote errors/{name}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--i-am-changing-the-contract", action="store_true")
    args = parser.parse_args()
    if not getattr(args, "i_am_changing_the_contract"):
        print(
            "Refusing to run. Vectors are frozen once committed; regenerating one is a contract\n"
            "change, which means bumping conformance/contract-version.txt and every library\n"
            "declaring the version it implements. Pass --i-am-changing-the-contract if that is\n"
            "genuinely what you are doing.",
            file=sys.stderr,
        )
        return 2

    _write_model_vector("001-single-entity", _shop())  # skipped: hand-written
    _write_model_vector("002-relations-keys-atomicity", _shop())
    _write_model_vector("003-type-vocabulary-and-unicode", _everything())
    _write_model_vector("004-astral-identifier", _astral())
    _routing_vector()
    _error_vectors()
    print("\nVectors written. Review the diff by hand before committing: this is the contract.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

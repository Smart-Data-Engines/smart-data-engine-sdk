"""Generate the vectors that came out of writing the third library, in C++.

A third implementation has to give a value a type before it can read it, so it met every place the
two references read one value two ways, or failed with their runtime's own error rather than the
contract's - `str(True)` is `"True"` in Python and `String(true)` is `"true"` in JavaScript, and a
materialisation id written as `true` named two different copies. Each of those is a rule now, with a
case here. So are the rules a mutation run showed no vector could see: removing each one left the
whole suite green. The list, with what each library did before, is in `docs/implementing.md`
("The third implementation: C++").

Every refusal is checked against the reference implementation before it is written: a case is
written only if the reference refuses it, with an error of the named class whose message contains
the `match` fragment. The fragments avoid quoting a value, because each library writes a value in
its own language's way. The one acceptance case is loaded and routed by the reference too.

This tool writes only the directories it names and never touches another vector. Rerunning it
rewrites exactly those, which is how a change to one of them is reviewed as a diff.

    python conformance/tools/third_implementation_vectors.py --i-am-changing-the-contract
"""

from __future__ import annotations

import argparse
import base64
import copy
import json
import shutil
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "python" / "src"))

import sde  # noqa: E402
from sde.errors import DeclarationError, MapError  # noqa: E402
from sde.testing.loader import model_from_neutral  # noqa: E402

VECTORS = ROOT / "conformance" / "vectors"
PROJECT = "7" * 32

# A key from the signature family, so a case that never reaches verification still carries a key a
# library can load. Any 32 bytes would do; these are already public.
PUBLIC_KEY = "eoAUc+0ZkRiFyx/zMmZYeTErCq1VJtKyyDoGxeyMKGw="
SIGNATURE = base64.b64encode(bytes(64)).decode()

# Two colocation groups: `Event` on its own, and `Line` with `Order`, joined by a relation, for the
# cases about the order entities of one layout are checked in.
MODEL: dict[str, Any] = {
    "entities": [
        {
            "name": "Event",
            "fields": [
                {"name": "at", "type": "timestamptz"},
                {"name": "id", "type": "int64"},
                {"name": "kind", "type": "string"},
            ],
            "key": ["id"],
        },
        {
            "name": "Line",
            "fields": [
                {"name": "line", "type": "int32"},
                {"name": "order_id", "type": "int64"},
                {"name": "qty", "type": "int64"},
            ],
            "key": ["order_id", "line"],
        },
        {
            "name": "Order",
            "fields": [
                {"name": "id", "type": "int64"},
                {"name": "placed", "type": "timestamptz"},
            ],
            "key": ["id"],
        },
    ],
    "relations": [{"name": "order", "from": "Line", "to": "Order"}],
}

EVENT_COLUMNS = {"at": "timestamptz", "id": "bigint", "kind": "text"}
LINE_TABLES = {"Line": "line", "Order": "order"}
LINE_COLUMNS = {
    "Line": {"line": "integer", "order_id": "bigint", "qty": "bigint"},
    "Order": {"id": "bigint", "placed": "timestamptz"},
}


def _model() -> sde.LogicalModel:
    return model_from_neutral(MODEL)


def _map(contract: int = 3, **event_source: Any) -> dict[str, Any]:
    """A valid unsigned map: each group on its own PostgreSQL source, `Event`'s overridable."""
    model = _model()
    epoch = {"write_epoch": 1} if contract >= 4 else {}
    source = {
        "id": "Event@pg",
        "engine": "pg-main",
        "layout": {"tables": {"Event": "event"}, "columns": {"Event": dict(EVENT_COLUMNS)}},
    }
    source.update(event_source)
    document: dict[str, Any] = {
        "contract": contract,
        "model_version": model.version,
        "map_version": 1,
        "groups": {
            "Event": {"source": source, **epoch},
            "Line": {
                "source": {
                    "id": "Line@pg",
                    "engine": "pg-main",
                    "layout": {"tables": dict(LINE_TABLES), "columns": copy.deepcopy(LINE_COLUMNS)},
                },
                **epoch,
            },
        },
    }
    if contract >= 4:
        document["project_id"] = PROJECT
    return document


def _with_copy(document: dict[str, Any], **copy_fields: Any) -> dict[str, Any]:
    """`Event` with a ClickHouse copy, `Event@ch`, whose fields the case may override."""
    derived = {
        "id": "Event@ch",
        "engine": "ch-1",
        "layout": {"tables": {"Event": "event_wide"}},
        "lag_budget_ms": 30000,
    }
    derived.update(copy_fields)
    document["groups"]["Event"]["derived"] = [derived]
    return document


def _line_layout(document: dict[str, Any], **design: Any) -> dict[str, Any]:
    document["groups"]["Line"]["source"]["layout"].update(design)
    return document


def _write(directory: Path, files: dict[str, Any]) -> None:
    if directory.exists():
        shutil.rmtree(directory)
    directory.mkdir(parents=True)
    for name, body in files.items():
        (directory / name).write_text(json.dumps(body, indent=2, ensure_ascii=True) + "\n")


def _model_refusal(name: str, model: dict[str, Any], match: str, why: str) -> str:
    try:
        model_from_neutral(model)
    except DeclarationError as exc:
        assert match in str(exc), (name, match, str(exc))
    else:
        raise AssertionError(f"{name}: the reference accepted the model")
    _write(
        VECTORS / "errors" / name,
        {
            "model.json": model,
            "expected.json": {
                "error": "DeclarationError",
                "match": match,
                "stage": "model",
                "why": why,
            },
        },
    )
    return "errors/" + name[:3]


def _map_refusal(
    name: str, document: dict[str, Any], match: str, why: str, *, signed: bool = False
) -> str:
    model = _model()
    load: dict[str, Any] = {"public_key": PUBLIC_KEY, "require_signature": True} if signed else {}
    try:
        sde.load_map(
            document,
            model=model,
            public_key=base64.b64decode(PUBLIC_KEY) if signed else None,
            require_signature=signed,
        )
    except MapError as exc:
        assert match in str(exc), (name, match, str(exc))
    else:
        raise AssertionError(f"{name}: the reference accepted the map")
    expected: dict[str, Any] = {"error": "MapError", "match": match, "stage": "map", "why": why}
    if load:
        expected["load"] = load
    _write(
        VECTORS / "errors" / name,
        {
            "model.json": sde.neutral_declaration(model),
            "map.json": document,
            "expected.json": expected,
        },
    )
    return "errors/" + name[:3]


def _entity(**overrides: Any) -> dict[str, Any]:
    entity: dict[str, Any] = copy.deepcopy(MODEL["entities"][0])
    entity.update(overrides)
    return entity


def model_vectors() -> list[str]:
    nullable = copy.deepcopy(MODEL)
    nullable["entities"][0]["fields"][2]["nullable"] = 1
    two_groups = copy.deepcopy(MODEL)
    return [
        _model_refusal(
            "083-a-nullable-that-is-not-a-boolean",
            nullable,
            "'nullable' is true or false",
            "Python read this as truthy and TypeScript as `=== true`, so `1` made a field "
            "optional in one library and required in the other: one declaration, two model "
            "versions, and a row one library stored and the other refused.",
        ),
        _model_refusal(
            "084-a-residency-that-is-not-a-name",
            {**two_groups, "entities": [_entity(residency=5), *two_groups["entities"][1:]]},
            "is a jurisdiction's name or null",
            "Residency is a hard placement constraint. A number reached the IR as a number, and a "
            "constraint nobody can name is one no placement can be checked against.",
        ),
        _model_refusal(
            "085-a-relation-without-a-target",
            {**two_groups, "relations": [{"name": "order", "from": "Line"}]},
            "each a string, not",
            "Python raised a bare KeyError out of its loader and TypeScript looked up `undefined` "
            "among the entity names: the same document, two runtime errors, neither the "
            "contract's. A relation is three strings.",
        ),
        _model_refusal(
            "086-atomic-groups-that-overlap",
            {**two_groups, "atomic": [["Event", "Line"], ["Line", "Order"]]},
            "atomic groups overlap on",
            "Section 4 merges atomicity transitively, and the loaders copied the groups as "
            "written, so these two groups reached the IR unmerged: another model_version than the "
            "same atomicity declared as one group, which is what neutral_declaration writes.",
        ),
        _model_refusal(
            "087-an-atomic-group-of-one",
            {**two_groups, "atomic": [["Event"]]},
            "names two or more distinct entities",
            "An entity is atomic with itself already. The group said nothing a placement could "
            "act on and still changed the model_version.",
        ),
        _model_refusal(
            "088-a-cost-ceiling-with-a-number",
            {**two_groups, "cost_ceiling": {"amount": 500, "currency": "EUR"}},
            "exactly those two keys, with the amount as a string",
            "Money is a decimal and the canonical encoding has no floating point, so the amount is "
            "a string. A number was refused much later, by the encoder, about a path the client "
            "never wrote - or not at all, by the loader that checked nothing.",
        ),
    ]


def map_vectors() -> list[str]:
    model = _model()
    writes = [s for s in sde.enumerate_shapes(model) if s.group == "Event" and s.kind == "write"]
    (write_shape,) = writes
    return [
        _map_refusal(
            "089-a-materialisation-id-that-is-not-a-string",
            _map(id=True),
            "a materialisation id is a string",
            "str(True) is \"True\" in Python and String(true) is \"true\" in JavaScript, so one "
            "map named two different copies, and routing that named either reached a copy in one "
            "library and nothing in the other.",
        ),
        _map_refusal(
            "090-an-engine-that-is-not-a-string",
            _map(engine=5),
            "an engine is named by a string",
            "An engine is looked up by name among the adapters a session holds. A number coerced "
            "to text is a name nobody wrote.",
        ),
        _map_refusal(
            "091-a-lag-budget-written-as-a-string",
            _with_copy(_map(), lag_budget_ms="30000"),
            "lag_budget_ms must be a non-negative integer",
            "int(\"30000\") and Number(\"30000\") both accepted it, and int() truncated 1.5 where "
            "Number() kept it. A budget is a count of milliseconds.",
        ),
        _map_refusal(
            "092-a-negative-lag-budget",
            _with_copy(_map(), lag_budget_ms=-1),
            "lag_budget_ms must be a non-negative integer",
            "A copy cannot be allowed to be ahead of its source. Both libraries accepted it.",
        ),
        _map_refusal(
            "093-a-layout-table-that-is-not-a-name",
            _map(layout={"tables": {"Event": 5}}),
            "must be a table name",
            "Python compared str(table) with its reserved names and TypeScript the value as it "
            "came, so 5 was a table name in one library and not in the other.",
        ),
        _map_refusal(
            "094-layout-columns-that-are-not-an-object",
            _map(layout={"tables": {"Event": "event"}, "columns": []}),
            "columns maps an entity to an object of column name",
            "Python read `[]`, `false` and `0` as no columns and turned a list of pairs into "
            "columns; TypeScript took whatever arrived. Absent or null says no columns.",
        ),
        _map_refusal(
            "095-a-column-type-that-is-not-a-name",
            _map(layout={"tables": {"Event": "event"}, "columns": {"Event": {"id": 5}}}),
            "must be a type name",
            "A column's type is rendered into DDL. A number there failed in the client's engine, "
            "at CREATE TABLE, instead of when the map arrived.",
        ),
        _map_refusal(
            "096-derived-that-is-not-a-list",
            {
                **_map(),
                "groups": {
                    **_map()["groups"],
                    "Event": {**_map()["groups"]["Event"], "derived": {}},
                },
            },
            "'derived' is a list of materialisations",
            "Python iterated an object's keys as materialisations and TypeScript called .map on "
            "it. An array, or absent.",
        ),
        _map_refusal(
            "097-a-materialisation-that-is-not-an-object",
            {
                **_map(),
                "groups": {**_map()["groups"], "Event": {"source": "Event@pg"}},
            },
            "expected an object",
            "Python tested a string for the substring 'id' and raised a TypeError on a number. "
            "TypeScript already said this; now both do.",
        ),
        _map_refusal(
            "098-a-signed-map-without-a-canonical-form",
            {
                **_map(),
                "note": 1.5,
                "signature": {"alg": "ed25519", "key_id": "k1", "value": SIGNATURE},
            },
            "its payload has no canonical form",
            "A signature covers canonical bytes, so a payload with none cannot carry a valid one. "
            "Both loaders let their encoder's error out instead of a map error. Below contract 4 "
            "an unsigned map with such an annotation still loads, without a fingerprint.",
            signed=True,
        ),
        _map_refusal(
            "099-a-contract-4-map-with-an-unsafe-integer",
            {**_map(contract=4), "map_version": 9007199254740993},
            "canonically encodable",
            "Past 2^53 - 1 JavaScript reads a nearby double, so one document is signed over one "
            "number and read as another. Section 7d refuses unsafe numbers from contract 4; "
            "TypeScript did and Python encoded any integer.",
        ),
        _map_refusal(
            "100-a-write-shape-routed-at-a-copy",
            {**_with_copy(_map()), "routing": {write_shape.id: "Event@ch"}},
            "Writes go to the source whatever this table says",
            "Section 8 sends a write to the source before the table is consulted, so this entry "
            "can only be a planner believing something untrue. The control plane refuses to issue "
            "it; a hand-written map met nothing, and no vector routed a write at all.",
        ),
        _map_refusal(
            "101-a-copy-that-is-the-source-under-another-name",
            _with_copy(
                _map(),
                engine="pg-main",
                layout={"tables": {"Event": "event"}, "columns": {"Event": dict(EVENT_COLUMNS)}},
            ),
            "is in the same engine as the source and reuses its tables",
            "Its lag would always read zero and a read routed to it would be a read of the "
            "source. Python checked this only with a model and TypeScript before the copies' "
            "fan-out; now after coverage, with or without one, and it has a vector.",
        ),
        _map_refusal(
            "102-an-index-granularity-above-its-ceiling",
            _line_layout(
                _map(contract=5),
                indexes=[
                    {
                        "entity": "Order",
                        "name": "order_placed",
                        "columns": ["placed"],
                        "method": "minmax",
                        "granularity": 1025,
                    }
                ],
            ),
            "needs an integer granularity from 1 to 1024",
            "Only the lower bound had a case, so moving the ceiling by one left every vector "
            "green.",
        ),
        _map_refusal(
            "103-a-set-index-above-its-row-ceiling",
            _line_layout(
                _map(contract=5),
                indexes=[
                    {
                        "entity": "Order",
                        "name": "order_placed",
                        "columns": ["placed"],
                        "method": "set",
                        "granularity": 1,
                        "max_rows": 65537,
                    }
                ],
            ),
            "needs an integer max_rows from 1 to 65536",
            "The ceiling of the other bounded index parameter, for the same reason.",
        ),
        _map_refusal(
            "104-an-uppercase-project-id",
            {**_map(contract=4), "project_id": "A" * 32},
            "32-digit lowercase hexadecimal project_id",
            "A project id is compared byte for byte with the one configured locally. Accepting "
            "uppercase in the map would make two spellings of one project two projects.",
        ),
        _map_refusal(
            "105-key-orders-checked-in-name-order",
            _line_layout(_map(contract=5), key_order={"Order": [], "Line": []}),
            "key_order['Line'] must be a non-empty list of column names",
            "Two defects, written Order first. Section 8a checks a layout's entities in name "
            "order, so the refusal names Line in every library, whatever order its parser keeps.",
        ),
        _map_refusal(
            "106-partitions-checked-in-name-order",
            _line_layout(
                _map(contract=5),
                partition_by={"Order": {"field": "placed"}, "Line": {"field": "line"}},
            ),
            "partition_by['Line'] must be exactly",
            "The same rule for partitions: two defects, the one in name order is named.",
        ),
        _map_refusal(
            "107-a-signature-with-padding-inside-a-quantum",
            {
                **_map(),
                "signature": {
                    "alg": "ed25519",
                    "key_id": "k1",
                    # 86 characters of data and its padding, with one `=` among them: without
                    # the rule it decodes to 64 bytes, a signature that merely does not verify.
                    "value": "AAAA=" + "A" * 82 + "==",
                },
            },
            "the signature is not valid base64",
            "Base64 as Python 3.12's b64decode(validate=True) reads it. TypeScript decoded "
            "anything and refused later, as a signature that does not verify: the right answer "
            "for the wrong reason.",
            signed=True,
        ),
        _map_refusal(
            "108-a-map-that-places-no-groups",
            {**_map(), "groups": {}},
            "the map places no groups",
            "TypeScript accepted an empty object and, without a model, loaded a map that placed "
            "nothing.",
        ),
        _map_refusal(
            "109-routing-that-is-a-list",
            {**_map(), "routing": ["Event@pg"]},
            "'routing' must be a mapping from shape id to materialisation id",
            "TypeScript accepted an array as an object and read its indexes as shape ids. An "
            "empty list, false or null still says no routing, as it always has.",
        ),
    ]


def order_vectors() -> list[str]:
    """Two defects each, where the order of section 8a decides which is named.

    TypeScript checked these in another order than the reference, so each case is the refusal one
    library gave and the other did not: a layout derived before coverage, the shape of `routing`
    after it, and the payload's encoding before the configured keys.
    """
    unknown_auto = _map()
    unknown_auto["groups"]["Ghost"] = {
        "source": {"id": "Ghost@pg", "engine": "pg-main", "layout": {"auto": True}}
    }
    routing_first = _map()
    del routing_first["groups"]["Line"]
    routing_first["routing"] = ["Event@pg"]
    short_key = {
        **_map(),
        "note": 1.5,
        "signature": {"alg": "ed25519", "key_id": "k1", "value": SIGNATURE},
    }
    model = _model()
    written = [
        _map_refusal(
            "110-a-group-the-model-does-not-have-asking-for-an-auto-layout",
            unknown_auto,
            "the map places groups this model does not have",
            "Coverage comes before a layout is derived. TypeScript derived it first, found no "
            "members for the group and said no model was supplied - with the model in hand.",
        ),
        _map_refusal(
            "111-routing-is-checked-before-coverage",
            routing_first,
            "'routing' must be a mapping from shape id to materialisation id",
            "The map misses a group and its routing is a list. The reference reads routing's shape "
            "straight after the groups, before coverage; TypeScript named the missing group.",
        ),
    ]
    # The configured key is refused before the payload is encoded. The errors runner passes one
    # bare key, so the defect in the configuration is its length.
    name = "112-a-short-key-before-a-payload-without-a-canonical-form"
    try:
        sde.load_map(short_key, model=model, public_key=bytes(31), require_signature=True)
    except MapError as exc:
        assert "is 31 bytes and an Ed25519 public key is 32" in str(exc), str(exc)
    else:
        raise AssertionError(f"{name}: the reference accepted the map")
    _write(
        VECTORS / "errors" / name,
        {
            "model.json": sde.neutral_declaration(model),
            "map.json": short_key,
            "expected.json": {
                "error": "MapError",
                "match": "is 31 bytes and an Ed25519 public key is 32",
                "stage": "map",
                "why": "The keys are the caller's configuration and are checked before the "
                "document's payload is encoded, so a key pasted a byte short is named as that "
                "rather than as a map with no canonical form. TypeScript encoded first.",
                "load": {
                    "public_key": base64.b64encode(bytes(31)).decode(),
                    "require_signature": True,
                },
            },
        },
    )
    written.append("errors/112")
    return written


def routing_vectors() -> list[str]:
    """An empty `partition_by` below contract 5 says nothing, as it always did."""
    model = _model()
    document = _map(contract=4)
    document["groups"]["Event"]["source"]["layout"]["partition_by"] = {}
    placement = sde.load_map(document, model=model)
    cases = []
    for shape in sde.enumerate_shapes(model):
        if shape.group == "Event" and shape.kind in ("point_read", "write"):
            got = sde.resolve(placement, shape)
            assert got.id == "Event@pg", got.id
            cases.append({"shape": shape.id, "expect": got.id})
    _write(
        VECTORS / "routing" / "003-an-empty-partition-below-contract-5-says-nothing",
        {
            "model.json": sde.neutral_declaration(model),
            "map.json": document,
            "cases.json": cases,
        },
    )
    return ["routing/003"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--i-am-changing-the-contract", action="store_true", required=True)
    parser.parse_args()
    written = model_vectors() + map_vectors() + order_vectors() + routing_vectors()
    print(f"Wrote {len(written)} vectors: {', '.join(written)}")


if __name__ == "__main__":
    main()

"""Generate the Elasticsearch vectors: the mapping a layout renders, and a row as a document.

Two libraries writing one row to one index have to produce the same ``_id`` - or a second save
becomes a second document - and the same ``_source``, or a reader in one language gets back
another value than the writer in the other wrote. Neither needs a server to check, so both are
vectors (format contract 7m):

- ``schema/019`` and ``020``: the index mapping of a layout, as the canonical statement the
  renderer emits, and the layouts it refuses.
- ``documents/``: a row's ``_id`` and ``_source``, or the refusal of a value the index would not
  keep exactly. Values travel in the case as text where JSON cannot carry them - an integer past
  2^53 as ``{"$int": "..."}``, a decimal and an instant as strings - and the runner decodes them
  by the column's type.

Each file is written only when it is new or unchanged; ``--check`` fails on any difference.

    python conformance/tools/elasticsearch_vectors.py [--check]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "python/src"))
import sde  # noqa: E402
from sde.engines.elasticsearch import document  # noqa: E402
from sde.errors import EngineError  # noqa: E402
from sde.testing.loader import model_from_neutral  # noqa: E402

VECTORS = ROOT / "conformance" / "vectors"
SAFE = 2**53


def _int(value: int) -> Any:
    return value if -SAFE < value < SAFE else {"$int": str(value)}


def _encoded(value: Any) -> Any:
    """A value as the case file carries it."""
    if value is None or isinstance(value, (bool, str, float)):
        return value
    if isinstance(value, int):
        return _int(value)
    if isinstance(value, (Decimal, UUID)):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(type(value))


def _json(value: Any) -> Any:
    """A stored value as the expectation carries it: integers past 2^53 as text."""
    if isinstance(value, bool) or value is None or isinstance(value, (str, float)):
        return value
    if isinstance(value, int):
        return _int(value)
    if isinstance(value, list):
        return [_json(item) for item in value]
    if isinstance(value, dict):
        return {name: _json(item) for name, item in value.items()}
    raise TypeError(type(value))


EVERY_TYPE = {
    "entities": [
        {
            "name": "Reading",
            "fields": [
                {"name": "station", "type": "string"},
                {"name": "at", "type": "timestamptz"},
                {"name": "local", "type": "timestamp", "nullable": True},
                {"name": "day", "type": "date"},
                {"name": "ref", "type": "uuid"},
                {"name": "amount", "type": "decimal(18,4)"},
                {"name": "level", "type": "float64"},
                {"name": "count", "type": "int32"},
                {"name": "total", "type": "int64"},
                {"name": "ok", "type": "bool"},
                {"name": "note", "type": "string", "nullable": True},
            ],
            "key": ["station", "at"],
        },
        {
            "name": "Station",
            "fields": [{"name": "code", "type": "string"}, {"name": "name", "type": "string"}],
            "key": ["code"],
        },
    ],
    "relations": [{"name": "home", "from": "Reading", "to": "Station"}],
    "atomic": [],
}


def _schema() -> dict[str, dict[str, Any]]:
    model = model_from_neutral(EVERY_TYPE)
    group = sde.colocation_groups(model)[0]
    layout = sde.default_layout(model, group, dialect="elasticsearch")
    raw_layout = {
        "tables": dict(layout.tables),
        "columns": {name: dict(columns) for name, columns in layout.columns.items()},
    }
    raw_map = {
        "contract": 1,
        "model_version": model.version,
        "map_version": 1,
        "groups": {group.name: {"source": {"id": "Reading@es", "engine": "es-1",
                                           "layout": raw_layout}}},
    }
    keys = {name: list(model.entity(name).key) for name in group.members}
    statements = list(sde.schema_statements(layout, keys=keys, dialect="elasticsearch"))
    rendered = {
        "materialization": "Reading@es",
        "dialect": "elasticsearch",
        "fixed": False,
        "statements": statements,
        "why": (
            "One strict index per entity, as the canonical JSON of what creates it. Every column "
            "is typed - the neutral type in `meta.sde_type` where the native type alone would not "
            "say how to read the value back - the key keeps its declared order in `_meta.sde.key`, "
            "and each text field, but not the uuid one, has a token field beside it under "
            "`__sde_tokens`. The relation's column is a plain keyword: every mapped field is "
            "indexed, so there is no index for a layout to declare."
        ),
    }

    def refused(columns: dict[str, dict[str, str]], keys: dict[str, list[str]], match: str,
                why: str) -> dict[str, Any]:
        bad = sde.PhysicalLayout(tables=dict(layout.tables), columns=columns)
        try:
            sde.schema_statements(bad, keys=keys, dialect="elasticsearch")
        except EngineError as exc:
            assert match in str(exc), str(exc)
            return {"materialization": "Reading@es", "dialect": "elasticsearch", "fixed": False,
                    "layout_columns": columns, "keys": keys, "error": "EngineError",
                    "match": match, "why": why}
        raise SystemExit(f"expected a refusal containing {match!r}")

    columns = {name: dict(cols) for name, cols in layout.columns.items()}
    floating = {name: dict(cols) for name, cols in columns.items()}
    floating["Reading"]["level"] = "double"
    unknown = {name: dict(cols) for name, cols in columns.items()}
    unknown["Reading"]["note"] = "text"
    reserved = {name: dict(cols) for name, cols in columns.items()}
    reserved["Station"]["__sde_tokens"] = "keyword"
    wide = {name: dict(cols) for name, cols in columns.items()}
    wide["Reading"]["amount"] = "long/decimal(19,4)"
    refusals = [
        refused(floating, {"Reading": ["station", "level"], "Station": ["code"]},
                "floating-point field",
                "A document's _id comes from its key's exact bytes, and a float has no text two "
                "languages agree on."),
        refused(unknown, keys, "is not a column type this library maps in Elasticsearch",
                "`text` is an Elasticsearch type, and not one a layout of this library names: a "
                "map that asked for it would get an analyzer nobody chose."),
        refused(reserved, keys, "starting with __sde_ are the library's own",
                "The token field's name is the library's; a column of that name would be "
                "overwritten by the tokens of every write."),
        refused(wide, keys, "is not a column type this library maps in Elasticsearch",
                "A decimal is its value times 10^scale in a long, exact to eighteen digits and "
                "no further."),
    ]
    return {
        "schema/019-elasticsearch-an-index-per-entity": {
            "model.json": sde.neutral_declaration(model),
            "map.json": raw_map,
            "cases.json": [rendered],
        },
        "schema/020-elasticsearch-refuses-what-it-would-not-keep": {
            "model.json": sde.neutral_declaration(model),
            "map.json": raw_map,
            "cases.json": refusals,
        },
    }


READING = {
    "station": "keyword", "at": "long/timestamptz", "local": "long/timestamp", "day": "date",
    "ref": "keyword/uuid", "amount": "long/decimal(18,4)", "level": "double",
    "count": "integer", "total": "long", "ok": "boolean", "note": "keyword",
    "home_code": "keyword",
}
KEY = ["station", "at"]

ROW = {
    "station": "WAW", "at": datetime.fromisoformat("2026-10-23T08:00:00.123456+00:00"),
    "local": datetime.fromisoformat("0001-01-01T00:00:00.000001"), "day": date(9999, 12, 31),
    "ref": UUID("0000000A-0000-0000-0000-000000000001"), "amount": Decimal("-99999999999999.9999"),
    "level": 0.1, "count": -(2**31), "total": 2**63 - 1, "ok": False,
    "note": "Red apple, RED apples", "home_code": "WAW",
}

GAUGE = {"id": "long/timestamp", "x": "float", "label": "keyword"}
"""Columns of the later cases: a float32 and a key that can be negative - an instant before 1970."""

DOCUMENT_CASES: list[tuple[str, dict[str, Any], str]] = [
    ("001-every-type", ROW,
     "Every type as the index keeps it: microseconds since the epoch for both instants - a naive "
     "one counted as if it were UTC - a date as its text, a uuid in lowercase, a decimal times "
     "10^4, an int64 past 2^53 exactly. Text keeps its tokens beside it, distinct and sorted."),
    ("002-a-null-and-no-tokens", {**ROW, "note": None, "local": None},
     "A null is kept as null, so a read returns the same fields however the row was written, and "
     "a null text has no tokens."),
    ("003-the-key-in-declared-order", {**ROW, "station": "e\u0301"},
     "The _id frames each key value by type and length, in the declared key order, from the text "
     "exactly as written. This row's station is a decomposed e-acute."),
    ("004-a-composed-key-is-another-document", {**ROW, "station": "\u00e9"},
     "The same letter composed: another _id. Every other engine compares keys byte by byte, so "
     "these are two rows there and must be two documents here."),
    ("005-a-decimal-that-would-be-rounded", {**ROW, "amount": Decimal("0.00001")},
     "Five fractional digits in a decimal(18,4): refused, not rounded."),
    ("006-text-longer-than-a-term", {**ROW, "note": "x" * 32767},
     "One byte past Lucene's limit on a term: refused before it is sent."),
    ("007-an-int32-out-of-range", {**ROW, "count": 2**31},
     "An int32 field takes no value outside int32."),
    ("008-a-key-without-a-value", {**ROW, "station": None},
     "A document is addressed by its key, so a key field cannot be null."),
    ("009-a-float32-as-the-engine-indexes-it",
     {"id": datetime.fromisoformat("2026-10-23T08:00:00"), "x": 0.1, "label": "a"},
     "A float field is indexed as a float32 and returned as written, so the library writes the "
     "float32: the value a filter compares is the value a read returns."),
    ("010-a-float32-out-of-range",
     {"id": datetime.fromisoformat("2026-10-23T08:00:00"), "x": 1e39, "label": "a"},
     "Past the largest float32 there is nothing the engine could index: refused before sending."),
    ("011-a-token-longer-than-a-term",
     {"id": datetime.fromisoformat("2026-10-23T08:00:00"), "x": 1.0, "label": "\u023a" * 16383},
     "The text is 32,766 UTF-8 bytes and folds to 49,149: a token is a term too, so it is "
     "refused before sending although the text itself fits."),
    ("012-a-key-before-1970",
     {"id": datetime.fromisoformat("1969-07-20T20:17:40.000001"), "x": -0.0, "label": "b"},
     "An integer is framed with its minus sign: an instant before 1970 is a negative number of "
     "microseconds. A float32 keeps the sign of a zero."),
    ("014-a-write-with-its-generation",
     {"id": datetime.fromisoformat("2026-10-23T08:00:00"), "x": 1.0, "label": "a",
      "__sde_write_epoch": 7},
     "A fenced index maps the write generation beside the layout's fields, and a session's write "
     "carries it as a long in _source - where the index's pipeline checks it. It is no part of "
     "the key, so the _id is the one the same row has without it."),
    ("013-a-lone-surrogate",
     {"id": datetime.fromisoformat("2026-10-23T08:00:00"), "x": 1.0, "label": "a\ud800b"},
     "UTF-8 has no bytes for half of a surrogate pair: refused, rather than written as U+FFFD - "
     "another text than the one saved."),
]


def _documents() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for name, row, why in DOCUMENT_CASES:
        columns, key = (GAUGE, ["id"]) if int(name[:3]) >= 9 else (READING, KEY)
        if "__sde_write_epoch" in row:
            columns = {**columns, "__sde_write_epoch": "long"}
        case = {"columns": columns, "key": key,
                "row": {column: _encoded(value) for column, value in row.items()}}
        try:
            identity, source = document(columns, key, row)
            expected: dict[str, Any] = {"id": identity, "source": _json(source)}
        except EngineError as exc:
            match = {
                "005": "fractional digits", "006": "cannot index", "007": "outside int32",
                "008": "needs its key", "010": "outside float32",
                "011": "once normalised and folded", "013": "lone surrogate",
            }[name[:3]]
            assert match in str(exc), str(exc)
            expected = {"error": "EngineError", "match": match}
        out[f"documents/{name}"] = {
            "case.json": case, "expected.json": expected, "why.txt": why,
        }
    return out


def _text(filename: str, body: Any) -> str:
    if filename.endswith(".txt"):
        return str(body) + "\n"
    return json.dumps(body, indent=2, ensure_ascii=True) + "\n"


FENCE_PROCESSORS = ROOT / "conformance" / "fence-processors.json"
"""The fence pipeline's processor for a fixed set of constraints (``docs/write-fences.md``). Not
a vector family: a table both libraries' tests read, so a fence one provisions the other reads."""

_OWNER = "__sde_f_owner_" + "0" * 31 + "1"


def _bounds(*epochs: int) -> dict[str, str]:
    out = {}
    for epoch in epochs:
        out[f"__sde_f_min_{epoch}"] = f"__sde_write_epoch >= {epoch}"
        out[f"__sde_f_max_{epoch}"] = f"__sde_write_epoch <= {epoch}"
    return out


def _fence_processors() -> dict[str, Any]:
    from sde.engines._write_fences import fence_processor

    cases: dict[str, dict[str, str]] = {
        "nothing": {},
        "owner only": {_OWNER: "1"},
        "one generation": {_OWNER: "1", **_bounds(3)},
        "held": {_OWNER: "1", **_bounds(3), "__sde_f_hold_" + "a" * 32: "0"},
        "mid-advance": {_OWNER: "1", **_bounds(3, 10)},
    }
    return {"cases": cases, "processors": {name: fence_processor(c) for name, c in cases.items()}}


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail if a vector would change")
    args = parser.parse_args()
    planned = {**_schema(), **_documents()}
    changed = []
    for directory, files in planned.items():
        for filename, body in files.items():
            path = VECTORS / directory / filename
            text = _text(filename, body)
            if path.exists() and path.read_text(encoding="utf-8") == text:
                continue
            changed.append(str(path.relative_to(ROOT)))
            if not args.check:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
    table = json.dumps(_fence_processors(), indent=2, ensure_ascii=True) + "\n"
    if not (FENCE_PROCESSORS.exists() and FENCE_PROCESSORS.read_text(encoding="utf-8") == table):
        changed.append(str(FENCE_PROCESSORS.relative_to(ROOT)))
        if not args.check:
            FENCE_PROCESSORS.write_text(table, encoding="utf-8")
    if args.check and changed:
        print("Elasticsearch vectors would change:", *changed, sep="\n  ")
        return 1
    written = "unchanged" if not changed else f"{len(changed)} files written"
    print(f"{len(planned)} Elasticsearch vectors, {written}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

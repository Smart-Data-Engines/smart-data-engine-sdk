"""A logical model suggested from sample rows, and what it refuses to guess.

Declaring a model by hand is the honest interface and it is a bad first step. Somebody with a few
thousand rows of weather readings wants to point at them and be told what the schema should be,
and telling them to write out entities, fields and neutral types first is asking for the work
before the value.

**The rows never leave the process.** This module runs in the client's library, on the client's
machine, and produces a declaration - names and types, no values. That is not an incidental
property of where the code happens to sit: the control plane's promise is that it never sees a
row, and the only way to make an on-ramp from real data compatible with that promise is to do the
inference on the side that already has the data. It also means the inference is public code
anybody can read, which is worth more than any assurance about it.

Everything here is a **suggestion with its evidence attached**, and four things are refused
outright.

**The four invariants cannot be inferred, at all, ever.** Atomicity, residency, personal data and
the cost ceiling are facts about a business and its jurisdiction, and there is nothing in a column
of numbers that implies them. This matters more than the rest of the module: a declaration that
looks complete and carries no invariants produces a placement that ignores the client's legal
constraints and looks perfectly healthy doing it. So the output states what is still missing, by
name, and ``what_you_must_still_declare`` is not decoration.

**Uniqueness across a sample is not a key.** Five rows with distinct values in a column is
evidence of nothing, and a key chosen that way is a key that starts rejecting writes in week
three. Candidates are reported with the sample size next to them and the choice stays with the
client. The one exception is the convention the library already has: a field named ``id`` is the
key unless ``Meta.key`` says otherwise, which is a documented rule rather than something guessed
from this data.

**Text is not coerced by looking at it.** A column of ISO-8601 strings is very probably a
timestamp, and inferring one would be right until the row that is not, at which point the client's
writes fail against a column type derived from data they no longer have. Candidate refinements are
reported instead.

**A column with two types has no type.** An integer in one row and a string in another is not a
narrowing problem, it is two columns that were given one name, and picking either produces silent
truncation later.
"""

from __future__ import annotations

import datetime as dt
import decimal
import re
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .errors import DeclarationError

_ISO_8601 = re.compile(
    r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?)?$"
)
_UUID_TEXT = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

# What a Python value maps to, with nothing name-based and nothing content-based in it.
_FROM_TYPE: Mapping[type, str] = {
    bool: "bool",
    int: "int64",
    float: "float64",
    str: "string",
    bytes: "bytes",
    uuid.UUID: "uuid",
    dt.date: "date",
}


@dataclass(frozen=True)
class Note:
    """One thing the client should look at, with the evidence that produced it.

    ``field`` is empty for notes about an entity as a whole. ``severity`` has two values and no
    middle one: ``blocking`` means the declaration will not build until it is resolved, and
    ``review`` means it will build and may be wrong. A third level would be a way to file
    something as neither.
    """

    severity: str
    entity: str
    field: str
    what: str

    def __post_init__(self) -> None:
        if self.severity not in ("blocking", "review"):
            raise ValueError(f"severity is 'blocking' or 'review', not {self.severity!r}")

    def __str__(self) -> str:
        where = f"{self.entity}.{self.field}" if self.field else self.entity
        return f"[{self.severity}] {where}: {self.what}"


@dataclass(frozen=True)
class InferredModel:
    """A declaration suggested from data, and everything that is still the client's to say.

    ``declaration`` is the neutral form - the same shape a model vector uses - so it can be
    written to a file, read, corrected and committed. That is the point: the output of this module
    is meant to be edited by a person, not consumed silently by the next call.
    """

    declaration: dict[str, Any]
    sample_size: int
    candidate_keys: Mapping[str, tuple[str, ...]]
    notes: tuple[Note, ...]

    @property
    def blocking(self) -> tuple[Note, ...]:
        return tuple(n for n in self.notes if n.severity == "blocking")

    def what_you_must_still_declare(self) -> tuple[str, ...]:
        """The four invariants, always, plus anything blocking.

        The invariants are listed unconditionally and not "if they look relevant". There is no
        signal in the data that says whether two entities must change together or whether a column
        is personal data, so their absence from this list would have to mean "we checked" - and
        nothing checked.
        """
        out = [
            "atomicity: which entities must change together in one transaction. Nothing in your "
            "data implies this and it decides which engines may hold them.",
            "residency: which entities must stay in which jurisdiction. This is a legal constraint "
            "and it is a hard exclusion, not a preference.",
            "personal data: which fields are personal data. It decides what may be copied into an "
            "analytical materialisation.",
            "cost ceiling: the monthly figure a placement may not exceed, and its currency.",
        ]
        out.extend(str(note) for note in self.blocking)
        return tuple(out)

    def summary(self) -> str:
        lines = [
            f"Inferred from {self.sample_size} row(s). This is a suggestion, not a declaration.",
            "",
        ]
        for entity in self.declaration["entities"]:
            fields = ", ".join(f"{f['name']}: {f['type']}" for f in entity["fields"])
            lines.append(f"  {entity['name']}({fields})")
            candidates = self.candidate_keys.get(entity["name"], ())
            if candidates and len(candidates) == len(entity["fields"]):
                # Every column unique tells you nothing about which one is a key, and printing the
                # full list dressed as a finding invites somebody to pick the first entry. Same
                # shape as a placement score of 1.0000 from a registry with one engine in it.
                lines.append(
                    f"    every column is unique across {self.sample_size} row(s), which says "
                    f"nothing about which is a key - a sample this size makes uniqueness free. "
                    f"The key is yours to declare."
                )
            elif candidates:
                lines.append(
                    f"    unique across the sample: {list(candidates)} - {self.sample_size} row(s) "
                    f"is not evidence of uniqueness, so the key is yours to choose"
                )
        lines += ["", "Still yours to declare:"]
        lines += [f"  - {item}" for item in self.what_you_must_still_declare()]
        if any(n.severity == "review" for n in self.notes):
            lines += ["", "Worth a look:"]
            lines += [f"  - {n}" for n in self.notes if n.severity == "review"]
        return "\n".join(lines)


def _decimal_type(values: Sequence[decimal.Decimal]) -> str:
    """A decimal type wide enough for what was seen, and one that says it is a guess.

    Precision from a sample is a floor rather than a fact, so the width is padded: a column
    holding 99.99 today holds 1000.00 next month, and a `numeric(4,2)` derived from four observed
    digits fails on that write rather than rounding it. Padding is stated in a note, because a
    client who knows the real range should narrow it.
    """
    digits = 1
    scale = 0
    for value in values:
        _, digits_tuple, exponent = value.as_tuple()
        if not isinstance(exponent, int):
            continue
        scale = max(scale, -exponent if exponent < 0 else 0)
        digits = max(digits, len(digits_tuple))
    return f"decimal({min(digits + 4, 38)},{scale})"


def _neutral_type(entity: str, name: str, values: list[Any]) -> tuple[str, list[Note]]:
    notes: list[Note] = []
    present = [v for v in values if v is not None]
    if not present:
        return "", [
            Note(
                severity="blocking",
                entity=entity,
                field=name,
                what=(
                    "every sampled row has this empty, so there is no evidence of what it holds. "
                    "Declare its type or drop the column."
                ),
            )
        ]

    kinds = {type(v) for v in present}
    # datetime is a subclass of date, so it has to be resolved before the mapping is consulted.
    if any(isinstance(v, dt.datetime) for v in present):
        kinds = {dt.datetime if isinstance(v, dt.datetime) else type(v) for v in present}

    if len(kinds) > 1:
        # int and float together is the one mixture with an answer: a column of 1, 2, 2.5 is a
        # float column somebody happened to write whole numbers into. Everything else is two
        # columns with one name.
        if kinds <= {int, float} and bool not in kinds:
            notes.append(
                Note(
                    severity="review",
                    entity=entity,
                    field=name,
                    what=(
                        "whole numbers and fractions in the same column, read as float64. If this "
                        "is money, declare it as a decimal: binary floating point cannot represent "
                        "0.01 and a total that is out by a cent is a total nobody trusts."
                    ),
                )
            )
            return "float64", notes
        return "", [
            Note(
                severity="blocking",
                entity=entity,
                field=name,
                what=(
                    f"two different types in the sample ({sorted(k.__name__ for k in kinds)}). "
                    f"This is not a narrowing problem: it is two columns that were given one name, "
                    f"and choosing either one truncates the other silently."
                ),
            )
        ]

    kind = next(iter(kinds))

    if kind is decimal.Decimal:
        inferred = _decimal_type([v for v in present if isinstance(v, decimal.Decimal)])
        notes.append(
            Note(
                severity="review",
                entity=entity,
                field=name,
                what=(
                    f"{inferred} - the precision is padded past what the sample shows, because a "
                    f"width taken from a sample is a floor and the first wider value would fail to "
                    f"write. Narrow it if you know the real range."
                ),
            )
        )
        return inferred, notes

    if kind is dt.datetime:
        aware = [v for v in present if isinstance(v, dt.datetime) and v.tzinfo is not None]
        if len(aware) == len(present):
            return "timestamptz", notes
        if not aware:
            notes.append(
                Note(
                    severity="review",
                    entity=entity,
                    field=name,
                    what=(
                        "naive datetimes, read as timestamp without a zone. If these are instants "
                        "rather than wall-clock readings, declare timestamptz: a naive value means "
                        "different moments in two engines, and the difference is silent."
                    ),
                )
            )
            return "timestamp", notes
        return "", [
            Note(
                severity="blocking",
                entity=entity,
                field=name,
                what=(
                    "some rows carry a timezone and some do not. One column cannot be both, and "
                    "guessing turns a subset of your data into the wrong instant."
                ),
            )
        ]

    if kind is float:
        notes.append(
            Note(
                severity="review",
                entity=entity,
                field=name,
                what=(
                    "float64. If this is money or anything summed and compared for equality, "
                    "declare a decimal instead: binary floating point cannot represent 0.01."
                ),
            )
        )
        return "float64", notes

    if kind is str:
        texts = [v for v in present if isinstance(v, str)]
        if texts and all(_UUID_TEXT.match(v) for v in texts):
            notes.append(
                Note(
                    severity="review",
                    entity=entity,
                    field=name,
                    what=(
                        "every sampled value looks like a UUID, and is still read as a "
                        "string. Declare uuid if that is what it is - not inferred, because the "
                        "row that is not a UUID would fail to write against a type derived from "
                        "rows you no longer have."
                    ),
                )
            )
        elif texts and all(_ISO_8601.match(v) for v in texts):
            notes.append(
                Note(
                    severity="review",
                    entity=entity,
                    field=name,
                    what=(
                        "every sampled value looks like an ISO-8601 timestamp, and this is still "
                        "read as a string. Declare timestamptz if that is what it is; inferring it "
                        "would be right until the first value that is not."
                    ),
                )
            )
        return "string", notes

    mapped = _FROM_TYPE.get(kind)
    if mapped is None:
        return "", [
            Note(
                severity="blocking",
                entity=entity,
                field=name,
                what=(
                    f"{kind.__name__} has no neutral type. Convert it in your application, or "
                    f"declare the field yourself - a type nobody mapped cannot be stored in any "
                    f"engine, and a fallback to text would store it and lose it."
                ),
            )
        ]
    return mapped, notes


def infer_model(
    rows: Iterable[Mapping[str, Any]], *, entity: str, sample_limit: int = 1000
) -> InferredModel:
    """Suggest a declaration for one entity from sample rows. Nothing leaves this process.

    ``sample_limit`` caps how many rows are read, because the point is a suggestion and reading a
    hundred million rows to produce one is a cost with no return. The number actually used is
    recorded in the result, since every claim in it is conditional on that number.

    Raises on no rows at all rather than returning an empty declaration: an empty model builds
    without error, places nothing, and the failure surfaces as a placement map with no groups.
    """
    sampled: list[Mapping[str, Any]] = []
    for row in rows:
        if len(sampled) >= sample_limit:
            break
        if not isinstance(row, Mapping):
            raise DeclarationError(
                f"a sample row is {type(row).__name__}, not a mapping of column to value. "
                f"Inference reads column names, so a positional row has nothing to name."
            )
        sampled.append(row)

    if not sampled:
        raise DeclarationError(
            "no sample rows. An empty declaration builds without error, places nothing, and the "
            "failure would surface later as a placement map with no groups."
        )

    columns: dict[str, list[Any]] = {}
    for row in sampled:
        for name, value in row.items():
            columns.setdefault(str(name), []).append(value)

    notes: list[Note] = []
    fields: list[dict[str, str]] = []
    for name in sorted(columns):
        values = columns[name]
        if len(values) < len(sampled):
            notes.append(
                Note(
                    severity="review",
                    entity=entity,
                    field=name,
                    what=(
                        f"present in {len(values)} of {len(sampled)} sampled rows. Every "
                        f"field in a declaration is required; if this one is genuinely "
                        f"optional, that is a modelling decision rather than a type."
                    ),
                )
            )
        inferred, field_notes = _neutral_type(entity, name, values)
        notes.extend(field_notes)
        if inferred:
            fields.append({"name": name, "type": inferred})

    candidates = tuple(
        name
        for name in sorted(columns)
        if len(columns[name]) == len(sampled)
        and None not in columns[name]
        and len({_hashable(v) for v in columns[name]}) == len(sampled)
    )

    declaration: dict[str, Any] = {
        "entities": [{"name": entity, "fields": fields}],
        "relations": [],
        "atomic": [],
    }

    return InferredModel(
        declaration=declaration,
        sample_size=len(sampled),
        candidate_keys={entity: candidates},
        notes=tuple(notes),
    )


def _hashable(value: Any) -> Any:
    """Values as something a set can hold, so uniqueness can be counted at all."""
    if isinstance(value, (list, dict, set)):
        return repr(value)
    return value


def infer_models(
    named_rows: Mapping[str, Iterable[Mapping[str, Any]]], *, sample_limit: int = 1000
) -> InferredModel:
    """Several entities in one declaration, which is what a real application has.

    Relations are **not** inferred. A column in one entity holding values that appear as another
    entity's key looks exactly like a foreign key and looks exactly the same when it is a
    coincidence of two independent identifier spaces - and a relation is what merges two entities
    into one colocation group, so guessing one wrong changes which engine both of them live in.
    """
    entities: list[dict[str, Any]] = []
    candidates: dict[str, tuple[str, ...]] = {}
    notes: list[Note] = []
    sizes: list[int] = []

    for entity in sorted(named_rows):
        one = infer_model(named_rows[entity], entity=entity, sample_limit=sample_limit)
        entities.extend(one.declaration["entities"])
        candidates.update(one.candidate_keys)
        notes.extend(one.notes)
        sizes.append(one.sample_size)

    return InferredModel(
        declaration={"entities": entities, "relations": [], "atomic": []},
        # The smallest sample, because every claim in the result is only as good as the weakest one
        # behind it and a mean would hide an entity inferred from two rows.
        sample_size=min(sizes),
        candidate_keys=candidates,
        notes=tuple(notes),
    )

"""Inference from sample rows, and the four things it refuses to guess.

The most important test in this file is the one about invariants. A declaration that looks complete
and carries no atomicity, residency, personal-data or cost-ceiling statement produces a placement
that ignores the client's legal constraints and looks perfectly healthy doing it - so the output has
to say what is still missing, unconditionally, and not "if it looks relevant".
"""

from __future__ import annotations

import datetime as dt
import decimal
import uuid

import pytest

import sde
from sde.errors import DeclarationError

UTC = dt.UTC


def _weather() -> list[dict[str, object]]:
    return [
        {
            "station": "WAW",
            "at": dt.datetime(2026, 8, 31, 10, 0, tzinfo=UTC),
            "celsius": decimal.Decimal("21.40"),
            "humidity": 62,
        },
        {
            "station": "KRK",
            "at": dt.datetime(2026, 8, 31, 10, 5, tzinfo=UTC),
            "celsius": decimal.Decimal("19.80"),
            "humidity": 71,
        },
    ]


def test_the_four_invariants_are_always_listed_as_still_the_client_s_to_declare() -> None:
    """Unconditionally, because nothing in a column of numbers implies any of them.

    Listing them only "when relevant" would make their absence mean "we checked", and nothing
    checked. This is the difference between an on-ramp and a trap.
    """
    inferred = sde.infer_model(_weather(), entity="Reading")
    missing = " ".join(inferred.what_you_must_still_declare())
    for invariant in ("atomicity", "residency", "personal data", "cost ceiling"):
        assert invariant in missing

    # And they are in the human summary, not only in the structure.
    assert "Still yours to declare" in inferred.summary()


def test_no_row_values_appear_anywhere_in_the_output() -> None:
    """The promise the whole product rests on, checked on the one path that reads real data.

    Inference runs in the client's library on the client's machine, and what it produces is a
    declaration: names and types. If a value could reach the output it could reach us, and the
    library is public precisely so that this is checkable by reading rather than by trust.
    """
    rows = [
        {"station": "SECRET-STATION-XYZ", "humidity": 4242},
        {"station": "ANOTHER-SECRET", "humidity": 9999},
    ]
    inferred = sde.infer_model(rows, entity="Reading")
    rendered = repr(inferred.declaration) + inferred.summary() + repr(inferred.notes)
    for leaked in ("SECRET-STATION-XYZ", "ANOTHER-SECRET", "4242", "9999"):
        assert leaked not in rendered, f"{leaked!r} reached the output of inference"


def test_types_come_from_python_types_and_not_from_column_names() -> None:
    inferred = sde.infer_model(_weather(), entity="Reading")
    fields = {f["name"]: f["type"] for f in inferred.declaration["entities"][0]["fields"]}
    assert fields["at"] == "timestamptz"
    assert fields["humidity"] == "int64"
    assert fields["station"] == "string"
    assert fields["celsius"].startswith("decimal(")


def test_decimal_precision_is_padded_and_says_so() -> None:
    """A width taken from a sample is a floor, and the first wider value fails to write."""
    inferred = sde.infer_model(_weather(), entity="Reading")
    fields = {f["name"]: f["type"] for f in inferred.declaration["entities"][0]["fields"]}
    # Two observed digits before the point, scale 2, padded well past it.
    assert fields["celsius"] == "decimal(8,2)"
    padding = [n for n in inferred.notes if n.field == "celsius"]
    assert padding
    assert "floor" in padding[0].what


def test_a_float_column_says_to_declare_a_decimal_if_it_is_money() -> None:
    rows = [{"total": 19.99}, {"total": 4.5}]
    inferred = sde.infer_model(rows, entity="Order")
    note = next(n for n in inferred.notes if n.field == "total")
    assert "0.01" in note.what
    assert note.severity == "review"


def test_two_types_in_one_column_is_blocking_rather_than_narrowed() -> None:
    """Not a narrowing problem: two columns that were given one name."""
    rows = [{"value": 1}, {"value": "one"}]
    inferred = sde.infer_model(rows, entity="Thing")
    blocking = [n for n in inferred.blocking if n.field == "value"]
    assert blocking
    assert "one name" in blocking[0].what
    # And the field is left out of the declaration rather than guessed.
    assert inferred.declaration["entities"][0]["fields"] == []


def test_whole_numbers_and_fractions_together_are_read_as_float_with_a_warning() -> None:
    """The one mixture with an answer, and it still gets a note about money."""
    rows = [{"value": 1}, {"value": 2.5}]
    inferred = sde.infer_model(rows, entity="Thing")
    fields = {f["name"]: f["type"] for f in inferred.declaration["entities"][0]["fields"]}
    assert fields["value"] == "float64"
    assert any("money" in n.what for n in inferred.notes)


def test_mixed_timezone_awareness_is_blocking() -> None:
    """One column cannot be both, and guessing turns a subset into the wrong instant."""
    rows = [
        {"at": dt.datetime(2026, 8, 31, 10, tzinfo=UTC)},
        {"at": dt.datetime(2026, 8, 31, 12)},
    ]
    inferred = sde.infer_model(rows, entity="Reading")
    assert any("some rows carry a timezone" in n.what for n in inferred.blocking)


def test_naive_datetimes_are_timestamp_and_warn_about_instants() -> None:
    rows = [{"at": dt.datetime(2026, 8, 31, 10)}, {"at": dt.datetime(2026, 8, 31, 11)}]
    inferred = sde.infer_model(rows, entity="Reading")
    fields = {f["name"]: f["type"] for f in inferred.declaration["entities"][0]["fields"]}
    assert fields["at"] == "timestamp"
    assert any("different moments in two engines" in n.what for n in inferred.notes)


def test_text_that_looks_like_a_timestamp_stays_text() -> None:
    """Right until the row that is not, at which point writes fail against a derived type."""
    rows = [{"at": "2026-08-31T10:00:00Z"}, {"at": "2026-08-31T11:00:00Z"}]
    inferred = sde.infer_model(rows, entity="Reading")
    fields = {f["name"]: f["type"] for f in inferred.declaration["entities"][0]["fields"]}
    assert fields["at"] == "string"
    assert any("ISO-8601" in n.what for n in inferred.notes)


def test_text_that_looks_like_a_uuid_stays_text() -> None:
    rows = [{"id": str(uuid.uuid4())}, {"id": str(uuid.uuid4())}]
    inferred = sde.infer_model(rows, entity="Thing")
    fields = {f["name"]: f["type"] for f in inferred.declaration["entities"][0]["fields"]}
    assert fields["id"] == "string"
    assert any("looks like a UUID" in n.what for n in inferred.notes)


def test_a_key_is_not_inferred_from_uniqueness_in_a_sample() -> None:
    """Five distinct values is evidence of nothing, and a key chosen that way starts rejecting
    writes in week three."""
    inferred = sde.infer_model(_weather(), entity="Reading")
    assert "key" not in inferred.declaration["entities"][0]
    assert inferred.candidate_keys["Reading"]


def test_every_column_unique_is_reported_as_uninformative_rather_than_as_a_list() -> None:
    """A candidate list containing every column invites somebody to pick the first entry.

    Same shape as a placement score of 1.0000 from a registry with one engine: technically true,
    and printed alone it supports a reading it does not justify.
    """
    inferred = sde.infer_model(_weather(), entity="Reading")
    summary = inferred.summary()
    assert "every column is unique" in summary
    assert "makes uniqueness free" in summary


def test_a_column_empty_in_every_row_is_blocking() -> None:
    rows = [{"note": None}, {"note": None}]
    inferred = sde.infer_model(rows, entity="Thing")
    assert any("no evidence of what it holds" in n.what for n in inferred.blocking)


def test_a_column_missing_from_some_rows_is_flagged_as_a_modelling_decision() -> None:
    rows = [{"a": 1, "b": 2}, {"a": 3}]
    inferred = sde.infer_model(rows, entity="Thing")
    assert any("genuinely optional" in n.what for n in inferred.notes)


def test_an_unmappable_python_type_is_blocking_rather_than_stored_as_text() -> None:
    """A fallback to text would store it and lose it."""
    rows = [{"weird": {1, 2}}, {"weird": {3}}]
    inferred = sde.infer_model(rows, entity="Thing")
    assert any("has no neutral type" in n.what for n in inferred.blocking)


def test_no_rows_is_refused() -> None:
    """An empty declaration builds without error and places nothing."""
    with pytest.raises(DeclarationError, match="no sample rows"):
        sde.infer_model([], entity="Thing")


def test_a_positional_row_is_refused() -> None:
    with pytest.raises(DeclarationError, match="not a mapping"):
        sde.infer_model([("WAW", 21)], entity="Reading")  # type: ignore[list-item]


def test_the_sample_limit_is_respected_and_recorded() -> None:
    """Every claim in the result is conditional on this number, so it travels with it."""
    rows = [{"n": index} for index in range(5000)]
    inferred = sde.infer_model(rows, entity="Thing", sample_limit=10)
    assert inferred.sample_size == 10
    assert "10 row(s)" in inferred.summary()


def test_several_entities_in_one_declaration_take_the_smallest_sample() -> None:
    """A mean would hide an entity inferred from two rows."""
    inferred = sde.infer_models(
        {
            "Reading": _weather(),
            "Station": [{"code": "WAW", "country": "PL"} for _ in range(50)],
        }
    )
    names = [e["name"] for e in inferred.declaration["entities"]]
    assert names == ["Reading", "Station"]
    assert inferred.sample_size == 2


def test_relations_are_not_inferred() -> None:
    """A coincidence of two identifier spaces looks exactly like a foreign key.

    A relation merges two entities into one colocation group, so guessing one wrong changes which
    engine both of them live in.
    """
    inferred = sde.infer_models(
        {
            "Order": [{"id": 1, "user": 7}, {"id": 2, "user": 8}],
            "User": [{"id": 7}, {"id": 8}],
        }
    )
    assert inferred.declaration["relations"] == []
    assert inferred.declaration["atomic"] == []


def test_the_declaration_builds_into_a_model_once_a_key_exists() -> None:
    """The output is meant to be edited by a person and then used, so the loop has to close."""
    from sde.testing.loader import model_from_neutral

    inferred = sde.infer_model(_weather(), entity="Reading")
    declaration = inferred.declaration
    declaration["entities"][0]["key"] = ["station", "at"]

    sde.clear_registry()
    model = model_from_neutral(declaration)
    assert model.version
    assert [e.name for e in model.entities] == ["Reading"]

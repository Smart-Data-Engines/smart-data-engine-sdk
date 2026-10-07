"""Section 8b, point 4: a value its field's type does not hold is refused before any engine.

Measured on 7 October 2026 through ``Session.save`` on PostgreSQL 15 and ClickHouse 24.8, before
this rule: a decimal rounded by one engine was truncated by the other, an integer outside int32
wrapped to the opposite sign in ClickHouse from TypeScript, a date that does not exist moved two
days. The shared vectors pin the refusals and the form a value reaches an engine in
(``errors/113``-``120``, ``migration/200``); these pin each type's boundaries, here and in
``admission.test.ts`` alike.
"""

from __future__ import annotations

import math
import uuid
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest

import sde
from sde.admission import Misfit, at_scale, check_for
from sde.errors import BulkWriteRefused, ModelPlanningError
from sde.hashing import hash_identifiers
from sde.testing.loader import model_from_neutral
from sde.testing.memory import engines_from


def admits(kind: str, value: Any) -> Any:
    check = check_for(kind)
    assert check is not None
    return check(value)


def refusal(kind: str, value: Any) -> str:
    check = check_for(kind)
    assert check is not None
    with pytest.raises(Misfit) as raised:
        check(value)
    return str(raised.value)


# --- decimal ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("given", "held"),
    [
        (Decimal("1.23"), "1.23"),
        ("1.230", "1.23"),
        ("123E-2", "1.23"),
        ("1E+3", "1000.00"),
        ("-0.00", "0.00"),
        ("0E+100", "0.00"),
        (" .5 ", "0.50"),
        ("+1.5", "1.50"),
        ("9999999999.99", "9999999999.99"),
        ("-9999999999.99", "-9999999999.99"),
        (Decimal("1.230000000000000000000000000000000"), "1.23"),
    ],
)
def test_a_decimal_that_fits_reaches_the_engine_at_its_scale(given: Any, held: str) -> None:
    got = admits("decimal(12,2)", given)
    assert isinstance(got, Decimal)
    assert format(got, "f") == held


def test_an_integer_given_to_a_decimal_stays_the_integer() -> None:
    given = 7
    assert admits("decimal(12,2)", given) is given
    assert admits("decimal(12,2)", -(10**10) + 1) == -(10**10) + 1


@pytest.mark.parametrize(
    ("given", "why"),
    [
        ("1.239", "more than 2 fractional digits"),
        ("0.001", "more than 2 fractional digits"),
        ("9999999999.995", "more than 2 fractional digits"),
        # Both misfit: the fractional digits are named first, in every library.
        ("12345678901.239", "more than 2 fractional digits"),
        ("12345678901.23", "more than 10 integer digits"),
        ("1E+10", "more than 10 integer digits"),
        (10**10, "more than 10 integer digits"),
        ("1e999999999999999999", "more than 10 integer digits"),
        ("1e-1999999999999999997", "more than 2 fractional digits"),
        # Past the reference's own limits its Decimal() calls the text invalid.
        ("1e1000000000000000000", "a value that is not an exact decimal"),
        ("1e-1999999999999999998", "a value that is not an exact decimal"),
        ("NaN", "a value that is not an exact decimal"),
        ("Infinity", "a value that is not an exact decimal"),
        (Decimal("NaN"), "a value that is not an exact decimal"),
        (Decimal("-Infinity"), "a value that is not an exact decimal"),
        (1.5, "a value that is not an exact decimal"),
        (0.1 + 0.2, "a value that is not an exact decimal"),
        (True, "a value that is not an exact decimal"),
        ("1_000.5", "a value that is not an exact decimal"),
        ("", "a value that is not an exact decimal"),
        ([], "a value that is not an exact decimal"),
    ],
)
def test_a_decimal_that_does_not_fit_is_refused_by_class(given: Any, why: str) -> None:
    assert refusal("decimal(12,2)", given) == why


def test_the_edges_of_a_decimal_with_no_integer_or_no_fractional_digits() -> None:
    assert format(admits("decimal(2,2)", "0.99"), "f") == "0.99"
    assert refusal("decimal(2,2)", "1.5") == "more than 0 integer digits"
    assert format(admits("decimal(5,0)", "12345"), "f") == "12345"
    assert refusal("decimal(5,0)", "1.5") == "more than 0 fractional digits"
    assert refusal("decimal(5,0)", "123456") == "more than 5 integer digits"


def test_at_scale_writes_plain_notation_with_the_columns_digits() -> None:
    assert at_scale(False, 123, -2, 2) == "1.23"
    assert at_scale(True, 5, -1, 2) == "-0.50"
    assert at_scale(False, 1, 3, 2) == "1000.00"
    assert at_scale(False, 0, 0, 0) == "0"
    assert at_scale(False, 1, 3, 0) == "1000"


# --- integers -----------------------------------------------------------------------------------


def test_integers_inside_their_type_are_admitted_as_given() -> None:
    for kind, low, high in (("int32", -(2**31), 2**31 - 1), ("int64", -(2**63), 2**63 - 1)):
        assert admits(kind, low) == low
        assert admits(kind, high) == high


@pytest.mark.parametrize(
    ("kind", "given", "why"),
    [
        ("int32", 2**31, "an integer outside int32"),
        ("int32", -(2**31) - 1, "an integer outside int32"),
        ("int64", 2**63, "an integer outside int64"),
        ("int64", -(2**63) - 1, "an integer outside int64"),
        ("int32", True, "a value that is not an integer"),
        ("int64", False, "a value that is not an integer"),
        ("int32", 1.0, "a value that is not an integer"),
        ("int32", 1.5, "a value that is not an integer"),
        ("int32", "7", "a value that is not an integer"),
        ("int64", Decimal(7), "a value that is not an integer"),
    ],
)
def test_an_integer_its_type_does_not_hold_is_refused(kind: str, given: Any, why: str) -> None:
    assert refusal(kind, given) == why


# --- floats -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "given",
    [1.1, 3.4028234663852886e38, -3.4028234663852886e38, 1e-45, 0.0, -0.0, math.inf, -math.inf],
)
def test_a_number_float32_holds_is_admitted(given: float) -> None:
    assert admits("float32", given) == given


def test_nan_is_a_float_value_in_both_widths() -> None:
    assert math.isnan(admits("float32", math.nan))
    assert math.isnan(admits("float64", math.nan))


@pytest.mark.parametrize("given", [3.4028235677973366e38, -3.5e38, 1e39, 1e-46, -1e-50])
def test_a_number_float32_rounds_to_an_infinity_or_to_zero_is_refused(given: float) -> None:
    assert refusal("float32", given) == "a number outside float32"


def test_floats_take_integers_and_refuse_other_kinds() -> None:
    assert admits("float64", 7) == 7.0
    assert isinstance(admits("float64", 7), float)
    assert admits("float64", 1e308) == 1e308
    assert refusal("float64", 10**400) == "a number outside float64"
    for given in (True, "1.5", Decimal("1.5"), [1.5]):
        assert refusal("float64", given) == "a value that is not a number"


# --- the types whose rule is a filter's ---------------------------------------------------------


def test_bool_string_and_bytes() -> None:
    assert admits("bool", True) is True
    assert refusal("bool", 1) == "a value that is not a boolean"
    assert refusal("bool", "true") == "a value that is not a boolean"
    assert admits("string", "é") == "é"
    assert refusal("string", 7) == "a value that is not text"
    assert refusal("string", "\ud800") == "a value that is not text"
    assert admits("bytes", bytearray(b"ab")) == b"ab"
    assert refusal("bytes", "ab") == "a value that is not bytes"


def test_uuid_and_date() -> None:
    token = uuid.UUID("0e984725-c51c-4bf4-9960-e1c80e27aba0")
    assert admits("uuid", token) is token
    assert admits("uuid", "0E984725-C51C-4BF4-9960-E1C80E27ABA0") == token
    assert refusal("uuid", "not-a-uuid") == "a value that is not a UUID"
    assert refusal("uuid", 7) == "a value that is not a UUID"
    assert admits("date", date(2026, 10, 7)) == date(2026, 10, 7)
    assert admits("date", "2026-10-07") == date(2026, 10, 7)
    assert refusal("date", "2026-02-30") == "a value that is not a date"
    assert refusal("date", datetime(2026, 10, 7, tzinfo=UTC)) == "a value that is not a date"
    assert refusal("date", 7) == "a value that is not a date"


def test_timestamps_are_instants_in_utc_and_wall_times_in_utc() -> None:
    two = timezone(timedelta(hours=2))
    aware = datetime(2026, 11, 9, 11, 30, 15, 123456, tzinfo=two)
    utc = datetime(2026, 11, 9, 9, 30, 15, 123456, tzinfo=UTC)
    assert admits("timestamptz", aware) == utc
    assert admits("timestamptz", "2026-11-09T11:30:15.123456+02:00") == utc
    assert admits("timestamptz", utc.replace(tzinfo=None)) == utc
    assert admits("timestamp", aware) == utc.replace(tzinfo=None)
    assert admits("timestamp", "2026-11-09 09:30:15") == datetime(2026, 11, 9, 9, 30, 15)
    for given in ("2026-10-07T25:00:00Z", "2026-10-07T10:00:00.1234567Z", "not a time", 7):
        assert refusal("timestamptz", given) == "a value that is not a timestamp"


def test_json_is_not_checked_yet() -> None:
    assert check_for("json") is None


# --- in a session -------------------------------------------------------------------------------

ENTRY: dict[str, Any] = {
    "entities": [
        {
            "name": "Entry",
            "fields": [
                {"name": "amount", "type": "decimal(12,2)"},
                {"name": "count", "type": "int32", "nullable": True},
                {"name": "id", "type": "string"},
            ],
            "key": ["id"],
        }
    ]
}


def _session(*, hashed: bool = False) -> tuple[sde.Session, Any]:
    model = model_from_neutral(ENTRY)
    names = None
    if hashed:
        model, names = hash_identifiers(model, b"admission-salt-for-a-test-only!!")
    raw: dict[str, Any] = {
        "contract": 3,
        "model_version": model.version,
        "map_version": 1,
        "groups": {
            group.name: {
                "source": {"id": "source", "engine": "pg-main", "layout": {"auto": True}}
            }
            for group in sde.colocation_groups(model)
        },
    }
    engines = engines_from({"pg-main": {"dialect": "postgres"}})
    session = sde.Session(model, sde.load_map(raw, model=model), engines, names=names)
    return session, engines["pg-main"]


def test_a_save_is_refused_before_the_engine_and_names_the_type() -> None:
    session, engine = _session()
    with pytest.raises(ModelPlanningError) as raised:
        session.save("Entry", {"amount": "1.239", "count": 1, "id": "e-1"})
    assert type(raised.value) is ModelPlanningError
    assert str(raised.value) == (
        "Entry.amount is decimal(12,2) and this row gives it more than 2 fractional digits"
    )
    assert engine.recorded.as_list() == []


def test_the_engine_receives_the_value_at_its_scale_and_the_integer_as_given() -> None:
    session, engine = _session()
    session.save("Entry", {"amount": "1.230", "count": None, "id": "e-1"})
    session.save("Entry", {"amount": 7, "count": 2, "id": "e-2"})
    rows = {row["id"]: row for row in next(iter(engine.tables.values()))}
    assert rows["e-1"]["amount"] == Decimal("1.23")
    assert format(rows["e-1"]["amount"], "f") == "1.23"
    assert rows["e-1"]["count"] is None
    assert type(rows["e-2"]["amount"]) is int


def test_a_batch_is_refused_whole_with_the_rows_position() -> None:
    session, engine = _session()
    with pytest.raises(BulkWriteRefused) as raised:
        session.save_many(
            "Entry",
            [
                {"amount": "1.23", "count": 1, "id": "e-1"},
                {"amount": "1.23", "count": 2**31, "id": "e-2"},
            ],
        )
    assert str(raised.value) == (
        "row 1: Entry.count is int32 and this row gives it an integer outside int32"
    )
    assert engine.recorded.as_list() == []


def test_a_refusal_names_the_clients_field_under_hashed_identifiers() -> None:
    session, engine = _session(hashed=True)
    with pytest.raises(ModelPlanningError) as raised:
        session.save("Entry", {"amount": "1.23", "count": True, "id": "e-1"})
    assert str(raised.value) == (
        "Entry.count is int32 and this row gives it a value that is not an integer"
    )
    assert engine.recorded.as_list() == []

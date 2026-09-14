"""Exact summaries must not depend on the process Decimal context or narrow native sums."""

from __future__ import annotations

from decimal import Decimal, localcontext

import pytest

from sde.query import ReadColumn, numeric_summary


@pytest.mark.parametrize(
    "total,present,scale,expected",
    [
        ("1", "2", 0, "0"),
        ("3", "2", 0, "2"),
        ("-1", "2", 0, "-0"),
        ("-3", "2", 0, "-2"),
        ("1", "3", 6, "0.333333"),
        ("2", "3", 6, "0.666667"),
        ("-1", "3", 6, "-0.333333"),
        ("-2", "3", 6, "-0.666667"),
        ("18446744073709551614", "2", 6, "9223372036854775807.000000"),
    ],
)
def test_exact_half_even_mean_is_independent_of_decimal_context(
    total: str,
    present: str,
    scale: int,
    expected: str,
) -> None:
    with localcontext() as context:
        context.prec = 2
        result = numeric_summary(
            {
                "sde_count": present,
                "sde_present": present,
                "sde_min": "0",
                "sde_max": total,
                "sde_total": total,
            },
            ReadColumn("value", "int64"),
            scale,
        )
    assert str(result.mean) == expected
    assert result.total == int(total)


def test_decimal_mean_keeps_input_scale_and_exact_large_sum() -> None:
    text = "199999999999999999999999999999999999998.246913578024691356"
    with localcontext() as context:
        context.prec = 2
        result = numeric_summary(
            {
                "sde_count": "3",
                "sde_present": "2",
                "sde_min": "0.00",
                "sde_max": "99999999999999999999999999999999999999.123456789012345678",
                "sde_total": text,
            },
            ReadColumn("value", "decimal(56,18)"),
            18,
        )
    assert result.total == Decimal(text)
    assert str(result.mean) == "99999999999999999999999999999999999999.123456789012345678"


def test_no_values_normalizes_native_empty_extrema_and_sum() -> None:
    result = numeric_summary(
        {"sde_count": "3", "sde_present": "0", "sde_min": "0", "sde_max": "0", "sde_total": "0"},
        ReadColumn("value", "int64"),
        6,
    )
    assert result.count == 3 and result.non_null_count == 0
    assert result.minimum is result.maximum is result.total is result.mean is None

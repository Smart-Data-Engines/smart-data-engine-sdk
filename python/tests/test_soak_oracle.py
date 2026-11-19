"""The soak's oracle on rows made up here: each breach it exists to find, and a clean run passes.

Every test starts from the rows a correct run leaves and changes one thing, so a check that stops
looking fails its test while the clean run - the control - keeps passing.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

from qualification import soak_traffic as traffic
from qualification.soak_oracle import Record, judge_orders, judge_readings

LANGUAGE = "python"


def postgres_run(record: Record) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The rows a correct run leaves: every acknowledged write, no failed one."""
    orders, fills = [], []
    for number in range(record.attempted + 1):
        if not record.acknowledged(number):
            continue
        orders.append(traffic.order(LANGUAGE, number))
        if traffic.is_transaction(number):
            fills.extend(traffic.fills(LANGUAGE, number))
    return orders, fills


def clickhouse_run(record: Record) -> list[dict[str, Any]]:
    return [
        row
        for number in range(record.attempted + 1)
        if record.acknowledged(number)
        for row in traffic.batch(LANGUAGE, number)
    ]


RECORD = Record(attempted=30, highest_acked=29, failed={4, 9, 30})


def test_a_clean_postgres_run_passes() -> None:
    orders, fills = postgres_run(RECORD)
    verdict = judge_orders(LANGUAGE, RECORD, orders, fills).as_dict()
    assert verdict["passed"], verdict["breaches"]
    assert verdict["acknowledged"] == verdict["acknowledged_present"] == 28
    assert (verdict["failed"], verdict["failed_absent"]) == (3, 3)


def test_a_failed_write_may_be_present_with_its_values() -> None:
    orders, fills = postgres_run(RECORD)
    orders.append(traffic.order(LANGUAGE, 4))
    verdict = judge_orders(LANGUAGE, RECORD, orders, fills).as_dict()
    assert verdict["passed"], verdict["breaches"]
    assert verdict["failed_present"] == 1


def test_a_missing_acknowledged_write_is_a_breach() -> None:
    orders, fills = postgres_run(RECORD)
    orders = [row for row in orders if traffic.order_number(row) != 7]
    verdict = judge_orders(LANGUAGE, RECORD, orders, fills)
    assert [b["kind"] for b in verdict.breaches] == ["acknowledged write missing"]


def test_a_value_other_than_the_one_written_is_a_breach() -> None:
    orders, fills = postgres_run(RECORD)
    orders[5] = {**orders[5], "price": orders[5]["price"] + Decimal("0.00000001")}
    verdict = judge_orders(LANGUAGE, RECORD, orders, fills)
    assert [b["kind"] for b in verdict.breaches] == ["value differs"]


def test_a_transaction_in_part_is_a_breach_acknowledged_or_not() -> None:
    orders, fills = postgres_run(RECORD)
    fills = [row for row in fills if row["at"] != traffic.fills(LANGUAGE, 6)[1]["at"]]
    fills.append(traffic.fills(LANGUAGE, 9)[0])  # a failed transaction, one fill of it
    verdict = judge_orders(LANGUAGE, RECORD, orders, fills)
    kinds = sorted((b["kind"], b["number"]) for b in verdict.breaches)
    assert kinds == [
        ("acknowledged write missing", 6),
        ("partial transaction", 6),
        ("partial transaction", 9),
    ]


def test_a_row_above_the_highest_attempted_is_a_breach() -> None:
    orders, fills = postgres_run(RECORD)
    orders.append(traffic.order(LANGUAGE, 31))
    verdict = judge_orders(LANGUAGE, RECORD, orders, fills)
    assert [(b["kind"], b["number"]) for b in verdict.breaches] == [("beyond attempted", 31)]


def test_a_naive_timestamp_from_a_driver_is_utc() -> None:
    orders, fills = postgres_run(RECORD)
    naive = [{**row, "placed_at": row["placed_at"].replace(tzinfo=None)} for row in orders]
    verdict = judge_orders(LANGUAGE, RECORD, naive, fills).as_dict()
    assert verdict["passed"], verdict["breaches"]


def test_a_clean_clickhouse_run_passes() -> None:
    verdict = judge_readings(LANGUAGE, RECORD, clickhouse_run(RECORD), {}).as_dict()
    assert verdict["passed"], verdict["breaches"]
    assert verdict["acknowledged_present"] == 28


def test_a_batch_in_part_is_a_breach() -> None:
    rows = clickhouse_run(RECORD)
    rows = [row for row in rows if row["at"] != traffic.reading(LANGUAGE, 3 * traffic.BATCH)["at"]]
    verdict = judge_readings(LANGUAGE, RECORD, rows, {})
    assert sorted((b["kind"], b["number"]) for b in verdict.breaches) == [
        ("acknowledged write missing", 3),
        ("partial batch", 3),
    ]


def test_a_reading_with_other_values_is_a_breach() -> None:
    rows = clickhouse_run(RECORD)
    rows[45] = {**rows[45], "humidity": rows[45]["humidity"] + 1}
    verdict = judge_readings(LANGUAGE, RECORD, rows, {})
    assert [(b["kind"], b["number"]) for b in verdict.breaches] == [("value differs", 2)]


def test_a_key_with_two_physical_rows_is_a_write_sent_twice() -> None:
    reading = traffic.reading_number(traffic.reading(LANGUAGE, 5 * traffic.BATCH + 3))
    verdict = judge_readings(LANGUAGE, RECORD, clickhouse_run(RECORD), {reading: 2, reading + 1: 1})
    assert [(b["kind"], b["number"]) for b in verdict.breaches] == [("written twice", 5)]


def test_a_batch_above_the_highest_attempted_is_a_breach() -> None:
    rows = clickhouse_run(RECORD) + traffic.batch(LANGUAGE, 40)
    verdict = judge_readings(LANGUAGE, RECORD, rows, {})
    assert [(b["kind"], b["number"]) for b in verdict.breaches] == [("beyond attempted", 40)]


def test_order_numbers_come_back_from_their_time() -> None:
    for number in (0, 1, 999_999, 8_640_000):
        assert traffic.order_number(traffic.order(LANGUAGE, number)) == number
    late = traffic.order(LANGUAGE, 3)["placed_at"] + timedelta(microseconds=1)
    try:
        traffic.order_number({"placed_at": late})
    except ValueError:
        return
    raise AssertionError("a time between orders was read as an order")

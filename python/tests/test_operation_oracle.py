"""The benchmark oracle rejects changed values, missing rows and extra rows across pages."""

from copy import deepcopy
from types import SimpleNamespace
from typing import Any

import pytest
from qualification.operations import verify
from qualification.weather_worker import reading


class Pages:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.offset = 0

    def scan(self, _entity: str, **kwargs: Any) -> Any:
        assert kwargs["fresh"] is True
        assert kwargs["where"] == {"station": reading(1, 1)["station"]}
        page = self.rows[self.offset : self.offset + 2]
        self.offset += len(page)
        more = self.offset < len(self.rows)
        return SimpleNamespace(rows=page, next_after={"at": self.offset} if more else None)


def test_every_source_value_is_checked_across_pages() -> None:
    reader: Any = Pages([reading(1, n) for n in range(1, 4)])
    verify(reader, 1, 3)
    assert reader.offset == 3


@pytest.mark.parametrize(
    "damage", ["station", "at", "celsius", "humidity", "id", "missing", "extra"]
)
def test_count_agreement_cannot_hide_value_corruption(damage: str) -> None:
    rows = [reading(1, n) for n in range(1, 4)]
    if damage == "missing":
        rows.pop()
    elif damage == "extra":
        rows.append(reading(1, 4))
    else:
        rows = deepcopy(rows)
        rows[1][damage] = None
    reader: Any = Pages(rows)
    with pytest.raises(AssertionError, match="values"):
        verify(reader, 1, 3)

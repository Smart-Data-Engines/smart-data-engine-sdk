"""The fixed, portable model used by the customer Weather starter."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import sde
from sde.testing.loader import model_from_neutral

DECLARATION: dict[str, Any] = {
    "entities": [
        {
            "name": "WeatherReading",
            "fields": [
                {"name": "at", "type": "timestamptz"},
                {"name": "celsius", "type": "decimal(8,2)"},
                {"name": "humidity", "type": "int64"},
                {"name": "id", "type": "uuid"},
                {"name": "station", "type": "string"},
            ],
            "key": ["station", "at"],
            "residency": "EU",
        }
    ],
    "relations": [],
    "atomic": [],
    "cost_ceiling": {"amount": "500.00", "currency": "EUR"},
}
BASE_TIME = datetime(2026, 1, 1, tzinfo=UTC)


def model() -> sde.LogicalModel:
    return model_from_neutral(DECLARATION)


def reading(run_id: str, worker: int, sequence: int) -> dict[str, Any]:
    """Deterministic synthetic values; the run namespace prevents keys crossing runs."""
    identity = hashlib.sha256(f"sde-weather-v1:{run_id}:{worker}:{sequence}".encode()).digest()[:16]
    cents = 1525 + sequence % 1000
    return {
        "station": f"weather-{run_id}-{worker}",
        "at": BASE_TIME + timedelta(microseconds=sequence),
        "celsius": Decimal(f"{cents // 100}.{cents % 100:02d}"),
        "humidity": 30 + sequence % 70,
        "id": UUID(bytes=identity, version=4),
    }

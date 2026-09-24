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
STATION_TEMPLATE = "weather-{run_id}-{worker}"
SEED_TEMPLATE = "sde-weather-v1:{run_id}:{worker}:{sequence}"
CELSIUS_BASE_CENTS, CELSIUS_MODULUS = 1525, 1000
HUMIDITY_BASE, HUMIDITY_MODULUS = 30, 70
UUID_VERSION = 4
WORKLOADS = ("mixed", "point", "analytics", "fleet")
"""What a run can drive. The CLI offers and the runtime accepts this one tuple."""
GENERATOR_SPEC = {
    "kind": "sde-weather-generator",
    "version": 1,
    "worker": 0,
    "station_template": STATION_TEMPLATE,
    "seed_template": SEED_TEMPLATE,
    "base_time": BASE_TIME.isoformat(timespec="microseconds").replace("+00:00", "Z"),
    "timestamp_step_microseconds": 1,
    "celsius_base_cents": CELSIUS_BASE_CENTS,
    "celsius_modulus": CELSIUS_MODULUS,
    "humidity_base": HUMIDITY_BASE,
    "humidity_modulus": HUMIDITY_MODULUS,
    "id": "first 16 bytes of SHA256(UTF8(seed)), RFC4122 UUID version bits",
    "uuid_version": UUID_VERSION,
}
GENERATOR_ID = "weather-v1:" + hashlib.sha256(sde.canonical_bytes(GENERATOR_SPEC)).hexdigest()


def model() -> sde.LogicalModel:
    return model_from_neutral(DECLARATION)


def reading(run_id: str, worker: int, sequence: int) -> dict[str, Any]:
    """Deterministic synthetic values; the run namespace prevents keys crossing runs."""
    identity = hashlib.sha256(
        SEED_TEMPLATE.format(run_id=run_id, worker=worker, sequence=sequence).encode()
    ).digest()[:16]
    cents = CELSIUS_BASE_CENTS + sequence % CELSIUS_MODULUS
    return {
        "station": STATION_TEMPLATE.format(run_id=run_id, worker=worker),
        "at": BASE_TIME + timedelta(microseconds=sequence),
        "celsius": Decimal(f"{cents // 100}.{cents % 100:02d}"),
        "humidity": HUMIDITY_BASE + sequence % HUMIDITY_MODULUS,
        "id": UUID(bytes=identity, version=UUID_VERSION),
    }

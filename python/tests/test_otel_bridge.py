"""The OpenTelemetry bridge of docs/observability.md, run: a hook that makes spans and a histogram.

The library depends on no tracing package, so the bridge is the application's code. The block
between the markers below is the one the document shows; ``test_the_document_shows_this_code``
holds the two equal, so the document cites code that runs. The tracer and the histogram are
stand-ins with OpenTelemetry's shape - ``start_span(name, start_time=, attributes=)``,
``set_status``, ``end(end_time=)``, ``record(value, attributes=)`` - so nothing is installed for it.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import sde
from sde.hook import OperationRecord

ROOT = Path(__file__).resolve().parents[2]


@dataclass
class Span:
    name: str
    start_time: int
    attributes: dict[str, Any]
    status: tuple[str, str] | None = None
    end_time: int | None = None

    def set_status(self, status: Any) -> None:
        self.status = (status.status_code, status.description)

    def end(self, end_time: int) -> None:
        self.end_time = end_time


@dataclass
class Tracer:
    spans: list[Span] = field(default_factory=list)

    def start_span(self, name: str, *, start_time: int, attributes: dict[str, Any]) -> Span:
        span = Span(name, start_time, attributes)
        self.spans.append(span)
        return span


@dataclass
class Histogram:
    values: list[tuple[float, dict[str, Any]]] = field(default_factory=list)

    def record(self, value: float, attributes: dict[str, Any]) -> None:
        self.values.append((value, attributes))


@dataclass
class Status:
    status_code: str
    description: str


class StatusCode:
    ERROR = "ERROR"


# bridge:begin
def opentelemetry_hook(tracer: Any, histogram: Any) -> sde.OperationHook:
    """A span and a histogram value for each operation the session completes."""

    def hook(record: sde.OperationRecord) -> None:
        end = time.time_ns()
        span = tracer.start_span(
            f"sde.{record.operation}",
            start_time=end - record.duration_ns,
            attributes={
                "db.system": record.dialect,
                "sde.entity": record.entity,
                "sde.group": record.group,
                "sde.shape": record.shape,
                "sde.engine": record.engine,
                "sde.materialization": record.materialization,
                "sde.rows": record.rows,
            },
        )
        if record.outcome != "ok":
            span.set_status(Status(StatusCode.ERROR, record.outcome))
        span.end(end_time=end)
        histogram.record(
            record.duration_ns / 1e9,
            attributes={
                "sde.operation": record.operation,
                "db.system": record.dialect,
                "sde.outcome": record.outcome,
            },
        )

    return hook
# bridge:end


def record(**changes: Any) -> OperationRecord:
    base: dict[str, Any] = {
        "operation": "get", "entity": "Note", "group": "Note", "shape": "a1b2c3",
        "engine": "pg-main", "dialect": "postgres", "materialization": "notes@pg",
        "duration_ns": 1_500_000, "rows": 1, "outcome": "ok",
    }  # fmt: skip
    return OperationRecord(**{**base, **changes})


def test_a_record_is_a_span_that_ends_now_and_began_its_duration_before() -> None:
    tracer, histogram = Tracer(), Histogram()
    hook = opentelemetry_hook(tracer, histogram)
    hook(record())
    [span] = tracer.spans
    assert span.name == "sde.get"
    assert span.end_time is not None and span.end_time - span.start_time == 1_500_000
    assert span.attributes["db.system"] == "postgres"
    assert span.attributes["sde.materialization"] == "notes@pg"
    assert span.status is None
    assert histogram.values == [
        (0.0015, {"sde.operation": "get", "db.system": "postgres", "sde.outcome": "ok"})
    ]


def test_an_error_is_the_span_status_with_its_class() -> None:
    tracer, histogram = Tracer(), Histogram()
    opentelemetry_hook(tracer, histogram)(record(outcome="EngineError", rows=0))
    assert tracer.spans[0].status == ("ERROR", "EngineError")
    assert histogram.values[0][1]["sde.outcome"] == "EngineError"


def test_the_document_shows_this_code() -> None:
    source = Path(__file__).read_text(encoding="utf-8")
    ours = source.split("# bridge:begin\n")[1].split("# bridge:end")[0]
    document = (ROOT / "docs" / "observability.md").read_text(encoding="utf-8")
    shown = re.search(r"<!-- bridge:python -->\n```python\n(.*?)```", document, re.DOTALL)
    assert shown is not None, "docs/observability.md shows no Python bridge"
    assert code(shown.group(1)) == code(ours)


def code(text: str) -> str:
    """The code without its imports, which the document states and this file has at its top."""
    lines = [line for line in text.splitlines() if not line.startswith(("import ", "from "))]
    return "\n".join(lines).strip()

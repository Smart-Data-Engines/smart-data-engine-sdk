"""Exercise the exact CLI used by CI, including misleading human and XML summaries."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

GUARD = Path(__file__).resolve().parents[2] / "tools" / "check_junit.py"


@pytest.mark.parametrize(
    ("body", "accepted"),
    [
        ('<testsuites><testsuite><testcase name="native"/></testsuite></testsuites>', True),
        ('<testsuite><testcase name="native"><skipped/></testcase></testsuite>', False),
        (
            '<testsuite><testcase name="unit"/>'
            '<testcase name="native"><skipped/></testcase></testsuite>',
            False,
        ),
        ('<testsuite><testcase name="native"><failure/></testcase></testsuite>', False),
        ('<testsuite><testcase name="native"><error/></testcase></testsuite>', False),
        ('<testsuite tests="100" failures="0" skipped="0"/>', False),
        ("<not-xml", False),
        (None, False),
    ],
    ids=[
        "passed",
        "skipped",
        "mixed-skips",
        "failed",
        "error",
        "empty-with-counters",
        "malformed",
        "missing",
    ],
)
def test_ci_uses_actual_test_cases(tmp_path: Path, body: str | None, accepted: bool) -> None:
    report = tmp_path / "integration.xml"
    if body is not None:
        report.write_text(body)
    result = subprocess.run(
        [sys.executable, str(GUARD), str(report)], capture_output=True, text=True
    )
    assert result.returncode == (0 if accepted else 1), result.stdout + result.stderr

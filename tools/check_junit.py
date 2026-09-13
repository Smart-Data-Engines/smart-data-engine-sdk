#!/usr/bin/env python3
"""Require a nonempty JUnit integration report containing only successful test cases.

Summary text can contain a progress line without the word 'skipped' even when every actual case
was skipped. Read the report that represents those cases; the suite's summary counters alone are
also insufficient because they can claim work absent from the document.
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def check(path: Path) -> int:
    try:
        cases = list(ET.parse(path).iter("testcase"))
    except (OSError, ET.ParseError) as exc:
        print(f"cannot read integration report: {exc}", file=sys.stderr)
        return 1
    if not cases:
        print("integration report contains no test cases", file=sys.stderr)
        return 1
    rejected = [
        case.get("name", "<unnamed>")
        for case in cases
        if any(case.find(kind) is not None for kind in ("skipped", "failure", "error"))
    ]
    if rejected:
        print(f"integration report contains skipped or failing cases: {rejected}", file=sys.stderr)
        return 1
    print(f"{len(cases)} integration cases passed; none skipped")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: check_junit.py REPORT.xml", file=sys.stderr)
        return 2
    return check(Path(argv[1]))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

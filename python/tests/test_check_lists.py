"""CI checks what ``make check`` checks: each lint and type-check line names the same files.

The Makefile and the CI workflow each spell out, by hand, the files ruff, mypy and the type-checked
JavaScript workers are run on: the scripts under ``conformance/tools`` are not a package, and only
some of them are held to the library's rules. Two lists kept by hand drift, and these did, three
times: the generators of the advance packet (#185) and of the auto member (#188) and the
TypeScript operation worker went into the Makefile alone. A contributor's ``make check`` read them,
and CI, which decides what is merged, never did.

So each command is found on its one line in both files, and the words after the command - options
and files - have to be the same, in any order. A failure names what only one side has.
"""

from __future__ import annotations

import re
import shlex
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MAKEFILE = ROOT / "Makefile"
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"

# The pattern that finds a command's one line in each file, the word the command starts with, and
# one file the line has to name: the one that went unchecked in CI, where there was one.
COMMANDS = {
    "ruff": (r"\bruff check\b", "ruff", "../conformance/tools/advance_vectors.py"),
    "mypy": (r"\bmypy src\b", "mypy", "../conformance/tools/auto_member_vectors.py"),
    "tsc, strict null checks": (
        r"--checkJs --strictNullChecks ",
        "tsc",
        "tests/qualification/operation-worker.mjs",
    ),
    "tsc, strict": (r"--checkJs --strict ", "tsc", "tests/qualification/tls-worker.mjs"),
}


def _words(path: Path, pattern: str, command: str) -> Counter[str]:
    text = path.read_text(encoding="utf-8")
    lines = [line for line in text.splitlines() if re.search(pattern, line)]
    assert len(lines) == 1, f"{path.name}: {len(lines)} lines match {pattern!r}, expected one"
    (line,) = lines
    return Counter(shlex.split(line[line.index(command) :])[1:])


@pytest.mark.parametrize("name", COMMANDS)
def test_ci_runs_each_check_on_the_files_the_makefile_does(name: str) -> None:
    pattern, command, _ = COMMANDS[name]
    make = _words(MAKEFILE, pattern, command)
    ci = _words(WORKFLOW, pattern, command)
    assert make == ci, (
        f"{name}: only in the Makefile {sorted((make - ci).elements())}, "
        f"only in CI {sorted((ci - make).elements())}"
    )


@pytest.mark.parametrize("path", [MAKEFILE, WORKFLOW], ids=["Makefile", "ci.yml"])
@pytest.mark.parametrize("name", COMMANDS)
def test_each_line_is_read_to_its_files(name: str, path: Path) -> None:
    """The control: two lists that were not read compare equal too. Each line read here reaches
    its files, the one it was missing in CI among them."""
    pattern, command, named = COMMANDS[name]
    assert named in _words(path, pattern, command)

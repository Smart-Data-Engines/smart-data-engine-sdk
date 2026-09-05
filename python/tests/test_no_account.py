"""Requirement 12.6: in the no-account mode the library sends nothing and does not go on about it.

The requirement has two halves and the second one is the one that gets built wrong. "Sends
nothing" is a property of the code and is easy to believe; "does not warn about it in every log
line" is a property of a *stream*, and the natural way to satisfy it - remember not to write the
nagging line - is not a mechanism at all.

So the shape of the claim here is not "no line contains the word account". A denylist over prose
cannot tell using a word from mentioning one, which cost four attempts elsewhere in this project,
and it would trip on the one line that legitimately names the mode. The claim is about the stream:

    running without an account emits nothing that running with one does not, past a single line
    at load, and that line does not recur.

A nag is exactly a line that violates that, whatever its wording, and so is a line that appears
once per operation instead of once per process. The comparison needs no vocabulary of forbidden
words, and it stays true when somebody invents a new way to phrase the complaint.

Three structural claims sit underneath it, each closing off a way to complain that the stream
comparison would not see:

* the library has no channel louder than INFO - it cannot warn, in the sense the standard library
  gives that word, because it never emits above INFO and never writes to a stream of its own;
* every name in the published vocabulary is emitted by some code path, and every emitted name is
  in the vocabulary - a vocabulary entry no code produces is a documented alert that never fires,
  and this is the check that would have caught ``sde.telemetry.window_sent`` describing a
  transmission that does not happen;
* the set of modules this library imports is an allowlist, so a network client cannot arrive
  without a decision. The equivalent check in the control plane is a denylist of module names,
  which fails open on the module nobody thought of - and it lives in the private repo, which is
  the wrong place for the mechanism behind a promise a client is invited to verify by reading the
  public code.

The behavioural half - that not one connection is attempted - is in ``test_no_account_live.py``,
because proving an attempt did not happen needs an instrument that can be shown to see attempts.
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

import pytest

import sde
from sde.logging import EVENTS

SOURCE = Path(sde.__file__).resolve().parent


def _library_files() -> list[Path]:
    return sorted(SOURCE.rglob("*.py"))


# --- the library cannot warn ---------------------------------------------------------------------
#
# Written as a function over source text rather than inline over the package, so that the checker
# itself can be handed planted code. A test that passes by finding nothing has to be shown a case
# it catches, or it can quietly stop looking and stay green.


LOUD_LOGGING = frozenset(
    {"warning", "warn", "error", "critical", "exception", "log", "basicConfig", "captureWarnings"}
)
QUIET_LOGGER = frozenset({"info", "isEnabledFor"})


def loud_channels(source: str, *, where: str = "<planted>") -> list[str]:
    """Every way this source could say something louder than one INFO event.

    The list is short because the surface is: one module-level ``logger``, the ``logging`` module,
    ``print``, ``warnings`` and the two standard streams. ``getattr`` on the logger is included
    because a computed level is a level, and a rule that only reads attribute names would miss it.
    """
    found: list[str] = []
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id == "print":
                found.append(f"{where}:{node.lineno}: print()")
            if (
                isinstance(func, ast.Name)
                and func.id == "getattr"
                and node.args
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id == "logger"
            ):
                found.append(f"{where}:{node.lineno}: getattr(logger, ...)")
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            owner, attr = node.value.id, node.attr
            if owner == "logger" and attr not in QUIET_LOGGER:
                found.append(f"{where}:{node.lineno}: logger.{attr}")
            if owner == "logging" and attr in LOUD_LOGGING:
                found.append(f"{where}:{node.lineno}: logging.{attr}")
            if owner == "warnings" and attr in {"warn", "warn_explicit"}:
                found.append(f"{where}:{node.lineno}: warnings.{attr}")
            if owner == "sys" and attr in {"stderr", "stdout"}:
                found.append(f"{where}:{node.lineno}: sys.{attr}")
    return found


def test_the_library_has_no_channel_louder_than_one_info_event() -> None:
    """A library that prints is a library people vendor and patch; one that warns is worse.

    "Does not warn" is a claim with a precise meaning in Python: WARNING is a level, and a library
    that never emits above INFO cannot make one. That turns a promise about tone into a property
    of the code, and it holds for complaints nobody has thought of yet.
    """
    offenders = [
        item
        for path in _library_files()
        for item in loud_channels(
            path.read_text(encoding="utf-8"), where=str(path.relative_to(SOURCE))
        )
    ]
    assert not offenders, offenders


@pytest.mark.parametrize(
    "planted",
    [
        'logger.warning("no account configured")',
        'logging.warning("no account configured")',
        'print("running in the free mode")',
        'warnings.warn("no account configured")',
        'sys.stderr.write("no account configured\\n")',
        'getattr(logger, level)("no account configured")',
    ],
)
def test_the_checker_catches_each_way_of_being_loud(planted: str) -> None:
    """The other half. Without it, the check above passes just as well after it stops looking."""
    assert loud_channels(planted), planted


# --- the published vocabulary and the code that emits it -----------------------------------------


def _emitted_names() -> tuple[set[str], list[str]]:
    """Every event name passed to ``log()`` anywhere in the library, plus any that is not a literal.

    The second half keeps the first honest: a computed event name would make the static reading of
    this vocabulary silently incomplete, and the incompleteness would look like agreement.
    """
    names: set[str] = set()
    computed: list[str] = []
    for path in _library_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
                continue
            if node.func.id != "log" or not node.args:
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                names.add(first.value)
            else:
                computed.append(f"{path.relative_to(SOURCE)}:{node.lineno}")
    return names, computed


def test_every_event_name_is_a_literal_so_the_vocabulary_can_be_read_statically() -> None:
    _, computed = _emitted_names()
    assert not computed, computed


def test_the_vocabulary_and_the_code_that_emits_it_agree_in_both_directions() -> None:
    """A name in ``EVENTS`` that no code emits is a documented alert that will never fire.

    This is the check that was missing when ``sde.telemetry.window_sent`` sat in the vocabulary
    describing an outbound transmission: the name was published, ``EVENTS`` is in ``__all__``, and
    a client reading it to find out whether this library phones home found a line saying it did.
    The name has gone, but the class of defect is the point - the same hole let ``__all__`` name
    ``ALSO_WRITE_SINCE`` with no importer, past both ruff and mypy --strict.
    """
    emitted, _ = _emitted_names()
    # `log()` itself raises on an unknown name, but only on a path some test reaches. Static makes
    # it total, which is the difference between the two directions being checked and one of them.
    assert emitted - EVENTS == set(), f"emitted but not in the vocabulary: {emitted - EVENTS}"
    assert EVENTS - emitted == set(), f"in the vocabulary but never emitted: {EVENTS - emitted}"


# The direction this one fails in is worth stating: a verb nobody listed gets through. It is not
# the mechanism behind "nothing is sent" - the measurement in test_no_account_live.py is - it names
# the class of mistake that already happened once, so that the next one is caught at review time
# rather than by somebody reading a client's log.
CLAIMS_A_TRANSMISSION = ("sent", "send", "upload", "deliver", "report", "push", "phone", "call")
NAMES_THE_COMMERCE = (
    "account",
    "licen",
    "subscription",
    "quota",
    "trial",
    "expire",
    "expiry",
    "paid",
    "unpaid",
    "upgrade",
    "billing",
)


def _offending_words(names: frozenset[str] | set[str]) -> list[str]:
    return sorted(
        f"{name}: {word}"
        for name in names
        for word in CLAIMS_A_TRANSMISSION + NAMES_THE_COMMERCE
        if word in name
    )


def test_no_event_name_claims_a_transmission_or_names_the_account() -> None:
    """Event names are identifiers from a closed set we author, so there is no use-and-mention
    problem here - unlike the prose checks, where quoting a word had to be told apart from
    claiming it. A name containing "sent" is a claim, always.
    """
    assert _offending_words(EVENTS) == []


def test_that_word_check_would_have_caught_the_name_it_was_written_for() -> None:
    planted = {"sde.telemetry.window_sent", "sde.account.expired", "sde.map.loaded"}
    assert _offending_words(planted) == [
        "sde.account.expired: account",
        "sde.account.expired: expire",
        "sde.telemetry.window_sent: sent",
    ]


# --- nothing that could open a connection --------------------------------------------------------
#
# An allowlist, not a denylist. The control plane's equivalent lists the network clients it knows
# about, which is the shape that fails open on the day somebody imports the one nobody thought of -
# the same reason the control plane's own import boundary is an allowlist. Here the whole import
# surface is pinned, so any new import trips this and gets a decision, network or not.

MAY_IMPORT = frozenset(
    {
        # Standard library, none of it network-capable.
        "__future__",
        "abc",
        "ast",
        "base64",
        "collections",
        "contextlib",
        "contextvars",
        "dataclasses",
        "datetime",
        "decimal",
        "enum",
        "functools",
        "hashlib",
        "hmac",
        "importlib",
        "inspect",
        "io",
        "ipaddress",
        "itertools",
        "json",
        "logging",
        "math",
        "os",
        "pathlib",
        "re",
        "secrets",
        "string",
        "sys",
        "threading",
        "time",
        "types",
        "typing",
        "unicodedata",
        "uuid",
        "warnings",
        "zoneinfo",
    }
)

# The one thing here that is not standard library, and the one thing here that ships compiled code.
# Imported in exactly one place, for exactly one operation - verifying an Ed25519 signature on a
# map that carries one - and it is an optional extra, so a deployment in the account-free mode
# never installs it. It stays on the list rather than being excused, because the live measurement
# runs the real verification path with the audit hook installed: whether that path can reach a
# socket is a question this project answers by measuring, not by reading an API.
MAY_IMPORT_COMPILED = frozenset({"cryptography"})


def _imported_top_level(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        # A relative import is our own package and says nothing about the outside world.
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return names


def test_the_import_surface_outside_the_engine_adapters_is_an_allowlist() -> None:
    """Why there is nothing to send with, and why a project key would have nothing to authenticate.

    The engine adapters are excluded because connecting to the client's engines is the whole
    architecture. What must not exist is a client of *ours* in the client's process.
    """
    offenders: list[str] = []
    for path in _library_files():
        if path.parts[-2] == "engines":
            continue
        allowed = MAY_IMPORT | MAY_IMPORT_COMPILED
        for name in sorted(_imported_top_level(path) - allowed):
            offenders.append(f"{path.relative_to(SOURCE)}: {name}")
    assert not offenders, offenders


def test_the_allowlist_itself_names_nothing_network_capable() -> None:
    """The allowlist is the thing to get wrong now: one careless addition and the check above
    passes while a socket sits behind it.
    """
    network = {
        "asyncio",
        "email",
        "ftplib",
        "http",
        "httpx",
        "imaplib",
        "requests",
        "smtplib",
        "socket",
        "socketserver",
        "ssl",
        "telnetlib",
        "urllib",
        "urllib3",
        "webbrowser",
        "xmlrpc",
    }
    assert (MAY_IMPORT | MAY_IMPORT_COMPILED) & network == set()


def test_the_document_lists_exactly_the_vocabulary_the_code_has() -> None:
    """``docs/observability.md`` is what a client reads to decide whether to believe the promise,
    so it cannot be a second copy of the vocabulary drifting on its own. The list in it is
    generated and this is the check that it still matches.
    """
    doc = (SOURCE.parent.parent.parent / "docs" / "observability.md").read_text(encoding="utf-8")
    block = doc.split("<!-- events:begin -->")[1].split("<!-- events:end -->")[0]
    listed = {line.strip().strip("-` ") for line in block.splitlines() if line.strip()}
    assert listed == set(EVENTS)


def test_every_file_the_document_points_at_exists() -> None:
    """This document is a liability if left unchecked: it tells a client we do not call them, and
    invites them to run the tests that say so. A table pointing at a test somebody deleted is a
    worse artefact than no table - it reads as verified and is not.
    """
    repo = SOURCE.parent.parent.parent
    doc = (repo / "docs" / "observability.md").read_text(encoding="utf-8")
    named = [
        "python/tests/test_no_account_live.py",
        "python/tests/test_no_account.py",
        "typescript/tests/silence.test.ts",
        "python/tests/test_no_expiry.py",
    ]
    for path in named:
        assert Path(path).name in doc, f"{path} is not cited in the document"
        assert (repo / path).is_file(), f"{path} is cited and does not exist"


def test_our_own_wheel_is_pure_python_and_requires_nothing() -> None:
    """What bounds the live measurement, and it is worth being exact about.

    An audit hook sees the Python socket layer and nothing below it. Measured: psycopg connects
    through libpq in C and raises no audit event at all, while clickhouse-connect goes over
    ``http.client`` and raises fourteen. So an assertion that nothing was attempted is only worth
    making if this library cannot reach a socket the hook would miss.

    Our own wheel cannot: no compiled extension, no required dependency. The one compiled thing in
    the picture is the optional ``cryptography`` extra, which is why the live test exercises a real
    signature verification with the hook installed rather than arguing from its API surface.
    """
    config = tomllib.loads((SOURCE.parent.parent / "pyproject.toml").read_text(encoding="utf-8"))
    assert config["project"]["dependencies"] == []
    build = config.get("tool", {}).get("hatch", {}).get("build", {})
    assert "hooks" not in build, "a build hook can compile an extension"
    for target in build.get("targets", {}).values():
        assert "ext-modules" not in target

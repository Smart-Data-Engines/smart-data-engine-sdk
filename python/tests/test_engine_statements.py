"""docs/engine-statements.md, held to the source of the libraries.

The page tells a client's administrator every statement the library and its operator can send to
their engines that changes something. A list like that is worth reading only if it cannot fall
behind the code, so this test reads the code.

**Python, exactly.** Every string expression in `sde` and `sde_operator` that begins with a SQL verb
which changes something, and every Elasticsearch request whose method can, is rendered with `…` for
each part the code fills in. Every rendered form must be on the page, and every form the page lists
must be found in the source, or be one of the forms composed out of the reader's sight that
:data:`COMPOSED` names together with the line that composes it. A form added to the code fails
the first check; a form removed from it fails the second.

**TypeScript and C++, by verb and object.** Their statements are assembled in pieces - template
literals in TypeScript, concatenation in C++ - that a reader of literals cannot reassemble. What it
can read is how each one begins: `ALTER TABLE`, `LOCK TABLE`. Every such opening must open a form on
the page, so a statement of a new kind in either library fails here as it would in Python.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

PYTHON = Path(__file__).resolve().parents[1]
ROOT = PYTHON.parent
SOURCES = (PYTHON / "src" / "sde", PYTHON / "src" / "sde_operator")
DOC = ROOT / "docs" / "engine-statements.md"

SQL = re.compile(
    r"(CREATE|ALTER|DROP|TRUNCATE|GRANT|REVOKE|LOCK|DETACH|ATTACH|KILL|COMMENT|DELETE|INSERT|"
    r"UPDATE|OPTIMIZE|RENAME|EXCHANGE|SET|SYSTEM|COPY) [A-Z…({]"
)
REQUEST = re.compile(r"(PUT|POST|DELETE) /")
WRITING_WITH = re.compile(r"WITH [^(]*\((DELETE|INSERT|UPDATE) ")
"""A statement that begins as a read and writes inside: ``WITH x AS (DELETE ...) SELECT ...``."""
CHANGING_METHODS = frozenset({"PUT", "POST", "DELETE"})
READING_ENDPOINTS = ("/_count", "/_search", "/_has_privileges", "/_mget")
"""Requests sent with POST that read and change nothing, so the page does not list them."""

STRING_METHODS = frozenset(
    {"startswith", "endswith", "replace", "find", "index", "count", "split", "removeprefix"}
)

NOT_SENT = {
    ("schema.py", "compatibility_views"): (
        "renders views for the client's administrator to create; the library sends none of them"
    ),
    ("erasure.py", "rendered_deletion"): (
        "renders the deletion of a table outside the map for the client to run or not; the "
        "operator sends none of them"
    ),
}
"""Functions whose statements are text handed to a person, not statements this library sends."""

COMPOSED: dict[str, tuple[str, str]] = {
    "CREATE TABLE …": (
        "engines/_staging.py",
        '.replace("CREATE TABLE IF NOT EXISTS ", "CREATE TABLE ", 1)',
    ),
    "CREATE TABLE … COMMENT '…'": ("engines/_staging.py", "statement + \" COMMENT '\" + marker"),
    "INSERT": ("engines/clickhouse.py", "self._cx.insert("),
}
"""Forms on the page that the source builds where a reader of literals cannot follow, with the
file and the text that builds each. A staging creates its copy with the table statement above it
on the page, minus ``IF NOT EXISTS`` and, on ClickHouse, plus its marker; ClickHouse inserts are
written by the driver from rows. Each entry is checked against its file, so it cannot outlive the
code it stands for."""


OTHER_LIBRARIES: dict[str, tuple[str, str]] = {
    "SET DateStyle TO ISO": ("cpp/src/engines/postgres.cpp", '"SET DateStyle TO ISO"'),
    "INSERT INTO … (…) FORMAT JSONEachRow": (
        "typescript/src/engines/clickhouse.ts",
        "FORMAT JSONEachRow`",
    ),
}
"""Forms on the page that only the TypeScript or the C++ library sends, with the file and the text
that sends each, checked like :data:`COMPOSED`."""

PROSE = re.compile(r"\b(the|and|is|are|was|be|this|that|which|whatever|refuses)\b")
"""A message that begins with a statement and goes on to explain it is a sentence, not a statement:
SQL keywords here are upper case, and none of these words is one."""


def _normal(text: str) -> str:
    text = text.replace("{}", "…")
    text = re.sub(r"\s+", " ", text).strip()
    return re.sub(r"…+", "…", text)


def _render(node: ast.AST) -> str | None:
    """A string expression with `…` for every part the code fills in, or ``None``."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            part.value if isinstance(part, ast.Constant) else "…" for part in node.values
        )
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _render(node.left), _render(node.right)
        return (left if left is not None else "…") + (right if right is not None else "…")
    if isinstance(node, ast.IfExp):
        return None
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_path"
    ):
        parts = [_render(argument) for argument in node.args]
        return "/" + "/".join(part if part is not None else "…" for part in parts)
    return None


def _excluded(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    """A literal that is part of something other than a statement: a comparison, a container of
    keywords, a string method's argument, a docstring."""
    parent = parents.get(node)
    if isinstance(parent, (ast.Tuple, ast.List, ast.Set, ast.Compare, ast.Expr)):
        return True
    return (
        isinstance(parent, ast.Call)
        and isinstance(parent.func, ast.Attribute)
        and parent.func.attr in STRING_METHODS
    )


def _function(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> str:
    while node in parents:
        node = parents[node]
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return node.name
    return "<module>"


def _python_forms() -> dict[str, set[str]]:
    """Every statement form in the Python sources, with where it is."""
    forms: dict[str, set[str]] = {}
    for source in SOURCES:
        for path in sorted(source.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            parents = {
                child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)
            }
            relative = path.relative_to(source).as_posix()
            for node in ast.walk(tree):
                if (relative, _function(node, parents)) in NOT_SENT:
                    continue
                if isinstance(node, (ast.Constant, ast.JoinedStr, ast.BinOp)):
                    parent = parents.get(node)
                    outermost = not (
                        isinstance(parent, ast.JoinedStr)
                        or (isinstance(parent, ast.BinOp) and _render(parent) is not None)
                    )
                    text = _render(node)
                    statement = (
                        outermost
                        and text
                        and not _excluded(node, parents)
                        and not PROSE.search(text)
                        and (SQL.match(text) or REQUEST.match(text) or WRITING_WITH.match(text))
                    )
                    if statement and text:
                        form = _normal(text)
                        if REQUEST.match(form):
                            form = re.sub(r" …$", "", form)
                        forms.setdefault(form, set()).add(f"{relative}:{node.lineno}")
                if isinstance(node, ast.Call):
                    arguments = node.args
                    for index, argument in enumerate(arguments[:-1]):
                        if not (
                            isinstance(argument, ast.Constant)
                            and argument.value in CHANGING_METHODS
                        ):
                            continue
                        path_text = _render(arguments[index + 1])
                        if path_text is None or not path_text.startswith("/"):
                            continue
                        form = _normal(f"{argument.value} {path_text}")
                        if any(endpoint in form for endpoint in READING_ENDPOINTS):
                            continue
                        forms.setdefault(form, set()).add(f"{relative}:{node.lineno}")
    return forms


def _page_rows() -> list[tuple[set[str], str]]:
    """Each row of the page's tables: the code spans of its first column, and who sends them."""
    rows: list[tuple[set[str], str]] = []
    for line in DOC.read_text(encoding="utf-8").splitlines():
        if not line.startswith("| ") or line.startswith(("| Statement", "| Request")):
            continue
        cells = line.split(" | ")
        rows.append(({_normal(span) for span in re.findall(r"`([^`]+)`", cells[0])}, cells[1]))
    return rows


def _page_forms() -> set[str]:
    """Every code span in the first column of the page's tables."""
    return {form for forms, _sender in _page_rows() for form in forms}


def test_every_statement_the_python_library_can_send_is_on_the_page() -> None:
    page = _page_forms()
    missing = {form: places for form, places in _python_forms().items() if form not in page}
    assert not missing, f"statements the page does not list, and where they are: {missing}"


def test_every_form_on_the_page_is_still_sent() -> None:
    sent = set(_python_forms())
    gone = sorted(
        form
        for form in _page_forms()
        if form not in sent and form not in COMPOSED and form not in OTHER_LIBRARIES
    )
    assert not gone, f"forms the page lists that no source sends any more: {gone}"


def test_each_composed_form_is_composed_where_it_says() -> None:
    for form, (relative, text) in COMPOSED.items():
        assert form in _page_forms(), form
        assert text in (SOURCES[0] / relative).read_text(encoding="utf-8"), (form, relative)
    for form, (relative, text) in OTHER_LIBRARIES.items():
        assert form in _page_forms(), form
        assert text in (ROOT / relative).read_text(encoding="utf-8"), (form, relative)


def test_nothing_that_renders_text_for_a_person_is_counted_as_sent() -> None:
    """The exclusion is narrow: one function, and it really only renders."""
    for (relative, function), _reason in NOT_SENT.items():
        tree = ast.parse((SOURCES[0] / relative).read_text(encoding="utf-8"))
        (found,) = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == function
        ]
        calls = {
            node.func.attr for node in ast.walk(found)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        assert not calls & {"execute", "command", "query", "_request", "_call"}, calls


OPENING = re.compile(
    r"""(?:^|[`'"])((?:CREATE(?: OR REPLACE)?|ALTER|DROP|TRUNCATE|GRANT|REVOKE|LOCK|DETACH|"""
    r"""ATTACH|KILL|COMMENT ON|DELETE|INSERT|UPDATE|OPTIMIZE|RENAME|EXCHANGE|SET|SYSTEM) """
    r"""[A-Z][A-Za-z]*)"""
)
RENDERED_FOR_A_PERSON = ("CREATE VIEW", "DROP VIEW", "CREATE OR")
"""The openings of the compatibility views each library renders for an administrator to run."""


def test_typescript_and_cpp_open_no_kind_of_statement_the_page_does_not_have() -> None:
    """The two libraries have no operator, so an opening counts only from an application's or a
    provisioning's row: a `TRUNCATE TABLE` in TypeScript would be news, though the operator's is
    not."""
    page = {
        form
        for forms, sender in _page_rows()
        if "application" in sender or "provisioning" in sender
        for form in forms
    }
    openings = {" ".join(form.split()[:2]) for form in page}
    sources = [
        *sorted((ROOT / "typescript" / "src").rglob("*.ts")),
        *sorted((ROOT / "cpp" / "src").rglob("*.cpp")),
        *sorted((ROOT / "cpp" / "include").rglob("*.hpp")),
    ]
    unknown: dict[str, str] = {}
    for path in sources:
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith(("//", "*", "/*")):
                continue
            for opening in OPENING.findall(line):
                if opening not in openings and not opening.startswith(RENDERED_FOR_A_PERSON):
                    unknown.setdefault(opening, str(path.relative_to(ROOT)))
    assert not unknown, f"statement openings the page has no form for: {unknown}"

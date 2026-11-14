"""``docs/compatibility.md`` against what it describes: the workflow, the packaging, the constants.

The page answers what a buyer's review asks first - which engine versions, which drivers, which
maps - and each answer already lives somewhere that is enforced: the engine images in the CI
workflow, the driver ranges in the packaging metadata, the contract and protocol numbers in the
three libraries' source. A page that repeats them rots the day one of them moves, so every row here
is compared with its source, in both directions where there are two.

Read from the files rather than imported where a second library is concerned: the TypeScript and
C++ constants are matched in their source, as ``test_implementer_documents.py`` reads them.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import sde
from sde import cutover, index_build, staging, verification

ROOT = Path(__file__).resolve().parents[2]
PAGE = ROOT / "docs" / "compatibility.md"
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
IMAGE = re.compile(r"(?<![\w./-])((?:[a-z0-9]+/)?[a-z0-9-]+:\d[\w.]*(?:-[a-z]+)?)(?![\w.-])")
"""An image with a version: ``postgres:15-alpine``, ``clickhouse/clickhouse-server:26.9-alpine``."""


def _section(title: str) -> str:
    text = PAGE.read_text(encoding="utf-8")
    assert f"\n## {title}\n" in text, title
    return text.split(f"\n## {title}\n", 1)[1].split("\n## ", 1)[0]


def _rows(section: str) -> list[list[str]]:
    """The cells of every table row of a section, header and rule excluded, per table in order."""
    rows: list[list[str]] = []
    for line in section.splitlines():
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if set("".join(cells)) <= set("-: "):
            rows.pop()  # the header above a rule
            continue
        rows.append(cells)
    return rows


def _workflow_images() -> set[str]:
    """Every engine image the CI workflow starts: service containers and ``docker run`` alike."""
    images: set[str] = set()
    for line in WORKFLOW.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if stripped.startswith("image:"):
            images.add(stripped.split(":", 1)[1].strip())
        elif re.search(r"\belasticsearch:\d", stripped):
            images.update(m for m in IMAGE.findall(stripped) if m.startswith("elasticsearch:"))
    return images


def _numbers(cell: str) -> list[int]:
    """``1``, ``1 and 2`` and ``1 to 6`` as the numbers they name."""
    found = [int(n) for n in re.findall(r"\d+", cell)]
    if " to " in cell:
        assert len(found) == 2, cell
        return list(range(found[0], found[1] + 1))
    return found


def _constant(path: str, name: str) -> int:
    text = (ROOT / path).read_text(encoding="utf-8")
    match = re.search(rf"\b{name}\s*=\s*(\d+)\s*;?\s*$", text, re.MULTILINE)
    assert match is not None, f"{path} has no {name}"
    return int(match.group(1))


def test_the_engine_images_on_the_page_are_the_ones_ci_starts() -> None:
    documented = set(IMAGE.findall(_section("Engines")))
    started = _workflow_images()
    assert started, "the workflow starts no engine this test can read"
    assert documented == started, (
        f"on the page and not started by CI: {sorted(documented - started)}; "
        f"started by CI and not on the page: {sorted(started - documented)}"
    )


def test_each_tested_version_is_the_version_of_its_image() -> None:
    """The ``Tested`` cell and the image names of one row say the same versions."""
    rows = [r for r in _rows(_section("Engines")) if len(r) == 3 and IMAGE.search(r[2])]
    assert len(rows) == 3, rows
    for _engine, tested, how in rows:
        tags = [image.split(":", 1)[1] for image in IMAGE.findall(how)]
        from_images = sorted(tag.split("-", 1)[0] for tag in tags)
        assert sorted(re.findall(r"\d+(?:\.\d+)+|\d+", tested)) == from_images, (tested, how)


def test_the_local_engines_are_among_the_tested_ones() -> None:
    """``make engines-up`` and ``make es-up`` start versions CI also runs, never a third one."""
    local = {
        image
        for image in IMAGE.findall((ROOT / "Makefile").read_text(encoding="utf-8"))
        if image.split(":", 1)[0] in {"postgres", "clickhouse/clickhouse-server", "elasticsearch"}
    }
    assert local, "the Makefile starts no engine this test can read"
    assert local <= _workflow_images(), sorted(local - _workflow_images())


def test_the_orderbook_engine_is_the_pinned_commit() -> None:
    section = _section("Engines")
    assert "](../.github/orderbook-engine.txt)" in section
    pin = (ROOT / ".github" / "orderbook-engine.txt").read_text(encoding="utf-8").strip()
    assert re.fullmatch(r"[0-9a-f]{40}", pin), pin


def test_the_driver_ranges_are_the_packaging_metadata() -> None:
    python = tomllib.loads((ROOT / "python" / "pyproject.toml").read_text(encoding="utf-8"))
    node = json.loads((ROOT / "typescript" / "package.json").read_text(encoding="utf-8"))
    assert python["project"]["dependencies"] == [] and "dependencies" not in node, (
        "a library's core gained a dependency: the page says it has none"
    )
    declared = {
        (row[0], row[1]): row[3] for row in _rows(_section("Drivers")) if row[3].startswith("`")
    }
    extras = python["project"]["optional-dependencies"]
    expected = {
        ("Python", "PostgreSQL"): ("postgres", extras["postgres"]),
        ("Python", "ClickHouse"): ("clickhouse", extras["clickhouse"]),
        ("Python", "signed maps"): ("signed", extras["signed"]),
        ("Python", "the orderbook engine"): ("orderbook", extras["orderbook"]),
    }
    assert set(extras) == {extra for extra, _ in expected.values()} | {"dev"}, (
        "an extra a client may choose is not on the page, or the page has one that is gone"
    )
    for key, (extra, requirements) in expected.items():
        assert len(requirements) == 1, requirements
        assert declared.pop(key) == f"`{requirements[0]}`, the extra `{extra}`", key
    peers = node["peerDependencies"]
    assert set(peers) == {"pg"} and node["peerDependenciesMeta"]["pg"]["optional"] is True
    assert declared.pop(("TypeScript", "PostgreSQL")) == (
        f"`pg` `{peers['pg']}`, an optional peer dependency"
    )
    assert not declared, f"a declared range on the page that no manifest has: {declared}"


def test_the_driver_floors_ci_runs_are_the_declared_floors() -> None:
    """A range's lower end is a claim, and CI installs exactly it for the two drivers it runs so."""
    python = tomllib.loads((ROOT / "python" / "pyproject.toml").read_text(encoding="utf-8"))
    workflow = WORKFLOW.read_text(encoding="utf-8")
    for extra in ("clickhouse", "orderbook"):
        (requirement,) = python["project"]["optional-dependencies"][extra]
        floor = requirement.replace(">=", "==")
        assert floor != requirement, requirement
        assert f"'{floor}'" in workflow, floor
        assert f"`{floor}`" in _section("Drivers"), floor


def test_the_audit_installs_every_extra_a_client_may_choose() -> None:
    """The supply-chain job says it does. A new extra is a new set of packages a client runs."""
    python = tomllib.loads((ROOT / "python" / "pyproject.toml").read_text(encoding="utf-8"))
    chosen = set(python["project"]["optional-dependencies"]) - {"dev"}
    (audited,) = re.findall(
        r'pip install -q "\./python\[([a-z,]+)\]" pip-audit', WORKFLOW.read_text(encoding="utf-8")
    )
    assert set(audited.split(",")) == chosen, (audited, sorted(chosen))


def test_the_page_points_at_the_record_of_the_orderbook_release() -> None:
    section = _section("Engines")
    assert "### The orderbook engine's release" in section
    (record,) = re.findall(r"\]\((qualification/[^)]+/README\.md)\)", section)
    folder = (PAGE.parent / record).parent
    assert (folder / "README.md").is_file() and (folder / "SHA256SUMS").is_file(), folder


def test_the_numbers_main_reads_are_the_libraries_constants() -> None:
    rows = {row[0]: _numbers(row[1]) for row in _rows(_section("Maps and signed packets"))[:6]}
    assert rows == {
        "model IR": [sde.CONTRACT],
        "placement map": list(range(sde.MAP_CONTRACT_FLOOR, sde.MAP_CONTRACT + 1)),
        "verification request": [verification.REQUEST_PROTOCOL],
        "cutover packet": [cutover.CUTOVER_PROTOCOL, cutover.CUTOVER_RELAYOUT_PROTOCOL],
        "staging packet": [staging.STAGING_PROTOCOL, staging.STAGING_RELAYOUT_PROTOCOL],
        "index build packet": [index_build.INDEX_PROTOCOL, index_build.INDEX_CHANGE_PROTOCOL],
    }
    contract = (ROOT / "conformance" / "contract-version.txt").read_text(encoding="utf-8").strip()
    assert rows["model IR"] == [int(contract)]


def test_the_three_libraries_read_the_same_numbers() -> None:
    """One table for three libraries is true only while their constants are equal."""
    packets = "cpp/include/sde/packets.hpp"
    sources = {
        "MAP_CONTRACT": ("typescript/src/placement.ts", "cpp/include/sde/placement.hpp"),
        "MAP_CONTRACT_FLOOR": ("typescript/src/placement.ts", "cpp/include/sde/placement.hpp"),
        "REQUEST_PROTOCOL": ("typescript/src/verification.ts", "cpp/include/sde/verification.hpp"),
        "CUTOVER_PROTOCOL": ("typescript/src/cutover.ts", packets),
        "CUTOVER_RELAYOUT_PROTOCOL": ("typescript/src/cutover.ts", packets),
        "STAGING_PROTOCOL": ("typescript/src/staging.ts", packets),
        "STAGING_RELAYOUT_PROTOCOL": ("typescript/src/staging.ts", packets),
        "INDEX_PROTOCOL": ("typescript/src/in-place-index.ts", packets),
        "INDEX_CHANGE_PROTOCOL": ("typescript/src/in-place-index.ts", packets),
    }
    reference = {
        "MAP_CONTRACT": sde.MAP_CONTRACT,
        "MAP_CONTRACT_FLOOR": sde.MAP_CONTRACT_FLOOR,
        "REQUEST_PROTOCOL": verification.REQUEST_PROTOCOL,
        "CUTOVER_PROTOCOL": cutover.CUTOVER_PROTOCOL,
        "CUTOVER_RELAYOUT_PROTOCOL": cutover.CUTOVER_RELAYOUT_PROTOCOL,
        "STAGING_PROTOCOL": staging.STAGING_PROTOCOL,
        "STAGING_RELAYOUT_PROTOCOL": staging.STAGING_RELAYOUT_PROTOCOL,
        "INDEX_PROTOCOL": index_build.INDEX_PROTOCOL,
        "INDEX_CHANGE_PROTOCOL": index_build.INDEX_CHANGE_PROTOCOL,
    }
    for name, paths in sources.items():
        for path in paths:
            assert _constant(path, name) == reference[name], (path, name)
    assert _constant("typescript/src/index.ts", "CONTRACT_VERSION") == sde.CONTRACT
    assert _constant("cpp/include/sde/model.hpp", "IR_CONTRACT") == sde.CONTRACT


def test_the_release_table_ends_with_main_and_has_every_release() -> None:
    rows = _rows(_section("Maps and signed packets"))[6:]
    assert rows[-1][0] == "`main`", rows[-1]
    assert _numbers(rows[-1][1]) == list(range(sde.MAP_CONTRACT_FLOOR, sde.MAP_CONTRACT + 1))
    assert _numbers(rows[-1][2]) == [cutover.CUTOVER_PROTOCOL, cutover.CUTOVER_RELAYOUT_PROTOCOL]
    listed = {version for row in rows[:-1] for version in re.findall(r"\d+\.\d+\.\d+", row[0])}
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    released = {
        version
        for heading in re.findall(r"^## (.+)$", changelog, re.MULTILINE)
        for version in re.findall(r"(?<![\w.])(\d+\.\d+\.\d+)(?![\w.-])", heading)
    }
    assert released and listed == released, (
        f"releases in the changelog and not in the table: {sorted(released - listed)}; "
        f"in the table and not in the changelog: {sorted(listed - released)}"
    )


def test_every_document_the_two_pages_link_to_exists() -> None:
    for page in (PAGE, ROOT / "docs" / "upgrading.md"):
        text = page.read_text(encoding="utf-8")
        targets = re.findall(r"\]\(([^)#\s]+)(#[^)\s]*)?\)", text)
        assert targets, page.name
        for target, anchor in targets:
            if "://" in target:
                continue
            linked = (page.parent / target).resolve()
            assert linked.exists(), f"{page.name} links to {target}, which is not there"
            if anchor and linked.suffix == ".md":
                headings = {
                    re.sub(r"[^\w\- ]", "", h.lower()).strip().replace(" ", "-")
                    for h in re.findall(
                        r"^#+ (.+)$", linked.read_text(encoding="utf-8"), re.MULTILINE
                    )
                }
                assert anchor[1:] in headings, (
                    f"{page.name} links to {target}{anchor}, and {linked.name} has no such heading"
                )

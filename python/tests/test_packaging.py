"""What the distribution has to contain, checked here rather than discovered by a consumer.

This file exists because of a gap that was invisible from inside the repository. This package is
annotated throughout and its own CI runs ``mypy --strict``, so from here the types looked fine. From
*outside* they did not exist: without a PEP 561 marker a consumer's type checker reports ``module
is installed, but missing library stubs or py.typed marker`` and silently treats everything as
``Any``.

That is worse than shipping no annotations. It is shipping annotations only we benefit from, in a
library whose entire argument is that a client can verify our privacy invariants by reading it - and
the first person to hit it was our own control plane, on the day it was created.

A second gap, found the same way and three weeks later: nothing here had ever looked at a *built*
package. Preparing the first upload to PyPI and npm turned up three defects a green suite cannot
see, because the suite runs the source tree and a user runs the artefact.

- Neither package carried its licence. `license = { text = "Apache-2.0" }` writes the *name* of
  a licence into the metadata and ships no file; the wheel's `dist-info` held `METADATA`, `WHEEL`
  and `RECORD` and nothing else, while Apache-2.0 section 4 asks for the text and, where one
  exists, the NOTICE.
- `npm pack` produced a tarball of two files, `package.json` and `README.md`. `files` lists `dist`,
  `dist` is a build output, and nothing built it - so a publish from a clean checkout would have put
  an importable-looking package with no code in it under our own scope, at a version npm will never
  let us reuse.
- Neither registry page would have linked to the repository, in a library whose argument is that the
  invariant is checkable because you can read the code.

The artefacts were built and unpacked by hand on 8 September 2026 - `python -m build` then
`twine check` (both pass), and `npm pack --dry-run` (70 files, with `dist/index.js` and both engine
adapters). Those runs are not repeated here: they need a network to fetch a build backend, and a
required check that depends on PyPI being reachable fails for reasons that have nothing to do with
the change under review. What is asserted here is the manifest each build reads.

The last test is not about an artefact at all but about what we *say* we have published, for the
reason set out in `_claims.py`: a claim that something has not happened is the one kind nothing ever
tries to use, so nothing ever disproves it.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from types import ModuleType

import pytest
from _claims import REGISTRIES, UNCLAIMED, sentences

import sde

PACKAGE = Path(sde.__file__).resolve().parent
ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = ROOT / "python" / "pyproject.toml"
PACKAGE_JSON = ROOT / "typescript" / "package.json"

#: The licence files each published package needs beside it, because a build backend cannot reach
#: outside its own project directory to the copies in the repository root.
LICENCE_FILES = ("LICENSE", "NOTICE")

#: The two directories that become published artefacts.
PACKAGES = ("python", "typescript")


def test_the_pep_561_marker_is_next_to_the_package() -> None:
    marker = PACKAGE / "py.typed"
    assert marker.exists(), (
        "src/sde/py.typed is missing. Without it every consumer's type checker treats this library "
        "as untyped, which is invisible from inside this repository because our own mypy run reads "
        "the source directly."
    )
    assert marker.stat().st_size == 0, (
        "the marker is a marker; PEP 561 gives its contents no meaning"
    )


def test_every_name_in_all_is_actually_importable() -> None:
    """`__all__` is a promise about `from sde import *`, and nothing else here checked it.

    Found by trying: `ALSO_WRITE_SINCE` was added to `__all__` when the map contract gained the key
    and never imported, so `sde.ALSO_WRITE_SINCE` raised and `from sde import *` would have too.
    Neither ruff nor mypy --strict reported it, which is the whole reason this test exists rather
    than a note saying to be careful.
    """
    missing = sorted(name for name in sde.__all__ if not hasattr(sde, name))
    assert not missing, f"named in __all__ and not importable: {missing}"


def test_nothing_public_is_left_out_of_all() -> None:
    """The other direction: a public name reachable by attribute and absent from `__all__`.

    Submodules are excluded by *type* rather than by a list of their names, and the first version
    of this test did it by list - which passed alone and failed in the full run, because importing
    `sde.engines` anywhere binds it as an attribute of `sde`. An order-dependent test is worse than
    no test: it reads as flaky and gets a re-run.
    """
    public = {
        name
        for name, value in vars(sde).items()
        if not name.startswith("_") and not isinstance(value, ModuleType)
    }
    # `annotations` is the __future__ feature object, not a name anybody imports from us.
    public.discard("annotations")
    assert public - set(sde.__all__) == set(), sorted(public - set(sde.__all__))


def _pyproject() -> dict[str, object]:
    parsed: dict[str, object] = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    return parsed


def _package_json() -> dict[str, object]:
    parsed: dict[str, object] = json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))
    return parsed


def _project() -> dict[str, object]:
    project = _pyproject()["project"]
    assert isinstance(project, dict)
    return project


@pytest.mark.parametrize("package", PACKAGES)
@pytest.mark.parametrize("licence", LICENCE_FILES)
def test_the_licence_beside_each_package_is_the_one_in_the_root(package: str, licence: str) -> None:
    """The copies exist because a backend cannot package a file above its own directory.

    Duplicating a file in a repository that argues against two copies of anything needs an answer,
    and this is it: the copies are asserted byte-identical, so the objection - that one of them
    quietly becomes a different licence - is the thing that fails here rather than the thing that
    ships.
    """
    beside = ROOT / package / licence
    assert beside.is_file(), (
        f"{package}/{licence} is missing, so the published package carries no {licence}. "
        f"Copy it from the repository root."
    )
    assert beside.read_bytes() == (ROOT / licence).read_bytes(), (
        f"{package}/{licence} has drifted from the root {licence}. The published artefact would "
        f"state different terms from the repository it came from."
    )


def test_the_python_package_ships_its_licence_rather_than_naming_one() -> None:
    """`license-files`, not `license = {text = ...}`.

    The distinction is invisible in the manifest and total in the artefact: the second form produces
    `License: Apache-2.0` in the metadata and puts no file in the wheel.
    """
    project = _project()
    assert project.get("license") == "Apache-2.0", (
        "`license` should be the SPDX expression. The table form ships no licence file."
    )
    declared = project.get("license-files", [])
    assert isinstance(declared, list)
    assert sorted(str(entry) for entry in declared) == sorted(LICENCE_FILES), (
        f"`license-files` must name {list(LICENCE_FILES)}; the wheel carries exactly what it lists."
    )


def test_the_python_package_declares_no_licence_classifier() -> None:
    """PEP 639 makes a `License ::` classifier an error next to a licence expression.

    Worth a test rather than a comment because the classifier is the older convention, it is what
    most projects still show, and the failure is at upload - after the version number is spent.
    """
    classifiers = _project().get("classifiers", [])
    assert isinstance(classifiers, list)
    offenders = [row for row in classifiers if str(row).startswith("License ::")]
    assert not offenders, (
        f"{offenders} cannot sit next to `license = \"Apache-2.0\"`; PyPI rejects the upload."
    )


@pytest.mark.parametrize("package", PACKAGES)
def test_each_registry_page_would_link_to_the_repository(package: str) -> None:
    """A page that does not link to the source cannot be checked by the reader we ask to check it.

    Both registries render this from metadata and neither invents it, so a package published without
    it has a page with a long README and no way out of it.
    """
    repository = "https://github.com/Smart-Data-Engines/smart-data-engine-sdk"
    if package == "python":
        urls = _project().get("urls", {})
        assert isinstance(urls, dict)
        assert urls.get("Repository") == repository, "pyproject names no Repository URL"
        assert urls.get("Homepage"), "pyproject names no Homepage"
    else:
        manifest = _package_json()
        assert manifest.get("homepage") == repository, "package.json names no homepage"
        declared = manifest.get("repository")
        assert isinstance(declared, dict)
        assert repository in str(declared.get("url")), "package.json names no repository URL"
        assert declared.get("directory") == "typescript", (
            "package.json must say which directory of the monorepo it is, or provenance and the "
            "'source' link point at the root."
        )


def test_the_typescript_package_publishes_public_without_anyone_remembering_a_flag() -> None:
    """A scoped npm package is **private by default**.

    `npm publish` without `--access public` on a scope with no paid plan fails, and the shape of the
    mistake is worse than the failure: the fix that comes to mind at that moment is to buy a plan or
    to unscope the package, and the actual answer is one field. It belongs in the manifest, where it
    applies to every publish anyone ever runs, rather than in a runbook step that has to be
    remembered on the one day it matters.
    """
    publish_config = _package_json().get("publishConfig", {})
    assert isinstance(publish_config, dict)
    assert publish_config.get("access") == "public", (
        "publishConfig.access must be 'public'; scoped packages are private by default."
    )


def test_the_typescript_package_cannot_be_packed_without_being_built() -> None:
    """`files` lists a build output, so packing has to build.

    Measured before this existed: `npm pack --dry-run` shipped two files, neither of them code. The
    mechanism is `prepack` rather than `prepublishOnly`, because `prepack` also covers `npm pack` -
    the command a person runs to see what they are about to publish, and therefore the one that must
    not lie to them.
    """
    manifest = _package_json()
    scripts = manifest.get("scripts", {})
    files = manifest.get("files", [])
    assert isinstance(scripts, dict)
    assert isinstance(files, list)
    assert "dist" in files, "`files` no longer ships the build output"
    assert scripts.get("prepack") == "npm run build", (
        "`prepack` must build. Without it a publish from a clean checkout ships no code, at a "
        "version number npm will not let us reuse."
    )
    for licence in LICENCE_FILES:
        assert licence in files, f"`files` does not ship {licence}"


@pytest.mark.parametrize(("registry", "name", "registered"), REGISTRIES)
def test_the_pages_agree_with_whether_the_name_is_ours(
    registry: str, name: str, registered: bool
) -> None:
    """The ratchet, and it fails in both directions from one flag.

    **Registered, and a page still calls it unclaimed.** That sentence is not trivia: three runtime
    error messages in this library tell a user, at the moment something has already failed, to run
    `pip install 'smart-data-engine[...]'`. A page saying the name belongs to nobody tells them the
    instruction their own dependency just gave them is not ours.

    **Unregistered, and no page says so.** Our README hands a reader an installation line for a name
    we do not own. Deleting the warning while it is still true is how this rots, and a one-sided
    check would let that through in silence - which is the same failure as the absence claims in
    :mod:`_claims`, arriving from the other side.

    Never vacuous: exactly one branch applies to each name, and both read the same flag, so
    registering a name and forgetting to say so is a red test rather than a stale page.
    """
    found = _claims_about(name)
    if registered:
        assert not found, (
            f"{name} is registered on {registry}. Still calling it unclaimed: "
            + "; ".join(f"{document}: {sentence!r}" for document, sentence in found)
        )
        return

    assert found, (
        f"{name} is not registered on {registry} and no document admits it. Our own README tells a "
        f"reader to install that name; if it resolves to somebody else's package, nothing here "
        f"warns them."
    )
    # And specifically next to the command, not merely somewhere in the repository. A first pass
    # asked only for "somewhere", and a mutation deleting the caveat from one page survived it -
    # correctly, since two other pages still carried one. Neither of those two is the page a person
    # is reading when they copy the command, which is the only moment the warning does any work.
    admitting = {document for document, _ in found}
    command = f"pip install {name}" if registry == "PyPI" else f"npm install {name}"
    for page in _documents():
        relative = page.relative_to(ROOT).as_posix()
        if command in page.read_text(encoding="utf-8") and relative not in admitting:
            raise AssertionError(
                f"{relative} tells a reader to run `{command}` and does not say the name is "
                f"unclaimed. The caveat belongs beside the command."
            )


def _documents() -> list[Path]:
    """Every markdown page in the repository, minus vendored trees.

    Derived rather than listed: the point of the check is the page nobody thought to add to a list.
    """
    return sorted(
        path
        for path in ROOT.rglob("*.md")
        if not {"node_modules", ".venv", "dist", ".git"} & set(path.parts)
    )


def _claims_about(name: str) -> list[tuple[str, str]]:
    """Every sentence, in every page, that names this distribution and calls it unclaimed."""
    hits: list[tuple[str, str]] = []
    for document in _documents():
        text = document.read_text(encoding="utf-8")
        if name not in text:
            continue
        for sentence in sentences(text):
            if name in sentence and UNCLAIMED.search(sentence):
                hits.append((document.relative_to(ROOT).as_posix(), sentence))
    return hits

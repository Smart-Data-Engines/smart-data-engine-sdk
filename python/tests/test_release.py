"""The two decisions a release cannot take back, checked here rather than in a workflow's shell.

A release in this repository is a tag and nothing else, and two things about it are irreversible. The
tag is immutable under the `refs/tags/*` ruleset, and both registries refuse to reuse a version
number - so a tag that disagrees with its manifest publishes the manifest's version under the tag's
name, permanently, and neither half can be corrected afterwards. The artefact is the other one: the
suite runs the source tree and a user runs the built package, and on 8 September 2026 that gap held
three defects, the worst of which would have put an importable-looking package with no code in it
under our own scope.

Both decisions therefore live in tested scripts rather than in `run:` blocks. Shell inside a workflow
is the least reviewed code in any repository, and these are the most consequential lines in this one.

What is *not* here is the agreement between the release workflow and the tag ruleset - that needs
PyYAML, which only the `contract` job installs, so it lives in `.github/rulesets/check_contexts.py`
beside the other workflow-versus-ruleset check. Putting it here would have meant a new dependency in
three Python matrix jobs to read a file none of them otherwise touch.
"""

from __future__ import annotations

import importlib.util
import json
import tarfile
import zipfile
from pathlib import Path
from types import ModuleType

import pytest

import sde

ROOT = Path(__file__).resolve().parents[2]


def _tool(name: str) -> ModuleType:
    """Load a script from `tools/` by path.

    By path rather than by mutating `sys.path`: an entry added to `sys.path` stays added for every
    later test in the session, which is how a test comes to pass because of an import somebody else
    arranged.
    """
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


release_tag = _tool("release_tag")
check_artefact = _tool("check_artefact")


# --------------------------------------------------------------------------------------------------
# The tag gate
# --------------------------------------------------------------------------------------------------


def test_the_python_tag_agrees_with_the_version_the_library_reports() -> None:
    """The gate reads the same number the wheel will carry, not a copy of it.

    This is the assertion that ties the script to reality: `sde.__version__` is what Hatchling puts
    in the artefact, so if the gate ever came to read some other file it would disagree here.
    """
    package, version = release_tag.resolve(f"refs/tags/python-v{sde.__version__}")
    assert package == "python"
    assert version == sde.__version__


def test_the_typescript_tag_agrees_with_its_manifest() -> None:
    declared = json.loads((ROOT / "typescript" / "package.json").read_text())["version"]
    package, version = release_tag.resolve(f"refs/tags/typescript-v{declared}")
    assert package == "typescript"
    assert version == declared


def test_the_version_is_read_from_the_path_pyproject_names() -> None:
    """Not from a path written in the script, which is how it would come to check a dead file.

    The failure this forbids is quiet and points the wrong way: the script would keep agreeing with a
    file the build no longer reads, and the disagreement would surface as a published artefact
    carrying a version nobody chose.
    """
    import tomllib

    config = tomllib.loads((ROOT / "python" / "pyproject.toml").read_text())
    assert "version" in config["project"]["dynamic"], (
        "the Python version is no longer dynamic, so tools/release_tag.py is reading a file the "
        "build backend has stopped consulting."
    )
    named = ROOT / "python" / config["tool"]["hatch"]["version"]["path"]
    assert named.resolve() == Path(sde.__file__).resolve(), (
        f"pyproject.toml points Hatchling at {named}, which is not where sde.__version__ lives."
    )


def test_a_bare_v_tag_is_refused_and_the_refusal_says_what_to_type_instead() -> None:
    """A tag that triggers nothing is a release that looks done.

    The tag is in the repository and protected by the ruleset, and nothing was published. So the
    workflow triggers on `v*` purely so this refusal can happen, and the refusal is only worth
    anything if it names both valid forms - the person reading it has just burned a version number
    they cannot reuse.
    """
    with pytest.raises(SystemExit) as raised:
        release_tag.resolve("refs/tags/v0.2.0")
    message = str(raised.value)
    assert "python-v0.2.0" in message
    assert "typescript-v0.2.0" in message
    assert "cannot be deleted or moved" in message


def test_a_tag_naming_a_package_that_does_not_exist_is_refused() -> None:
    with pytest.raises(SystemExit) as raised:
        release_tag.resolve("refs/tags/rust-v0.1.0")
    assert "rust" in str(raised.value)
    assert "tag ruleset" in str(raised.value)


def test_a_tag_that_disagrees_with_the_manifest_is_refused_and_both_numbers_are_named() -> None:
    with pytest.raises(SystemExit) as raised:
        release_tag.resolve("refs/tags/python-v99.99.99")
    message = str(raised.value)
    assert "99.99.99" in message
    assert sde.__version__ in message, "a refusal that hides the manifest's version is a riddle"


def test_something_that_is_not_a_release_tag_at_all_is_refused() -> None:
    with pytest.raises(SystemExit):
        release_tag.resolve("refs/tags/nightly")


# --------------------------------------------------------------------------------------------------
# The artefact
# --------------------------------------------------------------------------------------------------

FILLER = tuple(f"sde/filler_{n}.py" for n in range(8))


def _wheel(path: Path, *, version: str = "1.2.3", omit: str = "", extra: str = "") -> Path:
    dist_info = f"smart_data_engine_sdk-{version}.dist-info"
    members = {
        "sde/__init__.py": "x = 1\n",
        "sde/py.typed": "",
        f"{dist_info}/METADATA": f"Name: smart-data-engine-sdk\nVersion: {version}\n",
        f"{dist_info}/licenses/LICENSE": "Apache\n",
        f"{dist_info}/licenses/NOTICE": "notice\n",
        **{name: "" for name in FILLER},
    }
    if omit:
        members.pop(omit.replace("DIST_INFO", dist_info))
    if extra:
        members[extra] = ""
    with zipfile.ZipFile(path, "w") as archive:
        for name, body in members.items():
            archive.writestr(name, body)
    return path


def _npm(path: Path, *, version: str = "1.2.3", omit: str = "", extra: str = "",
         only_two: bool = False) -> Path:
    members = {
        "package/package.json": json.dumps({"name": "@smart-data-engines/sde", "version": version}),
        "package/README.md": "readme\n",
    }
    if not only_two:
        members.update({
            "package/LICENSE": "Apache\n",
            "package/NOTICE": "notice\n",
            "package/dist/index.js": "export {};\n",
            "package/dist/index.d.ts": "export {};\n",
            "package/dist/engines/postgres.js": "export {};\n",
            "package/dist/engines/clickhouse.js": "export {};\n",
            "package/dist/testing/memory.js": "export {};\n",
            **{f"package/dist/filler_{n}.js": "" for n in range(4)},
        })
    if omit:
        members.pop(omit)
    if extra:
        members[extra] = ""
    with tarfile.open(path, "w:gz") as archive:
        for name, body in members.items():
            data = body.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            import io

            archive.addfile(info, io.BytesIO(data))
    return path


def test_a_complete_wheel_has_nothing_to_complain_about(tmp_path: Path) -> None:
    assert check_artefact.verify("wheel", _wheel(tmp_path / "w.whl"), "1.2.3") == []


def test_a_complete_npm_tarball_has_nothing_to_complain_about(tmp_path: Path) -> None:
    assert check_artefact.verify("npm", _npm(tmp_path / "p.tgz"), "1.2.3") == []


@pytest.mark.parametrize(
    "omit",
    ["sde/py.typed", "DIST_INFO/licenses/LICENSE", "DIST_INFO/licenses/NOTICE"],
)
def test_a_wheel_missing_a_required_member_is_named(tmp_path: Path, omit: str) -> None:
    """Each of these has actually been missing, and none of it was visible from a green suite.

    Without `py.typed` a consumer's type checker silently treats the whole library as `Any`; without
    the licence files the wheel shipped the *name* of Apache-2.0 and not its text.
    """
    problems = check_artefact.verify("wheel", _wheel(tmp_path / "w.whl", omit=omit), "1.2.3")
    assert problems, f"omitting {omit} was not noticed"
    assert any("missing" in problem for problem in problems)


def test_the_two_file_tarball_that_started_all_of_this_is_refused(tmp_path: Path) -> None:
    """`npm pack` produced exactly `package.json` and `README.md`, and npm would have accepted it."""
    problems = check_artefact.verify("npm", _npm(tmp_path / "p.tgz", only_two=True), "1.2.3")
    assert any("fewer than" in problem for problem in problems), problems


def test_a_matcher_stuck_on_present_cannot_report_a_perfect_artefact(tmp_path: Path) -> None:
    """The control, and it needs its pair to mean anything.

    A checker that answered "present" to every question would find every required member and report a
    flawless package. Asking one question whose answer must be "absent" is what distinguishes that
    from an instrument that is looking. The pair is the mechanism: the archive with the sentinel in it
    must complain, and the one without it must not.
    """
    with_sentinel = _wheel(tmp_path / "a.whl", extra=check_artefact.CONTROL)
    without = _wheel(tmp_path / "b.whl")

    complaints = check_artefact.verify("wheel", with_sentinel, "1.2.3")
    assert any("CONTROL FAILED" in problem for problem in complaints), complaints
    assert check_artefact.verify("wheel", without, "1.2.3") == []


def test_a_star_does_not_cross_a_slash(tmp_path: Path) -> None:
    """Otherwise every pattern here is looser than it reads.

    `fnmatch` is the obvious implementation and its `*` matches `/`, which would let
    `*.dist-info/METADATA` be satisfied by a file nested anywhere at all - and a pattern that matches
    more than intended fails in the direction that publishes.
    """
    assert check_artefact.matches("*.dist-info/METADATA", ["sdk-1.0.dist-info/METADATA"])
    assert not check_artefact.matches("*.dist-info/METADATA", ["nested/sdk-1.0.dist-info/METADATA"])


def test_an_artefact_whose_recorded_version_is_not_the_tagged_one_is_refused(tmp_path: Path) -> None:
    """The tag gate agreeing with the manifest does not prove the build used it.

    What a user installs is the number inside the artefact, so that is the number checked - against
    the one the tag asked for, on the artefact about to be uploaded.
    """
    for kind, build in (("wheel", _wheel), ("npm", _npm)):
        path = build(tmp_path / f"{kind}-artefact", version="0.0.1")
        problems = check_artefact.verify(kind, path, "1.2.3")
        assert any("records version" in problem for problem in problems), (kind, problems)


def test_every_artefact_kind_the_workflow_asks_for_is_one_this_script_knows() -> None:
    """A kind the script does not know exits 2, and a workflow step that exits 2 stops the release.

    That is the right behaviour and it is worth pinning from this side, because the three strings
    live in a `run:` block where a typo is not a syntax error anywhere.
    """
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text()
    for kind in check_artefact.REQUIRED:
        assert f"check_artefact.py {kind} " in workflow, (
            f"tools/check_artefact.py knows how to open a {kind!r} and the release workflow never "
            f"asks it to. Either the workflow stopped checking an artefact it publishes, or the "
            f"script grew a kind nothing uses."
        )


def _synthetic_tree(root: Path, *, hatch_path: str | None, version: str) -> None:
    """A minimal `python/` whose version lives somewhere other than `src/sde/__init__.py`."""
    (root / "python" / "src" / "sde").mkdir(parents=True)
    if hatch_path is None:
        body = '[project]\nname = "x"\nversion = "9.9.9"\n'
    else:
        body = (
            '[project]\nname = "x"\ndynamic = ["version"]\n'
            f'[tool.hatch.version]\npath = "{hatch_path}"\n'
        )
        target = root / "python" / hatch_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f'__version__ = "{version}"\n')
    (root / "python" / "pyproject.toml").write_text(body)


def test_the_gate_follows_the_path_pyproject_names_even_when_it_moves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The derivation is load-bearing, not decorative.

    The other test asserts that today's declared path happens to be where `__version__` lives, which
    a hardcoded literal equal to today's value would satisfy just as well. This one puts the version
    somewhere else entirely and requires the gate to follow - so the only implementation that passes
    is one that reads `[tool.hatch.version] path` rather than remembering it.
    """
    _synthetic_tree(tmp_path, hatch_path="src/sde/_version.py", version="4.5.6")
    monkeypatch.setattr(release_tag, "ROOT", tmp_path)
    assert release_tag.resolve("refs/tags/python-v4.5.6") == ("python", "4.5.6")
    with pytest.raises(SystemExit):
        release_tag.resolve("refs/tags/python-v0.0.0")


def test_a_dynamic_version_with_nowhere_named_is_refused_rather_than_guessed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Guessing here means checking a file the build ignores, and the mistake ships."""
    (tmp_path / "python").mkdir()
    (tmp_path / "python" / "pyproject.toml").write_text(
        '[project]\nname = "x"\ndynamic = ["version"]\n'
    )
    monkeypatch.setattr(release_tag, "ROOT", tmp_path)
    with pytest.raises(SystemExit) as raised:
        release_tag.resolve("refs/tags/python-v1.0.0")
    assert "refuses to guess" in str(raised.value)


def test_a_static_version_is_read_from_pyproject_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the build backend ever stops being Hatchling, the gate still has a number to compare."""
    _synthetic_tree(tmp_path, hatch_path=None, version="")
    monkeypatch.setattr(release_tag, "ROOT", tmp_path)
    assert release_tag.resolve("refs/tags/python-v9.9.9") == ("python", "9.9.9")

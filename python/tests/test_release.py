"""The two decisions a release cannot take back, checked here rather than in a workflow's shell.

A release in this repository is a tag and nothing else, and two things about it are irreversible.
The tag is immutable under the `refs/tags/*` ruleset, and both registries refuse to reuse a version
number - so a tag that disagrees with its manifest publishes the manifest's version under the tag's
name, permanently, and neither half can be corrected afterwards. The artefact is the other one: the
suite runs the source tree and a user runs the built package, and on 8 September 2026 that gap held
three defects, the worst of which would have put an importable-looking package with no code in it
under our own scope.

Both decisions therefore live in tested scripts rather than in `run:` blocks. Shell inside a
workflow is the least reviewed code in any repository, and these are the most consequential lines
in this one.

What is *not* here is the agreement between the release workflow and the tag ruleset - that needs
PyYAML, which only the `contract` job installs, so it lives in `.github/rulesets/check_contexts.py`
beside the other workflow-versus-ruleset check. Putting it here would have meant a new dependency in
three Python matrix jobs to read a file none of them otherwise touch.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
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
npm_dist_tag = _tool("npm_dist_tag")
check_sbom = _tool("check_sbom")


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

    The failure this forbids is quiet and points the wrong way: the script would keep agreeing
    with a file the build no longer reads, and the disagreement would surface as a published
    artefact carrying a version nobody chose.
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
        "sde_demo/__main__.py": "",
        "sde_demo/py.typed": "",
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
            "package/bin/weather.mjs": "export {};\n",
            "package/dist/demo/weather.js": "export {};\n",
            "package/dist/demo/weather.d.ts": "export {};\n",
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
    """`npm pack` produced exactly `package.json` and `README.md`, and npm accepts that."""
    problems = check_artefact.verify("npm", _npm(tmp_path / "p.tgz", only_two=True), "1.2.3")
    assert any("fewer than" in problem for problem in problems), problems


def test_a_matcher_stuck_on_present_cannot_report_a_perfect_artefact(tmp_path: Path) -> None:
    """The control, and it needs its pair to mean anything.

    A checker that answered "present" to every question would find every required member and
    report a flawless package. Asking one question whose answer must be "absent" is what
    distinguishes that from an instrument that is looking. The pair is the mechanism: the archive
    with the sentinel in it must complain, and the one without it must not.
    """
    with_sentinel = _wheel(tmp_path / "a.whl", extra=check_artefact.CONTROL)
    without = _wheel(tmp_path / "b.whl")

    complaints = check_artefact.verify("wheel", with_sentinel, "1.2.3")
    assert any("CONTROL FAILED" in problem for problem in complaints), complaints
    assert check_artefact.verify("wheel", without, "1.2.3") == []


def test_a_star_does_not_cross_a_slash(tmp_path: Path) -> None:
    """Otherwise every pattern here is looser than it reads.

    `fnmatch` is the obvious implementation and its `*` matches `/`, which would let
    `*.dist-info/METADATA` be satisfied by a file nested anywhere at all - and a pattern that
    matches more than intended fails in the direction that publishes.
    """
    assert check_artefact.matches("*.dist-info/METADATA", ["sdk-1.0.dist-info/METADATA"])
    assert not check_artefact.matches("*.dist-info/METADATA", ["nested/sdk-1.0.dist-info/METADATA"])


def test_an_artefact_whose_recorded_version_is_not_the_tagged_one_is_refused(
    tmp_path: Path,
) -> None:
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


def test_the_security_document_counts_the_required_checks_the_ruleset_requires() -> None:
    """A number about our own configuration, written by hand, was wrong by one for two weeks.

    `docs/github-security.md` said "ten required checks" while `main.json` required eleven. Nobody
    was careless: the sentence was right when it was written, and every change since then added a
    *context* rather than revisiting the count of them. That is the same one-directional rot as a
    status section, and the fix is the same - derive it, so that the day an analysis is added the
    document fails until somebody has reread it.
    """
    from _claims import WORDS

    ruleset = json.loads((ROOT / ".github" / "rulesets" / "main.json").read_text())
    contexts = [
        rule["parameters"]["required_status_checks"]
        for rule in ruleset["rules"]
        if rule["type"] == "required_status_checks"
    ]
    assert len(contexts) == 1, "main.json has no single required_status_checks rule to count"
    count = len(contexts[0])
    assert count < len(WORDS), f"{count} is past the end of the number words in _claims.py"

    text = (ROOT / "docs" / "github-security.md").read_text(encoding="utf-8")
    # Every order the document uses. The first version of this pattern matched only "fourteen
    # required checks", so "Eleven required status checks" stayed wrong through three additions.
    matches = re.findall(
        r"(\w+) required (?:status )?checks|(\w+) (?:status )?checks required", text
    )
    written = [word.lower() for pair in matches for word in pair if word]
    assert written, (
        "docs/github-security.md no longer states a count of required checks, so this test would "
        "pass by finding nothing. Say the number, or delete this test deliberately."
    )
    for word in written:
        assert word == WORDS[count], (
            f"the ruleset requires {count} checks ({WORDS[count]}) and the document says {word!r}. "
            f"Adding a matrix entry or an analysis changes the first number and not the second."
        )


# --------------------------------------------------------------------------------------------------
# The npm dist-tag
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("version", "published", "tag"),
    [
        # The first candidate after the version published by hand: no final yet, so `latest`,
        # which is what an unpinned `pip install` gets on PyPI too.
        ("0.1.0-rc.1", ["0.1.0-dev.0"], "latest"),
        ("0.1.0-rc.2", ["0.1.0-dev.0", "0.1.0-rc.1"], "latest"),
        ("0.1.0", ["0.1.0-dev.0", "0.1.0-rc.1"], "latest"),
        # Once a final exists, a candidate must not take `latest` from it.
        ("0.2.0-rc.1", ["0.1.0-dev.0", "0.1.0"], "next"),
        ("0.2.0", ["0.1.0", "0.2.0-rc.1"], "latest"),
        # Semantic Versioning precedence, not string order: rc.10 is after rc.9, and a longer
        # prerelease ranks above its prefix.
        ("0.2.0-rc.10", ["0.1.0", "0.2.0-rc.9"], "next"),
        ("0.2.0-rc.1.1", ["0.1.0", "0.2.0-rc.1"], "next"),
    ],
)
def test_the_dist_tag_follows_what_an_unpinned_install_should_get(
    version: str, published: list[str], tag: str
) -> None:
    assert npm_dist_tag.choose(version, published)[0] == tag


@pytest.mark.parametrize(
    ("version", "published", "reason"),
    [
        ("0.1.0-rc.1", ["0.1.0-dev.0", "0.1.0-rc.1"], "already on the registry"),
        ("0.1.0-rc.1", ["0.1.0-rc.2"], "lower than 0.1.0-rc.2"),
        ("0.1.0-rc.9", ["0.1.0-rc.10"], "lower than 0.1.0-rc.10"),
        ("0.1.1", ["0.2.0"], "lower than 0.2.0"),
        ("0.1.1-rc.1", ["0.1.0", "0.2.0"], "lower than 0.2.0"),
        ("0.1", ["0.1.0-dev.0"], "not a semantic version"),
        ("01.0.0", ["0.1.0-dev.0"], "not a semantic version"),
    ],
)
def test_a_version_that_would_move_a_tag_backwards_or_repeat_is_refused(
    version: str, published: list[str], reason: str
) -> None:
    with pytest.raises(npm_dist_tag.Refused, match=re.escape(reason)):
        npm_dist_tag.choose(version, published)


def test_the_registry_answer_is_read_as_npm_prints_it() -> None:
    """`npm view <package> versions --json` prints a string when the registry holds one version."""
    assert npm_dist_tag.published_versions('"0.1.0-dev.0"') == ["0.1.0-dev.0"]
    assert npm_dist_tag.published_versions('["0.1.0-dev.0", "0.1.0-rc.1"]') == [
        "0.1.0-dev.0",
        "0.1.0-rc.1",
    ]
    for broken in ("[]", "", "not json", "{}", "[1]", '["0.1"]'):
        with pytest.raises(npm_dist_tag.Refused):
            npm_dist_tag.published_versions(broken)


def test_the_script_prints_one_tag_for_github_output_and_refuses_with_status_1(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert npm_dist_tag.main(["npm_dist_tag.py", "0.1.0-rc.1", '"0.1.0-dev.0"']) == 0
    captured = capsys.readouterr()
    assert captured.out == "tag=latest\n"
    assert "no final version is published" in captured.err
    assert npm_dist_tag.main(["npm_dist_tag.py", "0.1.0-rc.1", "[]"]) == 1
    assert capsys.readouterr().out == ""


def test_no_npm_publish_in_this_repository_goes_without_an_explicit_tag() -> None:
    """npm 11 refuses a prerelease without `--tag`, after the gate and the reviewer have said yes.

    The release workflow published with `npm publish "$tarball" --access public`, and the runbook
    told a person to type the same for the first, hand-made publish. Both were prereleases. So
    every `npm publish` a workflow runs or a document tells someone to type must name its tag.
    """
    places = [ROOT / ".github" / "workflows" / "release.yml", ROOT / "docs" / "publishing.md"]
    found = 0
    for path in places:
        for line in path.read_text(encoding="utf-8").splitlines():
            if re.search(r"\bnpm publish\b", line) and not line.lstrip().startswith(("#", "//")):
                if "`npm publish`" in line or "npm publish --provenance" in line:
                    continue  # prose naming the command, not an invocation
                found += 1
                assert "--tag" in line, f"{path.relative_to(ROOT)}: {line.strip()}"
    assert found >= 2, "the check found no invocation to look at, so it would pass on anything"


def test_the_publish_job_takes_the_tag_the_build_job_chose_from_the_registry() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    build = workflow.split("  build-npm:", 1)[1].split("\n  publish-npm:", 1)[0]
    publish = workflow.split("  publish-npm:", 1)[1]
    assert "dist_tag: ${{ steps.dist_tag.outputs.tag }}" in build
    assert 'npm view "@smart-data-engines/sde" versions --json' in build
    assert 'python3 tools/npm_dist_tag.py "$VERSION" "$published" >> "$GITHUB_OUTPUT"' in build
    assert publish.count("DIST_TAG: ${{ needs.build-npm.outputs.dist_tag }}") == 2
    assert 'npm publish "$tarball" --access public --tag "$DIST_TAG"' in publish
    assert '"dist-tags.$DIST_TAG"' in publish


def test_the_registry_check_waits_longer_than_the_registry_caches() -> None:
    """The package document is cached for 300 seconds, and the build job fetched it just before.

    Measured on the hand-made `0.1.0-dev.0`: published at 17:57:51Z and visible at 18:02:58Z. A
    check that gave up after a minute would have reported a successful publish as a failure.
    """
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    publish = workflow.split("  publish-npm:", 1)[1]
    loop = re.search(r"for attempt in \$\(seq 1 (\d+)\); do(.*?)done", publish, re.DOTALL)
    assert loop is not None, "the registry check is no longer a bounded loop"
    pause = re.search(r"sleep (\d+)", loop.group(2))
    assert pause is not None
    assert int(loop.group(1)) * int(pause.group(1)) > 300 + 60


def test_the_publishing_job_says_what_it_presented_and_why_npm_refused() -> None:
    """ENEEDAUTH alone is not a reason, and 0.1.0-rc.1 was refused twice with nothing else.

    npm's OIDC helper never throws: it logs why the exchange produced no token at verbose level,
    into its debug log, and the console shows only that no credential exists. So the job prints the
    claims the registry compares with the trusted publisher, and on a failed publish the `oidc`
    lines of npm's debug log - never the token itself.
    """
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    publish = workflow.split("  publish-npm:", 1)[1]
    claims = publish.split("What the registry will be asked to trust", 1)[1]
    claims = claims.split("- name: Publish", 1)[0]
    for claim in ("repository", "repository_owner", "workflow_ref", "environment"):
        assert f'"{claim}"' in claims, claim
    assert "audience=npm:registry.npmjs.org" in claims
    assert 'print(f"{key}: {claims.get(key)}")' in claims
    # The token is decoded, never printed.
    assert "SDE_ID_TOKEN" in claims
    assert not re.search(r"(echo|print)\W+\$?\{?SDE_ID_TOKEN", claims)
    assert 'grep -h "oidc" "$HOME"/.npm/_logs/*-debug-0.log' in publish


# --------------------------------------------------------------------------------------------------
# The bill of materials of each artefact, attested beside it
# --------------------------------------------------------------------------------------------------

WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"


def _bill(
    wheel: Path,
    *,
    version: str = "1.2.3",
    digest: str | None = None,
    names: tuple[str, ...] = ("cryptography", "psycopg"),
) -> dict[str, object]:
    """A CycloneDX bill as `cyclonedx-py environment` writes one for an installed wheel."""
    ours = {
        "name": "smart-data-engine-sdk",
        "version": version,
        "externalReferences": [
            {
                "type": "distribution",
                "url": f"file:///tmp/dist/{wheel.name}",
                "hashes": [
                    {
                        "alg": "SHA-256",
                        "content": digest or hashlib.sha256(wheel.read_bytes()).hexdigest(),
                    }
                ],
            }
        ],
    }
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "components": [ours, *({"name": name, "version": "1.0"} for name in names)],
    }


def test_a_bill_of_the_wheel_it_was_installed_from_has_nothing_to_complain_about(
    tmp_path: Path,
) -> None:
    wheel = _wheel(tmp_path / "w.whl")
    bill = _bill(wheel)
    assert check_sbom.check_python(bill, wheel, "1.2.3", ["cryptography", "Psycopg"]) == []


def test_a_bill_of_another_build_of_the_same_version_is_refused(tmp_path: Path) -> None:
    """The version alone would let the bill of a rebuild stand for this wheel."""
    wheel = _wheel(tmp_path / "w.whl")
    problems = check_sbom.check_python(_bill(wheel, digest="ab" * 32), wheel, "1.2.3")
    assert len(problems) == 1 and "another build of the same version" in problems[0]


def test_a_bill_at_another_version_is_refused(tmp_path: Path) -> None:
    wheel = _wheel(tmp_path / "w.whl")
    problems = check_sbom.check_python(_bill(wheel, version="1.2.2"), wheel, "1.2.3")
    assert problems == ["the bill has smart-data-engine-sdk at ['1.2.2'], not at 1.2.3"]


def test_a_bill_without_a_distribution_an_extra_brings_names_it(tmp_path: Path) -> None:
    wheel = _wheel(tmp_path / "w.whl")
    problems = check_sbom.check_python(_bill(wheel), wheel, "1.2.3", ["clickhouse-connect"])
    assert problems == [
        "the bill does not hold clickhouse-connect, which the installed extras bring"
    ]


@pytest.mark.parametrize("broken", ["name", "digest"])
def test_a_matcher_stuck_on_found_cannot_report_a_perfect_bill(tmp_path: Path, broken: str) -> None:
    """The control: a name and a digest that cannot be in a bill, and a bill that has them."""
    wheel = _wheel(tmp_path / "w.whl")
    bill = _bill(wheel, names=("cryptography", check_sbom.CONTROL) if broken == "name" else ())
    if broken == "digest":
        components = bill["components"]
        assert isinstance(components, list)
        components[0]["externalReferences"][0]["hashes"].append(
            {"alg": "SHA-256", "content": check_sbom.NOTHING}
        )
    problems = check_sbom.check_python(bill, wheel, "1.2.3")
    assert len(problems) == 1 and problems[0].startswith("CONTROL FAILED")


def test_the_npm_bill_is_known_by_its_package_url_and_not_by_its_name() -> None:
    """npm fills the root component's name from the directory - `typescript` here."""

    def bill(purl: str) -> dict[str, object]:
        root = {"type": "library", "name": "typescript", "version": "1.2.3", "purl": purl}
        return {"bomFormat": "CycloneDX", "specVersion": "1.5", "metadata": {"component": root}}

    ours = "pkg:npm/%40smart-data-engines/sde@1.2.3"
    assert check_sbom.check_npm(bill(ours), "1.2.3") == []
    assert check_sbom.check_npm(bill(ours + "?vcs_url=x"), "1.2.3") == []
    for other in ("pkg:npm/%40smart-data-engines/sde@1.2.2", "pkg:npm/typescript@1.2.3", ""):
        assert len(check_sbom.check_npm(bill(other), "1.2.3")) == 1, other


@pytest.mark.parametrize("bill", [{"bomFormat": "SPDX"}, [], {"bomFormat": "CycloneDX"}])
def test_a_file_that_is_not_a_cyclonedx_bill_is_refused(tmp_path: Path, bill: object) -> None:
    wheel = _wheel(tmp_path / "w.whl")
    assert len(check_sbom.check_python(bill, wheel, "1.2.3")) == 1
    assert len(check_sbom.check_npm(bill, "1.2.3")) == 1


def _job(name: str) -> str:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    job = workflow.split(f"\n  {name}:\n", 1)[1]
    return re.split(r"\n  [a-z][a-z-]*:\n", job, maxsplit=1)[0]


def test_the_wheel_s_bill_holds_every_extra_a_client_may_choose_and_what_each_brings() -> None:
    """A bill of the wheel alone would leave out what a client installs with it: the drivers.

    The extras installed for the bill are the ones `pyproject.toml` offers a client, and the
    distributions the bill must hold are the ones those extras name - so an extra added there
    and not here fails this, rather than a release whose bill leaves it out.
    """
    import tomllib

    project = tomllib.loads((ROOT / "python" / "pyproject.toml").read_text(encoding="utf-8"))
    offered = {
        extra: [re.split(r"[\[<>=!~; ]", requirement, maxsplit=1)[0] for requirement in names]
        for extra, names in project["project"]["optional-dependencies"].items()
        if extra != "dev"
    }
    build = _job("build-python")
    installed = re.search(r'"\$wheel\[([a-z,]+)\]"', build)
    required = re.search(r"--requires ([a-z -]+)\n", build)
    assert installed is not None and required is not None, "the bill is no longer built here"
    assert sorted(installed.group(1).split(",")) == sorted(offered)
    assert sorted(required.group(1).split()) == sorted(
        n for names in offered.values() for n in names
    )
    assert 'python3 tools/check_sbom.py python sbom-python.cdx.json "$wheel" "$VERSION"' in build


@pytest.mark.parametrize(
    ("job", "bill", "publish"),
    [
        ("publish-pypi", "sbom/sbom-python.cdx.json", "pypa/gh-action-pypi-publish@"),
        ("publish-npm", "sbom/sbom-npm.cdx.json", 'npm publish "$tarball"'),
    ],
)
def test_each_file_is_attested_with_its_bill_before_it_is_published(
    job: str, bill: str, publish: str
) -> None:
    """After the upload an attestation that failed would leave a published file without its bill,
    and a version number that cannot be published again."""
    text = _job(job)
    assert "      attestations: write\n" in text
    attest = text.index("uses: actions/attest@")
    assert f"sbom-path: {bill}" in text[attest:]
    assert attest < text.index(publish)

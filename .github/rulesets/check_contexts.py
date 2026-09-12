#!/usr/bin/env python3
"""Refuse a ruleset that has drifted from the workflows it is supposed to gate.

`docs/github-security.md` names two ways this configuration fails quietly, and a warning in a document
is exactly the kind of protection this repository does not accept elsewhere:

- **A required context that no job produces is a permanent block, not an error.** The pull request
  waits for a check that will never report. Renaming a job does this; so does a typo.
- **A job whose context nothing requires runs and gates nothing.** Adding Python 3.14 to `ci.yml`
  creates `python (3.14)`, which goes green on every pull request and blocks nothing until somebody
  remembers this file. That drift *looks* like coverage, which is what makes it the more dangerous of
  the two.

So this compares the contexts the workflows actually produce against the contexts `main.json` requires,
in both directions, and fails on any difference. What it cannot see is the live ruleset on GitHub -
that needs a token this job does not have, and should not. It checks the half that drifts when someone
edits a workflow, which is the half that drifts.

Since 12 September 2026 it checks the release workflow against `tags.json` the same way, plus three
properties of publishing that are otherwise held by a comment:

- **A tag pattern that drives a publish must be protected.** `refs/tags/v*` is immutable; a
  `rust-v*` trigger added here and forgotten there would publish from a tag that can still be
  deleted and repointed afterwards, which is the one thing a released version must not be.
- **Every action is pinned to a commit SHA.** A tag is a mutable pointer: `@v4` can be moved to
  different code by whoever owns that repository, with no diff here.
- **A job holding `id-token: write` holds the entire publishing credential**, so it must sit behind
  an environment with a reviewer, and it must not check out this repository - the point of splitting
  build from publish is that nothing which has run repository or dependency code holds the identity.

Run from the repository root:

    python3 .github/rulesets/check_contexts.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

import yaml

WORKFLOWS = Path(".github/workflows")
RULESET = Path(".github/rulesets/main.json")

# Contexts required by the ruleset that no workflow in this repository produces, with the reason. Each
# entry is a deliberate exception and has to stay short: an allowlist that grows is this check being
# switched off one line at a time.
NOT_FROM_A_WORKFLOW = {
    # Posted by GitHub's code scanning integration, not by our `analyze` jobs. Ours go green when the
    # job succeeds; this one fails when the analysis produced a new alert. Requiring only ours would
    # require that the scan ran, not that it found nothing.
    "CodeQL": "posted by the code scanning integration",
}

_MATRIX_REF = re.compile(r"\$\{\{\s*matrix\.([A-Za-z_][A-Za-z0-9_-]*)\s*\}\}")

TAGS_RULESET = Path(".github/rulesets/tags.json")

#: The filename both registries' trusted publishers are pinned to. Renaming this file revokes
#: publishing on PyPI and on npm simultaneously.
RELEASE = "release.yml"

#: `owner/repo@<40 hex>`. Local actions (`./.github/...`) are exempt: they are in this tree and a
#: diff here is exactly what reviewing them means.
_PINNED = re.compile(r"^[^@\s]+@[0-9a-f]{40}$")



def triggers_on_pull_request(workflow: dict[str, Any]) -> bool:
    """Only a job that runs on pull requests can gate one.

    `on` is the YAML 1.1 boolean `True` after parsing, not the string "on" - which is the kind of
    detail that makes a checker silently pass by finding nothing to check, so both spellings are read
    and a workflow with neither is an error rather than a skip.
    """
    for key in ("on", True):
        if key in workflow:
            on = workflow[key]
            if isinstance(on, str):
                return on == "pull_request"
            if isinstance(on, list):
                return "pull_request" in on
            if isinstance(on, dict):
                return "pull_request" in on
    raise SystemExit(f"a workflow has no `on:` trigger at all: {workflow.get('name')!r}")


def contexts_of(job_id: str, job: dict[str, Any]) -> list[str]:
    """The status check names GitHub will report for one job.

    Three rules, and the second is the one that surprises people: GitHub uses the job's `name:` when
    it has one and the job *id* when it does not, and for a matrix job it appends the matrix values in
    parentheses unless `name:` already interpolates them itself.
    """
    label = job.get("name", job_id)
    matrix = (job.get("strategy") or {}).get("matrix") or {}
    axes = {k: v for k, v in matrix.items() if isinstance(v, list)}

    if not axes:
        if _MATRIX_REF.search(str(label)):
            raise SystemExit(
                f"job {job_id!r} interpolates a matrix value into its name but declares no matrix "
                f"axis. Its context name cannot be predicted, so this checker would silently pass."
            )
        return [str(label)]

    referenced = set(_MATRIX_REF.findall(str(label)))
    if referenced:
        # `name: analyze (${{ matrix.language }})` - GitHub substitutes and adds nothing.
        unknown = referenced - set(axes)
        if unknown:
            raise SystemExit(f"job {job_id!r} refers to matrix axes that do not exist: {unknown}")
        if len(axes) != 1:
            raise SystemExit(
                f"job {job_id!r} has {len(axes)} matrix axes and an interpolated name. This checker "
                f"only knows how to predict the single-axis case; teach it before adding the second."
            )
        (axis,) = axes
        return [_MATRIX_REF.sub(lambda _: str(v), str(label)) for v in axes[axis]]

    if len(axes) != 1:
        raise SystemExit(
            f"job {job_id!r} has {len(axes)} matrix axes. GitHub joins the values with ', ' and this "
            f"checker only implements the single-axis case; teach it before adding the second."
        )
    (axis,) = axes
    return [f"{label} ({v})" for v in axes[axis]]


def produced() -> dict[str, str]:
    """Every context a pull request in this repository will see, mapped to its workflow file."""
    found: dict[str, str] = {}
    files = sorted(WORKFLOWS.glob("*.yml")) + sorted(WORKFLOWS.glob("*.yaml"))
    if not files:
        raise SystemExit(f"no workflows found under {WORKFLOWS}. Nothing to check, which is a bug.")
    for path in files:
        workflow = yaml.safe_load(path.read_text())
        if not triggers_on_pull_request(workflow):
            continue
        for job_id, job in (workflow.get("jobs") or {}).items():
            for context in contexts_of(job_id, job):
                if context in found:
                    raise SystemExit(
                        f"two jobs report the same context {context!r} ({found[context]} and "
                        f"{path.name}). One of them would satisfy a rule meant for the other."
                    )
                found[context] = path.name
    return found


def required() -> set[str]:
    ruleset = json.loads(RULESET.read_text())
    for rule in ruleset["rules"]:
        if rule["type"] == "required_status_checks":
            return {c["context"] for c in rule["parameters"]["required_status_checks"]}
    raise SystemExit(
        f"{RULESET} has no required_status_checks rule. Either protection was removed or this "
        f"checker is pointed at the wrong file; both are worth failing on."
    )



def parsed_workflows() -> dict[str, dict[str, Any]]:
    """Every workflow file, by filename. Unlike `produced()` this does not skip anything.

    `produced()` looks only at workflows that run on pull requests, because only those can gate one.
    The release checks below are about a workflow that deliberately does *not* run on pull requests,
    so they need the unfiltered set - and a checker that reused the filtered one would find no
    release workflow and report success.
    """
    files = sorted(WORKFLOWS.glob("*.yml")) + sorted(WORKFLOWS.glob("*.yaml"))
    return {path.name: yaml.safe_load(path.read_text()) for path in files}


def triggers(workflow: dict[str, Any]) -> dict[str, Any]:
    """The `on:` block. `on` parses as the YAML 1.1 boolean `True`, so both spellings are read."""
    for key in ("on", True):
        if key in workflow:
            block = workflow[key]
            if isinstance(block, dict):
                return block
            if isinstance(block, str):
                return {block: None}
            if isinstance(block, list):
                return dict.fromkeys(block)
    raise SystemExit(f"a workflow has no `on:` trigger at all: {workflow.get('name')!r}")


def tag_patterns(workflow: dict[str, Any]) -> list[str]:
    """The tag globs this workflow runs on, normalised to `refs/tags/...` as the ruleset writes them."""
    push = triggers(workflow).get("push") or {}
    if not isinstance(push, dict):
        return []
    return [f"refs/tags/{pattern}" for pattern in (push.get("tags") or [])]


def protected_tag_patterns() -> list[str]:
    ruleset = json.loads(TAGS_RULESET.read_text())
    if ruleset.get("target") != "tag":
        raise SystemExit(f"{TAGS_RULESET} does not target tags, so it protects no release.")
    return list(ruleset["conditions"]["ref_name"]["include"])


def release_problems() -> list[str]:
    """Everything wrong with how this repository publishes, as far as these files can tell."""
    problems: list[str] = []
    files = parsed_workflows()

    # Which workflows publish from a tag. There should be exactly one, and it should be the file the
    # trusted publishers name.
    publishing = {name: tag_patterns(wf) for name, wf in files.items() if tag_patterns(wf)}
    if not publishing:
        problems.append(
            f"  no workflow triggers on a tag, so nothing publishes\n"
            f"    Either {RELEASE} was deleted or its trigger was changed. Both registries are "
            f"configured to accept an OIDC token only from that file, so this is not a state the "
            f"repository can release from."
        )
    for name in sorted(set(publishing) - {RELEASE}):
        problems.append(
            f"  {name} publishes from a tag, and the trusted publishers do not name it\n"
            f"    PyPI and npm each pin the publisher to a workflow *filename*. A tag-triggered "
            f"publish from any other file cannot mint a token, so it fails at the registry after "
            f"the reviewer has already approved it."
        )

    # The two directions, exactly as for contexts.
    protected = protected_tag_patterns()
    triggered = sorted({pattern for patterns in publishing.values() for pattern in patterns})

    for pattern in triggered:
        if pattern not in protected:
            problems.append(
                f"  publishes from {pattern!r}, which {TAGS_RULESET} does not protect\n"
                f"    That tag can be deleted and repointed after the release, so the commit a "
                f"published version claims to come from would stop being fixed. Add it to the "
                f"ruleset and re-apply."
            )
    for pattern in protected:
        if pattern not in triggered:
            problems.append(
                f"  {TAGS_RULESET} protects {pattern!r}, which no workflow publishes from\n"
                f"    Harmless on its own, and worth failing on anyway: it is how the pair drifts. "
                f"Either the trigger was dropped - in which case tagging silently does nothing - or "
                f"the pattern is dead configuration."
            )

    # Properties that were previously held only by a comment.
    for name, workflow in sorted(files.items()):
        for job_id, job in (workflow.get("jobs") or {}).items():
            permissions = job.get("permissions") or {}
            holds_identity = (
                isinstance(permissions, dict) and permissions.get("id-token") == "write"
            )
            steps = job.get("steps") or []

            for step in steps:
                uses = step.get("uses")
                if uses and not uses.startswith("./") and not _PINNED.match(str(uses)):
                    problems.append(
                        f"  {name}: job {job_id!r} uses {uses!r}, which is not pinned to a SHA\n"
                        f"    A tag is a mutable pointer; the code it names can be replaced without "
                        f"a diff in this repository. Resolve with "
                        f"`git ls-remote <url> 'refs/tags/<tag>^{{}}'` - without the `^{{}}` an "
                        f"annotated tag gives you the tag object rather than the commit, and the "
                        f"symptom is Dependabot offering a version as an upgrade from itself."
                    )

            if not holds_identity:
                continue

            if not job.get("environment"):
                problems.append(
                    f"  {name}: job {job_id!r} may mint an OIDC token and is behind no environment\n"
                    f"    `id-token: write` is the whole publishing credential here - there is no "
                    f"token anywhere to also need. Without an environment carrying a required "
                    f"reviewer, a merge becomes a publish with nobody in the loop."
                )
            for step in steps:
                uses = str(step.get("uses") or "")
                if uses.startswith("actions/checkout@"):
                    problems.append(
                        f"  {name}: job {job_id!r} holds the publishing identity and checks out "
                        f"this repository\n"
                        f"    The reason build and publish are separate jobs is that nothing which "
                        f"has run repository or dependency code holds the credential. Checking out "
                        f"here puts it back."
                    )

    return problems


def main() -> int:
    from_workflows = produced()
    from_ruleset = required()

    problems: list[str] = []

    for context in sorted(from_ruleset - set(from_workflows) - set(NOT_FROM_A_WORKFLOW)):
        problems.append(
            f"  required but produced by nothing: {context!r}\n"
            f"    A pull request waits for this forever. Either a job was renamed, or the context "
            f"string is wrong - remember a matrix job is 'python (3.11)', not 'python'."
        )

    for context in sorted(set(from_workflows) - from_ruleset):
        problems.append(
            f"  produced but not required: {context!r}  (from {from_workflows[context]})\n"
            f"    It runs on every pull request and gates nothing, which looks like coverage. Add it "
            f"to {RULESET} and re-apply the ruleset, or delete the job."
        )

    for context, why in sorted(NOT_FROM_A_WORKFLOW.items()):
        if context not in from_ruleset:
            problems.append(
                f"  allowlisted but no longer required: {context!r} ({why})\n"
                f"    The exception in this script is now dead configuration. Remove it, or put the "
                f"context back in {RULESET}."
            )

    release = release_problems()

    if problems or release:
        print("the rulesets and the workflows disagree:\n")
        print("\n".join(problems + release))
        return 1

    print(
        f"{len(from_workflows)} contexts produced, all required; "
        f"{len(NOT_FROM_A_WORKFLOW)} allowlisted as not coming from a workflow"
    )
    print(
        f"{len(protected_tag_patterns())} tag patterns protected, all of them published from; "
        f"every action pinned to a SHA"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

# Rulesets as code

The GitHub rulesets protecting this repository, kept in version control so the configuration is
reviewable and reproducible rather than living only in the web UI.

These files are **not applied automatically**. GitHub does not read them; they are the inputs for the
`gh` commands below.

## Apply

```bash
REPO=Smart-Data-Engines/smart-data-engine-sdk

# First time: create. Afterwards use PUT with the id the create call returned, because POST would
# make a second ruleset and two rulesets on one branch are additive - which reads as "the change did
# nothing" when it actually doubled the rules.
gh api -X POST "repos/$REPO/rulesets" --input .github/rulesets/main.json
gh api -X POST "repos/$REPO/rulesets" --input .github/rulesets/tags.json

# Update (PUT replaces the ruleset wholesale, so send the whole file)
gh api -X PUT "repos/$REPO/rulesets/<id>" --input .github/rulesets/main.json
```

## Verify

```bash
REPO=Smart-Data-Engines/smart-data-engine-sdk
gh api "repos/$REPO/rulesets" --jq '.[] | {id, name, target, enforcement}'
gh api "repos/$REPO/rulesets/<id>" --jq '{rules: [.rules[].type], bypass: .bypass_actors}'

# The useful one: the checks a pull request actually has to pass.
gh api "repos/$REPO/rulesets/<id>" --jq '.rules[] | select(.type=="required_status_checks")
       | .parameters.required_status_checks[].context'
```

## The check contexts are exact strings, and matrix jobs are not named what you think

A required check is matched on the exact context string GitHub reports. For a matrix job that string
is `<job id> (<matrix value>)` — so this repository requires `python (3.11)` and not `python`, and
`analyze (javascript-typescript)` and not `codeql`. Two consequences worth knowing before editing a
workflow:

- **A context that never reports is not an error, it is a permanent block.** A required check with a
  typo, or one whose job was renamed, simply never arrives, and the PR waits forever. Read the names
  off a real run rather than from the workflow file:

  ```bash
  gh pr checks <number> --json name --jq '.[].name'
  ```

- **Adding a matrix entry adds a context nobody requires.** Adding Python 3.14 to `ci.yml` creates
  `python (3.14)`, which runs on every PR and blocks nothing until it is added here. That is the
  quiet failure mode of this file: the workflow and the ruleset drift, and the drift looks like
  green.

## Pushing a history rewrite

Rewriting published history needs the ruleset out of the way, and removing rules one at a time does
not work: `non_fast_forward` blocks the force push, `required_linear_history` rejects any push
containing a merge commit, `pull_request` blocks every direct push to `main`, and
`required_status_checks` rejects SHAs that have never been checked. The only reliable path is to flip
`enforcement` to `disabled` for the duration.

No "protection off" file is kept in this directory on purpose — a ready-made switch invites casual
use. Build it on the spot, and restore protection in the same command so a failed push cannot leave
the branch exposed:

```bash
R=Smart-Data-Engines/smart-data-engine-sdk
ID=<id>
jq '.enforcement = "disabled"' .github/rulesets/main.json > /tmp/off.json

gh api -X PUT "repos/$R/rulesets/$ID" --input /tmp/off.json --jq '.enforcement'
git push --force-with-lease=main:<current-sha> origin <branch>:main
gh api -X PUT "repos/$R/rulesets/$ID" --input .github/rulesets/main.json --jq '.enforcement'
```

Use `--force-with-lease` with an explicit SHA, never a bare `--force`: it is what stops the push when
something has landed in the meantime, which is exactly what happened the first time this was done in
the engine repository — two Dependabot PRs had merged.

## Notes

- `bypass_actors` is deliberately empty. The rules exist to catch the maintainer's own mistakes, so
  granting yourself a bypass defeats them. The escape hatch for a genuinely stuck queue is
  `gh pr merge --admin`, which leaves a trace on the PR; a standing bypass leaves none.
- `required_status_checks` contexts must have reported to GitHub at least once before they can be
  enforced, so let the workflows run on a pull request before applying this.
- `required_signatures` is intentionally absent. Add it only after registering an SSH or GPG
  **signing** key on the account and confirming a commit shows as Verified. Enabling it first locks
  you out of your own branch, and the lockout is total: you cannot push the fix either.
- `required_linear_history` and `allowed_merge_methods` are consistent with each other: squash and
  rebase preserve a linear history, a merge commit does not. Change both together or neither.
- `required_approving_review_count` stays at `0` while there is one maintainer — a required review
  you grant yourself is theatre. Raise it to `1` the day a second person has write access, and turn
  on `require_code_owner_review` at the same time, since `CODEOWNERS` is what makes it land on the
  right person.

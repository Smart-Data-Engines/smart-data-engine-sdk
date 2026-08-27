# Securing the public GitHub repository

This repository is the public half of an open-core product. Two things follow from that, and they
pull in opposite directions.

The first is the ordinary consequence: anyone can read the build configuration, the dependency
choices and the CI. The second is the one that matters more here. The central claim of this product
is *not one row of your data reaches us*, and the reason the client library is open source at all is
so a sceptic can check that claim by reading the code rather than believing a page on a website.
That makes the integrity of what ships the product argument itself. A repository where someone else
can quietly change `telemetry.py` is a repository whose invitation to read the code means nothing.

The engine repository — `low-cost-and-low-latency-orderbook-dbengine` — has the same document, and
this one deliberately mirrors it so a reader knows both repositories are held to one standard. Where
they differ, the difference is called out rather than left to be noticed.

**What is different here, in one list:**

- **This library gets published to PyPI and npm.** The engine ships source; this ships an artefact
  that runs inside other people's applications and holds their database credentials. A compromised
  release here is a far worse event than a compromised release there, which is what §4 is about.
- **The distribution names were not registered.** `smart-data-engine` on PyPI was unclaimed while the
  README told readers to `pip install smart-data-engine`. See §4.1; this is the single most urgent
  item in this document.
- **Two languages, so two analyses and ten required checks**, not four (§1).
- **The default branch is `main`, not `master`.** The rulesets differ in that one string, and a
  ruleset targeting the wrong ref is silently inert.
- **No runtime dependencies at all** in either package (§7), which removes most of the engine's §7
  problem and replaces it with a different one: a large `devDependencies` tree that only CI installs.

Everything below is either a file in this repository (marked ✅), a setting applied through the API
(marked ✅ with the value that was verified afterwards), or something that still needs a human
(marked ⚙️).

## 1. Protect the default branch

### What is in place ✅

A ruleset named `main` is active on `refs/heads/main` with **no bypass actors**, enforcing:

- `deletion` — the branch cannot be deleted
- `non_fast_forward` — no force pushes
- `required_linear_history` — no merge commits, so `git log main` stays readable
- `pull_request` — direct pushes are blocked, with `required_review_thread_resolution: true` so an
  unresolved comment cannot be merged past, and `allowed_merge_methods: [squash, rebase]`
- `required_status_checks` with `strict: true` and **eleven contexts**

The eleven are the whole matrix, not a representative sample:

```
python (3.11)   python (3.12)   python (3.13)
typescript (18) typescript (20) typescript (22)
contract
analyze (python)  analyze (javascript-typescript)  analyze (actions)
CodeQL
```

`CodeQL` is not a fourth analysis job, and it is easy to miss. The three `analyze (...)` contexts come
from our own workflow and go green when the *job* succeeds; `CodeQL` is posted by GitHub's code
scanning integration and is the one that fails when the analysis has produced a **new alert**.
Requiring the three without it would require that the scan ran, not that it found nothing. There are
zero open alerts today, so requiring it costs nothing to adopt — which is the only moment it is cheap.

Requiring all six language-version cells rather than one is specific to this repository and worth the
minute it costs. The product's guarantee is that four implementations encode a model to identical
bytes; a canonical encoding that drifts with a Python release or a Node release is precisely the
failure the byte contract exists to prevent, and a matrix cell that runs but blocks nothing is how
that drift would arrive looking green.

`contract` is the cheapest and most valuable of the eleven: it rebuilds the wheel and checks the PEP 561
marker is inside it, re-derives every committed hashing digest with `openssl`, and refuses a change to
the one hand-written conformance vector without a deliberate edit to the workflow.

The ruleset is kept as JSON in `.github/rulesets/` ✅ so the configuration is reviewable in a diff
rather than living only in the web UI. That directory's README covers applying it, verifying it, and
the two mistakes this configuration invites — see §1.2.

### 1.1 Why `bypass_actors` is empty

The rules exist to catch the maintainer's own mistakes. A standing bypass for yourself removes exactly
the case they were bought for, and it leaves no trace when used. The escape hatch for a genuinely
stuck queue is `gh pr merge --admin`, which is visible on the pull request afterwards; note on the PR
why it was needed.

### 1.2 Two ways this configuration fails quietly

**A required context is an exact string, and a matrix job is not named what the file calls it.** The
job id is `python`; the context is `python (3.11)`. Requiring `python` would create a check that never
reports — and a required check that never reports is not an error, it is a permanent block. Read the
names off a real run:

```bash
gh pr checks <number>
```

**Adding a matrix entry adds a context nobody requires.** Adding Python 3.14 to `ci.yml` produces
`python (3.14)`, which runs on every PR and gates nothing until it is added to `main.json` and
re-applied. The workflow and the ruleset drift, and the drift looks like green.

Both of those are now checked mechanically rather than warned about. `.github/rulesets/check_contexts.py`
derives the contexts the workflows will actually report — job id or `name:`, with matrix values
appended the way GitHub appends them — and compares that set against `main.json` in both directions.
It runs in the `contract` job, so a renamed job or a new matrix cell fails the pull request that
introduced it. `CodeQL` is the one allowlisted exception, because no job of ours produces it; the
allowlist is checked too, so removing that context from the ruleset fails as dead configuration.

What it cannot see is the **live** ruleset on GitHub, which needs a token the job does not have and
should not. It covers the half that drifts when somebody edits a workflow, which is the half that
drifts. After changing `main.json`, re-apply it — see `.github/rulesets/README.md`.

### 1.3 `required_signatures` is deliberately absent ⚙️

Add it only after registering an SSH or GPG **signing** key on the account and confirming a commit
shows as Verified. Today the commits here are unsigned, so enabling it first would lock the maintainer
out of `main` completely — including out of pushing the fix. See §6.

### Tags ✅

A second ruleset named `release tags` targets `refs/tags/v*` and enforces `deletion` and
`non_fast_forward`. This matters more here than in the engine: a tag is what a release workflow builds
from, so a silently moved tag is a published package containing code nobody reviewed.

## 2. GitHub's scanning

| Feature | State | Why |
|---|---|---|
| Secret scanning | ✅ enabled | Catches a leaked key the moment it is pushed. Free for public repositories. |
| Push protection | ✅ enabled | Blocks the push instead of reporting it afterwards. |
| Dependabot alerts | ✅ enabled | Vulnerability alerts for dependencies. |
| Dependabot security updates | ✅ enabled | Opens the bump PR, rather than only telling you one is needed. |
| Private vulnerability reporting | ✅ enabled | Lets a researcher report privately instead of opening a public issue — which, given `SECURITY.md` invites exactly that, was previously a broken promise. |
| Code scanning (CodeQL) | ✅ `.github/workflows/codeql.yml` | Three analyses; see §3.1. |
| Non-provider secret patterns | ⚙️ needs the organisation | See below. |
| Secret scanning validity checks | ⚙️ needs the organisation | See below. |

The last two are worth a paragraph, because of *how* they fail. `PATCH /repos/{owner}/{repo}` accepts
both with HTTP 200 and leaves them `disabled`; the response body reports the old value. They are
organisation-level Secret Protection features, not repository ones. The lesson generalises past these
two settings: **a 200 from this API is not evidence that a setting changed.** Read the value back.

A leaked secret must be **rotated, not deleted**. Removing a commit does not un-leak it: GitHub keeps
unreachable objects reachable through the API for a while, and forks keep copies indefinitely.

## 3. Harden CI ✅

In the tree:

- `.github/workflows/ci.yml` — lint, `mypy --strict`, the full test suite on three Python versions
  against a real PostgreSQL, `tsc` and `vitest` on three Node versions, plus the `contract` job
- `.github/workflows/codeql.yml` — see §3.1
- `.github/dependabot.yml` — three ecosystems: actions, `pip` under `python/`, `npm` under
  `typescript/`

Rules that hold:

- **Third-party actions are pinned to full commit SHAs** ✅, with the version in a trailing comment. A
  tag is a mutable pointer: whoever controls `actions/checkout` can move `@v4` to different code, and
  a step whose contents can change without a diff here has no place in a repository this one is judged
  by. Dependabot understands this form and bumps the SHA and the comment together.
- **The repository now *requires* SHA pinning** ✅ (`sha_pinning_required: true` on
  `/actions/permissions`), so an unpinned action is refused rather than merely discouraged. The cost is
  worth stating: a workflow that unpins one does not fail a test, it fails to start — which reads as
  infrastructure trouble rather than as policy.
- **Least-privilege `permissions:`** ✅ — `contents: read` at workflow level, widened only where
  needed (CodeQL needs `security-events: write`). The repository default for `GITHUB_TOKEN` is
  read-only ✅ and workflows cannot approve pull requests ✅.
- **Never use `pull_request_target`** with a checkout of the PR head. That combination hands a fork's
  code a write token. `pull_request` is the safe trigger and is what both workflows use.
- **Do not interpolate untrusted input into `run:`** — a PR title or branch name containing `$(...)`
  is shell injection. Pass values through `env:`. This is one of the things `analyze (actions)` is
  there to catch.
- **Fork pull requests need approval before workflows run** ✅ — set to
  `all_external_contributors`, not GitHub's default of first-time contributors only. A second PR from
  the same account should not be exempt from review just because the first one was benign.

### 3.1 CodeQL: three languages, and the default query suite on purpose

`python`, `javascript-typescript`, and `actions`. The third analyses the workflow files themselves,
which is what notices a future edit unpinning an action or interpolating a PR title into a shell.

The query suite is the **default**, not `security-and-quality`, and that is a decision with a scar
behind it. In the engine repository `security-and-quality` reported `cpp/loop-variable-changed` 29
times on a `for` loop that consumed flag values with `argv[++i]`. Every PR touching that file opened a
review thread, and with `required_review_thread_resolution` on the ruleset each one needed a manual
resolution — for a finding nobody intended to act on. A required analysis in that state does not add
safety; it trains the habit of clicking past findings. Widen this to `security-extended` after a few
months of clean runs, and only then.

The price of requiring a CodeQL check is worth stating too, because it has already been paid once: **a
CodeQL infrastructure failure blocks merges exactly as firmly as a real finding.** In August 2026
Dependabot split a `codeql-action` bump into two PRs, one for `init` and one for `analyze`, and each
failed alone with `Loaded a configuration file for version '4.37.7', but running version '3.37.7'` —
the two steps have to move together. `.github/dependabot.yml` here groups `github/codeql-action*` into
one PR ✅ so that specific failure cannot repeat.

### 3.2 The one dependency deliberately not pinned

`ci.yml` runs a `postgres:15-alpine` service container by tag, not by digest. That is an accepted
exception, not an oversight:

- Dependabot does not bump service images in workflow files, so a digest here would be pinned once
  and then rot — trading a mutable tag for an image that stops receiving patches.
- The threat is bounded: the container holds no secret, receives no untrusted input, and the token in
  that job is read-only on a public repository.

If that calculus changes — a release workflow with a publishing credential in the same job, say — pin
it by digest and accept the manual bumps.

## 4. Publishing, which is the part the engine does not have

Nothing is published yet, and no secret exists in this repository or its Actions settings. That is the
right moment to decide how publishing will work, because the wrong version of this is very hard to
walk back: a long-lived PyPI token in a repository secret is a credential that publishes to every
Python installation in the world, sitting in a place read access is enough to eventually reach.

### 4.1 Register the names before someone else does ⚙️ — most urgent item here

Checked while writing this document:

| Name | Registry | State |
|---|---|---|
| `smart-data-engine` | PyPI | **unclaimed** |
| `smart_data_engine` | PyPI | unclaimed (normalises to the same name) |
| `@smart-data-engines/sde` | npm | **unclaimed** |
| `sde` | PyPI | taken by someone else — which is why the import is `sde` and the distribution is not |

The README of this public repository tells readers to `pip install smart-data-engine`. Until that name
is ours, following our own documentation can install somebody else's code — and the person best placed
to notice the gap is anyone who reads the repository we are asking people to read.

Register both, with 2FA on both accounts, before the first release and preferably today. Registering a
placeholder version costs nothing and closes the window.

### 4.2 When a publish workflow is written ⚙️

- **OIDC trusted publishing**, not a token. PyPI and npm both support it: the workflow proves its
  identity to the registry with a short-lived token GitHub mints per run, and there is no long-lived
  credential to leak, rotate or find in a log.
- **npm provenance** (`npm publish --provenance`) and PyPI attestations, so a consumer can verify
  which workflow run and which commit produced the artefact they installed.
- **A GitHub Environment with required reviewers**, not a plain repository secret. An environment
  scopes the credential to one job and interposes a human between a merge and a publish.
- **Publish from a tag, and only from a tag** — protected by the tag ruleset in §1.
- **Never `npm publish` from a job that ran a fork's code.** Build and publish are separate jobs with
  separate permissions.
- **Watch the lockfile in a bump.** `npm ci` installs exactly what `package-lock.json` says, which is
  the protection; it is also why a lockfile change in a Dependabot PR deserves a look at the diff
  rather than a glance at the green tick.

## 5. Repository access ⚙️

- **Enforce 2FA** on the `Smart-Data-Engines` organisation (Settings → Authentication security).
  Hardware key or TOTP, not SMS. One member today, which is the cheapest possible moment to turn it on.
- Grant the minimum role: `write` for contributors, `admin` only where genuinely needed. Today there
  is one collaborator, `krzysztof-smartdataengines`, with `admin`.
- Review third-party OAuth apps and installed GitHub Apps periodically. Every app with write access is
  another path into the repository, and it is a path that does not show up in `git log`.
- `.github/CODEOWNERS` ✅ routes changes in sensitive paths — the byte contract, the conformance
  vectors, the three files `SECURITY.md` points a sceptic at, and the trust decisions in
  `placement.py` and `hashing.py`.

### 5.1 Verify the CODEOWNERS handle, because a wrong one is invisible

GitHub **silently ignores** an owner who does not exist or lacks write access. The file keeps looking
like protection and provides none. This is not hypothetical: the engine repository has shipped
`@kmacewicz` — a real GitHub user, but not a collaborator and not an organisation member — for months,
and all eight of its lines are inert.

```bash
gh api 'repos/Smart-Data-Engines/smart-data-engine-sdk/codeowners/errors?ref=refs/heads/main' \
  --jq '.errors'
```

An empty `errors` array is the only acceptable answer. Re-run it after adding or removing anyone.

The fully-qualified ref is not decoration. On this repository the bare endpoint and `?ref=main` both
answer **404**, and only `?ref=refs/heads/main` returns the array — so following the obvious form of
the command tells you the check is broken when in fact the file is fine. The engine repository answers
the bare form. Same endpoint, same account, different behaviour; use the long form and stop guessing.

Note that `required_approving_review_count` is `0` and `require_code_owner_review` is `false` while
there is one maintainer — a review you grant yourself is theatre. `CODEOWNERS` is therefore
documentation of what is load-bearing today, and becomes enforcement the day a second person has write
access; turn both settings on together at that point.

## 6. Signed commits ⚙️

Signing proves a commit came from the maintainer, which matters for a repository strangers are asked
to trust and matters more for one whose artefacts they will install.

```bash
# SSH signing is the simplest path if you already push over SSH
git config --global gpg.format ssh
git config --global user.signingkey ~/.ssh/id_ed25519.pub
git config --global commit.gpgsign true
git config --global tag.gpgsign true
```

Then add the same public key as a **signing key** in GitHub → Settings → SSH and GPG keys. A key
registered as an *authentication* key does not count as a signing key — it will keep working for
pushes and produce Unverified commits, which is the confusing failure. Confirm with:

```bash
gh api repos/Smart-Data-Engines/smart-data-engine-sdk/commits \
  --jq '.[:3][] | "\(.sha[0:8]) \(.commit.verification.verified) \(.commit.verification.reason)"'
```

Only once that prints `true` should `required_signatures` go into the ruleset (§1.3). Doing it in the
other order locks you out of your own branch, and out of pushing the fix.

## 7. Supply chain of our own dependencies ✅

Both packages have **zero runtime dependencies**, by design rather than by luck: this library goes
into someone else's application, so every dependency would be one they inherit and a version conflict
they might have to resolve. `cryptography` (signature verification) and `psycopg` (the PostgreSQL
adapter) are extras, installed only by a deployment that needs them.

That removes most of the engine's §7 problem — it has four `FetchContent` dependencies pinned by SHA —
and leaves a narrower one:

- **The npm `devDependencies` tree.** `typescript` and `vitest` pull in hundreds of transitive
  packages that CI installs on every run. `npm ci` from the committed `package-lock.json` is what makes
  that reproducible, so the lockfile is a security artefact and belongs in the diff review.
- **`pip install -e '.[dev,signed,postgres]'` resolves from PyPI at CI time.** Acceptable for a CI
  install that publishes nothing. It stops being acceptable the day a job in the same workflow holds a
  publishing credential — see §4.2 on keeping build and publish apart.
- **Extras reach clients.** A vulnerability in `cryptography` is one our users inherit through us, so
  Dependabot alerts on `python/pyproject.toml` are not advisory.

## 8. What a reader of this repository should also find ✅

These make the posture legible to somebody evaluating the project, which for this repository is a
commercial function and not a courtesy:

- `SECURITY.md` ✅ — how to report a vulnerability, what response to expect, what is in scope, and what
  is deliberately **not** a vulnerability (an unsigned map is accepted; the map format is public)
- `LICENSE` ✅ — Apache 2.0, with `NOTICE`
- `CONTRIBUTING.md` ✅ and `CODE_OF_CONDUCT.md` ✅
- `docs/format-contract.md` ✅ — the byte contract a fifth implementation is written from
- A green CI badge in the README ✅ — evidence the tests run, not merely that they exist

## 9. Threat model in one paragraph

This repository holds no secrets and no customer data, so the realistic threats are: (a) someone gets
malicious or broken code onto `main` and it reaches a release, (b) a published artefact is replaced or
forged, either through a leaked registry token or through the distribution name never having been
registered, (c) a dependency is compromised and lands in a client's application through our extras,
(d) a credential from a client engagement is committed by accident, (e) a leaked maintainer token is
used to rewrite history or publish a fake release. Eleven required status checks on a protected branch
with no bypass actors handle (a); trusted publishing with provenance, an environment with a reviewer,
tag protection and — first of all — **registering the names** handle (b); Dependabot with a committed
lockfile handles (c); secret scanning with push protection handles (d); 2FA, signed commits and tag
protection handle (e).

The threat this repository has and the engine does not is (b), and within (b) the unregistered
distribution name is the one an attacker needs no access at all to exploit.

## Checklist

```
✅ ci.yml: lint, mypy --strict, tests on 3 Pythons against a real PostgreSQL, tsc + vitest on 3 Nodes
✅ ci.yml: contract job — PEP 561 marker in the wheel, openssl-verified digests, frozen vector
✅ codeql.yml — python, javascript-typescript, actions; default query suite on purpose
✅ dependabot.yml — actions, pip (python/), npm (typescript/); codeql-action grouped
✅ third-party actions pinned to full commit SHAs
✅ SHA pinning required at the repository level
✅ SECURITY.md, LICENSE, NOTICE, CONTRIBUTING.md, CODE_OF_CONDUCT.md
✅ CODEOWNERS with a handle verified against /codeowners/errors
✅ pull_request_template.md and issue templates
✅ rulesets kept as JSON in .github/rulesets/
✅ branch ruleset on main: PR required, no force push, no deletion, linear history, no bypass actors
✅ branch ruleset: all eleven status checks required, strict
✅ tag ruleset on refs/tags/v*
✅ secret scanning + push protection
✅ Dependabot alerts + security updates
✅ private vulnerability reporting
✅ Actions: read-only default token, cannot approve PRs
✅ Actions: fork PR approval required for all external contributors
✅ merge commits off — squash and rebase only, consistent with required_linear_history
⚙️ register smart-data-engine on PyPI and @smart-data-engines on npm  ← most urgent
⚙️ org-wide 2FA on Smart-Data-Engines
⚙️ registry accounts with 2FA, before the first publish
⚙️ SSH/GPG signing key registered as a *signing* key, then required_signatures in the ruleset
⚙️ non-provider secret patterns + validity checks (organisation-level Secret Protection)
⚙️ when a publish workflow is written: OIDC trusted publishing, provenance, environment with reviewer
```

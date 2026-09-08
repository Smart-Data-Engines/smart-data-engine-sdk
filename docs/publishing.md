# Publishing these libraries

Two distribution names, two registries, and they do **not** work the same way. One can be reserved for
free in ten minutes and held forever without shipping anything. The other cannot be reserved at all:
the only act that claims a name on PyPI is an upload.

That asymmetry decides the order of everything below, so it is the first thing this page explains.

| Name | Registry | State | How it gets claimed |
|---|---|---|---|
| `@smart-data-engines/sde` | npm | unclaimed | creating the organisation grants the scope; no publish needed |
| `smart-data-engine` | PyPI | unclaimed | an upload, and nothing else |
| `sde` | PyPI | taken by someone else | — which is why the distribution and the import differ |

Both were checked against the registries on 8 September 2026: `GET /pypi/smart-data-engine/json` and
`GET /@smart-data-engines%2Fsde` both answer 404, `GET /pypi/sde/json` answers 200. Whether the npm
*organisation* name is free could not be checked without an account — npmjs.com answers 403 to a
script, and it answers 403 to a name that certainly exists and to one that certainly does not, so the
instrument says nothing. You will find out at signup.

## Why this is worth doing before the first release

`smart-data-engine` is not only in a README. It is in three **runtime error messages** — in
`placement.py`, `engines/postgres.py` and `engines/clickhouse.py` — where the library tells a user, at
the moment something has already failed, to run `pip install 'smart-data-engine[signed]'`. That is the
worst possible moment to point somebody at a name a stranger controls: they are debugging, they will
copy the command, and the instruction came from inside code they had already decided to trust.

Today the name resolves to nothing and the command simply fails, which is safe. It stops being safe
the moment anybody else registers it, and nothing warns us when that happens.

## What cannot be undone

Read this once before touching either registry. Every item is quoted from the registry's own policy.

- **PyPI never reuses a filename.** "PyPI does not allow for a filename to be reused, even once a
  project has been deleted and recreated." Upload `0.1.0.dev0` and that version is spent permanently.
- **Deleting a PyPI project hands the name back.** "Deletion of a project makes it uninstallable, and
  releases the project name for use by any other PyPI user." So deleting the project to tidy up
  destroys the exact thing you registered it for. If something is wrong, publish a new version.
- **npm never reuses `package@version` either.** "Once `package@version` has been used, you can never
  use it again." Unpublishing is possible within 72 hours of publishing while nothing depends on the
  package; after that it needs all three of no dependents, under 300 downloads a week, and a single
  maintainer. Unpublish every version and the name is blocked for 24 hours.
- **2FA is not optional on PyPI.** "Two-factor authentication is required on your PyPI account."

The practical consequence: the npm step below costs nothing and is reversible in the only sense that
matters, because it publishes nothing. The PyPI step spends a version number the first time you run
it, so it gets a rehearsal on TestPyPI first.

## What is already done, so that the first upload is right

An artefact is not the source tree, and nothing in this repository had ever looked at a built package
until this page was written. Building one found three defects, all fixed, all now held by
`python/tests/test_packaging.py`:

- **Neither package carried its licence.** `license = { text = "Apache-2.0" }` writes the *name* of a
  licence into the metadata and ships no file; the wheel's `dist-info` held `METADATA`, `WHEEL` and
  `RECORD` and nothing else. Apache-2.0 section 4 asks for the licence text and, where one exists, the
  NOTICE. Both now ship, in both packages.
- **`npm pack` produced a tarball of two files** — `package.json` and `README.md`. `files` lists
  `dist`, `dist` is a build output, and nothing built it. Publishing from a clean checkout would have
  put an importable-looking package with no code in it under our own scope, at a version npm will
  never let us reuse. A `prepack` script now builds, so `npm pack` cannot lie about what will ship.
  Measured after the fix: 70 files, with `dist/index.js` and both engine adapters.
- **Neither page would have linked to the repository.** For a library whose argument is "the invariant
  is checkable because you can read this code", a registry page with no route to the code spends the
  argument.

Also set: `publishConfig.access = "public"`, because **scoped npm packages are private by default** and
that flag is otherwise one someone has to remember on the single day it matters.

Verified on 8 September 2026: `python -m build` succeeds and `twine check` passes on both artefacts.

## 1. npm — reserve the scope. Ten minutes, nothing published.

npm grants a scope with an account: "When you sign up for an npm user account or create an
organization, you are granted a scope that matches your user or organization name." So the scope is
reserved by *existing*, not by publishing, and once `smart-data-engines` is our organisation nobody
else can publish anything under `@smart-data-engines/`.

1. Sign up at <https://www.npmjs.com/signup>. Use an address the company will still control in five
   years; `contact@smartdataengines.com` is fine now that it forwards.
2. Turn on 2FA immediately, set to **auth and writes**, with a TOTP app or a hardware key. Store the
   recovery codes in the password manager, not in a mailbox that 2FA protects.
3. Create the organisation: <https://www.npmjs.com/org/create>, name `smart-data-engines`, free plan.
   The free plan publishes public packages; it is the private ones that cost money, and ours is
   public. **If the name is taken, stop and tell me** — the package name in `typescript/package.json`
   and every document that cites it would have to change together, and that is a code change.
4. That is the whole reservation. Do not publish. The library is at `0.1.0-dev.0`, the first publish
   should carry provenance from CI, and publishing by hand would spend that version number to prove
   something the scope already guarantees.

Nothing else on npm is urgent after this.

## 2. PyPI — the name is claimed by an upload, and by nothing else

The tempting shortcut does not work. PyPI has a "pending publisher" feature that lets you configure a
trusted publisher for a project that does not exist yet, and it looks like a reservation. It is not:
"A 'pending' publisher does **not** create a project or reserve a project's name **until** it is
actually used to publish", and if somebody registers the name first, the pending publisher is
invalidated. So the choice is to upload, or to leave the name open.

### 2.1 Account and 2FA

1. Register at <https://pypi.org/account/register/>, and at <https://test.pypi.org/account/register/>
   too — TestPyPI is a **separate site with separate accounts and separate tokens**, which is the
   point of it.
2. Enable 2FA on both. PyPI requires it, and it will not let you upload without it.
3. Recovery codes into the password manager.

### 2.2 Rehearse on TestPyPI

Worth the ten minutes precisely because step 2.3 cannot be repeated with the same version number.
This rehearses the upload flow itself; the artefacts have already been checked here.

```bash
cd ~/projects/smart-data-engine/smart-data-engine-sdk/python

python3 -m venv /tmp/release && /tmp/release/bin/pip install -q build twine
rm -rf dist && /tmp/release/bin/python -m build          # sdist + wheel
/tmp/release/bin/twine check dist/*                      # must say PASSED twice
```

Then create an **API token** at <https://test.pypi.org/manage/account/token/>, scope "Entire account"
(there is no project to scope it to yet), and upload:

```bash
/tmp/release/bin/twine upload --repository testpypi dist/*
# username: __token__
# password: the pypi-... token, pasted whole, including the prefix
```

Open the page it prints. Check that the README renders, that the sidebar has the repository links, and
that the classifiers look right. Then **delete that TestPyPI token** — it is account-wide.

### 2.3 The real upload

Same artefacts, real index. Create an account-scoped token at
<https://pypi.org/manage/account/token/> — account-scoped because a project-scoped token can only be
made for a project that exists, and this upload is what creates it.

```bash
/tmp/release/bin/twine upload dist/*
# username: __token__
# password: the pypi-... token
```

The name is ours from the moment this returns.

`0.1.0.dev0` is a developmental release and that is deliberate: it is the version in the tree, it says
"dev" out loud, and it does not pretend to be a stable 0.1.0 we have not decided to cut. Measured
locally, pip does install it when it is the only candidate, so `pip install smart-data-engine` starts
working — which retires the one instruction in our public README that has been untrue on purpose.

### 2.4 Delete the token, immediately

An account-scoped PyPI token can publish to every project the account owns, forever, and it is now in
your shell history and possibly in `~/.pypirc`. Delete it at
<https://pypi.org/manage/account/token/> as soon as the upload succeeds, and check `~/.pypirc` is gone
or has no password in it. It is not needed again: from here on, publishing goes through CI.

**Never put either token in a GitHub secret.** A long-lived registry token in a repository is a
credential that publishes to every Python installation in the world, sitting somewhere read access
eventually reaches. §4.2 of [`github-security.md`](github-security.md) is the standing decision, and
the next section is how we honour it.

## 3. When you are done, one line changes here

`python/tests/_claims.py` holds `REGISTRIES`, and today both entries say `False`:

```python
REGISTRIES = (
    ("PyPI", "smart-data-engine", False),
    ("npm", "@smart-data-engines/sde", False),
)
```

Flip the flag for whatever you registered — or just tell me and I will. The test that reads it fails in
both directions: while a name is unregistered some page must say so, and once it is registered no page
may still call it unclaimed. So flipping one boolean names every document that has quietly become
false, including this one. That is on purpose: a claim that something has *not* happened is the one
kind nothing ever tries to use, so nothing ever disproves it, and this repository has now been caught
by that shape six times.

## 4. What comes after, and is ours rather than yours

Publishing by hand is the right way to claim a name and the wrong way to ship a release. The next piece
of work is a release workflow, and it is code, so it is our side:

- **OIDC trusted publishing on both registries**, so no long-lived credential exists anywhere. Note the
  ordering constraint that made section 2 look the way it does: npm configures trusted publishing from
  an existing package's settings page, and PyPI's pre-registration form does not reserve the name. Both
  therefore come *after* the first claim, not before it.
- **Provenance.** npm generates attestations automatically when publishing through trusted publishing
  from GitHub Actions, so a consumer can check which workflow run and which commit produced the tarball
  they installed.
- **A GitHub Environment with a required reviewer**, not a plain repository secret, so a merge cannot
  become a publish without a person.
- **Tag-gated**, under the existing `refs/tags/v*` ruleset, with build and publish as separate jobs so
  that no job which has run a fork's code holds the publishing identity.

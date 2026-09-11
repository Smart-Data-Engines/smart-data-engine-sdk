# Publishing these libraries

Two libraries exist and seven more are planned, across registries that do **not** work the same way.
Three things can be reserved for free, permanently, without publishing a line of code. Four can only
be claimed by publishing — which for a library that does not exist yet means publishing a lie. One
has no registry at all and is already ours.

Sorting the nine by *that* rather than by language is the whole structure of this page, because it is
what decides which ones are worth a visit today and which are worth nothing until there is code.

| Name | Registry | State | How it gets claimed |
|---|---|---|---|
| `@smart-data-engines/sde` | npm | unclaimed | creating the organisation grants the scope; **no publish needed** |
| `smart-data-engine` | PyPI | unclaimed | an upload, and nothing else |
| `sde` | PyPI | taken by someone else | — which is why the distribution and the import differ |

Section 4 does the same for the other seven languages. The short version, if you read nothing else:
**npm and Maven Central are worth doing today and cost nothing; PyPI costs one version number; the
rest are worth nothing until the library exists, and one of them would breach a registry's policy.**

Checked against the live registries on 11 September 2026, each with a control that must answer
differently so a silent instrument cannot read as good news:

| Checked | Ours | Control |
|---|---|---|
| PyPI `smart-data-engine`, `smart_data_engine` | 404 | `sde` → 200 |
| TestPyPI `smart-data-engine` | 404 | — |
| npm `@smart-data-engines/sde` | 404 | `is-callable` → 200 |
| crates.io `smart-data-engine` | 404 | `serde` → 200 |
| RubyGems `smart-data-engine`, `smart_data_engine` | 404 | `rails` → 200 |
| NuGet `smartdataengines`, `smartdataengine`, `smartdataengines.sde` | 404 | `newtonsoft.json` → 200 |
| Packagist `smart-data-engines/sde` | 404 | `monolog/monolog` → 200 |
| Maven Central `com.smartdataengines` | 0 artefacts | `org.apache.commons` → 150 |

**Two of those rows say less than they look like they say, and the difference matters.** The Maven
row and the NuGet row are searches for *published artefacts*, and both registries let a namespace or
an ID prefix be held with nothing published under it — which is precisely the feature we want to use.
So "no artefacts" does not mean "nobody holds it", and only a logged-in Portal account can tell you.
The npm **organisation** name cannot be checked at all without an account: npmjs.com answers 403 to a
script, and it answers 403 for a name that certainly exists and for one that certainly does not, so
the instrument says nothing either way. You find out at signup — which is why that is step 1.

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

## 1. npm — reserve the scope, and do it first

Ten minutes, nothing published. First not because it is most urgent but because it is the only step
whose failure would change code: everything else can be retried, and a taken organisation name cannot.

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

PyPI has a "pending publisher" feature that lets you configure a trusted publisher for a project that
does not exist yet, and it looks like a reservation. It is not one: "A 'pending' publisher does **not**
create a project or reserve a project's name **until** it is actually used to publish", and if
somebody registers the name first, the pending publisher is invalidated. So there is no way to hold
this name without uploading.

**A correction to an earlier revision of this page, because it lumped two registries together and
they differ.** That earlier text said trusted publishing on both registries necessarily comes *after*
the first claim. True for npm — it is configured from an existing package's settings page, so npm's
first publish cannot be credential-free. **Not true for PyPI**: a pending publisher cannot reserve the
name, but it *can be the thing that claims it*, with the first upload coming from GitHub Actions and
no token existing anywhere, ever.

That is strictly better on credentials and it costs a release workflow that has to work on the first
real attempt — against an upload that is irreversible, since PyPI never reuses a filename. Weighed
honestly, for tonight: **take the token path below.** The name is the thing at risk today; a token
that exists for ten minutes and is then deleted is a small, bounded exposure, and trusted publishing
gets configured on the existing project afterwards, which is the ordinary path. The workflow is on my
list either way (section 5) — it is just not worth standing between you and an unclaimed name.

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

`--repository testpypi` needs no `~/.pypirc`, which is worth stating because the obvious failure mode
is a `Missing 'testpypi' section` error that sends you writing a config file you do not need. twine
seeds defaults for both names when that file is absent — verified on this machine with no `~/.pypirc`
at all: it resolves to `https://test.pypi.org/legacy/`, and `pypi` to `https://upload.pypi.org/legacy/`.

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
**section 5** is how we honour it.

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

## 4. The other seven languages

`implementations.md` names the planned libraries: **Java, Rust, C# / .NET, Go, Kotlin, PHP and Ruby**,
none started and none claimed. Their registries do not resemble each other, and the differences are
not cosmetic — they decide whether a name can be held before there is code to put under it.

| Language | Registry | How a name is claimed | Holdable with nothing published? |
|---|---|---|---|
| Java | Maven Central | namespace verification, then artefacts under it | **Yes** — DNS TXT record, no artefact |
| Kotlin | Maven Central | same namespace as Java | **Yes** — one verification covers both |
| Go | **none** | the module path *is* the repository URL | **Already ours** — nothing to register, ever |
| C# / .NET | NuGet | publish; or an ID prefix reservation by review | Partly — see below |
| Rust | crates.io | publish, first-come-first-served | **No**, and a placeholder breaches policy |
| PHP | Packagist | submit a repository URL; vendor is protected after the first publish | **No** |
| Ruby | RubyGems | publish, name must be unique | **No** |

### 4.1 Maven Central — the one with a waiting period, so it is the one worth doing today ⚙️

Java and Kotlin share a namespace, so **one verification covers two of the seven planned libraries**,
and it can be done now with nothing to publish: "Publishing an artifact is NOT required to claim a
namespace. Registration and verification precede any artifact publication."

Two kinds of namespace are on offer and we want the first:

- **domain-based** — `com.smartdataengines`, verified by "a DNS TXT record with a value set to the
  Verification Key we assigned to your namespace request";
- **code-host-based** — `io.github.<username>`, verified by briefly creating a public repository whose
  *name* is the verification key.

Take the domain. `io.github.smart-data-engines` would tie every Java artefact we ever publish to a
GitHub account name, and a groupId cannot be changed after release without becoming a different
artefact to every build tool that resolves it. We own the domain; the GitHub account name is a tenancy.

**The reason this is today's job and not next quarter's: you are going to be at the DNS registrar
anyway.** The same visit that sets up forwarding for `contact@smartdataengines.com` can add the TXT
record, and a DNS change plus a verification round-trip is the only item on this whole page that
cannot be compressed into one sitting. Everything else here is instant or nearly so.

1. Sign in at <https://central.sonatype.com/> with the GitHub organisation account.
2. Add namespace `com.smartdataengines`. The Portal shows a verification key.
3. At the registrar for `smartdataengines.com`, add a TXT record whose value is that key. Their own
   warning is worth repeating: "Do not proceed with verification unless you have added and verified
   your DNS TXT record" — pressing the button before DNS has propagated fails the attempt rather than
   waiting for it.
4. Confirm in the Portal once `dig +short TXT smartdataengines.com` shows the key.
5. Stop. Publish nothing. Held permanently, and it covers Java and Kotlin both.

### 4.2 Go — nothing to register, and that is not an oversight

Go has no central registry: a module path is "the canonical name for a module" and "should describe
both what the module does and where to find it", the repository root path is part of it, and
`proxy.golang.org` is a cache in front of the source rather than a registry in front of a namespace.
Publishing a Go module "does not require an account anywhere".

So the Go name is **already ours, by owning the GitHub organisation** — it will be
`github.com/Smart-Data-Engines/smart-data-engine-sdk/go` or whatever directory we choose, and the
only decision left is where the directory goes, which is a decision in this tree rather than at a
registrar. Nothing to do today, nothing to do the day 19.6 opens either.

### 4.3 Rust, Ruby, PHP — do not claim these, and the reason is in one registry's own rules

All three are claimed by publishing, so holding a name means shipping a package with nothing in it.
Do not. Three independent reasons, and the first is decisive on its own.

**crates.io forbids it in the policy text.** They list, among content they act on, a crate that
"exists only to reserve a name for a prolonged period of time (often called 'name squatting') without
having any genuine functionality, purpose, or significant development activity on the corresponding
repository". There is also no way back if it goes wrong: names are "first-come, first-serve", "a
publish is generally permanent. The version can never be overwritten, and the code cannot be deleted",
and the team "will not transfer ownership of existing crates without the explicit approval of the
current owner".

**It is the failure requirement 17.6 exists to prevent.** `implementations.md` opens by saying that
writing "we support Java" when it means something weaker than it does for Python is misleading in the
direction that costs a client a migration. A package on crates.io called `smart-data-engine` is that
claim, made to everyone who searches, with no library behind it.

**And it spends something permanently to buy nothing.** Every one of these registries refuses to reuse
a version number, so the placeholder's version is gone, and the real `0.1.0` inherits a history whose
first entry was an empty package.

What to do instead: nothing today. On the day one of those libraries is real, the first publish claims
the name, and that publish is minutes of work. The risk we are accepting by waiting is that somebody
takes `smart-data-engine` on crates.io in the meantime — accepted deliberately, because the cost if it
happens is a different crate name in one language, and the cost of the alternative is a policy breach
plus a false claim in seven.

### 4.4 NuGet — reservable in principle, premature in practice

NuGet IDs are otherwise first-come-first-served, and it does offer prefix reservation: you email
`account@nuget.org` naming the prefix, and the team reviews it against published criteria. Two of
those criteria are the reason to wait rather than write the email today — "Is the package ID prefix
something common that should not belong to any individual owner or organization?" and "Would *not*
reserving the package ID prefix cause ambiguity, confusion, or other harm to the community?" With no
C# library in existence, the honest answer to the second is "not yet", and a reviewer asking us to
justify the request would be right to.

Worth knowing rather than worth doing: `SmartDataEngines.*` is the prefix to ask for, the ask is one
email, and the right moment is when the first C# package is ready to publish — not before.

## 5. What comes after, and is ours rather than yours

Publishing by hand is the right way to claim a name and the wrong way to ship a release. The next piece
of work is a release workflow, and it is code, so it is our side:

- **OIDC trusted publishing on both registries**, so no long-lived credential exists anywhere. The
  ordering is not the same on the two, which section 2 now states rather than glossing: npm configures
  trusted publishing from an existing package's settings page, so there it genuinely comes *after* the
  first claim. On PyPI a pending publisher can *be* the first claim — it just cannot reserve the name
  in advance.
- **Provenance.** npm generates attestations automatically when publishing through trusted publishing
  from GitHub Actions, so a consumer can check which workflow run and which commit produced the tarball
  they installed.
- **A GitHub Environment with a required reviewer**, not a plain repository secret, so a merge cannot
  become a publish without a person.
- **Tag-gated**, under the existing `refs/tags/v*` ruleset, with build and publish as separate jobs so
  that no job which has run a fork's code holds the publishing identity.

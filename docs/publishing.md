# Publishing these libraries

Two libraries exist and seven more are planned, across registries that do **not** work the same way.
Three things can be reserved for free, permanently, without publishing a line of code. Four can only
be claimed by publishing — which for a library that does not exist yet means publishing a lie. One
has no registry at all and is already ours.

Sorting the nine by *that* rather than by language is the whole structure of this page, because it is
what decides which ones are worth a visit today and which are worth nothing until there is code.

| Name | Registry | State | How it gets claimed |
|---|---|---|---|
| `smart-data-engine-sdk` | PyPI | **ours since 12 September 2026** | the upload that claimed it |
| `@smart-data-engines/sde` | npm | **scope ours since 12 September 2026**, package not yet published | creating the organisation granted the scope |
| `com.smartdataengines` | Maven Central | **ours since 12 September 2026** | a DNS TXT record; nothing published, ever |
| `smart-data-engine` | PyPI | **refused** | too similar to `smartdata-engine`; see §2.0 |
| `sde` | PyPI | taken by someone else | — which is why the distribution and the import differ |

**The refused row said `smart-data-engine-sdk` until 12 September**, contradicting the row above it in
the same table. The distribution was renamed that day and the substitution caught a mention of the
*old* name whose role was "the name PyPI refused" — so the page claimed one name was simultaneously
ours and rejected, and read plausibly enough for nobody to notice. `test_packaging.py` now refuses any
row that gives a name we own a state other than "ours"; the general rule is in
`contract-pitfalls.md`, and it is that when a name is a prefix of another name, a rename has to
classify each hit by role before substituting.

Section 4 does the same for the other seven languages. The short version, if you read nothing else:
**npm, Maven Central and PyPI are done; the rest are worth nothing until the library exists, and one
of them would breach a registry's policy.** The ordering that produced that, worth reusing: do the
one with a waiting period first, not the one that feels urgent.

Checked against the live registries on 11 September 2026, each with a control that must answer
differently so a silent instrument cannot read as good news:

| Checked | Ours | Control | Instrument |
|---|---|---|---|
| PyPI `smart-data-engine-sdk` | usable | `pip` → taken | `tools/pypi_name_available.py` |
| TestPyPI `smart-data-engine-sdk` | 404 | — | JSON API |
| npm `@smart-data-engines/sde` | 404 | `is-callable` → 200 | registry API |
| crates.io `smart-data-engine-sdk` | 404 | `serde` → 200 | registry API |
| RubyGems `smart-data-engine-sdk`, `smart_data_engine` | 404 | `rails` → 200 | registry API |
| NuGet `smartdataengines`, `smartdataengine`, `smartdataengines.sde` | 404 | `newtonsoft.json` → 200 | registry API |
| Packagist `smart-data-engines/sde` | 404 | `monolog/monolog` → 200 | registry API |
| Maven Central `com.smartdataengines` | 0 artefacts | `org.apache.commons` → 150 | artefact search |

**Three of those rows say less than they look like they say, and one of them already cost an
evening.** The Maven and NuGet rows search for *published artefacts*, and both registries let a
namespace or an ID prefix be held with nothing published under it — the very feature we use — so "no
artefacts" is not "nobody holds it", and only a logged-in account can tell you. The npm
**organisation** name cannot be checked without an account at all: npmjs.com answers 403 to a script,
and it answers 403 for a name that certainly exists and for one that certainly does not.

**The PyPI row is the one that was wrong, and the correction is §2.0.** Earlier revisions of this page
recorded `smart-data-engine-sdk` as available on four separate days, on the strength of
`GET /pypi/<name>/json` answering 404. PyPI then refused the upload. The row now names the instrument
it was measured with, because that column is what would have caught it.

## Why this is worth doing before the first release

`smart-data-engine-sdk` is not only in a README. It is in three **runtime error messages** — in
`placement.py`, `engines/postgres.py` and `engines/clickhouse.py` — where the library tells a user, at
the moment something has already failed, to run `pip install 'smart-data-engine-sdk[signed]'`. That is the
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

## 1. npm — the scope is ours ✅ (12 September 2026)

**Done.** The `smart-data-engines` organisation exists, `krzysztof-smartdataengines` owns it, and
`npm org ls smart-data-engines` is what says so rather than a screenshot. Nothing is published under
the scope and nothing should be — see step 4. Ten minutes, as estimated.

The steps are kept below rather than deleted, because the next scope this organisation reserves
follows exactly this path and the two warnings in it are the part worth having again.

It went first, and the reason generalises: not because it was the most urgent, but because it was the
only step whose **failure would have changed code**. Everything else on this page can be retried;
a taken organisation name would have meant editing six files at once.

npm grants a scope with an account: "When you sign up for an npm user account or create an
organization, you are granted a scope that matches your user or organization name." So the scope is
reserved by *existing*, not by publishing, and once `smart-data-engines` is our organisation nobody
else can publish anything under `@smart-data-engines/`.

1. Sign up at <https://www.npmjs.com/signup>. Use an address the company will still control in five
   years; `contact@smartdataengines.com` is fine now that it forwards.
2. Turn on 2FA immediately, in the mode npm calls **`auth-and-writes`** (the other, `auth-only`,
   leaves publishing unprotected). Store the recovery codes in the password manager, not in a mailbox
   that 2FA protects — npm says they are "the only way to ensure you can recover your account if you
   lose access to your second factor device".

   **Losing the device is worse than it sounds, which is the argument for the password manager.**
   Using a recovery code puts "a temporary 72-hour security hold on your account" during which you
   cannot publish, create tokens or change settings. So the failure mode is not an afternoon of
   annoyance, it is three days in which a release cannot go out — and linking the GitHub account,
   which npm recommends as a second recovery route, costs one click now.
3. Create the organisation. Profile picture → **Add an Organization**; the name you type *becomes the
   scope*, so it has to be exactly `smart-data-engines`. Choose the free plan — npm words the choice
   as "unlimited public packages" (free) against "unlimited private packages" (paid), and ours is
   public. Skip the invite-members step.

   **Do not take the "convert your user account to an organization" route.** It is a different
   operation on a neighbouring page: it turns the account you just made into the org and hands your
   personal scope over, which is not what we want and is awkward to undo.

   **If the name is taken, stop and tell me** — six files cite the scope (`typescript/package.json`,
   its lockfile, `typescript/README.md`, and three documents), and they would all have to change
   together. That is a code change, not a rename.
4. That is the whole reservation. Do not publish. The library is at `0.1.0-dev.0`, the first publish
   should carry provenance from CI, and publishing by hand would spend that version number to prove
   something the scope already guarantees.

**Nothing else on npm is urgent, and the distinction that makes that true is worth keeping straight:
the scope is ours and the package is unpublished, which are two different facts.** `npm install
@smart-data-engines/sde` still installs nothing — and cannot install somebody else's package either,
which is the whole protection the reservation buys. `REGISTRIES` in `_claims.py` therefore reads the
npm entry as registered while the package does not exist, and that is correct: the flag is about who
owns the name, not about whether anything has been shipped under it.

## 2. PyPI — published ✅ (12 September 2026)

**Done.** `smart-data-engine-sdk` `0.1.0.dev0` is on PyPI, and what says so is
`pip install smart-data-engine-sdk` in an empty virtualenv rather than the page rendering: it
imports, the wheel carries `licenses/LICENSE`, `licenses/NOTICE` and `py.typed`, and
`[signed,postgres]` resolves `cryptography` and `psycopg` from the real index.

It took two attempts and the first one is §2.0, which is the part of this section worth reading.
The steps are kept below because the second distribution this repository publishes walks the same
path — and because §2.4 is the step people skip.

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
honestly, the token path was taken, and it was the right call: the first attempt failed on the *name*
(§2.0), which a workflow would have failed on just as hard and slower. Trusted publishing gets
configured on the existing project now, which is the ordinary path, and section 5 is the workflow.

What the token path actually cost, recorded so the next decision is made on evidence rather than on
the argument above: an account-scoped token in `~/.pypirc`, which arrived with mode **664** —
world-readable — until it was corrected to 600. That is the failure mode of "it only exists for ten
minutes": it exists for as long as the file does, and the file outlives the intention.

### 2.0 The name is `smart-data-engine-sdk`, and the suffix was forced ⚠️

Measured on 12 September 2026, with a production token and everything ready: PyPI answered
**400 — "The name 'smart-data-engine' is too similar to an existing project."** Nothing was uploaded;
the request was refused before a byte of the wheel was accepted, so no version number was spent.

**The project it collides with is `smartdata-engine`, registered by somebody else with zero
releases.** PyPI compares `ultranormalize_name` for exact equality, and that function — warehouse
migration `d18d443f89f0` — strips `.`, `_` and `-`, folds `l|L|i|I` to `1` and `o|O` to `0`, and
lowercases. Both names reduce to `smartdataeng1ne`, so only one of them can exist. `-sdk` changes the
reduction to `smartdataeng1nesdk`, and it matches the repository name, which is the consistency worth
having anyway.

**Three lessons, and the first is about this page rather than about PyPI.**

*The check that said "available" could not have said anything else.* `GET /pypi/<name>/json` answers
404 both for a free name and for one somebody registered and never released — and the second is
exactly where a squatted name lives. `GET /project/<name>/` is worse: it answers 200 for a name
invented on the spot. Only `GET /simple/<name>/` distinguishes them, and even that is not sufficient,
because a free name can still be refused for similarity. **The question needs the whole index**, which
is why there is now a tool rather than a paragraph:

```bash
python tools/pypi_name_available.py <candidate> [more ...]
```

It downloads the simple index, applies PyPI's own comparison, and carries a control in the same run:
if `pip` does not appear in the population, it reports a broken instrument rather than a free name.

*The TestPyPI rehearsal passed.* TestPyPI accepted `smart-data-engine-sdk` and produced a perfectly good
project page. So the rehearsal returned green against a name production would refuse — worth knowing
about what a rehearsal is for. It exercises **the flow**: token, twine invocation, rendered page. It
does not exercise the production index's rules, and treating a green rehearsal as clearance for the
real upload is the mistake this paragraph exists to prevent.

*A 400 is not a 403.* The first attempt reported only `400 Bad Request` with no reason, and the reason
was one `--verbose` away. Read the server's words before forming a theory about the token.

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
locally, pip does install it when it is the only candidate, so `pip install smart-data-engine-sdk` starts
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

`python/tests/_claims.py` holds `REGISTRIES`, and all three entries now say `True`:

```python
REGISTRIES = (
    ("PyPI", "smart-data-engine-sdk", True),
    ("npm", "@smart-data-engines/sde", True),
    ("Maven Central", "com.smartdataengines", True),
)
```

The test that reads it fails in both directions: while a name is unregistered some page must say so,
and once it is registered no page may still call it unclaimed. So flipping one boolean names every
document that has quietly become false. **It earned that on 12 September**, twice — the flip named
eight passages across five files, and reading would not have found them.

Two things the mechanism learned that day. The caveat has to sit **beside** the install command and
not merely somewhere in the repository: a first version asked for "somewhere", and deleting the
warning from the one page a person is actually reading while copying the command survived it. And the
install command is now looked up in a table (`INSTALL_COMMAND`) rather than derived by a two-branch
conditional — that conditional was correct with two registries and silently invented
`npm install com.smartdataengines` the moment a third arrived.

Add a line here the day another name becomes ours. A claim that something has *not* happened is the
one kind nothing ever tries to use, so nothing ever disproves it, and this repository has now been
caught by that shape six times.

## 4. The other seven languages

`implementations.md` names the planned libraries: **Java, Rust, C# / .NET, Go, Kotlin, PHP and Ruby**,
none of them started. Two of their names are now settled anyway and a third never needed settling,
because their registries do not resemble each other — and the differences are not cosmetic. They
decide whether a name can be held before there is code to put under it.

| Language | Registry | How a name is claimed | Holdable with nothing published? |
|---|---|---|---|
| Java | Maven Central | namespace verification, then artefacts under it | **Yes, and done** ✅ — `com.smartdataengines` |
| Kotlin | Maven Central | same namespace as Java | **Yes, and done** ✅ — the same verification covered it |
| Go | **none** | the module path *is* the repository URL | **Already ours** — nothing to register, ever |
| C# / .NET | NuGet | publish; or an ID prefix reservation by review | Partly — see below |
| Rust | crates.io | publish, first-come-first-served | **No**, and a placeholder breaches policy |
| PHP | Packagist | submit a repository URL; vendor is protected after the first publish | **No** |
| Ruby | RubyGems | publish, name must be unique | **No** |

### 4.1 Maven Central — the namespace is ours ✅ (12 September 2026)

**Done.** `com.smartdataengines` is verified on the Central Portal, so **two of the seven planned
libraries have their name settled before either has a line of code**. Java and Kotlin share a
namespace, and nothing had to be published to hold it: "Publishing an artifact is NOT required to
claim a namespace. Registration and verification precede any artifact publication." Verification key
`bvhnylcfw8`.

The steps are kept below rather than deleted. The key is single-use, so the next domain this company
verifies walks exactly this path — and two things on it sent me the wrong way once each.

**The zone is managed at Squarespace, and no DNS answer says so.** `dig NS smartdataengines.com`
returns `ns-cloud-b{1,2,3,4}.googledomains.com`, which is what step 4 below used to say and what I
acted on. The record is added at
<https://account.squarespace.com/domains/managed/smartdataengines.com/dns/dns-settings>: Squarespace
bought Google Domains, kept the Google Cloud DNS nameservers, and moved the control panel. So every
instrument you have points at a company that no longer has the form you need, and nothing measurable
reveals it. That is the detail most likely to send the next reader to the wrong place.

**A measurement of mine was right while its conclusion was one possibility too early.** I queried the
authoritative nameservers directly, correctly saw no key, and said this was not propagation but a
record in the wrong place. The write had landed between my two commands. With a DNS change, "I cannot
see it" and "it is not there" stay different claims for as long as any cache holds the old answer;
the honest report is the measurement and the minute it was taken, not the conclusion drawn from it.

**Measured from four resolvers afterwards, and they did not agree** — which is the whole argument for
asking more than one:

| resolver | apex TXT, after verification succeeded |
|---|---|
| `8.8.8.8` Google | `v=spf1 …` **and** `bvhnylcfw8` |
| `1.1.1.1` Cloudflare | `bvhnylcfw8` **and** `v=spf1 …` |
| `9.9.9.9` Quad9 | **`v=spf1 …` alone** — still serving the pre-change set from cache |
| `208.67.222.222` OpenDNS | `bvhnylcfw8` **and** `v=spf1 …` |

One resolver in four still denied the record after the Portal had accepted it. A single `dig` against
a resolver that happens to be Quad9 therefore reads exactly like a failed edit, and the conclusion it
invites — go back and change the record — is the one that breaks something.

**Mail survived, and that is the check that matters more than the key**: `dig +short MX
smartdataengines.com` still answers `1 smtp.google.com.` Replacing the apex TXT instead of adding to
it would have broken company mail silently, in the direction of other people's spam folders.

Two kinds of namespace are on offer and we want the first:

- **domain-based** — `com.smartdataengines`, verified by "a DNS TXT record with a value set to the
  Verification Key we assigned to your namespace request";
- **code-host-based** — `io.github.<username>`, verified by briefly creating a public repository whose
  *name* is the verification key.

Take the domain. `io.github.smart-data-engines` would tie every Java artefact we ever publish to a
GitHub account name, and a groupId cannot be changed after release without becoming a different
artefact to every build tool that resolves it. We own the domain; the GitHub account name is a tenancy.

**Why it went before PyPI, which generalises to the next one.** A DNS change plus a verification
round-trip was the only item on this whole page that could not be compressed into one sitting, and it
needed a visit to the registrar that was happening anyway for `contact@smartdataengines.com`. Order
the page by what has a waiting period, not by what feels urgent: everything else here turned out to
be instant or nearly so, including the upload that was supposed to be the hard part.

**One verification covers every future Java and Kotlin artefact, not just the first.** Quoted, because
the alternative reading would mean registering a namespace per library: "if you are the owner or
maintainer of a domain name, you can use any groupId starting with the reverse domain name **and as
many subsections as you desire**". So `com.smartdataengines` admits `com.smartdataengines.sde`,
`com.smartdataengines.whatever`, all of it, registered once. And the groupId "should reverse the
domain name exactly, even if the domain name contains hyphens" — ours has none, so it is plainly
`com.smartdataengines`.

1. Sign in at <https://central.sonatype.com/>. Social login with Google or GitHub works, or a
   username and password; the address has to be one you can read, since support correspondence goes
   there.
2. Username menu (top right) → **View Namespaces** → **Add Namespace** → `com.smartdataengines`.
3. Press **Verify Namespace**. The Portal shows a **Verification Key**.
4. Add a TXT record at the registrar. Three details, and the second one is the one that can do
   damage:
   - **The apex domain only.** Central does "exact domain checking": for `com.smartdataengines` it
     looks at `smartdataengines.com` and at nothing else — not `www`, not a subdomain.
   - **Add a record, do not edit the one that is there.** Measured on 12 September 2026:
     `smartdataengines.com` has exactly **one** TXT record at the apex and it is
     `v=spf1 include:_spf.google.com ~all`, the SPF for company mail (`MX 1 smtp.google.com`). A
     registrar UI that presents TXT as one editable value invites replacing it, and replacing it
     breaks mail — quietly, in the direction of other people's spam folders. Multiple TXT records on
     one name are normal and correct.
   - **The panel is at Squarespace even though the nameservers say `googledomains`** (see above).
     The zone is served by Google Cloud DNS, where a TXT record *set* on the apex holds several
     values in one multi-line field. Add a line.
5. Wait, then confirm in the Portal. "If you have set up your DNS TXT record correctly, it should
   only take a few minutes for us to verify your namespace" and the check is automated, but their own
   warning still applies: "Do not proceed with verification unless you have added and verified your
   DNS TXT record" — pressing the button before the record resolves fails the attempt rather than
   waiting for it. Check from outside first, and check that mail survived:

   ```bash
   dig +short TXT smartdataengines.com      # the key AND the v=spf1 line, both
   dig +short MX  smartdataengines.com      # still 1 smtp.google.com.
   ```

6. Stop. Publish nothing — nothing exists to publish, and the namespace is held permanently.
   **Check from at least two resolvers**, for the reason in the table above, and check mail in the
   same breath.

**What publishing there will later need, so that it is clear the namespace is the only thing missing
today** and the rest is our work rather than yours: a POM carrying name, description, url, at least
one licence, developer details and SCM connections; **every file GPG-signed** with a `.asc` beside
it; `-sources.jar` and `-javadoc.jar` next to each main jar; and MD5 plus SHA1 checksums. That is a
release pipeline, which is why it waits for a library to exist.

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
direction that costs a client a migration. A package on crates.io called `smart-data-engine-sdk` is that
claim, made to everyone who searches, with no library behind it.

**And it spends something permanently to buy nothing.** Every one of these registries refuses to reuse
a version number, so the placeholder's version is gone, and the real `0.1.0` inherits a history whose
first entry was an empty package.

What to do instead: nothing today. On the day one of those libraries is real, the first publish claims
the name, and that publish is minutes of work. The risk we are accepting by waiting is that somebody
takes `smart-data-engine-sdk` on crates.io in the meantime — accepted deliberately, because the cost if it
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

## 5. Releasing, which is a tag ✅ (built 12 September 2026)

Publishing by hand is the right way to claim a name and the wrong way to ship a release. Both names
were claimed on 12 September, so this section stopped being a plan the same day. What does it:
`.github/workflows/release.yml`, with `tools/release_tag.py` and `tools/check_artefact.py` as the two
refusals in front of it, and `.github/rulesets/check_contexts.py` keeping the configuration from
drifting away from any of it.

**No credential exists anywhere.** Both registries authenticate the workflow over OIDC, so there is
no token in this repository, in its Actions secrets, or on a laptop — nothing to leak, nothing to
rotate, nothing to forget to delete. That matters more here than the convenience suggests: an
account-scoped PyPI token publishes to every project that account owns, forever, and section 2.4
exists because the alternative was keeping one.

### 5.1 Cutting a release

Bump the version on `main` through a pull request like any other change, then tag the commit that
landed:

```bash
git tag python-v0.1.0     && git push origin python-v0.1.0       # -> PyPI
git tag typescript-v0.1.0 && git push origin typescript-v0.1.0   # -> npm
```

Then approve the deployment on GitHub. The publish job waits on an environment with you as a required
reviewer, so a merge cannot become a publish without a person — and the environments are scoped to
their own tag pattern, so nothing but a `python-v*` tag can even ask to use the PyPI one.

**The tags are per-language, and the reason is not tidiness.** One shared tag would publish an
artefact byte-identical to its predecessor, with an empty changelog, every time the *other* language
moved — at a version number neither registry ever hands back. What makes the two libraries agree is
the conformance suite and `conformance/contract-version.txt`; making the version number carry that
meaning as well would be a second mechanism for something already held, and a duplicated guarantee
cannot be mutated separately.

### 5.2 What it refuses, and why each refusal exists

Everything here fails *before* the reviewer is asked, except the last two, which fail before the
upload:

| Refusal | The failure it prevents |
|---|---|
| a bare `v0.1.0` tag | A tag that triggers nothing is a release that **looks done**: the tag is in the repository, protected, and nothing was published. The workflow triggers on `v*` purely so this can be said out loud, with both correct forms named. |
| the tag disagrees with the manifest | Publishing the manifest's version under the tag's name. Neither half is correctable: the registry will not reuse a version number and the ruleset will not move the tag. |
| the tagged commit is not on `main` | The ruleset protects `main` with eleven required checks; it does **not** stop a tag being pointed at any commit in the repository, including one on a branch nobody reviewed. Ancestry is what makes "the published artefact passed CI" a fact rather than an assumption. |
| the artefact is missing a licence, `py.typed`, or `dist/` | All three have actually been missing, on 8 September, and none of it was visible from a green suite — the suite runs the source tree and a user runs the artefact. The worst would have put an importable-looking package with no code in it under our own scope. |
| the artefact records a version other than the tag's | The gate agreeing with the manifest does not prove the *build* used it, and what a user installs is the number inside the file. |

`tools/check_artefact.py` carries a control in the same run: one member that cannot exist must be
reported absent. A checker stuck on "present" would find every required file and report a flawless
package, which is the shape of good news worth distrusting.

### 5.3 Three things still need you

1. **PyPI: add the trusted publisher** ⚙️ — <https://pypi.org/manage/project/smart-data-engine-sdk/settings/publishing/>.
   Four fields, and all four must match exactly or the token is refused:

   | Field | Value |
   |---|---|
   | Owner | `Smart-Data-Engines` |
   | Repository name | `smart-data-engine-sdk` |
   | Workflow name | `release.yml` |
   | Environment name | `pypi` |

   The environment field is optional at PyPI and is filled in deliberately: with it, a token minted by
   any other job in this repository is rejected at the registry rather than trusted.

2. **npm: the first publish has to be by hand** ⚙️, and this is npm's constraint rather than a
   shortcut. Trusted publishing there is configured on a package's own settings page, and `npm trust`
   (npm ≥ 11.15.0) says the same thing in its documentation: "The package you're configuring must
   already exist on the npm registry." So the sequence is fixed:

   ```bash
   cd typescript
   npm login                      # 2FA, the auth-and-writes mode from section 1
   npm publish --access public    # prepack builds; this is the 0.1.0-dev.0 already on PyPI
   npm view @smart-data-engines/sde version
   ```

   Then configure the publisher, either on the package page or in one command:

   ```bash
   npx npm@11.15.0 trust github @smart-data-engines/sde \
       --repo Smart-Data-Engines/smart-data-engine-sdk --file release.yml --env npm
   ```

   **Do not put a token in an Actions secret to avoid this.** It would buy one attestation and leave
   behind a credential that publishes under our scope for as long as nobody remembers it is there.

3. **The two environments** ✅ — already created, with you as the required reviewer and each one
   locked to its own tag pattern. Worth reading back rather than trusting, since this API answers
   `200` to writes that change nothing:

   ```bash
   R=Smart-Data-Engines/smart-data-engine-sdk
   gh api "/repos/$R/environments" --jq '.environments[] | {name, rules: [.protection_rules[].type]}'
   gh api "/repos/$R/environments/pypi/deployment-branch-policies" --jq '.branch_policies[].name'
   ```

### 5.4 The one cost, named rather than hidden

**The first npm tarball will have no provenance attestation.** `npm publish --provenance` only works
from a supported CI provider on a cloud-hosted runner, and the publish that has to happen by hand
cannot be either. Every version after it gets one automatically, because trusted publishing generates
the attestation itself rather than leaving it to a flag somebody has to remember.

What makes that acceptable rather than merely tolerable: the version without an attestation is
`0.1.0-dev.0`, matching the `0.1.0.dev0` already on PyPI. It is a dev release, so **every version a
client would actually pin is attested** — the gap lands on the one release nobody depends on.

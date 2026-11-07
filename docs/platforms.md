# Where this library runs

Requirement 15.3: this list is **recorded and verified**, not presumed. The verification is
`python/tests/test_platforms.py`, which reads this file, the packaging metadata and the CI
workflow, and fails if the three disagree.

## Python

| | |
|---|---|
| Supported | **3.11, 3.12, 3.13, 3.14** |
| Tested in CI | 3.11, 3.12, 3.13, 3.14 — every one, on every pull request |
| Declared in `python/pyproject.toml` | `>=3.11,<3.15` |

The floor is 3.11 because the code uses `X | None` in annotations that are resolved at runtime by
`get_type_hints`.

**The ceiling is closed, and that is a correction rather than a caution.** It used to be
`>=3.11` with no upper bound, which claimed 3.14 and 4.0 while CI tested three versions. For most
libraries an open ceiling is the friendly default; for this one it is a hazard with a name. This
package's job is a **byte-identical canonical encoding**: `model_version` is a digest over it, and
two services of the same client that compute different digests reject each other's maps. The CI
workflow already says why both ends of the matrix are run — *"a canonical encoding that drifts with
a Python release is exactly the failure the contract exists to prevent"* — and an open upper bound
handed that failure to whoever upgraded first. `pip install` would have succeeded on an untested
interpreter and the drift would have shown up as a rejected map, in production, in the service that
upgraded.

So the ceiling moves when the matrix does, in the same commit, and the test here is what makes that
one action instead of two.

## Node

| | |
|---|---|
| Supported | **22, 24, 26** |
| Tested in CI | 22, 24, 26 — every one, on every pull request |
| Declared in `typescript/package.json` | `>=22 <27` |

Same reasoning, same closed ceiling. The TypeScript implementation has to agree with the Python one
byte for byte on the shared conformance vectors, so an untested runtime is an untested encoder.

## When a runtime leaves

A line of Python or of Node is supported while its upstream maintains it. In the first minor
release after a line's end of life it leaves, in one commit: its CI job, its place in
`requires-python` or `engines`, the required checks of `.github/rulesets/main.json`, and these
tables, with a CHANGELOG entry marked as one that may break a configuration. A release already on
the registry keeps working on the line it supported; it just gets no successor there.

| Line | Upstream end of life |
|---|---|
| Python 3.11 | October 2027 |
| Python 3.12 | October 2028 |
| Python 3.13 | October 2029 |
| Python 3.14 | October 2030 |
| Node 22 | 30 April 2027 |
| Node 24 | 30 April 2028 |
| Node 26 | 30 April 2029 |

Node 18 (end of life 30 April 2025) and Node 20 (30 April 2026) left by this rule, for 0.3.0. The
0.2.0 on npm still installs on them.

## Operating systems

CI runs on `ubuntu-latest`. That is what is **tested**, and it is not the same claim as what is
supported: this is pure Python and pure TypeScript with no compiled extension and no platform call
of its own, so there is nothing here that could be platform-specific except the drivers, which are
the engine vendors' code and carry their own support matrices.

What that means precisely, because "should work" is the phrasing requirement 15.3 refuses:

- **Tested:** Linux (x86-64, Ubuntu).
- **Expected to work and not tested by us:** macOS, Windows, and other Linux distributions. If you
  need one of those covered by CI rather than by reasoning, say so and it is a matrix entry —
  which is the honest answer, because adding it is a one-line change and claiming it without
  adding it is not.

## What is not on this list

**Anything about your engines.** The versions of PostgreSQL and ClickHouse this library speaks to
are a separate question, answered by the adapters and their live test slices, not by this page.

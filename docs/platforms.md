# Where this library runs

Requirement 15.3: this list is **recorded and verified**, not presumed. The verification is
`python/tests/test_platforms.py`, which reads this file, the packaging metadata and the CI
workflow, and fails if the three disagree.

## Python

| | |
|---|---|
| Supported | **3.11, 3.12, 3.13** |
| Tested in CI | 3.11, 3.12, 3.13 — every one, on every pull request |
| Declared in `python/pyproject.toml` | `>=3.11,<3.14` |

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
| Supported | **18, 20, 22** |
| Tested in CI | 18, 20, 22 — every one, on every pull request |
| Declared in `typescript/package.json` | `>=18 <23` |

Same reasoning, same closed ceiling. The TypeScript implementation has to agree with the Python one
byte for byte on the shared conformance vectors, so an untested runtime is an untested encoder.

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

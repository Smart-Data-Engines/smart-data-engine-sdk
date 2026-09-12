# sde — Smart Data Engine client library for Python

Declare your data model. We decide which database engine each part of it lives in, how it is laid out
there, and when it should move — and move it while your application keeps running.

Your code never names a table or an engine. That absence is the point: it is what lets the physical
schema change underneath you without touching a line of your code.

```python
from datetime import datetime
from decimal import Decimal
from typing import Annotated
from uuid import UUID
import sde

@sde.entity
class User:
    id: UUID
    email: str

    class Meta:
        pii = ["email"]

@sde.entity
class Order:
    id: UUID
    user: sde.Ref[User]
    total: Annotated[Decimal, sde.precision(12, 2)]
    created_at: datetime

    class Meta:
        residency = "EU"

model = sde.build_model()
```

## What you declare, and what you do not

You declare entities, relations, and four invariants. Everything else about storage is ours to
decide.

The four exist because no amount of watching traffic reveals them:

| Declaration | Why traffic cannot tell us |
|---|---|
| `atomic_with` | that two entities must commit together is a business rule, not a pattern |
| `residency` | where data may legally live is not visible in a query |
| `pii` | which column is personal data determines retention and what may be denormalised |
| `cost_ceiling` | your budget is not in your workload |

Anything beyond those four and the list of engines you have available would be us handing work back
to you, which is the opposite of what this is for.

## Two guarantees, and how to check them yourself

**We are not in your data path.** The library connects to your engines directly and answers every
operation from a locally cached placement map. Our service being down does not make your application
down. There is no configuration for this — there is no code path that routes your queries through us,
which you can confirm by reading `routing.py`: it is a dictionary lookup and three conditions.

**We never see a row.** Telemetry carries operation *shapes* and counts, never values, and a shape is
assembled from the structure of the call rather than from a query string — so there is nowhere for a
value to come from. This is the reason the library is open source: the guarantee is checkable instead
of promised.

## It works without an account

Write a placement map by hand, point the library at it, and everything runs: no key, no network, no
account.

```python
placement = sde.load_map(json.load(open("placement.json")), model=model)
```

An unsigned map is valid. A *signed* map with no key to verify it is not, because a signature is a
claim that it came from us and an unverifiable claim is worse than no claim. This mode is supported
and tested, not tolerated — it is also the honest answer to what happens if you stop paying us.

## Install

```bash
pip install smart-data-engine-sdk              # core, no dependencies at all
pip install 'smart-data-engine-sdk[signed]'    # verify maps we signed
pip install 'smart-data-engine-sdk[postgres]'  # PostgreSQL engine driver
```

The core has no runtime dependencies. This library goes into your application, so every dependency
would be one you inherit and a version conflict you might have to resolve.

**The distribution is `smart-data-engine-sdk` and the import is `sde`, and neither half of that is a
preference.** `sde` alone is taken on PyPI by somebody else, and renaming the import to a
squatting-adjacent misspelling is worse for you than a distribution name that differs from it. The
obvious distribution name, `smart-data-engine`, is *refused by PyPI*: it answers 400, "the name is
too similar to an existing project". The project is `smartdata-engine`, registered by somebody else
with zero releases — PyPI compares names after stripping `._-` and folding `l`/`i` to `1` and `o` to
`0`, and both reduce to `smartdataeng1ne`. The suffix changes the reduction, and it matches the
repository name, which is the consistency worth having regardless.

Published to PyPI on 12 September 2026. Until then this section was the one instruction in this
repository that was knowingly untrue, and it carried a warning saying so; the warning is gone because
the commands work.

## Conformance

Everything in `canonical.py`, `model.py`, `shapes.py` and `routing.py` implements a cross-language
contract, pinned by vectors in `../conformance/vectors`. The Python, TypeScript, Java and Rust
libraries run the same vectors in their own test runners, so a divergence is a red test for whoever
caused it rather than an operation written to the wrong engine in production.

If you are porting this to another language, `docs/format-contract.md` is meant to be sufficient on
its own. If it is not, that is a bug in the document.

## Licence

Apache-2.0.

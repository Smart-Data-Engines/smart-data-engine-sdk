---
name: Bug report
about: Something the library does that it should not
title: ''
labels: bug
---

## What happened

## What you expected

## Reproducing it

<!-- The most useful thing here is a declared model plus the call that misbehaves. Please do not
     paste real data - a shape or a field name is enough, and this is a public tracker. -->

```python
```

## Environment

- library version:
- language and version: <!-- Python 3.12 / Node 20 / ... -->
- engine and version: <!-- PostgreSQL 15, ClickHouse ..., or "none, this is a Tier 0 problem" -->
- placement map: <!-- hand-written, or issued by the control plane -->

## Is this a byte-contract disagreement?

<!-- If two implementations disagree - a different `model_version`, a different digest, a different
     encoding for the same model - say so here. That class of bug takes priority over everything
     else, because it makes maps issued for one language unusable in another. -->

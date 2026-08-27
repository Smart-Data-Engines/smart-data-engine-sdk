## What and why

<!-- What does this change do, and what problem does it solve? -->

## How it was verified

- [ ] `make check` is green (ruff, mypy --strict, pytest, tsc, vitest)
- [ ] `make conformance` is green — the shared vectors pass in **both** runners
- [ ] New behaviour is covered by a test

## Cross-language impact

<!-- This library exists in more than one language and they have to agree byte for byte. A change
     that lands in one implementation and not the other shows up as an operation written to the wrong
     engine, not as a compile error. -->

- [ ] Python only, and nothing here is part of the byte contract
- [ ] Changed in every implementation, with a shared vector covering it

## Byte contract

<!-- `docs/format-contract.md` is what a fifth implementation is written from. If the encoding, the
     ordering, the hashing or the model version derivation moved, this section is not optional. -->

- [ ] No contract change
- [ ] Contract changed — the document is updated, `conformance/contract-version.txt` is bumped, and
      the reason a client's existing `model_version` may change is written down

## Privacy

<!-- The claim is that not one row of a client's data reaches us. Anything touching telemetry,
     shapes or logging has to say which of the two it is. -->

- [ ] Does not touch telemetry, operation shapes or logging
- [ ] Touches them — and a negative test asserts no value can travel in the new field

## Notes for the reviewer

<!-- Anything worth a second pair of eyes: a tricky edge case, a decision you are unsure about. -->

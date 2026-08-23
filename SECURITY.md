# Security

## Reporting

Email **security@smartdataengines.com**. Please do not open a public issue for a vulnerability.

We will acknowledge within three working days and tell you what we intend to do and when. If we
disagree that something is a vulnerability we will say so and explain, rather than going quiet.

## What is in scope

This repository is the client library. It runs inside someone else's application and connects to
their databases, so the interesting questions are about what it does with what it is given:

- **Anything that would send a client's data to us.** Telemetry carries operation shapes and counts,
  never values, and a shape is assembled from the structure of a call rather than from a query string.
  A path by which a value could reach a telemetry payload is the most serious bug this library could
  have, and we would like to hear about it before anyone else does.
- **Anything that would make a placement map trusted when it should not be.** The map decides where
  data is written. A signature that verifies when it should not, a map for the wrong model version
  being accepted, a contract version being guessed at.
- **Injection through identifiers.** Table and column names come from the map, not from user input,
  and are always quoted — but if you find a path where they are not, that is in scope.
- **Credential handling.** Connection details stay in the client's process. The control plane holds
  no credentials for a client's engines at all.

## What is deliberately not a vulnerability

**An unsigned placement map is accepted.** That is the no-account mode: write a map by hand, run with
no key and no network. It is documented and tested. What is refused is a map carrying a signature we
have no key to verify — an unverifiable claim, which is worse than no claim.

**The map format is public.** It has to be: the library parses it, and the library is open source. A
client can write their own map and run without us, and we would rather say that plainly than pretend
the format is a moat.

## Verifying the privacy claim yourself

You do not have to take our word for any of it, which is the reason this library is open source:

- `python/src/sde/routing.py` is a dictionary lookup and three conditions. There is no code path that
  routes a query through us.
- `python/src/sde/shapes.py` builds an operation shape from a model and a call's structure. No
  argument reaches it.
- `python/tests/test_groups_and_shapes.py` asserts the shape encoding surface, so a new field cannot
  be added without a test noticing.

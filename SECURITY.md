# Security

## Reporting

Use GitHub's [private vulnerability
reporting](https://github.com/Smart-Data-Engines/smart-data-engine-sdk/security/advisories/new) — it is
enabled on this repository, it gives you a private thread with us, and it means you do not have to
trust that a mailbox is being read. If you would rather use email,
**security@smartdataengines.com** works too.

Either way, please do not open a public issue for a vulnerability.

## What happens after you report

| Step | Target |
|---|---|
| Acknowledgement | 3 working days |
| Assessment: validity, severity (CVSS v3.1 base score), affected versions, plan | 10 working days after the acknowledgement |
| Fixed release, critical (9.0-10.0) | 14 days after we confirm it |
| Fixed release, high (7.0-8.9) | 30 days |
| Fixed release, medium (4.0-6.9) | 90 days |
| Fixed release, low | the next regular release |

If we are going to miss a target, you hear that from us before it passes, with the reason and a new
date. If we disagree that something is a vulnerability we will say so and explain, rather than
going quiet.

## Disclosure

We publish a GitHub Security Advisory when the fixed release is on the registry, with a CVE where
one applies, and credit you unless you would rather we did not. Please keep the details private
until then, or for 90 days from your report, whichever comes first; if a fix is close, we may ask
you for more time. Subscribers of the control plane are told by e-mail no later than the advisory.

## Research in good faith

If you follow this policy, we will not take legal action against you for your research. Test
against your own installation of the libraries and the operator, and the hosted panel only with
your own account; do not access data beyond what shows the problem; do not degrade our services
(no load or denial-of-service testing of smartdataengines.com, the panel or our hosts); no social
engineering. There is no bug bounty.

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
- **Anything that would make the local operator do more than a signed instruction says.** Every
  statement the libraries and the operator can send to an engine is listed in
  [`docs/engine-statements.md`](docs/engine-statements.md), and a test holds that list to the source.
  A statement outside that list, one aimed at a table the instruction does not name, or one run
  without a signature the client's configured keys verify is in scope. So is anything that makes
  [leaving](docs/departure.md) remove a row, or release a table that is not in the map in force.

## Supported versions

Security fixes go into the latest minor release of each library and into `main`, and into the
previous minor release for 6 months after its successor is released. A fix ships as a patch release
of the affected library.

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

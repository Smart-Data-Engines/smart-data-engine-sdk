---
name: Feature request
about: Something the library should be able to do
title: ''
labels: enhancement
---

## The problem

<!-- What are you trying to do that this library makes hard? -->

## What you would like

## Which half does this belong to?

<!-- This repository is the client library: it declares a model, encodes it, routes operations and
     records shapes. Choosing where a group of entities lives, scoring engines against each other and
     orchestrating a migration are the control plane's job, and the control plane is not open source.
     A request for a planner feature is welcome, it just cannot be implemented here. -->

- [ ] The library — encoding, routing, a tier, another language, a conformance vector
- [ ] The control plane — placement, scoring, migration
- [ ] Not sure

## Would this change the byte contract?

<!-- Anything that alters the canonical encoding, the ordering rules, the hashing derivation or how
     `model_version` is computed changes what every other implementation must do, and invalidates
     existing model versions. Not a reason to say no; a reason to plan it. -->

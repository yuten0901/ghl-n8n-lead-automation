# Decisions

## 2026-09-16 — deepen the existing HighLevel project first

The acquisition log repeatedly shows that this portfolio is relevant to GHL
jobs but loses credibility because it has no real-account proof. Building a new
repository would not remove that objection. The next increment therefore adds
official Sandbox evidence to this focused project instead of merging unrelated
projects or creating another broad demo.

The offline mock remains the default because reviewability without credentials
is valuable. Live verification is opt-in, Sandbox-only, double-confirmed, and
excludes messaging, appointments, and payments.

## 2026-09-16 — update the API contract before live testing

The current official HighLevel reference documents `Version: v3`, camelCase
opportunity-search parameters, `fieldValue` in contact custom fields, and
replacement semantics for the tag array on contact upsert. The repository was
still encoding the older shapes. The strict mock and documentation are updated
before any Sandbox run so the live test measures the current contract rather
than legacy assumptions.

## 2026-09-16 — publish sanitized Sandbox evidence, not raw account data

The official Sandbox run proved authentication, pipeline/stage ownership,
idempotent contact upsert, and opportunity reuse. The public artifact retains
only booleans, counts, timestamps, API version, and irreversible ID
fingerprints. Tokens, emails, raw response bodies, and reusable account IDs stay
outside Git. Messaging, appointments, payments, and paid-account behavior remain
explicitly outside the claim.

The first write run also revealed that a newly created opportunity may not be
immediately returned by search. The verifier now polls briefly for index
convergence and never creates a second opportunity during that wait.

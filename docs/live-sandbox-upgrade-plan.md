# Live HighLevel sandbox upgrade plan

**Status:** Phase 1, Phase 2, and repository-side Phase 3 evidence completed on 2026-09-16. External portfolio updates remain.

## Goal

Turn the repository's strongest remaining caveat — no real HighLevel API run —
into client-visible evidence without weakening the account-free local demo.

The finished portfolio should prove two different things:

1. anyone can still clone and run the deterministic demo with no credentials;
2. the same integration contract has passed a controlled run against an official
   HighLevel Sandbox account.

## Scope

### Phase 1 — refresh the vendor contract

- Track the current documented HighLevel API header (`Version: v3`).
- Use the current camelCase opportunity-search parameters.
- Emit current contact custom-field entries (`fieldValue`).
- Model contact-upsert tags as replacement, not union.
- Keep the mock strict enough that old request shapes fail locally.

### Phase 2 — safe, repeatable sandbox verification

Add an opt-in command that:

- performs read-only authentication and inventory checks by default;
- treats calendar/user reads as optional so unused scopes do not block the
  contact/opportunity proof;
- refuses writes unless both an environment guard and an exact Location ID
  confirmation are supplied;
- creates test-only contact and opportunity records in a Sandbox;
- sends the same contact twice and proves that the contact ID is unchanged;
- checks that a returning lead reuses an open opportunity;
- never sends SMS/email, books an appointment, or calls a payment endpoint;
- writes a sanitized evidence file with no token, PII, or raw response body.

**Result:** Passed against an official HighLevel Sandbox. The first live write also exposed eventual consistency in opportunity search; the verifier now polls the search index without creating another opportunity. [Evidence.](evidence/ghl-sandbox-verification.json)

### Phase 3 — client-facing proof

- Run the command against an official HighLevel Sandbox.
- Capture 3–5 screenshots: contact fields/tags, opportunity pipeline, n8n run,
  retry or duplicate evidence, and the final evidence summary.
- Record a 90–150 second captioned walkthrough with no spoken-English
  requirement.
- Update the README, case study, Upwork Portfolio item, and proposal highlights.

**Current result:** Four genuine HighLevel UI captures, the sanitized JSON
evidence, and a reproducible 90-second captioned video are committed locally.
The README and case study reference them. The Upwork-side update remains.

## Acceptance criteria

- The default local demo and full offline test suite remain green.
- The mock rejects stale version/parameter/field shapes.
- A read-only sandbox preflight cannot mutate data.
- A write verification requires explicit double confirmation.
- Two identical contact upserts return the same non-empty contact ID.
- Two opportunity passes produce one open opportunity for that contact/pipeline.
- The public evidence contains only timestamps, counts, booleans, API version,
  and irreversible ID fingerprints.
- The repository never claims paid-account or production deployment experience.

## Non-goals

- A2P 10DLC registration, phone-number migration, or live SMS delivery.
- Stripe or other production payment flows.
- Funnel design, English sales copy, or account-wide migration.
- A public Marketplace listing or multi-tenant OAuth refresh service.

## Stop conditions

Do not continue a live run if the account is not visibly a Sandbox, required
scopes are missing, the selected pipeline/stage cannot be verified, or any
command would communicate with a real person.

## Official references

- [HighLevel Sandbox accounts](https://marketplace.gohighlevel.com/docs/oauth/SandboxAccount/index.html)
- [HighLevel app testing guide](https://marketplace.gohighlevel.com/docs/oauth/AppTestingGuide/index.html)
- [Upsert contact](https://marketplace.gohighlevel.com/docs/ghl/contacts/upsert-contact/)
- [Search opportunities](https://marketplace.gohighlevel.com/docs/ghl/opportunities/search-opportunity/)

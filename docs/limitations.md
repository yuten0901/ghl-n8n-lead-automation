# Limitations

What this does not do. Written so a client does not have to discover any of it after hiring me.

---

## Not verified against live third-party services

| | Status |
|---|---|
| GoHighLevel API | Implemented against the documented v2 API, exercised against a local mock. **Never called a paid sub-account.** [Eight things to verify on first contact.](ghl-integration.md#first-contact-with-a-real-account) |
| Anthropic / OpenAI | Clients implemented and configuration-selected. Request construction and response handling are tested through a stubbed transport; **no live call was made.** |
| n8n runtime | Both workflows were **imported into n8n 2.36.8**, and a repeat import updates rather than duplicating. On 2026-08-31 the main workflow was also **executed end to end** against the real service and bundled GHL mock; a repeated idempotency key produced no additional CRM calls. [Evidence and exact boundary.](n8n-runtime-verification.md) CI still validates structure rather than running n8n. |
| Docker Compose | Written but **not executed**; no Docker daemon in the build environment. YAML validity is checked. The non-Docker paths in [`demo.md`](demo.md) are the verified ones. |

**Verified from a clean checkout.** Before publishing, the repository was cloned to an empty directory with no development environment, a fresh virtualenv was created, and every command the README gives was run from there. That step is the reason three failures were found and fixed rather than shipped - see the note below.

Repeated on 2026-08-30 after the n8n import fix, from a clean clone of the published commit: **221 passing on SQLite**, `scripts/demo.py` end to end, secret scan clean over 93 files, the generator reproducing the committed workflow JSON with no drift, and both workflow files importing into a fresh n8n instance. On 2026-08-31 the main workflow was additionally run through n8n's production webhook against the service and bundled mock, including a duplicate redelivery. The earlier PostgreSQL 18 leg (217 + 1 skipped at the time) has not been re-run locally since; CI runs it on every push.

> **What the clean-checkout run caught.** The PostgreSQL CI job had been written but never executed, and it failed. Two test fixtures called `create_all()` without dropping first, so isolation worked only because SQLite gives each test its own file; on a shared PostgreSQL database, rows from the previous test survived. The third failure was a negative-control test that is inherently SQLite-specific. None of this was visible from the development machine, which is exactly the point of running it somewhere else.

> **What running n8n for real caught.** Until 2026-08-30 neither workflow could be
> imported by `n8n import:workflow` **at all**. The importer writes straight into
> `workflow_entity`, whose `id` column is `NOT NULL` and is not generated for you,
> and neither file had a top-level `id`. A second defect sat behind it: both files
> carried the same tag *names* with no ids, and `tag_entity.name` is `UNIQUE`, so a
> directory import failed partway through with some workflows written and some not.
>
> ⚠️ The lesson is about the shape of the checking, not the bug. Twenty-two tests
> validated this file — node fields, connection integrity, orphan detection, secret
> patterns, generator drift — and every one of them examined the workflow's
> *internal consistency*. Not one asked the only question that decides whether a
> client can use it: does the program that consumes this file accept it? The file
> was coherent and unusable at the same time, and no amount of more careful
> internal validation would have found that. Running the real consumer did, in one
> command. `tests/unit/test_n8n_workflow.py` now checks both requirements.

None of these are hidden behind a green badge. The tests that pass are the ones that test something.

---

## Deliberately not built

Each of these is a judgement call, not an oversight.

**No circuit breaker on the GoHighLevel client.** With one location and modest volume, retry-with-backoff plus dead letters is sufficient, and an untuned breaker causes more incidents than it prevents — it trips on a blip and fails leads that would have succeeded. It becomes correct once one location's outage would otherwise consume every worker.

**No OAuth refresh loop.** A Private Integration token does not expire and is the right choice for a single-client engagement. A 401 is treated as a loud permanent error rather than a silent refresh-and-retry. Shipping a half-tested OAuth flow would be worse than not shipping one; [`ghl-integration.md`](ghl-integration.md#tokens) says where the hook goes.

**No queue.** Retries ride on webhook redelivery plus an n8n wait node. That is genuinely adequate at the volume this is designed for, and the dead-letter table plus step memoization are already the hard part of a queue migration — swapping the transport is the easy half.

**No authentication on `/admin/*`.** It exposes lead metadata and a replay trigger, and is meant to sit behind the deployment's own boundary. Adding auth would have meant inventing a user model this project does not otherwise need. **Do not expose it publicly.** Flagged rather than hidden, in [`security.md`](security.md#least-privilege) too.

**No retention policy on stored payloads.** Raw bodies are kept because replaying a failed lead requires them. That is recoverability traded against retention, and the right N days is the client's policy, not a portfolio default. The purge is a single scheduled `UPDATE`.

**No admin UI.** The admin surface is JSON over HTTP. A client who wants a dashboard has n8n's execution list and their GoHighLevel pipeline; a third UI would be maintenance without a reader.

---

## Known weak spots

**Body-hash idempotency keys.** When a source sends no delivery id and no event id, the key is a hash of the body. Two genuinely distinct submissions with byte-identical bodies, no id and no timestamp would collapse into one. In practice payloads carry a timestamp. `/admin/stats` reports how many events relied on the weak derivation, so the exposure is measurable rather than assumed.

**`ensure_opportunity` is search-then-create.** Contacts are safe — GoHighLevel's `upsert` resolves the race server-side, and the local `leads` table backs it up. Opportunities have no equivalent: the code searches for an open opportunity and creates one if it finds none, so two *different* events for the same person arriving simultaneously can both find nothing and both create a card. The window is small and the outcome is cosmetic rather than lossy (a duplicate card a human closes), which is why it is documented rather than fixed — closing it properly needs either a GHL-side constraint that does not exist or an advisory lock keyed on the contact. The README used to claim "no check-then-act anywhere", which was not true of this path; it now says so.

**Tags accumulate in GoHighLevel.** Upsert unions tags rather than replacing them, so a lead that arrives `priority-medium` and later `priority-high` carries both. That is platform behaviour; the `lead_priority` custom field is authoritative because it is overwritten. Making tags mutually exclusive costs an extra API call per lead against the rate limit — a trade-off that should be the client's call, so it is not made silently. [Detail.](ghl-integration.md#known-behaviours-worth-stating)

**The deterministic scorer is English-only keyword matching.** Sarcasm, unusual phrasing and other languages defeat it. That is exactly the gap the LLM closes, which is why the model is the default in a paid deployment and the rules are the safety net — but a deployment running fallback-only should know its ceiling.

**Follow-up message copy is illustrative.** The wording in `ghl/operations.py` demonstrates the branch, not the campaign. Real content belongs in GoHighLevel workflows where the client's marketer can edit it without a deploy.

**One GoHighLevel location.** `config/ghl-mapping.json` is a single location's mapping. Multi-tenant means moving it into a table keyed by `location_id`; the code reads it through one interface, so nothing else changes.

**SQLite is single-writer.** Correct for one process, and every guarantee here still holds. It will not scale across workers. PostgreSQL is a URL change and has its own CI job.

**No load testing.** No claim is made about throughput. The tests prove correctness under concurrency (five simultaneous deliveries → one contact), not capacity.

---

## What a production deployment needs before go-live

1. Authentication or a network boundary in front of `/admin/*`.
2. A retention policy for `raw_payload`.
3. A real secret manager instead of a `.env` file.
4. TLS termination and edge rate limiting.
5. Alerting on `unverified_signatures`, `open_dead_letters` and `stale_processing` — all three are exposed on `/admin/stats`; nothing is watching them.
6. **A sweeper for abandoned events.** `PROCESSING_LEASE_SECONDS` is only ever *read*, by `claim_event`, when a redelivery happens to arrive. If a worker dies and the sender never redelivers, the event sits in `processing` for good. `/admin/stats` now counts those so they are at least visible; a scheduled job that flips expired leases back to `failed` is the actual fix.
7. PostgreSQL, if more than one worker.
8. `python scripts/discover_ghl_ids.py` against the client's location, and a first-contact pass through the [eight verification items](ghl-integration.md#first-contact-with-a-real-account).

# Security

Scoped to what this system actually touches: other people's customer data, a CRM that can send messages on a business's behalf, and three sets of API credentials.

---

## Secrets

**Nothing is committed.** Not GoHighLevel tokens, not webhook secrets, not model API keys, not client data, not production ids.

- `.env` is gitignored; [`.env.example`](../.env.example) documents every variable and holds no real value.
- `config/ghl-mapping.json` contains only `*_DEMO*` placeholders. Ids are configuration, not secrets — they identify, they do not authorise — but a real one still belongs to a client, so the committed file has none.
- n8n credentials are referenced by id, never embedded. Every `credentials` block carries `REPLACE_WITH_YOUR_CREDENTIAL_ID`, and a test enforces that.

**CI scans on every push** — [`scripts/scan_secrets.py`](../scripts/scan_secrets.py) checks for OpenAI/Anthropic/AWS/Slack/Google key shapes, JWTs, private key blocks, GHL private-integration tokens, and populated `password = "..."`-style assignments. It also verifies `.env` is not tracked, which is the leak that actually happens.

Two things keep it honest rather than decorative:

- **Allow-listing is by exact literal**, not by loose pattern. Replacing a placeholder with a real key trips it; you cannot satisfy the scanner by deleting the demo values.
- **CI plants a fake key and requires the scanner to fail.** A scanner that can only ever pass proves nothing. This one is verified to fire on every run.

It is not a replacement for gitleaks or trufflehog and does not claim to be. It targets the shapes this repository could plausibly leak.

---

## Webhook verification

**Scheme:** `X-Signature: t=<unix seconds>,v1=<hex hmac-sha256>` over `{timestamp}.{raw body}` — the same shape Stripe and several other providers use, chosen because it is well understood and easy for a client's existing sender to produce.

Three properties, each of which is a real attack if missing:

**Constant-time comparison.** `hmac.compare_digest`, never `==`. String equality short-circuits on the first differing byte and leaks the signature to a patient attacker.

**The signature covers a timestamp.** Signing only the body makes a captured request valid forever. Timestamps outside a 300-second window are rejected — in both directions, so a forged future timestamp does not buy extra life.

**Raw bytes are verified, not a re-parsed body.** `json.dumps(json.loads(x))` is not `x`; it differs in key order and whitespace. The route hands the verifier `bytes` straight from the request. There is a test that signs `{"b":2,"a":1}`, re-serialises it, and asserts the signature no longer verifies.

### When no secret is configured

The service accepts the request and reports `signature_verified: false`. It **does not** claim a verification it did not perform — the alternative, quietly returning `verified: true` when there is nothing to verify against, is how a deployment ends up believing it is authenticated when it is not.

That state is persisted on the event row and counted by `/admin/stats` as `unverified_signatures`, so it is visible rather than assumed. Set `REQUIRE_SIGNATURE=true` in staging and production; with no secret configured that makes the service reject everything, which is the correct way to fail.

Some senders (Meta, Google) cannot produce a custom HMAC header. For those, the practical controls are a long unguessable webhook path, IP allow-listing where the sender publishes ranges, and the provider's own verification scheme — and the honest position is that the service records those deliveries as unverified rather than pretending otherwise.

---

## PII

The data here is other people's names, email addresses, phone numbers and home service addresses. The two easiest places to leak it are logs and third-party API calls, so both are handled explicitly.

**Logs.** Every `extra=` field passes through `redact()`, which masks emails and phone numbers at any nesting depth before the line is emitted. Structured logging makes this enforceable in one place rather than at every call site.

The phone pattern is deliberately conservative. An earlier, looser version masked the middle of correlation ids (`cid_4155...`) as phone numbers, destroying the one field the logs exist to make searchable. It now requires at least nine digits and refuses to match inside a word. There is a regression test.

**Model calls.** The LLM receives `has_phone: true`, `has_email: true`, `has_name: true` — never the values. Classification does not need them, so they do not travel to a third party. See [`ai-qualification.md`](ai-qualification.md#prompt-injection).

**Storage.** Raw payloads are kept on `webhook_events` and `dead_letters` because replaying a failed lead requires the original body. That is a deliberate trade: recoverability against retention. A production deployment should add a retention policy — a scheduled purge of `raw_payload` on succeeded events older than N days keeps the ledger and the audit trail while dropping the customer data. Not implemented here; it belongs to the client's retention policy, not to a portfolio default.

**Right to erasure.** Deleting a person means deleting their `leads` row and nulling `raw_payload` on their events. The `identity_key` makes that a single indexed lookup. The audit log is append-only by design and holds ids and step names, not contact details.

---

## Least privilege

**GoHighLevel scopes** — request only what is used. A deployment that never books appointments leaves the calendar scopes off and nothing degrades. The full list is in [`ghl-integration.md`](ghl-integration.md#scopes).

**Prefer a Private Integration token** scoped to one sub-account over an agency-level token. A leaked location-scoped token exposes one client; an agency token exposes all of them.

**The container runs as an unprivileged user** (uid 10001). Nothing here needs root.

**The admin API has no authentication in this repository.** Stating that plainly because it matters: `/admin/*` exposes lead metadata and a replay trigger, and it is meant to sit behind the deployment's own boundary — a private network, a reverse proxy with auth, or an ingress rule. Do not expose it publicly. Adding auth here would have meant inventing a user model this project does not otherwise need; the honest answer is that this is a deployment concern, and it is flagged rather than hidden.

---

## Attack surface

| Vector | Handling |
|---|---|
| Forged webhook | HMAC with timestamp; unverified requests are marked and counted |
| Replayed webhook | Timestamp window; idempotency ledger makes a replay a no-op regardless |
| Malformed payload | Rejected with 400 before any CRM write; never a 500 |
| Oversized payload | Bounded in n8n (100 KB) and by field-level length caps in normalization |
| Control characters / unicode tricks in free text | Stripped and NFKC-normalized in `clean_text` |
| Prompt injection via the message body | Model classifies, deterministic rules route — see [`ai-qualification.md`](ai-qualification.md#prompt-injection) |
| Hallucinated enum value reaching a routing rule | Closed vocabulary, coerced to `other` |
| SQL injection | Parameterised throughout; no string-built SQL anywhere |
| Duplicate CRM writes from a retry | Insert-first idempotency plus per-step memoization |
| Credential in a shipped n8n workflow | Enforced by test and by the CI secret scan |

---

## What a production deployment still needs

Not implemented here, and listed so nobody assumes otherwise:

1. **Authentication on `/admin/*`**, or a network boundary in front of it.
2. **A retention policy** for `raw_payload`.
3. **Token rotation**, and the OAuth refresh loop if using OAuth rather than a Private Integration token.
4. **TLS termination and rate limiting at the edge** — the service assumes something in front of it does both.
5. **A real secret manager** (Vault, AWS Secrets Manager, Doppler) rather than a `.env` file on the host.
6. **Alerting on `unverified_signatures` and `open_dead_letters`** — both are already exposed on `/admin/stats`; nothing is watching them.

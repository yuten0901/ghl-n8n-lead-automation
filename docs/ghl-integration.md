# GoHighLevel integration

## What is being claimed

This is an **implemented integration interface** against the current documented HighLevel `v3` contract, exercised end to end against a local mock that reproduces the request and response shapes used here.

On 2026-09-16, the contact/opportunity path also passed a credentialed run against an official HighLevel Sandbox: two identical contact upserts returned one stable contact ID, and the open opportunity was reused rather than duplicated. [Sanitized evidence](evidence/ghl-sandbox-verification.json). It has **not** been run against a paid GoHighLevel sub-account. [First contact with a real account](#first-contact-with-a-real-account) lists what remains deliberately outside the Sandbox proof.

There are no GoHighLevel UI screenshots in the repository yet. Only genuine Sandbox captures will be added; the committed JSON exposes fingerprints and outcomes rather than credentials or raw account data.

---

## Connection

| | |
|---|---|
| Base URL | `https://services.leadconnectorhq.com` |
| Auth | `Authorization: Bearer <token>` |
| Version | `Version: v3` — **required**, and pinned deliberately |
| Content | `Accept: application/json`, `Content-Type: application/json` |

The version header is not optional. Pinning the current documented contract means an upstream change becomes a deliberate upgrade rather than a silent production change. The mock rejects a missing or stale value, so this class of error fails locally first.

### Tokens

Two ways in, and the choice matters:

**Private Integration token** (sub-account → Settings → Private Integrations). Scoped to one location, does not expire, no OAuth dance. This is the right choice for a single-client engagement and it is what `GHL_ACCESS_TOKEN` expects by default.

**OAuth 2.0**, for an app installed across multiple locations:

```
authorize  https://marketplace.gohighlevel.com/oauth/chooselocation
token      POST https://services.leadconnectorhq.com/oauth/token
           grant_type=authorization_code | refresh_token
```

Access tokens expire in **24 hours**; refresh tokens are long-lived. This repository does not implement the refresh loop — with a Private Integration token it is not needed, and shipping a half-tested OAuth flow would be worse than not shipping one. A 401 is therefore treated as a **permanent** error: it is raised loudly and dead-lettered rather than retried, because retrying an expired token four times just fails four times. In a multi-location deployment, 401 is where the refresh-and-retry-once hook goes.

### Scopes

| Scope | Used for |
|---|---|
| `contacts.write`, `contacts.readonly` | upsert, update, tags, notes |
| `opportunities.write`, `opportunities.readonly` | search, create, stage moves |
| `conversations/message.write` | SMS and email follow-up |
| `locations/customFields.readonly` | `scripts/discover_ghl_ids.py` |
| `calendars.readonly`, `calendars/events.write` | only if the appointment node is enabled |

Least privilege: if a deployment never books appointments, leave the calendar scopes off. Nothing else degrades.

### Rate limits

**100 requests per 10 seconds** burst, plus a daily ceiling, **per location**. That is generous for one business and tight for an agency running forty on one token.

429 is therefore a first-class outcome, not an exception case. The client honours `Retry-After` and applies full-jitter backoff — without jitter, every pending lead retries at the same instant after a rate-limit and the burst repeats. [`docs/architecture.md`](architecture.md#what-changes-at-100-the-volume) covers the per-location token bucket that becomes necessary at agency scale.

---

## Endpoints used

### Contacts

**`POST /contacts/upsert`** — the primary write.

```json
{
  "locationId": "loc_...",
  "firstName": "Dana",
  "lastName": "Whitfield",
  "name": "Dana Whitfield",
  "email": "dana.whitfield@example.com",
  "phone": "+15125550147",
  "city": "Round Rock, TX",
  "source": "leadops:website",
  "tags": ["leadops", "source-website", "priority-high", "emergency"],
  "customFields": [
    { "id": "cf_...", "fieldValue": "90" },
    { "id": "cf_...", "fieldValue": "emergency_repair" }
  ]
}
```

```json
{
  "new": false,
  "contact": { "id": "ct_...", "locationId": "loc_...", "email": "...", "tags": ["..."] },
  "traceId": "trace_..."
}
```

**Why upsert rather than search-then-create.** Search-then-create has a race: two forms submitted seconds apart both find nothing and both create. GoHighLevel already resolves this server-side, matching **email first, then phone, within the location**. Our own `leads` table is the second line of defence, not the first.

This is also exactly why normalization matters. `Dana.Whitfield@Example.COM ` and `dana.whitfield@example.com` are the same person to a human and different strings to a matcher; `(512) 555-0147` and `+15125550147` likewise. Normalizing before the call is what makes GHL's matching work.

**`GET /contacts/search/duplicate?locationId=&email=&number=`** — explicit lookup, used when a decision has to be made before writing.

**`PUT /contacts/{contactId}`** · **`POST /contacts/{contactId}/tags`** · **`POST /contacts/{contactId}/notes`**

The note carries the AI summary in prose. Custom fields are for filtering and automation; a note is what the person picking up the phone actually reads. Both, not either.

### Opportunities

**`GET /opportunities/search?locationId=&contactId=&pipelineId=&status=open`**

**`POST /opportunities/`**

```json
{
  "locationId": "loc_...",
  "pipelineId": "pipe_...",
  "pipelineStageId": "stage_...",
  "contactId": "ct_...",
  "name": "Dana Whitfield - emergency repair",
  "status": "open",
  "monetaryValue": 1200,
  "source": "leadops:website"
}
```

**`PUT /opportunities/{id}`** — stage moves and value updates.

**The rule that prevents the angriest client message.** A returning lead with an *already open* opportunity gets that opportunity **updated** — moved stage, value refreshed — never a second card on the board. A new opportunity is created only when nothing is open. Search first, then decide.

Opportunity search now uses the same camelCase ID parameters as the other current endpoints. The strict mock rejects the legacy snake_case shape by returning no match.

### Conversations and calendar

**`POST /conversations/messages`** — `{"type": "SMS" | "Email", "contactId": "...", "message": "..."}`; email adds `subject` and `html`.

**`POST /calendars/events/appointments`** — `{"calendarId", "locationId", "contactId", "title", "appointmentStatus": "new"}`.

---

## Account-specific ids

Every GoHighLevel location has **its own** custom-field ids, pipeline id and stage ids. They are not portable. This is the single most common reason a workflow that "worked in the demo" fails on the client's account.

They live in `config/ghl-mapping.json`, and every value there is a placeholder. Fill it in one command:

```bash
GHL_ACCESS_TOKEN=... GHL_LOCATION_ID=... python scripts/discover_ghl_ids.py > config/ghl-mapping.json
```

The script is read-only, matches field and stage names case-insensitively (so a client who called it "Lead Score" still maps), and reports on stderr what it could not find — so stdout stays a clean, redirectable JSON file.

**Ids are configuration, not secrets.** They identify; they do not authorise. Committing a real location id is not a leak. Committing the token is.

Two deliberate behaviours make a partly-wrong mapping non-fatal:

- **Unmapped custom fields are skipped, not sent.** Pushing an unknown field id is a 422 that fails the whole contact write. Losing one analytics field is not worth losing the lead. `unmapped_fields()` reports what was skipped.
- **An unknown pipeline stage falls back to `new_lead`.** A routing rule naming a stage that was renamed in GoHighLevel should land the lead somewhere a human will see it, not reject it. The fallback is logged as `routing.unknown_stage` so the drift is visible.

### Custom fields to create

Text fields on the sub-account:

| Field | Example | Why |
|---|---|---|
| Lead Source | `website` | Attribution inside GHL, where the client actually reports |
| Lead Score | `90` | Filter and sort the pipeline |
| Lead Priority | `high` | Workflow triggers |
| Service Category | `emergency_repair` | Segmentation |
| AI Summary | *(prose)* | Readable on the contact record |
| Missing Information | `phone number, service address` | Tells the salesperson what to ask |
| Correlation Id | `cid_2b6e...` | Joins a CRM record to our logs during a support call |
| Qualification Mode | `deterministic` \| `anthropic` \| `fallback` | **The most useful field during an incident:** was this scored by the model or the fallback? |

---

## Known behaviours worth stating

**Upsert tags replace the current tag array.** The current contract documents replacement semantics. This integration therefore sends the complete desired set on each upsert; `lead_priority` remains the authoritative value for reporting. Incremental changes outside this flow should use the dedicated add/remove-tag endpoints so they are not accidentally overwritten.

**Contact id location varies.** Some responses put the object at the top level, some under `contact`. The client handles both — cheaper than being wrong on the client's account.

---

## First contact with a real account

The remaining items to verify before this touches a paid sub-account. The official Sandbox command intentionally avoids messaging, appointments, payments, and real customer data.

1. **Re-check the documented version before deployment.** The contract is pinned to `v3`; a future upgrade remains a deliberate change.
2. **Confirm the paid location's duplicate-contact settings.** Upsert behavior follows those account-level settings; this integration explicitly sends `createNewIfDuplicateAllowed=false` in the Sandbox proof.
3. **Confirm custom-field IDs and data types.** The current contact shape is `{"id", "fieldValue"}`; IDs remain location-specific.
4. **Observe a real 429 response.** The client honors `Retry-After` when supplied and uses jittered backoff otherwise, but the Sandbox proof does not intentionally exhaust its rate limit.
5. **Verify the conversation and sender configuration before any outbound message.** No Sandbox verification sends SMS or email.
6. **Verify appointment configuration before enabling booking.** No Sandbox verification creates appointments.
7. **Replace the internal sales alert if necessary.** `notify_sales` still contains the documented uncertainty around `emailTo` and `userId`; Slack or SMTP is safer until the account-specific behavior is proven.

Run `python scripts/verify_ghl_sandbox.py` first. It exercises four read-only endpoints and writes only sanitized evidence. The write pass requires `GHL_SANDBOX=true`, `--write-test`, and an exact Location ID confirmation; it never calls messaging, appointment, or payment endpoints.

---

## The mock

`mock/ghl_mock/server.py` implements the endpoints above with the documented response shapes, plus scripted fault injection:

```bash
curl -X POST http://127.0.0.1:8081/_mock/faults \
  -H "Version: v3" -H "Authorization: Bearer demo_token_value" \
  -d '{"operation":"contacts.upsert","mode":"500","times":2}'
```

Modes: `500`, `503`, `429` (with `Retry-After`), `timeout`, `401`, `422`, `empty_body`.

Faults are **scripted, not random**, so a test that passes today passes tomorrow. `GET /_mock/state` returns everything created, including a per-operation call count — which is how the duplicate-delivery test proves that five deliveries produced exactly one `contacts.upsert`.

It is a stand-in, not a claim of behavioural equivalence. Its job is to make the failure paths — retries, rate limiting, partial failure, duplicate handling — runnable and assertable by anyone, with no account and no spend.

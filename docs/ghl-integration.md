# GoHighLevel integration

## What is being claimed

This is an **implemented integration interface** against the documented LeadConnector API v2, exercised end to end against a local mock that reproduces the documented request and response shapes.

It has **not** been run against a paid GoHighLevel sub-account, because no such account was available while building this portfolio project. Everything on this page is written from the published v2 API surface. [First contact with a real account](#first-contact-with-a-real-account) lists exactly what to re-verify, and it is a short list because the mock encodes the contract rather than a guess at it.

There are no screenshots of a GoHighLevel UI in this repository. Fabricating them would be worth less than saying this plainly.

---

## Connection

| | |
|---|---|
| Base URL | `https://services.leadconnectorhq.com` |
| Auth | `Authorization: Bearer <token>` |
| Version | `Version: 2021-07-28` — **required**, and pinned deliberately |
| Content | `Accept: application/json`, `Content-Type: application/json` |

The version header is not optional and GoHighLevel ships breaking changes behind new version dates. Pinning it means an upstream change becomes a deliberate upgrade rather than a Tuesday-morning outage. The mock rejects a request without it, so a missing header fails locally instead of on the client's account.

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
    { "id": "cf_...", "field_value": "90" },
    { "id": "cf_...", "field_value": "emergency_repair" }
  ]
}
```

```json
{
  "succeded": true,
  "new": false,
  "contact": { "id": "ct_...", "locationId": "loc_...", "email": "...", "tags": ["..."] }
}
```

> The response field really is spelled `succeded`. The client does not depend on it — `new` and the HTTP status are what it reads — but it is reproduced in the mock so nobody "fixes" the spelling and breaks a real integration.

**Why upsert rather than search-then-create.** Search-then-create has a race: two forms submitted seconds apart both find nothing and both create. GoHighLevel already resolves this server-side, matching **email first, then phone, within the location**. Our own `leads` table is the second line of defence, not the first.

This is also exactly why normalization matters. `Dana.Whitfield@Example.COM ` and `dana.whitfield@example.com` are the same person to a human and different strings to a matcher; `(512) 555-0147` and `+15125550147` likewise. Normalizing before the call is what makes GHL's matching work.

**`GET /contacts/search/duplicate?locationId=&email=&number=`** — explicit lookup, used when a decision has to be made before writing.

**`PUT /contacts/{contactId}`** · **`POST /contacts/{contactId}/tags`** · **`POST /contacts/{contactId}/notes`**

The note carries the AI summary in prose. Custom fields are for filtering and automation; a note is what the person picking up the phone actually reads. Both, not either.

### Opportunities

**`GET /opportunities/search?location_id=&contact_id=&pipeline_id=&status=open`**

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

Note the parameter-style inconsistency: opportunity search uses `location_id` / `contact_id` (snake_case) while contact endpoints use `locationId` (camelCase). That is the real API's inconsistency, reproduced in the mock so it is caught here rather than there.

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

**Tags accumulate.** GoHighLevel's upsert unions tags rather than replacing them. A lead that arrives as `priority-medium` and later as `priority-high` ends up carrying both. This is the platform's behaviour, not a bug here, and it is why the **`lead_priority` custom field is authoritative** — it is overwritten on every write. If a client needs mutually exclusive priority tags, the fix is an explicit `DELETE /contacts/{id}/tags` for the opposing tags before the upsert, at the cost of an extra round trip against the rate limit. That trade-off should be the client's call, so it is not made silently here.

**Contact id location varies.** Some responses put the object at the top level, some under `contact`. The client handles both — cheaper than being wrong on the client's account.

---

## First contact with a real account

The eight things to verify the first time this touches a paid sub-account. None require code changes if they hold.

1. **`Version: 2021-07-28` is still current.** If GoHighLevel has published a newer version date, read its changelog before bumping — the pin exists so this is a decision.
2. **`POST /contacts/upsert` returns `new` and a contact id in the shape above.** If the id moves, `_extract_contact_id` already handles the two known shapes; add a third if needed.
3. **Duplicate matching is email-then-phone within the location.** Send the same person twice with a differently formatted phone and confirm one contact.
4. **`GET /opportunities/search` really uses snake_case parameters.** If it is camelCase on the live API, it is a one-line change in `ghl/client.py`.
5. **429 carries `Retry-After`.** If it does not, our jittered backoff already covers it; confirm which.
6. **Custom field writes accept `{"id", "field_value"}`.** Some accounts prefer `{"key", "field_value"}` — the mapping supports both, per field.
7. **`POST /conversations/messages` requires a conversation to exist first** on some account configurations. If so, the first-touch message needs a conversation create ahead of it — an additive change to `operations.send_follow_up`.
8. **The internal sales alert actually reaches a salesperson.** `notify_sales` posts to `/conversations/messages` with the *customer's* `contactId` plus `emailTo` and `userId`, expecting GoHighLevel to redirect delivery to the internal address. Those two fields are **not part of the request shape documented above**, and the local mock stores the body without validating it — so this is an assumption this repository has never tested. Check it before the first live lead: the failure mode is a customer receiving an email that begins "New high-priority lead: <their own name>". If it does not redirect, the fix is a separate internal channel (Slack webhook or SMTP) rather than a GoHighLevel conversation.

Run `python scripts/discover_ghl_ids.py` first; it exercises four read-only endpoints and will surface auth or scope problems before any write is attempted.

---

## The mock

`mock/ghl_mock/server.py` implements the endpoints above with the documented response shapes, plus scripted fault injection:

```bash
curl -X POST http://127.0.0.1:8081/_mock/faults \
  -H "Version: 2021-07-28" -H "Authorization: Bearer demo_token_value" \
  -d '{"operation":"contacts.upsert","mode":"500","times":2}'
```

Modes: `500`, `503`, `429` (with `Retry-After`), `timeout`, `401`, `422`, `empty_body`.

Faults are **scripted, not random**, so a test that passes today passes tomorrow. `GET /_mock/state` returns everything created, including a per-operation call count — which is how the duplicate-delivery test proves that five deliveries produced exactly one `contacts.upsert`.

It is a stand-in, not a claim of behavioural equivalence. Its job is to make the failure paths — retries, rate limiting, partial failure, duplicate handling — runnable and assertable by anyone, with no account and no spend.

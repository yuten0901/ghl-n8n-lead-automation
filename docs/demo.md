# Running it

Every command on this page was executed while writing it. The `demo` job in CI runs `scripts/demo.py` on every push, so these instructions cannot silently rot.

**No paid accounts are needed for any of it.** No GoHighLevel account, no OpenAI or Anthropic key, no Docker.

---

## Setup

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"      # Windows: .venv\Scripts\pip install -e ".[dev]"
```

Python 3.11, 3.12 or 3.13. No `.env` is required — the defaults are a working configuration that points at the bundled GoHighLevel mock and the offline qualifier.

---

## 1. The scripted demo

```bash
python scripts/demo.py
```

Eight scenarios against the real pipeline and the real mock, in one process. Abridged output:

```
GoHighLevel + n8n lead automation - local demo
No credentials, no network, no Docker. GHL is the mock in mock/.

[1] Emergency lead from a website form
    High intent, complete details -> hot pipeline stage, SMS, sales alert.
    status          succeeded
    qualification   emergency_repair / high / score 90
    routed by       emergency_high_priority -> stage 'hot_lead', notify sales
    because         Emergency work is time-priced; a five-minute callback wins the job.
    CRM now         1 contact(s), 1 opportunity(ies), 2 message(s) sent

[2] The same webhook delivered four more times
    The single most common real-world problem. Nothing must be written twice.
    status          duplicate
    CRM now         1 contact(s), 1 opportunity(ies), 2 message(s) sent
    GHL contact upserts: 1 (not 5)

[3] Meta Lead Ads, then Google Ads, for the same person
    Two genuinely different events, one human. One contact, one opportunity.
    status          succeeded
    contacts        2 (website lead + this person, not four)

[4] A lead with no email and no phone
    status          dead_lettered
    error           validation_failed (retryable=False, terminal=True)

[5] Marketing spam through the contact form
    status          succeeded
    routed by       spam_or_unqualified -> stage 'unqualified'
    messages sent   0 (no customer contact)

[6] A lead whose message tries to instruct the AI
    qualification   other / low / score 5
    routed by       spam_or_unqualified -> stage 'unqualified'

[7] GoHighLevel returns 500 twice, then recovers
    status          succeeded
    upsert attempts 8 total; contacts created this scenario: 1

[8] Contact succeeds, then the opportunity write fails permanently
    status          failed
    contact created, opportunity missing, customer NOT messaged
    ...the outage ends; the sender redelivers the same webhook...
    status          succeeded
    contact re-created? no - upsert calls unchanged (True)

Final CRM state
  - Dana Whitfield           score  90  tags: leadops, source-website, priority-high, emergency, call-now
  - Priya Raghunathan        score  50  tags: high-value, installation, leadops, needs-info, ...
  - Growth Partner           score   5  tags: leadops, source-website, priority-low, unqualified
  - Marcus Oyelaran          score  60  tags: leadops, source-website, priority-low, qualified
```

Scenario 8 is the one worth reading twice. The first delivery creates the contact and then fails on the opportunity; the redelivery **resumes** — the upsert call count does not move — instead of creating a second contact. That is the failure most lead automations get wrong.

The two `WARNING`/`ERROR` log lines during scenarios 4 and 8 are the system working: they are the structured logs a real deployment would ship, and they carry the correlation id.

---

## 2. Over HTTP, the way n8n calls it

Two terminals, or background both:

```bash
python scripts/run_mock_ghl.py                        # mock GoHighLevel on :8081
uvicorn leadops.api.main:app --port 8000              # the service on :8000
```

Send leads:

```bash
python scripts/send_lead.py website-lead-emergency.json
python scripts/send_lead.py meta-lead-ads.json --times 3      # duplicate delivery
python scripts/send_lead.py invalid-lead-no-contact.json      # 400
python scripts/send_lead.py spam-lead.json
```

```
POST http://127.0.0.1:8000/webhooks/leads/meta  (meta-lead-ads.json, 3 deliveries)
  [1] HTTP 200  succeeded  rule=high_value_installation stage=hot_lead
  [2] HTTP 200  duplicate
  [3] HTTP 200  duplicate
```

Or with curl:

```bash
curl -X POST http://127.0.0.1:8000/webhooks/leads/website \
  -H "Content-Type: application/json" \
  -d @n8n/fixtures/website-lead-emergency.json
```

### Inspect what happened

```bash
curl http://127.0.0.1:8000/admin/events | jq
curl http://127.0.0.1:8000/admin/events/<event_id> | jq   # steps, timings, outbound calls
curl http://127.0.0.1:8000/admin/dead-letters | jq
curl http://127.0.0.1:8000/admin/stats | jq
```

`/admin/events/{id}` is the interesting one: every step with its duration, every outbound GoHighLevel and model call with attempts and latency, and the routing decision with the reason it fired. It turns *"the automation is broken"* into a diagnosis.

### Break it on purpose

```bash
AUTH='-H "Version: 2021-07-28" -H "Authorization: Bearer demo_token_value"'

# Two 500s, then recovery
curl -X POST http://127.0.0.1:8081/_mock/faults \
  -H "Version: 2021-07-28" -H "Authorization: Bearer demo_token_value" \
  -d '{"operation":"contacts.upsert","mode":"500","times":2}'
python scripts/send_lead.py website-lead-standard.json      # still 200

# A permanent 401 -> dead letter
curl -X POST http://127.0.0.1:8081/_mock/faults \
  -H "Version: 2021-07-28" -H "Authorization: Bearer demo_token_value" \
  -d '{"operation":"contacts.upsert","mode":"401","times":1}'
python scripts/send_lead.py partner-lead.json               # 422

# Recover it
curl -X POST http://127.0.0.1:8000/admin/dead-letters/1/replay
```

Fault modes: `500`, `503`, `429`, `timeout`, `401`, `422`, `empty_body`.

### Signature verification

```bash
export WEBHOOK_SIGNING_SECRET=$(python -c "import secrets; print('whsec_'+secrets.token_hex(32))")
export REQUIRE_SIGNATURE=true
uvicorn leadops.api.main:app --port 8000

python scripts/send_lead.py website-lead-standard.json            # 200, signature_verified: true
python scripts/send_lead.py website-lead-standard.json --tamper   # 401
```

`--tamper` signs a *different* body than it sends. It is the check worth running once after wiring a webhook up: if it returns 200, verification is not actually on.

---

## 3. In n8n

```bash
docker compose --profile n8n up      # n8n on :5678, service on :8000, mock on :8081
```

Then import the two workflows and follow [`n8n-workflow.md`](n8n-workflow.md#import).

> **Honest note:** `docker-compose.yml` was written but not executed — no Docker daemon was available in the environment where this was built. The Python entry points it invokes are the same ones CI runs on every push, so the risk is in the Compose wiring rather than the application. The non-Docker paths above are the verified ones.

Without Docker: run the service and the mock as in section 2, install n8n separately, and set `LEADOPS_BASE_URL=http://127.0.0.1:8000`.

---

## 4. Tests

```bash
pytest -q                                    # 218 tests, ~11 seconds, no network
pytest -q tests/unit                         # pure logic
pytest -q tests/integration                  # full pipeline against the mock GHL
pytest -q -k duplicate                       # just the idempotency cases
pytest -q -k "partial_failure or concurrent" # the two hardest guarantees
```

```bash
ruff check . && ruff format --check .        # lint
python scripts/scan_secrets.py               # secret scan
python scripts/build_workflow.py && git diff --exit-code -- n8n/workflows/   # workflow drift
```

Against PostgreSQL, as CI does:

```bash
docker run -d --name leadops-pg -e POSTGRES_USER=leadops -e POSTGRES_PASSWORD=pw \
  -e POSTGRES_DB=leadops -p 5432:5432 postgres:17
pip install -e ".[dev,postgres]"
LEADOPS_TEST_DATABASE_URL=postgresql+asyncpg://leadops:pw@localhost:5432/leadops pytest -q
```

---

## Connecting a real GoHighLevel account

```bash
export GHL_BASE_URL=https://services.leadconnectorhq.com
export GHL_ACCESS_TOKEN=<private integration token>
export GHL_LOCATION_ID=<sub-account id>

python scripts/discover_ghl_ids.py > config/ghl-mapping.json   # read-only
```

`discover_ghl_ids.py` reads the location's custom fields, pipelines, stages, calendars and users and prints a filled mapping file, reporting anything it could not match on stderr. It issues only GET requests.

Then read [`ghl-integration.md`](ghl-integration.md#first-contact-with-a-real-account) — eight things to verify on first contact with a live account, none of which should require code changes if they hold.

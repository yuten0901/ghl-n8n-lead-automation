# n8n runtime verification

This records the manual acceptance run that crosses the repository's real n8n
workflow, LeadOps service, database, and bundled GoHighLevel mock. It complements
the structural workflow checks in CI; it does not replace them.

## Verified path

Verified on **n8n 2.36.8 on 2026-08-31**:

```text
POST n8n production webhook
  -> validate and detect source
  -> call the LeadOps service
  -> normalize, qualify, and route
  -> write through the GHL v2 client
  -> return the n8n webhook response
```

The committed `01-lead-intake.json` was imported into an isolated local n8n
instance. Its placeholder Header Auth credential was mapped to a local dummy
credential after import; no credential value was added to the repository. The
LeadOps service used SQLite, the deterministic offline qualifier, and the
bundled GHL mock.

## Acceptance result

The production webhook received
`n8n/fixtures/website-lead-emergency.json` with a strong idempotency key.

| Check | Observed result |
|---|---|
| First n8n execution | `success` |
| First webhook response | `{"received":true,"outcome":"succeeded",...}` |
| Same payload and idempotency key sent again | `{"received":true,"outcome":"duplicate",...}` |
| CRM state after both deliveries | 1 contact, 1 opportunity, 1 note, 2 messages |
| GHL calls after first delivery | contact upsert 1, note 1, opportunity search 1, opportunity create 1, messages 2 |
| GHL calls added by duplicate delivery | **0** |

The unchanged object and call counts are the important assertion: n8n accepted
the redelivery, but the service did not repeat any CRM side effect.

## Boundary of this evidence

This run proves that the committed workflow can execute end to end in n8n and
coordinate the implemented service under local, repeatable conditions. This
evidence does **not** claim any of the following:

- a connection to a paid GoHighLevel sub-account;
- a live Anthropic or OpenAI call;
- the disabled appointment node;
- a production deployment or production lead volume;
- runtime execution inside CI.

The first live-account checks remain in
[`ghl-integration.md`](ghl-integration.md#first-contact-with-a-real-account).

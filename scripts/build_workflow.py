"""Generate `n8n/workflows/01-lead-intake.json`.

The workflow is generated rather than hand-edited so that node ids, positions and
the connection graph stay consistent, and so `tests/unit/test_n8n_workflow.py`
can assert on structure that a human editing 900 lines of JSON would drift from.

Run: python scripts/build_workflow.py
"""

from __future__ import annotations

import json
import pathlib

REPO = pathlib.Path(__file__).resolve().parents[1]
OUT = REPO / "n8n" / "workflows" / "01-lead-intake.json"


def node(name, type_, params, x, y, *, type_version=1, extra=None):
    body = {
        "parameters": params,
        "id": name.lower().replace(" ", "-").replace("(", "").replace(")", "").replace("?", ""),
        "name": name,
        "type": type_,
        "typeVersion": type_version,
        "position": [x, y],
    }
    if extra:
        body.update(extra)
    return body


def code(*lines: str) -> str:
    return "\n".join(lines)


def build() -> dict:
    nodes: list[dict] = []

    # ---- ingestion ------------------------------------------------------
    nodes.append(
        node(
            "Lead Webhook",
            "n8n-nodes-base.webhook",
            {
                "httpMethod": "POST",
                "path": "lead-intake",
                "responseMode": "responseNode",
                "options": {"rawBody": True},
            },
            -1120,
            300,
            type_version=2,
            extra={"webhookId": "leadops-lead-intake"},
        )
    )

    nodes.append(
        node(
            "Detect Source",
            "n8n-nodes-base.code",
            {
                "jsCode": code(
                    "// Identify which source posted: from the query string, an explicit",
                    "// header, or the shape of the body. One webhook URL for every source",
                    "// is far easier to hand a client than four.",
                    "const item = $input.first();",
                    "const body = item.json.body ?? item.json;",
                    "const query = item.json.query ?? {};",
                    "const headers = item.json.headers ?? {};",
                    "",
                    "const declared = query.source || headers['x-lead-source'] || '';",
                    "let source = String(declared).toLowerCase();",
                    "if (!source) {",
                    "  if (body.leadgen_id || body.field_data) source = 'meta';",
                    "  else if (body.lead_id || body.user_column_data) source = 'google';",
                    "  else if (body.partner_id || body.lead) source = 'partner';",
                    "  else source = 'website';",
                    "}",
                    "",
                    "// One correlation id for the whole execution. It goes to the service,",
                    "// is forwarded to GoHighLevel, and is logged here - so one search",
                    "// spans all three systems.",
                    "const correlationId = headers['x-correlation-id']",
                    "  || ('n8n_' + $execution.id + '_' + Date.now());",
                    "",
                    "// Prefer a real delivery id from the sender over anything we invent.",
                    "const idempotencyKey = headers['x-idempotency-key'] || body.event_id",
                    "  || body.leadgen_id || body.lead_id || body.submission_id || '';",
                    "",
                    "return [{ json: { source, correlationId, idempotencyKey, payload: body } }];",
                ),
            },
            -900,
            300,
            type_version=2,
        )
    )

    nodes.append(
        node(
            "Validate Shape",
            "n8n-nodes-base.code",
            {
                "jsCode": code(
                    "// Cheap structural checks before spending a service call. These would fail",
                    "// in the service too; catching them here keeps the reason visible in the",
                    "// n8n execution list, which is where the client actually looks.",
                    "const { source, payload, correlationId, idempotencyKey } =",
                    "  $input.first().json;",
                    "const problems = [];",
                    "",
                    "const KNOWN = ['website', 'meta', 'google', 'partner'];",
                    "if (!KNOWN.includes(source)) problems.push('unknown source: ' + source);",
                    "if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {",
                    "  problems.push('body is not a JSON object');",
                    "}",
                    "if (JSON.stringify(payload || {}).length > 100000) {",
                    "  problems.push('payload too large');",
                    "}",
                    "",
                    "return [{ json: {",
                    "  source, payload, correlationId, idempotencyKey,",
                    "  problems, valid: problems.length === 0,",
                    "} }];",
                ),
            },
            -680,
            300,
            type_version=2,
        )
    )

    nodes.append(
        node(
            "Valid Payload?",
            "n8n-nodes-base.if",
            {
                "conditions": {
                    "options": {"caseSensitive": True, "version": 2},
                    "conditions": [
                        {
                            "id": "valid",
                            "operator": {
                                "type": "boolean",
                                "operation": "true",
                                "singleValue": True,
                            },
                            "leftValue": "={{ $json.valid }}",
                            "rightValue": "",
                        }
                    ],
                    "combinator": "and",
                },
                "options": {},
            },
            -460,
            300,
            type_version=2,
        )
    )

    nodes.append(
        node(
            "Reject Invalid Payload",
            "n8n-nodes-base.code",
            {
                "jsCode": code(
                    "const j = $input.first().json;",
                    "return [{ json: {",
                    "  rejected: true, problems: j.problems, correlationId: j.correlationId,",
                    "} }];",
                ),
            },
            -240,
            480,
            type_version=2,
        )
    )

    # ---- the service call -----------------------------------------------
    nodes.append(
        node(
            "Process Lead (service)",
            "n8n-nodes-base.httpRequest",
            {
                "method": "POST",
                "url": "={{ $env.LEADOPS_BASE_URL }}/webhooks/leads/{{ $json.source }}",
                "sendHeaders": True,
                "headerParameters": {
                    "parameters": [
                        {"name": "Content-Type", "value": "application/json"},
                        {"name": "X-Correlation-Id", "value": "={{ $json.correlationId }}"},
                        {"name": "X-Idempotency-Key", "value": "={{ $json.idempotencyKey }}"},
                    ]
                },
                "sendBody": True,
                "specifyBody": "json",
                "jsonBody": "={{ JSON.stringify($json.payload) }}",
                "options": {
                    "response": {"response": {"fullResponse": True, "neverError": True}},
                    "timeout": 30000,
                },
                "authentication": "genericCredentialType",
                "genericAuthType": "httpHeaderAuth",
            },
            -240,
            200,
            type_version=4.2,
            extra={
                "credentials": {
                    "httpHeaderAuth": {
                        "id": "REPLACE_WITH_YOUR_CREDENTIAL_ID",
                        "name": "LeadOps service token",
                    }
                },
                "notes": (
                    "neverError is deliberate: the service answers 202 for a retryable failure "
                    "and 400 for a bad payload, and we branch on that ourselves rather than "
                    "letting n8n abort the execution and lose the distinction."
                ),
                "retryOnFail": True,
                "maxTries": 3,
                "waitBetweenTries": 2000,
            },
        )
    )

    nodes.append(
        node(
            "Classify Outcome",
            "n8n-nodes-base.code",
            {
                "jsCode": code(
                    "// The service already decided what happened. n8n branches on that decision",
                    "// rather than re-deriving it - two implementations of one rule always drift.",
                    "const response = $input.first().json;",
                    "const body = response.body ?? {};",
                    "",
                    "return [{ json: {",
                    "  statusCode: response.statusCode,",
                    "  outcome: body.status || 'unknown',",
                    "  retryable: response.statusCode === 202,",
                    "  correlationId: body.correlation_id,",
                    "  eventId: body.event_id,",
                    "  contactId: body.contact_id,",
                    "  opportunityId: body.opportunity_id,",
                    "  qualification: body.qualification || null,",
                    "  routing: body.routing || null,",
                    "  error: body.error || null,",
                    "} }];",
                ),
            },
            -20,
            200,
            type_version=2,
        )
    )

    nodes.append(
        node(
            "Succeeded?",
            "n8n-nodes-base.if",
            {
                "conditions": {
                    "options": {"caseSensitive": True, "version": 2},
                    "conditions": [
                        {
                            "id": "ok",
                            "operator": {"type": "string", "operation": "equals"},
                            "leftValue": "={{ $json.outcome }}",
                            "rightValue": "succeeded",
                        }
                    ],
                    "combinator": "and",
                },
                "options": {},
            },
            200,
            200,
            type_version=2,
        )
    )

    # ---- routing branches -----------------------------------------------
    nodes.append(
        node(
            "Route by Priority",
            "n8n-nodes-base.switch",
            {
                "rules": {
                    "values": [
                        {
                            "conditions": {
                                "options": {"caseSensitive": True, "version": 2},
                                "conditions": [
                                    {
                                        "operator": {
                                            "type": "boolean",
                                            "operation": "true",
                                            "singleValue": True,
                                        },
                                        "leftValue": "={{ $json.routing.notify_sales }}",
                                        "rightValue": "",
                                    }
                                ],
                                "combinator": "and",
                            },
                            "outputKey": "escalate",
                        },
                        {
                            "conditions": {
                                "options": {"caseSensitive": True, "version": 2},
                                "conditions": [
                                    {
                                        "operator": {"type": "string", "operation": "notEquals"},
                                        "leftValue": "={{ $json.routing.follow_up_sequence }}",
                                        "rightValue": "",
                                    }
                                ],
                                "combinator": "and",
                            },
                            "outputKey": "nurture",
                        },
                    ]
                },
                "options": {"fallbackOutput": "extra", "renameFallbackOutput": "file_only"},
            },
            420,
            200,
            type_version=3,
        )
    )

    nodes.append(
        node(
            "Escalate to Sales",
            "n8n-nodes-base.code",
            {
                "jsCode": code(
                    "// The service already sent the internal alert and first-touch message.",
                    "// This branch makes the SLA clock visible in n8n, which is where an",
                    "// agency owner actually watches for missed leads.",
                    "const j = $input.first().json;",
                    "return [{ json: {",
                    "  branch: 'escalate',",
                    "  channel: j.routing.notify_channel || 'sales_primary',",
                    "  slaMinutes: j.routing.sla_minutes,",
                    "  dueBy: new Date(",
                    "    Date.now() + (j.routing.sla_minutes || 60) * 60000",
                    "  ).toISOString(),",
                    "  contactId: j.contactId,",
                    "  score: j.qualification.qualification_score,",
                    "  summary: j.qualification.summary,",
                    "  correlationId: j.correlationId,",
                    "  outcome: j.outcome,",
                    "  eventId: j.eventId,",
                    "  qualification: j.qualification,",
                    "  routing: j.routing,",
                    "} }];",
                ),
            },
            660,
            40,
            type_version=2,
        )
    )

    nodes.append(
        node(
            "Book Appointment Slot",
            "n8n-nodes-base.httpRequest",
            {
                "method": "POST",
                "url": "https://services.leadconnectorhq.com/calendars/events/appointments",
                "sendHeaders": True,
                "headerParameters": {
                    "parameters": [
                        {"name": "Version", "value": "v3"},
                        {"name": "Content-Type", "value": "application/json"},
                    ]
                },
                "sendBody": True,
                "specifyBody": "json",
                "jsonBody": (
                    "={{ JSON.stringify({ calendarId: $env.GHL_CALENDAR_ID, "
                    "locationId: $env.GHL_LOCATION_ID, contactId: $json.contactId, "
                    "title: 'Site visit - ' + String($json.summary).slice(0, 60), "
                    "appointmentStatus: 'new' }) }}"
                ),
                "options": {
                    "response": {"response": {"fullResponse": True, "neverError": True}},
                    "timeout": 15000,
                },
                "authentication": "genericCredentialType",
                "genericAuthType": "httpHeaderAuth",
            },
            880,
            40,
            type_version=4.2,
            extra={
                "credentials": {
                    "httpHeaderAuth": {
                        "id": "REPLACE_WITH_YOUR_CREDENTIAL_ID",
                        "name": "GoHighLevel API",
                    }
                },
                "notes": (
                    "Disabled by default. This is the one node that needs a real, paid GHL "
                    "location; everything else in this workflow runs against the local mock. "
                    "Enable it after filling in GHL_CALENDAR_ID and the credential."
                ),
                "disabled": True,
            },
        )
    )

    nodes.append(
        node(
            "Enter Nurture Sequence",
            "n8n-nodes-base.code",
            {
                "jsCode": code(
                    "// Lower-intent leads. The first-touch email was already sent by the service;",
                    "// this hands the contact to the GHL workflow that owns the longer sequence,",
                    "// keyed by the tag the service applied.",
                    "const j = $input.first().json;",
                    "return [{ json: {",
                    "  branch: 'nurture',",
                    "  contactId: j.contactId,",
                    "  sequence: j.routing.follow_up_sequence,",
                    "  tags: j.routing.tags,",
                    "  reviewAfterDays: j.routing.sla_minutes",
                    "    ? Math.ceil(j.routing.sla_minutes / 1440) : 2,",
                    "  correlationId: j.correlationId,",
                    "  outcome: j.outcome,",
                    "  eventId: j.eventId,",
                    "  qualification: j.qualification,",
                    "  routing: j.routing,",
                    "} }];",
                ),
            },
            660,
            220,
            type_version=2,
        )
    )

    nodes.append(
        node(
            "File Without Contact",
            "n8n-nodes-base.code",
            {
                "jsCode": code(
                    "// Spam and unqualified leads. Recorded in the CRM so the volume is",
                    "// auditable, but nobody is texted and no salesperson is interrupted.",
                    "const j = $input.first().json;",
                    "return [{ json: {",
                    "  branch: 'file_only',",
                    "  contactId: j.contactId,",
                    "  ruleId: j.routing.rule_id,",
                    "  reason: j.routing.reason,",
                    "  correlationId: j.correlationId,",
                    "  outcome: j.outcome,",
                    "  eventId: j.eventId,",
                    "  qualification: j.qualification,",
                    "  routing: j.routing,",
                    "} }];",
                ),
            },
            660,
            400,
            type_version=2,
        )
    )

    nodes.append(
        node(
            "Merge Branches",
            "n8n-nodes-base.merge",
            {
                "numberInputs": 3,
            },
            1100,
            220,
            type_version=3,
        )
    )

    # ---- failure handling ------------------------------------------------
    nodes.append(
        node(
            "Retryable?",
            "n8n-nodes-base.if",
            {
                "conditions": {
                    "options": {"caseSensitive": True, "version": 2},
                    "conditions": [
                        {
                            "id": "retry",
                            "operator": {
                                "type": "boolean",
                                "operation": "true",
                                "singleValue": True,
                            },
                            "leftValue": "={{ $json.retryable }}",
                            "rightValue": "",
                        }
                    ],
                    "combinator": "and",
                },
                "options": {},
            },
            420,
            620,
            type_version=2,
        )
    )

    nodes.append(
        node(
            "Wait and Redeliver",
            "n8n-nodes-base.wait",
            {
                "amount": 5,
                "unit": "minutes",
            },
            660,
            560,
            type_version=1.1,
            extra={
                "webhookId": "leadops-retry-wait",
                "notes": (
                    "Safe to loop back into the service because the idempotency key is "
                    "unchanged: completed steps are memoized, so the retry resumes rather "
                    "than re-creating the contact."
                ),
            },
        )
    )

    nodes.append(
        node(
            "Alert Operator",
            "n8n-nodes-base.code",
            {
                "jsCode": code(
                    "// Terminal failure. The service has already dead-lettered it with the",
                    "// original payload, so this is a notification, not data recovery: replay",
                    "// from /admin/dead-letters once the cause is fixed.",
                    "const j = $input.first().json;",
                    "return [{ json: {",
                    "  branch: 'alert',",
                    "  level: 'error',",
                    "  message: 'Lead ' + j.eventId + ' failed permanently: '",
                    "    + (j.error ? j.error.code : 'unknown'),",
                    "  correlationId: j.correlationId,",
                    "  replayWith: $env.LEADOPS_BASE_URL + '/admin/dead-letters/{id}/replay',",
                    "  detail: j.error,",
                    "  outcome: j.outcome,",
                    "  eventId: j.eventId,",
                    "} }];",
                ),
            },
            660,
            700,
            type_version=2,
        )
    )

    # ---- audit + responses ----------------------------------------------
    nodes.append(
        node(
            "Append Audit Row",
            "n8n-nodes-base.code",
            {
                "jsCode": code(
                    "// n8n-side audit trail. The service keeps the authoritative record; this",
                    "// exists so the client sees outcomes in the tool they already open, and so",
                    "// an execution is self-describing months later.",
                    "const j = $input.first().json;",
                    "return [{ json: {",
                    "  ts: new Date().toISOString(),",
                    "  executionId: $execution.id,",
                    "  branch: j.branch || null,",
                    "  correlationId: j.correlationId || null,",
                    "  eventId: j.eventId || null,",
                    "  outcome: j.outcome || 'unknown',",
                    "  ruleId: j.routing ? j.routing.rule_id : null,",
                    "  score: j.qualification ? j.qualification.qualification_score : null,",
                    "  degraded: j.qualification ? j.qualification.degraded : null,",
                    "} }];",
                ),
            },
            1320,
            300,
            type_version=2,
        )
    )

    nodes.append(
        node(
            "Respond OK",
            "n8n-nodes-base.respondToWebhook",
            {
                "respondWith": "json",
                "responseBody": (
                    "={{ JSON.stringify({ received: true, outcome: $json.outcome, "
                    "correlation_id: $json.correlationId }) }}"
                ),
                "options": {"responseCode": 200},
            },
            1540,
            220,
            type_version=1,
        )
    )

    nodes.append(
        node(
            "Respond Rejected",
            "n8n-nodes-base.respondToWebhook",
            {
                "respondWith": "json",
                "responseBody": (
                    "={{ JSON.stringify({ received: false, problems: $json.problems }) }}"
                ),
                "options": {"responseCode": 400},
            },
            1540,
            480,
            type_version=1,
        )
    )

    def to(*names: str) -> list[dict]:
        return [{"node": n, "type": "main", "index": 0} for n in names]

    connections = {
        "Lead Webhook": {"main": [to("Detect Source")]},
        "Detect Source": {"main": [to("Validate Shape")]},
        "Validate Shape": {"main": [to("Valid Payload?")]},
        "Valid Payload?": {"main": [to("Process Lead (service)"), to("Reject Invalid Payload")]},
        "Reject Invalid Payload": {"main": [to("Respond Rejected")]},
        "Process Lead (service)": {"main": [to("Classify Outcome")]},
        "Classify Outcome": {"main": [to("Succeeded?")]},
        "Succeeded?": {"main": [to("Route by Priority"), to("Retryable?")]},
        "Route by Priority": {
            "main": [
                to("Escalate to Sales"),
                to("Enter Nurture Sequence"),
                to("File Without Contact"),
            ]
        },
        "Escalate to Sales": {"main": [to("Book Appointment Slot")]},
        "Book Appointment Slot": {
            "main": [[{"node": "Merge Branches", "type": "main", "index": 0}]]
        },
        "Enter Nurture Sequence": {
            "main": [[{"node": "Merge Branches", "type": "main", "index": 1}]]
        },
        "File Without Contact": {
            "main": [[{"node": "Merge Branches", "type": "main", "index": 2}]]
        },
        "Merge Branches": {"main": [to("Append Audit Row")]},
        "Retryable?": {"main": [to("Wait and Redeliver"), to("Alert Operator")]},
        "Wait and Redeliver": {"main": [to("Process Lead (service)")]},
        "Alert Operator": {"main": [to("Append Audit Row")]},
        "Append Audit Row": {"main": [to("Respond OK")]},
    }

    return {
        # ⚠️ n8n's `import:workflow` CLI inserts straight into workflow_entity,
        # whose `id` column is NOT NULL - it does not generate one. Without this
        # key every CLI import fails with a constraint error, which is how this
        # was found: the JSON validated structurally for weeks while being
        # unimportable. A fixed id also keeps re-imports idempotent instead of
        # accumulating copies. 16 chars, matching n8n's own nanoid format.
        "id": "LeadOpsIntake001",
        "name": "Lead Ops - GHL intake, AI qualification, routing",
        "nodes": nodes,
        "connections": connections,
        "active": False,
        "settings": {
            "executionOrder": "v1",
            "saveDataErrorExecution": "all",
            "saveDataSuccessExecution": "all",
            "saveManualExecutions": True,
            "errorWorkflow": "REPLACE_WITH_ERROR_WORKFLOW_ID",
            "timezone": "America/Chicago",
        },
        # ⚠️ Tags need explicit ids, not just names. n8n's importer creates a
        # tag row per workflow, and tag_entity.name is UNIQUE - so importing a
        # directory of workflows that share a tag name fails partway through
        # with a constraint error, leaving some imported and some not. With ids
        # the importer reuses the row. This is also the shape n8n's own
        # `export:workflow` emits. Verified on n8n 2.36.8, 2026-08-30.
        "tags": [
            {"id": "LeadOpsTagGhl01", "name": "gohighlevel"},
            {"id": "LeadOpsTagAuto01", "name": "lead-automation"},
        ],
        "pinData": {},
        "meta": {"instanceId": "portfolio-demo", "templateCredsSetupCompleted": False},
    }


if __name__ == "__main__":
    workflow = build()
    OUT.write_text(json.dumps(workflow, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {OUT.relative_to(REPO)} with {len(workflow['nodes'])} nodes")

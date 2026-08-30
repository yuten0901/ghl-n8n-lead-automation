"""Structural validation of the n8n workflow JSON.

A broken workflow file fails at import time, in the client's n8n, with a message
like "Cannot read properties of undefined". These tests catch the usual causes -
a connection naming a node that does not exist, an orphan node, a hard-coded
secret - before the file is ever shipped.

This is not a claim that the workflow *executes* correctly; that needs a running
n8n. It is a claim that it is importable and internally consistent, which is what
CI can honestly check. `docs/n8n-workflow.md` states the difference.
"""

from __future__ import annotations

import json
import re

import pytest

from tests.conftest import REPO_ROOT

WORKFLOW_DIR = REPO_ROOT / "n8n" / "workflows"
WORKFLOWS = sorted(WORKFLOW_DIR.glob("*.json"))
MAIN = WORKFLOW_DIR / "01-lead-intake.json"


def load(path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(params=WORKFLOWS, ids=lambda p: p.name)
def workflow(request) -> dict:
    return load(request.param)


@pytest.fixture
def main() -> dict:
    return load(MAIN)


class TestEveryWorkflowIsWellFormed:
    def test_there_is_at_least_one_workflow_and_it_parses(self) -> None:
        assert WORKFLOWS, "no workflow JSON found"

    def test_required_top_level_keys_are_present(self, workflow: dict) -> None:
        for key in ("name", "nodes", "connections", "settings"):
            assert key in workflow, f"missing top-level key: {key}"

    def test_every_workflow_carries_an_id(self, workflow: dict) -> None:
        # ⚠️ This is the check that was missing. `n8n import:workflow` writes
        # straight into workflow_entity, whose `id` column is NOT NULL and is
        # not generated for you, so a file without an id cannot be imported by
        # the CLI at all - it fails with
        #   SQLITE_CONSTRAINT: NOT NULL constraint failed: workflow_entity.id
        # Verified against a real n8n 2.36.8 instance on 2026-08-30: both files
        # failed to import before this, and both imported after.
        #
        # Everything else in this class checks the workflow's internal shape,
        # which is why the gap survived: the graph was consistent, the nodes
        # were valid, and the file was still unimportable.
        wf_id = workflow.get("id")
        assert wf_id, "workflow has no top-level id; n8n import:workflow will reject it"
        assert isinstance(wf_id, str)
        assert wf_id.isalnum(), f"id should be alphanumeric like n8n's nanoid, got {wf_id!r}"

    def test_workflow_ids_are_unique_across_files(self) -> None:
        # A shared id makes the second import overwrite the first rather than
        # fail, which is the quieter and worse outcome.
        ids = [load(p).get("id") for p in WORKFLOWS]
        assert len(ids) == len(set(ids)), f"duplicate workflow ids: {ids}"

    def test_node_names_are_unique(self, workflow: dict) -> None:
        # n8n keys connections by node *name*, so a duplicate name silently
        # redirects edges to the wrong node.
        names = [n["name"] for n in workflow["nodes"]]
        assert len(names) == len(set(names))

    def test_node_ids_are_unique(self, workflow: dict) -> None:
        ids = [n["id"] for n in workflow["nodes"]]
        assert len(ids) == len(set(ids))

    def test_every_node_has_the_fields_n8n_requires(self, workflow: dict) -> None:
        for node in workflow["nodes"]:
            for key in ("parameters", "id", "name", "type", "typeVersion", "position"):
                assert key in node, f"{node.get('name')} missing {key}"
            assert node["type"].startswith("n8n-nodes-base."), node["type"]
            assert len(node["position"]) == 2

    def test_every_connection_points_at_a_node_that_exists(self, workflow: dict) -> None:
        """The most common cause of a broken import: a node was renamed and one
        edge still refers to the old name."""
        names = {n["name"] for n in workflow["nodes"]}
        for source, outputs in workflow["connections"].items():
            assert source in names, f"connection from unknown node: {source}"
            for branch in outputs.get("main", []):
                for edge in branch:
                    assert edge["node"] in names, f"{source} -> unknown node {edge['node']}"

    def test_no_node_is_orphaned(self, workflow: dict) -> None:
        """Every node is either a trigger or reachable from one. An unreachable
        node is dead weight that a reviewer has to reason about for nothing."""
        names = {n["name"] for n in workflow["nodes"]}
        reachable = {
            n["name"]
            for n in workflow["nodes"]
            if "trigger" in n["type"].lower() or n["type"].endswith(".webhook")
        }
        assert reachable, "workflow has no trigger node"

        changed = True
        while changed:
            changed = False
            for source, outputs in workflow["connections"].items():
                if source not in reachable:
                    continue
                for branch in outputs.get("main", []):
                    for edge in branch:
                        if edge["node"] not in reachable:
                            reachable.add(edge["node"])
                            changed = True

        assert names - reachable == set(), f"unreachable nodes: {sorted(names - reachable)}"

    def test_exactly_one_trigger(self, workflow: dict) -> None:
        triggers = [
            n
            for n in workflow["nodes"]
            if n["type"].endswith(".webhook") or n["type"].endswith(".errorTrigger")
        ]
        assert len(triggers) == 1


class TestNoSecretsInWorkflowFiles:
    """Workflow JSON is the easiest place to leak a key: it is exported from a
    working instance, where the credential was real."""

    LEAKS = [
        (re.compile(r"sk-[A-Za-z0-9]{20,}"), "OpenAI-style API key"),
        (re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"), "Anthropic API key"),
        (re.compile(r"eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}"), "JWT"),
        (re.compile(r"whsec_[A-Za-z0-9]{20,}"), "webhook signing secret"),
        (re.compile(r"pit-[0-9a-f-]{30,}"), "GHL private integration token"),
        (re.compile(r"Bearer\s+[A-Za-z0-9._-]{25,}"), "hard-coded bearer token"),
    ]

    def test_no_credential_shaped_strings(self, workflow: dict) -> None:
        raw = json.dumps(workflow)
        for pattern, description in self.LEAKS:
            assert not pattern.search(raw), f"possible {description} in workflow JSON"

    def test_credentials_are_referenced_not_embedded(self, workflow: dict) -> None:
        """n8n credentials belong in the credential store. The exported file
        should carry a reference and a placeholder id, never a value."""
        for node in workflow["nodes"]:
            for credential in (node.get("credentials") or {}).values():
                assert set(credential) <= {"id", "name"}, credential
                assert "REPLACE_WITH" in credential["id"], (
                    f"{node['name']} ships a real-looking credential id"
                )

    def test_secrets_come_from_environment_expressions(self, main: dict) -> None:
        raw = json.dumps(main)
        assert "$env.LEADOPS_BASE_URL" in raw
        assert "$env.GHL_LOCATION_ID" in raw
        # A literal localhost URL in a shipped workflow is a deploy-time surprise.
        assert "127.0.0.1" not in raw
        assert "localhost" not in raw


class TestMainWorkflowShape:
    def test_it_is_substantial_without_being_padded(self, main: dict) -> None:
        assert 15 <= len(main["nodes"]) <= 30

    def test_the_required_capabilities_are_all_present(self, main: dict) -> None:
        """Each entry is a capability an Upwork brief actually asks for.
        Checking by name keeps the README's feature list honest."""
        names = {n["name"] for n in main["nodes"]}
        required = {
            "Lead Webhook",  # webhook ingestion
            "Detect Source",  # multi-source normalization
            "Validate Shape",  # input validation
            "Valid Payload?",  # conditional rejection
            "Process Lead (service)",  # idempotent CRM sync + AI qualification
            "Classify Outcome",  # structured outcome handling
            "Route by Priority",  # conditional routing
            "Escalate to Sales",  # internal notification branch
            "Book Appointment Slot",  # appointment booking
            "Enter Nurture Sequence",  # follow-up branch
            "Retryable?",  # failure classification
            "Wait and Redeliver",  # retry with delay
            "Alert Operator",  # dead-letter alerting
            "Append Audit Row",  # audit logging
            "Respond OK",  # final response
        }
        assert required <= names, f"missing: {sorted(required - names)}"

    def test_the_service_call_does_not_abort_on_non_2xx(self, main: dict) -> None:
        """`neverError` is load-bearing: without it a 202 (retryable) and a 400
        (permanent) both become the same n8n exception, and the branch that
        tells them apart never runs."""
        node = next(n for n in main["nodes"] if n["name"] == "Process Lead (service)")
        response = node["parameters"]["options"]["response"]["response"]
        assert response["neverError"] is True
        assert response["fullResponse"] is True

    def test_the_service_call_has_a_timeout(self, main: dict) -> None:
        node = next(n for n in main["nodes"] if n["name"] == "Process Lead (service)")
        assert node["parameters"]["options"]["timeout"] > 0

    def test_the_retry_loop_returns_to_the_service_call(self, main: dict) -> None:
        """Safe only because the idempotency key is unchanged on the way round."""
        edges = main["connections"]["Wait and Redeliver"]["main"][0]
        assert [e["node"] for e in edges] == ["Process Lead (service)"]

    def test_both_webhook_responses_are_reachable(self, main: dict) -> None:
        responders = {n["name"] for n in main["nodes"] if n["type"].endswith(".respondToWebhook")}
        assert responders == {"Respond OK", "Respond Rejected"}
        targeted = {
            edge["node"]
            for outputs in main["connections"].values()
            for branch in outputs.get("main", [])
            for edge in branch
        }
        assert responders <= targeted

    def test_an_error_workflow_is_configured(self, main: dict) -> None:
        assert main["settings"]["errorWorkflow"]
        assert main["settings"]["saveDataErrorExecution"] == "all"

    def test_nodes_needing_paid_credentials_are_disabled_by_default(self, main: dict) -> None:
        """A reviewer importing this must not hit a wall of red nodes. The one
        node that genuinely needs a paid GHL location ships disabled and says so."""
        node = next(n for n in main["nodes"] if n["name"] == "Book Appointment Slot")
        assert node.get("disabled") is True
        assert "GHL" in node.get("notes", "")

    def test_the_workflow_ships_inactive(self, main: dict) -> None:
        # Importing a portfolio workflow should never start processing traffic.
        assert main["active"] is False


class TestGeneratorMatchesCommittedFile:
    def test_regenerating_produces_the_committed_json(self) -> None:
        """The workflow is generated by `scripts/build_workflow.py`. If someone
        hand-edits the JSON, this fails and points them at the generator - which
        is what stops the two from silently diverging."""
        import sys

        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        from build_workflow import build  # noqa: PLC0415

        assert build() == load(MAIN)

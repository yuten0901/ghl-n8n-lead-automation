"""本番への副作用を避けて公式HighLevel Sandboxを検証する。"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from leadops.errors import UpstreamRejected, ValidationFailed
from leadops.ghl.client import GHLClient


def fingerprint(value: str) -> str:
    """公開証跡で実IDを出さず、同一性だけ確認できる指紋を返す。"""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def authorize_sandbox_write(*, sandbox_guard: str, confirmation: str, location_id: str) -> None:
    """誤って本番Locationへ書き込まないための二重確認。"""
    if sandbox_guard.strip().lower() != "true":
        raise ValidationFailed("Set GHL_SANDBOX=true before requesting a write verification.")
    if not confirmation or confirmation != location_id:
        raise ValidationFailed("--confirm-sandbox-location must exactly match GHL_LOCATION_ID.")


def _pipelines(body: dict[str, Any]) -> list[dict[str, Any]]:
    values = body.get("pipelines") or []
    return [value for value in values if isinstance(value, dict)]


def _find_pipeline(
    body: dict[str, Any], *, pipeline_id: str, stage_id: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    pipeline = next((item for item in _pipelines(body) if item.get("id") == pipeline_id), None)
    if pipeline is None:
        raise ValidationFailed("The confirmed pipeline was not returned by this Location.")
    stages = [item for item in (pipeline.get("stages") or []) if isinstance(item, dict)]
    stage = next((item for item in stages if item.get("id") == stage_id), None)
    if stage is None:
        raise ValidationFailed("The confirmed stage was not returned by this pipeline.")
    return pipeline, stage


@dataclass(slots=True)
class SandboxEvidence:
    schema_version: int
    verified_at: str
    mode: str
    base_url: str
    api_version: str
    location_fingerprint: str
    checks: dict[str, bool]
    inventory_counts: dict[str, int]
    record_fingerprints: dict[str, str]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


async def read_inventory(client: GHLClient) -> dict[str, dict[str, Any]]:
    """認証と主要スコープを、データ変更なしで確認する。"""
    location_id = client.location_id
    fields = await client.request(
        "GET", f"/locations/{location_id}/customFields", params={"model": "contact"}
    )
    pipelines = await client.request(
        "GET", "/opportunities/pipelines", params={"locationId": location_id}
    )
    calendars = await _optional_read(client, "/calendars/", params={"locationId": location_id})
    users = await _optional_read(client, "/users/", params={"locationId": location_id})
    return {
        "custom_fields": fields.body,
        "pipelines": pipelines.body,
        "calendars": calendars,
        "users": users,
    }


async def _optional_read(client: GHLClient, path: str, *, params: dict[str, str]) -> dict[str, Any]:
    """Private Integrationで未付与の任意スコープを記録し、主要検証は継続する。"""
    try:
        return (await client.request("GET", path, params=params)).body
    except UpstreamRejected as exc:
        status = (exc.detail or {}).get("status_code")
        # HighLevelはPrivate Integrationの未付与スコープに401を返すことがある。
        # 認証自体は直前の必須エンドポイントで確認済みなので、ここでは権限差として扱う。
        if status in {401, 403}:
            return {"_scope_available": False}
        raise


async def verify_sandbox(
    client: GHLClient,
    *,
    write_test: bool = False,
    sandbox_guard: str = "",
    confirmation: str = "",
    pipeline_id: str = "",
    stage_id: str = "",
    marker: str | None = None,
) -> SandboxEvidence:
    """読取専用の事前確認、または明示承認済みテストデータ書込を実行する。"""
    inventory = await read_inventory(client)
    counts = {
        "custom_fields": len(inventory["custom_fields"].get("customFields") or []),
        "pipelines": len(_pipelines(inventory["pipelines"])),
        "calendars": len(inventory["calendars"].get("calendars") or []),
        "users": len(inventory["users"].get("users") or []),
    }
    checks = {
        "authentication_and_read_scopes": True,
        "optional_calendar_scope": inventory["calendars"].get("_scope_available") is not False,
        "optional_user_scope": inventory["users"].get("_scope_available") is not False,
        "pipeline_and_stage_belong_to_location": False,
        "contact_upsert_is_idempotent": False,
        "opportunity_is_reused": False,
        "no_message_or_payment_endpoint_called": True,
    }
    records: dict[str, str] = {}

    if write_test:
        authorize_sandbox_write(
            sandbox_guard=sandbox_guard,
            confirmation=confirmation,
            location_id=client.location_id,
        )
        if not pipeline_id or not stage_id:
            raise ValidationFailed(
                "A pipeline ID and stage ID are required for write verification."
            )
        _find_pipeline(inventory["pipelines"], pipeline_id=pipeline_id, stage_id=stage_id)
        checks["pipeline_and_stage_belong_to_location"] = True

        run_marker = marker or uuid4().hex[:16]
        contact_payload = {
            "firstName": "LeadOps",
            "lastName": "Sandbox Verification",
            "name": "LeadOps Sandbox Verification",
            "email": f"leadops.sandbox+{run_marker}@example.com",
            "source": "leadops:sandbox-verification",
            "tags": ["leadops-sandbox-proof"],
            "createNewIfDuplicateAllowed": False,
        }
        first = await client.upsert_contact(contact_payload)
        second = await client.upsert_contact(contact_payload)
        first_id = _contact_id(first.body)
        second_id = _contact_id(second.body)
        if not first_id or first_id != second_id:
            raise ValidationFailed("Repeated contact upsert did not return one stable contact ID.")
        checks["contact_upsert_is_idempotent"] = True
        records["contact"] = fingerprint(first_id)

        found = await client.search_opportunities(
            contact_id=first_id, pipeline_id=pipeline_id, status="open"
        )
        opportunities = found.body.get("opportunities") or []
        if opportunities:
            opportunity_id = str(opportunities[0].get("id") or "")
        else:
            created = await client.create_opportunity(
                {
                    "pipelineId": pipeline_id,
                    "pipelineStageId": stage_id,
                    "contactId": first_id,
                    "name": f"LeadOps Sandbox Verification {run_marker}",
                    "status": "open",
                    "monetaryValue": 0,
                }
            )
            opportunity_id = _opportunity_id(created.body)
        if not opportunity_id:
            raise ValidationFailed("The opportunity create/search response had no ID.")

        if not await _wait_for_opportunity_reuse(
            client,
            contact_id=first_id,
            pipeline_id=pipeline_id,
            opportunity_id=opportunity_id,
        ):
            raise ValidationFailed("Exactly one open opportunity was not reused.")
        checks["opportunity_is_reused"] = True
        records["opportunity"] = fingerprint(opportunity_id)

    return SandboxEvidence(
        schema_version=1,
        verified_at=datetime.now(UTC).isoformat(),
        mode="write-test" if write_test else "read-only",
        base_url=client.base_url,
        api_version=client.api_version,
        location_fingerprint=fingerprint(client.location_id),
        checks=checks,
        inventory_counts=counts,
        record_fingerprints=records,
    )


def _contact_id(body: dict[str, Any]) -> str:
    contact = body.get("contact") if isinstance(body.get("contact"), dict) else body
    return str(contact.get("id") or contact.get("contactId") or "")


def _opportunity_id(body: dict[str, Any]) -> str:
    opportunity = body.get("opportunity") if isinstance(body.get("opportunity"), dict) else body
    return str(opportunity.get("id") or opportunity.get("opportunityId") or "")


async def _wait_for_opportunity_reuse(
    client: GHLClient,
    *,
    contact_id: str,
    pipeline_id: str,
    opportunity_id: str,
) -> bool:
    """作成直後の検索インデックス反映を待ち、同じ案件が1件だけ見えることを確認する。"""
    for delay_seconds in (0.0, 0.5, 1.0, 2.0, 4.0):
        if delay_seconds:
            await asyncio.sleep(delay_seconds)
        response = await client.search_opportunities(
            contact_id=contact_id,
            pipeline_id=pipeline_id,
            status="open",
        )
        matching = response.body.get("opportunities") or []
        if len(matching) == 1 and str(matching[0].get("id") or "") == opportunity_id:
            return True
    return False

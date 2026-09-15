"""公式HighLevel Sandboxを安全に検証し、公開可能な証跡を生成する。"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import sys

from dotenv import load_dotenv

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from leadops.errors import LeadOpsError  # noqa: E402
from leadops.ghl.client import GHLClient  # noqa: E402
from leadops.ghl.sandbox import verify_sandbox  # noqa: E402
from leadops.reliability.retry import RetryPolicy  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parents[1]

# Sandbox検証では、このリポジトリのGit管理外 .env を正とする。
# 親シェルに残った古いGHLトークンで別アカウントへ接続しないよう上書きする。
load_dotenv(REPO / ".env", override=True)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Verify current HighLevel API behavior in an official Sandbox."
    )
    result.add_argument(
        "--write-test",
        action="store_true",
        help="Create test-only contact/opportunity records after double confirmation.",
    )
    result.add_argument(
        "--confirm-sandbox-location",
        default="",
        help="Must exactly equal GHL_LOCATION_ID when --write-test is used.",
    )
    result.add_argument("--pipeline-id", default=os.environ.get("GHL_PIPELINE_ID", ""))
    result.add_argument("--stage-id", default=os.environ.get("GHL_PIPELINE_STAGE_ID", ""))
    result.add_argument(
        "--output",
        type=pathlib.Path,
        default=REPO / "tmp" / "ghl-sandbox-evidence.json",
    )
    return result


async def run(args: argparse.Namespace) -> int:
    token = os.environ.get("GHL_ACCESS_TOKEN", "").strip()
    location_id = os.environ.get("GHL_LOCATION_ID", "").strip()
    if not token or not location_id:
        print("GHL_ACCESS_TOKEN and GHL_LOCATION_ID are required.", file=sys.stderr)
        return 2

    client = GHLClient(
        base_url=os.environ.get("GHL_BASE_URL", "https://services.leadconnectorhq.com"),
        access_token=token,
        location_id=location_id,
        api_version=os.environ.get("GHL_API_VERSION", "v3"),
        timeout_seconds=20,
        policy=RetryPolicy(max_attempts=4, base_seconds=0.5, max_seconds=8),
    )
    try:
        evidence = await verify_sandbox(
            client,
            write_test=args.write_test,
            sandbox_guard=os.environ.get("GHL_SANDBOX", ""),
            confirmation=args.confirm_sandbox_location,
            pipeline_id=args.pipeline_id,
            stage_id=args.stage_id,
        )
    except LeadOpsError as exc:
        print(f"Verification failed safely: {exc}", file=sys.stderr)
        return 1
    finally:
        await client.aclose()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(evidence.as_dict(), indent=2) + "\n", encoding="utf-8")
    print(json.dumps(evidence.as_dict(), indent=2))
    print(f"\nSanitized evidence written to {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run(parser().parse_args())))

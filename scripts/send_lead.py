"""Post a lead fixture to a running service, correctly signed.

    python scripts/send_lead.py website-lead-emergency.json
    python scripts/send_lead.py meta-lead-ads.json --times 3     # duplicate delivery
    python scripts/send_lead.py website-lead-standard.json --tamper

`--tamper` sends a valid signature for a *different* body, which is the check a
client should run once after wiring the webhook up: if it returns 200, signature
verification is not actually on.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import httpx  # noqa: E402

from leadops.api.security import sign  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parents[1]
FIXTURES = REPO / "n8n" / "fixtures"


def main() -> int:
    parser = argparse.ArgumentParser(description="Send a lead fixture to the service.")
    parser.add_argument("fixture", help=f"file name inside {FIXTURES.relative_to(REPO)}")
    parser.add_argument(
        "--url", default=os.environ.get("LEADOPS_BASE_URL", "http://127.0.0.1:8000")
    )
    parser.add_argument("--source", default="", help="defaults to a guess from the file name")
    parser.add_argument("--times", type=int, default=1, help="deliver N times (duplicate test)")
    parser.add_argument("--idempotency-key", default="")
    parser.add_argument("--tamper", action="store_true", help="sign a different body than we send")
    args = parser.parse_args()

    path = FIXTURES / args.fixture
    if not path.exists():
        available = "\n  ".join(sorted(p.name for p in FIXTURES.glob("*.json")))
        print(f"No such fixture: {args.fixture}\n\nAvailable:\n  {available}")
        return 2

    payload = json.loads(path.read_text(encoding="utf-8"))
    source = args.source or next(
        (s for s in ("meta", "google", "partner") if s in path.name), "website"
    )
    body = json.dumps(payload).encode()

    headers = {"Content-Type": "application/json"}
    secret = os.environ.get("WEBHOOK_SIGNING_SECRET", "")
    if secret:
        signed_over = b'{"different":"body"}' if args.tamper else body
        headers["X-Signature"] = sign(signed_over, secret)
    elif args.tamper:
        print("WEBHOOK_SIGNING_SECRET is not set, so --tamper proves nothing. Set it first.")
        return 2
    if args.idempotency_key:
        headers["X-Idempotency-Key"] = args.idempotency_key

    url = f"{args.url.rstrip('/')}/webhooks/leads/{source}"
    print(f"POST {url}  ({path.name}, {args.times} delivery/deliveries)")

    with httpx.Client(timeout=60.0) as client:
        for attempt in range(1, args.times + 1):
            response = client.post(url, content=body, headers=headers)
            data = response.json()
            summary = data.get("status", data.get("error", {}).get("code", "?"))
            extra = ""
            if data.get("routing"):
                routing = data["routing"]
                extra = f"  rule={routing['rule_id']} stage={routing['pipeline_stage']}"
            elif data.get("error"):
                error = data["error"]
                extra = f"  error={error['code']} retryable={error.get('retryable')}"
            print(f"  [{attempt}] HTTP {response.status_code}  {summary}{extra}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

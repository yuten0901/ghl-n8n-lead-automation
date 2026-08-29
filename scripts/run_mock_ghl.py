"""Run the mock GoHighLevel API on a real port.

Used when you want to drive the system over HTTP - from n8n, from curl, or from
`scripts/send_lead.py` - rather than in-process as the tests do.

    python scripts/run_mock_ghl.py            # http://127.0.0.1:8081

Then point the service at it (this is already the default in `.env.example`):

    GHL_BASE_URL=http://127.0.0.1:8081

Control plane, for demonstrating failure handling by hand:

    curl -X POST http://127.0.0.1:8081/_mock/faults \\
      -H "Version: 2021-07-28" -H "Authorization: Bearer demo_token_value" \\
      -d '{"operation":"contacts.upsert","mode":"500","times":2}'

    curl http://127.0.0.1:8081/_mock/state \
      -H "Version: 2021-07-28" -H "Authorization: Bearer demo_token_value"
"""

from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import uvicorn  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the mock GoHighLevel v2 API.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8081)
    args = parser.parse_args()

    print(f"Mock GoHighLevel v2 API on http://{args.host}:{args.port}")
    print("  This is NOT GoHighLevel. It implements the documented v2 request and")
    print("  response shapes so the demo runs without a paid account. See")
    print("  docs/ghl-integration.md for what must be re-verified against a real one.")
    uvicorn.run("mock.ghl_mock.server:app", host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

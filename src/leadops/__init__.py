"""Lead operations automation: webhook ingestion -> normalization -> idempotency ->
AI qualification -> GoHighLevel CRM sync -> routing -> follow-up.

The n8n workflow in `n8n/workflows/` orchestrates and makes the flow visible;
this package owns the correctness-critical logic so it can be unit tested.
"""

__version__ = "1.0.0"

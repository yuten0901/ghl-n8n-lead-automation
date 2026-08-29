# Single image, two entry points (the service and the GHL mock), because they
# share every dependency and building two near-identical images buys nothing.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependency layer first, so source edits do not invalidate the pip cache.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir -e ".[postgres]"

COPY mock ./mock
COPY config ./config
COPY scripts ./scripts
COPY n8n ./n8n

# Run unprivileged. Nothing here needs root, and least privilege is cheap.
RUN useradd --create-home --uid 10001 leadops && chown -R leadops:leadops /app
USER leadops

EXPOSE 8000
CMD ["uvicorn", "leadops.api.main:app", "--host", "0.0.0.0", "--port", "8000"]

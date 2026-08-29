"""Model provider abstraction.

One interface, three implementations:

* `DeterministicProvider` - keyword rules, no network. The default.
* `AnthropicProvider`     - Claude via the Messages API, structured output
                            enforced with a tool schema.
* `OpenAIProvider`        - Chat Completions with `response_format` json_schema.

Both live providers are written against the documented request shapes and are
selected by configuration. Neither was executed against a paid account while
building this repository; the tests drive them through a stubbed transport, which
verifies our request construction and our response handling but not the vendors'
behaviour. `docs/ai-qualification.md` says exactly that.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol

import httpx

from leadops.ai.schema import (
    QUALIFICATION_JSON_SCHEMA,
    SYSTEM_PROMPT,
)
from leadops.errors import UpstreamRejected, UpstreamTimeout, UpstreamUnavailable


@dataclass(slots=True)
class ModelReply:
    """Raw text from the model plus what it cost us. Parsing happens in
    `qualify.py`, so a provider is never responsible for validation."""

    text: str
    provider: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0


class Provider(Protocol):
    name: str

    async def complete(
        self, *, system: str, user: str, repair: str | None = None
    ) -> ModelReply: ...


class DeterministicProvider:
    """No network. `qualify.py` short-circuits to the rules engine when this is
    selected, so `complete()` is never the path that produces a result."""

    name = "deterministic"

    async def complete(self, *, system: str, user: str, repair: str | None = None) -> ModelReply:
        raise NotImplementedError(
            "DeterministicProvider does not call a model; qualify() uses rules.classify()."
        )


@dataclass
class AnthropicProvider:
    """Claude Messages API.

    Structured output is enforced by declaring a single tool whose input schema is
    the qualification schema and forcing its use. That is stricter than asking for
    JSON in the prompt: the API rejects a response that does not fit the schema,
    so most malformed-output cases never reach our validator at all.
    """

    api_key: str
    model: str = "claude-sonnet-5"
    timeout_seconds: float = 20.0
    temperature: float = 0.0
    base_url: str = "https://api.anthropic.com"
    api_version: str = "2023-06-01"
    max_tokens: int = 1024
    transport: httpx.AsyncBaseTransport | None = None
    name: str = "anthropic"

    async def complete(self, *, system: str, user: str, repair: str | None = None) -> ModelReply:
        messages = [{"role": "user", "content": user}]
        if repair:
            # Repair turn: the model sees its own bad output and the validator error.
            messages.append({"role": "assistant", "content": repair.split("\n\n")[0][:2000]})
            messages.append({"role": "user", "content": repair})

        body = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "system": system or SYSTEM_PROMPT,
            "messages": messages,
            "tools": [
                {
                    "name": "record_qualification",
                    "description": "Record the structured qualification for this lead.",
                    "input_schema": QUALIFICATION_JSON_SCHEMA,
                }
            ],
            "tool_choice": {"type": "tool", "name": "record_qualification"},
        }
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": self.api_version,
            "content-type": "application/json",
        }

        async with httpx.AsyncClient(
            base_url=self.base_url, timeout=self.timeout_seconds, transport=self.transport
        ) as client:
            try:
                response = await client.post("/v1/messages", json=body, headers=headers)
            except httpx.TimeoutException as exc:
                raise UpstreamTimeout("Anthropic request timed out") from exc
            except httpx.HTTPError as exc:
                raise UpstreamUnavailable(f"Anthropic unreachable: {type(exc).__name__}") from exc

        _raise_for_status(response, "Anthropic")
        payload = response.json()

        text = ""
        for block in payload.get("content", []):
            if block.get("type") == "tool_use":
                text = json.dumps(block.get("input", {}))
                break
            if block.get("type") == "text" and not text:
                text = block.get("text", "")

        usage = payload.get("usage") or {}
        return ModelReply(
            text=text,
            provider=self.name,
            model=payload.get("model", self.model),
            input_tokens=int(usage.get("input_tokens", 0)),
            output_tokens=int(usage.get("output_tokens", 0)),
        )


@dataclass
class OpenAIProvider:
    """OpenAI-compatible Chat Completions with a json_schema response format.

    Written for compatibility rather than for one vendor: any endpoint that
    accepts this body (OpenAI, Azure OpenAI, or a self-hosted gateway) works by
    changing `base_url`, which is what clients on a fixed vendor actually need.
    """

    api_key: str
    model: str = "gpt-4o-mini"
    timeout_seconds: float = 20.0
    temperature: float = 0.0
    base_url: str = "https://api.openai.com/v1"
    transport: httpx.AsyncBaseTransport | None = None
    name: str = "openai"

    async def complete(self, *, system: str, user: str, repair: str | None = None) -> ModelReply:
        messages = [
            {"role": "system", "content": system or SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ]
        if repair:
            messages.append({"role": "user", "content": repair})

        body = {
            "model": self.model,
            "temperature": self.temperature,
            "messages": messages,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "lead_qualification",
                    "strict": True,
                    "schema": QUALIFICATION_JSON_SCHEMA,
                },
            },
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(
            base_url=self.base_url, timeout=self.timeout_seconds, transport=self.transport
        ) as client:
            try:
                response = await client.post("/chat/completions", json=body, headers=headers)
            except httpx.TimeoutException as exc:
                raise UpstreamTimeout("OpenAI request timed out") from exc
            except httpx.HTTPError as exc:
                raise UpstreamUnavailable(f"OpenAI unreachable: {type(exc).__name__}") from exc

        _raise_for_status(response, "OpenAI")
        payload = response.json()
        choices = payload.get("choices") or [{}]
        text = (choices[0].get("message") or {}).get("content") or ""
        usage = payload.get("usage") or {}
        return ModelReply(
            text=text,
            provider=self.name,
            model=payload.get("model", self.model),
            input_tokens=int(usage.get("prompt_tokens", 0)),
            output_tokens=int(usage.get("completion_tokens", 0)),
        )


def _raise_for_status(response: httpx.Response, vendor: str) -> None:
    if response.status_code < 400:
        return
    detail = {"status_code": response.status_code, "body": response.text[:500]}
    if response.status_code == 429 or response.status_code >= 500:
        raise UpstreamUnavailable(f"{vendor} returned {response.status_code}", detail=detail)
    raise UpstreamRejected(f"{vendor} rejected the request ({response.status_code})", detail=detail)


def build_provider(
    *,
    provider: str,
    api_key: str,
    model: str,
    timeout_seconds: float,
    temperature: float,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Provider:
    """Select a provider from configuration.

    A live provider with no key falls back to deterministic rather than failing
    at startup. A half-configured deployment should degrade visibly, not refuse
    to accept leads - the leads are the business.
    """
    if provider == "anthropic" and api_key:
        return AnthropicProvider(
            api_key=api_key,
            model=model,
            timeout_seconds=timeout_seconds,
            temperature=temperature,
            transport=transport,
        )
    if provider == "openai" and api_key:
        return OpenAIProvider(
            api_key=api_key,
            model=model,
            timeout_seconds=timeout_seconds,
            temperature=temperature,
            transport=transport,
        )
    return DeterministicProvider()

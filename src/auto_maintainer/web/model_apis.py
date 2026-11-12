# Copyright 2026 The RLinf Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Asking a model API about itself, for the provider forms on the Settings page.

``list_models`` reads the endpoint's model list with its key, spending no tokens, and
keeps what the endpoint says of each model: how much it reads and writes and whether
it reasons. Few endpoints say all of it (OpenRouter and Google's Gemini API say most;
OpenAI and Anthropic only the ids). ``examine`` learns the rest by talking to the
model: a two-turn conversation on each API the address might serve, then a tool call,
an image, a reasoning request, and a streamed answer on the one the agents will use.
Both follow no redirects and never echo the endpoint's body back beyond a short,
redacted reason.
"""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import json
import struct
import zlib
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, Field

from ..domain.secrets import redact_secrets
from ..domain.settings import ModelSpec, Provider, ProviderProtocol

TIMEOUT = 20.0
DEFAULT_REASONING = ["low", "medium", "high"]
"""The levels offered for a model the endpoint says reasons, when it names none."""
MAX_MODELS = 500


class ApiForm(BaseModel):
    """A model API as the provider forms send it: a ``Provider`` and, write-only, its
    key's value."""

    name: str | None = Field(default=None, max_length=80)
    protocol: ProviderProtocol
    base_url: str = Field(min_length=1, max_length=500)
    models: list[ModelSpec] = Field(default_factory=list, max_length=MAX_MODELS)
    api_key: str | None = None
    """The service's secret that holds the key (service providers only)."""
    key: str | None = Field(default=None, max_length=4096)
    """The key itself, stored apart and never sent back."""

    def provider(self) -> Provider:
        """The provider this form describes.

        Raises:
            ValueError: A field does not check, said in a sentence.
        """
        try:
            return Provider(
                name=(self.name or "").strip() or None,
                protocol=self.protocol,
                base_url=self.base_url,
                api_key=self.api_key or None,
                models=_unique(self.models),
            )
        except ValueError as e:
            errors = getattr(e, "errors", lambda: [])()
            raise ValueError(
                "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in errors)
                or str(e)
            ) from e


class ProbeBody(BaseModel):
    """What a form asks of an endpoint before (or after) it is saved."""

    protocol: ProviderProtocol
    base_url: str = Field(min_length=1, max_length=500)
    key: str | None = Field(default=None, max_length=4096)
    """A key typed into the form; else the stored key of ``provider``."""
    provider: str | None = Field(default=None, max_length=64)
    model: str | None = Field(default=None, max_length=200)


def _unique(models: list[ModelSpec]) -> list[ModelSpec]:
    seen: set[str] = set()
    out = []
    for m in models:
        if m.id not in seen:
            seen.add(m.id)
            out.append(m)
    return out


async def refused_address(base_url: str) -> str | None:
    """Why the host should not send a person's request to ``base_url``: it is not an
    http(s) URL, or its host is the service's own host (loopback), a link-local
    address (such as a cloud's metadata service), or no usable address. Private
    networks stay allowed, since lab machines serve models there."""

    parts = urlsplit(base_url.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return "the base URL must look like https://host/v1"
    return await refused_host(
        parts.hostname,
        parts.port or (443 if parts.scheme == "https" else 80),
        "personal model APIs",
    )


async def refused_host(host: str, port: int, what: str) -> str | None:
    """Why the host should not connect to ``host`` for a person (``what`` they set
    up): it is the service's own host, a link-local address, or does not resolve."""
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, port)
    except OSError:
        return f"{host} has no address the host can resolve"
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%", 1)[0])
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast:
            return (
                f"{host} is this host or a link-local address, which {what} may not "
                "reach"
            )
    return None


def _anthropic_base(base: str) -> str:
    return base if base.endswith("/v1") else f"{base}/v1"


def _google_base(base: str) -> str:
    return base if base.endswith(("/v1beta", "/v1")) else f"{base}/v1beta"


def _headers(protocol: str, key: str | None) -> dict[str, str]:
    headers = {"Accept": "application/json", "User-Agent": "auto-maintainer"}
    if not key:
        return headers
    if protocol == "anthropic":
        headers |= {"x-api-key": key, "anthropic-version": "2023-06-01"}
        headers["Authorization"] = f"Bearer {key}"
    elif protocol == "google":
        headers["x-goog-api-key"] = key
    else:
        headers["Authorization"] = f"Bearer {key}"
    return headers


def describe_error(status: int | None, error: Exception | None, body: str = "") -> str:
    """Why a request to a model API failed, in a person's terms, with at most a short
    reason from the endpoint."""
    reason = ""
    if body:
        reason = redact_secrets(body.strip().replace("\n", " "))[0][:200]
    if error is not None:
        return "could not reach the endpoint; check the base URL and the host's network"
    if status in (401, 403):
        return "the endpoint rejected the key" + (f": {reason}" if reason else "")
    if status == 404:
        return "nothing at this URL; check the base URL (many end in /v1)" + (
            f": {reason}" if reason else ""
        )
    if status == 429:
        return "the endpoint is rate limiting this key"
    if status is not None and status >= 500:
        return f"the endpoint failed ({status})"
    return f"unexpected answer ({status})" + (f": {reason}" if reason else "")


def _int(value: Any) -> int | None:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _openai_model(m: dict[str, Any]) -> dict[str, Any]:
    """An OpenAI-style list entry, with what OpenRouter, vLLM, and others add."""
    top = m.get("top_provider") if isinstance(m.get("top_provider"), dict) else {}
    params = m.get("supported_parameters") or []
    reasons = "reasoning" in params or "include_reasoning" in params
    return {
        "id": str(m["id"]),
        "context_window": _int(
            m.get("context_length") or m.get("context_window") or m.get("max_model_len")
        ),
        "max_output": _int(
            top.get("max_completion_tokens") or m.get("max_output_tokens")
        ),
        "reasoning": DEFAULT_REASONING if reasons else [],
    }


def _anthropic_model(m: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(m["id"]),
        "context_window": _int(m.get("max_input_tokens")),
        "max_output": _int(m.get("max_tokens")),
        "reasoning": [],
    }


def _google_model(m: dict[str, Any]) -> dict[str, Any] | None:
    methods = m.get("supportedGenerationMethods") or []
    if methods and "generateContent" not in methods:
        return None
    return {
        "id": str(m.get("name", "")).removeprefix("models/"),
        "context_window": _int(m.get("inputTokenLimit")),
        "max_output": _int(m.get("outputTokenLimit")),
        "reasoning": DEFAULT_REASONING if m.get("thinking") else [],
    }


async def list_models(
    protocol: str,
    base_url: str,
    key: str | None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """The endpoint's models, each with what it says of them; spends no tokens.

    Returns ``{"ok", "models": [{id, context_window, max_output, reasoning}], "error"}``.
    """
    base = base_url.rstrip("/")
    if protocol == "anthropic":
        url = f"{_anthropic_base(base)}/models?limit=1000"
    elif protocol == "google":
        url = f"{_google_base(base)}/models?pageSize=1000"
    else:
        url = f"{base}/models"
    try:
        async with httpx.AsyncClient(
            timeout=TIMEOUT, follow_redirects=False, transport=transport
        ) as client:
            res = await client.get(url, headers=_headers(protocol, key))
    except httpx.HTTPError as e:
        return {"ok": False, "error": describe_error(None, e), "models": []}
    if res.status_code != 200:
        return {
            "ok": False,
            "error": describe_error(res.status_code, None, res.text),
            "status": res.status_code,
            "models": [],
        }
    try:
        data = res.json()
        rows = data.get("models" if protocol == "google" else "data") or []
        models: list[dict[str, Any]] = []
        for m in rows:
            if not isinstance(m, dict):
                continue
            if protocol == "google":
                found = _google_model(m)
            elif "id" not in m:
                continue
            elif protocol == "anthropic":
                found = _anthropic_model(m)
            else:
                found = _openai_model(m)
            if found and found["id"]:
                models.append(found)
    except (ValueError, AttributeError, TypeError):
        return {"ok": False, "error": "the answer was not a model list", "models": []}
    models.sort(key=lambda m: m["id"])
    return {"ok": True, "models": models[:MAX_MODELS], "status": 200}


def _reply(protocol: str, data: dict[str, Any]) -> str:
    if protocol == "anthropic":
        return "".join(
            c.get("text", "") for c in data.get("content", []) if isinstance(c, dict)
        ).strip()
    if protocol == "google":
        parts = data["candidates"][0].get("content", {}).get("parts", [])
        return "".join(p.get("text", "") for p in parts if isinstance(p, dict)).strip()
    if protocol == "openai-responses":
        if isinstance(data.get("output_text"), str):
            return data["output_text"].strip()
        texts = [
            c.get("text", "")
            for item in data.get("output", [])
            if isinstance(item, dict)
            for c in item.get("content", []) or []
            if isinstance(c, dict)
        ]
        return "".join(texts).strip()
    message = data["choices"][0].get("message") or {}
    return str(message.get("content") or message.get("reasoning_content") or "").strip()


# -- examining a model ------------------------------------------------------------

WORD = "ORCHID-41"
SUM = (1234, 4321)
TOOL = {
    "name": "add_numbers",
    "description": "Add two integers and return their sum.",
    "parameters": {
        "type": "object",
        "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
        "required": ["a", "b"],
    },
}


def _red_png(size: int = 16) -> str:
    """A small red square as base64 PNG, for the image check."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    raw = b"".join(b"\x00" + b"\xff\x00\x00" * size for _ in range(size))
    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )
    return base64.b64encode(png).decode()


class _Api:
    """One protocol: how to send turns, tools, an image, and a reasoning request at
    ``base`` for ``model``, and how to read the answers."""

    def __init__(self, protocol: str, base: str, key: str | None, model: str) -> None:
        self.protocol, self.key, self.model = protocol, key, model
        base = base.rstrip("/")
        if protocol == "anthropic":
            self.url = f"{_anthropic_base(base)}/messages"
        elif protocol == "google":
            self.url = f"{_google_base(base)}/models/{model}:generateContent"
            self.stream_url = (
                f"{_google_base(base)}/models/{model}:streamGenerateContent?alt=sse"
            )
        elif protocol == "openai-responses":
            self.url = f"{base}/responses"
        else:
            self.url = f"{base}/chat/completions"
        self.headers = _headers(protocol, key)
        self.completion_tokens = False
        """Whether this OpenAI endpoint wants ``max_completion_tokens`` (its reasoning
        models refuse ``max_tokens``); learned from the first refusal."""

    # requests ---------------------------------------------------------------
    def body(self, turns: list[tuple[str, Any]], **extra: Any) -> dict[str, Any]:
        """A request for ``turns`` (role, content), content as this API writes it."""
        limit = int(extra.pop("limit", 1024))
        if self.protocol == "google":
            contents = [
                {"role": "model" if r == "assistant" else r, "parts": c}
                if isinstance(c, list)
                else {
                    "role": "model" if r == "assistant" else r,
                    "parts": [{"text": c}],
                }
                for r, c in turns
            ]
            config = {"maxOutputTokens": limit, **extra.pop("config", {})}
            return {"contents": contents, "generationConfig": config, **extra}
        messages = [{"role": r, "content": c} for r, c in turns]
        if self.protocol == "anthropic":
            return {
                "model": self.model,
                "max_tokens": limit,
                "messages": messages,
                **extra,
            }
        if self.protocol == "openai-responses":
            return {
                "model": self.model,
                "input": messages,
                "max_output_tokens": limit,
                **extra,
            }
        cap = "max_completion_tokens" if self.completion_tokens else "max_tokens"
        return {"model": self.model, "messages": messages, cap: limit, **extra}

    def image(self, question: str) -> Any:
        data = _red_png()
        if self.protocol == "anthropic":
            return [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": data,
                    },
                },
                {"type": "text", "text": question},
            ]
        if self.protocol == "google":
            return [
                {"inlineData": {"mimeType": "image/png", "data": data}},
                {"text": question},
            ]
        if self.protocol == "openai-responses":
            return [
                {"type": "input_text", "text": question},
                {"type": "input_image", "image_url": f"data:image/png;base64,{data}"},
            ]
        return [
            {"type": "text", "text": question},
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{data}"},
            },
        ]

    def tools(self) -> dict[str, Any]:
        if self.protocol == "anthropic":
            return {
                "tools": [
                    {
                        "name": TOOL["name"],
                        "description": TOOL["description"],
                        "input_schema": TOOL["parameters"],
                    }
                ]
            }
        if self.protocol == "google":
            return {"tools": [{"functionDeclarations": [TOOL]}]}
        if self.protocol == "openai-responses":
            return {"tools": [{"type": "function", **TOOL}]}
        return {"tools": [{"type": "function", "function": TOOL}]}

    def reasoning(self) -> dict[str, Any]:
        if self.protocol == "anthropic":
            return {
                "thinking": {"type": "enabled", "budget_tokens": 1024},
                "limit": 4096,
            }
        if self.protocol == "google":
            return {
                "config": {
                    "thinkingConfig": {"thinkingBudget": 1024, "includeThoughts": True}
                },
                "limit": 4096,
            }
        if self.protocol == "openai-responses":
            return {"reasoning": {"effort": "low"}, "limit": 4096}
        return {"reasoning_effort": "low", "limit": 4096}

    async def send(
        self, client: httpx.AsyncClient, body: dict[str, Any]
    ) -> tuple[int, Any, str]:
        res = await client.post(self.url, headers=self.headers, json=body)
        if (
            res.status_code == 400
            and self.protocol == "openai"
            and "max_completion_tokens" in res.text
            and "max_tokens" in body
        ):
            self.completion_tokens = True
            body = dict(body)
            body["max_completion_tokens"] = body.pop("max_tokens")
            res = await client.post(self.url, headers=self.headers, json=body)
        if (
            res.status_code == 400
            and self.protocol == "anthropic"
            and "budget_tokens" in json.dumps(body.get("thinking") or {})
        ):
            body = {**body, "thinking": {"type": "adaptive"}}
            res = await client.post(self.url, headers=self.headers, json=body)
        try:
            data = res.json()
        except ValueError:
            data = None
        return res.status_code, data, res.text

    # answers ----------------------------------------------------------------
    def text(self, data: Any) -> str:
        if self.protocol == "google":
            parts = data["candidates"][0].get("content", {}).get("parts", [])
            return "".join(
                p.get("text", "")
                for p in parts
                if isinstance(p, dict) and not p.get("thought")
            ).strip()
        return _reply(self.protocol, data)

    def call(self, data: Any) -> tuple[str, dict[str, Any], Any] | None:
        """The tool call in an answer: (name, arguments, what to send back with it)."""
        if self.protocol == "anthropic":
            for b in data.get("content", []):
                if b.get("type") == "tool_use":
                    return b.get("name", ""), b.get("input") or {}, b
        elif self.protocol == "google":
            parts = data["candidates"][0].get("content", {}).get("parts", [])
            for p in parts:
                if "functionCall" in p:
                    fc = p["functionCall"]
                    return fc.get("name", ""), fc.get("args") or {}, parts
        elif self.protocol == "openai-responses":
            output = data.get("output", [])
            for item in output:
                if item.get("type") == "function_call":
                    item_args = _args(item.get("arguments"))
                    return item.get("name", ""), item_args, (output, item)
        else:
            calls = (data["choices"][0].get("message") or {}).get("tool_calls") or []
            if calls:
                fn = calls[0].get("function") or {}
                return (
                    fn.get("name", ""),
                    _args(fn.get("arguments")),
                    data["choices"][0]["message"],
                )
        return None

    def answer_call(
        self, turns: list[tuple[str, Any]], call: Any, result: str
    ) -> dict[str, Any]:
        """The follow-up request that hands the tool's result back, with the whole
        answer that made the call (reasoning items and thought signatures included,
        which some models require)."""
        if self.protocol == "anthropic":
            return self.body(
                [
                    *turns,
                    ("assistant", [call]),
                    (
                        "user",
                        [
                            {
                                "type": "tool_result",
                                "tool_use_id": call.get("id"),
                                "content": result,
                            }
                        ],
                    ),
                ],
                **self.tools(),
            )
        if self.protocol == "google":
            body = self.body(turns, **self.tools())
            body["contents"] += [
                {"role": "model", "parts": call},
                {
                    "role": "user",
                    "parts": [
                        {
                            "functionResponse": {
                                "name": TOOL["name"],
                                "response": {"result": result},
                            }
                        }
                    ],
                },
            ]
            return body
        if self.protocol == "openai-responses":
            output, item = call
            body = self.body(turns, **self.tools())
            body["input"] += [
                *output,
                {
                    "type": "function_call_output",
                    "call_id": item.get("call_id"),
                    "output": result,
                },
            ]
            return body
        body = self.body(turns, **self.tools())
        tool_call = (call.get("tool_calls") or [{}])[0]
        body["messages"] += [
            {
                "role": "assistant",
                "content": call.get("content"),
                "tool_calls": call.get("tool_calls"),
            },
            {"role": "tool", "tool_call_id": tool_call.get("id"), "content": result},
        ]
        return body

    def reasoned(self, data: Any) -> bool:
        if self.protocol == "anthropic":
            return any(
                b.get("type") in ("thinking", "redacted_thinking")
                for b in data.get("content", [])
            )
        if self.protocol == "google":
            parts = data["candidates"][0].get("content", {}).get("parts", [])
            used = (data.get("usageMetadata") or {}).get("thoughtsTokenCount") or 0
            return used > 0 or any(
                p.get("thought") for p in parts if isinstance(p, dict)
            )
        usage = data.get("usage") or {}
        if self.protocol == "openai-responses":
            used = (usage.get("output_tokens_details") or {}).get(
                "reasoning_tokens"
            ) or 0
            return used > 0 or any(
                i.get("type") == "reasoning" for i in data.get("output", [])
            )
        used = (usage.get("completion_tokens_details") or {}).get(
            "reasoning_tokens"
        ) or 0
        message = data["choices"][0].get("message") or {}
        return used > 0 or bool(
            message.get("reasoning_content") or message.get("reasoning")
        )

    async def stream(self, client: httpx.AsyncClient, body: dict[str, Any]) -> str:
        """The text of a streamed answer, read from its server-sent events."""
        url = self.stream_url if self.protocol == "google" else self.url
        if self.protocol != "google":
            body = {**body, "stream": True}
        text: list[str] = []
        async with client.stream("POST", url, headers=self.headers, json=body) as res:
            if res.status_code != 200:
                await res.aread()
                raise _Refused(res.status_code, res.text)
            async for line in res.aiter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if not payload or payload == "[DONE]":
                    continue
                try:
                    event = json.loads(payload)
                except ValueError:
                    continue
                text.append(self._delta(event))
        return "".join(text).strip()

    def _delta(self, event: dict[str, Any]) -> str:
        if self.protocol == "anthropic":
            delta = event.get("delta") or {}
            return (
                str(delta.get("text") or "")
                if event.get("type") == "content_block_delta"
                else ""
            )
        if self.protocol == "google":
            try:
                return self.text(event)
            except (KeyError, IndexError, TypeError):
                return ""
        if self.protocol == "openai-responses":
            return (
                str(event.get("delta") or "")
                if event.get("type") == "response.output_text.delta"
                else ""
            )
        choices = event.get("choices") or [{}]
        return str((choices[0].get("delta") or {}).get("content") or "")


class _Refused(Exception):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(status)
        self.status, self.body = status, body


def _args(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        out = json.loads(value or "{}")
    except ValueError:
        return {}
    return out if isinstance(out, dict) else {}


def _check(name: str, ok: bool | None, detail: str) -> dict[str, Any]:
    return {"name": name, "ok": ok, "detail": detail}


async def _conversation(api: _Api, client: httpx.AsyncClient) -> dict[str, Any]:
    first = [("user", f"Remember the code word {WORD}. Reply with OK only.")]
    status, data, raw = await api.send(client, api.body(first))
    if status != 200 or data is None:
        return _check("conversation", False, describe_error(status, None, raw))
    said = api.text(data)
    turns = [
        *first,
        ("assistant", said or "OK"),
        ("user", "What was the code word? Reply with it only."),
    ]
    status, data, raw = await api.send(client, api.body(turns))
    if status != 200 or data is None:
        return _check("conversation", False, describe_error(status, None, raw))
    recalled = api.text(data)
    if WORD.lower() not in recalled.lower().replace(" ", ""):
        return _check(
            "conversation",
            False,
            f"it answered but did not recall the earlier turn: {recalled[:80]!r}",
        )
    return _check(
        "conversation",
        True,
        f"two turns; it recalled the code word ({recalled[:40]!r})",
    )


async def _tools(api: _Api, client: httpx.AsyncClient) -> dict[str, Any]:
    a, b = SUM
    turns = [
        ("user", f"Use the add_numbers tool to add {a} and {b}, then tell me the sum.")
    ]
    status, data, raw = await api.send(
        client, api.body(turns, limit=256, **api.tools())
    )
    if status != 200 or data is None:
        return _check("tools", False, describe_error(status, None, raw))
    call = api.call(data)
    if call is None or call[0] != TOOL["name"]:
        return _check("tools", False, "it answered without calling the tool")
    name, args, raw_call = call
    if {int(args.get("a", 0) or 0), int(args.get("b", 0) or 0)} != {a, b}:
        return _check(
            "tools", False, f"it called the tool with the wrong arguments: {args}"
        )
    status, data, raw = await api.send(
        client, api.answer_call(turns, raw_call, str(a + b))
    )
    if status != 200 or data is None:
        return _check(
            "tools",
            False,
            "it called the tool but refused its result: "
            + describe_error(status, None, raw),
        )
    if str(a + b) not in api.text(data):
        return _check("tools", False, "it called the tool but did not use its result")
    return _check(
        "tools", True, "it called a tool with the right arguments and used the result"
    )


async def _images(api: _Api, client: httpx.AsyncClient) -> dict[str, Any]:
    turns = [("user", api.image("What colour is this image? Reply with one word."))]
    status, data, raw = await api.send(client, api.body(turns))
    if status != 200 or data is None:
        return _check("images", False, describe_error(status, None, raw))
    said = api.text(data)
    if "red" not in said.lower():
        return _check(
            "images", False, f"it answered but did not see the image: {said[:60]!r}"
        )
    return _check("images", True, "it named the colour of an image")


async def _reasoning(api: _Api, client: httpx.AsyncClient) -> dict[str, Any]:
    turns = [
        ("user", "Is 2027 a prime number? Think it through, then answer yes or no.")
    ]
    status, data, raw = await api.send(client, api.body(turns, **api.reasoning()))
    if status != 200 or data is None:
        return _check("reasoning", False, describe_error(status, None, raw))
    if not api.reasoned(data):
        return _check(
            "reasoning", False, "it took the reasoning setting but showed no reasoning"
        )
    return _check("reasoning", True, "it reasoned before answering")


async def _streaming(api: _Api, client: httpx.AsyncClient) -> dict[str, Any]:
    try:
        said = await api.stream(
            client, api.body([("user", "Count from 1 to 5, separated by spaces.")])
        )
    except _Refused as e:
        return _check("streaming", False, describe_error(e.status, None, e.body))
    if "3" not in said:
        return _check("streaming", False, "the stream carried no answer")
    return _check("streaming", True, "it streamed its answer")


async def examine(
    base_url: str,
    key: str | None,
    model: str,
    protocol: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """Hold real conversations with ``model`` to learn what it can do: which APIs
    answer at ``base_url`` (``protocol``, and the other OpenAI and Anthropic APIs at
    the same address), and, on its own protocol's API (else the first that answered),
    whether it calls tools, reads images, reasons, and streams. Spends a few thousand
    tokens at most.

    Returns ``{"spec": ModelSpec fields, "checks": [{name, ok, detail}], "ok"}``.
    """
    tried = (
        ["google"]
        if protocol == "google"
        else ["openai", "openai-responses", "anthropic"]
    )
    if protocol not in tried:
        tried.insert(0, protocol)
    async with httpx.AsyncClient(
        timeout=90, follow_redirects=False, transport=transport
    ) as client:
        apis = {p: _Api(p, base_url, key, model) for p in tried}

        async def talk(p: str) -> dict[str, Any]:
            try:
                return await _conversation(apis[p], client)
            except httpx.HTTPError as e:
                return _check("conversation", False, describe_error(None, e))
            except (KeyError, IndexError, TypeError, AttributeError):
                return _check(
                    "conversation", False, "the answer was not a model's reply"
                )

        talks = dict(
            zip(tried, await asyncio.gather(*(talk(p) for p in tried)), strict=True)
        )
        working = [p for p in tried if talks[p]["ok"]]
        checks = [{**c, "name": f"conversation ({p})"} for p, c in talks.items()]
        spec: dict[str, Any] = {
            "id": model,
            "interfaces": working,
            "tested_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        if not working:
            return {"ok": False, "spec": spec, "checks": checks}
        # The conversation's own: it learned the endpoint's token field.
        api = apis[protocol if protocol in working else working[0]]

        async def run(check: Any) -> dict[str, Any]:
            try:
                return await check(api, client)
            except httpx.HTTPError as e:
                return _check(check.__name__.strip("_"), False, describe_error(None, e))
            except (KeyError, IndexError, TypeError, AttributeError, ValueError):
                return _check(
                    check.__name__.strip("_"),
                    False,
                    "the answer was not in the shape this API uses",
                )

        results = await asyncio.gather(
            *(run(c) for c in (_tools, _images, _reasoning, _streaming))
        )
    found = {r["name"]: r["ok"] for r in results}
    spec |= {
        "tools": found["tools"],
        "images": found["images"],
        "streaming": found["streaming"],
        "reasoning": DEFAULT_REASONING if found["reasoning"] else [],
    }
    return {
        "ok": True,
        "spec": spec,
        "checks": [
            *checks,
            *(dict(r, name=f"{r['name']} ({api.protocol})") for r in results),
        ],
    }

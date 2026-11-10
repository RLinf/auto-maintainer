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

"""Asking a model API about itself: its models with what it says of them, per
protocol, and real conversations that learn what a model can do."""

from __future__ import annotations

import json
from typing import Any

import httpx
import respx

from auto_maintainer.domain.settings import ModelSpec, Provider, agents_for
from auto_maintainer.web.model_apis import examine, list_models, refused_address

KEY = "sk-" + "t" * 30


async def test_openrouter_style_lists_say_context_output_and_reasoning() -> None:
    with respx.mock(base_url="https://openrouter.ai/api/v1") as mock:
        route = mock.get("/models").respond(
            200,
            json={
                "data": [
                    {
                        "id": "deepseek/deepseek-r1",
                        "context_length": 163840,
                        "top_provider": {"max_completion_tokens": 32768},
                        "supported_parameters": ["reasoning", "tools"],
                    },
                    {"id": "plain"},
                ]
            },
        )
        out = await list_models("openai", "https://openrouter.ai/api/v1/", KEY)
    assert route.calls[0].request.headers["authorization"] == f"Bearer {KEY}"
    assert out["ok"] and out["models"] == [
        {
            "id": "deepseek/deepseek-r1",
            "context_window": 163840,
            "max_output": 32768,
            "reasoning": ["low", "medium", "high"],
        },
        {"id": "plain", "context_window": None, "max_output": None, "reasoning": []},
    ]


async def test_gemini_lists_its_limits_and_thinking_models_only_that_generate() -> None:
    with respx.mock(base_url="https://generativelanguage.googleapis.com") as mock:
        route = mock.get("/v1beta/models").respond(
            200,
            json={
                "models": [
                    {
                        "name": "models/gemini-2.5-pro",
                        "inputTokenLimit": 1048576,
                        "outputTokenLimit": 65536,
                        "thinking": True,
                        "supportedGenerationMethods": ["generateContent"],
                    },
                    {
                        "name": "models/embed",
                        "supportedGenerationMethods": ["embedContent"],
                    },
                ]
            },
        )
        out = await list_models(
            "google", "https://generativelanguage.googleapis.com", KEY
        )
    assert route.calls[0].request.headers["x-goog-api-key"] == KEY
    assert [m["id"] for m in out["models"]] == ["gemini-2.5-pro"]
    assert out["models"][0]["reasoning"] == ["low", "medium", "high"]


async def test_a_rejected_key_says_so_without_the_key() -> None:
    with respx.mock(base_url="https://api.anthropic.com") as mock:
        mock.get("/v1/models").respond(401, text=f"bad key {KEY}")
        out = await list_models("anthropic", "https://api.anthropic.com", KEY)
    assert (
        not out["ok"] and "rejected the key" in out["error"] and KEY not in out["error"]
    )


def _gateway(*, remembers: bool = True) -> httpx.MockTransport:
    """An OpenAI-compatible server with one model that sees images, calls tools,
    reasons, and streams; it serves neither the Responses nor the Anthropic API."""

    def reply(content: str, **extra: Any) -> httpx.Response:
        message = {"role": "assistant", "content": content, **extra}
        return httpx.Response(
            200, json={"choices": [{"message": message}], **extra.pop("top", {})}
        )

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path != "/v1/chat/completions":
            return httpx.Response(404, json={"error": "no such route"})
        assert request.headers["authorization"] == f"Bearer {KEY}"
        body = json.loads(request.content)
        if "max_tokens" in body:
            return httpx.Response(
                400, text="Unsupported parameter: use max_completion_tokens instead"
            )
        messages = body["messages"]
        text = json.dumps(messages)
        if body.get("stream"):
            chunks = [
                {"choices": [{"delta": {"content": w}}]} for w in ("1 2 ", "3 4 5")
            ]
            sse = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks)
            return httpx.Response(
                200,
                text=sse + "data: [DONE]\n\n",
                headers={"content-type": "text/event-stream; charset=utf-8"},
            )
        if "tools" in body:
            if messages[-1]["role"] == "tool":
                return reply(f"The sum is {messages[-1]['content']}.")
            call = {
                "id": "c1",
                "type": "function",
                "function": {
                    "name": "add_numbers",
                    "arguments": '{"a": 1234, "b": 4321}',
                },
            }
            return reply("", tool_calls=[call])
        if "reasoning_effort" in body:
            usage = {"completion_tokens_details": {"reasoning_tokens": 40}}
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "yes"}}], "usage": usage},
            )
        if "image_url" in text:
            return reply("Red")
        if "What was the code word" in text:
            return reply(
                "ORCHID-41" if remembers and "ORCHID-41" in text else "I don't know"
            )
        return reply("OK")

    return httpx.MockTransport(handle)


async def test_examining_a_model_learns_what_it_can_do_by_talking_to_it() -> None:
    out = await examine(
        "https://gw.example/v1", KEY, "m1", "openai", transport=_gateway()
    )
    assert out["ok"]
    spec = ModelSpec.model_validate(out["spec"])
    assert spec.interfaces == ["openai"], "only Chat Completions answered"
    assert (spec.tools, spec.images, spec.streaming) == (True, True, True)
    assert spec.reasoning == ["low", "medium", "high"] and spec.tested_at
    checks = {c["name"]: c["ok"] for c in out["checks"]}
    assert checks["conversation (openai-responses)"] is False
    assert checks["conversation (anthropic)"] is False
    assert all(
        checks[f"{n} (openai)"] for n in ("tools", "images", "reasoning", "streaming")
    )
    assert agents_for(spec, "openai") == ["dsh", "opencode", "pi"]


async def test_a_model_that_forgets_the_last_turn_fails_the_conversation() -> None:
    out = await examine(
        "https://gw.example/v1",
        KEY,
        "m1",
        "openai",
        transport=_gateway(remembers=False),
    )
    assert not out["ok"] and out["spec"]["interfaces"] == []
    [chat] = [c for c in out["checks"] if c["name"] == "conversation (openai)"]
    assert "did not recall" in chat["detail"]


async def test_a_persons_probe_may_not_reach_the_host_itself() -> None:
    assert await refused_address("http://127.0.0.1:8000/v1")
    assert await refused_address("http://169.254.169.254/latest")
    assert await refused_address("ftp://gw.example")
    assert await refused_address("http://10.0.0.5:8000/v1") is None, (
        "lab machines serve models on private networks"
    )


def test_a_settings_file_may_name_models_by_id_or_describe_them() -> None:
    p = Provider.model_validate(
        {
            "protocol": "google",
            "base_url": "https://generativelanguage.googleapis.com",
            "models": [
                "gemini-2.5-flash",
                {
                    "id": "gemini-2.5-pro",
                    "context_window": 1048576,
                    "reasoning": ["high"],
                },
            ],
        }
    )
    assert p.model_ids == ["gemini-2.5-flash", "gemini-2.5-pro"]
    assert p.model("gemini-2.5-pro") == ModelSpec(
        id="gemini-2.5-pro", context_window=1048576, reasoning=["high"]
    )

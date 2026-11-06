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

"""Service settings: checking a document, who may apply it, keys that never come back
out, configuring through the agent, and how providers reach each CLI."""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from auto_maintainer.agents.adapters.claude_code import ClaudeCodeRuntime
from auto_maintainer.agents.adapters.codex import CodexRuntime
from auto_maintainer.agents.adapters.opencode import OpenCodeRuntime
from auto_maintainer.agents.registry import build_runtime
from auto_maintainer.agents.sandbox import scrub
from auto_maintainer.domain.agents import AgentTask, TaskKind
from auto_maintainer.domain.permissions import Permission
from auto_maintainer.domain.profile import Route
from auto_maintainer.domain.settings import (
    SETTINGS_PATH,
    ProviderBinding,
    RuntimeConfig,
    SettingsError,
    check_settings,
    parse_settings,
)
from auto_maintainer.testing.demo import DEMO_SETTINGS_CHANGE, build_demo
from auto_maintainer.web.app import create_app

KEY = "dsk-0123456789abcdef0123456789"
DEFAULTS = {
    "review": Route(primary="claude-code", fallback=["codex"]),
    "chat": Route(primary="claude-code"),
}


def _problems(text: str) -> list[str]:
    return check_settings(parse_settings(text), DEFAULTS)


# -- the document ---------------------------------------------------------------------


def test_a_document_that_fits_has_no_problems() -> None:
    assert _problems(DEMO_SETTINGS_CHANGE) == []
    assert _problems("") == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("agents: {claude-code: {provider: nowhere}}", "no provider named 'nowhere'"),
        (
            "providers: {gw: {protocol: openai, base_url: 'https://gw.example/v1', models: [m]}}\n"
            "agents: {claude-code: {provider: gw}}",
            "claude-code cannot use a openai provider",
        ),
        (
            "providers: {gw: {protocol: anthropic, base_url: 'https://gw.example', models: [m]}}\n"
            "agents: {gemini: {provider: gw}}",
            "supports no custom provider",
        ),
        ("routing: {review: {primary: robot}}", "there is no agent named 'robot'"),
        ("routing: {dance: {primary: codex}}", "no task kind with that name"),
        ("routing: {review: {primary: pi}}", "agent 'pi' is disabled"),
    ],
)
def test_pieces_that_do_not_fit_are_named(text: str, expected: str) -> None:
    problems = _problems(text)
    assert any(expected in p for p in problems), problems


@pytest.mark.parametrize(
    "provider",
    [
        "{protocol: openai, base_url: 'https://gw.example', api_key: 'sk-live-0123456789abcdef0123'}",
        "{protocol: openai, base_url: 'https://user:pass@gw.example'}",
        "{protocol: openai, base_url: 'ftp://gw.example'}",
    ],
)
def test_a_provider_names_its_key_and_carries_no_credentials(provider: str) -> None:
    with pytest.raises(SettingsError):
        parse_settings(f"providers: {{gw: {provider}}}")


# -- applying through the dashboard -----------------------------------------------------


@pytest.fixture
async def demo(tmp_path: Path):
    h = await build_demo(tmp_path)
    h.forge.set_permission("acme/auto-maintainer", "walt", Permission.WRITE)
    h.forge.set_permission("acme/widget", "walt", Permission.WRITE)
    h.forge.set_permission("acme/widget", "oscar", Permission.WRITE)
    h.service.settings.operators = {"oscar"}
    await h.service.reload_settings()
    app = create_app(h.service, data_dir=tmp_path, allow_dev_login=True)
    yield h, app
    await h.close()


@contextlib.asynccontextmanager
async def _client(app, login: str) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.get(f"/auth/dev?login={login}")
        client.headers["x-am-csrf"] = client.cookies.get("am_csrf") or ""
        yield client


async def test_a_maintainer_of_the_service_repo_applies_settings_at_once(demo) -> None:
    h, app = demo
    assert h.service.settings.routing["evaluate"].primary == "claude-code"
    await h.service.a.secrets.set("DEEPSEEK_API_KEY", KEY, "maria")
    async with _client(app, "maria") as maria:
        out = await maria.put("/api/settings", json={"yaml": DEMO_SETTINGS_CHANGE})
        assert out.status_code == 200, out.text
        body = out.json()
        assert body["approved"] is True and body["change"]["state"] == "merged"
        view = (await maria.get("/api/settings")).json()
    assert h.memory.files[SETTINGS_PATH] == DEMO_SETTINGS_CHANGE
    assert view["status"]["applied"] is True
    [provider] = view["providers"]
    assert provider["used_by"] == ["claude-code"]
    assert provider["key"]["configured"] is True
    assert KEY not in json.dumps(view)
    agents, bindings = h.service.a.reconfigure.calls[-1]
    assert agents["claude-code"].provider == "deepseek"
    assert bindings["deepseek"].api_key == KEY


@pytest.mark.parametrize("login", ["walt", "oscar"])
async def test_anyone_else_only_proposes_even_an_operator(demo, login: str) -> None:
    h, app = demo
    text = "schema: 1\nsystem: {poll_seconds: 600}\n"
    async with _client(app, login) as other:
        body = (await other.put("/api/settings", json={"yaml": text})).json()
        assert body["approved"] is False and body["change"]["state"] == "open"
        assert "needs maintain" in body["gate"]["reason"]
        assert (
            await other.put(
                "/api/settings/secrets/DEEPSEEK_API_KEY", json={"value": KEY}
            )
        ).status_code == 403
    assert h.service.settings.poll_seconds == 300
    async with _client(app, "maria") as maria:
        pending = (await maria.get("/api/settings")).json()["pending"]
        assert [c["id"] for c in pending] == [body["change"]["id"]]
        approved = await maria.post(
            f"/api/memory/changes/{body['change']['id']}/approve"
        )
        assert approved.json()["state"] == "merged"
    assert h.service.settings.poll_seconds == 600
    assert h.service.scheduler.poll_seconds == 600


async def test_an_agent_is_switched_to_skip_permissions_through_the_gate(
    demo,
) -> None:
    h, app = demo
    body = {"permissions": "bypass"}
    async with _client(app, "walt") as walt:
        proposed = (
            await walt.put("/api/settings/agents/antigravity/permissions", json=body)
        ).json()
        assert proposed["approved"] is False
        missing = await walt.put("/api/settings/agents/nobody/permissions", json=body)
        assert missing.status_code == 404
    assert (
        h.service.service_config.agents.get("antigravity", {}).get("permissions")
        is None
    )
    async with _client(app, "maria") as maria:
        applied = (
            await maria.put("/api/settings/agents/antigravity/permissions", json=body)
        ).json()
        assert applied["approved"] is True
        [agy] = [
            a
            for a in (await maria.get("/api/settings")).json()["agents"]
            if a["name"] == "antigravity"
        ]
        assert agy["permissions"] == "bypass" and agy["can_bypass"] is True
        agents, _ = h.service.a.reconfigure.calls[-1]
        assert agents["antigravity"].permissions == "bypass"
        back = (
            await maria.put(
                "/api/settings/agents/antigravity/permissions",
                json={"permissions": None},
            )
        ).json()
        assert back["approved"] is True
    assert "permissions" not in (
        h.service.service_config.agents.get("antigravity") or {}
    )


def test_only_a_cli_with_a_bypass_mode_can_be_set_to_bypass() -> None:
    text = (
        "schema: 1\nagents:\n  pi:\n    adapter: generic\n    enabled: true\n"
        "    command: [pi, '{prompt_file}']\n    permissions: bypass\n"
    )
    assert any("no bypass mode" in p for p in _problems(text))
    assert not [
        p
        for p in _problems(
            text.replace(
                "permissions: bypass", "bypass_args: [--yes]\n    permissions: bypass"
            )
        )
        if "bypass" in p
    ]


async def test_the_service_account_approves_only_when_a_person_signed_in_as_it(
    demo,
) -> None:
    """With the GitHub CLI's login as the service's account (gh mode), its owner still
    configures the service from the dashboard; the service itself never approves."""
    h, _ = demo
    login = h.service.settings.login
    h.forge.set_permission("acme/auto-maintainer", login, Permission.ADMIN)
    h.forge.set_permission("acme/widget", login, Permission.ADMIN)
    gate = h.service.gate
    need = Permission.MAINTAIN
    refused = await gate.check_service("acme/auto-maintainer", login, need)
    assert refused.allowed is False and "its own" in refused.reason
    assert (await h.service.service_gate(login)).allowed is True
    current = await h.service.profiles.active("acme/widget")
    assert (await gate.check("acme/widget", login, current)).allowed is False
    assert (
        await gate.check("acme/widget", login, current, person=True)
    ).allowed is True


async def test_a_document_that_does_not_check_is_refused(demo) -> None:
    _, app = demo
    async with _client(app, "maria") as maria:
        out = await maria.put(
            "/api/settings", json={"yaml": "routing: {review: {primary: robot}}"}
        )
        assert out.status_code == 422
        assert "no agent named 'robot'" in json.dumps(out.json())
        check = (
            await maria.post(
                "/api/settings/check", json={"yaml": "system: {poll_seconds: 5}"}
            )
        ).json()
        assert check["ok"] is False and "poll_seconds" in check["problems"][0]


async def test_a_broken_document_keeps_the_settings_in_force(demo) -> None:
    h, _ = demo
    h.memory.files[SETTINGS_PATH] = "routing: {review: {primary: robot}}\n"
    status = await h.service.reload_settings()
    assert status.applied is False and "robot" in status.problems[0]
    assert h.service.settings.routing["evaluate"].primary == "claude-code"


async def test_keys_go_in_and_never_come_out(demo) -> None:
    h, app = demo
    async with _client(app, "maria") as maria:
        await maria.put("/api/settings", json={"yaml": DEMO_SETTINGS_CHANGE})
        info = (
            await maria.put(
                "/api/settings/secrets/DEEPSEEK_API_KEY", json={"value": KEY}
            )
        ).json()
        assert info == {**info, "name": "DEEPSEEK_API_KEY", "configured": True}
        view = (await maria.get("/api/settings")).json()
        audit = (await maria.get("/api/audit")).json()
    assert KEY not in json.dumps(view) + json.dumps(audit)
    assert await h.service.a.secrets.get("DEEPSEEK_API_KEY") == KEY
    _, bindings = h.service.a.reconfigure.calls[-1]
    assert bindings["deepseek"].api_key == KEY


# -- configuring through the agent ----------------------------------------------------------


async def test_the_agent_drafts_a_change_the_maintainer_applies(demo) -> None:
    h, app = demo
    async with _client(app, "maria") as maria:
        thread = (
            await maria.post("/api/chat/threads", json={"text": "use deepseek"})
        ).json()
        message = (
            await maria.post(
                f"/api/chat/threads/{thread['id']}/messages",
                json={"text": f"Run Claude Code on DeepSeek, my key is sk-{KEY}"},
            )
        ).json()
        [change_id] = message["changes"]
        assert "removed what looked like a credential" in message["text"]
        history = (await maria.get(f"/api/chat/threads/{thread['id']}/messages")).json()
        pending = (await maria.get("/api/settings")).json()["pending"]
        assert [c["id"] for c in pending] == [change_id]
        assert pending[0]["approvals"] == {}
        approved = await maria.post(f"/api/memory/changes/{change_id}/approve")
        assert approved.json()["state"] == "merged"
    assert KEY not in json.dumps(history)
    [prompt] = [t.prompt for t in h.runner.tasks if t.kind == TaskKind.CHAT]
    assert KEY not in prompt and "Service settings" in prompt
    assert h.memory.files[SETTINGS_PATH] == DEMO_SETTINGS_CHANGE


# -- providers in each CLI ------------------------------------------------------------------


def _binding(protocol: str) -> ProviderBinding:
    return ProviderBinding(
        id="gw",
        name="Gateway",
        protocol=protocol,
        base_url="https://gw.example/v1",
        models=("m-large",),
        api_key=KEY,
    )


def _task(tmp_path: Path) -> AgentTask:
    return AgentTask(kind=TaskKind.REVIEW, prompt="hi", workdir=tmp_path)


def test_claude_code_gets_the_endpoint_in_its_environment(tmp_path: Path) -> None:
    rt = build_runtime(
        "claude-code",
        RuntimeConfig(adapter="claude-code"),
        tmp_path,
        _binding("anthropic"),
    )
    assert isinstance(rt, ClaudeCodeRuntime)
    env = rt.provider_env()
    assert env["ANTHROPIC_BASE_URL"] == "https://gw.example/v1"
    assert env["ANTHROPIC_AUTH_TOKEN"] == KEY and env["ANTHROPIC_API_KEY"] is None
    assert rt.model == "m-large" and env["ANTHROPIC_MODEL"] == "m-large"


def test_codex_selects_the_provider_by_flags_without_the_key(tmp_path: Path) -> None:
    rt = build_runtime(
        "codex", RuntimeConfig(adapter="codex"), tmp_path, _binding("openai")
    )
    assert isinstance(rt, CodexRuntime)
    args = rt.provider_args()
    assert 'model_provider="am-gw"' in args
    assert 'model_providers.am-gw.base_url="https://gw.example/v1"' in args
    assert 'model_providers.am-gw.env_key="AM_PROVIDER_API_KEY"' in args
    assert 'model_providers.am-gw.wire_api="chat"' in args
    assert KEY not in " ".join(args)
    assert rt.provider_env() == {"AM_PROVIDER_API_KEY": KEY}


def test_opencode_gets_an_inline_provider_that_reads_the_key_from_env(
    tmp_path: Path,
) -> None:
    rt = build_runtime(
        "opencode", RuntimeConfig(adapter="opencode"), tmp_path, _binding("openai")
    )
    assert isinstance(rt, OpenCodeRuntime)
    config = json.loads(rt.task_env(_task(tmp_path))["OPENCODE_CONFIG_CONTENT"])
    provider = config["provider"]["am-gw"]
    assert provider["npm"] == "@ai-sdk/openai-compatible"
    assert provider["options"]["apiKey"] == "{env:AM_PROVIDER_API_KEY}"
    assert KEY not in json.dumps(config)
    [(_, params)] = rt.session_setup(_task(tmp_path), {})
    assert params["value"] == "am-gw/m-large"


def test_a_provider_key_is_scrubbed_from_transcripts(tmp_path: Path) -> None:
    build_runtime("codex", RuntimeConfig(adapter="codex"), tmp_path, _binding("openai"))
    assert KEY not in scrub(f"the key is {KEY}.")


async def test_the_settings_say_which_agents_are_confined(demo) -> None:
    h, app = demo
    h.memory.files[SETTINGS_PATH] = (
        "agents: {codex: {command_prefix: [sudo, -u, am-agent, --]}}\n"
    )
    assert (await h.service.reload_settings()).applied is True
    async with _client(app, "maria") as maria:
        agents = {
            a["name"]: a for a in (await maria.get("/api/settings")).json()["agents"]
        }
    assert agents["codex"]["confined"] is True
    assert agents["claude-code"]["confined"] is False

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

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from auto_maintainer.agents.adapters.claude_code import ClaudeCodeRuntime
from auto_maintainer.agents.adapters.codex import CodexRuntime
from auto_maintainer.agents.adapters.opencode import OpenCodeRuntime
from auto_maintainer.agents.exec import RunFiles
from auto_maintainer.agents.registry import build_runtime
from auto_maintainer.agents.sandbox import scrub
from auto_maintainer.domain.agents import AgentTask, TaskKind
from auto_maintainer.domain.permissions import Permission
from auto_maintainer.domain.profile import Route
from auto_maintainer.domain.settings import (
    SETTINGS_PATH,
    ModelSpec,
    ProviderBinding,
    RuntimeConfig,
    ServiceConfig,
    SettingsError,
    check_settings,
    effective_routing,
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


def test_the_default_agent_leads_every_default_route_but_verification() -> None:
    defaults = {
        **DEFAULTS,
        "verify": Route(primary="codex", fallback=["claude-code", "antigravity"]),
    }
    from_setup = effective_routing(defaults, ServiceConfig(), "antigravity")
    assert from_setup["review"].primary == "antigravity"
    assert from_setup["review"].fallback == ["claude-code", "codex"]
    assert from_setup["chat"].fallback == ["claude-code"]
    assert from_setup["verify"].primary == "codex", "verification wants a second agent"

    config = parse_settings(
        "default_agent: codex\nrouting: {chat: {primary: claude-code, fallback: []}}"
    )
    routes = effective_routing(defaults, config, "antigravity")
    assert routes["review"].primary == "codex", "the settings override setup's choice"
    assert routes["chat"].primary == "claude-code" and routes["chat"].fallback == []
    assert effective_routing(defaults, ServiceConfig()) == {
        k: v.model_copy(deep=True) for k, v in defaults.items()
    }


def test_a_document_that_fits_has_no_problems() -> None:
    assert _problems(DEMO_SETTINGS_CHANGE) == []
    assert _problems("") == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("agents: {claude-code: {provider: nowhere}}", "no provider named 'nowhere'"),
        ("default_agent: devin", "default_agent: there is no agent named 'devin'"),
        ("agents: {codex: {effort: turbo}}", "agents.codex.effort: use one of"),
        (
            "default_agent: codex\nagents: {codex: {enabled: false}}",
            "default_agent: agent 'codex' is disabled",
        ),
        (
            "providers: {gw: {protocol: openai, base_url: 'https://gw.example/v1', models: [m]}}\n"
            "agents: {claude-code: {provider: gw}}",
            "claude-code cannot use a openai provider",
        ),
        (
            "providers: {gw: {protocol: anthropic, base_url: 'https://gw.example', models: [m]}}\n"
            "agents: {antigravity: {provider: gw}}",
            "supports no custom provider",
        ),
        ("routing: {review: {primary: robot}}", "there is no agent named 'robot'"),
        ("routing: {dance: {primary: codex}}", "no task kind with that name"),
        (
            "agents: {pi: {enabled: false}}\nrouting: {review: {primary: pi}}",
            "agent 'pi' is disabled",
        ),
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


async def test_the_agent_chosen_during_setup_runs_every_task(demo) -> None:
    h, app = demo
    h.service.host_values["default_agent"] = "antigravity"
    await h.service.reload_settings()
    routes = h.service.settings.routing
    assert routes["onboard"].primary == "antigravity"
    assert routes["onboard"].fallback[0] == "claude-code"
    async with _client(app, "maria") as maria:
        view = (await maria.get("/api/settings")).json()
    assert view["default_agent"] == {"name": "antigravity", "source": "setup"}


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
        assert "only maria, who owns this service" in body["gate"]["reason"]
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
    """When the owner signed the service in as themselves, they still configure the
    service from the dashboard; the service itself never approves."""
    h, _ = demo
    login = h.service.settings.login
    h.service.settings.owner = login
    h.forge.set_permission("acme/auto-maintainer", login, Permission.ADMIN)
    h.forge.set_permission("acme/widget", login, Permission.ADMIN)
    gate = h.service.gate
    refused = await gate.check_service("acme/auto-maintainer", login, login)
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


async def test_a_provider_is_added_by_its_form_and_its_key_kept_apart(
    demo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The owner adds a provider from its form, key included: the key goes to the
    secret store and the settings name it; models with nothing known are written by
    id. Anyone else's form waits as a proposal, without a key, and may not ask the
    endpoint, which could spend the service's keys."""
    from auto_maintainer.web import settings as settings_web

    h, app = demo
    asked: list[str | None] = []

    async def fake_list(protocol: str, base_url: str, key: str | None, **_: Any):
        asked.append(key)
        return {"ok": True, "models": [{"id": "m1"}], "status": 200}

    monkeypatch.setattr(settings_web, "list_models", fake_list)
    form = {
        "name": "Gateway",
        "protocol": "openai",
        "base_url": "https://gw.example/v1/",
        "models": [{"id": "m1", "context_window": 128000, "tools": True}, {"id": "m2"}],
        "key": KEY,
    }
    async with _client(app, "walt") as walt:
        assert (
            await walt.put("/api/settings/providers/gw", json=form)
        ).status_code == 403
        proposed = await walt.put(
            "/api/settings/providers/gw", json={**form, "key": None}
        )
        assert proposed.json()["approved"] is False
        assert (
            await walt.post(
                "/api/settings/providers/query",
                json={"protocol": "openai", "base_url": "https://gw.example/v1"},
            )
        ).status_code == 403
    async with _client(app, "maria") as maria:
        bad = await maria.put("/api/settings/providers/Gw", json=form)
        assert bad.status_code == 422
        saved = await maria.put("/api/settings/providers/gw", json=form)
        assert saved.status_code == 200 and saved.json()["approved"] is True
        assert KEY not in saved.text
        listed = await maria.post(
            "/api/settings/providers/query",
            json={
                "protocol": "openai",
                "base_url": "https://gw.example/v1",
                "provider": "gw",
            },
        )
        assert listed.json()["models"] == [{"id": "m1"}] and asked == [KEY], (
            "the stored key, when the form types none"
        )
        view = (await maria.get("/api/settings")).json()
    written = parse_settings(h.memory.files[SETTINGS_PATH])
    gw = written.providers["gw"]
    assert (gw.api_key, gw.base_url) == ("GW_API_KEY", "https://gw.example/v1")
    assert "- m2\n" in h.memory.files[SETTINGS_PATH], "a bare model by its id"
    assert await h.service.a.secrets.get("GW_API_KEY") == KEY
    [row] = [p for p in view["providers"] if p["id"] == "gw"]
    assert row["works_with"]["m1"] == ["dsh", "opencode", "pi"]
    assert KEY not in json.dumps(view)


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


def _binding(protocol: str, *specs: ModelSpec) -> ProviderBinding:
    return ProviderBinding(
        id="gw",
        name="Gateway",
        protocol=protocol,
        base_url="https://gw.example/v1",
        models=tuple(m.id for m in specs) or ("m-large",),
        api_key=KEY,
        specs=specs,
    )


LARGE = ModelSpec(
    id="m-large",
    context_window=200_000,
    max_output=32_000,
    reasoning=["low", "high"],
    images=True,
    interfaces=["openai", "openai-responses", "anthropic"],
    tested_at="2026-11-10T00:00:00+00:00",
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
    assert env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
    assert rt.model == "m-large"
    for var in (
        "ANTHROPIC_MODEL",
        "ANTHROPIC_SMALL_FAST_MODEL",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL",
        "ANTHROPIC_DEFAULT_SONNET_MODEL",
        "ANTHROPIC_DEFAULT_OPUS_MODEL",
        "CLAUDE_CODE_SUBAGENT_MODEL",
    ):
        assert env[var] == "m-large"

    override = _task(tmp_path).model_copy(update={"model": "m-override"})
    task_env = rt.task_env(override)
    assert task_env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
    for var in (
        "ANTHROPIC_MODEL",
        "ANTHROPIC_SMALL_FAST_MODEL",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL",
        "ANTHROPIC_DEFAULT_SONNET_MODEL",
        "ANTHROPIC_DEFAULT_OPUS_MODEL",
        "CLAUDE_CODE_SUBAGENT_MODEL",
    ):
        assert task_env[var] == "m-override"

    native = build_runtime("claude-code", RuntimeConfig(adapter="claude-code"))
    assert native.provider_env() == {}
    assert native.task_env(override) == {}


def test_codex_selects_the_provider_by_flags_without_the_key(tmp_path: Path) -> None:
    rt = build_runtime(
        "codex",
        RuntimeConfig(adapter="codex"),
        tmp_path,
        _binding("openai-responses", LARGE),
    )
    assert isinstance(rt, CodexRuntime)
    args = rt.provider_args()
    assert 'model_provider="am-gw"' in args
    assert 'model_providers.am-gw.base_url="https://gw.example/v1"' in args
    assert 'model_providers.am-gw.env_key="AM_PROVIDER_API_KEY"' in args
    assert 'model_providers.am-gw.wire_api="responses"' in args
    assert "model_context_window=200000" in args
    assert KEY not in " ".join(args)
    assert rt.provider_env() == {"AM_PROVIDER_API_KEY": KEY}


def test_an_agent_reaches_a_provider_over_an_api_its_model_answered_on(
    tmp_path: Path,
) -> None:
    """Codex no longer talks Chat Completions: on a chat provider it runs on its own
    sign-in, unless the model answered on the Responses API too when it was tested."""
    untested = build_runtime(
        "codex", RuntimeConfig(adapter="codex"), tmp_path, _binding("openai")
    )
    assert untested.provider is None
    both = build_runtime(
        "codex", RuntimeConfig(adapter="codex"), tmp_path, _binding("openai", LARGE)
    )
    assert both.provider is not None and both.provider.protocol == "openai-responses"
    claude = build_runtime(
        "claude-code",
        RuntimeConfig(adapter="claude-code"),
        tmp_path,
        _binding("openai", LARGE),
    )
    assert claude.provider.protocol == "anthropic"
    assert claude.provider_env()["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "32000"


def test_pi_reads_its_key_by_name_and_knows_the_model(tmp_path: Path) -> None:
    rt = build_runtime(
        "pi", RuntimeConfig(adapter="pi"), tmp_path, _binding("openai", LARGE)
    )
    files = RunFiles.create(tmp_path / "runs", "run_pi")
    rt.build_invocation(_task(tmp_path), files, "pi")
    models = json.loads((files.dir / "pi-agent" / "models.json").read_text())
    provider = models["providers"]["am"]
    assert provider["apiKey"] == "AM_PROVIDER_API_KEY", (
        "pi reads a value that names a set variable from it, and sends any other as is"
    )
    assert provider["api"] == "openai-completions"
    assert provider["models"] == [
        {
            "id": "m-large",
            "contextWindow": 200_000,
            "maxTokens": 32_000,
            "input": ["text", "image"],
            "reasoning": True,
            "thinkingLevelMap": {"minimal": None, "medium": None, "xhigh": None},
        }
    ]
    assert KEY not in json.dumps(models)


def test_gemini_cli_reaches_a_gemini_api_at_another_address(tmp_path: Path) -> None:
    binding = ProviderBinding(
        id="g",
        name="Gemini gateway",
        protocol="google",
        base_url="https://gemini.example/v1beta",
        models=("gemini-pro",),
        api_key=KEY,
    )
    rt = build_runtime("gemini", RuntimeConfig(adapter="gemini"), tmp_path, binding)
    assert rt.provider_env() == {
        "GOOGLE_GEMINI_BASE_URL": "https://gemini.example",
        "GEMINI_API_KEY": KEY,
        "GOOGLE_API_KEY": None,
    }


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
    assert "limit" not in provider["models"]["m-large"], "nothing is known of it"
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


async def test_the_sandbox_is_a_setting_of_the_services_maintainers(demo) -> None:
    from auto_maintainer.agents.container import ContainerSandbox, SandboxStarter

    h, app = demo

    class ReadyBox(ContainerSandbox):
        async def ready(self) -> bool:
            return True

    runtime = build_runtime("antigravity", RuntimeConfig(adapter="antigravity"))
    saved: list[dict] = []
    control = SandboxStarter(
        ReadyBox("agent:1", docker="sh"),
        lambda: {"antigravity": runtime},
        save=saved.append,
    )
    await control.start()
    await control._task  # type: ignore[misc]
    h.service.a.agent_sandbox = control
    assert runtime.container_sandbox is not None
    change = {"enabled": True, "network": "host", "cpus": 4, "memory": "16g"}
    async with _client(app, "walt") as walt:
        view = (await walt.get("/api/settings/sandbox")).json()
        assert view["ready"] and view["network"] == "bridge"
        assert view["can_configure"] is False
        refused = await walt.put("/api/settings/sandbox", json=change)
        assert refused.status_code == 403 and not saved
    async with _client(app, "maria") as maria:
        out = (await maria.put("/api/settings/sandbox", json=change)).json()
        assert (out["network"], out["cpus"], out["memory"]) == ("host", 4, "16g")
        assert saved == [change], "kept in this host's local.yaml"
        assert control.sandbox.network == "host", "the next run uses it"
        bad = await maria.put(
            "/api/settings/sandbox", json={**change, "network": "none"}
        )
        assert bad.status_code == 422
        off = (
            await maria.put("/api/settings/sandbox", json={**change, "enabled": False})
        ).json()
        assert off["enabled"] is False and "on the host" in off["status"]
        assert runtime.container_sandbox is None, "agents run on the host at once"


class _Installer:
    def sources(self) -> dict[str, str]:
        return {"codex": "npm @openai/codex"}

    async def install(self, name, on_line=None):
        from auto_maintainer.domain.agents import InstalledAgent

        if on_line is not None:
            await on_line("added 1 package")
        return InstalledAgent(
            name=name, version="9.9.9", path="/x", source="npm @openai/codex"
        )


async def test_only_maintainers_of_the_service_install_agents(demo) -> None:
    h, app = demo
    h.service.a.installer = _Installer()
    told: list[str] = []
    notify = h.service.bus.notify
    h.service.bus.notify = lambda e: (told.append(e.kind), notify(e))  # type: ignore[method-assign]
    async with _client(app, "walt") as walt:
        refused = await walt.post("/api/settings/agents/codex/install")
        assert refused.status_code == 403
    async with _client(app, "maria") as maria:
        view = (await maria.get("/api/settings")).json()
        assert view["installable"] == {"codex": "npm @openai/codex"}
        started = await maria.post("/api/settings/agents/codex/install")
        assert started.status_code == 200 and started.json()["by"] == "maria"
        assert (
            await maria.post("/api/settings/agents/nonesuch/install")
        ).status_code == 404
        await asyncio.gather(*h.service._install_tasks)
        done = (await maria.get("/api/settings")).json()["installs"]["codex"]
    assert (done["state"], done["version"], done["lines"]) == (
        "done",
        "9.9.9",
        ["added 1 package"],
    )
    audit = await h.store.list_audit(limit=5)
    assert any("installed codex 9.9.9" in a.summary for a in audit)
    assert "service.changed" in told, "open Settings pages show it installed"


async def test_people_run_agents_on_their_own_accounts_and_nobody_elses(
    demo, tmp_path: Path
) -> None:
    from auto_maintainer.agents.adapters.claude_code import ClaudeCodeRuntime
    from auto_maintainer.store.people import PeopleAgents

    h, app = demo
    cli = tmp_path / "claude"
    cli.write_text(
        "#!/bin/sh\necho 'visit: https://sign.example/a'\n"
        "printf 'Paste code here > '\nread code\n[ \"$code\" = ok ]\n"
    )
    cli.chmod(0o755)
    runtime = ClaudeCodeRuntime(binary=str(cli))
    h.runner.runtimes = {"claude-code": runtime}
    h.service.a.people = PeopleAgents(tmp_path / "people")
    own = {"use_own": True, "provider": "gw", "model": "m1"}
    api = {
        "name": "Gateway",
        "protocol": "anthropic",
        "base_url": "https://gw.example",
        "models": [{"id": "m1", "reasoning": ["high"]}],
        "key": "sk-w",
    }
    async with _client(app, "walt") as walt:
        [row] = (await walt.get("/api/me/agents")).json()["agents"]
        assert (row["name"], row["can_sign_in"], row["isolated"]) == (
            "claude-code",
            True,
            False,
        )
        refused = await walt.put("/api/me/agents/claude-code", json=own)
        assert refused.status_code == 409 and "on the host" in refused.json()["detail"]
        runtime.container_sandbox = object()
        local = {"protocol": "openai", "base_url": "http://127.0.0.1:8080/v1"}
        probe = await walt.post("/api/me/providers/query", json=local)
        assert probe.status_code == 422 and "this host" in probe.json()["detail"], (
            "a person's probe may not point the host at itself"
        )
        saved = await walt.put("/api/me/providers/gw", json=api)
        assert saved.status_code == 200 and "sk-w" not in saved.text
        [row] = saved.json()["providers"]
        assert row["has_key"] and row["works_with"]["m1"] == [
            "claude-code",
            "opencode",
            "pi",
        ]
        wrong = await walt.put(
            "/api/me/providers/oa", json={**api, "protocol": "openai"}
        )
        assert wrong.status_code == 200
        assert (
            await walt.put("/api/me/agents/claude-code", json={**own, "provider": "oa"})
        ).status_code == 422, "Claude Code does not talk the OpenAI API"
        assert (
            await walt.put("/api/me/agents/claude-code", json={**own, "effort": "low"})
        ).status_code == 422, "m1 reasons only at high"
        mine = await walt.put(
            "/api/me/agents/claude-code", json={**own, "effort": "high"}
        )
        assert mine.status_code == 200
        assert mine.json()["setup"] == {
            "agent": "claude-code",
            "use_own": True,
            "provider": "gw",
            "model": "m1",
            "effort": "high",
        }

        async def own(login: str) -> list[str]:
            return ["claude-code"] if login == "walt" else []

        h.runner.own_agents = own  # what the router reports
        [row] = [
            a
            for a in (await walt.get("/api/settings")).json()["agents"]
            if a["name"] == "claude-code"
        ]
        assert (row["mine"], row["can_sign_in"]) == ("api", True), (
            "the pickers offer it to walt, on his API"
        )
        assert (
            await walt.post("/api/settings/agents/claude-code/sign-in")
        ).status_code == 403
        mine = (await walt.post("/api/me/agents/claude-code/sign-in")).json()
        assert mine["account"] == "walt"
    async with _client(app, "maria") as maria:
        assert (await maria.get(f"/api/agent-logins/{mine['id']}")).status_code == 404
        assert (
            await maria.post(f"/api/agent-logins/{mine['id']}/code", json={"code": "x"})
        ).status_code == 404
        [row] = [
            a
            for a in (await maria.get("/api/settings")).json()["agents"]
            if a["name"] == "claude-code"
        ]
        assert row["mine"] is None, "walt's account is not maria's"
        theirs = (await maria.get("/api/me/agents")).json()
        assert theirs["agents"][0]["setup"]["use_own"] is False, (
            "walt's setup is walt's alone"
        )
        assert "gw" not in [a["id"] for a in theirs["providers"]], (
            "and so are his model APIs"
        )
        service_login = await maria.post("/api/settings/agents/claude-code/sign-in")
        assert service_login.status_code == 200
        assert service_login.json()["account"] is None
        await maria.delete(f"/api/agent-logins/{service_login.json()['id']}")
    async with _client(app, "walt") as walt:
        for _ in range(100):
            state = (await walt.get(f"/api/agent-logins/{mine['id']}")).json()
            if state["needs_code"]:
                break
            await asyncio.sleep(0.05)
        assert state["url"] == "https://sign.example/a"
        await walt.post(f"/api/agent-logins/{mine['id']}/code", json={"code": "ok"})
        await h.service.logins[mine["id"]].wait()
        assert (await walt.get(f"/api/agent-logins/{mine['id']}")).json()[
            "state"
        ] == "done"
    assert await h.service.a.people.provider_key("walt", "gw") == "sk-w"


async def test_one_owner_acts_while_an_admin_of_the_service_repo(demo) -> None:
    """The owner alone changes the service, and only while an admin of its
    repository; another admin cannot, and a host set up before owners lets every
    admin act until one takes ownership."""
    h, _ = demo
    repo = "acme/auto-maintainer"
    h.forge.set_permission(repo, "ada", Permission.ADMIN)
    assert (await h.service.service_gate("maria")).allowed is True
    refused = await h.service.service_gate("ada")
    assert refused.allowed is False and "only maria" in refused.reason
    h.forge.set_permission(repo, "maria", Permission.MAINTAIN)
    demoted = await h.service.service_gate("maria")
    assert demoted.allowed is False and "must be an admin" in demoted.reason
    h.service.settings.owner = None
    assert (await h.service.service_gate("ada")).allowed is True
    assert (await h.service.service_gate("maria")).allowed is False
    h.service.settings.code_repo = None
    assert "no repository of its own" in (await h.service.service_gate("ada")).reason


async def test_a_persons_work_is_shown_on_their_own_agent_before_it_runs(demo) -> None:
    """What the dashboard names before an agent starts (a job's agent, the review
    button's): the requester's own agent, which the router tries first."""
    from auto_maintainer.domain.profile import Route

    h, _ = demo

    async def own(login: str) -> list[str]:
        return ["claude-code"] if login == "maria" else []

    h.runner.own_agents = own  # type: ignore[attr-defined]
    route = Route(primary="antigravity")
    assert await h.service.runs_on_for(route, "maria", "fix") == "claude-code"
    assert await h.service.runs_on_for(route, "walt", "fix") == h.service.runs_on(route)
    assert await h.service.runs_on_for(route, "maria", "verify") == (
        h.service.runs_on(route)
    ), "a verification keeps its route's agent"
    assert await h.service.runs_on_for(route, None, "fix") == h.service.runs_on(route)

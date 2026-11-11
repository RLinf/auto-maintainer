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

"""The pi adapter: its command line per permission mode, a model provider through
a run's own models.json, its JSON event stream (as pi's docs/json.md shows it), and
its sign-in through pi's own login, without its terminal UI."""

from __future__ import annotations

import asyncio
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from auto_maintainer.agents.adapters.pi import PiRuntime
from auto_maintainer.agents.exec import RunFiles
from auto_maintainer.agents.registry import DEFAULT_RUNTIMES, build_runtime
from auto_maintainer.domain.agents import (
    AgentEventType,
    AgentLogin,
    AgentTask,
    TaskKind,
)
from auto_maintainer.domain.settings import ProviderBinding


def _task(tmp_path: Path, **kw: object) -> AgentTask:
    data: dict[str, object] = {
        "kind": TaskKind.REVIEW,
        "prompt": "Review it.",
        "workdir": tmp_path,
    }
    data.update(kw)
    return AgentTask.model_validate(data)


def _files(tmp_path: Path) -> RunFiles:
    return RunFiles.create(tmp_path / "runs", "run_pi")


def test_pi_is_on_by_default_through_its_own_adapter() -> None:
    assert DEFAULT_RUNTIMES["pi"].adapter == "pi" and DEFAULT_RUNTIMES["pi"].enabled
    assert isinstance(build_runtime("pi", DEFAULT_RUNTIMES["pi"]), PiRuntime)


def test_only_bypass_mode_gives_pi_its_bash_tool(tmp_path: Path) -> None:
    pi = PiRuntime("pi")
    files = _files(tmp_path)
    read = pi.build_invocation(_task(tmp_path), files, "pi")
    assert read.argv == [
        "pi",
        "-p",
        "--mode",
        "json",
        "--no-session",
        "--offline",
        "--tools",
        "read,grep,find,ls",
    ]
    assert read.stdin == "Review it.", "the prompt goes in on standard input"
    write = pi.build_invocation(_task(tmp_path, mode="workspace_write"), files, "pi")
    assert write.argv[-1] == "read,grep,find,ls,edit,write"
    bypass = pi.build_invocation(
        _task(
            tmp_path,
            permissions="bypass",
            model="anthropic/claude-sonnet-4",
            effort="max",
        ),
        files,
        "pi",
    )
    assert "--tools" not in bypass.argv
    assert bypass.argv[-4:] == [
        "--model",
        "anthropic/claude-sonnet-4",
        "--thinking",
        "xhigh",
    ]
    assert pi.task_env(_task(tmp_path))["PI_OFFLINE"] == "1"


def test_a_model_provider_goes_into_the_runs_own_models_json(tmp_path: Path) -> None:
    key = "sk-" + "k" * 30
    pi = PiRuntime(
        "pi",
        model="deepseek-chat",
        provider=ProviderBinding(
            id="ds",
            name="DeepSeek",
            protocol="openai",
            base_url="https://api.deepseek.com/v1",
            api_key=key,
        ),
    )
    task = _task(tmp_path)
    files = _files(tmp_path)
    inv = pi.build_invocation(task, files, "pi")
    assert inv.argv[-4:] == ["--provider", "am", "--model", "deepseek-chat"]
    written = (files.dir / "pi-agent" / "models.json").read_text()
    assert key not in written, "the key stays in the environment"
    assert json.loads(written)["providers"]["am"] == {
        "baseUrl": "https://api.deepseek.com/v1",
        "api": "openai-completions",
        "apiKey": "AM_PROVIDER_API_KEY",
        "models": [{"id": "deepseek-chat"}],
    }
    assert pi.task_env(task)["PI_CODING_AGENT_DIR"] == str(files.dir / "pi-agent")
    assert pi.provider_env() == {"AM_PROVIDER_API_KEY": key}


def test_pis_json_events_become_steps_usage_and_the_answer() -> None:
    pi = PiRuntime("pi")
    lines = [
        {"type": "session", "version": 3, "id": "u", "cwd": "/w"},
        {"type": "message_end", "message": {"role": "user", "content": "Review it."}},
        {
            "type": "tool_execution_start",
            "toolCallId": "c1",
            "toolName": "bash",
            "args": {"command": "ls -la"},
        },
        {
            "type": "tool_execution_end",
            "toolCallId": "c1",
            "toolName": "bash",
            "result": {"content": [{"type": "text", "text": "a.py"}]},
            "isError": False,
        },
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "hm"},
                    {"type": "text", "text": '{"ok": true}'},
                ],
                "stopReason": "stop",
                "usage": {
                    "input": 100,
                    "output": 7,
                    "cacheRead": 20,
                    "cost": {"total": 0.01},
                },
            },
        },
    ]
    events = [e for line in lines for e in pi.parse_line(json.dumps(line))]
    kinds = [e.type for e in events]
    assert kinds == [
        AgentEventType.TOOL_CALL,
        AgentEventType.TOOL_RESULT,
        AgentEventType.USAGE,
        AgentEventType.MESSAGE,
        AgentEventType.FINAL,
    ]
    assert events[0].text.startswith("bash:") and events[1].text == "a.py"
    assert events[2].data == {
        "input_tokens": 120,
        "cached_input_tokens": 20,
        "output_tokens": 7,
        "cost_usd": 0.01,
    }
    assert events[3].text == '{"ok": true}'
    failed = pi.parse_line(
        json.dumps(
            {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "content": [],
                    "stopReason": "error",
                    "errorMessage": "401 invalid key",
                },
            }
        )
    )
    assert (
        failed[-1].type == AgentEventType.ERROR and failed[-1].text == "401 invalid key"
    )
    assert pi.parse_line("not json") == []


async def test_a_run_reads_the_answer_from_a_pi_that_speaks_json(
    tmp_path: Path,
) -> None:
    script = tmp_path / "pi"
    script.write_text(
        "#!/bin/sh\n"
        "prompt=$(cat)\n"
        'echo \'{"type":"session","version":3,"id":"u"}\'\n'
        'printf \'{"type":"message_end","message":{"role":"assistant","content":[{"type":"text","text":"saw: %s"}],"stopReason":"stop","usage":{"input":5,"output":2}}}\\n\' "$prompt"\n'
    )
    script.chmod(0o755)
    pi = PiRuntime("pi", binary=str(script), runs_dir=tmp_path / "runs")
    events = [e async for e in pi.run(_task(tmp_path, prompt="hello"))]
    result = events[-1].result
    assert result is not None and result.ok, result
    assert result.text == "saw: hello"
    assert (result.usage.input_tokens, result.usage.output_tokens) == (5, 2)


FAKE_PI = """
import { mkdirSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
export class AuthStorage {
  static create() { return new AuthStorage(); }
  getOAuthProviders() {
    return [{ id: "anthropic", name: "Claude Pro/Max", usesCallbackServer: true }];
  }
  async login(id, cb) {
    cb.onAuth({ url: "https://auth.example/authorize?x=1", instructions: "Log in" });
    const code = await cb.onManualCodeInput();
    cb.onProgress("Exchanging authorization code for tokens...");
    const dir = join(homedir(), ".pi", "agent");
    mkdirSync(dir, { recursive: true });
    writeFileSync(join(dir, "auth.json"), JSON.stringify({ [id]: { access: code } }));
  }
}
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node.js")
async def test_pi_signs_in_through_its_own_login_without_its_terminal_ui(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dashboard relays pi's own sign-in: the page to open, then the pasted
    address, which goes only to the login; nothing but a listed account kind
    starts one, and the sign-in lands in the account's home, whatever the host's
    ``PI_CODING_AGENT_DIR`` says."""
    package = tmp_path / "node_modules/@mariozechner/pi-coding-agent"
    (package / "dist").mkdir(parents=True)
    (package / "package.json").write_text(
        '{"name": "@mariozechner/pi-coding-agent", "type": "module"}'
    )
    (package / "dist/index.js").write_text(FAKE_PI)
    (package / "dist/cli.js").write_text("#!/usr/bin/env node\n")
    (package / "dist/cli.js").chmod(0o755)
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin/pi").symlink_to(package / "dist/cli.js")
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "elsewhere"))
    runtime = PiRuntime("pi", binary=str(tmp_path / "bin/pi"))
    home = tmp_path / "home"
    assert await runtime.signed_in(home) is False

    def state() -> AgentLogin:
        return AgentLogin(agent="pi", by="maria", started_at=datetime.now(UTC))

    for option in (None, "; sh", "openai"):
        with pytest.raises(RuntimeError, match="choose what pi signs in to"):
            runtime.start_login(home, state(), option)
    login = state()
    process = runtime.start_login(home, login, "anthropic")
    for _ in range(200):
        if login.needs_code:
            break
        await asyncio.sleep(0.05)
    assert login.url == "https://auth.example/authorize?x=1" and login.needs_code
    await process.submit("http://localhost:53692/callback?code=c1&state=s")
    await asyncio.wait_for(process.wait(), 20)
    assert login.state == "done", login.message
    saved = json.loads((home / ".pi/agent/auth.json").read_text())
    assert saved == {
        "anthropic": {"access": "http://localhost:53692/callback?code=c1&state=s"}
    }
    assert not (tmp_path / "elsewhere").exists()
    assert await runtime.signed_in(home) is True

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

"""Agent sign-ins run for someone on the dashboard, and each person's own agent
accounts: kept apart on the host, and used only for the work that person asks for."""

from __future__ import annotations

import asyncio
import os
import stat
from datetime import UTC, datetime
from pathlib import Path

from auto_maintainer.agents.adapters.claude_code import ClaudeCodeRuntime
from auto_maintainer.agents.container import ContainerSandbox
from auto_maintainer.agents.router import AgentRouter
from auto_maintainer.domain.agents import (
    AgentLogin,
    AgentTask,
    PersonalAgent,
    PersonalProvider,
    TaskKind,
)
from auto_maintainer.store.people import PeopleAgents

PASTE_CLI = """#!/bin/sh
# Signs in the way Claude Code and Antigravity do without a browser.
echo "If the browser didn't open, visit: https://sign.example/authorize?state=s1"
printf 'Paste code here if prompted > '
read code
if [ "$code" = "good-code" ]; then
  mkdir -p "$HOME/.claude" && echo signed > "$HOME/.claude/.credentials.json"
  echo "Login successful."
  exit 0
fi
echo "Invalid code"
exit 1
"""

DEVICE_CLI = """#!/bin/sh
# Signs in the way Codex does: a page and a one-time code, then it waits.
printf '\\033[94mhttps://auth.example/device\\033[0m\\n'
echo "2. Enter this one-time code (expires in 15 minutes)"
printf '   \\033[94mABCD-EFGH\\033[0m\\n'
sleep 1
exit 0
"""


def _cli(tmp_path: Path, body: str) -> str:
    path = tmp_path / "cli"
    path.write_text(body)
    path.chmod(0o755)
    return str(path)


async def _until(state: AgentLogin, done: callable, seconds: float = 10) -> None:
    for _ in range(int(seconds / 0.05)):
        if done(state):
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"sign-in stuck: {state}")


async def test_a_pasted_code_reaches_only_the_cli_and_signs_it_in(
    tmp_path: Path,
) -> None:
    runtime = ClaudeCodeRuntime(binary=_cli(tmp_path, PASTE_CLI))
    home = tmp_path / "home"
    state = AgentLogin(agent="claude-code", by="maria", started_at=datetime.now(UTC))
    process = runtime.start_login(home, state)
    await _until(state, lambda s: s.needs_code)
    assert state.url == "https://sign.example/authorize?state=s1"
    await process.submit("good-code")
    await process.wait()
    assert state.state == "done" and not state.needs_code
    assert (home / ".claude/.credentials.json").read_text() == "signed\n"
    assert "good-code" not in state.model_dump_json()


async def test_a_device_login_shows_its_page_and_one_time_code(tmp_path: Path) -> None:
    runtime = ClaudeCodeRuntime(binary=_cli(tmp_path, DEVICE_CLI))
    state = AgentLogin(agent="codex", by="maria", started_at=datetime.now(UTC))
    process = runtime.start_login(tmp_path / "home", state)
    await process.wait()
    assert (state.url, state.code, state.state) == (
        "https://auth.example/device",
        "ABCD-EFGH",
        "done",
    )


async def test_each_persons_folder_is_private_and_their_key_write_only(
    tmp_path: Path,
) -> None:
    people = PeopleAgents(tmp_path / "people")
    home = people.home("Maria")
    assert stat.S_IMODE(os.stat(home.parent).st_mode) == 0o700
    await people.put(
        "maria",
        PersonalAgent(
            agent="claude-code",
            use_own=True,
            provider=PersonalProvider(
                protocol="anthropic", base_url="https://gw.example", models=["m1"]
            ),
        ),
    )
    await people.set_key("maria", "claude-code", "sk-own")
    [setup] = (await people.get("MARIA")).values()
    assert setup.use_own and setup.has_key
    assert "sk-own" not in setup.model_dump_json()
    keys = home.parent / "keys.json"
    assert stat.S_IMODE(os.stat(keys).st_mode) == 0o600
    assert await people.key("maria", "claude-code") == "sk-own"

    (home / ".claude").mkdir()
    (home / ".claude.json").write_text("{}")
    (home / ".codex").mkdir()
    await people.sign_out("maria", (".claude", ".claude.json", "../.."))
    assert not (home / ".claude").exists() and not (home / ".claude.json").exists()
    assert (home / ".codex").exists() and (tmp_path / "people").exists()


async def test_a_task_runs_on_its_requesters_own_account_only_in_a_container(
    tmp_path: Path,
) -> None:
    people = PeopleAgents(tmp_path / "people")
    await people.put(
        "maria",
        PersonalAgent(
            agent="claude-code",
            use_own=True,
            provider=PersonalProvider(
                protocol="anthropic", base_url="https://gw.example", models=["m1"]
            ),
        ),
    )
    await people.set_key("maria", "claude-code", "sk-own")
    await people.put("walt", PersonalAgent(agent="claude-code", use_own=False))
    runtime = ClaudeCodeRuntime(model="opus")
    router = AgentRouter({"claude-code": runtime})
    router.people = people

    def task(by: str | None) -> AgentTask:
        return AgentTask(
            kind=TaskKind.REVIEW, prompt="p", workdir=tmp_path, requested_by=by
        )

    assert await router._account(runtime, task("maria")) is runtime, (
        "on the host, every run could read every person's sign-in"
    )
    runtime.container_sandbox = object()
    mine = await router._account(runtime, task("maria"))
    assert mine is not runtime
    assert (mine.account, mine.account_home) == ("maria", people.home("maria"))
    assert mine.provider.api_key == "sk-own" and mine.model == "m1"
    assert runtime.provider is None and runtime.model == "opus", "the original stays"
    for other in ("walt", "oscar", None):
        assert await router._account(runtime, task(other)) is runtime


def test_a_persons_run_mounts_their_sign_in_and_not_the_services(
    tmp_path: Path,
) -> None:
    host_home, own = tmp_path / "host", tmp_path / "people/maria/home"
    for home in (host_home, own):
        (home / ".claude").mkdir(parents=True)
    work, run = tmp_path / "work", tmp_path / "run"
    work.mkdir()
    run.mkdir()
    sandbox = ContainerSandbox("img", home=host_home)
    argv = sandbox.prepare(
        "r1",
        work,
        run,
        "/bin/true",
        ["/bin/true"],
        {},
        home_paths=(".claude",),
        home_source=own,
    ).argv
    mounts = " ".join(argv)
    assert f"src={own / '.claude'},dst={host_home / '.claude'}" in mounts
    assert f"src={host_home / '.claude'}," not in mounts


async def test_antigravity_without_its_token_is_not_signed_in_at_once(
    tmp_path: Path,
) -> None:
    from auto_maintainer.agents.adapters.antigravity import AntigravityRuntime

    hangs = tmp_path / "agy"
    hangs.write_text("#!/bin/sh\nsleep 30\n")  # agy -p waits for a sign-in
    hangs.chmod(0o755)
    runtime = AntigravityRuntime(binary=str(hangs))
    assert await asyncio.wait_for(runtime.signed_in(tmp_path / "home"), 2) is False


async def test_a_cli_that_stays_up_after_signing_in_is_stopped_and_done(
    tmp_path: Path,
) -> None:
    """Antigravity's ``acp --login`` serves ACP once it signed in; the sign-in ends
    when the CLI writes its sign-in file."""
    stays = _cli(
        tmp_path,
        "#!/bin/sh\necho 'visit: https://sign.example/a'\nprintf 'paste the code > '\n"
        'read code\nmkdir -p "$HOME/.claude"\necho t > "$HOME/.claude/.credentials.json"\n'
        "sleep 60\n",
    )
    runtime = ClaudeCodeRuntime(binary=stays)
    state = AgentLogin(agent="claude-code", by="maria", started_at=datetime.now(UTC))
    process = runtime.start_login(tmp_path / "home", state)
    await _until(state, lambda s: s.needs_code)
    await process.submit("c")
    await asyncio.wait_for(process.wait(), 10)
    assert state.state == "done"

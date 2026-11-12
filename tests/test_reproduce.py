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

"""Reproducing a PR on a test machine: machines in the settings, the agent's script
waiting for the requester's approval, both sides run on the machine, and a report
drafted for the requester."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from auto_maintainer.domain.actions import LabReproducePayload
from auto_maintainer.domain.clock import FixedClock
from auto_maintainer.domain.permissions import Permission
from auto_maintainer.domain.ports import CommandResult, GpuStatus
from auto_maintainer.domain.profile import LabBinding
from auto_maintainer.domain.records import JobStatus
from auto_maintainer.domain.settings import LabMachine, machine_problems
from auto_maintainer.labs.executor import LabExecutor
from auto_maintainer.testing.demo import build_demo
from auto_maintainer.testing.fakes import FakeLabHost
from auto_maintainer.web.app import create_app

REPO = "acme/widget"


# -- the machine -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("machine", "problem"),
    [
        (LabMachine(host="-oProxyCommand=touch /tmp/x"), "HostName"),
        (LabMachine(host="gpu 01"), "HostName"),
        (LabMachine(workdir="../elsewhere"), "without '..'"),
        (LabMachine(workdir="/"), "folder of its own"),
        (LabMachine(forbid=["("]), "not a regular expression"),
        (LabMachine(repos=["widget"]), "is not owner"),
    ],
)
def test_a_machine_that_cannot_be_used_safely_is_refused(
    machine: LabMachine, problem: str
) -> None:
    assert any(problem in p for p in machine_problems("gpu-01", machine))


def test_a_plain_machine_checks() -> None:
    assert machine_problems("gpu-01", LabMachine(host="ci@gpu-01.lab")) == []


async def _executor(
    tmp_path: Path, machine: LabMachine, host: FakeLabHost
) -> LabExecutor:
    from auto_maintainer.store.sqlite import SqliteStore

    store = await SqliteStore.open(tmp_path / "am.db")
    executor = LabExecutor({}, {}, store, FixedClock())
    executor.configure({"gpu-01": machine})
    executor.hosts["gpu-01"] = host
    return executor


def _payload(**kw: Any) -> LabReproducePayload:
    data: dict[str, Any] = {
        "lab": "gpu-01",
        "summary": "a batch of one item",
        "steps": ["install it", "run the check on base and head"],
        "expect": "base fails, head passes",
    }
    data.update(kw)
    return LabReproducePayload.model_validate(data)


async def test_a_session_runs_each_command_in_its_sides_folder_and_is_removed(
    tmp_path: Path,
) -> None:
    host = FakeLabHost("gpu-01")
    secret = "ghp_" + "c" * 36
    host.results = [
        CommandResult(exit_code=0, stdout="/home/ci"),
        CommandResult(exit_code=0),
        CommandResult(exit_code=0),
        CommandResult(exit_code=1, stdout=f"FAILED test_one\ntoken {secret}"),
        CommandResult(exit_code=0, stdout="1 passed"),
    ]
    machine = LabMachine(setup="module load cuda/12.4", timeout_seconds=3600)
    executor = await _executor(tmp_path, machine, host)
    used = LabBinding(setup="source ~/venv/bin/activate", timeout_seconds=600)
    heard: list[tuple[str | None, str, str]] = []

    async def progress(p) -> None:  # type: ignore[no-untyped-def]
        heard.append((p.side, p.phase, p.line))

    opened = await executor.open_session(
        _payload(timeout_seconds=1800),
        REPO,
        42,
        {"base": "QkFTRQ==", "head": "SEVBRA=="},
        "act_12345678",
        binding=used,
        progress=progress,
    )
    run_dir = "/home/ci/am-labs/acme__widget-42-12345678"
    assert host.stdin == ["QkFTRQ==", "SEVBRA=="]
    assert f"mkdir -p {run_dir}/base" in host.commands[1][0]
    assert "token" not in opened, "the token stays in memory"
    token = executor.session_token(opened["session"])
    assert token and executor.session_info(token)["seconds_left"] == 600
    lines: list[str] = []

    async def on_line(line: str) -> None:
        lines.append(line)

    failed = await executor.run_in_session(
        token, "pytest -q -k one", side="base", on_line=on_line
    )
    assert failed["exit_code"] == 1 and lines[0] == "FAILED test_one"
    command, cwd, _ = host.commands[3]
    # the machine's setup, then the repository's, then the agent's command
    assert (
        command == "module load cuda/12.4\nsource ~/venv/bin/activate\npytest -q -k one"
    )
    assert cwd == f"{run_dir}/base"
    assert (await executor.run_in_session(token, "python check.py"))["exit_code"] == 0
    assert host.commands[4][1] == f"{run_dir}/head", "head by default"
    assert ("base", "$ pytest -q -k one", "") in heard
    closed = await executor.close_session(opened["session"])
    assert closed is not None
    assert host.commands[-1][0] == f"rm -rf {run_dir}"
    assert [c["exit_code"] for c in closed["commands"]] == [1, 0]
    assert secret not in str(closed), "output kept is scrubbed"
    assert executor.session_token(opened["session"]) is None
    assert "error" in await executor.run_in_session(token, "ls")


async def test_a_command_the_machines_rules_refuse_never_reaches_it(
    tmp_path: Path,
) -> None:
    host = FakeLabHost("gpu-01")
    host.results = [CommandResult(exit_code=0, stdout="/home/ci")]
    executor = await _executor(tmp_path, LabMachine(forbid=[r"\bdocker\b"]), host)
    used = LabBinding(guard={"forbid": [r"\bwget\b"]}, protected_paths=["/data/ckpt"])
    opened = await executor.open_session(
        _payload(), REPO, 42, {"base": "", "head": ""}, "act_1", binding=used
    )
    token = executor.session_token(opened["session"])
    assert token
    before = len(host.commands)
    for command in (
        "sudo rm -rf build",
        "ray stop --force",
        "docker run x",
        "wget http://example.com/x",
        "cp out.pt /data/ckpt/latest.pt",
    ):
        out = await executor.run_in_session(token, command)
        assert out["refused"] and out["exit_code"] == 126, command
    assert len(host.commands) == before


async def test_a_machine_for_other_repositories_or_without_a_free_gpu_runs_nothing(
    tmp_path: Path,
) -> None:
    host = FakeLabHost(
        "gpu-01", gpus=[GpuStatus(index=0, memory_used_mib=40000, processes=1)]
    )
    executor = await _executor(tmp_path, LabMachine(repos=["acme/other"]), host)
    with pytest.raises(RuntimeError, match="not offered to acme/widget"):
        await executor.open_session(
            _payload(), REPO, 42, {}, "act_1", binding=LabBinding()
        )
    executor = await _executor(tmp_path / "2", LabMachine(), host)
    for unused in (None, LabBinding(use=False)):
        with pytest.raises(RuntimeError, match="does not use"):
            await executor.open_session(
                _payload(), REPO, 42, {}, "act_1", binding=unused
            )
    host.results = [CommandResult(exit_code=0, stdout="/home/ci")]
    out = await executor.open_session(
        _payload(needs_gpu=True),
        REPO,
        42,
        {"base": "", "head": ""},
        "act_1",
        binding=LabBinding(),
    )
    assert out["deferred"] and not host.stdin


async def test_a_session_ends_at_its_time_limit_and_after_a_restart(
    tmp_path: Path,
) -> None:
    host = FakeLabHost("gpu-01")
    host.results = [CommandResult(exit_code=0, stdout="/home/ci")]
    executor = await _executor(tmp_path, LabMachine(), host)
    first = await executor.open_session(
        _payload(timeout_seconds=300, sides=["head"]),
        REPO,
        42,
        {"head": ""},
        "act_aaaaaaaa",
        binding=LabBinding(),
    )
    token = executor.session_token(first["session"])
    assert token
    executor.clock.advance(seconds=301)  # type: ignore[attr-defined]
    assert "time is up" in (await executor.run_in_session(token, "ls"))["error"]
    assert await executor.expire_sessions() == 1
    assert host.commands[-1][0].startswith("rm -rf ")
    host.results = [CommandResult(exit_code=0, stdout="/home/ci")]
    kept = await executor.open_session(
        _payload(sides=["head"], timeout_seconds=600),
        REPO,
        43,
        {"head": ""},
        "act_bbbbbbbb",
        binding=LabBinding(),
        job="job_1",
    )
    old_token = executor.session_token(kept["session"])
    assert old_token
    host.results = [CommandResult(exit_code=0, stdout="built")]
    await executor.run_in_session(old_token, "make build")

    # A restart: a new process adopts the session that has time left, under a new
    # token, with its folder and what ran in it; its output still reaches the job.
    seen: list[tuple[str, str]] = []

    async def sink(job: str, repo: str, number: Any, lab: str, step: Any) -> None:
        seen.append((job, step.phase or step.line))

    again = LabExecutor({}, {}, executor.store, executor.clock)
    again.configure({"gpu-01": LabMachine()})
    again.hosts["gpu-01"] = host
    again.progress_sink = sink
    assert await again.recover_sessions() == 0, "nothing to clean up"
    token = again.session_token(kept["session"])
    assert token and token != old_token
    assert "error" in await again.run_in_session(old_token, "ls"), "the old token"
    host.results = [CommandResult(exit_code=0)]
    await again.run_in_session(token, "make test")
    assert host.commands[-1][1] == "/home/ci/am-labs/acme__widget-43-bbbbbbbb/head"
    assert seen and seen[0][0] == "job_1"
    closed = await again.close_session(kept["session"])
    assert [c["command"] for c in closed["commands"]] == ["make build", "make test"]

    # A session whose time ran out by the restart is cleaned up instead.
    host.results = [CommandResult(exit_code=0, stdout="/home/ci")]
    await again.open_session(
        _payload(sides=["head"], timeout_seconds=120),
        REPO,
        44,
        {"head": ""},
        "act_cccccccc",
        binding=LabBinding(),
    )
    again.clock.advance(seconds=90)  # type: ignore[attr-defined]
    third = LabExecutor({}, {}, executor.store, executor.clock)
    third.configure({"gpu-01": LabMachine()})
    third.hosts["gpu-01"] = host
    assert await third.recover_sessions() == 1, "under a minute left"
    assert host.commands[-1][0] == "rm -rf /home/ci/am-labs/acme__widget-44-cccccccc"


# -- the flow --------------------------------------------------------------------------


@pytest.fixture
async def demo(tmp_path: Path):
    h = await build_demo(tmp_path)
    h.forge.set_permission(REPO, "walt", Permission.WRITE)
    app = create_app(h.service, data_dir=tmp_path, allow_dev_login=True)
    yield h, app
    await h.close()


@contextlib.asynccontextmanager
async def _as(app: Any, login: str) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.get(f"/auth/dev?login={login}")
        client.headers["x-am-csrf"] = client.cookies.get("am_csrf") or ""
        yield client


async def test_only_service_maintainers_add_machines(demo) -> None:
    h, app = demo
    machine = {"host": "ci@a100-2", "description": "4× A100"}
    async with _as(app, "walt") as walt:
        proposed = await walt.put(
            "/api/settings/labs/a100-2", json={"machine": machine}
        )
        assert proposed.status_code == 200 and proposed.json()["approved"] is False
        assert "a100-2" not in h.service.machines
        bad = await walt.put(
            "/api/settings/labs/a100-2", json={"machine": {"host": "-oX=y"}}
        )
        assert bad.status_code == 422
    async with _as(app, "maria") as maria:
        applied = await maria.put(
            "/api/settings/labs/a100-2", json={"machine": machine}
        )
        assert applied.json()["approved"] is True
        assert h.service.machines["a100-2"].description == "4× A100"
        edited = await maria.put(
            "/api/settings/labs/a100-2",
            json={
                "machine": {
                    **machine,
                    "description": "8× A100",
                    "workdir": "/scratch/am",
                    "repos": [REPO],
                }
            },
        )
        assert edited.json()["approved"] is True
        assert h.service.machines["a100-2"].description == "8× A100"
        assert h.service.machines["a100-2"].workdir == "/scratch/am"
        assert h.service.machines["a100-2"].repos == [REPO]
        offered = (await maria.get(f"/api/labs?repo={REPO}")).json()["machines"]
        [added] = [m for m in offered if m["name"] == "a100-2"]
        assert added["used"] is False
        assert added["host"] == "ci@a100-2" and added["workdir"] == "/scratch/am"
        checked = (await maria.post("/api/settings/labs/a100-2/check")).json()
        assert checked["ok"] and checked["gpus"][0]["idle"]
        await maria.put(
            "/api/settings/labs/other-01",
            json={"machine": {"host": "ci@other-01", "repos": ["acme/other"]}},
        )
    async with _as(app, "walt") as walt:
        checked = await walt.post("/api/settings/labs/a100-2/check")
        assert checked.status_code == 200 and checked.json()["ok"]
        denied = await walt.post("/api/settings/labs/other-01/check")
        assert denied.status_code == 403


async def test_a_reproduction_asked_for_runs_at_once_in_its_session(demo) -> None:
    """A person's request is the approval: the skill's reproduction agent starts on
    the machine at once, reads its skill and what was asked, and works through
    ``lab``; its record comes back to the person who asked."""
    h, app = demo
    runner = h.service.a.lab_runner
    async with _as(app, "maria") as maria:
        started = await maria.post(
            f"/api/prs/{REPO}/42/reproduce",
            json={"lab": "gpu-01", "note": "the batch of one", "agent": "codex"},
        )
        assert started.status_code == 200, started.text
        await h.settle()
        [opened] = runner.reproduced
        assert set(opened["code"]) == {"base", "head"}
        [record] = await h.store.list_actions(
            repo=REPO, number=42, kinds=["lab.reproduce"]
        )
        assert record.approver == "maria", "her request approved it"
        assert not [
            p
            for p in (await maria.get("/api/proposals")).json()
            if p["action"]["action"]["kind"] == "lab.reproduce"
        ], "nothing waits for approval"
        [worker] = [t for t in h.runner.tasks if t.kind == "reproduce"]
        assert worker.lab_session == f"token-{opened['session']}"
        assert worker.env["AM_LAB_SOCKET"], "the agent reaches its session"
        assert worker.mode == "workspace_write"
        assert "You are the reproduction agent" in worker.prompt
        assert "SKILL.md" in worker.prompt, "its skill says what it does"
        assert "the batch of one" in worker.prompt
        assert "lab -- 'COMMAND'" in worker.prompt and "lab --gpus N" in worker.prompt
        assert "source ~/envs/widget/bin/activate" in worker.prompt, "the setup"
        assert "When only maria can settle something" in worker.prompt
        assert worker.requested_by == "maria", "its questions go to maria"
        assert runner.closed == [opened["session"]], (
            "the session closes when it is done"
        )
        [job] = [j for j in await h.store.list_jobs() if j.playbook == "reproduce"]
        progress = await h.service.progress.steps(job.id)
        assert [s.text for s in progress if s.kind == "output"] == [
            "gpu-01 · base",
            "gpu-01 · head",
        ], "the job shows the machine's output as it comes"
        assert job.status == JobStatus.DONE
        assert job.result and job.result["conclusion"] == "reproduced"
        assert job.result["commands"][0]["command"] == "python check.py"
        [report] = [
            p
            for p in (await maria.get("/api/proposals")).json()
            if p["action"]["action"]["kind"] == "comment.upsert"
            and p["requested_by"] == "maria"
        ]
        assert "batch of one" in report["action"]["action"]["payload"]["body"]


async def test_an_agent_that_fails_for_now_leaves_its_session_for_the_next_try(
    demo,
) -> None:
    from auto_maintainer.domain.agents import AgentResult

    h, app = demo
    runner = h.service.a.lab_runner
    calls: list[int] = []

    def agent(task: Any) -> Any:
        calls.append(1)
        if len(calls) == 1:
            return AgentResult(
                runtime="scripted", ok=False, error="rate limited", error_kind="quota"
            )
        return {"conclusion": "reproduced", "comment": "Reproduced."}

    h.runner.on("reproduce", agent)
    async with _as(app, "maria") as maria:
        await maria.post(f"/api/prs/{REPO}/42/reproduce", json={"lab": "gpu-01"})
        await h.settle()
    [opened] = runner.reproduced
    assert runner.closed == [] and opened["session"] in runner.sessions


async def test_a_repository_that_turns_reproductions_off_runs_none(demo) -> None:
    """A request approves its reproduction, but the repository's rule still decides:
    ``off`` runs nothing, and the job says so."""
    h, app = demo
    from auto_maintainer.domain.memory import profile_path
    from auto_maintainer.profiles.layers import dump_yaml, parse_yaml

    path = profile_path(REPO)
    layer = parse_yaml(h.memory.files[path], path)
    layer.setdefault("policy", {})["lab.reproduce"] = {"level": "off"}
    h.memory.files[path] = dump_yaml(layer)
    await h.approve_directly(REPO, "maria")
    async with _as(app, "maria") as maria:
        await maria.post(f"/api/prs/{REPO}/42/reproduce", json={"lab": "gpu-01"})
        await h.settle()
    [job] = [j for j in await h.store.list_jobs() if j.playbook == "reproduce"]
    assert job.status == JobStatus.DONE and job.result
    assert job.result["skipped"] == "the repository's settings refused it"
    assert h.service.a.lab_runner.reproduced == []


# -- each repository decides --------------------------------------------------------------


async def test_each_repository_turns_machines_on_and_sets_how_it_uses_them(
    demo,
) -> None:
    h, app = demo
    async with _as(app, "maria") as maria:
        view = (await maria.get(f"/api/labs?repo={REPO}")).json()
        used = {m["name"]: m for m in view["machines"]}
        assert used["gpu-01"]["used"] and not used["cpu-01"]["used"]
        assert used["gpu-01"]["setup"] == "source ~/envs/widget/bin/activate"
        assert view["can_configure"] is True
        refused = await maria.post(
            f"/api/prs/{REPO}/42/reproduce", json={"lab": "cpu-01"}
        )
        assert refused.status_code == 409 and "does not use" in refused.text
    body = {
        "use": True,
        "setup": "source ~/envs/cpu/bin/activate",
        "timeout_seconds": 600,
        "forbid": [r"\bwget\b"],
    }
    async with _as(app, "walt") as walt:
        assert (await walt.get(f"/api/labs?repo={REPO}")).json()[
            "can_configure"
        ] is False
        proposed = (await walt.put(f"/api/repos/{REPO}/labs/cpu-01", json=body)).json()
        assert proposed["applied"] is False
        bad = await walt.put(
            f"/api/repos/{REPO}/labs/cpu-01", json={**body, "forbid": ["("]}
        )
        assert bad.status_code == 422
    profile = await h.service.profiles.active(REPO)
    assert profile is not None and "cpu-01" not in profile.labs
    async with _as(app, "maria") as maria:
        applied = (await maria.put(f"/api/repos/{REPO}/labs/cpu-01", json=body)).json()
        assert applied["applied"] is True
        profile = await h.service.profiles.active(REPO)
        assert profile is not None
        binding = profile.labs["cpu-01"]
        assert binding.use and binding.timeout_seconds == 600
        assert binding.setup == "source ~/envs/cpu/bin/activate"
        assert binding.guard.forbid == [r"\bwget\b"]
        started = await maria.post(
            f"/api/prs/{REPO}/42/reproduce", json={"lab": "cpu-01"}
        )
        assert started.status_code == 200, started.text
        await h.settle()
        [worker] = [t for t in h.runner.tasks if t.kind == "reproduce"]
        assert "source ~/envs/cpu/bin/activate" in worker.prompt
        assert "at most 10 minutes" in worker.prompt
        off = await maria.put(f"/api/repos/{REPO}/labs/gpu-01", json={"use": False})
        assert off.json()["applied"] is True
        offered = (await maria.get(f"/api/labs?repo={REPO}")).json()["machines"]
        assert not [m for m in offered if m["name"] == "gpu-01"][0]["used"]
        pol = (await maria.get(f"/api/labs?repo={REPO}")).json()["review_policy"]
        assert pol["level"] == "auto", "reviews test every PR by default"
        rule = await maria.put(
            f"/api/repos/{REPO}/policy/lab.review",
            json={
                "level": "auto",
                "conditions": {"author": "team"},
                "otherwise": "off",
            },
        )
        assert rule.json()["applied"] is True
        pol = (await maria.get(f"/api/labs?repo={REPO}")).json()["review_policy"]
        assert (pol["level"], pol["conditions"]["author"], pol["otherwise"]) == (
            "auto",
            "team",
            "off",
        )


async def test_the_doctor_checks_the_machines_a_repository_uses(demo) -> None:
    from auto_maintainer.profiles.doctor import run_doctor

    h, _ = demo
    profile = await h.service.profiles.active(REPO)
    assert profile is not None
    machines = dict(h.service.machines)
    items = {i.check: i for i in await run_doctor(profile, h.forge, set(), machines)}
    assert items["lab gpu-01"].ok and "lab gpu-01 paths" not in items
    machines["gpu-01"] = machines["gpu-01"].model_copy(update={"repos": ["acme/other"]})
    items = {i.check: i for i in await run_doctor(profile, h.forge, set(), machines)}
    assert not items["lab gpu-01"].ok and "not offered" in items["lab gpu-01"].detail


# -- a review tests and reproduces its PR in its own session --------------------------------


async def _review_rule(h: Any, **rule: Any) -> None:
    """Set the repository's ``lab.review`` rule."""
    from auto_maintainer.domain.memory import profile_path
    from auto_maintainer.profiles.layers import dump_yaml, parse_yaml

    path = profile_path(REPO)
    layer = parse_yaml(h.memory.files[path], path)
    layer.setdefault("policy", {})["lab.review"] = rule
    h.memory.files[path] = dump_yaml(layer)
    await h.approve_directly(REPO, "maria")


async def _review(h: Any, client: httpx.AsyncClient, number: int) -> dict[str, Any]:
    """Ask for a review of ``number``; its draft, as the proposals list shows it."""
    await client.post("/api/reviews", json={"prs": [{"repo": REPO, "number": number}]})
    await h.settle()
    [draft] = [
        p
        for p in (await client.get("/api/proposals")).json()
        if p["action"]["action"]["kind"] == "review.post"
        and p["action"]["action"]["number"] == number
    ]
    return draft


async def test_a_review_tests_and_reproduces_its_pr_in_its_own_session(demo) -> None:
    h, app = demo
    runner = h.service.a.lab_runner
    async with _as(app, "maria") as maria:
        await _review(h, maria, 42)  # alice is not on the team: on by default
    [opened] = runner.reproduced
    assert set(opened["code"]) == {"base", "head"}
    assert opened["payload"].wants_gpu and not opened["payload"].needs_gpu
    [record] = await h.store.list_actions(repo=REPO, number=42, kinds=["lab.review"])
    assert record.status == "executed" and record.approver is None, "nobody approved"
    [review] = [t for t in h.runner.tasks if t.kind == "review"]
    assert review.lab_session == f"token-{opened['session']}"
    assert review.env["AM_LAB_SOCKET"] and review.min_timeout_seconds
    assert "Test machine gpu-01 is yours while you work" in review.prompt
    assert "lab --gpus N" in review.prompt, "it takes the GPUs it needs"
    assert "choose its reproduction agent" in review.prompt, "the service runs it"
    assert "Do not run the PR's tests" not in review.prompt
    assert runner.closed == [opened["session"]], "the session closes after the review"
    [job] = [j for j in await h.store.list_jobs() if j.playbook == "review"]
    assert job.result and job.result["tested_on"] == "gpu-01"
    assert job.result["commands"][0]["command"] == "python check.py"
    assert not [j for j in await h.store.list_jobs() if j.playbook == "reproduce"], (
        "no separate reproduction follows the review"
    )


async def test_a_review_that_needs_a_reproduction_starts_the_skills_agent(
    demo,
) -> None:
    """The reviewer writes the brief the skill describes; the service runs a fresh
    reproduction agent with it on the machine at once, under the rule the review's
    own session runs by, and its record goes into that review's draft."""
    h, app = demo
    runner = h.service.a.lab_runner
    answer = h.runner.handlers["review"]
    brief = "Follow docs/batching.md on 1 GPU; check a batch of one loads."
    h.runner.on("review", lambda t: {**answer(t), "reproduction": brief})
    async with _as(app, "maria") as maria:
        draft = await _review(h, maria, 42)
        assert not [
            p
            for p in (await maria.get("/api/proposals")).json()
            if p["action"]["action"]["kind"] in ("lab.review", "lab.reproduce")
        ], "nobody approves it"
    sessions = await h.store.list_actions(repo=REPO, number=42, kinds=["lab.review"])
    assert len(sessions) == 2, "the review's own session, then the reproduction's"
    assert all(r.status == "executed" for r in sessions)
    [worker] = [t for t in h.runner.tasks if t.kind == "reproduce"]
    assert brief in worker.prompt and "You are the reproduction agent" in worker.prompt
    assert len(runner.reproduced) == 2 and len(runner.closed) == 2
    [job] = [j for j in await h.store.list_jobs() if j.playbook == "reproduce"]
    assert job.result and job.result["revised"] == draft["action_id"]
    [update] = [t for t in h.runner.tasks if "That reproduction has ended" in t.prompt]
    assert brief in update.prompt, "the review is revised with its record"


async def test_a_restart_keeps_the_reviews_session_for_its_next_run(demo) -> None:
    """A deploy stops the service while a review's agent works in its session: the
    session stays, and the review that runs again on the next start resumes in it.
    A person stopping a review ends its session."""
    import asyncio

    h, app = demo
    runner = h.service.a.lab_runner
    engine = h.service.engine
    answer = h.runner.handlers["review"]
    working = asyncio.Event()
    hold = {42, 44}

    async def review(task: Any) -> Any:
        number = int(task.prompt.split("PR: #")[1].split(" ")[0])
        if number in hold:
            hold.discard(number)
            working.set()
            await asyncio.Event().wait()  # works until it is cancelled
        return answer(task)

    async def job_of(number: int) -> Any:
        return next(
            j
            for j in await h.store.list_jobs()
            if j.playbook == "review" and j.number == number
        )

    async def until(number: int, *states: str) -> Any:
        for _ in range(500):
            job = await job_of(number)
            if job.status in states:
                return job
            await asyncio.sleep(0.01)
        raise AssertionError(f"review of #{number} is {job.status}")

    h.runner.on("review", review)
    async with _as(app, "maria") as maria:
        await maria.post("/api/reviews", json={"prs": [{"repo": REPO, "number": 42}]})
    running = asyncio.create_task(h.settle())
    await asyncio.wait_for(working.wait(), 10)
    [opened] = runner.reproduced
    await engine.stop()  # the service stops for a deploy ...
    running.cancel()  # ... and cancels the review, as its job pool does
    with contextlib.suppress(asyncio.CancelledError):
        await running
    assert runner.closed == [], "the session waits for the review's next run"
    assert (await job_of(42)).status == JobStatus.QUEUED

    engine.start()  # the next start runs the review again
    job = await until(42, JobStatus.DONE, JobStatus.FAILED)
    first, again = [t for t in h.runner.tasks if t.kind == "review"]
    assert again.lab_session == first.lab_session == f"token-{opened['session']}"
    assert job.result and job.result["tested_on"] == "gpu-01"
    assert runner.closed == [opened["session"]]

    working.clear()
    async with _as(app, "maria") as maria:
        await maria.post("/api/reviews", json={"prs": [{"repo": REPO, "number": 44}]})
    await asyncio.wait_for(working.wait(), 10)
    second = runner.reproduced[-1]
    await engine.stop_job((await job_of(44)).id, "maria")
    assert runner.closed[-1] == second["session"], "a person's stop ends it"


async def test_a_repository_can_keep_outside_prs_off_its_machine(demo) -> None:
    h, app = demo
    await _review_rule(h, level="auto", conditions={"author": "team"}, otherwise="off")
    async with _as(app, "maria") as maria:
        await _review(h, maria, 42)  # alice: outside
        await _review(h, maria, 44)  # bob: on the team
    [opened] = h.service.a.lab_runner.reproduced
    assert opened["number"] == 44
    outside = next(
        t for t in h.runner.tasks if t.kind == "review" and t.lab_session is None
    )
    assert "do not let reviews of this PR run it on a test machine" in outside.prompt
    assert "Do not run the PR's tests" in outside.prompt


async def test_a_session_that_does_not_open_leaves_the_review_untested(demo) -> None:
    h, app = demo
    h.service.a.lab_runner.cannot_open = "no idle GPU on the machine"
    async with _as(app, "maria") as maria:
        await _review(h, maria, 44)
    [review] = [t for t in h.runner.tasks if t.kind == "review"]
    assert review.lab_session is None
    assert "gpu-01 could not be used (no idle GPU on the machine)" in review.prompt
    assert "say that no run was made in this review" in review.prompt


async def test_without_a_machine_the_review_says_no_run_was_made(demo) -> None:
    from auto_maintainer.domain.memory import profile_path
    from auto_maintainer.profiles.layers import dump_yaml, parse_yaml

    h, app = demo
    path = profile_path(REPO)
    layer = parse_yaml(h.memory.files[path], path)
    layer["labs"] = {"gpu-01": {"use": False}}
    h.memory.files[path] = dump_yaml(layer)
    await h.approve_directly(REPO, "maria")
    async with _as(app, "maria") as maria:
        await _review(h, maria, 44)
    [review] = [t for t in h.runner.tasks if t.kind == "review"]
    assert "No test machine is set up for this repository" in review.prompt
    assert not await h.store.list_actions(repo=REPO, number=44, kinds=["lab.review"])


# -- connections like ~/.ssh/config, and settings drafted from a description --------------


def test_a_connection_is_written_like_an_ssh_config_entry() -> None:
    from auto_maintainer.domain.settings import parse_ssh_config, ssh_config_text

    fields, problems = parse_ssh_config(
        "Host gpu-01\n  HostName 10.0.0.5\n  User ci\n  Port 2222\n"
        "  ProxyJump ops@bastion.example.com:22,jump2\n  IdentityFile ~/.ssh/id_ed25519\n"
    )
    assert problems == []
    machine = LabMachine(**fields)
    assert (machine.host, machine.user, machine.port) == ("10.0.0.5", "ci", 2222)
    assert machine_problems("gpu-01", machine) == []
    assert parse_ssh_config(ssh_config_text("gpu-01", machine))[0] == fields
    for refused in ("ProxyCommand nc %h %p", "LocalCommand id", "Match exec true"):
        assert parse_ssh_config(f"Host x\n  {refused}\n")[1], refused
    assert parse_ssh_config("Host x\n  Port 99999\n")[1]
    for bad in (
        LabMachine(host="h", proxy_jump="-oProxyCommand=id"),
        LabMachine(host="h", user="-l root"),
        LabMachine(host="h", identity_file="key file"),
    ):
        assert machine_problems("gpu-01", bad), bad


async def test_ssh_gets_the_connection_as_options(monkeypatch) -> None:
    from auto_maintainer.labs import hosts

    seen: list[list[str]] = []

    async def fake_run(argv, **kwargs):  # type: ignore[no-untyped-def]
        seen.append(argv)
        return type(
            "R",
            (),
            {
                "exit_code": 0,
                "stdout": "",
                "stderr": "",
                "duration_seconds": 0.0,
                "timed_out": False,
            },
        )()

    monkeypatch.setattr(hosts, "run", fake_run)
    host = hosts.SshLabHost(
        "gpu-01",
        "10.0.0.5",
        user="ci",
        port=2222,
        proxy_jump="bastion",
        identity_file="~/.ssh/k",
    )
    await host.run("true", timeout=5)
    argv = seen[0]
    for option in (
        "User=ci",
        "Port=2222",
        "ProxyJump=bastion",
        "IdentityFile=~/.ssh/k",
        "IdentitiesOnly=yes",
    ):
        assert option in argv
    assert argv[argv.index("--") + 1] == "10.0.0.5", (
        "the host comes after --, never as an option"
    )


async def test_a_machines_connection_is_saved_from_its_ssh_entry(demo) -> None:
    h, app = demo
    entry = "Host a100-3\n  HostName 10.0.0.7\n  User ci\n  Port 2200\n  ProxyJump bastion\n"
    async with _as(app, "maria") as maria:
        saved = await maria.put(
            "/api/settings/labs/a100-3",
            json={"machine": {"description": "4× A100"}, "ssh_config": entry},
        )
        assert saved.status_code == 200 and saved.json()["approved"] is True, saved.text
        refused = await maria.put(
            "/api/settings/labs/a100-3",
            json={
                "machine": {},
                "ssh_config": "Host a100-3\n  ProxyCommand nc %h %p\n",
            },
        )
        assert refused.status_code == 422
    machine = h.service.machines["a100-3"]
    assert (machine.host, machine.user, machine.port, machine.proxy_jump) == (
        "10.0.0.7",
        "ci",
        2200,
        "bastion",
    )


async def test_an_agent_drafts_what_the_service_enforces_from_the_description(
    demo,
) -> None:
    h, app = demo
    asked: list[str] = []

    def chat(task):  # type: ignore[no-untyped-def]
        asked.append(task.prompt)
        return {
            "workdir": "/data/am-runs",
            "setup": "conda activate rlinf\nexport CUDA_VISIBLE_DEVICES=0,1,2,3",
            "timeout_minutes": 120,
            "forbid": [r"\bray\s+stop\b", "/data/datasets"],
            "max_gpu_memory_mib": 1024,
            "max_gpu_util_pct": 5,
            "left_to_agent": ["prefer small batch sizes"],
        }

    h.runner.on("chat", chat)
    description = (
        "Activate the conda environment rlinf. Runs go in /data/am-runs and use GPUs 0-3. "
        "Never run ray stop or write to /data/datasets. Two hours at most. Prefer small batch sizes."
    )
    body = {
        "machine": {"description": description},
        "ssh_config": "Host gpu-09\n  HostName 10.0.0.9\n",
    }
    async with _as(app, "walt") as walt:
        assert (
            await walt.post("/api/settings/labs/gpu-09/draft", json=body)
        ).status_code == 403
    async with _as(app, "maria") as maria:
        drafted = (
            await maria.post("/api/settings/labs/gpu-09/draft", json=body)
        ).json()
        assert "gpu-09" not in h.service.machines, "a draft saves nothing"
        assert description in asked[0]
        machine = drafted["machine"]
        assert machine["setup"].startswith("conda activate rlinf")
        assert machine["timeout_seconds"] == 7200 and machine["host"] == "10.0.0.9"
        assert drafted["problems"] == [] and drafted["left_to_agent"] == [
            "prefer small batch sizes"
        ]
        assert "/data/am-runs" in drafted["diff"]
        saved = await maria.put(
            "/api/settings/labs/gpu-09",
            json={"machine": machine, "ssh_config": body["ssh_config"]},
        )
        assert saved.json()["approved"] is True
    assert h.service.machines["gpu-09"].forbid == [r"\bray\s+stop\b", "/data/datasets"]


async def test_a_run_on_a_test_machine_does_not_hold_up_posting(demo) -> None:
    """A reproduction can hold a machine for an hour; labels, reviews, and comments
    approved meanwhile for the same repository go out without waiting for it."""
    import asyncio

    from auto_maintainer.domain.actions import ActionKind, LabelPayload, make_action

    h, app = demo
    runner = h.service.a.lab_runner
    machine_free = asyncio.Event()
    finish = runner.open_session

    async def slow_open(*args, **kwargs):
        await machine_free.wait()
        return await finish(*args, **kwargs)

    runner.open_session = slow_open
    async with _as(app, "maria") as maria:
        await maria.post(f"/api/prs/{REPO}/42/reproduce", json={"lab": "gpu-01"})
    # The request runs at once; the machine holds its session until it is free.
    settling = asyncio.create_task(h.settle())

    async def lab_record() -> Any:
        found = await h.store.list_actions(
            repo=REPO, number=42, kinds=["lab.reproduce"]
        )
        return found[0] if found else None

    for _ in range(200):
        record = await lab_record()
        if record is not None and record.status == "intent":
            break
        await asyncio.sleep(0.02)
    pr = await h.forge.get_pr(REPO, 42)
    assert pr is not None
    await h.service.pipeline.submit(
        make_action(
            ActionKind.LABEL_APPLY,
            REPO,
            LabelPayload(labels=["needs-gpu-check"]),
            number=42,
            head_sha=pr.head_sha,
            discriminator="while the machine runs",
        )
    )

    async def label_status() -> str:
        [record] = [
            r
            for r in await h.store.list_actions(repo=REPO, number=42, limit=50)
            if r.action.kind == "label.apply"
            and "needs-gpu-check" in str(r.action.payload)
        ]
        return record.status

    for _ in range(100):
        if await label_status() not in ("queued", "intent"):
            break
        await asyncio.sleep(0.02)
    assert await label_status() not in ("queued", "intent"), (
        "the label waited for the machine"
    )
    lab = await lab_record()
    assert lab is not None and lab.status == "intent", "the run is still going"
    machine_free.set()
    await settling
    await h.settle()
    assert len(runner.reproduced) == 1


async def test_actions_waiting_in_the_queue_at_a_restart_go_out_after_it(demo) -> None:
    """The queue lives in memory: an allowed action still waiting in it when the
    service stops runs once the service starts again, after any it was running."""
    from auto_maintainer.domain.actions import (
        ActionKind,
        ActionStatus,
        LabelPayload,
        make_action,
    )
    from auto_maintainer.domain.records import ActionRecord

    h, _app = demo
    pr = await h.forge.get_pr(REPO, 42)
    assert pr is not None
    waiting = []
    for i, label in enumerate(["needs review", "community"]):
        action = make_action(
            ActionKind.LABEL_APPLY,
            REPO,
            LabelPayload(labels=[label]),
            number=42,
            head_sha=pr.head_sha,
            discriminator=f"left in the queue {i}",
        )
        record, _ = await h.store.add_action(
            ActionRecord(action=action, status=ActionStatus.QUEUED, approver="walt")
        )
        waiting.append(record.action.id)
    await h.service.actuator.reconcile_intents()
    await h.service.actuator.drain()
    for action_id in waiting:
        record = await h.store.get_action(action_id)
        assert record is not None and record.status in ("executed", "noop"), (
            record.status if record else None
        )


async def test_a_reproduction_asked_for_goes_into_the_waiting_review(demo) -> None:
    h, app = demo
    async with _as(app, "maria") as maria:
        draft = await _review(h, maria, 42)
        await maria.post(f"/api/prs/{REPO}/42/reproduce", json={"lab": "gpu-01"})
        await h.settle()
    comments = await h.store.list_actions(
        repo=REPO, number=42, kinds=["comment.upsert"]
    )
    assert not [
        c for c in comments if c.action.payload.get("purpose") == "reproduction"
    ]
    [update] = [t for t in h.runner.tasks if "That reproduction has ended" in t.prompt]
    assert "python check.py" in update.prompt, "the run's record reaches the review"
    [job] = [j for j in await h.store.list_jobs() if j.playbook == "reproduce"]
    assert job.result and job.result["revised"] == draft["action_id"]


async def test_an_answer_given_earlier_in_a_job_reaches_its_next_agent(demo) -> None:
    from auto_maintainer.domain.agents import (
        AgentAnswer,
        AgentQuestion,
        AgentTask,
        InputQuestion,
        QuestionKind,
        QuestionState,
        TaskKind,
    )
    from auto_maintainer.domain.events import Event, EventKind
    from auto_maintainer.domain.records import Job

    h, _app = demo
    job = Job(playbook="reproduce", key="k", repo=REPO, number=42)
    await h.store.save_question(
        AgentQuestion(
            kind=QuestionKind.INPUT,
            title="Which scope?",
            inputs=[InputQuestion(id="0", question="Which scope?", options=["head"])],
            repo=REPO,
            job_id=job.id,
            state=QuestionState.ANSWERED,
            answer=AgentAnswer(allow=True, answers={"0": ["head only"]}, by="maria"),
        )
    )
    ctx = await h.service.context_for(
        job, Event(kind=EventKind.DASHBOARD_COMMAND, repo=REPO, number=42)
    )
    h.runner.on("reproduce", lambda task: {"summary": "s", "steps": ["x"]})
    await ctx.ask(
        AgentTask(kind=TaskKind.REPRODUCE, prompt="Plan it.", workdir=Path("."))
    )
    prompt = h.runner.tasks[-1].prompt
    assert (
        "Q: Which scope?\nA: head only" in prompt and "do not ask these again" in prompt
    )


async def test_a_session_the_policy_only_records_does_not_hold_the_review(
    demo,
) -> None:
    h, app = demo
    h.service.pipeline.dev_mode = True  # a development instance: shadow at most
    async with _as(app, "maria") as maria:
        await maria.post("/api/reviews", json={"prs": [{"repo": REPO, "number": 44}]})
        await h.settle()
    [record] = await h.store.list_actions(repo=REPO, number=44, kinds=["lab.review"])
    assert record.status == "shadowed"
    [job] = [j for j in await h.store.list_jobs() if j.playbook == "review"]
    assert job.status == "done", "the review goes on at once, without waiting an hour"
    assert job.result and "only records such runs" in job.result["not_tested"]
    assert h.service.a.lab_runner.reproduced == []


async def test_a_reviews_reproduction_stored_before_the_change_ends_cleanly(
    demo,
) -> None:
    """Jobs a review started before reviews tested in their own session still load
    after the upgrade and end, instead of failing on every try."""
    from auto_maintainer.domain.events import Event, EventKind
    from auto_maintainer.domain.records import Job

    h, _ = demo
    old = Event(
        kind=EventKind.REVIEW_REPRODUCE,
        repo=REPO,
        number=42,
        payload={"lab": "gpu-01", "review": "act_1"},
    )
    job, _ = await h.store.upsert_job(
        Job(
            playbook="reproduce",
            key=f"reproduce:{REPO}#42:review:act_1",
            repo=REPO,
            number=42,
            pool="agent",
            events=[old.model_dump(mode="json")],
        )
    )
    await h.settle()
    done = await h.store.get_job(job.id)
    assert done is not None and done.status == "done"
    assert done.result and "ask for one again" in done.result["skipped"]

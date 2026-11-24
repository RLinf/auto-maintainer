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
    keep = "/home/ci/am-labs/keep/acme__widget-42"
    assert host.stdin == ["QkFTRQ==", "SEVBRA=="]
    assert f"mkdir -p {run_dir}/base" in host.commands[1][0]
    assert f"mkdir -p {keep} && touch {keep}/.last_used" in host.commands[1][0]
    assert (
        executor.session_info(executor.session_token(opened["session"]))["keep_dir"]
        == keep
    )
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
    env = host.commands[3][2]
    assert env["AM_LAB_RUN"] == opened["session"] and env["AM_LAB_KEEP"] == keep, (
        "what it starts carries the session's id, never its token"
    )
    assert token not in str(env)
    assert (await executor.run_in_session(token, "python check.py"))["exit_code"] == 0
    assert host.commands[4][1] == f"{run_dir}/head", "head by default"
    assert ("base", "$ pytest -q -k one", "") in heard
    closed = await executor.close_session(opened["session"])
    assert closed is not None
    stop = host.commands[-1][0]
    assert f"AM_LAB_RUN={opened['session']}" in stop and "kill -TERM" in stop, (
        "what the session started is stopped"
    )
    assert stop.endswith(f"rm -rf {run_dir}") and keep not in stop
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
    assert host.commands[-1][0].endswith(
        "rm -rf /home/ci/am-labs/acme__widget-42-aaaaaaaa"
    )
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
    assert host.commands[-1][0].endswith(
        "rm -rf /home/ci/am-labs/acme__widget-44-cccccccc"
    )
    assert "AM_LAB_RUN=lab_" in host.commands[-1][0], "stopped by its tag"


# -- the flow --------------------------------------------------------------------------


@pytest.fixture
async def demo(tmp_path: Path):
    h = await build_demo(tmp_path)
    h.forge.set_permission(REPO, "walt", Permission.WRITE)
    # walt, a reviewer, may run outside contributors' PRs on the service's machines
    # here (design 2.7: by default only maintainers do).
    h.memory.files["config/access.yaml"] = (
        "people: {walt: {grants: {machines.outside_prs: true}}}\n"
    )
    await h.service.reload_access()
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


async def test_a_second_reproduction_starts_from_what_the_first_did(demo) -> None:
    """The next session of a PR is told what earlier ones ran and concluded, and
    where the PR's environments are kept, instead of finding it all out again."""
    h, app = demo
    async with _as(app, "maria") as maria:
        for note in ("the batch of one", "the batch of two"):
            started = await maria.post(
                f"/api/prs/{REPO}/42/reproduce", json={"lab": "gpu-01", "note": note}
            )
            assert started.status_code == 200, started.text
            await h.settle()
    first, second = [t for t in h.runner.tasks if t.kind == "reproduce"]
    assert "What earlier agents did on test machines" not in first.prompt
    assert "What earlier agents did on test machines for this PR" in second.prompt
    assert "Conclusion: reproduced" in second.prompt
    assert "python check.py" in second.prompt, "the commands it ran"
    assert "(the PR's head now)" in second.prompt, "at the same commit"
    assert "$AM_LAB_KEEP" in second.prompt, "where its environments are kept"
    [done, _] = [j for j in await h.store.list_jobs() if j.playbook == "reproduce"]
    assert done.result and done.result["tested_on"] == "gpu-01"
    assert done.result["commit"]


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


def _review_sessions(runner: Any) -> list[dict[str, Any]]:
    """The sessions reviews opened, apart from their reproductions'."""
    return [o for o in runner.reproduced if o["payload"].summary.startswith("Review #")]


def _reviews(h: Any) -> list[Any]:
    """The review agents' first turns, apart from their last turns with a
    reproduction's record and revisions of their drafts."""
    return [
        t
        for t in h.runner.tasks
        if t.kind == "review"
        and "That reproduction has ended" not in t.prompt
        and "The reproduction you started has ended" not in t.prompt
        and not t.prompt.startswith("You judge the quality of PR")
    ]


BRIEF = "Follow docs/start.md as a user would, on one GPU, with the e2e config."


def _starts_reproduction(h: Any) -> list[dict[str, Any]]:
    """Have the review's agent start the skill's reproduction from its session, as
    `lab --reproduce` does, then answer as the demo's agent does. Returns what the
    service told it."""
    answer = h.runner.handlers["review"]
    told: list[dict[str, Any]] = []

    async def reviewing(task: Any) -> Any:
        if task.lab_session and "lab --reproduce" in task.prompt and not told:
            told.append(
                await h.service.start_review_reproduction(
                    {
                        "job": task.job_id,
                        "repo": task.repo,
                        "number": task.pr,
                        "lab": "gpu-01",
                    },
                    BRIEF,
                )
            )
        return answer(task)

    h.runner.on("review", reviewing)
    return told


def _revisions(h: Any) -> list[Any]:
    """Revision turns triggered after a reproduction finishes."""
    return [
        t
        for t in h.runner.tasks
        if t.kind == "review" and "That reproduction has ended" in t.prompt
    ]


async def test_a_review_tests_its_pr_in_its_own_session(demo) -> None:
    h, app = demo
    runner = h.service.a.lab_runner
    async with _as(app, "maria") as maria:
        await _review(h, maria, 42)  # alice is not on the team: on by default
    [opened] = _review_sessions(runner)
    assert set(opened["code"]) == {"base", "head"}
    assert not opened["payload"].wants_gpu and not opened["payload"].needs_gpu, (
        "no GPU until a check needs one: its reproduction may need them all"
    )
    records = await h.store.list_actions(repo=REPO, number=42, kinds=["lab.review"])
    assert {(r.status, r.approver) for r in records} == {("executed", None)}, (
        "nobody approved the review's session or its reproduction's"
    )
    [review] = _reviews(h)
    assert review.lab_session == f"token-{opened['session']}"
    assert review.env["AM_LAB_SOCKET"] and review.min_timeout_seconds
    assert "Test machine gpu-01 is yours while you work" in review.prompt
    assert "lab --gpus N" in review.prompt, "it takes the GPUs it needs"
    assert "Use the machine to test what you read" in review.prompt
    assert (
        "start the skill's reproduction agent with `lab --reproduce`" in review.prompt
    )
    assert "Do not run the PR's code here" not in review.prompt
    assert not [t for t in h.runner.tasks if t.kind == "reproduce"], (
        "a review whose agent starts none runs none"
    )
    assert opened["session"] in runner.closed, "the session closes after the review"
    [job] = [j for j in await h.store.list_jobs() if j.playbook == "review"]
    assert job.result and job.result["tested_on"] == "gpu-01"
    assert job.result["commands"][0]["command"] == "python check.py"


async def test_the_reviewer_starts_the_reproduction_and_revises_draft_later(
    demo,
) -> None:
    """The review's agent judges that the PR needs a reproduction and starts the
    skill's reproduction agent from its session: the review finishes immediately and
    creates its draft without waiting, and once the reproduction finishes later, an
    agent revises the whole review draft freely with its results and any newly found
    problems."""
    h, app = demo
    runner = h.service.a.lab_runner
    told = _starts_reproduction(h)
    async with _as(app, "maria") as maria:
        await _review(h, maria, 42)
        assert not [
            p
            for p in (await maria.get("/api/proposals")).json()
            if p["action"]["action"]["kind"] in ("lab.review", "lab.reproduce")
        ], "nobody approves it"
    assert told and told[0]["started"]
    sessions = await h.store.list_actions(repo=REPO, number=42, kinds=["lab.review"])
    assert len(sessions) == 2, "the review's own session, and the reproduction's"
    assert all(r.status == "executed" for r in sessions)
    [worker] = [t for t in h.runner.tasks if t.kind == "reproduce"]
    assert "You are the reproduction agent" in worker.prompt
    assert BRIEF in worker.prompt, "it follows the reviewer's brief"
    [_first] = _reviews(h)
    [rev] = _revisions(h)
    assert "python check.py" in rev.prompt, "the revision gets the run's record"
    assert "do not limit your edits to the reproduction section" in rev.prompt
    assert "Keep everything else word for word" not in rev.prompt
    [review] = [j for j in await h.store.list_jobs() if j.playbook == "review"]
    [job] = [j for j in await h.store.list_jobs() if j.playbook == "reproduce"]
    assert job.result and job.result["for_review"] == review.id
    assert job.result.get("revised"), "reproduction revised the review draft"
    assert review.status == JobStatus.DONE
    assert len(runner.reproduced) == 2 and len(runner.closed) == 2
    again = await h.service.start_review_reproduction(
        {"job": review.id, "lab": "gpu-01"}, BRIEF
    )
    assert "error" in again, "only a review that is working starts one"


async def test_a_review_nobody_asked_for_tests_and_reproduces_team_work(demo) -> None:
    """A scan's review of a team member's PR gets the machine too, and its agent may
    start the reproduction, which runs for nobody: the machine takes team code."""
    from auto_maintainer.domain.events import Event, EventKind

    h, _ = demo
    told = _starts_reproduction(h)
    await h.emit(
        Event(
            kind=EventKind.DASHBOARD_COMMAND,
            repo=REPO,
            number=44,
            payload={"command": "review", "source": "scan"},
        )
    )
    [review] = _reviews(h)
    assert "lab --reproduce" in review.prompt
    assert told and told[0]["started"]
    [job] = [j for j in await h.store.list_jobs() if j.playbook == "reproduce"]
    assert job.events[0]["payload"]["by"] is None, "nobody asked for it"
    assert [t for t in h.runner.tasks if t.kind == "reproduce"]
    [rev] = _revisions(h)
    assert "python check.py" in rev.prompt


async def test_outside_code_runs_only_for_someone_who_may_run_it(demo) -> None:
    """A scan's review of an outside contributor's PR gets no session, since nobody
    who may run outside code asked for it, so its agent can start no reproduction."""
    from auto_maintainer.domain.events import Event, EventKind

    h, _ = demo
    await h.emit(
        Event(
            kind=EventKind.DASHBOARD_COMMAND,
            repo=REPO,
            number=42,
            payload={"command": "review", "source": "scan"},
        )
    )
    [review] = _reviews(h)
    assert not review.lab_session and "lab --reproduce" not in review.prompt
    assert "outside the team" in review.prompt
    assert not [t for t in h.runner.tasks if t.kind == "reproduce"]


async def test_a_reproduction_the_pr_needs_not_is_said_in_the_review(demo) -> None:
    h, app = demo
    h.runner.on(
        "reproduce",
        lambda t: {"conclusion": "not_needed", "comment": "Nothing user-facing."},
    )
    _starts_reproduction(h)
    async with _as(app, "maria") as maria:
        await _review(h, maria, 42)
    [job] = [j for j in await h.store.list_jobs() if j.playbook == "reproduce"]
    assert job.result and "needs no reproduction" in job.result["skipped"]
    [rev] = _revisions(h)
    assert "needs no reproduction" in rev.prompt and "Nothing user-facing" in rev.prompt


async def test_a_reproduction_that_does_not_run_is_said_in_the_review(demo) -> None:
    """When the machine cannot take the reproduction, the review still finishes, and
    the revision turn updates the draft with why no run happened."""
    h, app = demo
    runner = h.service.a.lab_runner
    answer = h.runner.handlers["review"]

    async def reviewing(task: Any) -> Any:
        if task.lab_session and "lab --reproduce" in task.prompt:
            runner.cannot_open = "every GPU is busy"
            await h.service.start_review_reproduction(
                {"job": task.job_id, "lab": "gpu-01"}, BRIEF
            )
        return answer(task)

    h.runner.on("review", reviewing)
    async with _as(app, "maria") as maria:
        await _review(h, maria, 42)
    assert not [t for t in h.runner.tasks if t.kind == "reproduce"]
    [rev] = _revisions(h)
    assert "did not run" in rev.prompt and "every GPU is busy" in rev.prompt


async def test_a_reproduction_that_ends_first_waits_for_its_review(demo) -> None:
    """A reproduction that ends while its review still runs keeps its record and
    waits; once the review's job ends, the record goes into its review, or stays with
    the job when that wrote none. The run is not done again."""
    from datetime import timedelta

    from auto_maintainer.domain.events import Event, EventKind
    from auto_maintainer.domain.records import Job, WaitSpec

    h, _ = demo
    review, _ = await h.store.upsert_job(
        Job(
            playbook="review",
            key=f"review:{REPO}#42:still-running",
            repo=REPO,
            number=42,
            pool="agent",
            status=JobStatus.WAITING,
            wait=WaitSpec(
                kinds=[EventKind.PROPOSAL_DECIDED],
                action_ids=["act_none"],
                deadline=h.clock.now() + timedelta(days=1),
            ),
        )
    )
    pr = await h.forge.get_pr(REPO, 42)
    assert pr is not None
    await h.service.bus.publish(
        Event(
            kind=EventKind.DASHBOARD_COMMAND,
            repo=REPO,
            number=42,
            sha=pr.head_sha,
            payload={
                "command": "reproduce",
                "by": "maria",
                "lab": "gpu-01",
                "alongside": review.id,
            },
            source="review",
        )
    )
    await h.settle()
    [job] = [j for j in await h.store.list_jobs() if j.playbook == "reproduce"]
    assert job.status == JobStatus.WAITING, "it waits for the review to be written"

    review.status, review.wait = JobStatus.DONE, None
    await h.store.save_job(review)
    await h.service.bus.publish(
        Event(
            kind=EventKind.JOB_FINISHED,
            repo=REPO,
            number=42,
            payload={"job": review.id, "playbook": "review", "status": "done"},
        )
    )
    await h.settle()
    done = await h.store.get_job(job.id)
    assert done is not None and done.status == JobStatus.DONE and done.result
    assert done.result["comment"], "the review wrote nothing: the record stays here"
    assert len([t for t in h.runner.tasks if t.kind == "reproduce"]) == 1


async def _robot_machine(h: Any) -> None:
    """A second machine the repository uses, suited to robot PRs."""
    from auto_maintainer.domain.memory import profile_path
    from auto_maintainer.domain.settings import LabMachine
    from auto_maintainer.profiles.layers import dump_yaml, parse_yaml

    h.service.machines["robot-01"] = LabMachine(
        host="robot-01",
        suited_for="PRs that change the arm's driver; a robot arm is attached",
    )
    path = profile_path(REPO)
    layer = parse_yaml(h.memory.files[path], path)
    layer.setdefault("labs", {})["robot-01"] = {"use": True}
    h.memory.files[path] = dump_yaml(layer)
    await h.approve_directly(REPO, "maria")


async def test_a_pr_runs_on_the_machine_suited_to_it(demo) -> None:
    """With several machines, an agent reads what each is suited for beside the PR
    and picks one, which the review's reproduction uses too."""
    h, app = demo
    runner = h.service.a.lab_runner
    await _robot_machine(h)
    picks: list[Any] = []
    answer = h.runner.handlers.get("triage")

    def triage(task: Any) -> Any:
        if task.activity == "machine_choice":
            picks.append(task)
            assert "a robot arm is attached" in task.prompt
            assert task.budget == "quick" and task.mode == "read_only"
            wants_gpu = "gpu-only benchmark" in task.prompt
            return {"machine": "gpu-01" if wants_gpu else "robot-01", "why": "fits"}
        return answer(task) if answer else {"summary": "s", "labels": []}

    h.runner.on("triage", triage)
    async with _as(app, "maria") as maria:
        await _review(h, maria, 42)
    assert {o["payload"].lab for o in runner.reproduced} == {"robot-01"}, (
        "the review and its reproduction test on the robot machine"
    )
    assert len(picks) == 1, "once for the review; not on resuming"


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
        if "That reproduction has ended" in task.prompt:
            return answer(task)
        number = int(task.prompt.split("review PR #")[1].split(" ")[0])
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
    [opened] = _review_sessions(runner)
    await engine.stop()  # the service stops for a deploy ...
    running.cancel()  # ... and cancels the review, as its job pool does
    with contextlib.suppress(asyncio.CancelledError):
        await running
    assert opened["session"] not in runner.closed, "it waits for the review's next run"
    assert (await job_of(42)).status == JobStatus.QUEUED

    engine.start()  # the next start runs the review again
    job = await until(42, JobStatus.DONE, JobStatus.FAILED)
    first, again = _reviews(h)
    assert again.lab_session == first.lab_session == f"token-{opened['session']}"
    assert job.result and job.result["tested_on"] == "gpu-01"
    assert opened["session"] in runner.closed

    working.clear()
    async with _as(app, "maria") as maria:
        await maria.post("/api/reviews", json={"prs": [{"repo": REPO, "number": 44}]})
    await asyncio.wait_for(working.wait(), 10)
    second = _review_sessions(runner)[-1]
    await engine.stop_job((await job_of(44)).id, "maria")
    assert second["session"] in runner.closed, "a person's stop ends it"


async def test_a_repository_can_keep_outside_prs_off_its_machine(demo) -> None:
    h, app = demo
    await _review_rule(h, level="auto", conditions={"author": "team"}, otherwise="off")
    async with _as(app, "maria") as maria:
        await _review(h, maria, 42)  # alice: outside
        await _review(h, maria, 44)  # bob: on the team
    assert {o["number"] for o in h.service.a.lab_runner.reproduced} == {44}
    outside = next(t for t in _reviews(h) if t.lab_session is None)
    assert "do not let reviews of this PR run it on a test machine" in outside.prompt
    assert "Do not run the PR's code here" in outside.prompt


async def test_a_session_that_does_not_open_leaves_the_review_untested(demo) -> None:
    h, app = demo
    h.service.a.lab_runner.cannot_open = "no idle GPU on the machine"
    async with _as(app, "maria") as maria:
        await _review(h, maria, 44)
    [review] = _reviews(h)
    assert review.lab_session is None
    assert "gpu-01 could not be used (no idle GPU on the machine)" in review.prompt
    assert "say that no run was made and what one would need" in review.prompt


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
    updates = [t for t in h.runner.tasks if "That reproduction has ended" in t.prompt]
    [update] = updates
    assert "python check.py" in update.prompt, "the run's record reaches it"
    [job] = [
        j
        for j in await h.store.list_jobs()
        if j.playbook == "reproduce" and not j.events[0]["payload"].get("alongside")
    ]
    assert job.result and job.result["revised"] == draft["action_id"]


async def _posted(
    h: Any, client: httpx.AsyncClient, number: int, *, as_me: bool = False
) -> str:
    """Review ``number`` and post the draft; the id of the posted review's action."""
    draft = await _review(h, client, number)
    await client.post(f"/api/proposals/{draft['id']}/approve", json={"as_me": as_me})
    await h.settle()
    record = await h.store.get_action(draft["action_id"])
    assert record is not None and record.status == "executed"
    return draft["action_id"]


async def _review_edits(h: Any) -> list[Any]:
    return await h.store.list_actions(repo=REPO, number=42, kinds=["review.update"])


async def test_a_reproduction_asked_for_edits_the_services_posted_review(
    demo,
) -> None:
    """The request approves adding the result to a review the service posted: nobody
    is asked again, even when another reviewer posted that draft."""
    h, app = demo
    async with _as(app, "maria") as maria:
        review_id = await _posted(h, maria, 42)
    async with _as(app, "walt") as walt:
        await walt.post(f"/api/prs/{REPO}/42/reproduce", json={"lab": "gpu-01"})
        await h.settle()
        assert not [
            p
            for p in (await walt.get("/api/proposals")).json()
            if p["action"]["action"]["kind"] == "review.update"
        ], "nothing waits for approval"
    [edit] = await _review_edits(h)
    assert edit.status == "executed" and edit.approver == "walt"
    assert edit.post_as is None, "the service's review is edited as the service"
    assert h.forge.writes_of("update_review"), "the posted review now has the result"
    updated = await h.store.get_action(review_id)
    assert (
        updated is not None
        and updated.action.payload["body"] == edit.action.payload["body"]
    )


async def test_a_reproduction_edits_a_review_as_its_author_only_when_they_asked(
    demo,
) -> None:
    """GitHub lets only a review's author edit it, and a person's sign-in serves only
    what they asked for: someone else's request leaves the edit for the author."""
    from auto_maintainer.domain.settings import UserToken

    h, app = demo
    await h.service.a.user_tokens.set("maria", UserToken(token="m"))
    async with _as(app, "maria") as maria:
        await _posted(h, maria, 42, as_me=True)
    async with _as(app, "walt") as walt:
        await walt.post(f"/api/prs/{REPO}/42/reproduce", json={"lab": "gpu-01"})
        await h.settle()
    [waiting] = await _review_edits(h)
    assert waiting.status == "proposed" and waiting.action.draft_for == "maria"
    assert not h.forge.writes_of("update_review")

    async with _as(app, "maria") as maria:
        await maria.post(f"/api/prs/{REPO}/42/reproduce", json={"lab": "gpu-01"})
        await h.settle()
    [mine] = [e for e in await _review_edits(h) if e.action.id != waiting.action.id]
    assert mine.status == "executed" and mine.post_as == "maria"
    assert h.forge.writes_of("update_review")


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
    records = await h.store.list_actions(repo=REPO, number=44, kinds=["lab.review"])
    assert {r.status for r in records} == {"shadowed"}
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


async def test_one_machine_needs_no_choice(demo) -> None:
    h, app = demo
    calls: list[Any] = []
    answer = h.runner.handlers.get("triage")
    h.runner.on(
        "triage",
        lambda t: (
            calls.append(t) or (answer(t) if answer else {"summary": "s", "labels": []})
        ),
    )
    async with _as(app, "maria") as maria:
        await _review(h, maria, 42)
    assert not [t for t in calls if t.activity == "machine_choice"]
    assert h.service.a.lab_runner.reproduced[0]["payload"].lab == "gpu-01"


async def test_a_choice_that_fails_falls_back_to_the_first_machine(demo) -> None:
    h, app = demo
    await _robot_machine(h)
    h.runner.on("triage", lambda t: {"summary": "not a machine", "labels": []})
    async with _as(app, "maria") as maria:
        await _review(h, maria, 42)
    assert h.service.a.lab_runner.reproduced[0]["payload"].lab == "gpu-01"


async def test_a_follow_up_starts_the_reproduction_and_revises_later(demo) -> None:
    """A follow-up of new commits works as a review does: its agent may start the
    skill's reproduction without blocking, and once the reproduction finishes it
    revises the follow-up draft."""
    h, app = demo
    answer = h.runner.handlers["followup"]
    told: list[dict[str, Any]] = []

    async def following(task: Any) -> Any:
        if task.lab_session and "lab --reproduce" in task.prompt and not told:
            told.append(
                await h.service.start_review_reproduction(
                    {"job": task.job_id, "lab": "gpu-01"}, BRIEF
                )
            )
        return answer(task)

    h.runner.on("followup", following)
    async with _as(app, "maria") as maria:
        await _posted(h, maria, 42)
        pr = await h.forge.get_pr(REPO, 42)
        assert pr is not None
        h.forge.put_pr(pr.model_copy(update={"head_sha": "c" * 40}))
        await h.poll()
        await maria.post("/api/reviews", json={"prs": [{"repo": REPO, "number": 42}]})
        await h.settle()
    assert told and told[0]["started"]
    [job] = [j for j in await h.store.list_jobs() if j.playbook == "reproduce"]
    # The request hands the follow-up to the job for the new head; that one tests.
    [follow] = [
        j
        for j in await h.store.list_jobs()
        if j.playbook == "followup" and j.id == job.result["for_review"]
    ]
    [rev] = [
        t
        for t in h.runner.tasks
        if t.kind == "followup" and "That reproduction has ended" in t.prompt
    ]
    assert "python check.py" in rev.prompt
    assert follow.status == JobStatus.DONE


def test_record_text_keeps_conclusion_and_report_when_commands_exceed_limit() -> None:
    """Long command strings and outputs are bounded per command and serialized after
    ``conclusion`` and ``report`` so >40,000 chars of commands never truncate them."""
    from auto_maintainer.playbooks.reproduce import (
        RECORD_COMMAND_CHARS,
        RECORD_LIMIT,
        RECORD_TAIL_CHARS,
        _record_text,
    )

    report_body = "Detailed findings: " + ("metric=0.95 " * 50) + "FINAL_REPORT_MARKER"
    # 1. Single command with huge command + tail (>40,000 chars) is bounded per command.
    huge_cmd = "python train.py " + ("--arg=value " * 300)
    huge_tail = [f"step {i}: " + ("x" * 300) for i in range(150)]
    text = _record_text(
        machine_description="4x A100",
        commit="a" * 40,
        brief="Run training smoke test",
        conclusion="reproduced",
        report=report_body,
        commands=[
            {
                "command": huge_cmd,
                "side": "head",
                "exit_code": 0,
                "tail": huge_tail,
            }
        ],
    )
    assert '"conclusion": "reproduced"' in text
    assert report_body in text
    assert text.index('"conclusion"') < text.index('"commands"')
    assert text.index('"report"') < text.index('"commands"')
    assert huge_cmd not in text
    assert huge_cmd[:RECORD_COMMAND_CHARS] + " ...[truncated]" in text
    assert "earlier lines omitted" in text
    assert "step 149:" in text
    assert len(text) < RECORD_LIMIT + 500

    # 2. Many commands exceeding 40,000 chars even after per-command trimming still
    # preserve conclusion and report completely.
    many_commands = [
        {
            "command": f"python run_{i}.py " + ("a" * (RECORD_COMMAND_CHARS + 200)),
            "side": "head",
            "exit_code": 0,
            "tail": [f"cmd {i} line {j}: " + ("y" * 180) for j in range(40)],
        }
        for i in range(30)
    ]
    assert sum(len(str(c)) for c in many_commands) > RECORD_LIMIT
    text_many = _record_text(
        machine_description="4x A100",
        commit="a" * 40,
        brief="Run full suite",
        conclusion="not_reproduced",
        report=report_body,
        commands=many_commands,
    )
    assert '"conclusion": "not_reproduced"' in text_many
    assert report_body in text_many
    assert "[truncated]" in text_many
    # Also verify string-typed tail is bounded.
    str_tail = "Z" * (RECORD_TAIL_CHARS + 500) + "TAIL_END_MARKER"
    text_str_tail = _record_text(
        machine_description="4x A100",
        commit="a" * 40,
        brief="Check string tail",
        conclusion="inconclusive",
        report="short report",
        commands=[
            {"command": "echo hi", "side": "head", "exit_code": 0, "tail": str_tail}
        ],
    )
    assert "TAIL_END_MARKER" in text_str_tail
    assert str_tail not in text_str_tail


async def test_reproduction_with_long_commands_passes_full_report_to_revision(
    demo,
) -> None:
    """End-to-end: when a reproduction runs >40,000 chars of commands and output,
    the revision agent still receives the full conclusion and report."""
    h, app = demo
    report_body = "Reproduction verified crash on base and fix on head. END_MARKER_42"
    h.runner.on(
        "reproduce",
        lambda _t: {"conclusion": "reproduced", "comment": report_body, "notes": ""},
    )
    h.service.a.lab_runner.commands = [
        {
            "command": f"python step_{i}.py " + ("--flag=1 " * 250),
            "side": "head",
            "exit_code": 0,
            "tail": [f"step {i} line {j}: " + ("log " * 60) for j in range(50)],
        }
        for i in range(25)
    ]
    async with _as(app, "maria") as maria:
        await _review(h, maria, 42)
        await maria.post(f"/api/prs/{REPO}/42/reproduce", json={"lab": "gpu-01"})
        await h.settle()
    [update] = _revisions(h)
    assert '"conclusion": "reproduced"' in update.prompt
    assert report_body in update.prompt

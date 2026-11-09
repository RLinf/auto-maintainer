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
    await executor.open_session(
        _payload(sides=["head"]),
        REPO,
        43,
        {"head": ""},
        "act_bbbbbbbb",
        binding=LabBinding(),
    )
    # A new process finds the session the last one left open and cleans it up.
    again = LabExecutor({}, {}, executor.store, executor.clock)
    again.configure({"gpu-01": LabMachine()})
    again.hosts["gpu-01"] = host
    assert await again.recover_sessions() == 1
    assert host.commands[-1][0] == "rm -rf /home/ci/am-labs/acme__widget-43-bbbbbbbb"


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


async def test_an_agent_does_the_reproduction_in_its_session_once_approved(
    demo,
) -> None:
    h, app = demo
    runner = h.service.a.lab_runner
    async with _as(app, "maria") as maria:
        started = await maria.post(
            f"/api/prs/{REPO}/42/reproduce",
            json={"lab": "gpu-01", "note": "the batch of one", "agent": "codex"},
        )
        assert started.status_code == 200, started.text
        await h.settle()
        [plan_task] = [t for t in h.runner.tasks if t.kind == "reproduce"]
        assert "the batch of one" in plan_task.prompt and plan_task.mode == "read_only"
        assert plan_task.lab_session is None, "planning gets no machine"
        [job] = [j for j in await h.store.list_jobs() if j.playbook == "reproduce"]
        assert job.status == JobStatus.WAITING
        [draft] = [
            p
            for p in (await maria.get("/api/proposals")).json()
            if p["action"]["action"]["kind"] == "lab.reproduce"
        ]
        assert draft["requested_by"] == "maria"
        steps = draft["action"]["action"]["payload"]["steps"]
        assert steps and "Install the package" in steps[0], "the plan is approved"
        assert runner.reproduced == []
        ok = await maria.post(f"/api/proposals/{draft['id']}/approve", json={})
        assert ok.status_code == 200, ok.text
        await h.settle()
        [opened] = runner.reproduced
        assert set(opened["code"]) == {"base", "head"}
        [_, worker] = [t for t in h.runner.tasks if t.kind == "reproduce"]
        assert worker.lab_session == f"token-{opened['session']}"
        assert worker.env["AM_LAB_SOCKET"], "the agent reaches its session"
        assert (
            "lab -- 'COMMAND'" in worker.prompt
            and "Install the package" in worker.prompt
        )
        assert "find the cause in the output" in worker.prompt
        assert "ask maria with your tool for asking the user" in worker.prompt
        assert worker.requested_by == "maria", "its questions go to maria"
        assert runner.closed == [opened["session"]], (
            "the session closes when it is done"
        )
        progress = await h.service.progress.steps(job.id)
        assert [s.text for s in progress if s.kind == "output"] == [
            "gpu-01 · base",
            "gpu-01 · head",
        ], "the job shows the machine's output as it comes"
        job = await h.store.get_job(job.id)
        assert job is not None and job.status == JobStatus.DONE
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
        if "carry it out yourself" not in task.prompt:
            return {
                "summary": "check it",
                "steps": ["run the check"],
                "sides": ["head"],
            }
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
        [draft] = [
            p
            for p in (await maria.get("/api/proposals")).json()
            if p["action"]["action"]["kind"] == "lab.reproduce"
        ]
        await maria.post(f"/api/proposals/{draft['id']}/approve", json={})
        await h.settle()
    [opened] = runner.reproduced
    assert runner.closed == [] and opened["session"] in runner.sessions


async def test_a_rejected_plan_ends_the_job_and_runs_nothing(demo) -> None:
    h, app = demo
    async with _as(app, "maria") as maria:
        await maria.post(f"/api/prs/{REPO}/42/reproduce", json={"lab": "gpu-01"})
        await h.settle()
        [draft] = [
            p
            for p in (await maria.get("/api/proposals")).json()
            if p["action"]["action"]["kind"] == "lab.reproduce"
        ]
        await maria.post(
            f"/api/proposals/{draft['id']}/reject", json={"reason": "too slow"}
        )
        await h.settle()
    [job] = [j for j in await h.store.list_jobs() if j.playbook == "reproduce"]
    assert job.status == JobStatus.DONE and job.result
    assert job.result["skipped"] == "the plan was not approved"
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
        [plan] = [t for t in h.runner.tasks if t.kind == "reproduce"]
        assert "source ~/envs/cpu/bin/activate" in plan.prompt
        assert "at most 10 minutes" in plan.prompt
        off = await maria.put(f"/api/repos/{REPO}/labs/gpu-01", json={"use": False})
        assert off.json()["applied"] is True
        offered = (await maria.get(f"/api/labs?repo={REPO}")).json()["machines"]
        assert not [m for m in offered if m["name"] == "gpu-01"][0]["used"]
        rule = await maria.put(
            f"/api/repos/{REPO}/policy/lab.reproduce",
            json={
                "level": "auto",
                "conditions": {"author": "team"},
                "otherwise": "propose",
            },
        )
        assert rule.json()["applied"] is True
        pol = (await maria.get(f"/api/labs?repo={REPO}")).json()["reproduce_policy"]
        assert pol["level"] == "auto"
        assert pol["conditions"]["author"] == "team"
        assert pol["otherwise"] == "propose"
        reset = await maria.put(
            f"/api/repos/{REPO}/policy/lab.reproduce",
            json={
                "level": "propose",
                "conditions": {"author": "any"},
                "otherwise": "shadow",
            },
        )
        assert reset.json()["applied"] is True
        pol = (await maria.get(f"/api/labs?repo={REPO}")).json()["reproduce_policy"]
        assert pol["level"] == "propose"
        assert pol["conditions"]["author"] == "any"
        assert pol["otherwise"] == "shadow"


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


# -- a review reproduces its PR -------------------------------------------------------------


async def _reproduce_in_reviews(h: Any) -> None:
    """The repository's rule: a team PR runs at once, anyone else's waits for a
    maintainer to approve its script."""
    from auto_maintainer.domain.memory import profile_path
    from auto_maintainer.profiles.layers import dump_yaml, parse_yaml

    path = profile_path(REPO)
    layer = parse_yaml(h.memory.files[path], path)
    layer.setdefault("policy", {})["lab.reproduce"] = {
        "level": "auto",
        "conditions": {"author": "team"},
        "otherwise": "propose",
    }
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


async def test_a_team_prs_review_reproduces_it_at_once_and_adds_the_result(
    demo,
) -> None:
    h, app = demo
    await _reproduce_in_reviews(h)
    async with _as(app, "maria") as maria:
        draft = await _review(h, maria, 44)  # bob may write: on the team
    [review] = [
        t
        for t in h.runner.tasks
        if t.kind == "review" and "That reproduction has ended" not in t.prompt
    ]
    assert "starts after this review: it runs at once" in review.prompt
    [plan] = [
        t
        for t in h.runner.tasks
        if t.kind == "reproduce" and "You are planning" in t.prompt
    ]
    assert "follow its documentation" in plan.prompt
    assert "it runs on its own" in plan.prompt
    [ran] = h.service.a.lab_runner.reproduced
    assert ran["payload"].requested_by == "maria", "who asked for the review"
    records = await h.store.list_actions(repo=REPO, number=44, kinds=["lab.reproduce"])
    assert [r.status for r in records] == ["executed"] and records[0].approver is None
    updated = [t for t in h.runner.tasks if "That reproduction has ended" in t.prompt]
    assert len(updated) == 1 and "batch of one item" in updated[0].prompt
    record = await h.store.get_action(draft["action_id"])
    assert record is not None and "Reproduced on a test machine" in str(
        record.action.payload["body"]
    )
    proposal = await h.store.proposal_for_action(draft["action_id"])
    assert proposal is not None and proposal.state == "pending"
    assert not proposal.edited, "the service's own addition is not a person's edit"
    comments = await h.store.list_actions(
        repo=REPO, number=44, kinds=["comment.upsert"]
    )
    assert not [
        c
        for c in comments
        if "reproduction" in c.action.discriminator + str(c.action.payload)
    ]


async def test_an_outside_prs_reproduction_waits_for_a_maintainer(demo) -> None:
    h, app = demo
    await _reproduce_in_reviews(h)
    async with _as(app, "maria") as maria:
        draft = await _review(h, maria, 42)  # alice is not on the team
        [review] = [
            t
            for t in h.runner.tasks
            if t.kind == "review" and "That reproduction has ended" not in t.prompt
        ]
        assert "waits for a maintainer to approve its plan" in review.prompt
        assert h.service.a.lab_runner.reproduced == []
        [script] = [
            p
            for p in (await maria.get("/api/proposals")).json()
            if p["action"]["action"]["kind"] == "lab.reproduce"
        ]
        assert script["requested_by"] is None
        # The review is posted before the script is approved: the result becomes an
        # edit of the posted review, not a comment of its own.
        posted = await maria.post(f"/api/proposals/{draft['id']}/approve", json={})
        assert posted.status_code == 200, posted.text
        await maria.post(f"/api/proposals/{script['id']}/approve", json={})
        await h.settle()
    assert len(h.service.a.lab_runner.reproduced) == 1
    comments = await h.store.list_actions(
        repo=REPO, number=42, kinds=["comment.upsert"]
    )
    assert not [
        c for c in comments if c.action.payload.get("purpose") == "reproduction"
    ]
    [edit] = await h.store.list_actions(repo=REPO, number=42, kinds=["review.update"])
    assert edit.action.payload["revises"] == draft["action_id"]
    assert edit.status == "proposed", "whoever posted the review approves the edit"


async def test_a_run_the_machine_cannot_do_is_reported_in_the_review(demo) -> None:
    h, app = demo
    await _reproduce_in_reviews(h)
    h.runner.on(
        "reproduce",
        lambda task: {
            "summary": "Run the documented pick-and-place demo.",
            "blocked": "the docs run it on a UR5 arm, which the machine does not have",
        },
    )
    async with _as(app, "maria") as maria:
        await _review(h, maria, 44)
    assert h.service.a.lab_runner.reproduced == []
    assert not await h.store.list_actions(repo=REPO, number=44, kinds=["lab.reproduce"])
    [updated] = [t for t in h.runner.tasks if "That reproduction has ended" in t.prompt]
    assert "UR5 arm" in updated.prompt and '"ran": false' in updated.prompt
    [job] = [j for j in await h.store.list_jobs() if j.playbook == "reproduce"]
    assert job.result and job.result["blocked"] and job.result.get("revised")


async def test_without_a_machine_the_review_says_no_run_was_made(demo) -> None:
    from auto_maintainer.domain.memory import profile_path
    from auto_maintainer.profiles.layers import dump_yaml, parse_yaml

    h, app = demo
    await _reproduce_in_reviews(h)
    path = profile_path(REPO)
    layer = parse_yaml(h.memory.files[path], path)
    layer["labs"] = {"gpu-01": {"use": False}}
    h.memory.files[path] = dump_yaml(layer)
    await h.approve_directly(REPO, "maria")
    async with _as(app, "maria") as maria:
        await _review(h, maria, 44)
    [review] = [
        t
        for t in h.runner.tasks
        if t.kind == "review" and "That reproduction has ended" not in t.prompt
    ]
    assert "no test machine is set up for this repository" in review.prompt
    assert not [j for j in await h.store.list_jobs() if j.playbook == "reproduce"]


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
        await h.settle()
        [draft] = [
            p
            for p in (await maria.get("/api/proposals")).json()
            if p["action"]["action"]["kind"] == "lab.reproduce"
        ]
        await maria.post(f"/api/proposals/{draft['id']}/approve", json={})
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
    lab = await h.store.get_action(draft["action_id"])
    assert lab is not None and lab.status == "intent", "the run is still going"
    machine_free.set()
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


async def _approve_scripts(h: Any, client: httpx.AsyncClient) -> int:
    """Approve every reproduction script waiting; how many there were."""
    waiting = [
        p
        for p in (await client.get("/api/proposals")).json()
        if p["action"]["action"]["kind"] == "lab.reproduce"
    ]
    for p in waiting:
        await client.post(f"/api/proposals/{p['id']}/approve", json={})
    await h.settle()
    return len(waiting)


async def test_a_reproduction_asked_for_goes_into_the_waiting_review(demo) -> None:
    h, app = demo
    async with _as(app, "maria") as maria:
        draft = await _review(h, maria, 42)
        await maria.post(f"/api/prs/{REPO}/42/reproduce", json={"lab": "gpu-01"})
        await h.settle()
        assert await _approve_scripts(h, maria) == 1
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

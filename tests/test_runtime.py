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

"""Store, poller, engine waits, actuator guarantees, hygiene checks, git memory, and labs."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from auto_maintainer.domain.actions import (
    Action,
    ActionKind,
    ActionStatus,
    CommentUpsertPayload,
    LabelPayload,
    LabRunPayload,
    RerunPayload,
    make_action,
)
from auto_maintainer.domain.clock import FixedClock
from auto_maintainer.domain.events import Event, EventKind
from auto_maintainer.domain.memory import MemoryChange, Tier
from auto_maintainer.domain.ports import CommandResult, GpuStatus
from auto_maintainer.domain.profile import CheckSpec, RepoProfile
from auto_maintainer.domain.records import ActionRecord, Job, JobStatus
from auto_maintainer.domain.snapshot import (
    CheckConclusion,
    CheckRun,
    CheckStatus,
    Commit,
    IssueComment,
    PrFile,
    PrState,
)
from auto_maintainer.engine.poller import diff_snapshots
from auto_maintainer.labs.executor import LabExecutor, guard
from auto_maintainer.labs.hosts import parse_gpus
from auto_maintainer.memory.git_store import GitMemoryStore
from auto_maintainer.playbooks.base import (
    Playbook,
    PlaybookContext,
    PlaybookResult,
    Wait,
)
from auto_maintainer.profiles.checks import (
    CheckInputs,
    paired_paths,
    run_builtin,
    signoff,
    template_sections,
    title_rule,
)
from auto_maintainer.store.sqlite import SqliteStore
from auto_maintainer.testing.fakes import FakeLabHost, make_pr
from auto_maintainer.testing.harness import Harness

REPO = "acme/widget"


# -- store ---------------------------------------------------------------------------


async def test_store_dedupes_jobs_and_actions(tmp_path: Path) -> None:
    store = await SqliteStore.open(tmp_path / "s.db")
    try:
        job = Job(playbook="p", key="k", events=[{"kind": "pr.opened"}])
        first, new = await store.upsert_job(job)
        again, new2 = await store.upsert_job(
            Job(playbook="p", key="k", events=[{"kind": "pr.updated"}])
        )
        assert new and not new2 and again.id == first.id and len(again.events) == 2
        action = Action(
            kind=ActionKind.NOTIFY, repo=REPO, payload={"event": "x", "text": "y"}
        )
        _, created = await store.add_action(ActionRecord(action=action))
        dup = action.model_copy(update={"id": "act_other"})
        stored, created2 = await store.add_action(ActionRecord(action=dup))
        assert created and not created2 and stored.action.id == action.id
        clock = FixedClock()
        assert await store.acquire_lease("lab", "gpu0", "a", 60, clock.now())
        assert not await store.acquire_lease("lab", "gpu0", "b", 60, clock.now())
        assert await store.acquire_lease(
            "lab", "gpu0", "b", 60, clock.advance(seconds=61)
        )
    finally:
        await store.close()


# -- poller -------------------------------------------------------------------------


def test_poller_diff_events() -> None:
    old = make_pr(checks=[CheckRun(id=1, name="unit", status=CheckStatus.IN_PROGRESS)])
    assert [e.kind for e in diff_snapshots(None, old, "bot", "/am")] == [
        EventKind.PR_OPENED
    ]
    new = old.model_copy(
        update={
            "labels": ["bug"],
            "comments": [
                IssueComment(id=9, author="maria", body="/am merge"),
                IssueComment(id=10, author="bot", body="mine"),
            ],
            "checks": [
                CheckRun(
                    id=1,
                    name="unit",
                    status=CheckStatus.COMPLETED,
                    conclusion=CheckConclusion.FAILURE,
                )
            ],
        }
    )
    events = {e.kind: e for e in diff_snapshots(old, new, "bot", "/am")}
    assert events[EventKind.PR_LABELED].payload["added"] == ["bug"]
    assert events[EventKind.PR_COMMENT].payload["commands"] == [
        {"name": "merge", "args": []}
    ]
    assert events[EventKind.CHECKS_COMPLETED].payload["failed"] == [1]
    moved = new.model_copy(update={"head_sha": "b" * 40})
    assert EventKind.PR_SYNCHRONIZED in {
        e.kind for e in diff_snapshots(new, moved, "bot", "/am")
    }
    closed = new.model_copy(update={"state": PrState.MERGED})
    assert diff_snapshots(new, closed, "bot", "/am")[0].payload == {"merged": True}


# -- engine waits ---------------------------------------------------------------------


class _Waiter(Playbook):
    name = "waiter"
    triggers = (EventKind.MERGE_ARMED,)
    runs: list[str] = []

    def key(self, event: Event) -> str:
        return f"waiter:{event.repo}#{event.number}"

    async def run(self, ctx: PlaybookContext, event: Event) -> PlaybookResult:
        self.runs.append(str(event.kind))
        if event.kind == EventKind.MERGE_ARMED:
            return PlaybookResult(
                wait=Wait(kinds=[EventKind.CHECKS_COMPLETED], seconds=60)
            )
        return PlaybookResult(result={"woke": str(event.kind)})


async def test_waiting_job_wakes_on_matching_event_and_times_out(
    tmp_path: Path,
) -> None:
    _Waiter.runs = []
    h = await Harness.create(tmp_path, layer={}, playbooks=[_Waiter()])
    try:
        await h.emit(Event(kind=EventKind.MERGE_ARMED, repo=REPO, number=1))
        [job] = await h.store.list_jobs([JobStatus.WAITING])
        await h.emit(Event(kind=EventKind.CHECKS_COMPLETED, repo=REPO, number=2))
        assert (await h.store.get_job(job.id)).status == JobStatus.WAITING, (
            "a different PR does not wake it"
        )
        await h.emit(Event(kind=EventKind.CHECKS_COMPLETED, repo=REPO, number=1))
        assert (await h.store.get_job(job.id)).status == JobStatus.DONE
        assert _Waiter.runs == ["merge.armed", "checks.completed"]

        await h.emit(Event(kind=EventKind.MERGE_ARMED, repo=REPO, number=3))
        h.clock.advance(seconds=61)
        await h.service.engine.tick()
        await h.settle()
        assert _Waiter.runs[-1] == "job.timeout"
    finally:
        await h.close()


# -- actuator ------------------------------------------------------------------------


async def test_actuator_writes_once_refuses_stale_and_guards_leaks(
    harness: Harness,
) -> None:
    harness.forge.put_pr(make_pr())
    profile_layer = {
        "policy": {
            "comment.upsert": {"level": "auto"},
            "label.apply": {"level": "auto"},
            "ci.rerun_failed": {"level": "auto"},
        }
    }
    from auto_maintainer.domain.memory import profile_path
    from auto_maintainer.profiles.layers import dump_yaml

    harness.memory.files[profile_path(REPO)] = dump_yaml({"schema": 1, **profile_layer})
    await harness.approve_directly(REPO, "maria")
    pipeline = harness.service.pipeline
    a = make_action(
        ActionKind.COMMENT_UPSERT,
        REPO,
        CommentUpsertPayload(purpose="hygiene", body="All good."),
        number=42,
        head_sha="a" * 40,
    )
    await pipeline.submit(a)
    await pipeline.submit(a.model_copy(update={"id": "act_dup"}))
    await harness.settle()
    assert len(harness.forge.writes_of("create_comment")) == 1

    stale = make_action(
        ActionKind.LABEL_APPLY,
        REPO,
        LabelPayload(labels=["bug"]),
        number=42,
        head_sha="0" * 40,
    )
    harness.forge.put_pr(
        (await harness.forge.get_pr(REPO, 42)).model_copy(update={"head_sha": "b" * 40})
    )
    stale_ci = make_action(
        ActionKind.CI_RERUN_FAILED,
        REPO,
        RerunPayload(run_id=1),
        number=42,
        head_sha="a" * 40,
    )
    await pipeline.submit(stale)
    await pipeline.submit(stale_ci)
    leak = make_action(
        ActionKind.COMMENT_UPSERT,
        REPO,
        CommentUpsertPayload(purpose="ci", body="This is P0 for our rubric."),
        number=42,
        head_sha="b" * 40,
    )
    await pipeline.submit(leak)
    await harness.settle()
    records = {r.action.id: r for r in await harness.store.list_actions(repo=REPO)}
    assert records[stale.id].status == ActionStatus.EXECUTED, (
        "labels do not need the current head"
    )
    assert records[stale_ci.id].status == ActionStatus.SUPERSEDED, records[
        stale_ci.id
    ].error
    assert records[leak.id].status == ActionStatus.FAILED and "leak guard" in (
        records[leak.id].error or ""
    )
    assert not any("P0" in w["body"] for w in harness.forge.writes_of("create_comment"))


# -- hygiene checks --------------------------------------------------------------------


def test_builtin_checks() -> None:
    profile = RepoProfile.model_validate(
        {
            "repo": REPO,
            "contribution": {
                "signoff": True,
                "template_sections": ["Description", "Checklist"],
                "title_rule": {
                    "pattern": r"^\w+: ",
                    "max_description": 20,
                    "lowercase_start": True,
                    "no_trailing_period": True,
                },
            },
        }
    )
    spec = CheckSpec(id="x")
    pr = make_pr(
        title="feat: Add a very long description here.",
        body="### Description\n<!-- fill -->\n### Checklist\n- [x] done",
        commits=[
            Commit(sha="1" * 40, message="feat: a"),
            Commit(sha="2" * 40, message="fix: b\n\nSigned-off-by: A <a@x>"),
        ],
    )
    t = title_rule(pr, profile, spec, CheckInputs())
    assert not t.passed and len(t.details) == 3
    s = signoff(pr, profile, spec, CheckInputs())
    assert not s.passed and len(s.details) == 1
    tpl = template_sections(pr, profile, spec, CheckInputs())
    assert not tpl.passed and tpl.details == ["`Description`"]
    pair = paired_paths(
        make_pr(files=[PrFile(path="docs/en/a.md")]),
        profile,
        CheckSpec(id="p", settings={"pairs": [["docs/en", "docs/zh"]]}),
        CheckInputs(),
    )
    assert not pair.passed
    wf = """
jobs:
  changes:
    steps:
      - uses: dorny/paths-filter@v3
        with:
          filters: |
            core:
              - 'src/**'
              - 'pyproject.toml'
"""
    spec_wf = CheckSpec(
        id="pf", name="paths_filter_coverage", settings={"workflow": "ci.yml"}
    )
    pr2 = make_pr(
        files=[
            PrFile(path="src/a/b.py"),
            PrFile(path="tools/x.py"),
            PrFile(path="README.md"),
        ]
    )
    res = run_builtin(pr2, profile, spec_wf, CheckInputs(files={"ci.yml": wf}))
    assert not res.passed and res.details == ["`tools/x.py`"]


async def test_hygiene_comment_and_draft_proposal(harness: Harness) -> None:
    from auto_maintainer.domain.memory import profile_path
    from auto_maintainer.profiles.layers import dump_yaml

    harness.memory.files[profile_path(REPO)] = dump_yaml(
        {
            "schema": 1,
            "extends": ["conventional-commits", "dco"],
            "contribution": {"template_sections": ["Description"]},
            "checks": [
                {"id": "template", "name": "template_sections"},
                {
                    "id": "lint",
                    "type": "command",
                    "run": "python3 tools/lint.py {body_file}",
                    "blocking": True,
                },
            ],
            "policy": {
                "comment.upsert": {"level": "auto"},
                "pr.convert_to_draft": {"level": "propose"},
            },
        }
    )
    await harness.approve_directly(REPO, "maria")
    harness.sandbox.results = [CommandResult(exit_code=1, stdout="title too long")]
    harness.forge.put_pr(
        make_pr(
            title="Fix things.",
            body="",
            commits=[Commit(sha="1" * 40, message="Fix things")],
        )
    )
    harness.runner.on("triage", lambda t: {"summary": "s", "labels": []})
    harness.runner.on(
        "evaluate",
        lambda t: {
            "scores": {"impact": 5, "urgency": 5, "strategic": 5, "readiness": 5},
            "summary": "s",
            "rationale": "r",
        },
    )
    harness.runner.on("review", lambda t: {"summary": "s", "recommendation": "Ready"})
    await harness.poll()
    [comment] = harness.forge.writes_of("create_comment")
    assert (
        "### Hygiene checks" in comment["body"]
        and "Signed-off-by" in comment["body"]
        and "(blocks merging)" in comment["body"]
    )
    assert harness.sandbox.runs and harness.sandbox.runs[0][0] == "python3"
    drafts = await harness.store.list_actions(kinds=[ActionKind.PR_CONVERT_TO_DRAFT])
    assert drafts and drafts[0].status == ActionStatus.PROPOSED


# -- git memory store -----------------------------------------------------------------


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
async def test_git_memory_store_stages_and_merges(tmp_path: Path) -> None:
    store = GitMemoryStore(tmp_path / "mem")
    await store.open()
    assert await store.read_text("MEMORY.md")
    await store.commit_direct(
        {"repos/a/b/journal/x.jsonl": "{}\n"}, "journal: x", "service"
    )
    change = MemoryChange(
        tier=Tier.T2,
        title="profile: x",
        files={"repos/a/b/profile.yaml": "schema: 1\n"},
        repos=["a/b"],
        approvals={"a/b": ["maria"]},
    )
    change.branch = await store.stage_change(change)
    assert await store.read_text("repos/a/b/profile.yaml") is None, (
        "staged changes are not on main yet"
    )
    await store.merge_change(change)
    assert await store.read_text("repos/a/b/profile.yaml") == "schema: 1\n"
    log = subprocess.run(
        ["git", "log", "--format=%B", "-1"],
        cwd=tmp_path / "mem",
        capture_output=True,
        text=True,
    ).stdout
    assert "Approved-by: maria (a/b)" in log
    with pytest.raises(ValueError, match="escapes"):
        await store.commit_direct({"../evil": "x"}, "m", "a")


# -- labs ----------------------------------------------------------------------------


def test_lab_guard_and_gpu_parsing() -> None:
    from auto_maintainer.domain.profile import LabBinding, LabGuard

    binding = LabBinding(
        base_clone="/srv/base",
        protected_paths=["/srv/cache"],
        guard=LabGuard(forbid=[r"\bray\s+stop\b"]),
    )
    assert guard("pytest tests", binding, "/srv/wt/pr-1", None) == []
    assert guard("ray stop", binding, "/srv/wt/pr-1", None)
    assert guard("rm -rf /srv/cache/uv", binding, "/srv/wt/pr-1", None)
    assert guard("cd /srv/base && pytest", binding, "/srv/wt/pr-1", None)
    assert guard("rm -rf /srv/wt/pr-1/build", binding, "/srv/wt/pr-1", None) == []
    assert guard("rm -rf ../other", binding, "/srv/wt/pr-1", None)
    gpus = parse_gpus(
        "0, GPU-a, 100, 80000, 0\n1, GPU-b, 30000, 80000, 97\n", "GPU-b, 1234\n"
    )
    assert [(g.index, g.processes) for g in gpus] == [(0, 0), (1, 1)]


async def test_lab_executor_leases_idle_gpu_and_cleans_up(tmp_path: Path) -> None:
    store = await SqliteStore.open(tmp_path / "s.db")
    try:
        host = FakeLabHost(
            "gpu-host",
            gpus=[
                GpuStatus(
                    index=0, memory_used_mib=40000, utilization_pct=90, processes=1
                ),
                GpuStatus(index=1, memory_used_mib=10),
            ],
        )
        profile = RepoProfile.model_validate(
            {
                "repo": REPO,
                "labs": {
                    "gpu-host": {
                        "base_clone": "/srv/base",
                        "worktree_root": "/srv/wt",
                        "templates": {
                            "unit": {
                                "run": "pytest {file}",
                                "needs_gpu": True,
                                "cpu_fallback": "pytest -m 'not gpu' {file}",
                            }
                        },
                    }
                },
            }
        )
        lab = LabExecutor({"gpu-host": host}, {}, store, FixedClock())
        out = await lab.run(
            profile,
            LabRunPayload(lab="gpu-host", template="unit", args={"file": "tests/a.py"}),
            REPO,
            7,
            "a" * 40,
            "act_1",
        )
        assert out["passed"] and out["gpu"] == 1
        commands = [c[0] for c in host.commands]
        assert (
            "worktree add" in commands[0]
            and commands[1] == "pytest tests/a.py"
            and "worktree remove" in commands[-1]
        )
        assert host.commands[1][2]["CUDA_VISIBLE_DEVICES"] == "1"
        host.gpus = [
            GpuStatus(index=0, memory_used_mib=40000, utilization_pct=90, processes=1)
        ]
        out = await lab.run(
            profile,
            LabRunPayload(lab="gpu-host", template="unit", args={"file": "tests/a.py"}),
            REPO,
            7,
            "a" * 40,
            "act_2",
        )
        assert out["deferred"] and host.commands[-2][2]["CUDA_VISIBLE_DEVICES"] == ""
    finally:
        await store.close()

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

"""Reviews a maintainer asks for: the repository's own skills, questions for the
person who asked (never about posting), one review per commit and agent, posting the
draft as yourself, and when reviews start on their own."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from auto_maintainer.agents.approver import posting_request
from auto_maintainer.domain.agents import (
    AgentQuestion,
    AgentResult,
    InputQuestion,
    QuestionKind,
    TaskKind,
)
from auto_maintainer.domain.permissions import Permission
from auto_maintainer.domain.records import Job, JobStatus
from auto_maintainer.domain.settings import UserToken
from auto_maintainer.testing.demo import build_demo
from auto_maintainer.web.app import create_app

REPO = "acme/widget"


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


def _skill(h: Any, number: int, sha: str) -> None:
    """A review skill in the repository, where the workspace checks the PR out."""
    root = h.service.a.workspace.root / f"{REPO.replace('/', '__')}-{number}-{sha[:7]}"
    skill = root / ".agents" / "skills" / "code-review" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\nname: code-review\ndescription: How this project reviews PRs\n---\nRead it.\n"
    )


# -- what agents may ask ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "text", "posting"),
    [
        ("permission", "gh pr comment 42 --body 'nice'", True),
        ("permission", "gh api repos/a/b/pulls/1/reviews -X POST -f body=x", True),
        ("permission", "git push origin HEAD", True),
        ("permission", "gh pr view 42 --json files", False),
        ("input", "Should I post these review comments to the PR?", True),
        ("input", "要把评审发布到 GitHub 吗？", True),
        ("input", "Is the retry loop meant to be unbounded?", False),
    ],
)
def test_questions_about_posting_never_reach_anyone(
    kind: str, text: str, posting: bool
) -> None:
    if kind == "permission":
        q = AgentQuestion(
            kind=QuestionKind.PERMISSION,
            title="Run a command",
            tool="execute",
            detail=text,
        )
    else:
        q = AgentQuestion(
            kind=QuestionKind.INPUT,
            title="Question",
            inputs=[InputQuestion(id="q", question=text, header="", options=[])],
        )
    assert posting_request(q) is posting


# -- requested reviews ---------------------------------------------------------------------


async def _review(app: Any, login: str, number: int, agent: str | None = None) -> dict:
    async with _as(app, login) as client:
        r = await client.post(
            "/api/reviews",
            json={"prs": [{"repo": REPO, "number": number}], "agent": agent},
        )
        assert r.status_code == 200, r.text
        return r.json()[0]


async def test_a_requested_review_follows_the_repos_skill_and_asks_the_requester(
    demo,
) -> None:
    h, app = demo
    pr = h.forge.prs[(REPO, 42)]
    _skill(h, 42, pr.head_sha)
    started = await _review(app, "maria", 42, agent="codex")
    assert started["run"] == "review" and started["agent"] == "codex"
    await h.settle()
    [i] = [i for i, t in enumerate(h.runner.tasks) if t.kind == TaskKind.REVIEW]
    task, route = h.runner.tasks[i], h.runner.routes[i]
    assert ".agents/skills/code-review/SKILL.md" in task.prompt
    assert (
        "reviewing for maria" in task.prompt
        and "Do not ask questions" not in task.prompt
    )
    assert task.requested_by == "maria"
    assert route.primary == "codex" and route.fallback == []


async def test_one_review_per_commit_and_agent(demo) -> None:
    h, app = demo
    await _review(app, "maria", 42, agent="codex")
    await h.settle()
    again = await _review(app, "walt", 42, agent="codex")
    assert again["run"] == "drafted" and again["by"] == "maria"
    other = await _review(app, "walt", 42, agent="claude-code")
    assert other["run"] == "review" and other["agent"] == "claude-code"

    pr = h.forge.prs[(REPO, 43)]
    await h.store.upsert_job(
        Job(
            playbook="review",
            key="review:acme/widget#43:req:x",
            repo=REPO,
            number=43,
            pool="agent",
            status=JobStatus.RUNNING,
            events=[{"sha": pr.head_sha, "payload": {"by": "maria", "agent": "codex"}}],
        )
    )
    joined = await _review(app, "walt", 43, agent="codex")
    assert joined["run"] == "joined" and joined["by"] == "maria"


async def test_a_draft_posted_as_yourself_still_counts_as_the_services_review(
    demo,
) -> None:
    h, app = demo
    await _review(app, "maria", 42)
    await h.settle()
    async with _as(app, "maria") as maria:
        [draft] = [
            p
            for p in (await maria.get("/api/proposals")).json()
            if p["action"]["action"]["kind"] == "review.post"
        ]
        assert (await maria.get("/api/me")).json()["can_post_as_me"] is False
        refused = await maria.post(
            f"/api/proposals/{draft['id']}/approve", json={"as_me": True}
        )
        assert refused.status_code == 409
        await h.service.a.user_tokens.set("maria", UserToken(token="t"))
        assert (await maria.get("/api/me")).json()["can_post_as_me"] is True
        ok = await maria.post(
            f"/api/proposals/{draft['id']}/approve", json={"as_me": True}
        )
        assert ok.status_code == 200
        await h.settle()
        [review] = h.forge.prs[(REPO, 42)].reviews
        assert review.author == "maria"
        await h.poll()
        [row] = [r for r in (await maria.get("/api/prs")).json() if r["number"] == 42]
        assert row["review_state"] == "UP_TO_DATE"
    again = await _review(app, "walt", 42)
    assert again["run"] == "skip"


# -- when reviews start on their own -------------------------------------------------------


async def test_maintainers_set_when_reviews_start_on_their_own(demo) -> None:
    h, app = demo
    body = {"trigger": "scan", "every_hours": 3, "max_prs": 4, "followups": False}
    async with _as(app, "walt") as walt:
        current = (await walt.get(f"/api/repos/{REPO}/review-automation")).json()
        assert current["trigger"] == "manual" and current["can_configure"] is False
        proposed = (
            await walt.put(f"/api/repos/{REPO}/review-automation", json=body)
        ).json()
        assert proposed["applied"] is False
    async with _as(app, "maria") as maria:
        applied = (
            await maria.put(f"/api/repos/{REPO}/review-automation", json=body)
        ).json()
        assert applied["applied"] is True
        now = (await maria.get(f"/api/repos/{REPO}/review-automation")).json()
    assert now["trigger"] == "scan" and now["every_hours"] == 3
    assert now["max_prs"] == 4 and now["followups"] is False
    profile = await h.service.profiles.active(REPO)
    assert profile is not None and profile.review.scan.schedule == "0 */3 * * *"


# -- fixing what a review found --------------------------------------------------------------


async def test_a_fix_is_drafted_for_the_reviewer_and_opened_as_them(demo) -> None:
    h, app = demo
    await _review(app, "maria", 42)
    await h.settle()

    def fix(task: Any) -> Any:
        if task.mode == "read_only":
            # The second, short task describes the diff the edit left.
            assert "test_one.py" in task.prompt and "Added a test" in task.prompt
            return {
                "title": "test(loader): cover a batch of one",
                "summary": "Adds the missing boundary test.",
                "fixed": [{"id": "F1", "change": "added a test for one item"}],
            }
        assert (
            "reviewing" not in task.prompt
            and "fixing a pull request for maria" in task.prompt
        )
        assert task.requested_by == "maria" and task.mode == "workspace_write"
        (Path(task.workdir) / "tests").mkdir(exist_ok=True)
        (Path(task.workdir) / "tests" / "test_one.py").write_text(
            "def test_one(): ...\n"
        )
        # Like a long edit on some CLIs: a plain closing message, no JSON.
        return AgentResult(runtime="scripted", ok=True, text="Added a test for F1.")

    h.runner.on("fix", fix)
    async with _as(app, "maria") as maria:
        started = await maria.post(f"/api/prs/{REPO}/42/fix", json={"agent": "codex"})
        assert started.status_code == 200, started.text
        await h.settle()
        fixes = [i for i, t in enumerate(h.runner.tasks) if t.kind == TaskKind.FIX]
        assert len(fixes) == 2, "the edit, then the description of its diff"
        assert all(h.runner.routes[i].primary == "codex" for i in fixes)
        [draft] = [
            p
            for p in (await maria.get("/api/proposals")).json()
            if p["action"]["action"]["kind"] == "pr.fix"
        ]
        payload = draft["action"]["action"]["payload"]
        assert payload["files"] == ["tests/test_one.py"] and payload["findings"] == [
            "F1"
        ]
        assert draft["requested_by"] == "maria"
        refused = await maria.post(f"/api/proposals/{draft['id']}/approve", json={})
        assert refused.status_code == 409
        await h.service.a.user_tokens.set("maria", UserToken(token="t"))
        ok = await maria.post(
            f"/api/proposals/{draft['id']}/approve", json={"as_me": True}
        )
        assert ok.status_code == 200, ok.text
        await h.settle()
    pr = h.forge.prs[(REPO, 42)]
    publisher = h.service.a.publisher
    assert publisher is not None
    [pushed] = [p for p in publisher.published if p["branch"].startswith("am/fix-42-")]
    assert pushed["author"] == ("maria", "maria@users.noreply.github.com")
    assert pushed["base"] == pr.head_sha and "Signed-off-by: maria" in pushed["message"]
    [opened] = h.forge.writes_of("create_pr")
    assert opened["base"] == pr.head_ref and opened["head"] == pushed["branch"]
    assert (
        opened["author"] == "maria"
        and opened["title"] == "test(loader): cover a batch of one"
    )


async def test_a_fix_can_go_straight_onto_the_prs_branch(demo) -> None:
    h, app = demo
    await _review(app, "maria", 42)
    await h.settle()

    def fix(task: Any) -> Any:
        if task.mode == "read_only":
            return {"title": "test: cover one", "summary": "Adds a test.", "fixed": []}
        (Path(task.workdir) / "test_one.py").write_text("def test_one(): ...\n")
        return AgentResult(runtime="scripted", ok=True, text="Added a test.")

    h.runner.on("fix", fix)
    await h.service.a.user_tokens.set("maria", UserToken(token="t"))
    async with _as(app, "maria") as maria:
        await maria.post(f"/api/prs/{REPO}/42/fix", json={})
        await h.settle()
        [draft] = [
            p
            for p in (await maria.get("/api/proposals")).json()
            if p["action"]["action"]["kind"] == "pr.fix"
        ]
        payload = {**draft["action"]["action"]["payload"], "target": "branch"}
        ok = await maria.post(
            f"/api/proposals/{draft['id']}/approve",
            json={"as_me": True, "payload": payload},
        )
        assert ok.status_code == 200, ok.text
        await h.settle()
    pr = h.forge.prs[(REPO, 42)]
    publisher = h.service.a.publisher
    assert publisher is not None
    [pushed] = publisher.published
    assert pushed["branch"] == pr.head_ref and pushed["base"] == pr.head_sha
    assert pushed["push_to"] == (pr.head_repo or REPO)
    assert pushed["author"] == ("maria", "maria@users.noreply.github.com")
    assert not h.forge.writes_of("create_pr"), "no PR into the PR"


async def test_merging_the_base_branch_without_conflicts_is_githubs_update(
    demo,
) -> None:
    h, app = demo
    async with _as(app, "maria") as maria:
        refused = await maria.post(f"/api/prs/{REPO}/42/merge-base", json={})
        assert refused.status_code == 403, "pushed as a person, never the service"
        await h.service.a.user_tokens.set("maria", UserToken(token="t"))
        out = (await maria.post(f"/api/prs/{REPO}/42/merge-base", json={})).json()
        await h.settle()
    assert out["run"] == "update"
    [update] = h.forge.writes_of("update_branch")
    assert update["number"] == 42 and update["author"] == "maria"


async def test_an_agent_resolves_conflicts_and_the_merge_is_pushed_as_the_approver(
    demo,
) -> None:
    from auto_maintainer.domain.snapshot import Mergeable

    h, app = demo
    pr = h.forge.prs[(REPO, 42)]
    h.forge.prs[(REPO, 42)] = pr.model_copy(update={"mergeable": Mergeable.CONFLICTING})
    h.service.a.workspace.merge_conflicts = {  # type: ignore[attr-defined]
        "src/loader.py": "<<<<<<< HEAD\nmine\n=======\ntheirs\n>>>>>>> main\n"
    }

    def resolve(task: Any) -> Any:
        assert "resolving merge conflicts for maria" in task.prompt
        assert (
            "src/loader.py" in task.prompt
            and "Do not run the PR's tests" in task.prompt
        )
        (Path(task.workdir) / "src" / "loader.py").write_text("mine and theirs\n")
        return AgentResult(runtime="scripted", ok=True, text="Kept both changes.")

    h.runner.on("fix", resolve)
    await h.service.a.user_tokens.set("maria", UserToken(token="t"))
    async with _as(app, "maria") as maria:
        out = (await maria.post(f"/api/prs/{REPO}/42/merge-base", json={})).json()
        assert out["run"] == "resolve"
        await h.settle()
        [draft] = [
            p
            for p in (await maria.get("/api/proposals")).json()
            if p["action"]["action"]["kind"] == "pr.fix"
        ]
        payload = draft["action"]["action"]["payload"]
        assert payload["merge_ref"] == "main" and payload["target"] == "branch"
        assert payload["resolved"] == {"src/loader.py": "mine and theirs\n"}
        ok = await maria.post(
            f"/api/proposals/{draft['id']}/approve", json={"as_me": True}
        )
        assert ok.status_code == 200, ok.text
        await h.settle()
    publisher = h.service.a.publisher
    assert publisher is not None
    [pushed] = publisher.published
    assert pushed["merge_sha"] == "d" * 40 and pushed["branch"] == pr.head_ref
    assert pushed["resolved"] == {"src/loader.py": "mine and theirs\n"}
    assert pushed["author"][0] == "maria"


async def test_a_merge_goes_as_a_pr_into_the_pr_when_its_fork_refuses_pushes(
    demo,
) -> None:
    from auto_maintainer.domain.snapshot import Mergeable

    h, app = demo
    pr = h.forge.prs[(REPO, 42)].model_copy(
        update={
            "mergeable": Mergeable.CONFLICTING,
            "head_repo": "alice/widget",
            "maintainer_can_modify": False,
        }
    )
    h.forge.put_pr(pr)
    await h.store.save_snapshot(pr)
    h.service.a.workspace.merge_conflicts = {  # type: ignore[attr-defined]
        "src/loader.py": "<<<<<<< HEAD\nmine\n=======\ntheirs\n>>>>>>> main\n"
    }

    def resolve(task: Any) -> Any:
        (Path(task.workdir) / "src" / "loader.py").write_text("mine and theirs\n")
        return AgentResult(runtime="scripted", ok=True, text="Kept both changes.")

    h.runner.on("fix", resolve)
    await h.service.a.user_tokens.set("maria", UserToken(token="t"))
    async with _as(app, "maria") as maria:
        await maria.post(f"/api/prs/{REPO}/42/merge-base", json={})
        await h.settle()
        [draft] = [
            p
            for p in (await maria.get("/api/proposals")).json()
            if p["action"]["action"]["kind"] == "pr.fix"
        ]
        assert draft["can_push"] is False, "the fork does not let maintainers push"
        payload = {**draft["action"]["action"]["payload"], "target": "pr"}
        ok = await maria.post(
            f"/api/proposals/{draft['id']}/approve",
            json={"as_me": True, "payload": payload},
        )
        assert ok.status_code == 200, ok.text
        await h.settle()
    publisher = h.service.a.publisher
    assert publisher is not None
    [pushed] = publisher.published
    assert pushed["branch"].startswith("am/merge-42-")
    assert pushed["merge_sha"] == "d" * 40
    assert pushed["resolved"] == {"src/loader.py": "mine and theirs\n"}
    [opened] = h.forge.writes_of("create_pr")
    assert opened["repo"] == "alice/widget" and opened["base"] == pr.head_ref
    assert opened["head"] == f"acme:{pushed['branch']}"


# -- labels the review suggests ----------------------------------------------------------


def _review_with_labels(labels: list[str]):
    from auto_maintainer.testing.demo import _answer

    def answer(task: Any) -> dict:
        return {**_answer(task), "labels": labels}

    return answer


async def test_a_review_suggests_only_the_repositorys_own_labels(demo) -> None:
    h, app = demo
    pr = h.forge.prs[(REPO, 42)]
    h.runner.on(
        "review",
        _review_with_labels(
            [
                "enhancement",
                "needs review",  # a role label the service manages
                "no-such-label",
                *pr.labels,  # already on the PR
                "documentation",
                "enhancement",
                "reproduced",
            ]
        ),
    )
    await _review(app, "maria", 42)
    await h.settle()
    [task] = [t for t in h.runner.tasks if t.kind == TaskKind.REVIEW]
    offered = task.prompt.split("Suggesting labels:")[1].split("Only these exist:")[1]
    offered = offered.split("</untrusted-data>")[0]
    assert "documentation" in offered and "enhancement" in offered
    assert "needs review" not in offered and "run-ci" not in offered
    async with _as(app, "maria") as maria:
        drafts = [
            p
            for p in (await maria.get("/api/proposals")).json()
            if p["action"]["action"]["kind"] == "label.apply"
            and p["action"]["action"]["payload"]["labels"]
        ]
    [labels] = drafts
    assert labels["requested_by"] == "maria"
    assert labels["action"]["action"]["payload"]["labels"] == [
        "enhancement",
        "documentation",
        "reproduced",
    ]
    async with _as(app, "maria") as maria:
        ok = await maria.post(f"/api/proposals/{labels['id']}/approve", json={})
        assert ok.status_code == 200, ok.text
    await h.settle()
    # the PR page shows the new labels at once, not after the next poll
    snapshot = await h.store.get_snapshot(REPO, 42)
    assert snapshot is not None
    assert {"enhancement", "documentation", "reproduced"} <= set(snapshot.labels)


async def test_a_repository_can_turn_label_suggestions_off(demo) -> None:
    from auto_maintainer.domain.memory import profile_path
    from auto_maintainer.profiles.layers import dump_yaml, parse_yaml

    h, app = demo
    layer = parse_yaml(h.memory.files[profile_path(REPO)], "settings")
    layer["review"] = {**(layer.get("review") or {}), "suggest_labels": False}
    h.memory.files[profile_path(REPO)] = dump_yaml(layer)
    await h.approve_directly(REPO, "maria")
    h.runner.on("review", _review_with_labels(["enhancement"]))
    await _review(app, "maria", 42)
    await h.settle()
    [task] = [t for t in h.runner.tasks if t.kind == TaskKind.REVIEW]
    assert "Suggesting labels:" not in task.prompt
    async with _as(app, "maria") as maria:
        assert not [
            p
            for p in (await maria.get("/api/proposals")).json()
            if p["action"]["action"]["kind"] == "label.apply"
            and p["action"]["action"]["payload"]["labels"]
        ]


# -- the repository's own review format ----------------------------------------------


QUESTIONS = [
    "1. What settings did you use to reproduce this PR?",
    "2. What were the reproduction results?",
    "9. What is your recommendation?",
]


def _skill_review(task: Any) -> dict:
    return {
        "summary": "Guards against empty batches.",
        "findings": [
            {
                "id": "F1",
                "severity": "major",
                "category": "correctness",
                "title": "Off-by-one in the batch loop",
                "body": "The loop skips the last batch.",
                "path": "src/loader.py",
                "line": 1,
            }
        ],
        "sections": {
            QUESTIONS[0]: "- No run was made; it needs one GPU.",
            QUESTIONS[1]: "- Not reproduced.",
            QUESTIONS[2]: "Changes requested\n- Fix F1 (`src/loader.py:1`).",
        },
        "recommendation": "Changes requested",
    }


async def test_a_review_posts_in_the_format_of_the_repositorys_skill(demo) -> None:
    h, app = demo
    pr = h.forge.prs[(REPO, 42)]
    _skill(h, 42, pr.head_sha)
    h.runner.on("review", _skill_review)
    await _review(app, "maria", 42)
    await h.settle()
    [task] = [t for t in h.runner.tasks if t.kind == TaskKind.REVIEW]
    assert "fixes the main comment's format" in task.prompt
    assert "The main review comment has these sections" not in task.prompt
    assert "Test and reproduce the PR on test machine gpu-01" in task.prompt
    assert task.lab_session, "the reviewing agent tests the PR in its own session"
    async with _as(app, "maria") as maria:
        [draft] = [
            p
            for p in (await maria.get("/api/proposals")).json()
            if p["action"]["action"]["kind"] == "review.post"
        ]
    payload = draft["action"]["action"]["payload"]
    headings = [
        line[4:] for line in payload["body"].splitlines() if line.startswith("### ")
    ]
    assert headings == [*QUESTIONS, "Findings"], (
        "the skill's sections, in its order, word for word, then the findings by id"
    )
    assert "### Summary" not in payload["body"]
    [comment] = payload["comments"]
    assert comment["path"] == "src/loader.py"
    assert comment["body"] == "The loop skips the last batch.", "posted as written"


async def test_a_review_reports_the_reproduction_on_record(demo) -> None:
    from auto_maintainer.domain.actions import (
        ActionKind,
        ActionStatus,
        LabReproducePayload,
        make_action,
    )
    from auto_maintainer.domain.records import ActionRecord

    h, app = demo
    pr = h.forge.prs[(REPO, 42)]
    _skill(h, 42, pr.head_sha)
    action = make_action(
        ActionKind.LAB_REPRODUCE,
        REPO,
        LabReproducePayload(
            lab="gpu-01",
            summary="Run the loader on an empty batch.",
            script="python -m loader --empty",
            expect="head passes, base crashes",
        ),
        number=42,
        head_sha=pr.head_sha,
        discriminator="req:x",
    )
    await h.store.add_action(
        ActionRecord(
            action=action,
            status=ActionStatus.EXECUTED,
            approver="maria",
            external_ref={
                "runs": {
                    "base": {"exit_code": 1, "tail": "Traceback"},
                    "head": {"exit_code": 0, "tail": "ok"},
                }
            },
        )
    )
    await h.store.upsert_job(
        Job(
            playbook="reproduce",
            key="reproduce:acme/widget#42:req:x",
            repo=REPO,
            number=42,
            pool="agent",
            status=JobStatus.DONE,
            result={"conclusion": "reproduced", "notes": "fails before, passes after"},
        )
    )
    h.runner.on("review", _skill_review)
    await _review(app, "maria", 42)
    await h.settle()
    [task] = [t for t in h.runner.tasks if t.kind == TaskKind.REVIEW]
    record = task.prompt.split("Reproduction on record")[1]
    assert "gpu-01" in record and '"conclusion": "reproduced"' in record
    assert "Traceback" not in record, "run output stays with the job"

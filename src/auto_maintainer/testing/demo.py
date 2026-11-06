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

"""``am serve --demo``: the whole service on fakes with sample data. Nothing leaves the host."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..domain.agents import AgentTask
from ..domain.permissions import Permission
from ..domain.snapshot import (
    CheckConclusion,
    CheckRun,
    CheckStatus,
    Commit,
    PrFile,
    Review,
    ReviewEventState,
)
from .fakes import make_pr
from .harness import Harness

DEMO_REPO = "acme/widget"
DEMO_USER = "maria"
DEMO_SERVICE_REPO = "acme/auto-maintainer"

DEMO_SETTINGS = """schema: 1
routing:
  evaluate: {primary: claude-code, fallback: [codex]}
system:
  poll_seconds: 300
"""

DEMO_SETTINGS_CHANGE = """schema: 1
providers:
  deepseek:
    name: DeepSeek
    protocol: anthropic
    base_url: https://api.deepseek.com/anthropic
    api_key: DEEPSEEK_API_KEY
    models: [deepseek-chat]
agents:
  claude-code: {provider: deepseek}
routing:
  evaluate: {primary: claude-code, fallback: [codex]}
system:
  poll_seconds: 300
"""

_SETTINGS_WORDS = (
    "provider",
    "deepseek",
    "model",
    "agent",
    "setting",
    "configure",
    "route",
)

LAYER: dict[str, Any] = {
    "extends": ["conventional-commits", "dco", "github-actions", "codeowners"],
    "labels": {
        "roles": {
            "needs_review": "needs review",
            "changes_requested": "waiting on author",
            "ci_gate": "run-ci",
            "community": "community",
        }
    },
    "ci": {"required_workflows": ["CI"]},
    "merge": {"enabled": True},
    "policy": {
        "label.apply": {"level": "auto"},
        "review.post": {"level": "propose"},
        "comment.upsert": {"level": "propose"},
        "ci.rerun_failed": {"level": "auto"},
    },
}


def _answer(task: AgentTask) -> dict[str, Any]:
    kind = str(task.kind)
    if kind == "triage":
        return {
            "summary": "Handles empty batches in the loader without crashing.",
            "labels": ["bug"],
        }
    if kind == "evaluate":
        return {
            "scores": {
                "impact": 6.5,
                "urgency": 7.0,
                "strategic": 5.0,
                "readiness": 8.0,
            },
            "summary": "Fixes a crash on empty input.",
            "rationale": "Small, tested crash fix on a core path.",
        }
    if kind == "review":
        return {
            "summary": "Adds a guard for empty batches and a regression test.",
            "findings": [
                {
                    "id": "F1",
                    "severity": "minor",
                    "category": "tests",
                    "title": "Test does not cover a batch size of one",
                    "body": "The new test checks zero items but not the boundary at one item.",
                    "path": "tests/test_loader.py",
                    "line": 20,
                }
            ],
            "recommendation": "Ready",
            "remaining_actions": ["Add the boundary case."],
        }
    if kind == "verify":
        return {
            "verdicts": [
                {"id": "F1", "confirmed": True, "reason": "the boundary is untested"}
            ]
        }
    if kind == "followup":
        return {"statuses": [], "new_findings": [], "recommendation": "Ready"}
    if kind == "ci_diagnose":
        return {
            "category": "pr_caused",
            "confidence": 0.7,
            "explanation": "test_loader fails on the new guard.",
            "evidence": [],
        }
    if kind == "calibrate":
        return {
            "disposition": "SINGLE_PR_OVERRIDE",
            "sufficiency": 7,
            "message": "Kept as a one-off override for this PR.",
        }
    if kind == "chat":
        last = task.prompt.rsplit("user: ", 1)[-1].split("\n", 1)[0].lower()
        if any(word in last for word in ("summar", "risk", "focus")):
            return {
                "reply": (
                    "#42 guards the loader against empty batches, which used to crash "
                    "it, and adds a regression test. The change is small and on a core "
                    "path; CI passes. One gap worth a comment: the test covers an empty "
                    "batch but not a batch of exactly one item."
                ),
                "proposals": [],
            }
        if any(word in last for word in _SETTINGS_WORDS):
            return {
                "reply": (
                    "Here is a draft: Claude Code runs on DeepSeek through its "
                    "Anthropic-compatible endpoint, and everything else stays as it is. "
                    "Apply it below, then paste the key into DEEPSEEK_API_KEY on the "
                    "Settings page."
                ),
                "proposals": [
                    {
                        "kind": "configure",
                        "repo": "",
                        "summary": "settings: run Claude Code on DeepSeek",
                        "settings_yaml": DEMO_SETTINGS_CHANGE,
                    }
                ],
            }
        return {
            "reply": "Two PRs wait for a first review; #42 is the top of your queue.",
            "proposals": [],
        }
    if kind == "fix":
        test = Path(task.workdir) / "tests" / "test_loader_one.py"
        test.parent.mkdir(parents=True, exist_ok=True)
        test.write_text(
            "from loader import load\n\n\ndef test_a_batch_of_one():\n"
            "    assert load([1]) == [1]\n"
        )
        return {
            "title": "test(loader): cover a batch of one item",
            "summary": "Adds the boundary test the review asked for.",
            "fixed": [{"id": "F1", "change": "added a test for a batch of one item"}],
        }
    if kind == "onboard":
        return {
            "profile": {"extends": ["conventional-commits"]},
            "questions": ["Which label means a PR needs review?"],
        }
    return {}


async def build_demo(root: Path) -> Harness:
    h = await Harness.create(
        root,
        repo=DEMO_REPO,
        layer=LAYER,
        maintainer=DEMO_USER,
        code_repo=DEMO_SERVICE_REPO,
    )
    h.memory.files["config/service.yaml"] = DEMO_SETTINGS
    h.forge.set_permission(DEMO_REPO, "bob", Permission.WRITE)
    for kind in (
        "triage",
        "evaluate",
        "review",
        "verify",
        "followup",
        "ci_diagnose",
        "calibrate",
        "chat",
        "onboard",
        "hygiene",
        "fix",
    ):
        h.runner.on(kind, _answer)
    h.forge.put_file(DEMO_REPO, ".github/CODEOWNERS", "* @maria\n/docs/ @bob\n")
    signed = "\n\nSigned-off-by: Alice <alice@example.com>"
    h.forge.put_pr(
        make_pr(
            number=42,
            title="fix(loader): handle empty batches",
            files=[
                PrFile(path="src/loader.py", additions=12, deletions=3),
                PrFile(path="tests/test_loader.py", additions=30),
            ],
            commits=[
                Commit(
                    sha="c" * 40, message="fix(loader): handle empty batches" + signed
                )
            ],
            checks=[
                CheckRun(
                    id=1,
                    name="unit",
                    workflow="CI",
                    status=CheckStatus.COMPLETED,
                    conclusion=CheckConclusion.SUCCESS,
                    run_id=10,
                )
            ],
        )
    )
    h.forge.put_pr(
        make_pr(
            number=43,
            sha="b" * 40,
            title="Add a progress bar",
            author="carol",
            files=[PrFile(path="src/ui.py", additions=80, deletions=5)],
            commits=[Commit(sha="e" * 40, message="Add a progress bar")],
            checks=[
                CheckRun(
                    id=2,
                    name="unit",
                    workflow="CI",
                    status=CheckStatus.COMPLETED,
                    conclusion=CheckConclusion.FAILURE,
                    run_id=11,
                )
            ],
        )
    )
    h.forge.put_pr(
        make_pr(
            number=44,
            sha="f" * 40,
            title="docs: explain batching",
            author="bob",
            author_association="MEMBER",
            files=[PrFile(path="docs/batching.md", additions=40)],
            commits=[Commit(sha="9" * 40, message="docs: explain batching" + signed)],
            reviews=[
                Review(
                    id=5,
                    author="maria",
                    state=ReviewEventState.APPROVED,
                    commit_sha="f" * 40,
                )
            ],
        )
    )
    h.forge.job_logs[2] = (
        "collecting...\nFAILED tests/test_ui.py::test_bar - AssertionError: expected 3 got 2\n"
    )
    await h.poll(DEMO_REPO)
    return h

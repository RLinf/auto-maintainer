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

import re
from pathlib import Path
from typing import Any

from ..domain.agents import AgentEvent, AgentEventType, AgentTask
from ..domain.permissions import Permission
from ..domain.settings import ModelSpec, Provider
from ..domain.snapshot import (
    CheckConclusion,
    CheckRun,
    CheckStatus,
    Commit,
    PrFile,
    Review,
    ReviewEventState,
    ReviewThread,
    ThreadComment,
)
from ..store.people import PeopleAgents
from .fakes import make_pr
from .harness import Harness

DEMO_REPO = "acme/widget"
DEMO_USER = "maria"
DEMO_SERVICE_REPO = "acme/auto-maintainer"

DEMO_LABS = """labs:
  gpu-01:
    description: 8× A100 80GB, CUDA 12.4
    host: ci@gpu-01
    setup: module load cuda/12.4
  cpu-01:
    description: 64 cores, no GPU
    host: ci@cpu-01
"""

DEMO_SETTINGS = (
    """schema: 1
routing:
  evaluate: {primary: claude-code, fallback: [codex]}
system:
  poll_seconds: 300
"""
    + DEMO_LABS
)

DEMO_SETTINGS_CHANGE = (
    """schema: 1
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
    + DEMO_LABS
)

DEMO_DIFF_42 = """diff --git a/src/loader.py b/src/loader.py
--- a/src/loader.py
+++ b/src/loader.py
@@ -8,9 +8,18 @@ from .batch import Batch

 def load(items, size=32):
-    batches = [items[i : i + size] for i in range(0, len(items), size)]
-    return [Batch(b) for b in batches]
+    \"\"\"Split ``items`` into batches of at most ``size``; no batch for no items.\"\"\"
+    if not items:
+        return []
+    if size < 1:
+        raise ValueError("size must be at least 1")
+    batches = []
+    for start in range(0, len(items), size):
+        chunk = items[start : start + size]
+        batches.append(Batch(chunk))
+    return batches


 def total(batches):
diff --git a/tests/test_loader.py b/tests/test_loader.py
--- a/tests/test_loader.py
+++ b/tests/test_loader.py
@@ -0,0 +1,30 @@
+import pytest
+
+from loader import load, total
+
+
+def test_no_items_gives_no_batches():
+    assert load([]) == []
+
+
+def test_size_must_be_positive():
+    with pytest.raises(ValueError):
+        load([1], size=0)
+
+
+def test_full_batches():
+    batches = load(list(range(64)), size=32)
+    assert [len(b) for b in batches] == [32, 32]
+
+
+def test_empty_batch_is_skipped():
+    batches = load([], size=8)
+    assert total(batches) == 0
+
+
+def test_last_batch_is_partial():
+    batches = load(list(range(10)), size=4)
+    assert [len(b) for b in batches] == [4, 4, 2]
+
+
+def test_total_counts_items():
+    assert total(load(list(range(5)), size=2)) == 5
"""


def _demo_steps() -> dict[str, Any]:
    """What the demo's agents appear to do, for the live view."""

    def say(text: str) -> AgentEvent:
        return AgentEvent(type=AgentEventType.MESSAGE, text=text)

    def run(
        name: str, ok: bool = True, out: str = "", **inputs: str
    ) -> list[AgentEvent]:
        return [
            AgentEvent(
                type=AgentEventType.TOOL_CALL,
                text=name,
                data={"name": name, "input": inputs},
            ),
            AgentEvent(
                type=AgentEventType.TOOL_RESULT, text=out, data={"is_error": not ok}
            ),
        ]

    talk = [
        say("Looking at the finding again."),
        *run("Read", file_path="tests/test_loader.py", out="def test_one_item(): ..."),
    ]
    review = [
        say("I'll read the change and the repository's review skill first."),
        *run(
            "Read",
            file_path=".agents/skills/code-review/SKILL.md",
            out="---\nname: code-review",
        ),
        *run("Read", file_path="src/loader.py", out="def load(items, size=32): ..."),
        *run("Grep", pattern="load\\(", out="tests/test_loader.py:7\nsrc/cli.py:41"),
        say(
            "The guard for empty input looks right. Checking whether the tests cover the boundary at one item."
        ),
        *run(
            "Bash",
            command="python -m pytest -q tests/test_loader.py",
            out="......\n6 passed in 0.21s",
        ),
        say(
            "All six tests pass, but none loads exactly one item, which is where the old slicing broke. One finding."
        ),
    ]
    return {
        "chat": [
            say("Reading what the dashboard shows for this."),
            *run("Bash", command="am-read /api/prs", out="[ ... 3 open PRs ... ]"),
            say("Writing the answer."),
        ],
        "review": lambda task: talk if TALKING in task.prompt else review,
        "fix": [
            say("Adding the boundary test the review asked for."),
            *run("Read", file_path="tests/test_loader.py"),
            *run("Edit", file_path="tests/test_loader_one.py"),
            *run(
                "Bash",
                command="python -m pytest -q tests/test_loader_one.py",
                out="1 passed",
            ),
            say("The new test passes on this branch."),
        ],
        "reproduce": lambda task: (
            [
                say("Installing the package on the machine, in the session's folder."),
                *run(
                    "Bash",
                    command="lab -- 'python -m venv .venv && . .venv/bin/activate "
                    "&& pip install -q -e .'",
                    out="Successfully installed widget-0.4.0",
                ),
                say("Now the check on the code before the PR."),
                *run(
                    "Bash",
                    ok=False,
                    command="lab --side base -- '. .venv/bin/activate && python check.py'",
                    out="AssertionError: expected 1 item, got 0",
                ),
                *run(
                    "Bash",
                    command="lab -- '. .venv/bin/activate && python check.py'",
                    out="1 passed",
                ),
                say("Base fails and head passes: the fix holds."),
            ]
            if "carry it out yourself" in task.prompt
            else [
                say("The claim: loading a batch of one item used to return nothing."),
                *run("Read", file_path="src/loader.py"),
                *run(
                    "Bash",
                    command="git log -1 --format=%s",
                    out="fix(loader): handle empty batches",
                ),
                say("A short check settles it: load([1]) and count the batches."),
            ]
        ),
    }


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
    "labs": {
        "gpu-01": {
            "use": True,
            "setup": "source ~/envs/widget/bin/activate",
            "timeout_seconds": 900,
        }
    },
    "policy": {
        "label.apply": {"level": "auto"},
        "review.post": {"level": "propose"},
        "comment.upsert": {"level": "propose"},
        "ci.rerun_failed": {"level": "auto"},
    },
}


TALKING = "is talking with you about it"
"""In the prompt of a reviewer's turn in the conversation about a review."""


def _talk(task: AgentTask, kind: str) -> dict[str, Any]:
    """A reviewer's turn in the conversation about a review: the demo agent explains
    its finding, and drops it when the reviewer asks it to."""
    newest = task.prompt.split("which you answer:\n", 1)[-1]
    last = newest.split("\n\n", 1)[0].lower()
    field = "review" if kind == "review" else "followup"
    if any(word in last for word in ("drop", "remove", "not needed")):
        revised: dict[str, Any] = (
            {
                "summary": "Adds a guard for empty batches and a regression test.",
                "findings": [],
                "recommendation": "Ready",
                "remaining_actions": [],
            }
            if kind == "review"
            else {"statuses": [], "new_findings": [], "recommendation": "Ready"}
        )
        return {
            "reply": "Agreed: `tests/test_loader.py:12` already loads a single item "
            "through the same path, so the boundary is covered. I removed F1 from the "
            "draft; it now recommends Ready.",
            field: revised,
        }
    return {
        "reply": "F1 is about the boundary at one item: the new test at "
        "`tests/test_loader.py:20` loads zero items, and the old slicing in "
        "`src/loader.py:31` broke at one. Ask me to drop it if another test covers "
        "that case.",
        field: None,
    }


REPRODUCED = "That reproduction has ended"
"""In the prompt that adds a finished reproduction to the review it followed."""


def _with_reproduction(kind: str) -> dict[str, Any]:
    """The review again, with what the reproduction showed."""
    if kind == "followup":
        return {
            "reply": "Added the reproduction's result.",
            "followup": {
                "statuses": [],
                "new_findings": [],
                "summary": "Reproduced on a test machine: a batch of one item loads at this head.",
                "recommendation": "Ready",
            },
        }
    return {
        "reply": "Added the reproduction's result to the review.",
        "review": {
            "summary": (
                "Adds a guard for empty batches and a regression test. Reproduced on a "
                "test machine: before this PR a batch of one item failed to load; at its "
                "head it loads."
            ),
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
        },
    }


def _machine_rules(prompt: str) -> dict[str, Any]:
    """The demo's draft of a test machine's enforced settings from its description:
    the obvious values picked out of the text."""
    import re

    text = prompt.split("Description:", 1)[-1]
    env = re.search(r"conda environment (\S+?)[ .,]", text)
    folder = re.search(r"(/[\w./-]+-runs)", text)
    hours = re.search(r"(\w+) hours? at most|at most (\w+) hours?", text)
    count = {"one": 1, "two": 2, "three": 3, "four": 4}
    limit = 30
    if hours:
        word = hours.group(1) or hours.group(2)
        limit = 60 * (int(word) if word.isdigit() else count.get(word, 1))
    forbid = []
    if "ray stop" in text:
        forbid.append(r"\bray\s+stop\b")
    if "/data/datasets" in text:
        forbid.append("/data/datasets")
    return {
        "workdir": folder.group(1) if folder else "am-labs",
        "setup": f"conda activate {env.group(1)}" if env else "",
        "timeout_minutes": limit,
        "forbid": forbid,
        "max_gpu_memory_mib": 500,
        "max_gpu_util_pct": 0,
        "left_to_agent": [],
    }


_PROGRESS_FINDINGS = [
    {
        "id": "F2",
        "severity": "major",
        "category": "correctness",
        "title": "The progress bar writes to stdout from every worker",
        "body": "Each data-loader worker draws its own bar, so piped output interleaves them.",
    },
    {
        "id": "F3",
        "severity": "minor",
        "category": "docs",
        "title": "The new --progress flag is not documented",
        "body": "The README lists the loader's flags but not this one.",
    },
]
"""What the demo's review finds in the progress-bar PR besides the shared finding,
so the demo's PRs show different findings."""


def _quality(task: AgentTask) -> dict[str, Any]:
    """The demo's judgment of a PR's quality: the progress bar is a useful feature
    with a design problem; the other PRs are small, well-placed fixes."""
    if "progress bar" in task.prompt:
        return {
            "score": 64,
            "reasons": {
                "en": "A useful feature, but **every worker draws its own bar**, so "
                "the output gets mixed up when work runs in parallel.\n\n"
                "- Settle where the bar is drawn first.\n"
                "- The missing documentation is a small fix.",
                "zh": "功能有用，但**每个工作进程各画一个进度条**，并行运行时输出会混在一起。\n\n"
                "- 先确定进度条由谁来画。\n"
                "- 缺少文档是小问题。",
            },
            "checks": {
                "requirements": True,
                "tests": True,
                "docs": False,
                "style": True,
                "design": False,
            },
        }
    return {
        "score": 88,
        "reasons": {
            "en": "A focused fix that follows the loader's existing checks; only a "
            "boundary test is missing.",
            "zh": "修复聚焦，沿用了加载器已有的检查方式；只缺一个边界测试。",
        },
        "checks": {
            "requirements": True,
            "tests": False,
            "docs": None,
            "style": True,
            "design": True,
        },
    }


def _translated(task: AgentTask) -> dict[str, Any]:
    """The demo's translation: the review's text, marked as translated."""
    import re

    from ..playbooks.prompts import _FENCE

    language = "中文" if "Simplified Chinese" in task.prompt else "English"
    texts = dict(
        re.findall(
            r"<untrusted-data source='([^']+)'>.*?"
            + re.escape(_FENCE)
            + r"\n(.*?)\n"
            + re.escape(_FENCE),
            task.prompt,
            re.S,
        )
    )
    comments = [
        part.split("\n", 1)[1]
        for part in re.split(
            r"\n\n(?=\[\d+\]\n)", texts.get("review.inline_comments", "")
        )
        if part.strip() and "\n" in part
    ]
    return {
        "body": f"*({language})*\n\n" + texts.get("review.body", ""),
        "comments": [f"*({language})* {c}" for c in comments],
    }


def _answer(task: AgentTask) -> dict[str, Any]:
    kind = str(task.kind)
    if kind == "comment_check":
        return _comment_check(task)
    if kind == "translate":
        return _translated(task)
    if kind == "review" and task.prompt.startswith("You judge the quality of PR"):
        return _quality(task)
    if kind == "evaluate" and "into a checklist for a quick check" in task.prompt:
        return {
            "items": [
                {"area": "tests", "check": "A behavior change comes with a test."},
                {"area": "docs", "check": "A new option is documented."},
                {
                    "area": "style",
                    "check": "Code follows the loader's existing checks.",
                },
            ]
        }
    if kind == "evaluate" and task.prompt.startswith("You give PR"):
        quick = _quality(task)
        docs = "docs: explain batching" in task.prompt
        return {**quick, "score": 93 if docs else quick["score"] - 6}
    if kind == "chat" and "You turn the description" in task.prompt:
        return _machine_rules(task.prompt)
    if kind in ("review", "followup") and TALKING in task.prompt:
        return _talk(task, kind)
    if kind in ("review", "followup") and REPRODUCED in task.prompt:
        return _with_reproduction(kind)
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
            ]
            + (_PROGRESS_FINDINGS if "progress bar" in task.prompt else []),
            "quality": _quality(task),
            "recommendation": "Ready",
            "remaining_actions": ["Add the boundary case."],
            # a label that fits the PR under review; one it already has is dropped
            "labels": ["enhancement"]
            if "progress bar" in task.prompt
            else ["documentation"]
            if "docs: explain" in task.prompt
            else ["bug"],
        }
    if kind == "verify":
        return {
            "verdicts": [
                {"id": "F1", "confirmed": True, "reason": "the boundary is untested"}
            ]
        }
    if kind == "followup":
        return {
            "statuses": [],
            "new_findings": [],
            "recommendation": "Ready",
            "quality": _quality(task),
        }
    if kind == "ci_diagnose":
        return {
            "category": "pr_caused",
            "confidence": 0.7,
            "explanation": "test_bar expects three steps and sees two.",
            "evidence": [],
        }
    if kind == "ci_root_cause":
        return {
            "causes": [
                {
                    "jobs": ["unit"],
                    "failing": "tests/test_ui.py::test_bar",
                    "origin": "change",
                    "cause": "ProgressBar.step draws the bar before it counts the "
                    "finished step, so after three steps it shows two.",
                    "culprits": [
                        {
                            "path": "src/ui.py",
                            "line": 41,
                            "why": "self.done += 1 comes after self.draw()",
                        }
                    ],
                    "fix": "Count the step before drawing: move self.done += 1 above "
                    "self.draw().",
                    "evidence": [
                        "FAILED tests/test_ui.py::test_bar - AssertionError: "
                        "expected 3 got 2"
                    ],
                    "confidence": 0.8,
                }
            ]
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
        props = (task.output_schema or {}).get("properties", {})
        if "pr_title" in props and "summary" not in props:
            # Asked whether the request was for the PR's own title or description.
            return {
                "pr_title": "fix(loader): handle empty batches and batches of one",
                "pr_body": "### Description\nThe loader returns an empty list for an "
                "empty batch and keeps a batch of one item.\n\n### Checklist\n"
                "- [x] tests",
            }
        request = task.prompt.split("What ", 1)[-1][:600]
        if task.mode == "workspace_write" and re.search(
            r"(?i)only\b.{0,60}\btitle", request
        ):
            return {
                "title": "Rename the PR",
                "summary": "Gave the PR a new title and description; changed no file.",
            }
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
    if kind == "reproduce" and "carry it out yourself" in task.prompt:
        return {
            "conclusion": "reproduced",
            "comment": (
                "Reproduced on a GPU test machine. Before this PR, loading a batch "
                "of one item raised `AssertionError: expected 1 item, got 0`; at "
                "this PR's head the same check passes (`1 passed`)."
            ),
            "notes": "",
        }
    if kind == "reproduce":
        return {
            "summary": "Load a batch of one item, which the review says is untested.",
            "steps": [
                "Install the package in a virtual environment in the session's folder.",
                "On the code before the PR, load a batch of one item and count it.",
                "Do the same at the PR's head.",
            ],
            "expect": "base raises AssertionError; head prints 1 passed",
            "needs_gpu": False,
            "timeout_minutes": 10,
        }
    if kind == "onboard":
        return {
            "profile": {"extends": ["conventional-commits"]},
            "questions": ["Which label means a PR needs review?"],
        }
    if kind == "governance":
        return _governance(task)
    return {}


DEMO_GOVERNANCE: dict[str, Any] = {
    "schema": 1,
    "established": True,
    "summary": "Reviewers own areas of the code; maintainers own the repository; the "
    "project lead settles disputes. From GOVERNANCE.md.",
    "sources": [
        {"kind": "file", "where": "GOVERNANCE.md"},
        {"kind": "file", "where": ".github/CODEOWNERS"},
    ],
    "roles": [
        {
            "id": "reviewer",
            "name": "Reviewer",
            "level": 1,
            "scoped": True,
            "github_permission": "write",
            "description": "Reviews PRs in the areas they know.",
            "responsibilities": [
                "Review PRs in their areas within two business days.",
                "Check that docs change with behavior.",
            ],
            "privileges": ["Approve PRs in their areas."],
        },
        {
            "id": "maintainer",
            "name": "Maintainer",
            "level": 2,
            "github_permission": "maintain",
            "maintains": True,
            "description": "Owns the quality and direction of the repository.",
            "responsibilities": [
                "Merge PRs once a reviewer of the area approves.",
                "Triage new issues every week.",
                "Nominate reviewers.",
            ],
            "privileges": ["Merge PRs.", "Change labels and releases."],
        },
        {
            "id": "lead",
            "name": "Project lead",
            "level": 3,
            "github_permission": "admin",
            "maintains": True,
            "description": "Settles what the maintainers cannot agree on.",
            "responsibilities": ["Decide the roadmap with the maintainers."],
            "privileges": ["Appoint and remove maintainers."],
        },
        {"id": "emeritus", "name": "Emeritus", "level": 0},
    ],
    "transitions": [
        {
            "id": "become-reviewer",
            "to_role": "reviewer",
            "criteria": [
                {
                    "text": "Five merged PRs in the area in the last year.",
                    "measure": "merged_prs",
                    "threshold": 5,
                    "window_months": 12,
                }
            ],
            "process": [
                "A maintainer nominates the contributor in a discussion.",
                "No maintainer objects for a week.",
            ],
            "decided_by": ["maintainer"],
        },
        {
            "id": "reviewer-to-maintainer",
            "from_role": "reviewer",
            "to_role": "maintainer",
            "criteria": [
                {
                    "text": "Six months as a reviewer.",
                    "measure": "months_in_role",
                    "threshold": 6,
                },
                {
                    "text": "Thirty reviews in the last year.",
                    "measure": "reviews",
                    "threshold": 30,
                    "window_months": 12,
                },
            ],
            "process": ["A maintainer nominates them.", "Two maintainers agree."],
            "decided_by": ["maintainer"],
            "approvals": 2,
        },
        {
            "id": "step-down",
            "from_role": "reviewer",
            "to_role": "emeritus",
            "criteria": [
                {
                    "text": "No reviews for six months.",
                    "measure": "inactive_months",
                    "threshold": 6,
                }
            ],
            "process": ["A maintainer asks them first."],
            "decided_by": ["maintainer"],
        },
    ],
    "score_label": "Contribution points",
    "members": [
        {
            "login": "maria",
            "role": "maintainer",
            "since": "2025-01-10",
            "score": 412.5,
        },
        {
            "login": "bob",
            "role": "reviewer",
            "areas": ["docs/"],
            "since": "2025-09-01",
            "score": 96.0,
            "next_step": "Thirty reviews in a year and six months as a reviewer make "
            "him a maintainer candidate; he has 18 reviews.",
        },
    ],
    "history": [],
    "open_questions": ["Does the project lead also count as a maintainer for merges?"],
}


def _people_turn(task: AgentTask) -> dict[str, Any]:
    """The demo agent's answer in the conversation on the People tab."""
    newest = task.prompt.split("which you answer:\n", 1)[-1].split("\n\n", 1)[0].lower()
    if "lead" in newest:
        return {
            "reply": "Noted: the project lead also merges like a maintainer. I added "
            "that to the lead's privileges and closed the question.",
            "governance": {
                **DEMO_GOVERNANCE,
                "roles": [
                    {**r, "privileges": [*r.get("privileges", []), "Merge PRs."]}
                    if r["id"] == "lead"
                    else r
                    for r in DEMO_GOVERNANCE["roles"]
                ],
                "open_questions": [],
            },
        }
    if "check who" in newest:
        return {
            "reply": "carol has five merged PRs in src/ui.py this year, which meets "
            "**become-reviewer**. I drafted her as a reviewer of src/ui.py; nobody "
            "else qualifies yet.",
            "governance": _governance(
                task.model_copy(
                    update={"prompt": "asked you to check who should change role"}
                )
            )["governance"],
        }
    if "governance" in newest or "process" in newest:
        return {
            "reply": "I read GOVERNANCE.md and CODEOWNERS and filled in the roles, how "
            "people move between them, and the members. One thing they do not say: "
            "**does the project lead also count as a maintainer for merges?**",
            "governance": DEMO_GOVERNANCE,
        }
    return {
        "reply": "maria is the maintainer, and bob reviews docs/. Ask me to change "
        "anything, or to check who should change role.",
        "governance": None,
    }


def _governance(task: AgentTask) -> dict[str, Any]:
    """The demo agent's People record: filled in from GOVERNANCE.md, or, when asked
    who should change role, with carol proposed as a reviewer."""
    if "talk with you about it" in task.prompt:
        return _people_turn(task)
    if "asked you to check who should change role" not in task.prompt:
        return {
            "governance": DEMO_GOVERNANCE,
            "summary": "Filled in the roles, how people move between them, and the "
            "current members from GOVERNANCE.md and CODEOWNERS.",
            "questions": DEMO_GOVERNANCE["open_questions"],
        }
    carol = {"login": "carol", "role": "reviewer", "areas": ["src/ui.py"]}
    return {
        "governance": {
            **DEMO_GOVERNANCE,
            "members": [*DEMO_GOVERNANCE["members"], carol],
            "history": [
                {
                    "login": "carol",
                    "to_role": "reviewer",
                    "on": "2026-01-06",
                    "by": "maria",
                    "reason": "become-reviewer: five merged PRs in src/ui.py this year",
                    "evidence": ["#31, #33, #36, #39, #40 merged since 2025-06"],
                }
            ],
        },
        "summary": "Propose carol as a reviewer of src/ui.py: five merged PRs there "
        "in the last year. Nobody else qualifies yet.",
    }


DEMO_OWN_API = Provider(
    name="My gateway",
    protocol="openai",
    base_url="https://gateway.example/v1",
    models=[
        ModelSpec(
            id="qwen3-coder",
            context_window=262_144,
            max_output=65_536,
            reasoning=["low", "medium", "high"],
            interfaces=["openai", "anthropic"],
            tools=True,
            images=False,
            streaming=True,
            tested_at="2026-11-10T08:00:00+00:00",
        ),
        ModelSpec(id="qwen3-small"),
    ],
)
"""The demo maintainer's own model API, used only by their own agents."""


DEMO_THREADS = [
    ReviewThread(
        id="RT_demo_1",
        path="src/loader.py",
        line=14,
        comments=[
            ThreadComment(
                id=901,
                author="bob",
                body="Return early when the batch is empty instead of indexing it.",
                commit_sha="c" * 40,
            )
        ],
    ),
    ReviewThread(
        id="RT_demo_2",
        path="tests/test_loader.py",
        line=8,
        comments=[
            ThreadComment(
                id=902,
                author="bob",
                body="Add a test for a batch of one item too.",
                commit_sha="c" * 40,
            )
        ],
    ),
    ReviewThread(
        id="RT_demo_3",
        path="src/loader.py",
        line=3,
        is_resolved=True,
        comments=[ThreadComment(id=903, author="maria", body="Typo in the docstring.")],
    ),
]
"""Review comments on the demo's PR #42, for its review progress."""


def _comment_check(task: AgentTask) -> dict[str, Any]:
    """The demo's comment check: the first comment fixed, the rest open."""
    items = []
    for i, block in enumerate(task.prompt.split("### Item ")[1:]):
        cid = block.split(":", 1)[0].strip()
        items.append(
            {
                "id": cid,
                "summary": "Return early on an empty batch"
                if i == 0
                else "Test a batch of one item",
                "status": "addressed" if i == 0 else "open",
                "reason": "load() returns [] before indexing"
                if i == 0
                else "no test for a single item yet",
                "fixed_in": "c" * 7 if i == 0 else None,
            }
        )
    return {"items": items}


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
    h.service.a.people = PeopleAgents(root / "people")
    await h.service.a.people.put_provider(DEMO_USER, "my-gateway", DEMO_OWN_API)
    for kind in (
        "triage",
        "evaluate",
        "review",
        "verify",
        "followup",
        "ci_diagnose",
        "ci_root_cause",
        "calibrate",
        "chat",
        "onboard",
        "hygiene",
        "fix",
        "reproduce",
        "governance",
        "translate",
        "comment_check",
    ):
        h.runner.on(kind, _answer)
    h.forge.put_file(DEMO_REPO, ".github/CODEOWNERS", "* @maria\n/docs/ @bob\n")
    h.forge.put_file(
        DEMO_REPO,
        "GOVERNANCE.md",
        "# Governance\n\nReviewers own areas; maintainers own the repository.\n",
    )
    h.forge.put_file(
        DEMO_REPO,
        "GOVERNANCE.md",
        "# Governance\n\nReviewers own areas; maintainers own the repository.\n",
    )
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
            threads=DEMO_THREADS,
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
    h.forge.diffs[(DEMO_REPO, 42)] = DEMO_DIFF_42
    h.runner.steps.update(_demo_steps())
    await h.service.reload_settings()
    # PR 43's unit job finishes, and fails, after the first look at the repository,
    # as it would on GitHub: that is what starts the CI doctor.
    running = await h.forge.get_pr(DEMO_REPO, 43)
    assert running is not None
    failed = running.checks
    h.forge.put_pr(
        running.model_copy(
            update={
                "checks": [
                    c.model_copy(
                        update={"status": CheckStatus.IN_PROGRESS, "conclusion": None}
                    )
                    for c in failed
                ]
            }
        )
    )
    await h.poll(DEMO_REPO)
    h.forge.put_pr(running.model_copy(update={"checks": failed}))
    await h.poll(DEMO_REPO)
    return h

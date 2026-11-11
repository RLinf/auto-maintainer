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

"""View rules and translations for the PR detail page (``ui/src/prActions.ts``)."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import quote

import pytest

UI_SRC = Path(__file__).resolve().parents[1] / "ui" / "src"
PR_ACTIONS_TS = UI_SRC / "prActions.ts"


def _node_ts_argv() -> list[str] | None:
    node = shutil.which("node")
    if node is None:
        return None
    for flags in (
        ["--input-type=module-typescript"],
        ["--experimental-strip-types", "--input-type=module-typescript"],
    ):
        probe = subprocess.run(
            [node, *flags, "-e", "const x: number = 1; void x;"],
            capture_output=True,
            text=True,
        )
        if probe.returncode == 0:
            return [node, *flags]
    return None


def test_pr_actions_view_rules() -> None:
    argv = _node_ts_argv()
    if argv is None:
        pytest.skip("needs Node.js with TypeScript support")
    module_url = f"file://{quote(str(PR_ACTIONS_TS))}"
    script = f"""
import assert from "node:assert/strict";
import {{
  activePrActionPanel,
  agentName,
  describeRunCi,
  prMenuItems,
  resolveAgentChoices,
}} from {module_url!r};

assert.equal(agentName("claude-code"), "Claude Code");
assert.equal(agentName("gemini"), "Gemini CLI");
assert.equal(agentName("custom-cli"), "custom-cli");
assert.equal(agentName(null), "");

const agents = [
  {{ name: "claude-code", enabled: true, installed: true, usable: true, mine: null }},
  {{ name: "codex", enabled: true, installed: true, usable: false, mine: "sign-in" }},
  {{ name: "gemini", enabled: false, installed: true, usable: true, mine: null }},
  {{ name: "opencode", enabled: true, installed: false, usable: true, mine: null }},
  {{ name: "antigravity", enabled: true, installed: true, usable: true, mine: null }},
];
const routes = [
  {{ task: "review", primary: "claude-code", runs_on: "claude-code" }},
  {{ task: "verify", primary: "claude-code", runs_on: "antigravity" }},
];

// A user's own agent leads on interactive tasks; choosing it explicitly collapses to "".
const reviewDefault = resolveAgentChoices("codex", "review", true, agents, routes);
assert.equal(reviewDefault.primary, "codex");
assert.equal(reviewDefault.current, "");
assert.deepEqual(
  reviewDefault.others.map((a) => a.name),
  ["claude-code", "antigravity"],
);

// Non-default usable agent stays selected; disabled remembered agent resets once loaded.
const explicit = resolveAgentChoices("antigravity", "review", true, agents, routes);
assert.equal(explicit.current, "antigravity");
assert.equal(resolveAgentChoices("gemini", "review", true, agents, routes).current, "");
assert.equal(resolveAgentChoices("gemini", "review", false, [], routes).current, "gemini");

// Verification keeps its configured route instead of switching to the user's own agent.
const verify = resolveAgentChoices("", "verify", true, agents, routes);
assert.equal(verify.primary, "antigravity");

// Contextual CI hides "none", turns "passed" into a pill, and gates "approval" on canPush.
assert.deepEqual(describeRunCi("none", true), {{ inline: "none", actionable: false, disabled: true }});
assert.deepEqual(describeRunCi("passed", true), {{ inline: "pill", actionable: false, disabled: true }});
assert.deepEqual(describeRunCi("running", true), {{ inline: "running", actionable: false, disabled: true }});
assert.deepEqual(describeRunCi("approval", false), {{ inline: "button", actionable: true, disabled: true }});
assert.deepEqual(describeRunCi("approval", true), {{ inline: "button", actionable: true, disabled: false }});
assert.deepEqual(describeRunCi("gated", false), {{ inline: "button", actionable: true, disabled: false }});
assert.deepEqual(describeRunCi("failed", false), {{ inline: "button", actionable: true, disabled: false }});

// Actions menu items follow open/closed state, review findings, failed jobs, and CI state.
assert.deepEqual(
  prMenuItems({{ isOpen: true, hasReviewFindings: true, failedCount: 2, ciState: "failed" }}),
  ["fix", "reproduce", "fix-ci", "run-ci", "merge-base"],
);
assert.deepEqual(
  prMenuItems({{ isOpen: false, hasReviewFindings: false, failedCount: 2, ciState: "passed" }}),
  ["reproduce"],
);

// Live PR updates that invalidate an open inline form return the popover to the menu.
const closedClean = {{ isOpen: false, hasReviewFindings: false, failedCount: 0, ciState: "none" as const }};
assert.equal(activePrActionPanel("fix", closedClean), "menu");
assert.equal(activePrActionPanel("fix-ci", closedClean), "menu");
assert.equal(activePrActionPanel("merge-base", closedClean), "menu");
assert.equal(activePrActionPanel("reproduce", closedClean), "reproduce");
"""
    proc = subprocess.run([*argv, "-e", script], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr or proc.stdout


def test_pr_page_translations_cover_every_literal_key() -> None:
    i18n_src = (UI_SRC / "i18n.tsx").read_text(encoding="utf-8")
    zh_keys = {
        (m.group(1) if m.group(1) is not None else m.group(2)).replace('\\"', '"')
        for m in re.finditer(
            r'^\s*(?:"((?:\\.|[^"\\])*)"|([A-Za-z_][A-Za-z0-9_]*))\s*:\s*"(?:\\.|[^"\\])+"',
            i18n_src,
            re.MULTILINE,
        )
    }
    for required in (
        "Actions",
        "More actions",
        "Back",
        "People",
        "Open on GitHub",
        "Open on GitLab",
    ):
        assert required in zh_keys

    missing: list[str] = []
    for rel in (
        "pages/Pr.tsx",
        "pages/Chat.tsx",
        "assignees.tsx",
        "proposal.tsx",
        "reproduce.tsx",
        "fixCi.tsx",
        "agentPicker.tsx",
        "agent.tsx",
        "reviewTalk.tsx",
        "people.tsx",
    ):
        src = (UI_SRC / rel).read_text(encoding="utf-8")
        for m in re.finditer(r'\bt\(\s*"((?:\\.|[^"\\])*)"\s*\)', src):
            key = m.group(1).replace('\\"', '"')
            if key not in zh_keys:
                missing.append(f"{rel}: {key}")
    assert missing == []

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

"""Collapse rules, toolbar placement, and translations for Approvals (``ui/src/proposalCollapse.ts``)."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import quote

import pytest

UI_SRC = Path(__file__).resolve().parents[1] / "ui" / "src"
COLLAPSE_TS = UI_SRC / "proposalCollapse.ts"


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


def test_proposal_collapse_state_rules() -> None:
    argv = _node_ts_argv()
    if argv is None:
        pytest.skip("needs Node.js with TypeScript support")
    module_url = f"file://{quote(str(COLLAPSE_TS))}"
    script = f"""
import assert from "node:assert/strict";
import {{
  collapseProposals,
  expandProposals,
  proposalCollapseState,
  toggleProposalCollapsed,
}} from {module_url!r};

// Empty views disable both bulk controls.
assert.deepEqual(proposalCollapseState(new Set(), []), {{
  allCollapsed: true,
  allExpanded: true,
}});

const view = [{{ id: "p1" }}, {{ id: "p2" }}, {{ id: "p3" }}];
const initial = new Set<string>();
assert.deepEqual(proposalCollapseState(initial, view), {{
  allCollapsed: false,
  allExpanded: true,
}});

// Toggling one card leaves both Collapse all and Expand all active, without mutating prior sets.
const one = toggleProposalCollapsed(initial, "p1");
assert.equal(initial.size, 0);
assert.deepEqual([...one], ["p1"]);
assert.deepEqual(proposalCollapseState(one, view), {{
  allCollapsed: false,
  allExpanded: false,
}});
assert.deepEqual([...toggleProposalCollapsed(one, "p1")], []);

// Collapsing and expanding a filtered view preserves collapsed state outside that view.
const withOtherTab = new Set(["other-tab"]);
const collapsedView = collapseProposals(withOtherTab, view);
assert.deepEqual([...collapsedView], ["other-tab", "p1", "p2", "p3"]);
assert.deepEqual(proposalCollapseState(collapsedView, view), {{
  allCollapsed: true,
  allExpanded: false,
}});

const reopenedOne = toggleProposalCollapsed(collapsedView, "p2");
assert.deepEqual(proposalCollapseState(reopenedOne, view), {{
  allCollapsed: false,
  allExpanded: false,
}});

const expandedView = expandProposals(collapsedView, view);
assert.deepEqual([...expandedView], ["other-tab"]);
assert.deepEqual(proposalCollapseState(expandedView, view), {{
  allCollapsed: false,
  allExpanded: true,
}});
"""
    proc = subprocess.run([*argv, "-e", script], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr or proc.stdout


def test_approvals_collapse_wiring_and_translations() -> None:
    i18n_src = (UI_SRC / "i18n.tsx").read_text(encoding="utf-8")
    zh_map: dict[str, str] = {}
    for m in re.finditer(
        r'^\s*(?:"((?:\\.|[^"\\])*)"|([A-Za-z_][A-Za-z0-9_]*))\s*:\s*"((?:\\.|[^"\\])+)"',
        i18n_src,
        re.MULTILINE,
    ):
        key = json.loads(f'"{m.group(1)}"') if m.group(1) is not None else m.group(2)
        zh_map[key] = json.loads(f'"{m.group(3)}"')

    assert zh_map.get("Collapse") == "折叠"
    assert zh_map.get("Expand") == "展开"
    assert zh_map.get("Collapse all") == "折叠所有"
    assert zh_map.get("Expand all") == "展开所有"

    missing: list[str] = []
    for rel in ("pages/Inbox.tsx", "proposal.tsx", "list.tsx"):
        src = (UI_SRC / rel).read_text(encoding="utf-8")
        for m in re.finditer(r'\bt\(\s*"((?:\\.|[^"\\])*)"\s*\)', src):
            key = m.group(1).replace('\\"', '"')
            if key not in zh_map:
                missing.append(f"{rel}: {key}")
    assert missing == []

    # ListToolbar places beforeGroup to the left of the Group button.
    list_src = (UI_SRC / "list.tsx").read_text(encoding="utf-8")
    spacer_idx = list_src.index('<span className="spacer" />')
    before_group_idx = list_src.index("{beforeGroup}", spacer_idx)
    group_idx = list_src.index("list.groups.length > 0", spacer_idx)
    assert spacer_idx < before_group_idx < group_idx

    # InboxPage wires Collapse all and Expand all into beforeGroup, and passes collapse props.
    inbox_src = (UI_SRC / "pages" / "Inbox.tsx").read_text(encoding="utf-8")
    assert "beforeGroup=" in inbox_src
    assert inbox_src.index('t("Collapse all")') < inbox_src.index('t("Expand all")')
    assert "collapsed={collapsed.has(p.id)}" in inbox_src
    assert "onToggleCollapse=" in inbox_src

    # ProposalCard puts the collapse button left of the checkbox, hides details when collapsed,
    # and keeps the bottom action bar (Post as me, etc.) outside the collapsed block.
    proposal_src = (UI_SRC / "proposal.tsx").read_text(encoding="utf-8")
    card_src = proposal_src[
        proposal_src.index("export function ProposalCard") : proposal_src.index(
            "function KindBody"
        )
    ]
    header_idx = card_src.index('<div className="proposal"')
    collapse_btn_idx = card_src.index("{onToggleCollapse && (", header_idx)
    checkbox_idx = card_src.index('type="checkbox"', header_idx)
    collapsed_body_idx = card_src.index("{!collapsed && (", checkbox_idx)
    details_idx = card_src.index("{details}", collapsed_body_idx)
    non_decider_row_idx = card_src.index("{!done && !decides && (", details_idx)
    decider_row_idx = card_src.index("{!done && decides && (", non_decider_row_idx)
    assert collapse_btn_idx < checkbox_idx < collapsed_body_idx < details_idx
    # Ensure the {!collapsed && ( block closes before the bottom action rows begin.
    between = card_src[collapsed_body_idx:non_decider_row_idx]
    assert between.count("(") == between.count(")")
    assert decider_row_idx > non_decider_row_idx

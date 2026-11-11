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

"""Mobile layout invariants for the dashboard at 360-430px and <=640px."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import quote

import pytest

UI_SRC = Path(__file__).resolve().parents[1] / "ui" / "src"
STYLES_CSS = UI_SRC / "styles.css"
POPOVER_TS = UI_SRC / "popover.ts"


def _strip_css_comments(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)


def _extract_blocks(css: str) -> list[tuple[str | None, str, str]]:
    """Return ``(media_condition, selector_list, declarations)`` in stylesheet order."""
    text = _strip_css_comments(css)
    out: list[tuple[str | None, str, str]] = []
    i = 0
    n = len(text)
    while i < n:
        while i < n and text[i].isspace():
            i += 1
        if i >= n:
            break
        brace = text.find("{", i)
        if brace == -1:
            break
        prelude = " ".join(text[i:brace].split())
        depth = 1
        j = brace + 1
        while j < n and depth > 0:
            if text[j] == "{":
                depth += 1
            elif text[j] == "}":
                depth -= 1
            j += 1
        body = text[brace + 1 : j - 1]
        if prelude.startswith("@media"):
            for _, sel, decls in _extract_blocks(body):
                out.append((prelude, sel, decls))
        elif not prelude.startswith("@"):
            out.append((None, prelude, body))
        i = j
    return out


def _media_applies(media: str | None, viewport_px: int) -> bool:
    if media is None:
        return True
    if "prefers-" in media:
        return False
    max_m = re.search(r"max-width:\s*(\d+)px", media)
    if max_m and viewport_px > int(max_m.group(1)):
        return False
    min_m = re.search(r"min-width:\s*(\d+)px", media)
    if min_m and viewport_px < int(min_m.group(1)):
        return False
    return bool(max_m or min_m)


def _decls_for(
    blocks: list[tuple[str | None, str, str]], target: str, viewport_px: int
) -> dict[str, str]:
    """Cascade declarations whose selector list contains ``target`` at ``viewport_px``."""
    props: dict[str, str] = {}
    for media, selector_list, body in blocks:
        if not _media_applies(media, viewport_px):
            continue
        selectors = [" ".join(s.split()) for s in selector_list.split(",")]
        if target not in selectors:
            continue
        for item in body.split(";"):
            if ":" not in item:
                continue
            k, v = item.split(":", 1)
            props[k.strip()] = " ".join(v.split())
    return props


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


def test_mobile_css_collapses_grids_and_relaxes_fixed_widths() -> None:
    blocks = _extract_blocks(STYLES_CSS.read_text(encoding="utf-8"))

    # Desktop keeps its multi-column layouts and minimum widths.
    assert _decls_for(blocks, ".title-cell", 1280)["min-width"] == "260px"
    assert _decls_for(blocks, ".search", 1280)["min-width"] == "240px"
    assert (
        _decls_for(blocks, ".split", 1280)["grid-template-columns"]
        == "minmax(0, 1fr) 340px"
    )
    assert (
        _decls_for(blocks, ".split.memory-browser", 1280)["grid-template-columns"]
        == "320px minmax(0, 1fr)"
    )
    assert _decls_for(blocks, ".props", 1280)["grid-template-columns"] == "auto 1fr"
    assert (
        _decls_for(blocks, ".level-row", 1280)["grid-template-columns"]
        == "minmax(0, 1fr) 90px 40px 120px"
    )

    # Below 640px (including 375px phones), fixed min-widths drop and grids collapse to 1fr.
    single_col_selectors = (
        ".grid2",
        ".split",
        ".split.memory-browser",
        ".chat",
        ".tiles",
        ".choices",
        ".agent-options",
        ".steps",
        ".skill-options",
        ".api-form-grid",
        ".progress-stats",
        ".sandbox-grid",
        ".pv-row",
        ".owner-group",
        ".people .role-grid",
        ".people .role-cols",
        ".props",
        ".level-row",
    )
    for vw in (375, 640):
        assert _decls_for(blocks, ".title-cell", vw)["min-width"] == "0"
        assert _decls_for(blocks, ".search", vw)["min-width"] == "0"
        for sel in single_col_selectors:
            assert _decls_for(blocks, sel, vw).get("grid-template-columns") == "1fr", (
                f"{sel} at {vw}px"
            )

        # Primary-column tables (including Pull requests) become stacked cards at <=640px.
        assert _decls_for(blocks, "table.cards", vw).get("display") == "block"
        assert _decls_for(blocks, "table.cards thead", vw).get("display") == "none"
        assert "calc(100% - 36px)" in _decls_for(
            blocks, "table.cards td.primary", vw
        ).get("flex", "")

        # Plain tables scroll sideways in .table-wrap instead of crushing cells to 1 word per line.
        assert _decls_for(blocks, ".table-wrap", vw).get("overflow-x") == "auto"
        assert _decls_for(blocks, ".table-wrap td", vw) == {}
        assert (
            _decls_for(blocks, ".table-wrap > table:not(.cards)", vw).get("min-width")
            == "520px"
        )

    # Long tokens wrap anywhere; tabs and code/diff boxes scroll horizontally without shrinking.
    assert _decls_for(blocks, "body", 375).get("overflow-wrap") == "anywhere"
    assert _decls_for(blocks, ".tabs", 375).get("overflow-x") == "auto"
    assert _decls_for(blocks, ".tabs button", 375).get("flex") == "0 0 auto"
    assert _decls_for(blocks, ".tabs button", 375).get("white-space") == "nowrap"
    assert _decls_for(blocks, "pre.diff", 375).get("overflow-x") == "auto"
    assert _decls_for(blocks, ".md-code", 375).get("overflow-x") == "auto"


def test_tsx_avoids_rigid_widths_and_inline_grids() -> None:
    tsx_files = sorted(UI_SRC.rglob("*.tsx"))
    assert tsx_files

    inline_grids: list[str] = []
    rigid_min_widths: list[str] = []
    rigid_panel_widths: list[str] = []

    for path in tsx_files:
        rel = path.relative_to(UI_SRC).as_posix()
        src = path.read_text(encoding="utf-8")
        if "gridTemplateColumns" in src:
            inline_grids.append(rel)
        for m in re.finditer(r"minWidth\s*:\s*(\d+)", src):
            if int(m.group(1)) >= 120:
                rigid_min_widths.append(f"{rel}:{m.group(1)}")
        for m in re.finditer(r"style=\{\{[^}]*\bwidth\s*:\s*(\d+)", src):
            if int(m.group(1)) >= 240:
                rigid_panel_widths.append(f"{rel}:{m.group(1)}")

    assert inline_grids == []
    assert rigid_min_widths == []
    assert rigid_panel_widths == []

    # Popovers with target desktop widths clamp to calc(100vw - 24px) on phones.
    clamped_popovers = {
        "proposal.tsx": "min(340px, calc(100vw - 24px))",
        "reproduce.tsx": "min(360px, calc(100vw - 24px))",
        "fixCi.tsx": "min(360px, calc(100vw - 24px))",
        "App.tsx": "min(300px, calc(100vw - 24px))",
        "pages/homeParts.tsx": "min(360px, calc(100vw - 24px))",
        "pages/Settings.tsx": "min(380px, calc(100vw - 24px))",
    }
    for rel, expected in clamped_popovers.items():
        assert expected in (UI_SRC / rel).read_text(encoding="utf-8"), rel

    # Every DataTable page marks a primary column so narrow viewports render stacked cards.
    datatable_files = (
        "pages/Queue.tsx",
        "pages/Pr.tsx",
        "pages/Repos.tsx",
        "pages/Ci.tsx",
        "pages/Questions.tsx",
        "pages/Agents.tsx",
        "pages/Service.tsx",
        "pages/Audit.tsx",
        "people.tsx",
    )
    for rel in datatable_files:
        src = (UI_SRC / rel).read_text(encoding="utf-8")
        assert "<DataTable" in src and "primary: true" in src, rel


def test_phone_popover_and_text_container_widths_at_360_to_430px() -> None:
    argv = _node_ts_argv()
    if argv is None:
        pytest.skip("needs Node.js with TypeScript support")
    module_url = f"file://{quote(str(POPOVER_TS))}"
    script = f"""
import assert from "node:assert/strict";
import {{ placePopover }} from {module_url!r};

for (const vw of [360, 375, 430]) {{
  for (const target of [300, 340, 360, 380]) {{
    const panelWidth = Math.min(target, vw - 24);
    for (const align of ["left", "right"] as const) {{
      for (const trigger of [
        {{ left: 14, right: 96, top: 120, bottom: 154 }},
        {{ left: Math.floor(vw / 2) - 40, right: Math.floor(vw / 2) + 40, top: 200, bottom: 234 }},
        {{ left: vw - 98, right: vw - 14, top: 12, bottom: 46 }},
      ]) {{
        const p = placePopover({{
          trigger,
          panelWidth,
          panelHeight: 280,
          align,
          viewportWidth: vw,
          viewportHeight: 667,
        }});
        assert.ok(p.left >= 12, `left=${{p.left}} at vw=${{vw}}`);
        assert.ok(p.left + panelWidth <= vw - 12, `right=${{p.left + panelWidth}} > ${{vw - 12}}`);
        // Padding inside a popover (2 * 6px panel + 2 * 6px stack) leaves >= 120px for text.
        assert.ok(panelWidth - 24 >= 120, `text width too narrow at vw=${{vw}}`);
      }}
    }}
  }}

  // Main page text containers at phone widths (content padding 2*14, card padding 2*18 + 2 border).
  const contentWidth = vw - 28;
  const cardInner = contentWidth - 38;
  const prCardTitle = contentWidth - 2 - 28 - 36;
  const reviewerMain = cardInner - 64;
  const commentMain = cardInner - 10 - 12 - 30;
  const scoreBarName = 120;
  for (const [name, width] of Object.entries({{
    contentWidth,
    cardInner,
    prCardTitle,
    reviewerMain,
    commentMain,
    scoreBarName,
  }})) {{
    assert.ok(width >= 120, `${{name}}=${{width}} < 120 at vw=${{vw}}`);
    assert.ok(width <= vw, `${{name}}=${{width}} > ${{vw}}`);
  }}
}}
"""
    proc = subprocess.run([*argv, "-e", script], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr or proc.stdout

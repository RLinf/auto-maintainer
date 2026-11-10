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

"""Viewport clamping and vertical flipping for dashboard popovers (``ui/src/popover.ts``)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from urllib.parse import quote

import pytest

POPOVER_TS = Path(__file__).resolve().parents[1] / "ui" / "src" / "popover.ts"


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


def test_popovers_stay_inside_the_viewport_and_flip_when_space_below_is_tight() -> None:
    argv = _node_ts_argv()
    if argv is None:
        pytest.skip("needs Node.js with TypeScript support")
    module_url = f"file://{quote(str(POPOVER_TS))}"
    script = f"""
import assert from "node:assert/strict";
import {{ applyPopoverLayout, placePopover }} from {module_url!r};

// An align="right" button (Fix CI, Reproduce, Merge, Approve) that wrapped to the left
// edge of .page-head .actions clamps to 12px instead of extending off the left edge.
const wrapped = placePopover({{
  trigger: {{ left: 20, right: 105, top: 180, bottom: 214 }},
  panelWidth: 394,
  panelHeight: 380,
  align: "right",
  viewportWidth: 1280,
  viewportHeight: 800,
}});
assert.equal(wrapped.left, 12);
assert.equal(wrapped.top, 220);
assert.equal(wrapped.above, false);
assert.equal(wrapped.maxHeight, 568);
assert.equal(wrapped.maxWidth, 1256);

// With room on the left, align="right" keeps the panel's right edge on the trigger's right.
const anchoredRight = placePopover({{
  trigger: {{ left: 1100, right: 1185, top: 180, bottom: 214 }},
  panelWidth: 394,
  panelHeight: 320,
  align: "right",
  viewportWidth: 1280,
  viewportHeight: 900,
}});
assert.equal(anchoredRight.left, 1185 - 394);
assert.equal(anchoredRight.maxHeight, 600);

// An align="left" popover near the right edge clamps 12px inside the viewport.
const clampedRight = placePopover({{
  trigger: {{ left: 820, right: 920, top: 120, bottom: 154 }},
  panelWidth: 374,
  panelHeight: 240,
  align: "left",
  viewportWidth: 1024,
  viewportHeight: 800,
}});
assert.equal(clampedRight.left, 1024 - 12 - 374);

// When the desktop .agent-panel is open (viewportWidth >= 560), page popovers stay 12px
// to its left when they fit, never go below left = 12 when they do not, and header menus
// over the panel keep anchoring to their own button.
const besideAgent = placePopover({{
  trigger: {{ left: 420, right: 500, top: 180, bottom: 214 }},
  panelWidth: 380,
  panelHeight: 300,
  align: "left",
  viewportWidth: 1000,
  viewportHeight: 800,
  agentLeft: 548,
}});
assert.equal(besideAgent.left, 548 - 12 - 380);

const narrowDesktopWithAgent = placePopover({{
  trigger: {{ left: 20, right: 105, top: 180, bottom: 214 }},
  panelWidth: 394,
  panelHeight: 300,
  align: "right",
  viewportWidth: 750,
  viewportHeight: 800,
  agentLeft: 298,
}});
assert.equal(narrowDesktopWithAgent.left, 12);
assert.ok(narrowDesktopWithAgent.left + 394 <= 750 - 12);

const headerOverAgent = placePopover({{
  trigger: {{ left: 1236, right: 1262, top: 11, bottom: 45 }},
  panelWidth: 240,
  panelHeight: 280,
  align: "right",
  viewportWidth: 1280,
  viewportHeight: 800,
  agentLeft: 828,
}});
assert.equal(headerOverAgent.left, 1262 - 240);

// On a phone (< 560px), maxWidth is constrained to 100vw - 24px and left stays at 12px.
const phone = placePopover({{
  trigger: {{ left: 16, right: 100, top: 160, bottom: 194 }},
  panelWidth: 380,
  panelHeight: 320,
  align: "right",
  viewportWidth: 360,
  viewportHeight: 640,
  agentLeft: 0,
}});
assert.equal(phone.maxWidth, 336);
assert.equal(phone.left, 12);

// When space below is tight (< 260px) and space above is larger, the panel flips above
// the trigger and caps maxHeight to the space above so buttons stay reachable.
const flipped = placePopover({{
  trigger: {{ left: 400, right: 490, top: 560, bottom: 594 }},
  panelWidth: 394,
  panelHeight: 360,
  naturalHeight: 360,
  align: "right",
  viewportWidth: 1280,
  viewportHeight: 760,
}});
assert.equal(flipped.above, true);
assert.equal(flipped.maxHeight, 542);
assert.equal(flipped.top, 560 - 6 - 360);

// When space below is tight (< 260px) and space above is even smaller, it stays below
// and clamps maxHeight to the space below.
const stayBelow = placePopover({{
  trigger: {{ left: 200, right: 290, top: 140, bottom: 174 }},
  panelWidth: 340,
  panelHeight: 208,
  naturalHeight: 360,
  align: "left",
  viewportWidth: 800,
  viewportHeight: 400,
}});
assert.equal(stayBelow.above, false);
assert.equal(stayBelow.maxHeight, 208);
assert.equal(stayBelow.top, 180);
assert.equal(stayBelow.top + stayBelow.maxHeight, 400 - 12);

// applyPopoverLayout measures DOM boxes, applies clamped fixed styles + overflowY: auto,
// toggles pop-above, and accounts for body.agent-open on desktop.
const classes = new Set<string>(["agent-open"]);
const panelClasses = new Set<string>();
const style: Record<string, string> = {{}};
(globalThis as Record<string, unknown>).window = {{ innerWidth: 1000, innerHeight: 720 }};
(globalThis as Record<string, unknown>).document = {{
  documentElement: {{ clientWidth: 1000, clientHeight: 720 }},
  body: {{ classList: {{ contains: (c: string) => classes.has(c) }} }},
  querySelector: (sel: string) => (sel === ".agent-panel.open" ? {{ offsetWidth: 440 }} : null),
}};
const anchor = {{
  getBoundingClientRect: () => ({{ left: 20, right: 105, top: 540, bottom: 574 }}),
}} as unknown as HTMLElement;
const panel = {{
  style,
  scrollHeight: 420,
  offsetHeight: 422,
  clientHeight: 420,
  getBoundingClientRect: () => ({{
    width: 394,
    height: Math.min(422, parseFloat(style.maxHeight) || 422),
  }}),
  classList: {{
    toggle: (name: string, force?: boolean) => {{
      if (force) panelClasses.add(name);
      else panelClasses.delete(name);
    }},
  }},
}} as unknown as HTMLElement;

applyPopoverLayout(anchor, panel, "right", true);
assert.equal(style.left, "12px");
assert.equal(style.maxWidth, "976px");
assert.equal(style.maxHeight, "522px");
assert.equal(style.top, `${{540 - 6 - 422}}px`);
assert.equal(style.overflowY, "auto");
assert.equal(panelClasses.has("pop-above"), true);
"""
    result = subprocess.run([*argv, "-e", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr or result.stdout

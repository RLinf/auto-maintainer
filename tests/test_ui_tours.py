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

"""The dashboard's walkthroughs (``ui/src/tours.ts``) point at the dashboard as it is:
every step's target is a ``data-tour`` name a page carries, every step opens a page
(and a tab that page has), every example a step shows exists, and every text is in
both languages with buttons named as the dashboard names them."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

import pytest

UI_SRC = Path(__file__).resolve().parents[1] / "ui" / "src"
TOURS_TS = UI_SRC / "tours.ts"
GUIDE_TS = UI_SRC / "guide.ts"

CHIP = re.compile(r"\[\[([^\]]+)\]\]")
LINK = re.compile(r"(?<!\[)\[([^\[\]]+)\]\(([^)\s]+)\)")
ENTRY = re.compile(
    r'^\s*(?:"((?:[^"\\]|\\.)*)"|([A-Za-z_]\w*)):\s*"((?:[^"\\]|\\.)*)",?\s*$'
)
# Where a page's `?tab=` values are defined, by the page's path.
TAB_PAGES = {"/repos/": "pages/Repos.tsx", "/settings": "pages/Settings.tsx"}


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


@pytest.fixture(scope="module")
def tours() -> dict:
    argv = _node_ts_argv()
    if argv is None:
        pytest.skip("needs Node.js with TypeScript support")
    script = f"""
import * as t from {f"file://{quote(str(TOURS_TS))}"!r};
import * as g from {f"file://{quote(str(GUIDE_TS))}"!r};
console.log(JSON.stringify({{
  tours: t.TOURS,
  roles: Object.keys(g.ROLE_LABEL),
  selector: t.targetSelector("draft draft-post"),
}}));
"""
    out = subprocess.run([*argv, "-e", script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def _sources() -> str:
    return "\n".join(
        p.read_text(encoding="utf-8")
        for p in UI_SRC.rglob("*.tsx")
        if "static" not in p.parts
    )


def _texts(tours: dict) -> list[tuple[str, dict]]:
    out: list[tuple[str, dict]] = []
    for tour in tours["tours"]:
        out += [
            (f"{tour['id']}.title", tour["title"]),
            (f"{tour['id']}.summary", tour["summary"]),
        ]
        for i, step in enumerate(tour["steps"]):
            where = f"{tour['id']}.step{i + 1}"
            out += [(f"{where}.title", step["title"]), (f"{where}.body", step["body"])]
            if step.get("missing"):
                out.append((f"{where}.missing", step["missing"]))
    return out


def _zh_labels() -> dict[str, str]:
    labels: dict[str, str] = {}
    for line in (UI_SRC / "i18n.tsx").read_text(encoding="utf-8").splitlines():
        m = ENTRY.match(line)
        if m:
            key = (
                json.loads(f'"{m.group(1)}"') if m.group(1) is not None else m.group(2)
            )
            labels[key] = json.loads(f'"{m.group(3)}"')
    return labels


def _routes() -> list[str]:
    app = (UI_SRC / "App.tsx").read_text(encoding="utf-8")
    return [p for p in re.findall(r'<Route\s+path="([^"]+)"', app) if p != "*"]


def _is_page(path: str, routes: list[str]) -> bool:
    for route in routes:
        pattern = re.escape(route).replace(r"\*", ".*")
        pattern = re.sub(r":\w+", r"[^/]+", pattern)
        if re.fullmatch(pattern, path):
            return True
    return False


def test_every_target_is_on_a_page(tours: dict) -> None:
    src = _sources()
    named = set(re.findall(r'\b(?:data-tour|tour)="([\w-]+)"', src))
    # A target given by a condition, e.g. data-tour={kind === "x" ? "draft" : undefined}.
    for expr in re.findall(r"\bdata-tour=\{([^}]*)\}", src):
        named |= set(re.findall(r'"([\w-]+)"', expr))
    missing = [
        f"{tour['id']}.step{i + 1}: {name}"
        for tour in tours["tours"]
        for i, step in enumerate(tour["steps"])
        for name in (step.get("target") or "").split()
        if name not in named
    ]
    assert missing == []
    assert tours["selector"] == '[data-tour="draft"] [data-tour="draft-post"]'


def test_every_step_opens_a_page_and_its_tab(tours: dict) -> None:
    routes = _routes()
    bad: list[str] = []
    for tour in tours["tours"]:
        for i, step in enumerate(tour["steps"]):
            if not step.get("path"):
                continue
            where = f"{tour['id']}.step{i + 1}"
            filled = step["path"].replace("{pr}", "acme/widget/7")
            filled = filled.replace("{repo}", "acme/widget")
            url = urlsplit(filled)
            if not _is_page(url.path, routes):
                bad.append(f"{where}: {step['path']}")
            for tab in parse_qs(url.query).get("tab", []):
                page = next(
                    (f for p, f in TAB_PAGES.items() if url.path.startswith(p)), None
                )
                text = (UI_SRC / page).read_text(encoding="utf-8") if page else ""
                if f'id: "{tab}"' not in text:
                    bad.append(f"{where}: no tab {tab} on {url.path}")
    assert bad == []


def test_every_example_a_step_shows_exists(tours: dict) -> None:
    tour_tsx = (UI_SRC / "tour.tsx").read_text(encoding="utf-8")
    block = tour_tsx[tour_tsx.index("export const EXAMPLES") :]
    block = block[: block.index("};")]
    examples = set(re.findall(r"^\s*(\w+):", block, re.M))
    wanted = {
        s["example"] for t in tours["tours"] for s in t["steps"] if s.get("example")
    }
    assert wanted and wanted <= examples


def test_every_text_is_in_both_languages_with_the_dashboards_labels(
    tours: dict,
) -> None:
    cjk = re.compile(r"[一-鿿]")
    zh = _zh_labels()
    routes = _routes()
    wrong: list[str] = []
    for where, text in _texts(tours):
        if not text["en"].strip() or not cjk.search(text["zh"]):
            wrong.append(f"{where}: not in both languages")
        en, cn = CHIP.findall(text["en"]), CHIP.findall(text["zh"])
        unknown = [label for label in en if label not in zh]
        if unknown:
            wrong.append(f"{where}: no such label {unknown}")
        elif sorted(zh[label] for label in en) != sorted(cn):
            wrong.append(f"{where}: {en} is {[zh[x] for x in en]} in Chinese, not {cn}")
        links = [href for _, href in LINK.findall(text["en"])]
        if links != [href for _, href in LINK.findall(text["zh"])]:
            wrong.append(f"{where}: links differ between languages")
        wrong += [f"{where}: {h}" for h in links if not _is_page(h, routes)]
    assert wrong == []


def test_tours_are_told_apart_and_meant_for_someone(tours: dict) -> None:
    ids = [t["id"] for t in tours["tours"]]
    assert len(ids) == len(set(ids))
    roles = set(tours["roles"]) - {"everyone"}
    for tour in tours["tours"]:
        assert tour["who"] and set(tour["who"]) <= roles, tour["id"]
        assert tour["steps"], tour["id"]

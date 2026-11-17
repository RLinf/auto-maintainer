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

"""The dashboard's guide (``ui/src/guide.ts``) names the dashboard as it is: every
button it names carries that button's label in both languages, every link leads to a
page, and every page in the top bar opens a topic of it."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import quote, urlsplit

import pytest

UI_SRC = Path(__file__).resolve().parents[1] / "ui" / "src"
GUIDE_TS = UI_SRC / "guide.ts"

CHIP = re.compile(r"\[\[([^\]]+)\]\]")
KEY = re.compile(r"\{\{([^}]+)\}\}")
LINK = re.compile(r"(?<!\[)\[([^\[\]]+)\]\(([^)\s]+)\)")
ENTRY = re.compile(
    r'^\s*(?:"((?:[^"\\]|\\.)*)"|([A-Za-z_]\w*)):\s*"((?:[^"\\]|\\.)*)",?\s*$'
)


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


def _nav_paths() -> list[str]:
    src = (UI_SRC / "palette.tsx").read_text(encoding="utf-8")
    block = src[
        src.index("export const NAV") : src.index("];", src.index("export const NAV"))
    ]
    return re.findall(r'\bto:\s*"([^"]+)"', block)


@pytest.fixture(scope="module")
def guide() -> dict:
    argv = _node_ts_argv()
    if argv is None:
        pytest.skip("needs Node.js with TypeScript support")
    probes = [
        *_nav_paths(),
        "/pr/acme/widget/7",
        "/repos/acme/widget",
        *(
            f"/repos/acme/widget?tab={t}"
            for t in ("people", "labs", "usage", "candidate", "settings")
        ),
        *(
            f"/settings?tab={t}"
            for t in (
                "agents",
                "providers",
                "routing",
                "usage",
                "labs",
                "system",
                "file",
            )
        ),
    ]
    script = f"""
import * as g from {f"file://{quote(str(GUIDE_TS))}"!r};
const probes = {json.dumps(probes)};
const help = Object.fromEntries(probes.map((p) => {{
  const [path, search] = p.split("?");
  return [p, g.helpTopicFor(path, search ? "?" + search : "")];
}}));
console.log(JSON.stringify({{
  topics: g.TOPICS, groups: g.GROUPS, paths: g.PATHS, glossary: g.GLOSSARY, faq: g.FAQ,
  roles: g.ROLE_LABEL, help,
  search: g.searchTopics("merge", "en").map((t) => t.id),
  searchZh: g.searchTopics("合并", "zh").map((t) => t.id),
  searchAcross: g.searchTopics("merge", "zh").map((t) => t.id),
}}));
"""
    out = subprocess.run([*argv, "-e", script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


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


def _texts(guide: dict) -> list[tuple[str, dict]]:
    """Every bilingual text in the guide, with where it is."""
    out: list[tuple[str, dict]] = []
    for topic in guide["topics"]:
        where = topic["id"]
        out += [
            (f"{where}.title", topic["title"]),
            (f"{where}.summary", topic["summary"]),
        ]
        out += [(f"{where}.step{i + 1}", s) for i, s in enumerate(topic["steps"])]
        out += [
            (f"{where}.tip{i + 1}", s) for i, s in enumerate(topic.get("tips") or [])
        ]
        out += [(f"{where}.open", o["label"]) for o in topic.get("open") or []]
        if topic.get("ask"):
            out.append((f"{where}.ask", topic["ask"]))
    for group in guide["groups"]:
        out += [
            (f"group {group['id']}", group["title"]),
            (f"group {group['id']}", group["blurb"]),
        ]
    for path in guide["paths"]:
        out += [
            (f"path {path['role']}", path["label"]),
            (f"path {path['role']}", path["blurb"]),
        ]
    for entry in guide["glossary"]:
        out += [
            (f"glossary {entry['term']['en']}", entry["term"]),
            (f"glossary {entry['term']['en']}", entry["text"]),
        ]
    for entry in guide["faq"]:
        out += [
            (f"faq {entry['q']['en']}", entry["q"]),
            (f"faq {entry['q']['en']}", entry["a"]),
        ]
    out += [(f"role {r}", t) for r, t in guide["roles"].items()]
    return out


def _routes() -> list[str]:
    app = (UI_SRC / "App.tsx").read_text(encoding="utf-8")
    return [p for p in re.findall(r'<Route\s+path="([^"]+)"', app) if p != "*"]


def _is_page(href: str, routes: list[str]) -> bool:
    path = urlsplit(href).path
    for route in routes:
        pattern = re.escape(route).replace(r"\*", ".*")
        pattern = re.sub(r":\w+", r"[^/]+", pattern)
        if re.fullmatch(pattern, path):
            return True
    return False


def test_every_text_is_written_in_both_languages(guide: dict) -> None:
    cjk = re.compile(r"[一-鿿]")
    missing = [
        w for w, t in _texts(guide) if not t["en"].strip() or not t["zh"].strip()
    ]
    untranslated = [w for w, t in _texts(guide) if not cjk.search(t["zh"])]
    assert missing == [] and untranslated == []


def test_buttons_are_named_as_the_dashboard_labels_them(guide: dict) -> None:
    zh = _zh_labels()
    wrong: list[str] = []
    for where, text in _texts(guide):
        en, cn = CHIP.findall(text["en"]), CHIP.findall(text["zh"])
        unknown = [label for label in en if label not in zh]
        if unknown:
            wrong.append(f"{where}: no such label {unknown}")
        elif sorted(zh[label] for label in en) != sorted(cn):
            wrong.append(
                f"{where}: {en} is {[zh[label] for label in en]} in Chinese, not {cn}"
            )
        if KEY.findall(text["en"]) != KEY.findall(text["zh"]):
            wrong.append(f"{where}: keys differ between languages")
    assert wrong == []


def test_links_lead_to_pages(guide: dict) -> None:
    routes = _routes()
    bad: list[str] = []
    for where, text in _texts(guide):
        en = [href for _, href in LINK.findall(text["en"])]
        if en != [href for _, href in LINK.findall(text["zh"])]:
            bad.append(f"{where}: links differ between languages")
        bad += [f"{where}: {href}" for href in en if not _is_page(href, routes)]
    for topic in guide["topics"]:
        bad += [
            f"{topic['id']}.open: {o['to']}"
            for o in topic.get("open") or []
            if not _is_page(o["to"], routes)
        ]
    assert bad == []


def test_topics_groups_and_paths_fit_together(guide: dict) -> None:
    ids = [t["id"] for t in guide["topics"]]
    assert len(ids) == len(set(ids))
    groups = {g["id"] for g in guide["groups"]}
    assert {t["group"] for t in guide["topics"]} == groups
    for path in guide["paths"]:
        assert path["topics"] and set(path["topics"]) <= set(ids), path["role"]
    assert {r for t in guide["topics"] for r in t["who"]} <= set(guide["roles"])


def test_every_page_opens_a_topic_about_it(guide: dict) -> None:
    ids = {t["id"] for t in guide["topics"]}
    for page, topic in guide["help"].items():
        if page == "/guide":
            assert topic is None
        else:
            assert topic in ids, page
    assert guide["help"]["/pr/acme/widget/7"] == "pr-page"
    assert guide["help"]["/repos/acme/widget?tab=people"] == "people"
    assert guide["help"]["/settings?tab=labs"] == "my-machines"


def test_search_finds_a_topic_by_its_title_in_either_language(guide: dict) -> None:
    assert guide["search"][0] in {"merge-pr", "merge-schedule"}
    assert guide["searchZh"][0] in {"merge-pr", "merge-schedule"}
    assert "merge-schedule" in guide["searchAcross"]

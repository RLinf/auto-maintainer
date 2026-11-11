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

"""Remembered PR list view state, breadcrumbs, and translations (``ui/src/viewState.ts``)."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import quote

import pytest

UI_SRC = Path(__file__).resolve().parents[1] / "ui" / "src"
VIEW_STATE_TS = UI_SRC / "viewState.ts"


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


def test_view_state_preserves_home_and_prs_filters_and_sorting() -> None:
    argv = _node_ts_argv()
    if argv is None:
        pytest.skip("needs Node.js with TypeScript support")
    module_url = f"file://{quote(str(VIEW_STATE_TS))}"
    script = f"""
import assert from "node:assert/strict";
import {{
  HOME_ORDER_KEY,
  HOME_VIEW_KEY,
  PRS_VIEW_KEY,
  REPO_KEY,
  enterView,
  homeHref,
  navHref,
  prBreadcrumbs,
  previewViewEntry,
  prsHref,
  readRememberedRepo,
  rememberRepo,
  resolveHomeOrder,
  saveView,
  writeHomeOrder,
}} from {module_url!r};

function makeStorage() {{
  const data = new Map<string, string>();
  return {{
    data,
    getItem: (k: string) => (data.has(k) ? data.get(k)! : null),
    setItem: (k: string, v: string) => {{
      data.set(k, v);
    }},
    removeItem: (k: string) => {{
      data.delete(k);
    }},
  }};
}}

const store = makeStorage();

// Home order defaults to "priority", falls back to am.home.order, and honors rorder in URL.
assert.equal(resolveHomeOrder("", store), "priority");
writeHomeOrder("oldest", store);
assert.equal(store.getItem(HOME_ORDER_KEY), "oldest");
assert.equal(resolveHomeOrder("", store), "oldest");
assert.equal(resolveHomeOrder("?rorder=updated", store), "updated");
assert.equal(resolveHomeOrder("?rorder=bogus", store), "oldest");

// Saving Home filters & sorting omits repo and transient job params, and updates am.home.order.
saveView(
  "home",
  "?repo=org/a&rq=scheduler&only=owner&staffed=no-reviewers&rorder=updated&rall=1&show=question&order=old&job=j1",
  {{ repo: "org/a", storage: store }},
);
assert.equal(
  store.getItem(HOME_VIEW_KEY),
  "rq=scheduler&only=owner&staffed=no-reviewers&rorder=updated&rall=1&show=question&order=old",
);
assert.equal(store.getItem(HOME_ORDER_KEY), "updated");

// Entering bare / or /?repo=... restores the remembered Home view on first render and entry.
assert.equal(
  previewViewEntry("home", "?repo=org/a", {{ storage: store }}).toString(),
  "repo=org%2Fa&rq=scheduler&only=owner&staffed=no-reviewers&rorder=updated&rall=1&show=question&order=old",
);
const restoredHome = enterView("home", "?repo=org/a", {{ storage: store }});
assert.equal(restoredHome.restored, true);
assert.equal(
  restoredHome.search,
  "?repo=org%2Fa&rq=scheduler&only=owner&staffed=no-reviewers&rorder=updated&rall=1&show=question&order=old",
);

// Saving and restoring /prs filters, search, tabs, grouping, sorting, and paging.
rememberRepo("org/a", store);
assert.equal(readRememberedRepo(store), "org/a");
saveView(
  "prs",
  "?repo=org/a&tab=review&q=memory&f.tier=P0,P1&f.author=alice&sort=-updated&group=state&page=2&job=j9",
  {{ repo: "org/a", storage: store }},
);
assert.equal(
  store.getItem(PRS_VIEW_KEY),
  "tab=review&q=memory&f.tier=P0%2CP1&f.author=alice&sort=-updated&group=state&page=2",
);
const restoredPrs = enterView("prs", "", {{ storage: store }});
assert.equal(restoredPrs.restored, true);
assert.equal(
  restoredPrs.search,
  "?tab=review&q=memory&f.tier=P0%2CP1&f.author=alice&sort=-updated&group=state&page=2",
);

// Explicit URL filter links (like Home's /prs?f.tier=P0&repo=...) win and overwrite storage.
assert.equal(
  previewViewEntry("prs", "?f.tier=P0&repo=org/a", {{ storage: store }}).toString(),
  "f.tier=P0&repo=org%2Fa",
);
const explicitLink = enterView("prs", "?f.tier=P0&repo=org/a", {{ storage: store }});
assert.equal(explicitLink.restored, false);
assert.equal(explicitLink.search, "?f.tier=P0&repo=org%2Fa");
assert.equal(store.getItem(PRS_VIEW_KEY), "f.tier=P0");

// Resetting/clearing filters immediately persists the cleared state so old filters do not return.
saveView("home", "?rorder=newest", {{ storage: store }});
assert.equal(enterView("home", "", {{ storage: store }}).search, "?rorder=newest");
saveView("home", "", {{ storage: store }});
assert.equal(enterView("home", "", {{ storage: store }}).search, "");
assert.equal(enterView("home", "", {{ storage: store }}).restored, false);

saveView("prs", "", {{ storage: store }});
assert.equal(enterView("prs", "", {{ storage: store }}).search, "");
assert.equal(enterView("prs", "", {{ storage: store }}).restored, false);

// am.repo governs repository agreement (never frozen in am.view.*), and page drops on repo switch.
rememberRepo("org/a", store);
saveView("prs", "?tab=review&q=fix&page=3", {{ repo: "org/a", storage: store }});
assert.equal(prsHref("org/a", store), "/prs?repo=org%2Fa&tab=review&q=fix&page=3");
assert.equal(prsHref(undefined, store), "/prs?tab=review&q=fix&page=3");
assert.equal(prsHref("org/b", store), "/prs?repo=org%2Fb&tab=review&q=fix");

// Entering /prs?repo=org/b directly drops the stale page offset from both URL and storage.
const switchedViaUrl = enterView("prs", "?repo=org/b", {{ storage: store }});
assert.equal(switchedViaUrl.search, "?repo=org%2Fb&tab=review&q=fix");
assert.equal(store.getItem(PRS_VIEW_KEY), "tab=review&q=fix");

// Switching repo via useRepoScope (rememberRepo) also drops page while keeping am.repo synced.
saveView("prs", "?tab=mine&page=1", {{ repo: "org/b", storage: store }});
rememberRepo("org/b", store);
assert.equal(store.getItem(PRS_VIEW_KEY), "tab=mine&page=1");
rememberRepo("org/c", store);
assert.equal(store.getItem(REPO_KEY), "org/c");
assert.equal(store.getItem(PRS_VIEW_KEY), "tab=mine");

// Breadcrumbs on PrPage and navHref preserve remembered Home and /prs views.
saveView("home", "?rq=bug&only=reviewer&rorder=updated", {{ storage: store }});
saveView("prs", "?tab=review&f.tier=P0&sort=-opened", {{ storage: store }});
assert.deepEqual(
  prBreadcrumbs("RLinf/rlinf", "home", {{ home: "Home", prs: "Pull requests" }}, store),
  [
    {{ to: "/?repo=RLinf%2Frlinf&rq=bug&only=reviewer&rorder=updated", label: "Home" }},
    {{ to: "/prs?repo=RLinf%2Frlinf&tab=review&f.tier=P0&sort=-opened", label: "RLinf/rlinf" }},
  ],
);
assert.deepEqual(
  prBreadcrumbs("RLinf/rlinf", "prs", {{ home: "Home", prs: "Pull requests" }}, store),
  [
    {{ to: "/prs?tab=review&f.tier=P0&sort=-opened", label: "Pull requests" }},
    {{ to: "/prs?repo=RLinf%2Frlinf&tab=review&f.tier=P0&sort=-opened", label: "RLinf/rlinf" }},
  ],
);
assert.deepEqual(
  prBreadcrumbs("gitlab:group/sub/project", undefined, {{ home: "首页", prs: "拉取请求" }}, store),
  [
    {{ to: "/prs?tab=review&f.tier=P0&sort=-opened", label: "拉取请求" }},
    {{
      to: "/prs?repo=gitlab%3Agroup%2Fsub%2Fproject&tab=review&f.tier=P0&sort=-opened",
      label: "gitlab:group/sub/project",
    }},
  ],
);

assert.equal(homeHref(undefined, store), "/?rq=bug&only=reviewer&rorder=updated");
assert.equal(navHref("/", {{ pathname: "/pr/RLinf/rlinf/42", storage: store }}), "/?rq=bug&only=reviewer&rorder=updated");
assert.equal(navHref("/prs", {{ pathname: "/pr/RLinf/rlinf/42", storage: store }}), "/prs?tab=review&f.tier=P0&sort=-opened");
assert.equal(navHref("/prs", {{ pathname: "/prs", search: "?repo=org/c&q=live&job=j2", storage: store }}), "/prs?repo=org%2Fc&q=live");
assert.equal(navHref("/ci", {{ pathname: "/", storage: store }}), "/ci");

// Unavailable or throwing localStorage never breaks view resolution or href helpers.
const broken = {{
  getItem: () => {{
    throw new Error("SecurityError");
  }},
  setItem: () => {{
    throw new Error("QuotaExceededError");
  }},
  removeItem: () => {{
    throw new Error("SecurityError");
  }},
}};
saveView("home", "?rq=safe", {{ storage: broken }});
assert.equal(resolveHomeOrder("", broken), "priority");
assert.equal(enterView("prs", "?tab=review", {{ storage: broken }}).search, "?tab=review");
assert.equal(homeHref("org/a", broken), "/?repo=org%2Fa");
assert.equal(prsHref(undefined, broken), "/prs");
"""
    proc = subprocess.run([*argv, "-e", script], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr or proc.stdout


def test_touched_ui_files_have_chinese_translations() -> None:
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
        "Reset",
        "Home",
        "Pull requests",
        "Sort by",
        "Owners and reviewers",
    ):
        assert required in zh_keys

    missing: list[str] = []
    for rel in (
        "pages/Home.tsx",
        "pages/homeParts.tsx",
        "pages/Queue.tsx",
        "pages/Pr.tsx",
        "list.tsx",
        "repoScope.tsx",
        "ui.tsx",
        "App.tsx",
        "palette.tsx",
    ):
        src = (UI_SRC / rel).read_text(encoding="utf-8")
        for m in re.finditer(r'\bt\(\s*"((?:\\.|[^"\\])*)"\s*\)', src):
            key = m.group(1).replace('\\"', '"')
            if key not in zh_keys:
                missing.append(f"{rel}: {key}")
    assert missing == []

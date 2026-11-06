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

"""The built dashboard: where it lives, building it on first run, and the page shown
when it is missing.

The build output (``web/static/``) is not in git, so a fresh clone has none. ``am
serve`` builds it from ``ui/`` at startup whenever it is missing or older than its
sources, when Node is available; otherwise every page explains how to build it
instead of answering 404.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from html import escape
from pathlib import Path

STATIC = Path(__file__).parent / "static"
UI = Path(__file__).resolve().parents[3] / "ui"
"""The dashboard's sources in a source checkout; absent in an installed package."""


SOURCES = (
    "src",
    "public",
    "index.html",
    "package.json",
    "package-lock.json",
    "vite.config.ts",
    "tsconfig.json",
)


def is_built(static: Path = STATIC) -> bool:
    return (static / "index.html").is_file()


def _newest(paths: list[Path]) -> float:
    newest = 0.0
    for path in paths:
        files = path.rglob("*") if path.is_dir() else [path]
        for f in files:
            if f.is_file():
                newest = max(newest, f.stat().st_mtime)
    return newest


def needs_build(ui: Path = UI, static: Path = STATIC) -> bool:
    """Whether the build is missing or older than any of the dashboard's sources."""
    if not is_built(static):
        return True
    if not (ui / "package.json").is_file():
        return False
    built = (static / "index.html").stat().st_mtime
    return _newest([ui / name for name in SOURCES]) > built


def ensure_built(
    ui: Path = UI, static: Path = STATIC, echo: Callable[[str], None] = print
) -> bool:
    """Build the dashboard when it is missing or its sources changed since the last build.

    ``npm ci`` runs only when ``node_modules`` is missing or older than the lockfile.
    A failed rebuild keeps the previous build. Returns whether a build is there afterwards.
    """
    if not needs_build(ui, static):
        return True
    if not (ui / "package.json").is_file():
        echo(f"the dashboard is not built and {ui} has no sources to build it from")
        return False
    npm = shutil.which("npm")
    if npm is None:
        echo(
            "the dashboard needs building and npm is not installed; install Node 20 or "
            f"newer and run: cd {ui} && npm ci && npm run build"
        )
        return is_built(static)
    marker = ui / "node_modules" / ".package-lock.json"
    lock = ui / "package-lock.json"
    steps: list[list[str]] = []
    if not marker.is_file() or (
        lock.is_file() and lock.stat().st_mtime > marker.stat().st_mtime
    ):
        steps.append([npm, "ci", "--no-audit", "--no-fund"])
    steps.append([npm, "run", "build"])
    echo(
        "building the dashboard"
        + (" (installing its packages first)" if len(steps) == 2 else "")
    )
    for argv in steps:
        result = subprocess.run(argv, cwd=ui, capture_output=True, text=True)
        if result.returncode != 0:
            tail = (result.stdout + result.stderr).strip()[-1500:]
            kept = " the previous build stays." if is_built(static) else ""
            echo(f"npm {' '.join(argv[1:])} failed in {ui}:{kept}\n{tail}")
            return is_built(static)
    return is_built(static)


def missing_page() -> str:
    command = f"cd {escape(str(UI))} &amp;&amp; npm ci &amp;&amp; npm run build"
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>auto-maintainer</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>body{{font:16px/1.5 system-ui,sans-serif;max-width:40rem;margin:4rem auto;padding:0 1rem}}
code{{background:#f2f2f2;padding:.1rem .3rem;border-radius:4px}}</style></head>
<body><h1>The dashboard is not built</h1>
<p>The service is running, but this copy has no built dashboard. Build it with Node 20 or
newer, then reload this page:</p>
<p><code>{command}</code></p>
<p><code>am serve</code> does this on its own at startup when <code>npm</code> is installed.
The API works meanwhile: <a href="/api/docs">/api/docs</a>, <a href="/healthz">/healthz</a>.</p>
</body></html>"""

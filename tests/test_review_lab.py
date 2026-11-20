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

"""Review & lab execution: real git worktrees on test machines, sliding session
deadlines for large checkpoint/dataset downloads, clean unanchored review prompts,
and transient git fetch retries."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

from auto_maintainer.domain.actions import ActionKind, LabReproducePayload
from auto_maintainer.domain.clock import FixedClock
from auto_maintainer.domain.events import Event, EventKind
from auto_maintainer.domain.profile import LabBinding
from auto_maintainer.domain.settings import LabMachine
from auto_maintainer.domain.snapshot import Commit, PrFile
from auto_maintainer.labs.executor import MAX_SESSION_SECONDS, LabExecutor
from auto_maintainer.labs.hosts import LocalLabHost
from auto_maintainer.store.sqlite import SqliteStore
from auto_maintainer.testing.fakes import FakeLabHost, make_pr, report
from auto_maintainer.testing.harness import Harness
from auto_maintainer.workspace.git import GitWorkspace


def _git(cwd: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(cwd), *args],
        text=True,
        stderr=subprocess.STDOUT,
    ).strip()


async def test_lab_open_session_creates_real_git_worktree_and_restores_instructions(
    tmp_path: Path,
) -> None:
    """``open_session`` pushes to a bare mirror on the test machine, creates a real
    git worktree of the PR head, restores trusted instruction/skill files from
    ``origin/<default_branch>``, and removes the worktree cleanly on close."""
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _git(upstream, "init", "-b", "main")
    _git(upstream, "config", "user.email", "t@example.com")
    _git(upstream, "config", "user.name", "Test")
    (upstream / "app.py").write_text("x = 1\n", encoding="utf-8")
    (upstream / "AGENTS.md").write_text("trusted main agents\n", encoding="utf-8")
    skill = upstream / ".agents" / "skills" / "review-pr" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("trusted skill\n", encoding="utf-8")
    _git(upstream, "add", ".")
    _git(upstream, "commit", "-m", "base")
    base_sha = _git(upstream, "rev-parse", "HEAD")

    _git(upstream, "checkout", "-b", "pr-7")
    (upstream / "app.py").write_text("x = 2\n", encoding="utf-8")
    (upstream / "AGENTS.md").write_text("HIJACKED BY PR\n", encoding="utf-8")
    skill.write_text("HIJACKED SKILL\n", encoding="utf-8")
    _git(upstream, "add", ".")
    _git(upstream, "commit", "-m", "pr change")
    head_sha = _git(upstream, "rev-parse", "HEAD")
    _git(upstream, "update-ref", "refs/pull/7/head", head_sha)

    ws_root = tmp_path / "ws"
    mirror = ws_root / "mirrors" / "acme__widget.git"
    mirror.parent.mkdir(parents=True)
    subprocess.check_call(
        ["git", "clone", "--bare", str(upstream), str(mirror)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _git(mirror, "update-ref", "refs/remotes/origin/main", base_sha)
    _git(mirror, "update-ref", "refs/pull/7/head", head_sha)
    ws = GitWorkspace(ws_root)

    store = await SqliteStore.open(tmp_path / "am.db")
    labs = LabExecutor({}, {}, store, FixedClock())
    lab_root = tmp_path / "lab-work"
    labs.configure({"local-box": LabMachine(transport="local", workdir=str(lab_root))})
    labs.hosts["local-box"] = LocalLabHost("local-box")
    try:

        async def sync_git(target_url: str, ssh_command: str | None) -> None:
            await ws.push_mirror(
                "acme/widget",
                "main",
                head_sha,
                target_url,
                ssh_command=ssh_command,
                number=7,
            )

        opened = await labs.open_session(
            LabReproducePayload(
                lab="local-box", summary="review", sides=["base", "head"]
            ),
            "acme/widget",
            7,
            {},
            "act_wt000001",
            binding=LabBinding(),
            sync_git=sync_git,
            default_branch="main",
            head_sha=head_sha,
            base_sha=base_sha,
        )
        sid = opened["session"]
        wt = Path(opened["worktree"])
        assert (wt / ".git").exists(), "real git worktree created"
        assert (wt / "app.py").read_text(encoding="utf-8") == "x = 2\n"
        assert (wt / "AGENTS.md").read_text(encoding="utf-8") == "trusted main agents\n"
        assert (wt / ".agents" / "skills" / "review-pr" / "SKILL.md").read_text(
            encoding="utf-8"
        ) == "trusted skill\n"
        diff = _git(wt, "diff", "--name-only", "origin/main...HEAD")
        assert "app.py" in diff

        await labs.close_session(sid)
        assert not wt.exists()
    finally:
        await store.close()


async def test_lab_session_slides_deadline_while_active_and_respects_machine_timeout(
    tmp_path: Path,
) -> None:
    """Omitting ``LabReproducePayload.timeout_seconds`` uses the machine's full limit
    (not a 1800 s clamp); active downloads/commands slide the session deadline forward
    up to ``MAX_SESSION_SECONDS`` and are never killed by ``expire_sessions()`` while
    running, while idle sessions still expire normally."""
    clock = FixedClock()
    store = await SqliteStore.open(tmp_path / "am.db")
    gate = asyncio.Event()
    started = asyncio.Event()

    class _SlowHost(FakeLabHost):
        async def run(
            self,
            command: str,
            *,
            cwd: str | None = None,
            env=None,
            timeout: float = 1800,
            stdin: str | None = None,
            on_output=None,
        ):
            if "snapshot_download" in command:
                started.set()
                await gate.wait()
                if on_output is not None:
                    await on_output("downloaded 100%")
                return type(
                    "R",
                    (),
                    {
                        "exit_code": 0,
                        "stdout": "downloaded 100%\n",
                        "stderr": "",
                        "timed_out": False,
                        "duration_seconds": 2400.0,
                    },
                )()
            return await super().run(
                command,
                cwd=cwd,
                env=env,
                timeout=timeout,
                stdin=stdin,
                on_output=on_output,
            )

    host = _SlowHost("gpu-01")
    labs = LabExecutor({}, {}, store, clock)
    labs.configure({"gpu-01": LabMachine(workdir="/tmp/lab", timeout_seconds=3600)})
    labs.hosts["gpu-01"] = host
    try:
        opened = await labs.open_session(
            LabReproducePayload(lab="gpu-01", summary="large download"),
            "acme/widget",
            9,
            {"head": ""},
            "act_slide001",
            binding=LabBinding(use=True),
        )
        sid = opened["session"]
        tok = labs.session_token(sid)
        assert tok is not None
        # Machine limit (3600s) is respected when payload.timeout_seconds is None.
        info = labs.session_info(tok)
        assert info is not None and info["seconds_left"] == 3600

        # Start a long download, advance clock past the initial 3600s deadline while active.
        run_task = asyncio.create_task(
            labs.run_in_session(tok, "python -c 'snapshot_download()'")
        )
        await started.wait()
        clock.advance(seconds=4000)
        assert await labs.expire_sessions() == 0, "active command is not killed"
        gate.set()
        res = await run_task
        assert res["exit_code"] == 0
        # After download finishes, session still has a fresh 3600s window left.
        info2 = labs.session_info(tok)
        assert info2 is not None and info2["seconds_left"] == 3600

        # Advancing past the idle deadline with no active command expires it.
        clock.advance(seconds=3601)
        assert await labs.expire_sessions() == 1
        assert labs.session_token(sid) is None

        # Hard ceiling still bounds runaway sessions even if active.
        opened2 = await labs.open_session(
            LabReproducePayload(lab="gpu-01", summary="ceiling test"),
            "acme/widget",
            10,
            {"head": ""},
            "act_slide002",
            binding=LabBinding(use=True),
        )
        sid2 = opened2["session"]
        labs._sessions[sid2].active_commands = 1
        clock.advance(seconds=MAX_SESSION_SECONDS + 1)
        assert await labs.expire_sessions() == 1
        assert labs.session_token(sid2) is None
    finally:
        await store.close()


async def test_review_prompt_is_clean_with_and_without_test_machine(
    tmp_path: Path,
) -> None:
    """Reviews get full-PR scope, no ``earlier.work`` dump, generous timeout headroom
    when a test machine is attached, and a clean fallback when no machine is set up."""
    h = await Harness.create(tmp_path)
    try:
        h.service.a.workspace.diff_text = (
            "diff --git a/src/a.py b/src/a.py\n"
            "--- a/src/a.py\n+++ b/src/a.py\n@@ -1 +1 @@\n-1\n+2\n"
        )
        h.forge.put_pr(
            make_pr(
                number=55,
                files=[PrFile(path="src/a.py")],
                commits=[Commit(sha="d" * 40, message="feat: a")],
            )
        )
        h.runner.on("triage", lambda t: {"summary": "s", "labels": []})
        h.runner.on(
            "evaluate", lambda t: {"scores": {}, "summary": "s", "rationale": "r"}
        )
        h.runner.on("review", lambda t: report("# Review\n\nNo issues."))
        h.runner.on(
            "form",
            lambda t: {"summary": "Clean.", "findings": [], "recommendation": "Ready"},
        )

        # Case 1: No test machine configured -> clean fallback + full-scope framing,
        # and review_lessons apply during the form turn rather than the audit turn.
        h.memory.files["repos/acme/widget/knowledge/review_lessons.md"] = (
            "# Review lessons\n- Keep review comments concise.\n"
        )
        await h.emit(
            Event(
                kind=EventKind.DASHBOARD_COMMAND,
                repo="acme/widget",
                number=55,
                payload={"command": "review", "by": "maria", "source": "dashboard"},
            )
        )
        [review_task] = [t for t in h.runner.tasks if str(t.kind) == "review"]
        [form_task] = [t for t in h.runner.tasks if str(t.kind) == "form"]
        assert "No test machine is set up for this repository" in review_task.prompt
        assert "This is a full review of the entire PR diff" in review_task.prompt
        assert "earlier.work" not in review_task.prompt
        assert "Keep review comments concise." not in review_task.prompt
        assert "Keep review comments concise." in form_task.prompt

        # Case 2: Test machine configured -> clean lab prompt, no "You do not reproduce
        # it yourself" override, no earlier.work dump, generous min_timeout_seconds.
        h.runner.tasks.clear()
        h.service.info.machines["gpu-01"] = LabMachine(description="4x GPU")
        prof = await h.service.profiles.active("acme/widget")
        assert prof is not None
        prof.labs["gpu-01"] = LabBinding(use=True)
        await h.emit(
            Event(
                kind=EventKind.DASHBOARD_COMMAND,
                repo="acme/widget",
                number=55,
                payload={
                    "command": "review",
                    "by": "maria",
                    "source": "dashboard",
                    "full": True,
                },
            )
        )
        [lab_review] = [t for t in h.runner.tasks if str(t.kind) == "review"]
        assert "Test machine gpu-01 is yours while you work" in lab_review.prompt
        assert "real git worktree" in lab_review.prompt
        assert "You do not reproduce it yourself" not in lab_review.prompt
        assert "earlier.work" not in lab_review.prompt
        assert (lab_review.min_timeout_seconds or 0) >= 7200
        actions = await h.store.list_actions(
            repo="acme/widget", number=55, kinds=[ActionKind.REVIEW_POST]
        )
        assert len(actions) >= 2
    finally:
        await h.close()

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

"""Conversation routing: sticky fallback sessions, explicit agent selection before
starting a conversation, and mid-conversation agent switching across chat, review
conversations, and People conversations."""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from auto_maintainer.agents.router import AgentRouter
from auto_maintainer.domain.agents import (
    AgentEvent,
    AgentEventType,
    AgentResult,
    AgentTask,
    Capabilities,
    RuntimeHealth,
)
from auto_maintainer.domain.permissions import Permission
from auto_maintainer.domain.profile import Route
from auto_maintainer.domain.records import ChatThread
from auto_maintainer.testing.demo import build_demo
from auto_maintainer.web.app import create_app
from auto_maintainer.web.chat import keep_session, turn_route

REPO = "acme/widget"
ROUTE = Route(primary="antigravity", fallback=["claude-code"], model="gemini-x")


class _Service:
    def __init__(self, own: dict[str, list[str]], ready: set[str]) -> None:
        self.own, self.ready = own, ready

    async def own_agents(self, login: str) -> list[str]:
        return self.own.get(login, [])

    def agent_ready(self, name: str) -> bool:
        return name in self.ready


async def _turn(service: Any, login: str, thread: ChatThread) -> tuple[Route, Any]:
    return await turn_route(service, login, thread, ROUTE)


class _Runtime:
    """In-memory ``AgentRuntime`` for conversation router flow tests."""

    def __init__(
        self,
        name: str,
        fn: Callable[[AgentTask], AgentResult],
        *,
        installed: bool = True,
    ) -> None:
        self.name = name
        self._fn = fn
        self._installed = installed
        self.calls: list[AgentTask] = []

    def installed(self) -> bool:
        return self._installed

    def capabilities(self) -> Capabilities:
        return Capabilities(schema_flag=True, sessions=True, streaming=True)

    async def probe(self) -> RuntimeHealth:
        return RuntimeHealth(name=self.name, available=self._installed)

    async def run(self, task: AgentTask) -> AsyncIterator[AgentEvent]:
        self.calls.append(task)
        yield AgentEvent(type=AgentEventType.STARTED)
        yield AgentEvent(type=AgentEventType.FINAL, result=self._fn(task))


@pytest.fixture
async def demo(tmp_path: Path):
    h = await build_demo(tmp_path)
    h.forge.set_permission(REPO, "walt", Permission.WRITE)
    app = create_app(h.service, data_dir=tmp_path, allow_dev_login=True)
    yield h, app
    await h.close()


@contextlib.asynccontextmanager
async def _as(app: Any, login: str) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.get(f"/auth/dev?login={login}")
        client.headers["x-am-csrf"] = client.cookies.get("am_csrf") or ""
        yield client


async def test_a_session_resumes_only_on_the_agent_that_holds_it() -> None:
    service = _Service({}, {"antigravity", "claude-code"})
    thread = ChatThread(owner="maria", session="s1", runtime="claude-code")
    route, resume = await _turn(service, "maria", thread)
    assert resume == "s1" and route.primary == "claude-code"
    assert route.fallback == ["antigravity"] and route.model is None

    assert await _turn(service, "maria", ChatThread(owner="maria")) == (ROUTE, None)
    gone = _Service({}, {"antigravity"})
    assert await _turn(gone, "maria", thread) == (ROUTE, None), "its agent is gone"


async def test_established_session_stays_sticky_even_when_own_agent_exists() -> None:
    service = _Service({"maria": ["claude-code"]}, {"antigravity", "claude-code"})
    on_fallback = ChatThread(owner="maria", session="s1", runtime="antigravity")
    route, resume = await _turn(service, "maria", on_fallback)
    assert (route.primary, route.fallback, resume) == (
        "antigravity",
        ["claude-code"],
        "s1",
    )

    on_hers = ChatThread(owner="maria", session="s2", runtime="claude-code")
    route, resume = await _turn(service, "maria", on_hers)
    assert (route.primary, resume) == ("claude-code", "s2")

    keep_session(on_fallback, AgentResult(runtime="claude-code", ok=True))
    assert (on_fallback.session, on_fallback.runtime) == (None, "claude-code"), (
        "the old session was another agent's"
    )
    keep_session(on_hers, AgentResult(runtime="claude-code", ok=True))
    assert on_hers.session == "s2", "the same agent keeps its session"


async def test_explicit_thread_agent_pins_route_and_clears_foreign_session() -> None:
    service = _Service(
        {"maria": ["claude-code"]}, {"antigravity", "claude-code", "codex"}
    )
    switched = ChatThread(
        owner="maria", session="s1", runtime="antigravity", agent="codex"
    )
    route, resume = await _turn(service, "maria", switched)
    assert (route.primary, route.fallback, route.model, resume) == (
        "codex",
        [],
        None,
        None,
    )
    assert switched.session is None

    same = ChatThread(owner="maria", session="s2", runtime="codex", agent="codex")
    route, resume = await _turn(service, "maria", same)
    assert (route.primary, route.fallback, resume) == ("codex", [], "s2")


async def test_fallback_from_own_agent_stays_sticky_after_breaker_cools_down(
    demo,
) -> None:
    h, app = demo

    def claude_fn(task: AgentTask) -> AgentResult:
        if len(claude.calls) == 1:
            return AgentResult(
                runtime="claude-code",
                ok=False,
                error="403 Forbidden: built-in haiku model rejected",
                error_kind="auth_expired",
            )
        reply = {"reply": "claude recovered", "proposals": []}
        return AgentResult(
            runtime="claude-code",
            ok=True,
            text=json.dumps(reply),
            structured=reply,
            session="s_claude",
        )

    def agy_fn(task: AgentTask) -> AgentResult:
        n = len(agy.calls)
        reply = {"reply": f"antigravity turn {n}", "proposals": []}
        return AgentResult(
            runtime="antigravity",
            ok=True,
            text=json.dumps(reply),
            structured=reply,
            session=f"s_agy_{n}",
        )

    claude = _Runtime("claude-code", claude_fn)
    agy = _Runtime("antigravity", agy_fn)
    router = AgentRouter({"claude-code": claude, "antigravity": agy})
    router.own_agents = lambda login: _async_list(  # type: ignore[method-assign]
        ["claude-code"] if login == "maria" else []
    )
    h.service.a.agents = router
    h.service.settings.routing["chat"] = Route(
        primary="claude-code", fallback=["antigravity"]
    )

    async with _as(app, "maria") as maria:
        thread = (await maria.post("/api/chat/threads", json={"text": "hello"})).json()
        first = (
            await maria.post(
                f"/api/chat/threads/{thread['id']}/messages",
                json={"text": "hello"},
            )
        ).json()
        assert first["text"] == "antigravity turn 1"
        assert first["runtime"] == "antigravity"
        assert len(claude.calls) == 1 and len(agy.calls) == 1
        assert agy.calls[0].resume_session is None

        # Even after claude-code's circuit breaker cools down, the established
        # antigravity session remains sticky instead of bouncing back to claude-code.
        router.breakers.clear()
        second = (
            await maria.post(
                f"/api/chat/threads/{thread['id']}/messages",
                json={"text": "follow up"},
            )
        ).json()
        assert second["text"] == "antigravity turn 2"
        assert second["runtime"] == "antigravity"
        assert len(claude.calls) == 1, "did not bounce back to failing own_agents CLI"
        assert len(agy.calls) == 2
        assert agy.calls[1].resume_session == "s_agy_1"


async def test_selecting_and_switching_agent_in_chat_conversations(demo) -> None:
    h, app = demo

    def reply_for(name: str):
        def _fn(task: AgentTask) -> AgentResult:
            n = len(runtimes[name].calls)
            body = {"reply": f"{name} answer {n}", "proposals": []}
            return AgentResult(
                runtime=name,
                ok=True,
                text=json.dumps(body),
                structured=body,
                session=f"s_{name}_{n}",
            )

        return _fn

    runtimes = {
        "claude-code": _Runtime("claude-code", reply_for("claude-code")),
        "antigravity": _Runtime("antigravity", reply_for("antigravity")),
        "opencode": _Runtime("opencode", reply_for("opencode")),
        "codex": _Runtime("codex", reply_for("codex")),
        "gemini": _Runtime("gemini", reply_for("gemini"), installed=False),
    }
    router = AgentRouter(runtimes, disabled={"codex"})
    router.own_agents = lambda login: _async_list(  # type: ignore[method-assign]
        ["claude-code"] if login == "maria" else []
    )
    h.service.a.agents = router
    h.service.settings.routing["chat"] = Route(
        primary="claude-code", fallback=["antigravity"]
    )

    async with _as(app, "maria") as maria:
        # Unknown, disabled, and uninstalled agents are rejected with 400/409.
        bad_unknown = await maria.post(
            "/api/chat/threads", json={"text": "hi", "agent": "nonesuch"}
        )
        assert bad_unknown.status_code == 400
        bad_disabled = await maria.post(
            "/api/chat/threads", json={"text": "hi", "agent": "codex"}
        )
        assert bad_disabled.status_code == 409
        bad_uninstalled = await maria.post(
            "/api/chat/threads", json={"text": "hi", "agent": "gemini"}
        )
        assert bad_uninstalled.status_code == 409

        # Enable codex so we can switch to it mid-conversation.
        router.disabled.clear()

        # (b) Starting a conversation with explicit `agent` runs that exact runtime
        # with agent_chosen=True instead of own_agents / default primary.
        thread = (
            await maria.post(
                "/api/chat/threads",
                json={"text": "First question", "agent": "opencode"},
            )
        ).json()
        assert thread["agent"] == "opencode"

        first = (
            await maria.post(
                f"/api/chat/threads/{thread['id']}/messages",
                json={"text": "First question", "agent": "opencode"},
            )
        ).json()
        assert first["text"] == "opencode answer 1"
        assert first["runtime"] == "opencode"
        assert len(runtimes["claude-code"].calls) == 0
        assert len(runtimes["opencode"].calls) == 1
        assert runtimes["opencode"].calls[0].agent_chosen is True
        assert runtimes["opencode"].calls[0].resume_session is None

        # Omitting `agent` on the next turn keeps thread.agent="opencode" and resumes
        # its session.
        second = (
            await maria.post(
                f"/api/chat/threads/{thread['id']}/messages",
                json={"text": "Continue on opencode"},
            )
        ).json()
        assert second["text"] == "opencode answer 2"
        assert second["runtime"] == "opencode"
        assert len(runtimes["opencode"].calls) == 2
        assert runtimes["opencode"].calls[1].agent_chosen is True
        assert runtimes["opencode"].calls[1].resume_session == "s_opencode_1"

        # (c) Switching `agent` mid-conversation clears the prior runtime's session,
        # runs the newly chosen runtime with prior messages in the prompt, and records
        # `runtime` on the assistant message.
        switched = (
            await maria.post(
                f"/api/chat/threads/{thread['id']}/messages",
                json={"text": "Now answer with codex", "agent": "codex"},
            )
        ).json()
        assert switched["text"] == "codex answer 1"
        assert switched["runtime"] == "codex"
        assert len(runtimes["codex"].calls) == 1
        codex_task = runtimes["codex"].calls[0]
        assert codex_task.agent_chosen is True
        assert codex_task.resume_session is None
        assert "user: First question" in codex_task.prompt
        assert "assistant: opencode answer 1" in codex_task.prompt
        assert "user: Continue on opencode" in codex_task.prompt
        assert "assistant: opencode answer 2" in codex_task.prompt

        listed = (await maria.get("/api/chat/threads")).json()
        [stored_thread] = [t for t in listed if t["id"] == thread["id"]]
        assert stored_thread["agent"] == "codex"
        assert stored_thread["runtime"] == "codex"

        # Clearing `agent=""` returns the thread to automatic routing, dropping codex's
        # session so the default/own agent takes over cleanly.
        cleared = (
            await maria.post(
                f"/api/chat/threads/{thread['id']}/messages",
                json={"text": "Return to default", "agent": ""},
            )
        ).json()
        assert cleared["runtime"] == "claude-code"
        assert len(runtimes["claude-code"].calls) == 1
        assert runtimes["claude-code"].calls[0].agent_chosen is False
        assert runtimes["claude-code"].calls[0].resume_session is None


async def test_switching_agent_in_review_and_people_conversations(demo) -> None:
    h, app = demo

    async with _as(app, "maria") as maria:
        # Produce a review draft first using the demo's scripted runner.
        await maria.post("/api/reviews", json={"prs": [{"repo": REPO, "number": 42}]})
        await h.settle()
        work = (await maria.get(f"/api/work?repo={REPO}&number=42")).json()
        [job] = [j for j in work if j["playbook"] == "review"]

        # Review talk: start on opencode, then switch mid-conversation to codex.
        bad_review_agent = await maria.post(
            f"/api/jobs/{job['id']}/talk",
            json={"text": "Why F1?", "agent": "nonesuch"},
        )
        assert bad_review_agent.status_code == 400

        r1 = (
            await maria.post(
                f"/api/jobs/{job['id']}/talk",
                json={"text": "Why is F1 a finding?", "agent": "opencode"},
            )
        ).json()
        assert r1["runtime"] == "opencode"
        assert h.runner.tasks[-1].agent_chosen is True
        assert h.runner.routes[-1].primary == "opencode"
        assert h.runner.routes[-1].fallback == []

        review_thread = await h.store.get_thread(f"chat_{job['id']}")
        assert review_thread is not None
        review_thread.session = "s_open_rev"
        await h.store.save_thread(review_thread)

        r2 = (
            await maria.post(
                f"/api/jobs/{job['id']}/talk",
                json={"text": "Check again with codex.", "agent": "codex"},
            )
        ).json()
        assert r2["runtime"] == "codex"
        assert h.runner.tasks[-1].agent_chosen is True
        assert h.runner.tasks[-1].resume_session is None
        assert "maria: Why is F1 a finding?" in h.runner.tasks[-1].prompt
        assert h.runner.routes[-1].primary == "codex"
        talk_state = (await maria.get(f"/api/jobs/{job['id']}/talk")).json()
        assert talk_state["agent"] == "codex" and talk_state["runtime"] == "codex"

        # People talk: start on opencode, then switch mid-conversation to codex.
        people_url = f"/api/repos/{REPO}/governance/talk"
        bad_people_agent = await maria.post(
            people_url, json={"text": "Hi", "agent": "nonesuch"}
        )
        assert bad_people_agent.status_code == 400

        p1 = (
            await maria.post(
                people_url,
                json={"text": "Our process is in GOVERNANCE.md.", "agent": "opencode"},
            )
        ).json()
        assert p1["runtime"] == "opencode"
        assert h.runner.tasks[-1].agent_chosen is True
        assert h.runner.routes[-1].primary == "opencode"
        assert h.runner.routes[-1].fallback == []

        people_state = (await maria.get(people_url)).json()
        assert people_state["agent"] == "opencode"
        assert people_state["runtime"] == "opencode"
        people_thread = await h.store.get_thread(people_state["thread"])
        assert people_thread is not None
        people_thread.session = "s_open_gov"
        await h.store.save_thread(people_thread)

        p2 = (
            await maria.post(
                people_url,
                json={"text": "Who maintains it?", "agent": "codex"},
            )
        ).json()
        assert p2["runtime"] == "codex"
        assert h.runner.tasks[-1].agent_chosen is True
        assert h.runner.tasks[-1].resume_session is None
        assert "maria: Our process is in GOVERNANCE.md." in h.runner.tasks[-1].prompt
        assert h.runner.routes[-1].primary == "codex"


async def _async_list(items: list[str]) -> list[str]:
    return items

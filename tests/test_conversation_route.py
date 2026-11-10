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

"""Which agent a conversation's next turn runs on: a session resumes only on the agent
that holds it, and a person who runs their own agents moves the conversation there."""

from __future__ import annotations

from typing import Any

from auto_maintainer.domain.agents import AgentResult
from auto_maintainer.domain.profile import Route
from auto_maintainer.domain.records import ChatThread
from auto_maintainer.web.chat import keep_session, turn_route

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


async def test_a_session_resumes_only_on_the_agent_that_holds_it() -> None:
    service = _Service({}, {"antigravity", "claude-code"})
    thread = ChatThread(owner="maria", session="s1", runtime="claude-code")
    route, resume = await _turn(service, "maria", thread)
    assert resume == "s1" and route.primary == "claude-code"
    assert route.fallback == ["antigravity"] and route.model is None

    assert await _turn(service, "maria", ChatThread(owner="maria")) == (ROUTE, None)
    gone = _Service({}, {"antigravity"})
    assert await _turn(gone, "maria", thread) == (ROUTE, None), "its agent is gone"


async def test_a_person_with_their_own_agent_moves_the_conversation_there() -> None:
    service = _Service({"maria": ["claude-code"]}, {"antigravity", "claude-code"})
    on_service = ChatThread(owner="maria", session="s1", runtime="antigravity")
    assert await _turn(service, "maria", on_service) == (ROUTE, None), (
        "a fresh turn, which the router starts on her own agent"
    )
    on_hers = ChatThread(owner="maria", session="s2", runtime="claude-code")
    route, resume = await _turn(service, "maria", on_hers)
    assert (route.primary, resume) == ("claude-code", "s2")

    keep_session(on_service, AgentResult(runtime="claude-code", ok=True))
    assert (on_service.session, on_service.runtime) == (None, "claude-code"), (
        "the old session was another agent's"
    )
    keep_session(on_hers, AgentResult(runtime="claude-code", ok=True))
    assert on_hers.session == "s2", "the same agent keeps its session"

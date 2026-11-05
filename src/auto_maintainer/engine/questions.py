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

"""The question desk: agents' permission prompts and questions wait here for a reviewer.

A question lives in the store from the moment an agent asks it, so any process can
answer it: the dashboard of ``am serve`` answers a question that an agent started by
``am repo add`` is waiting on. The waiting side watches the store as well as an
in-process signal, and denies the request when nobody answers in time.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
from collections.abc import Awaitable, Callable
from datetime import timedelta

from ..domain.agents import (
    AgentAnswer,
    AgentQuestion,
    QuestionKind,
    QuestionState,
    denial,
)
from ..domain.clock import Clock
from ..domain.events import Event, EventKind
from ..domain.ports import EventSink, Notifier, Store

log = logging.getLogger(__name__)

ChannelFor = Callable[[str | None], Awaitable[str | None]]
"""Returns the notification channel for questions about a repository, if any."""

WAITER = f"{socket.gethostname()}:{os.getpid()}"


class QuestionDesk:
    """Implements ``AgentQuestions``.

    Args:
        store: Where questions are kept.
        bus: Receives ``question.asked`` and ``question.answered`` for live dashboards.
        notifier: Alerts reviewers outside the dashboard.
        clock: Time source for timestamps.
        channel_for: Picks the notification channel for a repository.
        dashboard_url: Base URL put into alerts.
        poll_seconds: How often a waiting agent checks the store for an answer.
    """

    def __init__(
        self,
        store: Store,
        bus: EventSink,
        notifier: Notifier,
        clock: Clock,
        *,
        channel_for: ChannelFor | None = None,
        dashboard_url: str | None = None,
        poll_seconds: float = 2.0,
    ) -> None:
        self.store = store
        self.bus = bus
        self.notifier = notifier
        self.clock = clock
        self.channel_for = channel_for
        self.dashboard_url = (dashboard_url or "").rstrip("/")
        self.poll_seconds = poll_seconds
        self._signals: dict[str, asyncio.Event] = {}

    # -- AgentQuestions ----------------------------------------------------------------

    async def ask(self, question: AgentQuestion, timeout_seconds: float) -> AgentAnswer:
        now = self.clock.now()
        question = question.model_copy(
            update={
                "state": QuestionState.PENDING,
                "created_at": now,
                "expires_at": now + timedelta(seconds=timeout_seconds),
                "answer": None,
                "waiter": WAITER,
            }
        )
        await self.store.save_question(question)
        signal = self._signals[question.id] = asyncio.Event()
        await self.bus.publish(
            Event(
                kind=EventKind.QUESTION_ASKED,
                repo=question.repo,
                number=question.pr,
                payload={
                    "id": question.id,
                    "kind": question.kind.value,
                    "title": question.title,
                },
            )
        )
        await self._alert(question)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        try:
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        signal.wait(), min(self.poll_seconds, remaining)
                    )
                current = await self.store.get_question(question.id)
                if current is None:
                    return denial("the question disappeared from the store")
                if current.state == QuestionState.ANSWERED and current.answer:
                    return current.answer
                if current.state != QuestionState.PENDING:
                    return current.answer or denial(f"the question was {current.state}")
            minutes = max(1, round(timeout_seconds / 60))
            answer = denial(
                f"No reviewer answered within {minutes} minutes, so the request was denied."
            )
            await self._close(question.id, QuestionState.EXPIRED, answer)
            return answer
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await self._close(
                    question.id,
                    QuestionState.CANCELLED,
                    denial("The agent run ended before anyone answered."),
                )
            raise
        finally:
            self._signals.pop(question.id, None)

    # -- reviewers -------------------------------------------------------------------

    async def answer(
        self, question_id: str, answer: AgentAnswer, login: str
    ) -> AgentQuestion:
        """Record a reviewer's answer.

        Raises:
            LookupError: No question has this id.
            ValueError: The question is no longer pending, or the answer does not fit it.
        """
        question = await self.store.get_question(question_id)
        if question is None:
            raise LookupError(f"unknown question {question_id}")
        if question.state != QuestionState.PENDING:
            raise ValueError(f"the question is already {question.state.value}")
        now = self.clock.now()
        if question.expires_at and question.expires_at <= now:
            await self._close(
                question.id,
                QuestionState.EXPIRED,
                denial("No reviewer answered in time, so the request was denied."),
            )
            raise ValueError("the question expired before this answer arrived")
        answer = _checked(question, answer).model_copy(
            update={"by": login, "automatic": False, "at": now}
        )
        updated = question.model_copy(
            update={"state": QuestionState.ANSWERED, "answer": answer}
        )
        await self.store.save_question(updated)
        signal = self._signals.get(question.id)
        if signal is not None:
            signal.set()
        await self._published(updated)
        return updated

    async def cancel_orphans(self) -> int:
        """Cancel pending questions whose waiting process on this host is gone."""
        host = socket.gethostname()
        cancelled = 0
        for question in await self.store.list_questions(QuestionState.PENDING):
            name, _, pid = question.waiter.partition(":")
            if question.waiter == WAITER or name != host or not pid.isdigit():
                continue
            if _alive(int(pid)):
                continue
            await self._close(
                question.id,
                QuestionState.CANCELLED,
                denial("The agent run ended before anyone answered."),
            )
            cancelled += 1
        return cancelled

    # -- internals ---------------------------------------------------------------------

    async def _close(
        self, question_id: str, state: QuestionState, answer: AgentAnswer
    ) -> None:
        current = await self.store.get_question(question_id)
        if current is None or current.state != QuestionState.PENDING:
            return
        closed = current.model_copy(
            update={
                "state": state,
                "answer": answer.model_copy(update={"at": self.clock.now()}),
            }
        )
        await self.store.save_question(closed)
        await self._published(closed)

    async def _published(self, question: AgentQuestion) -> None:
        await self.bus.publish(
            Event(
                kind=EventKind.QUESTION_ANSWERED,
                repo=question.repo,
                number=question.pr,
                payload={
                    "id": question.id,
                    "state": question.state.value,
                    "allow": bool(question.answer and question.answer.allow),
                    "by": question.answer.by if question.answer else "",
                },
            )
        )

    async def _alert(self, question: AgentQuestion) -> None:
        channel = None
        if self.channel_for is not None:
            try:
                channel = await self.channel_for(question.repo)
            except Exception:
                log.exception("could not pick a channel for %s", question.repo)
        where = question.repo or "the service"
        if question.repo and question.pr:
            where = f"{question.repo}#{question.pr}"
        link = (
            f" Answer at {self.dashboard_url}/questions." if self.dashboard_url else ""
        )
        verb = "asks" if question.kind == QuestionKind.INPUT else "asks to"
        text = (
            f"{question.runtime or 'An agent'} ({question.task_kind or 'task'}) on "
            f"{where} {verb}: {question.title}.{link}"
        )
        try:
            await self.notifier.send(channel or "log", text, "agent_question")
        except Exception:
            log.exception("could not alert reviewers about question %s", question.id)


def _checked(question: AgentQuestion, answer: AgentAnswer) -> AgentAnswer:
    if question.kind == QuestionKind.PERMISSION and question.options:
        if answer.option_id is None:
            kind = "allow" if answer.allow else "deny"
            option = next((o for o in question.options if o.kind == kind), None)
            if option is None:
                raise ValueError(f"the question has no {kind} option")
            return answer.model_copy(update={"option_id": option.id})
        option = next((o for o in question.options if o.id == answer.option_id), None)
        if option is None:
            raise ValueError(f"unknown option {answer.option_id!r}")
        return answer.model_copy(
            update={"allow": option.kind in ("allow", "allow_session")}
        )
    if question.kind == QuestionKind.INPUT and answer.allow:
        missing = [q.id for q in question.inputs if not answer.answers.get(q.id)]
        if missing:
            raise ValueError(f"answers are missing for {', '.join(missing)}")
    return answer


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True

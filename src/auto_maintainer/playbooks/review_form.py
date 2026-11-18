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

"""A review in two turns (design 5.5). The agent follows the review skill the way a
person asks for a review, and ends with the skill's own report; then, continuing its
session, it copies the report into the fields the service renders and posts
(``TaskKind.FORM``). The report is kept for the job, so a form turn that fails runs
again without the review."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from ..domain.agents import AgentResult, AgentTask, TaskKind
from .base import PlaybookContext

REPORT_STATE = "review_report:{job}"
"""The finished report of a job's review or follow-up agent, with the head it read."""

M = TypeVar("M", bound=BaseModel)


async def saved_report(ctx: PlaybookContext, head_sha: str) -> AgentResult | None:
    """The report this job's agent already wrote about ``head_sha``, if any."""
    raw = await ctx.store.get_state(REPORT_STATE.format(job=ctx.job.id))
    if not isinstance(raw, dict) or raw.get("head_sha") != head_sha:
        return None
    try:
        return AgentResult.model_validate(raw.get("answer"))
    except ValidationError:
        return None


async def keep_report(ctx: PlaybookContext, head_sha: str, answer: AgentResult) -> None:
    await ctx.store.set_state(
        REPORT_STATE.format(job=ctx.job.id),
        {"head_sha": head_sha, "answer": answer.model_dump(mode="json")},
    )


async def forget_report(ctx: PlaybookContext) -> None:
    await ctx.store.set_state(REPORT_STATE.format(job=ctx.job.id), None)


def answered(answer: AgentResult, model: type[M]) -> M | None:
    """``answer`` as ``model`` when the agent gave the fields already (a skill that
    returns them, or a reply that is that JSON alone), so no form turn is needed."""
    data = answer.structured
    if data is None:
        text = answer.text.strip()
        if text.startswith("```"):
            text = text.strip("`").removeprefix("json").strip()
        if not text.startswith("{"):
            return None
        try:
            data = json.loads(text)
        except ValueError:
            return None
    try:
        return model.model_validate(data)
    except ValidationError:
        return None


async def fill_form(
    ctx: PlaybookContext,
    answer: AgentResult,
    model: type[M],
    prompt: str,
    workdir: Path,
) -> M:
    """The report in ``answer`` as ``model``: the agent that wrote it fills the form
    in its own session (another agent of the route, from ``prompt`` alone, when it
    cannot)."""
    done = answered(answer, model)
    if done is not None:
        return done
    form = await ctx.ask(
        AgentTask(
            kind=TaskKind.FORM,
            prompt=prompt,
            workdir=workdir,
            output_schema=model.model_json_schema(),
            repo=ctx.repo,
        ),
        model,
        after=answer,
    )
    if not form.ok or form.structured is None:
        raise RuntimeError(f"the agent did not fill in its review: {form.error}")
    return model.model_validate(form.structured)

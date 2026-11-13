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

"""Learning from the service's own trouble (design 5.11): after an agent's work ran
into trouble (failed commands, errors, a failed run), a quick agent looks back at it.

What a future agent doing the same kind of work could do differently becomes a
lesson, for the repository (how to build, test, or run its code), for a test machine
(its tools, paths, mirrors), or for the service's own tools (the lab command,
sessions, the rules on what may run). Every later agent of that kind reads the
lessons that people approved (``Knowledge.agent_lessons``,
``prompts.work_lessons_block``). The lessons come from work on untrusted PR content,
so none takes effect on its own: a repository's lessons wait for one of its
maintainers (``from_own_signals`` false), a machine's and the service's for its owner
(``service``). When the service itself made the work fail or go worse and no lesson
can avoid it, the agent suggests a change to the service's owner instead
(``domain/retro.py:Suggestion``), who may start a self-improvement with it.

A job that went smoothly costs nothing: ``domain/retro.py:trouble`` reads its saved
steps first, and only trouble starts an agent.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from ..domain.actions import ActionKind, MemoryChangePayload, NotifyPayload, make_action
from ..domain.agents import AgentTask, TaskKind
from ..domain.events import Event, EventKind
from ..domain.memory import MemoryChange, Tier, repo_dir
from ..domain.profile import RepoProfile
from ..domain.progress import JobStep, steps_key
from ..domain.retro import (
    KEEP_SUGGESTIONS,
    PER_DAY,
    SUGGESTIONS_KEY,
    Suggestion,
    trouble,
)
from ..domain.secrets import redact_secrets
from ..profiles.knowledge import (
    AGENT_LESSONS,
    GLOBAL_AGENT_LESSONS,
    machine_lessons_path,
)
from .base import Playbook, PlaybookContext, PlaybookResult
from .prompts import schema_hint, untrusted

WATCHED = {
    "review": "a review of a pull request",
    "followup": "a follow-up review of a pull request",
    "reproduce": "a reproduction of a pull request on a test machine",
    "fix": "a fix of a pull request",
    "fix_ci": "a fix of a pull request's failing CI",
    "merge_base": "a merge of the base branch into a pull request",
    "ci_doctor": "a diagnosis of a pull request's failed CI",
}
"""The playbooks whose agents' trouble is looked back at, with what they do."""

LESSONS_HEADER = "# Agent lessons\n"
MAX_LESSONS = 3

RETROSPECT = """An agent of an automated maintainer service just did {what} in {repo}{pr} and ran into trouble. Its tool calls in order (ok or FAILED, with what failed), its errors, and its own account at the end are below. Look back at it the way an engineer looks back at a bad day: what could a future agent doing the same kind of work do differently, and is the service itself at fault?
- A lesson is one or two sentences a future agent can act on, standing on their own: the situation and what to do (a dependency to install first, a command or flag that works, a file to read, a step that cannot work here and what to do instead). Take it only from what the record shows. Never a lesson about this one PR, nor one the current lessons below already say. Its scope is `repo` (building, testing, or running this repository's code), `machine` (a test machine's environment: its tools, paths, mirrors, GPUs; name the machine), or `service` (the service's own tools: the lab command, sessions, the records command, the rules on what may run).
- A suggestion is for the service's maintainers, only when the service itself made the work fail or go worse and no lesson can avoid it: a rule that refuses a legitimate command, a tool missing from the agents' image, an instruction that misleads, a time limit too short, a bug in the service. Say the problem and a concrete change. When one of the open suggestions below is the same problem, give its id in `same_as`.
- When nothing went wrong that a lesson or a suggestion would fix (the network failed once, the PR's own code is broken, someone stopped the job), give no lessons and no suggestion.
Write in {language}. Judge only from what is given here: do not run commands, read files, or fetch anything."""


class Lesson(BaseModel):
    scope: Literal["repo", "machine", "service"]
    machine: str = Field("", description="For scope machine: the machine's name.")
    lesson: str = Field(max_length=600)


class SuggestionDraft(BaseModel):
    same_as: str = Field("", description="An open suggestion's id, when the same.")
    title: str = Field(max_length=120)
    problem: str = Field(max_length=1500)
    proposal: str = Field(max_length=1500)


class RetroResult(BaseModel):
    cause: str = Field("", description="One sentence: what went wrong.")
    lessons: list[Lesson] = Field(default_factory=list)
    suggestion: SuggestionDraft | None = None


def _clean(text: str) -> str:
    out, _ = redact_secrets(" ".join(text.split()))
    return out


def _appended(current: str, lessons: list[str]) -> str | None:
    """``current`` (a lessons file) with ``lessons`` added as list items, or ``None``
    when it says each of them already."""
    body = current if current.strip() else LESSONS_HEADER
    known = {
        line.strip()[2:].strip() for line in body.splitlines() if line.startswith("- ")
    }
    new = [x for x in lessons if x and x not in known]
    if not new:
        return None
    return body.rstrip() + "\n" + "".join(f"- {x}\n" for x in new)


class Retrospect(Playbook):
    name = "retrospect"
    triggers = (EventKind.JOB_FINISHED,)
    pool = "quality"

    def accepts(self, event: Event, profile: RepoProfile | None) -> bool:
        return (
            event.payload.get("playbook") in WATCHED
            and bool(event.payload.get("job"))
            and profile is not None
        )

    def key(self, event: Event) -> str:
        return f"retrospect:{event.payload.get('job')}"

    async def run(self, ctx: PlaybookContext, event: Event) -> PlaybookResult:
        profile = ctx.require_profile()
        repo = profile.repo
        job = await ctx.store.get_job(str(event.payload["job"]))
        if job is None:
            return PlaybookResult(result={"skipped": "no such job"})
        raw = await ctx.store.get_state(steps_key(job.id)) or []
        steps = []
        for item in raw:
            try:
                steps.append(JobStep.model_validate(item))
            except ValueError:
                continue
        found = trouble(steps, job.error if job.status == "failed" else None)
        if not found.worth_a_look:
            return PlaybookResult(result={"skipped": "nothing went wrong"})
        count_key = f"retrospect-count:{repo}:{ctx.clock.now():%Y-%m-%d}"
        today = int(await ctx.store.get_state(count_key) or 0)
        if today >= PER_DAY:
            return PlaybookResult(result={"skipped": f"{PER_DAY} looks today already"})
        await ctx.store.set_state(count_key, today + 1)
        machines = found.machines
        current = await ctx.knowledge.agent_lessons(repo, machines)
        suggestions = [
            Suggestion.model_validate(x)
            for x in await ctx.store.get_state(SUGGESTIONS_KEY) or []
        ]
        open_ones = [s for s in suggestions if s.state == "open"]
        pr = f"#{job.number}" if job.number else ""
        prompt = "\n\n".join(
            part
            for part in [
                RETROSPECT.format(
                    what=WATCHED[job.playbook],
                    repo=repo,
                    pr=f" {pr}" if pr else "",
                    language=profile.languages.internal,
                ),
                f"Test machines it used: {', '.join(machines)}" if machines else "",
                "What happened:\n" + untrusted("job.record", found.digest, 16_000),
                "Current lessons:\n" + untrusted("lessons", current, 6_000)
                if current
                else "",
                "Open suggestions:\n"
                + "\n".join(f"- {s.id}: {s.title}" for s in open_ones[-20:])
                if open_ones
                else "",
                schema_hint(RetroResult.model_json_schema()),
            ]
            if part
        )
        answer = await ctx.ask(
            AgentTask(
                kind=TaskKind.LEARN,
                prompt=prompt,
                workdir=ctx.scratch(),
                mode="read_only",
                budget="quick",
                output_schema=RetroResult.model_json_schema(),
                repo=repo,
                pr=job.number,
                untrusted_input=True,
                activity="retrospect",
            ),
            RetroResult,
        )
        if not answer.ok or answer.structured is None:
            return PlaybookResult(failed=f"the retrospective failed: {answer.error}")
        found_out = RetroResult.model_validate(answer.structured)
        actions = []
        by_scope: dict[str, list[str]] = {}
        for lesson in found_out.lessons[:MAX_LESSONS]:
            text = _clean(lesson.lesson)
            if not text:
                continue
            if lesson.scope == "machine":
                if lesson.machine not in machines:
                    continue
                by_scope.setdefault(machine_lessons_path(lesson.machine), []).append(
                    text
                )
            elif lesson.scope == "service":
                by_scope.setdefault(GLOBAL_AGENT_LESSONS, []).append(text)
            else:
                by_scope.setdefault(f"{repo_dir(repo)}/{AGENT_LESSONS}", []).append(
                    text
                )
        cause = _clean(found_out.cause)
        where = f"{job.playbook} on {repo}{pr}"
        own = {p: x for p, x in by_scope.items() if not p.startswith("global/")}
        shared = {p: x for p, x in by_scope.items() if p.startswith("global/")}
        for files, service in ((own, False), (shared, True)):
            new_files: dict[str, str | None] = {}
            told: list[str] = []
            for path, lessons in files.items():
                updated = _appended(await ctx.memory.read_text(path) or "", lessons)
                if updated is not None:
                    new_files[path] = updated
                    told += lessons
            if not new_files:
                continue
            change = MemoryChange(
                tier=Tier.T1,
                title=f"lessons from {where}",
                description=(f"{cause}\n\n" if cause else "")
                + "\n".join(f"- {x}" for x in told)
                + f"\n\nFrom the trouble in job {job.id}.",
                files=new_files,
                repos=[] if service else [repo],
                service=service,
                # Written by an agent from work on untrusted content: a person
                # approves every lesson before any agent reads it.
                from_own_signals=False,
                created_at=ctx.clock.now(),
            )
            await ctx.store.save_memory_change(change)
            actions.append(
                make_action(
                    ActionKind.MEMORY_CHANGE,
                    repo,
                    MemoryChangePayload(change_id=change.id),
                    discriminator=change.id,
                )
            )
        suggested = None
        draft = found_out.suggestion
        if draft is not None and draft.title.strip() and draft.proposal.strip():
            now = ctx.clock.now()
            same = next((s for s in open_ones if s.id == draft.same_as), None)
            if same is not None:
                same.count += 1
                same.last_at = now
                same.jobs = [*same.jobs, job.id][-10:]
                suggested = same
            else:
                suggested = Suggestion(
                    title=_clean(draft.title)[:120],
                    problem=_clean(draft.problem),
                    proposal=_clean(draft.proposal),
                    repo=repo,
                    playbook=job.playbook,
                    jobs=[job.id],
                    first_at=now,
                    last_at=now,
                )
                suggestions.append(suggested)
            await ctx.store.set_state(
                SUGGESTIONS_KEY,
                [s.model_dump(mode="json") for s in suggestions][-KEEP_SUGGESTIONS:],
            )
            if suggested.count == 1:
                actions.append(
                    make_action(
                        ActionKind.NOTIFY,
                        repo,
                        NotifyPayload(
                            event="service.suggestion",
                            text=f"The agents suggest a change to the service: "
                            f"{suggested.title}",
                        ),
                        discriminator=f"suggestion:{suggested.id}",
                    )
                )
        return PlaybookResult(
            actions=actions,
            result={
                "cause": cause,
                "lessons": sum(len(x) for x in by_scope.values()),
                "suggestion": suggested.id if suggested else None,
            },
        )

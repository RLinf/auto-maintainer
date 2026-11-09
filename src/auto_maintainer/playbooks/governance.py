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

"""Keeping a repository's governance record (design 2.6).

Two ways in share what is here. On the People tab, reviewers talk with the agent
that keeps the record (``web/people_talk.py``): one turn at a time, each answered in
the conversation. In chat, or through the API, someone asks for a job
(``dashboard.command`` with ``command: governance``): in ``edit`` mode the agent
reads the repository's own process (its governance, maintainers, owners, and
contributing files, ``CODEOWNERS``, the docs, and the pages they or the request
name) and rewrites the record; in ``members`` mode it checks people's GitHub
activity against the record's criteria and proposes who should change role, with
the evidence, and changes nothing else.

Every way builds on the newest draft that waits for approval, when there is one,
keeps the history growing, records every change of role it finds, and drafts a T2
memory change that replaces that draft (``MemoryChangePayload.replaces``) and that a
maintainer of the repository approves; nothing applies before that.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from ..domain.actions import ActionKind, MemoryChangePayload, make_action
from ..domain.agents import AgentTask, TaskKind
from ..domain.events import Event, EventKind
from ..domain.governance import Governance, role_changes, with_history
from ..domain.memory import MemoryChange, MemoryChangeState
from ..profiles.governance import GovernanceRecords, Loaded, render
from .base import Playbook, PlaybookContext, PlaybookResult, may_succeed_later
from .prompts import no_code_rule, schema_hint

Mode = Literal["edit", "members"]

KEEPING = """You keep the record of the people of {repo} for its maintainers: the roles it has, what each role is responsible for and may do, how people move between roles and who decides, who holds each role now, and how that changed. The working directory is a checkout of the repository's default branch, and `gh` reads GitHub as the service."""

RULES = """- The repository's process is written down in its governance, maintainers, owners, or committers files, contributing guides, `CODEOWNERS`, the docs, and the pages they link to; read the ones that matter, and every source {by} names, files and web pages alike.
- A record you return is whole, in the structure of the record below: keep what is still true, change what the sources or {by} say differently, and drop the skeleton's guesses that this repository's process does not have. Use the repository's own role names and words, and write responsibilities and steps plainly.
- List as members only people the sources or {by} name, by GitHub login; never guess a login. For a role held for areas, list the areas.
- Set `maintains` on the roles whose holders run the repository in this service: they assign PRs, change its settings, this record, and its test machines, and approve such changes (usually the maintainers or owners). When no role maintains, the repository's GitHub admins do.
- When the process keeps a score or points per person, put each member's number in `score` (and its name in `score_label`), what they need for their next role in `next_step`, and anything else in a short `note`; how scores are counted belongs in the transitions' criteria, not in each note.
- `history` only grows: keep every entry, and add one for each change of someone's role you make, with `by` set to whoever decided it ({by} when they told you) and the reason.
- Give each criterion that GitHub activity can show a `measure` and a `threshold`, and a `window_months` when it counts over a period.
- List every place you took the process from in `sources`. Set `established` to true once the record describes this repository's real process rather than the skeleton.
- Put what only the maintainers can answer in `open_questions`; do not invent rules the sources do not state. When {by} answers one, apply the answer where it belongs and drop the question; keep the ones still open.
- To check who should change role, compare GitHub activity with the transitions' criteria, for example `gh search prs --repo {repo} --merged --author LOGIN --merged-at '>=YYYY-MM-DD' --json number,title --limit 100`, `gh search prs --repo {repo} --reviewed-by LOGIN --json number --limit 100`, and `gh api 'repos/{repo}/pulls/NUMBER/reviews'`. Propose a change only when the evidence meets the criteria, with a `history` entry whose `evidence` holds the numbers and links; the people in the transition's `decided_by` still decide.
- What you return is a draft: a maintainer of the repository applies it. Never post, push, or change the repository's files.
{no_code}"""

EDIT = (
    KEEPING
    + " {by}, a reviewer of the repository, asked you what is below.\n"
    + RULES
    + "\n- Return the whole record in `governance`. Write the record, `summary`, and `questions` in {language}. In `summary`, say what you changed and where it came from."
)

MEMBERS = (
    KEEPING
    + " {by}, a reviewer of the repository, asked you to check who should change role under the process the record below describes.\n"
    + RULES
    + "\n- Change only `members` and `history`; keep every other part of the record exactly as it is. Return the whole record in `governance`. In `summary`, list each proposed change with its evidence, then the people who nearly qualify and what they lack. Write `summary` and `questions` in {language}."
)

TALK = (
    KEEPING
    + " Reviewers of the repository talk with you about it; {by} wrote the newest message of the conversation below.\n"
    + "- Answer in `reply`, plainly and briefly, in the language {by} writes in. Say what you did, what you found and where, and ask there what only they can settle.\n"
    + RULES
    + "\n- When the conversation changes the record (a source to follow, an answer, a rule, a member, a change of role), return the whole revised record in `governance`; leave it null when nothing changes. Write the record in {language}."
)


def request_block(by: str, instruction: str) -> str:
    """What the person asked. It comes from a reviewer of the repository, so it is
    the task, not data to analyze."""
    return f"What {by} asked:\n{instruction[:8_000] or '(nothing more)'}"


def record_block(gov: Governance, *, draft: bool, loaded: Loaded) -> str:
    """The record the agent builds on, and what it is."""
    if draft:
        what = "The draft that waits for a maintainer's approval, which you revise"
    elif loaded.stored and not loaded.error:
        what = "The record as it is applied now"
    else:
        what = "The record (the skeleton: nobody has filled it in yet)"
    parts = [f"{what}:\n```yaml\n{render(gov)}```"]
    if loaded.error and not draft:
        parts.append(
            f"The stored record could not be read ({loaded.error}); rebuild it from "
            "the skeleton and the sources."
        )
    return "\n\n".join(parts)


class GovernanceAnswer(BaseModel):
    governance: Governance
    summary: str = Field(description="What changed and why, for the maintainers.")
    questions: list[str] = Field(
        default_factory=list, description="What only the maintainers can answer."
    )


class PeopleTurn(BaseModel):
    """One answer in the conversation about the People record."""

    reply: str = Field(description="The answer to the newest message.")
    governance: Governance | None = Field(
        default=None,
        description="The whole revised record when the conversation changes it; "
        "null when it stays as it is.",
    )


async def base_record(
    records: GovernanceRecords, repo: str, open_changes: list[MemoryChange]
) -> tuple[Loaded, Governance, MemoryChange | None]:
    """What a revision builds on: the newest unapproved draft of the record when
    one waits, else the record itself; with the stored record and that draft."""
    loaded = await records.load(repo)
    for change in sorted(open_changes, key=lambda c: c.created_at, reverse=True):
        if change.state != MemoryChangeState.OPEN or change.approvals:
            continue
        drafted = records.drafted(repo, change)
        if drafted is not None:
            return loaded, drafted, change
    return loaded, loaded.governance, None


def revised(
    current: Governance,
    after: Governance,
    *,
    repo: str,
    questions: list[str],
    by: str,
    on: date,
    reason: str,
) -> Governance:
    """``after`` as it is drafted: for ``repo``, with ``questions`` among its open
    questions, ``current``'s history kept, and every change of role recorded."""
    open_questions = list(after.open_questions)
    for q in questions:
        if q.strip() and q.strip() not in open_questions:
            open_questions.append(q.strip())
    after = after.model_copy(update={"repo": repo, "open_questions": open_questions})
    return with_history(current, after, by=by, on=on, reason=reason)


class GovernancePlaybook(Playbook):
    name = "governance"
    triggers = (EventKind.DASHBOARD_COMMAND,)
    pool = "agent"

    def accepts(self, event: Event, profile: object) -> bool:
        return event.payload.get("command") == "governance"

    def key(self, event: Event) -> str:
        return f"governance:{event.repo}:{event.id}"

    async def run(self, ctx: PlaybookContext, event: Event) -> PlaybookResult:
        profile = ctx.require_profile()
        assert event.repo and ctx.governance is not None
        repo = event.repo
        by = str(event.payload["by"])
        mode: Mode = "members" if event.payload.get("mode") == "members" else "edit"
        instruction = str(event.payload.get("instruction") or "").strip()
        ctx.requested_by = by
        ctx.agent = event.payload.get("agent") or None
        loaded, current, replaced = await base_record(
            ctx.governance,
            repo,
            await ctx.store.list_memory_changes(MemoryChangeState.OPEN),
        )
        info = await ctx.forge.get_repo(repo)
        workdir = await ctx.workspace.prepare(
            repo,
            None,
            info.default_branch_sha or info.default_branch,
            info.default_branch,
        )
        try:
            template = MEMBERS if mode == "members" else EDIT
            prompt = "\n\n".join(
                [
                    template.format(
                        repo=repo,
                        by=by,
                        no_code=no_code_rule(repo),
                        language=profile.languages.internal,
                    ),
                    request_block(by, instruction),
                    record_block(current, draft=replaced is not None, loaded=loaded),
                    schema_hint(GovernanceAnswer.model_json_schema()),
                ]
            )
            answer = await ctx.ask(
                AgentTask(
                    kind=TaskKind.GOVERNANCE,
                    prompt=prompt,
                    workdir=workdir,
                    output_schema=GovernanceAnswer.model_json_schema(),
                    skill_dirs=ctx.skill_dirs(),
                    repo=repo,
                ),
                GovernanceAnswer,
            )
        finally:
            await ctx.workspace.release(workdir)
        if not answer.ok or answer.structured is None:
            if may_succeed_later(answer):
                raise RuntimeError(f"the governance agent failed: {answer.error}")
            return PlaybookResult(failed=f"the governance agent failed: {answer.error}")
        result = GovernanceAnswer.model_validate(answer.structured)
        after = result.governance
        if mode == "members":
            # Checking people never changes the roles or the process.
            try:
                after = Governance.model_validate(
                    {
                        **current.dump(),
                        "members": [m.model_dump(mode="json") for m in after.members],
                        "history": [c.model_dump(mode="json") for c in after.history],
                    }
                )
            except ValidationError as e:
                return PlaybookResult(failed=f"the proposed members do not check: {e}")
        after = revised(
            current,
            after,
            repo=repo,
            questions=result.questions,
            by=by,
            on=ctx.clock.now().date(),
            reason=instruction[:300] or "asked on the dashboard",
        )
        changes = role_changes(loaded.governance, after)
        if after.dump() == current.dump() and (
            replaced is not None or (loaded.stored and not loaded.error)
        ):
            return PlaybookResult(
                result={"unchanged": True, "summary": result.summary.strip()}
            )
        title = (
            f"people: {len(changes)} role change(s) in {repo}"
            if mode == "members"
            else f"people: update {repo}'s roles and process"
        )
        description = "\n\n".join(
            part
            for part in [
                result.summary.strip(),
                "Questions for the maintainers:\n"
                + "\n".join(f"- {q}" for q in after.open_questions)
                if after.open_questions
                else "",
                f"Asked by {by}: {instruction}" if instruction else f"Asked by {by}.",
            ]
            if part
        )
        change = ctx.governance.draft(
            repo, after, title=title, description=description, requested_by=by
        )
        await ctx.store.save_memory_change(change)
        return PlaybookResult(
            actions=[
                make_action(
                    ActionKind.MEMORY_CHANGE,
                    repo,
                    MemoryChangePayload(
                        change_id=change.id,
                        replaces=replaced.id if replaced else None,
                    ),
                    discriminator=change.id,
                )
            ],
            result={
                "change": change.id,
                "mode": mode,
                "role_changes": [
                    {"login": login, "from": old, "to": new}
                    for login, old, new in changes
                ],
                "questions": after.open_questions,
            },
        )

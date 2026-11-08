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

Someone who may see the repository asks from its People page or in chat
(``dashboard.command`` with ``command: governance``), in plain words. In ``edit``
mode an agent reads the repository's own process (its governance, maintainers,
owners, and contributing files, ``CODEOWNERS``, the docs, and the pages they or the
request name) and rewrites the record: roles, responsibilities, how people change
roles, and members. In ``members`` mode it checks people's GitHub activity against
the record's criteria and proposes who should change role, with the evidence, and
changes nothing else. Either way the playbook keeps the history growing, records
every change of role it finds, and drafts a T2 memory change that a maintainer of
the repository approves; nothing applies before that.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from ..domain.actions import ActionKind, MemoryChangePayload, make_action
from ..domain.agents import AgentTask, TaskKind
from ..domain.events import Event, EventKind
from ..domain.governance import Governance, role_changes, with_history
from ..profiles.governance import render
from .base import Playbook, PlaybookContext, PlaybookResult, may_succeed_later
from .prompts import NO_CODE_RULE, schema_hint, untrusted

Mode = Literal["edit", "members"]

EDIT = """You keep the record of the people of {repo} for its maintainers: the roles it has, what each role is responsible for and may do, how people move between roles and who decides, who holds each role now, and how that changed. {by}, a reviewer of the repository, asked you what is below. The working directory is a checkout of the repository's default branch.
- Find where the repository writes its process down: governance, maintainers, owners, or committers files, contributing guides, `CODEOWNERS`, the docs, and the pages they link to. Read every source the request names, files and web pages alike.
- Return the whole record in `governance`, in the structure of the current record below. Keep what is still true, change what the sources or the request say differently, and drop the skeleton's guesses that this repository's process does not have. Use the repository's own role names and words, and write responsibilities and steps plainly.
- List as members only people the sources or {by} name, by GitHub login; never guess a login. For a role held for areas, list the areas.
- `history` only grows: keep every entry, and add one for each change of someone's role you make, with `by` set to whoever decided it ({by} when they told you) and the reason.
- Give each criterion that GitHub activity can show a `measure` and a `threshold`, and a `window_months` when it counts over a period.
- List every place you took the process from in `sources`. Set `established` to true once the record describes this repository's real process rather than the skeleton.
- Put what only the maintainers can answer in `open_questions`; do not invent rules the sources do not state.
- Never post, push, or change the repository's files.
{no_code}
- Write the record, `summary`, and `questions` in {language}. In `summary`, say what you changed and where it came from."""

MEMBERS = """You keep the record of the people of {repo} for its maintainers. {by}, a reviewer of the repository, asked you to check who should change role under the repository's process, which the current record below describes; their request is below. The working directory is a checkout of the repository's default branch.
- For each transition with criteria, find the people who may meet them: contributors with recent merged PRs and reviews, current members, and members who have gone quiet. `gh` reads GitHub as the service, for example `gh search prs --repo {repo} --merged --author LOGIN --merged-at '>=YYYY-MM-DD' --json number,title --limit 100`, `gh search prs --repo {repo} --reviewed-by LOGIN --json number --limit 100`, and `gh api 'repos/{repo}/pulls/NUMBER/reviews'`.
- Propose a change only when the evidence meets the transition's criteria; the people in its `decided_by` still decide. Count from the dates the criteria say.
- Return the whole record in `governance` with the members as they would be after the changes you propose, and a `history` entry for each: `by` "{by}", `reason` naming the transition, and `evidence` with the numbers and links you found. Keep every other part of the record exactly as it is.
- In `summary`, list each proposed change with its evidence, then the people who nearly qualify and what they lack.
- Never post, push, or change the repository's files.
{no_code}
- Write `summary` and `questions` in {language}."""


class GovernanceAnswer(BaseModel):
    governance: Governance
    summary: str = Field(description="What changed and why, for the maintainers.")
    questions: list[str] = Field(
        default_factory=list, description="What only the maintainers can answer."
    )


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
        loaded = await ctx.governance.load(repo)
        before = loaded.governance
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
                part
                for part in [
                    template.format(
                        repo=repo,
                        by=by,
                        no_code=NO_CODE_RULE,
                        language=profile.languages.internal,
                    ),
                    untrusted(
                        f"request from {by}",
                        instruction or "(no further instructions)",
                        8_000,
                    ),
                    "The current record"
                    + (
                        ""
                        if loaded.stored and not loaded.error
                        else " (the skeleton: nobody has filled it in yet)"
                    )
                    + ":\n```yaml\n"
                    + render(before)
                    + "```",
                    f"The stored record could not be read ({loaded.error}); "
                    "rebuild it from the skeleton and the sources."
                    if loaded.error
                    else "",
                    schema_hint(GovernanceAnswer.model_json_schema()),
                ]
                if part
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
        after = result.governance.model_copy(update={"repo": repo})
        if mode == "members":
            # Checking people never changes the roles or the process.
            try:
                after = Governance.model_validate(
                    {
                        **before.dump(),
                        "members": [m.model_dump(mode="json") for m in after.members],
                        "history": [c.model_dump(mode="json") for c in after.history],
                    }
                )
            except ValidationError as e:
                return PlaybookResult(failed=f"the proposed members do not check: {e}")
        after = with_history(
            before,
            after,
            by=by,
            on=ctx.clock.now().date(),
            reason=instruction[:300] or "asked on the dashboard",
        )
        changes = role_changes(before, after)
        if after.dump() == before.dump() and loaded.stored and not loaded.error:
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
                + "\n".join(f"- {q}" for q in result.questions)
                if result.questions
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
                    MemoryChangePayload(change_id=change.id),
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
                "questions": result.questions,
            },
        )

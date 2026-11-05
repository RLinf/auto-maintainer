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

"""Onboarding (design 2.5) and profile drift: draft a profile from a repository's own files.

Both produce a T2 memory change that only the repository's maintainers can approve.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

from ..domain.actions import ActionKind, MemoryChangePayload, NotifyPayload, make_action
from ..domain.agents import AgentTask, TaskKind
from ..domain.codeowners import CODEOWNERS_LOCATIONS
from ..domain.events import Event, EventKind
from ..domain.memory import profile_path
from ..domain.profile import ROLES, RepoProfile
from ..profiles.layers import ProfileError, parse_yaml
from ..profiles.loader import IN_REPO_PATH
from .base import Playbook, PlaybookContext, PlaybookResult
from .prompts import schema_hint, untrusted

READ_FILES = [
    "CONTRIBUTING.md",
    ".github/CONTRIBUTING.md",
    "AGENTS.md",
    "CLAUDE.md",
    ".github/PULL_REQUEST_TEMPLATE.md",
    IN_REPO_PATH,
    *CODEOWNERS_LOCATIONS,
]


class OnboardAnswer(BaseModel):
    profile: dict[str, Any] = Field(
        description="The memory profile layer as a mapping (see the schema outline)."
    )
    questions: list[str] = Field(
        default_factory=list, description="What the maintainers must decide."
    )
    rationale: str = ""


SCHEMA_OUTLINE = """Profile keys you may set: extends (packs: {packs}), scope {{min_pr, base_branches, include_drafts, ignore_authors}},
people {{codeowners, team, review_identities}}, contribution {{commit_convention: none|conventional, title_rule {{pattern,
max_description, lowercase_start, ascii_only, no_trailing_period}}, signoff, template_sections, license_header {{pattern, paths}}}},
checks [{{id, type: builtin|command|agent, name, run, prompt, blocking, settings}}], review {{skill (builtin:pr-review or
repo:<path>), template [section names], recommendations {{ready, changes, blocked}}}}, labels {{roles {{{roles}}}, rules
[{{label, keywords, title_prefixes, paths}}]}}, ci {{required_workflows, cancelled_is_failure, runner_groups}},
merge {{enabled, method: squash|merge|rebase, arming {{who: write|maintain|admin|codeowners}}, message {{preserve_trailers}}}},
timing {{first_response_days, rereview_days, stale_days}}, languages {{public, internal, style_guide}}.
Do not set policy: every action starts in shadow until maintainers raise it."""


class Onboard(Playbook):
    name = "onboard"
    triggers = (EventKind.REPO_ADDED, EventKind.REPO_FILES_CHANGED)
    pool = "agent"
    needs_profile = False

    def accepts(self, event: Event, profile: RepoProfile | None) -> bool:
        if event.kind == EventKind.REPO_FILES_CHANGED:
            return profile is not None and any(
                p != IN_REPO_PATH for p in event.payload.get("paths", [])
            )
        return event.repo is not None

    def key(self, event: Event) -> str:
        return f"onboard:{event.repo}:{event.kind}:{event.id}"

    async def run(self, ctx: PlaybookContext, event: Event) -> PlaybookResult:
        repo = event.repo
        assert repo
        drift = event.kind == EventKind.REPO_FILES_CHANGED
        info = await ctx.forge.get_repo(repo)
        files = {}
        for path in READ_FILES:
            text = await ctx.forge.get_file(repo, path)
            if text:
                files[path] = text
        workflows = await ctx.forge.list_dir(repo, ".github/workflows")
        for name in workflows[:12]:
            text = await ctx.forge.get_file(repo, f".github/workflows/{name}")
            if text:
                files[f".github/workflows/{name}"] = text
        merged = await ctx.forge.list_merged_prs(repo, 50)
        history = "\n".join(
            f"#{m.number} {m.title} by {m.author} labels={m.labels} trailers={[ln for msg in m.commit_messages for ln in msg.splitlines() if ':' in ln and ln.split(':')[0] in ('Signed-off-by', 'Co-authored-by')][:2]}"
            for m in merged
        )
        current = await ctx.memory.read_text(profile_path(repo))
        packs = ctx.profiles.loader.packs.names()
        prompt = "\n\n".join(
            [
                (
                    f"The repository {repo} changed some of its contribution files ({event.payload.get('paths')}). "
                    "Propose an updated memory profile layer that follows the new conventions. Keep everything that still applies."
                    if drift
                    else f"Draft a profile for the repository {repo} from its own files and recent history."
                ),
                SCHEMA_OUTLINE.format(
                    packs=", ".join(packs),
                    roles=", ".join(f"{r}: <label>" for r in ROLES),
                ),
                f"Labels in the repository: {info.labels}",
                f"Default branch: {info.default_branch}. Required checks: {info.required_checks}.",
                untrusted("current.memory_profile", current or "(none)", 10_000),
                *[untrusted(path, text, 15_000) for path, text in files.items()],
                untrusted("merged_prs", history or "(none)", 15_000),
                schema_hint(OnboardAnswer.model_json_schema()),
            ]
        )
        workdir = ctx.service.scratch_dir / ctx.job.id
        workdir.mkdir(parents=True, exist_ok=True)
        task = AgentTask(
            kind=TaskKind.ONBOARD,
            prompt=prompt,
            workdir=workdir,
            output_schema=OnboardAnswer.model_json_schema(),
            repo=repo,
        )
        answer = await ctx.ask(task, OnboardAnswer)
        if not answer.ok or answer.structured is None:
            raise RuntimeError(f"onboarding agent failed: {answer.error}")
        result = OnboardAnswer.model_validate(answer.structured)
        layer = {
            k: v
            for k, v in result.profile.items()
            if k not in ("policy", "ceilings", "repo", "schema")
        }
        layer["schema"] = 1
        try:
            merged_profile, change = await ctx.profiles.propose_memory_layer(
                repo,
                layer,
                author="service",
                title=(
                    "profile: follow convention changes in "
                    if drift
                    else "profile: onboard "
                )
                + repo,
            )
        except ProfileError as e:
            fixed = await self._repair(ctx, task, str(e))
            if fixed is None:
                raise
            merged_profile, change = await ctx.profiles.propose_memory_layer(
                repo, fixed, author="service", title=f"profile: onboard {repo}"
            )
        change.description = "\n\n".join(
            [
                change.description,
                "Questions for the maintainers:\n"
                + "\n".join(f"- {q}" for q in result.questions)
                if result.questions
                else "",
                result.rationale,
            ]
        ).strip()
        await ctx.store.save_memory_change(change)
        actions = [
            make_action(
                ActionKind.MEMORY_CHANGE,
                repo,
                MemoryChangePayload(change_id=change.id),
                discriminator=change.id,
            ),
            make_action(
                ActionKind.NOTIFY,
                repo,
                NotifyPayload(
                    event="profile.pending",
                    text=f"A profile for {repo} waits for a maintainer's approval: {change.title}",
                ),
                discriminator=change.id,
            ),
        ]
        return PlaybookResult(
            actions=actions,
            result={
                "change": change.id,
                "questions": result.questions,
                "hash": merged_profile.hash,
            },
        )

    async def _repair(
        self, ctx: PlaybookContext, task: AgentTask, error: str
    ) -> dict[str, Any] | None:
        retry = task.model_copy(
            update={
                "prompt": task.prompt
                + f"\n\nYour previous profile did not validate: {error}\nReturn a corrected one."
            }
        )
        answer = await ctx.ask(retry, OnboardAnswer)
        if not answer.ok or answer.structured is None:
            return None
        layer = {
            k: v
            for k, v in OnboardAnswer.model_validate(answer.structured).profile.items()
            if k not in ("policy", "ceilings", "repo")
        }
        layer["schema"] = 1
        return layer


class ProfileDrift(Playbook):
    """Recompute the candidate profile; follow in-repo changes whose author qualifies."""

    name = "profile_drift"
    triggers = (
        EventKind.REPO_FILES_CHANGED,
        EventKind.MEMORY_CHANGED,
        EventKind.CODE_CHANGED,
    )
    tick_names = ("drift",)

    def key(self, event: Event) -> str:
        return f"profile_drift:{event.repo}:{event.id}"

    def accepts(self, event: Event, profile: RepoProfile | None) -> bool:
        return event.repo is not None

    async def run(self, ctx: PlaybookContext, event: Event) -> PlaybookResult:
        repo = event.repo
        assert repo
        state = await ctx.profiles.refresh(repo)
        actions = []
        if state.candidate:
            change = await ctx.profiles.auto_follow(repo, state)
            if change is not None:
                await ctx.store.save_memory_change(change)
                actions.append(
                    make_action(
                        ActionKind.MEMORY_CHANGE,
                        repo,
                        MemoryChangePayload(change_id=change.id),
                        discriminator=change.id,
                    )
                )
            else:
                actions.append(
                    make_action(
                        ActionKind.NOTIFY,
                        repo,
                        NotifyPayload(
                            event="profile.pending",
                            text=f"{repo} has a pending profile change ({state.candidate_hash}); a maintainer must approve it.",
                        ),
                        discriminator=state.candidate_hash or "",
                    )
                )
        return PlaybookResult(
            actions=actions,
            result={"candidate": state.candidate_hash, "error": state.candidate_error},
        )


def layer_from_yaml(text: str) -> dict[str, Any]:
    return parse_yaml(text, "profile")


def dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=1)

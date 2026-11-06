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

"""Hygiene (design 3.2): the profile's checks, in one comment per PR that is updated in place."""

from __future__ import annotations

import hashlib
import json
import shlex

from pydantic import BaseModel

from ..domain.actions import (
    ActionHints,
    ActionKind,
    CommentUpsertPayload,
    PrStatePayload,
    make_action,
)
from ..domain.agents import AgentTask, TaskKind
from ..domain.codeowners import CODEOWNERS_LOCATIONS
from ..domain.events import Event
from ..domain.profile import CheckSpec, RepoProfile
from ..domain.snapshot import PrSnapshot
from ..profiles.checks import (
    CheckInputs,
    CheckResult,
    files_needed,
    render_checklist,
    run_builtin,
    to_data,
)
from .base import PR_EVENTS, Playbook, PlaybookContext, PlaybookResult, in_scope
from .prompts import pr_block, schema_hint, untrusted

HYGIENE_STATE = "hygiene:{repo}#{number}"


class AgentCheckAnswer(BaseModel):
    passed: bool
    summary: str
    details: list[str] = []


def hygiene_key(repo: str, number: int) -> str:
    return HYGIENE_STATE.format(repo=repo, number=number)


class Hygiene(Playbook):
    name = "hygiene"
    triggers = PR_EVENTS

    def key(self, event: Event) -> str:
        return f"hygiene:{event.repo}#{event.number}@{(event.sha or '-')[:12]}:{event.kind}:{event.id[-6:]}"

    async def run(self, ctx: PlaybookContext, event: Event) -> PlaybookResult:
        profile = ctx.require_profile()
        assert event.repo and event.number
        pr = await ctx.forge.get_pr(event.repo, event.number)
        if pr is None or not in_scope(pr, profile):
            return PlaybookResult(result={"skipped": "out of scope"})
        ctx.trusted_input = await ctx.author_is_team(pr)
        fingerprint = hashlib.sha256(
            json.dumps([pr.head_sha, pr.title, pr.body]).encode()
        ).hexdigest()[:12]
        previous = await ctx.store.get_state(hygiene_key(pr.repo, pr.number))
        if previous and previous.get("fingerprint") == fingerprint:
            return PlaybookResult(result={"skipped": "unchanged"})

        specs = [s for s in profile.checks if s.enabled]
        inputs = await self._inputs(ctx, pr, profile, specs)
        results: list[CheckResult] = []
        for spec in specs:
            if spec.type == "builtin":
                results.append(run_builtin(pr, profile, spec, inputs))
            elif spec.type == "command":
                results.append(await self._command(ctx, pr, spec))
            else:
                results.append(await self._agent(ctx, pr, spec))
        await ctx.store.set_state(
            hygiene_key(pr.repo, pr.number),
            {
                "sha": pr.head_sha,
                "fingerprint": fingerprint,
                "results": to_data(results),
            },
        )

        failing = [r for r in results if not r.passed and not r.skipped]
        has_comment = any(
            c.author.lower() == ctx.service.login.lower() and "kind=hygiene" in c.body
            for c in pr.comments
        )
        actions = []
        hints = ActionHints(
            author_is_team=await ctx.author_is_team(pr), changed_paths=pr.paths
        )
        if failing or has_comment:
            actions.append(
                make_action(
                    ActionKind.COMMENT_UPSERT,
                    pr.repo,
                    CommentUpsertPayload(
                        purpose="hygiene", body=render_checklist(results)
                    ),
                    number=pr.number,
                    head_sha=pr.head_sha,
                    discriminator=fingerprint,
                    rationale=f"{len(failing)} hygiene checks need attention",
                    hints=hints,
                )
            )
        template = next(
            (
                r
                for r in results
                if (self._name(profile, r.id) == "template_sections") and not r.passed
            ),
            None,
        )
        if template is not None and not pr.is_draft:
            actions.append(
                make_action(
                    ActionKind.PR_CONVERT_TO_DRAFT,
                    pr.repo,
                    PrStatePayload(
                        body="Converting to draft until the PR description fills the template sections listed in the hygiene checks."
                    ),
                    number=pr.number,
                    head_sha=pr.head_sha,
                    hints=hints,
                )
            )
        return PlaybookResult(
            actions=actions, result={"failing": [r.id for r in failing]}
        )

    @staticmethod
    def _name(profile: RepoProfile, check_id: str) -> str:
        spec = next((s for s in profile.checks if s.id == check_id), None)
        return (spec.name or spec.id) if spec else ""

    async def _inputs(
        self,
        ctx: PlaybookContext,
        pr: PrSnapshot,
        profile: RepoProfile,
        specs: list[CheckSpec],
    ) -> CheckInputs:
        inputs = CheckInputs()
        for spec in specs:
            for path in files_needed(spec):
                if path and path not in inputs.files:
                    inputs.files[path] = await ctx.forge.get_file(pr.repo, path)
        if any((s.name or s.id) == "codeowners_coverage" for s in specs):
            for loc in (
                [profile.people.codeowners]
                if profile.people.codeowners
                else CODEOWNERS_LOCATIONS
            ):
                text = await ctx.forge.get_file(pr.repo, loc)
                if text:
                    inputs.files["CODEOWNERS"] = text
                    break
        if profile.contribution.license_header and any(
            (s.name or s.id) == "license_header" for s in specs
        ):
            for f in pr.files:
                if f.change_type.upper() == "ADDED":
                    inputs.head_files[f.path] = await ctx.forge.get_file(
                        pr.repo, f.path, ref=pr.head_sha
                    )
        return inputs

    async def _command(
        self, ctx: PlaybookContext, pr: PrSnapshot, spec: CheckSpec
    ) -> CheckResult:
        if not spec.run:
            return CheckResult(
                id=spec.id, passed=True, skipped=True, summary="no command configured"
            )
        info = await ctx.forge.get_repo(pr.repo)
        sha = info.default_branch_sha or await ctx.forge.default_branch_sha(pr.repo)
        workdir = await ctx.workspace.prepare(pr.repo, None, sha, info.default_branch)
        try:
            body_file = workdir / ".am-pr-body.md"
            files_file = workdir / ".am-pr-files.txt"
            body_file.write_text(pr.body or "", encoding="utf-8")
            files_file.write_text("\n".join(pr.paths), encoding="utf-8")
            argv = [
                part.format(
                    title=pr.title,
                    body_file=str(body_file),
                    files_file=str(files_file),
                    number=pr.number,
                    head_sha=pr.head_sha,
                )
                for part in shlex.split(spec.run)
            ]
            result = await ctx.sandbox.run(
                argv, cwd=workdir, timeout=float(spec.settings.get("timeout", 120))
            )
        finally:
            await ctx.workspace.release(workdir)
        output = (result.stdout + "\n" + result.stderr).strip().splitlines()[-15:]
        passed = result.exit_code == 0 and not result.timed_out
        summary = spec.settings.get("summary") or f"`{spec.id}`"
        return CheckResult(
            id=spec.id,
            passed=passed,
            blocking=spec.blocking,
            summary=f"{summary} passes" if passed else f"{summary} fails",
            details=[] if passed else [f"`{line}`" for line in output if line.strip()],
        )

    async def _agent(
        self, ctx: PlaybookContext, pr: PrSnapshot, spec: CheckSpec
    ) -> CheckResult:
        workdir = ctx.scratch()
        prompt = "\n\n".join(
            [
                "Judge one hygiene check for this pull request. Answer whether it passes, a one-line summary "
                "written for the PR author, and up to five specific details.",
                untrusted("check.prompt", spec.prompt or spec.id, 4000),
                pr_block(pr),
                schema_hint(AgentCheckAnswer.model_json_schema()),
            ]
        )
        task = AgentTask(
            kind=TaskKind.HYGIENE,
            prompt=prompt,
            workdir=workdir,
            output_schema=AgentCheckAnswer.model_json_schema(),
            repo=pr.repo,
        )
        answer = await ctx.ask(task, AgentCheckAnswer)
        if not answer.ok or answer.structured is None:
            return CheckResult(
                id=spec.id, passed=True, skipped=True, summary="agent check unavailable"
            )
        a = AgentCheckAnswer.model_validate(answer.structured)
        return CheckResult(
            id=spec.id,
            passed=a.passed,
            blocking=spec.blocking,
            summary=a.summary,
            details=a.details[:5],
        )

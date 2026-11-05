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

"""Priority calibration (design 5.6): turn a maintainer's override into the right kind of change."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Literal

from pydantic import BaseModel, Field

from ..domain.actions import ActionKind, MemoryChangePayload, make_action
from ..domain.agents import AgentTask, TaskKind
from ..domain.events import Event, EventKind
from ..domain.memory import MemoryChange, Signal, SignalKind, Tier, repo_dir
from ..domain.profile import RepoProfile
from ..domain.records import ChatMessage, ChatThread
from ..domain.scoring import Rubric, StageGoal
from ..profiles.knowledge import STAGE_GOALS, dump_stage_goals
from ..profiles.layers import dump_yaml, parse_yaml
from .base import Playbook, PlaybookContext, PlaybookResult
from .evaluate import rubric_of
from .prompts import schema_hint, untrusted


class StageGoalChange(BaseModel):
    op: Literal["none", "add", "update", "deactivate"] = "none"
    id: str | None = None
    title: str = ""
    description: str = ""
    keywords: list[str] = Field(default_factory=list)
    boost: float = 2.5
    expires_in_days: int = 30


class RubricChange(BaseModel):
    op: Literal["none", "update_rule", "update_weights"] = "none"
    dimension: str | None = None
    rule: str = ""
    weights: dict[str, float] = Field(default_factory=dict)


class CalibrationResult(BaseModel):
    disposition: Literal[
        "SINGLE_PR_OVERRIDE",
        "STAGE_GOAL_UPDATE",
        "CORE_RUBRIC_UPDATE",
        "ASK_CLARIFICATION",
    ]
    sufficiency: int = Field(ge=1, le=10)
    oscillation_detected: bool = False
    oscillation_warning: str = ""
    message: str = Field(
        description="Reply to the maintainer, in the internal language."
    )
    question: str = ""
    stage_goal: StageGoalChange = Field(default_factory=StageGoalChange)
    rubric: RubricChange = Field(default_factory=RubricChange)


class Calibrate(Playbook):
    name = "calibrate"
    triggers = (EventKind.PRIORITY_OVERRIDDEN,)
    pool = "agent"

    def key(self, event: Event) -> str:
        return f"calibrate:{event.repo}#{event.number}:{event.id}"

    async def run(self, ctx: PlaybookContext, event: Event) -> PlaybookResult:
        profile = ctx.require_profile()
        assert event.repo and event.number is not None
        p = event.payload
        rubric = rubric_of(profile)
        goals = await ctx.knowledge.stage_goals(event.repo, profile)
        history = [
            s.payload
            for s in await ctx.store.list_signals(event.repo, unconsumed=False)
            if s.kind == SignalKind.PRIORITY_OVERRIDE
        ][-8:]
        workdir = ctx.service.scratch_dir / ctx.job.id
        workdir.mkdir(parents=True, exist_ok=True)
        prompt = "\n\n".join(
            [
                "A maintainer changed a PR's internal priority and gave a reason. Decide what kind of change this is:",
                "- SINGLE_PR_OVERRIDE: the reason is specific to this PR; change nothing else.\n"
                "- STAGE_GOAL_UPDATE: the reason describes a time-boxed shift of focus; add or update a stage goal.\n"
                "- CORE_RUBRIC_UPDATE: the reason exposes a gap in the rubric's rules or weights; propose a rubric change.\n"
                "- ASK_CLARIFICATION: the reason is too vague, or contradicts recent overrides; ask one question.",
                "Never turn a one-off reason into a rule. Report oscillation against recent overrides and name them.",
                f"Write the message and question in {profile.languages.internal}.",
                f"PR #{event.number}: before={json.dumps(p.get('before'))} after={json.dumps(p.get('after'))}",
                untrusted("maintainer.reason", str(p.get("reason", "")), 2000),
                f"Rubric:\n{json.dumps(rubric.model_dump(mode='json'), ensure_ascii=False)}",
                f"Active stage goals:\n{json.dumps([g.model_dump(mode='json') for g in goals], ensure_ascii=False)}",
                f"Recent overrides by this repository's maintainers:\n{json.dumps(history, ensure_ascii=False, default=str)}",
                schema_hint(CalibrationResult.model_json_schema()),
            ]
        )
        task = AgentTask(
            kind=TaskKind.CALIBRATE,
            prompt=prompt,
            workdir=workdir,
            output_schema=CalibrationResult.model_json_schema(),
            repo=event.repo,
        )
        ctx.trusted_input = (
            True  # a maintainer's reason and the rubric; no contributor text
        )
        answer = await ctx.ask(task, CalibrationResult)
        if not answer.ok or answer.structured is None:
            raise RuntimeError(f"calibration agent failed: {answer.error}")
        result = CalibrationResult.model_validate(answer.structured)

        actions = []
        change: MemoryChange | None = None
        if result.disposition == "STAGE_GOAL_UPDATE" and result.stage_goal.op != "none":
            change = self._stage_goal_change(
                ctx, profile, goals, result.stage_goal, p.get("by", "service")
            )
        elif result.disposition == "CORE_RUBRIC_UPDATE" and result.rubric.op != "none":
            change = await self._rubric_change(ctx, profile, rubric, result.rubric)
        if change is not None:
            await ctx.store.save_memory_change(change)
            actions.append(
                make_action(
                    ActionKind.MEMORY_CHANGE,
                    event.repo,
                    MemoryChangePayload(change_id=change.id),
                    discriminator=change.id,
                )
            )

        by = str(p.get("by", "service"))
        thread_id = p.get("thread_id")
        thread = await ctx.store.get_thread(thread_id) if thread_id else None
        if thread is None:
            thread = ChatThread(
                owner=by,
                title=f"Calibration: {event.repo}#{event.number}",
                repo=event.repo,
            )
            await ctx.store.save_thread(thread)
        text = result.message
        if result.oscillation_detected and result.oscillation_warning:
            text += f"\n\n⚠️ {result.oscillation_warning}"
        if result.disposition == "ASK_CLARIFICATION" and result.question:
            text += f"\n\n❓ {result.question}"
        await ctx.store.add_message(
            ChatMessage(
                thread_id=thread.id,
                role="assistant",
                text=text,
                proposals=[a.id for a in actions],
            )
        )
        signal = Signal(
            kind=SignalKind.PRIORITY_OVERRIDE,
            repo=event.repo,
            number=event.number,
            source=by,
            payload={**p, "disposition": result.disposition},
        )
        return PlaybookResult(
            actions=actions,
            signals=[signal],
            result={"disposition": result.disposition, "thread": thread.id},
        )

    def _stage_goal_change(
        self,
        ctx: PlaybookContext,
        profile: RepoProfile,
        goals: list[StageGoal],
        spec: StageGoalChange,
        by: str,
    ) -> MemoryChange:
        now = ctx.clock.now()
        goals = list(goals)
        if spec.op == "add":
            gid = spec.id or f"SG-{len(goals) + 1}"
            goals.append(
                StageGoal(
                    id=gid,
                    title=spec.title,
                    description=spec.description,
                    keywords=spec.keywords,
                    boost=spec.boost,
                    expires_at=now + timedelta(days=spec.expires_in_days),
                    added_by=by,
                    added_at=now,
                )
            )
        else:
            for i, g in enumerate(goals):
                if g.id == spec.id:
                    if spec.op == "deactivate":
                        goals[i] = g.model_copy(update={"active": False})
                    else:
                        goals[i] = g.model_copy(
                            update={
                                k: v
                                for k, v in {
                                    "title": spec.title,
                                    "description": spec.description,
                                    "keywords": spec.keywords,
                                    "boost": spec.boost,
                                }.items()
                                if v
                            }
                        )
        path = f"{repo_dir(profile.repo)}/{profile.priority.stage_goals or STAGE_GOALS}"
        return MemoryChange(
            tier=Tier.T1,
            title=f"stage goals: {spec.op} {spec.id or spec.title}",
            files={path: dump_stage_goals(goals)},
            repos=[profile.repo],
            from_own_signals=True,
            created_at=now,
        )

    async def _rubric_change(
        self,
        ctx: PlaybookContext,
        profile: RepoProfile,
        rubric: Rubric,
        spec: RubricChange,
    ) -> MemoryChange | None:
        data = rubric.model_dump(mode="json")
        if spec.op == "update_rule" and spec.dimension in data["dimensions"]:
            data["dimensions"][spec.dimension]["rule"] = spec.rule
        elif spec.op == "update_weights" and spec.weights:
            total = sum(spec.weights.get(k, 0) for k in data["dimensions"]) or 1.0
            for k in data["dimensions"]:
                data["dimensions"][k]["weight"] = round(
                    spec.weights.get(k, 0) / total, 3
                )
        else:
            return None
        data["version"] = rubric.version + 1
        new_rubric = Rubric.model_validate(data)
        layer_text = await ctx.memory.read_text(
            f"{repo_dir(profile.repo)}/profile.yaml"
        )
        layer = parse_yaml(layer_text, "memory profile") if layer_text else {}
        layer.setdefault("priority", {})
        layer["priority"] = {**layer["priority"], "rubric": "rubric/rubric.yaml"}
        rubric_path = f"{repo_dir(profile.repo)}/rubric/rubric.yaml"
        inline = {
            **layer,
            "priority": {
                **layer["priority"],
                "rubric": new_rubric.model_dump(mode="json"),
            },
        }
        merged = await ctx.profiles.loader.candidate(
            profile.repo, memory_override=inline
        )
        change = ctx.profiles.lock_change(
            merged,
            [],
            "calibration",
            extra_files={
                rubric_path: dump_yaml(new_rubric.model_dump(mode="json")),
                f"{repo_dir(profile.repo)}/profile.yaml": dump_yaml(layer),
            },
            title=f"rubric: {spec.op} for {profile.repo}",
        )
        return change

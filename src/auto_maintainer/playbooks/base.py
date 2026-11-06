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

"""The playbook contract (design 5.5) and the context playbooks run with.

A playbook reads through its context and returns actions, signals, events, and an
optional wait. The context has no way to write outside the process: it holds a
``ForgeReader`` and read-only views of CI, memory, and labs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel, Field

from ..domain.actions import Action
from ..domain.agents import AgentResult, AgentTask, TaskKind
from ..domain.clock import Clock
from ..domain.codeowners import (
    CODEOWNERS_LOCATIONS,
    OwnerRule,
    owners_of_paths,
    parse_codeowners,
)
from ..domain.events import Event, EventKind
from ..domain.memory import Signal
from ..domain.permissions import Permission
from ..domain.ports import (
    AgentRunner,
    CiProvider,
    ForgeReader,
    GpuStatus,
    LabHost,
    MemoryStore,
    Sandbox,
    Store,
    Workspace,
)
from ..domain.profile import RepoProfile, Route
from ..domain.records import Job
from ..domain.review import ReviewClassification, classify_review_state
from ..domain.snapshot import PrSnapshot, RunnerInfo
from ..profiles.knowledge import Knowledge
from ..profiles.service import ProfileService


class Wait(BaseModel):
    kinds: list[EventKind]
    seconds: int
    reason: str = ""
    action_ids: list[str] = Field(default_factory=list)


class PlaybookResult(BaseModel):
    actions: list[Action] = Field(default_factory=list)
    signals: list[Signal] = Field(default_factory=list)
    events: list[Event] = Field(default_factory=list)
    wait: Wait | None = None
    retry_after: int | None = None
    """Run again after this many seconds, from fresh reads."""
    result: dict[str, Any] = Field(default_factory=dict)


class CiReader:
    """The read half of a ``CiProvider``."""

    def __init__(self, ci: CiProvider) -> None:
        self._ci = ci

    async def get_job_log(self, repo: str, job_id: int) -> str | None:
        return await self._ci.get_job_log(repo, job_id)

    async def list_runners(self, repo: str) -> list[RunnerInfo]:
        return await self._ci.list_runners(repo)


class MemoryReader:
    """The read half of a ``MemoryStore``."""

    def __init__(self, memory: MemoryStore) -> None:
        self._memory = memory

    async def read_text(self, path: str) -> str | None:
        return await self._memory.read_text(path)

    async def list_files(self, prefix: str) -> list[str]:
        return await self._memory.list_files(prefix)

    async def head(self) -> str:
        return await self._memory.head()


class LabStatus:
    """The read half of lab hosts: resource status only. Running code is an action."""

    def __init__(self, hosts: dict[str, LabHost]) -> None:
        self._hosts = hosts

    def names(self) -> list[str]:
        return sorted(self._hosts)

    async def gpu_status(self, lab: str) -> list[GpuStatus]:
        host = self._hosts.get(lab)
        return await host.gpu_status() if host else []


@dataclass
class ServiceInfo:
    """Service-wide settings playbooks read."""

    login: str
    identities: set[str]
    routing: dict[str, Route]
    skills_dir: Path
    dev_mode: bool = False
    pack_skill_dirs: list[Path] = field(default_factory=list)
    scratch_dir: Path = Path("/tmp/auto-maintainer-scratch")
    code_repo: str | None = None
    """The service's own repository, where self-improvements go."""
    code_branch: str = "main"


@dataclass
class PlaybookContext:
    job: Job
    repo: str | None
    profile: RepoProfile | None
    forge: ForgeReader
    ci: CiReader
    agents: AgentRunner
    memory: MemoryReader
    knowledge: Knowledge
    profiles: ProfileService
    store: Store
    workspace: Workspace
    sandbox: Sandbox
    labs: LabStatus
    clock: Clock
    service: ServiceInfo
    trusted_input: bool = False
    """Whether the input of this job comes from the repository's team; agent tasks
    may run in bypass mode only for trusted input (unless the route says otherwise),
    and reviewers see it next to the agent's questions."""
    requested_by: str | None = None
    """The maintainer who asked for this job; set by the playbook once it knows."""
    agent: str | None = None
    """A runtime the requester chose; the job's main task runs on it with no fallback."""
    own_reviews: frozenset[str] = frozenset()
    """This service's review actions that maintainers posted as themselves."""
    _codeowners: list[OwnerRule] | None = None

    def require_profile(self) -> RepoProfile:
        if self.profile is None:
            raise RuntimeError(
                f"{self.job.playbook} needs an approved profile for {self.repo}"
            )
        return self.profile

    def identities(self) -> set[str]:
        profile = self.profile
        extra = set(profile.people.review_identities) if profile else set()
        return self.service.identities | extra

    def classify(self, pr: PrSnapshot) -> ReviewClassification:
        profile = self.require_profile()
        return classify_review_state(
            pr,
            self.identities(),
            profile.comments.legacy_markers,
            profile.scope.include_drafts,
            own_actions=self.own_reviews,
        )

    def route(self, kind: TaskKind) -> Route:
        profile = self.profile
        route = (
            profile.route(str(kind), self.service.routing)
            if profile
            else self.service.routing.get(str(kind))
        )
        if route is None:
            raise RuntimeError(f"no agent route for {kind}")
        return route

    async def ask(
        self,
        task: AgentTask,
        output_model: type[BaseModel] | None = None,
        kind: TaskKind | None = None,
    ) -> AgentResult:
        task = task.model_copy(
            update={
                "untrusted_input": not self.trusted_input,
                "repo": task.repo or self.repo,
                "pr": task.pr if task.pr is not None else self.job.number,
                "job_id": task.job_id or self.job.id,
                "requested_by": task.requested_by or self.requested_by,
            }
        )
        task_kind = kind or task.kind
        route = self.route(task_kind)
        if self.agent and task_kind in (
            TaskKind.REVIEW,
            TaskKind.FOLLOWUP,
            TaskKind.FIX,
        ):
            # The requester chose the agent; verification keeps its own route.
            route = route.model_copy(update={"primary": self.agent, "fallback": []})
        return await self.agents.run(task, route, output_model)

    async def codeowners(self) -> list[OwnerRule]:
        if self._codeowners is None:
            profile = self.require_profile()
            paths = (
                [profile.people.codeowners]
                if profile.people.codeowners
                else list(CODEOWNERS_LOCATIONS)
            )
            text = None
            for path in paths:
                text = await self.forge.get_file(profile.repo, path)
                if text:
                    break
            self._codeowners = parse_codeowners(text)
        return self._codeowners

    async def codeowners_for(self, paths: list[str]) -> list[str]:
        return owners_of_paths(await self.codeowners(), paths)

    async def may_command(
        self, login: str, minimum: Permission = Permission.WRITE
    ) -> bool:
        """Whether ``login`` may run a slash command that needs ``minimum``."""
        if login.lower() in {i.lower() for i in self.identities()}:
            return False
        return (await self.permission(login)).at_least(minimum)

    async def author_is_team(self, pr: PrSnapshot) -> bool:
        profile = self.require_profile()
        if pr.author in profile.people.team:
            return True
        if pr.author_association in {"OWNER", "MEMBER"}:
            return True
        perm = await self.forge.get_permission(profile.repo, pr.author)
        return perm.at_least(profile.people.team_min_permission)

    async def permission(self, login: str) -> Permission:
        return await self.forge.get_permission(self.require_profile().repo, login)

    def skill_dirs(self) -> list[Path]:
        return [self.service.skills_dir, *self.service.pack_skill_dirs]


class Playbook:
    """Base class. Subclasses set ``name``, ``triggers``, and ``pool``, and implement ``run``."""

    name: ClassVar[str]
    triggers: ClassVar[tuple[EventKind, ...]]
    pool: ClassVar[str] = "io"
    needs_profile: ClassVar[bool] = True
    tick_names: ClassVar[tuple[str, ...]] = ()
    """Schedule names that start one job per active repository."""

    def accepts(self, event: Event, profile: RepoProfile | None) -> bool:
        return True

    def key(self, event: Event) -> str:
        return f"{self.name}:{event.repo}#{event.number}@{(event.sha or '-')[:12]}"

    async def run(self, ctx: PlaybookContext, event: Event) -> PlaybookResult:
        raise NotImplementedError


def in_scope(pr: PrSnapshot, profile: RepoProfile) -> bool:
    """Whether the profile's scope covers ``pr``."""
    scope = profile.scope
    if pr.number < scope.min_pr or not scope.base_in_scope(pr.base_ref):
        return False
    if pr.author in scope.ignore_authors:
        return False
    return scope.include_drafts or not pr.is_draft


PR_EVENTS = (
    EventKind.PR_OPENED,
    EventKind.PR_SYNCHRONIZED,
    EventKind.PR_UPDATED,
    EventKind.PR_RECONCILED,
)

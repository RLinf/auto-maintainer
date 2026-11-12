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

"""Each person's own test machines (design 5.10), as personal agents and model APIs
are theirs (``web/my_agents.py``).

A signed-in person with a repository here adds machines of their own: the reviews,
reproductions, and Fix CI runs they ask for use them first, on any repository they
work on, and nobody else's work ever does (``domain/labs.py:unusable``). The service
reaches them over SSH, through jump hosts of theirs when they name some, with a key it
made for that person (``/api/me/machines`` shows its public half, to add to the
machine's and each jump host's ``authorized_keys``), with an SSH config of their own:
that key alone, no SSH agent, none of the service's own SSH config, keys, or open
connections. Every route works on the signed-in person's own machines.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..domain.labs import personal_name
from ..domain.ports import PersonalAgents
from ..domain.settings import LabMachine, personal_machine_problems
from ..service import Service
from .api import service_of, visible_repos
from .auth import User, current_user
from .model_apis import refused_host


class MachineForm(BaseModel):
    """A person's machine as they describe it; the owner, key, and SSH config are the
    service's to add."""

    host: str = Field(min_length=1, max_length=255)
    user: str | None = Field(default=None, max_length=64)
    port: int | None = Field(default=None, ge=1, le=65535)
    proxy_jump: str | None = Field(default=None, max_length=500)
    """Jump hosts to go through first: ``[user@]host[:port]``, several separated by
    commas, each reached with the same key."""
    description: str = Field(default="", max_length=8_000)
    suited_for: str = Field(default="", max_length=2_000)
    """What the machine is for: the PRs and tests it suits, such as those for hardware
    attached to it."""
    workdir: str = Field(default="am-labs", max_length=500)
    setup: str = Field(default="", max_length=4_000)
    timeout_seconds: int = Field(default=3600, ge=60, le=6 * 3600)
    forbid: list[str] = Field(default_factory=list, max_length=50)
    max_gpu_memory_mib: int = Field(default=500, ge=0, le=1_000_000)
    max_gpu_util_pct: int = Field(default=0, ge=0, le=100)

    def machine(self) -> LabMachine:
        return LabMachine(
            transport="ssh",
            host=self.host.strip(),
            user=(self.user or "").strip() or None,
            port=self.port,
            proxy_jump=(self.proxy_jump or "").strip() or None,
            description=self.description.strip(),
            suited_for=self.suited_for.strip(),
            workdir=self.workdir.strip() or "am-labs",
            setup=self.setup.strip(),
            timeout_seconds=self.timeout_seconds,
            forbid=[p for p in self.forbid if p.strip()],
            max_gpu_memory_mib=self.max_gpu_memory_mib,
            max_gpu_util_pct=self.max_gpu_util_pct,
        )


def _host_port(where: str, port: int | None) -> tuple[str, int]:
    """The host and port of ``[user@]host[:port]``."""
    name, _, after = where.rsplit("@", 1)[-1].partition(":")
    return name, int(after) if after.isdigit() else port or 22


def _people(service: Service) -> PersonalAgents:
    if service.a.people is None:
        raise HTTPException(409, "this host keeps nothing of people's own")
    return service.a.people


async def _may_have_machines(service: Service, user: User) -> PersonalAgents:
    """The store of the user's own things, once they may keep machines here: a
    person with a repository here."""
    people = _people(service)
    if not await visible_repos(service, user):
        raise HTTPException(403, "only people with a repository here add machines")
    return people


def _row(login: str, name: str, machine: LabMachine) -> dict[str, Any]:
    return {
        "name": name,
        "id": personal_name(login, name),
        "host": machine.host,
        "user": machine.user,
        "port": machine.port,
        "proxy_jump": machine.proxy_jump,
        "description": machine.description,
        "suited_for": machine.suited_for,
        "workdir": machine.workdir,
        "setup": machine.setup,
        "timeout_seconds": machine.timeout_seconds,
        "forbid": machine.forbid,
        "max_gpu_memory_mib": machine.max_gpu_memory_mib,
        "max_gpu_util_pct": machine.max_gpu_util_pct,
    }


async def _view(service: Service, user: User, people: PersonalAgents) -> dict[str, Any]:
    key, problem = None, None
    try:
        key = await people.ssh_public_key(user.login)
    except RuntimeError as e:
        problem = str(e)
    machines = await people.machines(user.login)
    return {
        "public_key": key,
        "key_problem": problem,
        "machines": [_row(user.login, n, m) for n, m in sorted(machines.items())],
    }


def build_router() -> APIRouter:
    r = APIRouter(prefix="/api")

    @r.get("/me/machines")
    async def my_machines(
        request: Request, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        """The user's own test machines, and the public key to add to each."""
        service = service_of(request)
        return await _view(service, user, await _may_have_machines(service, user))

    @r.put("/me/machines/{name}")
    async def put_my_machine(
        request: Request,
        name: str,
        body: MachineForm,
        user: User = Depends(current_user),
    ) -> dict[str, Any]:
        """Add or change one of the user's own test machines; the work they ask for
        uses it from now on."""
        service = service_of(request)
        people = await _may_have_machines(service, user)
        machine = body.machine()
        problems = personal_machine_problems(name, machine)
        if problems:
            raise HTTPException(422, "; ".join(problems))
        for where in [machine.host or name, *(machine.proxy_jump or "").split(",")]:
            if not where.strip():
                continue
            host, port = _host_port(
                where.strip(), machine.port if where == machine.host else None
            )
            refused = await refused_host(host, port, "your test machines")
            if refused:
                raise HTTPException(422, refused)
        await people.put_machine(user.login, name, machine)
        await service.refresh_machines()
        return await _view(service, user, people)

    @r.delete("/me/machines/{name}")
    async def delete_my_machine(
        request: Request, name: str, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        """Remove one of the user's own test machines."""
        service = service_of(request)
        people = await _may_have_machines(service, user)
        if name not in await people.machines(user.login):
            raise HTTPException(404, f"you have no test machine {name!r}")
        await people.put_machine(user.login, name, None)
        await service.refresh_machines()
        return await _view(service, user, people)

    @r.post("/me/machines/{name}/check")
    async def check_my_machine(
        request: Request, name: str, user: User = Depends(current_user)
    ) -> dict[str, Any]:
        """Connect to one of the user's own machines with their key and report what
        it is and its GPUs."""
        service = service_of(request)
        people = await _may_have_machines(service, user)
        if name not in await people.machines(user.login):
            raise HTTPException(404, f"you have no test machine {name!r}")
        runner = service.a.lab_runner
        if runner is None:
            raise HTTPException(409, "this host runs nothing on test machines")
        try:
            return await runner.check(personal_name(user.login, name))
        except Exception as e:  # a connection that fails is the answer
            return {"ok": False, "error": str(e)[:500]}

    return r

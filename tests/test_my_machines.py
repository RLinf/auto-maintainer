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

"""Each person's own test machines: theirs to add, reached with a key the service
made for them, and used by the work they ask for and nobody else's."""

from __future__ import annotations

import contextlib
import shutil
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from auto_maintainer.domain.permissions import Permission
from auto_maintainer.testing.demo import build_demo
from auto_maintainer.web.app import create_app

REPO = "acme/widget"
MINE = "maria/gpu-box"

pytestmark = pytest.mark.skipif(
    shutil.which("ssh-keygen") is None, reason="needs ssh-keygen"
)


@pytest.fixture
async def demo(tmp_path: Path):
    h = await build_demo(tmp_path)
    h.forge.set_permission(REPO, "walt", Permission.WRITE)
    app = create_app(h.service, data_dir=tmp_path, allow_dev_login=True)
    yield h, app
    await h.close()


@contextlib.asynccontextmanager
async def _as(app: Any, login: str) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.get(f"/auth/dev?login={login}")
        client.headers["x-am-csrf"] = client.cookies.get("am_csrf") or ""
        yield client


MACHINE = {
    "host": "203.0.113.7",
    "user": "ci",
    "description": "4x A100; models in /data/models",
    "setup": "source ~/venv/bin/activate",
}


async def test_a_person_adds_a_machine_of_their_own(demo, tmp_path: Path) -> None:
    h, app = demo
    async with _as(app, "maria") as maria:
        view = (await maria.get("/api/me/machines")).json()
        assert view["public_key"].startswith("ssh-ed25519 ")
        assert "PRIVATE" not in str(view), "the private key never comes back"
        bad = await maria.put(
            "/api/me/machines/box", json={**MACHINE, "host": "127.0.0.1"}
        )
        assert bad.status_code == 422 and "this host" in bad.text
        named = await maria.put("/api/me/machines/Gpu Box", json=MACHINE)
        assert named.status_code == 422
        added = await maria.put("/api/me/machines/gpu-box", json=MACHINE)
        assert added.status_code == 200, added.text
        [row] = added.json()["machines"]
        assert row["id"] == MINE and row["host"] == "203.0.113.7"
    machine = h.service.machines[MINE]
    assert machine.owner == "maria" and machine.ssh_config and machine.identity_file
    config = Path(machine.ssh_config).read_text()
    assert "IdentitiesOnly yes" in config and "IdentityAgent none" in config
    assert machine.identity_file in config, "its own key and nothing else"
    assert Path(machine.ssh_config).parent != Path.home() / ".ssh"
    async with _as(app, "walt") as walt:
        assert (await walt.get("/api/me/machines")).json()["machines"] == []
        mine = await walt.delete("/api/me/machines/gpu-box")
        assert mine.status_code == 404, "walt cannot touch maria's machine"
    async with _as(app, "maria") as maria:
        gone = await maria.delete("/api/me/machines/gpu-box")
        assert gone.json()["machines"] == [] and MINE not in h.service.machines


async def test_only_the_owners_work_runs_on_their_machine(demo) -> None:
    h, app = demo
    runner = h.service.a.lab_runner
    async with _as(app, "maria") as maria:
        await maria.put("/api/me/machines/gpu-box", json=MACHINE)
        offered = (await maria.get(f"/api/labs?repo={REPO}")).json()["machines"]
        assert offered[0]["name"] == MINE and offered[0]["mine"], "her own come first"
    async with _as(app, "walt") as walt:
        offered = (await walt.get(f"/api/labs?repo={REPO}")).json()["machines"]
        assert MINE not in [m["name"] for m in offered], "nobody else sees it"
        refused = await walt.post(f"/api/prs/{REPO}/42/reproduce", json={"lab": MINE})
        assert refused.status_code == 409 and "maria's own" in refused.text
        checked = await walt.post(f"/api/settings/labs/{MINE}/check")
        assert checked.status_code in (404, 405), "not even routed to it"
        bound = await walt.put(f"/api/repos/{REPO}/labs/{MINE}", json={"use": True})
        assert bound.status_code in (404, 405), "no repository binds someone's own"
    async with _as(app, "maria") as maria:
        await maria.post("/api/reviews", json={"prs": [{"repo": REPO, "number": 42}]})
        await h.settle()
    [opened] = runner.reproduced
    assert opened["payload"].lab == MINE, "her review tests on her own machine"
    assert opened["payload"].requested_by == "maria"


async def test_a_persons_machine_is_reached_with_their_key_alone(
    tmp_path: Path,
) -> None:
    from auto_maintainer.domain.clock import FixedClock
    from auto_maintainer.domain.settings import LabMachine, machine_problems
    from auto_maintainer.labs.executor import LabExecutor
    from auto_maintainer.store.people import PeopleAgents
    from auto_maintainer.store.sqlite import SqliteStore

    people = PeopleAgents(tmp_path / "people")
    await people.ssh_public_key("maria")
    await people.put_machine("maria", "gpu-box", LabMachine(host="203.0.113.7"))
    machines = await people.all_machines()
    store = await SqliteStore.open(tmp_path / "am.db")
    labs = LabExecutor({}, {}, store, FixedClock())
    labs.configure({"a800": LabMachine(host="a800"), **machines})
    mine, shared = labs.hosts[MINE], labs.hosts["a800"]
    assert mine._options[:2] == ["-F", machines[MINE].ssh_config]
    assert str(tmp_path / "people" / "maria" / "ssh") in mine._control
    assert "-F" not in shared._options, "the service's own machines keep its config"
    problems = machine_problems("box", LabMachine(host="203.0.113.7", owner="walt"))
    assert any("owner and ssh_config" in p for p in problems), (
        "the service settings cannot make someone's machine"
    )

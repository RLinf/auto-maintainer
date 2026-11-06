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

"""The ``am`` command line."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import typer

from .config import CONFIG_DIR, EXAMPLE, HostConfig, load_host_config

app = typer.Typer(
    help="auto-maintainer: a maintainer service for GitHub repositories.",
    no_args_is_help=True,
)
repo_app = typer.Typer(help="Repositories the service serves.", no_args_is_help=True)
profile_app = typer.Typer(help="Repository profiles.", no_args_is_help=True)
import_app = typer.Typer(
    help="Import state from other tools (design 7.4).", no_args_is_help=True
)
app.add_typer(repo_app, name="repo")
app.add_typer(profile_app, name="profile")
app.add_typer(import_app, name="import")


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _cfg() -> HostConfig:
    return load_host_config()


@contextlib.asynccontextmanager
async def _service(dev: bool = False) -> AsyncIterator[Any]:
    from .app import build_service

    service = await build_service(_cfg(), dev=dev or os.environ.get("AM_ENV") == "dev")
    try:
        yield service
    finally:
        await service.stop()
        close = getattr(service.store, "close", None)
        if close:
            await close()


async def _whoami() -> str:
    """The caller's own GitHub login, from their ``gh`` token; it identifies them to the gate."""
    import httpx

    from .github.auth import StaticTokenProvider

    token = await StaticTokenProvider("AM_USER_TOKEN").token(None)
    async with httpx.AsyncClient(trust_env=True, timeout=15) as client:
        r = await client.get(
            "https://api.github.com/user", headers={"Authorization": f"Bearer {token}"}
        )
        r.raise_for_status()
        return r.json()["login"]


@app.command()
def init() -> None:
    """Write an example local.yaml if none exists."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    path = CONFIG_DIR / "local.yaml"
    if path.exists():
        typer.echo(f"{path} exists; not overwriting")
        raise typer.Exit(1)
    path.write_text(EXAMPLE, encoding="utf-8")
    secrets = CONFIG_DIR / "secrets.env"
    if not secrets.exists():
        secrets.write_text(
            "# GITHUB_TOKEN=...\n# AM_OAUTH_CLIENT_SECRET=...\n", encoding="utf-8"
        )
        os.chmod(secrets, 0o600)
    typer.echo(f"wrote {path} and {secrets}")


STOP_SECONDS = 15.0
"""How long the service may take to stop after the web server has."""


async def _serve_until_stopped(service: Any, web: Any, host: str, port: int) -> None:
    """Serve ``web`` until Ctrl+C, SIGTERM, or a deploy asks for a restart, then stop
    the service and close its store.

    Shutdown never waits on an idle browser: the dashboard's live streams end as soon
    as it begins, other connections get five seconds, and the service gets
    ``STOP_SECONDS``. A second Ctrl+C skips the wait for connections.
    """
    import uvicorn

    loop = asyncio.get_running_loop()

    def begin_closing() -> None:
        loop.call_soon_threadsafe(service.closing.set)

    class Server(uvicorn.Server):
        def handle_exit(self, sig: int, frame: Any) -> None:
            begin_closing()
            super().handle_exit(sig, frame)
            # uvicorn raises the captured signals again when serve() returns, which
            # would cancel the service's shutdown below halfway; that shutdown is
            # bounded instead.
            self._captured_signals.clear()

    server = Server(
        uvicorn.Config(
            web, host=host, port=port, log_level="info", timeout_graceful_shutdown=5
        )
    )

    async def restart_when_deployed() -> None:
        await service.restart_requested.wait()
        typer.echo("a maintainer deployed new code; restarting")
        begin_closing()
        server.should_exit = True

    watcher = asyncio.create_task(restart_when_deployed())
    try:
        await server.serve()
    finally:
        watcher.cancel()
        try:
            await asyncio.wait_for(service.stop(), STOP_SECONDS)
        except TimeoutError:
            typer.echo(
                f"the service did not stop within {STOP_SECONDS:.0f} s; exiting anyway",
                err=True,
            )
        close = getattr(service.store, "close", None)
        if close is not None:
            await close()


@app.command()
def serve(
    dev: bool = typer.Option(
        False, help="Force shadow mode for every action (AM_ENV=dev)."
    ),
    demo: bool = typer.Option(
        False, help="Run on fakes with sample data; nothing leaves the host."
    ),
    host: str | None = None,
    port: int | None = None,
    verbose: bool = False,
) -> None:
    """Run the API, the workers, and the dashboard."""
    from .web.app import create_app
    from .web.dashboard import ensure_built

    _setup_logging(verbose)
    ensure_built(echo=typer.echo)

    if demo:
        from .testing.demo import DEMO_USER, build_demo

        root = Path(tempfile.mkdtemp(prefix="am-demo-"))

        async def demo_main() -> None:
            # Build and serve in one event loop: the bus, engine, and actuator tasks
            # belong to the loop that created them.
            harness = await build_demo(root)
            service = harness.service
            service.settings.operators.add(DEMO_USER)
            web = create_app(service, data_dir=root, allow_dev_login=True)
            await service.start()
            bind = host or "0.0.0.0"
            url = f"http://{'127.0.0.1' if bind == '0.0.0.0' else bind}:{port or 7777}"
            typer.echo(
                f"demo data in {root}; sign in at {url}/auth/dev?login={DEMO_USER}"
            )
            await _serve_until_stopped(service, web, bind, port or 7777)

        asyncio.run(demo_main())
        return

    cfg = _cfg()
    dev = dev or os.environ.get("AM_ENV") == "dev"
    if dev:
        typer.echo(
            f"dev mode: serving only {cfg.sandbox_repo}"
            if cfg.sandbox_repo
            else "dev mode: every repository action is held at shadow"
        )

    async def main() -> None:
        from .app import build_service
        from .github.webhooks import hint, verify_signature

        service = await build_service(cfg, dev=dev)
        web = create_app(
            service,
            data_dir=cfg.data_path,
            base_url=cfg.base_url,
            web_url=cfg.github.web_url,
            api_url=cfg.github.api_url,
            oauth_client_id=cfg.github.oauth_client_id,
            oauth_secret_env=cfg.github.oauth_client_secret_env,
            webhook_secret_env=cfg.github.webhook_secret_env,
            cookie_secure=cfg.cookie_secure,
            verify_webhook=verify_signature,
            webhook_target=hint,
        )
        await service.start()
        await _serve_until_stopped(service, web, host or cfg.host, port or cfg.port)
        return service.restart_requested.is_set()

    if asyncio.run(main()):
        from .selfupdate import RESTART_EXIT_CODE

        raise typer.Exit(RESTART_EXIT_CODE)


@app.command(
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True}
)
def supervise(ctx: typer.Context) -> None:
    """Run ``am serve`` and keep it running: restart it after a deploy, roll back a
    deploy whose new code fails to start. Extra arguments go to ``am serve``."""
    import sys

    from .selfupdate.supervise import supervise as run_supervisor

    cfg = _cfg()
    checkout = (
        Path(cfg.self_update.checkout).expanduser()
        if cfg.self_update.checkout
        else Path(__file__).resolve().parents[2]
    )
    command = [sys.executable, "-m", "auto_maintainer.cli", "serve", *ctx.args]
    raise typer.Exit(
        run_supervisor(command, checkout, cfg.data_path / "deploys" / "deploy.json")
    )


@app.command()
def doctor(
    live: bool = typer.Option(
        False, help="Also run a tiny structured task on each agent CLI."
    ),
) -> None:
    """Check agent CLIs, forge access, the memory repo, and labs."""

    async def main() -> None:
        async with _service() as service:
            typer.echo(f"forge login: {service.settings.login}")
            typer.echo(f"memory head: {await service.a.memory.head()}")
            for health in await service.a.agents.health():
                mark = "ok " if health.available else "-- "
                typer.echo(
                    f"{mark} agent {health.name}: {health.version or ''} {health.detail}"
                )
            for name, host in service.a.labs.items():
                gpus = await host.gpu_status()
                typer.echo(
                    f"lab {name}: {len(gpus)} GPUs, {sum(1 for g in gpus if g.processes == 0)} without processes"
                )
            if live:
                from .domain.agents import AgentTask, TaskKind
                from .domain.profile import Route

                with tempfile.TemporaryDirectory() as tmp:
                    for name in getattr(service.a.agents, "runtimes", {}):
                        task = AgentTask(
                            kind=TaskKind.PROBE,
                            prompt='Reply with {"ok": true}.',
                            workdir=Path(tmp),
                            output_schema={
                                "type": "object",
                                "properties": {"ok": {"type": "boolean"}},
                                "required": ["ok"],
                            },
                            timeout_seconds=120,
                        )
                        result = await service.a.agents.run(task, Route(primary=name))
                        typer.echo(
                            f"live {name}: {'ok' if result.ok else result.error}"
                        )

    asyncio.run(main())


@repo_app.command("add")
def repo_add(repo: str) -> None:
    """Start onboarding a repository. Needs your GitHub identity (gh auth) to pass the gate."""

    async def main() -> None:
        login = await _whoami()
        async with _service() as service:
            decision = await service.gate.check(
                repo, login, await service.profiles.active(repo)
            )
            if not decision.allowed:
                typer.echo(decision.reason)
                raise typer.Exit(1)
            try:
                await service.add_repo(repo, login)
            except ValueError as e:
                typer.echo(str(e))
                raise typer.Exit(1) from e
            await service.engine.run_until_idle()
            typer.echo(
                f"onboarding {repo}; approve the drafted profile on the dashboard"
            )

    asyncio.run(main())


@repo_app.command("list")
def repo_list() -> None:
    async def main() -> None:
        async with _service() as service:
            for record in await service.store.list_repos():
                typer.echo(
                    f"{record.repo:40} {'active' if record.active else 'pending'}{' paused' if record.paused else ''}"
                )

    asyncio.run(main())


@profile_app.command("show")
def profile_show(
    repo: str,
    candidate: bool = typer.Option(False, help="Show the pending candidate instead."),
) -> None:
    """Print the approved profile (or the candidate) as YAML."""
    from .profiles.layers import dump_yaml

    async def main() -> None:
        async with _service() as service:
            if candidate:
                merged = await service.loader.candidate(repo)
                typer.echo(dump_yaml(merged.data))
                typer.echo("# origins: " + json.dumps(merged.origins, sort_keys=True))
                return
            lock = await service.profiles.lock(repo)
            if lock is None:
                typer.echo(f"{repo} has no approved profile")
                raise typer.Exit(1)
            typer.echo(lock.to_yaml())

    asyncio.run(main())


@profile_app.command("doctor")
def profile_doctor(repo: str) -> None:
    """Check the profile against the live repository."""
    from .profiles.doctor import run_doctor

    async def main() -> None:
        async with _service() as service:
            merged = await service.loader.candidate(repo)
            items = await run_doctor(
                merged.profile, service.a.forge, set(service.a.labs)
            )
            for item in items:
                typer.echo(f"{'ok ' if item.ok else 'XX '} {item.check}: {item.detail}")
            if any(not i.ok for i in items):
                raise typer.Exit(1)

    asyncio.run(main())


@import_app.command("evaluations")
def import_evaluations(path: Path, repo: str = typer.Option(...)) -> None:
    """Load PR evaluations from a JSON object keyed by PR number (design 7.4)."""
    from .domain.records import AuditEntry
    from .domain.scoring import Evaluation, recompute
    from .playbooks.evaluate import rubric_of

    data = json.loads(path.read_text(encoding="utf-8"))

    async def main() -> None:
        async with _service() as service:
            profile = await service.profiles.active(repo)
            if profile is None:
                typer.echo(f"{repo} has no approved profile; approve one first")
                raise typer.Exit(1)
            rubric = rubric_of(profile)
            goals = await service.knowledge.stage_goals(repo, profile)
            count = 0
            for key, entry in data.items():
                scores = entry.get("scores") or {}
                unknown = sorted(set(scores) - set(rubric.dimensions))
                if unknown:
                    typer.echo(
                        f"#{key}: dimensions {unknown} are not in the rubric {sorted(rubric.dimensions)}"
                    )
                    raise typer.Exit(1)
                ev = Evaluation(
                    repo=repo,
                    number=int(key),
                    title=entry.get("title", ""),
                    scores=scores,
                    summary=entry.get("summary", ""),
                    rationale=entry.get("rationale", ""),
                    manual_boost=float(entry.get("manual_boost") or 0),
                    tier_override=entry.get("tier_override")
                    or entry.get("manual_tier_override"),
                    override_reason=entry.get("override_reason"),
                    override_by=entry.get("override_by"),
                    evaluated_by="import",
                    source=f"import:{path.name}",
                )
                ev = recompute(ev, rubric, goals, " ".join([ev.title, ev.summary]))
                await service.store.save_evaluation(ev)
                count += 1
            await service.store.add_audit(
                AuditEntry(
                    kind="import",
                    repo=repo,
                    summary=f"imported {count} evaluations from {path.name}",
                )
            )
            typer.echo(f"imported {count} evaluations into {repo}")

    asyncio.run(main())


@app.command()
def pause(repo: str | None = None, reason: str = "") -> None:
    """Stop execution, for the whole service or one repository. Proposals keep accumulating."""

    async def main() -> None:
        async with _service() as service:
            await service.pipeline.set_paused(True, "cli", repo, reason)
            typer.echo("paused" + (f" {repo}" if repo else ""))

    asyncio.run(main())


@app.command()
def resume(repo: str | None = None) -> None:
    """Resume execution. Resuming one repository needs your identity to pass its gate."""

    async def main() -> None:
        async with _service() as service:
            actor = "cli"
            if repo:
                actor = await _whoami()
                decision = await service.gate.check(
                    repo, actor, await service.profiles.active(repo)
                )
                if not decision.allowed:
                    typer.echo(decision.reason)
                    raise typer.Exit(1)
            await service.pipeline.set_paused(False, actor, repo)
            typer.echo("resumed" + (f" {repo}" if repo else ""))

    asyncio.run(main())


def main() -> None:
    app()


if __name__ == "__main__":
    main()

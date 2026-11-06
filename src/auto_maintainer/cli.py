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
import shutil
import tempfile
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import typer

from .config import HostConfig, load_host_config

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


LINKS_FILE = "sign-in-links.json"
"""One-time sign-in links from ``am login``, in the data directory."""


def _lan_ip() -> str | None:
    import socket

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("10.255.255.255", 1))
            return str(sock.getsockname()[0])
    except OSError:
        return None


def _print_setup_link(port: int, code: str) -> None:
    hosts = ["127.0.0.1", *([ip] if (ip := _lan_ip()) else [])]
    typer.echo(
        "auto-maintainer is not set up yet. Open this in a browser to set it up:"
    )
    for h in hosts:
        typer.echo(f"  http://{h}:{port}/setup/start?code={code}")
    typer.echo(
        "The code proves you are at this host; it stops working once setup is done."
    )


@app.command()
def setup(port: int | None = None) -> None:
    """Print the link to the setup page, where the host is set up on the web."""
    from .config import is_configured
    from .hostsetup import HostSetup

    if is_configured():
        cfg = _cfg()
        typer.echo(
            f"already set up. Change it on the dashboard: {cfg.base_url}/setup "
            "(needs maintain on the service's repository)."
        )
        return
    _print_setup_link(port or HostConfig().port, HostSetup().code())


@app.command(hidden=True)
def init() -> None:
    """Older name of ``am setup``."""
    setup()


@app.command()
def login() -> None:
    """Print a one-time link that signs you in to the dashboard as the GitHub account
    logged in on this host (``gh``)."""
    from .web.auth import LINK_MINUTES, create_link

    cfg = _cfg()
    who = asyncio.run(_whoami())
    url = create_link(cfg.data_path / LINKS_FILE, who, cfg.base_url)
    typer.echo(
        f"Open this to sign in as {who}; it works once, for {LINK_MINUTES} minutes:"
    )
    typer.echo(f"  {url}")


STOP_SECONDS = 15.0
"""How long the service may take to stop after the web server has."""


def _gh_signin(cfg: HostConfig) -> Any:
    """GitHub CLI logins for signing in on the web, each in its own throwaway folder.

    Sign-ins in progress live in the web server's memory, so none survives a restart:
    folders a previous run left behind (a browser closed before the poll that deletes
    them) are removed here, with any token in them.
    """
    from .hostsetup import GhLogin

    root = cfg.data_path / "sign-in"
    shutil.rmtree(root, ignore_errors=True)
    return lambda flow_id: GhLogin(root / flow_id, cfg.github.web_url, throwaway=True)


class _Switch:
    """An ASGI app that forwards to the current one: the setup page until the host is
    set up, then the running service's dashboard and API."""

    def __init__(self) -> None:
        self.app: Any = None

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        await self.app(scope, receive, send)


class _Host:
    """The web server and the service behind it. Setting the host up, or changing the
    setup from the dashboard, starts a new service without stopping the server.

    Shutdown never waits on an idle browser: the dashboard's live streams end as soon
    as it begins, other connections get five seconds, and the service gets
    ``STOP_SECONDS``. A second Ctrl+C skips the wait for connections.
    """

    def __init__(self, dev: bool = False) -> None:
        self.dev = dev
        self.service: Any = None
        self.switch = _Switch()
        self.restart = False
        self._steps: Any = None
        self._server: Any = None
        self._watch: asyncio.Task[None] | None = None

    def steps(self) -> Any:
        """The host setup in progress (``hostsetup.HostSetup``)."""
        from .hostsetup import HostSetup

        if self._steps is None:
            self._steps = HostSetup()
        return self._steps

    def adopt(self, service: Any, web: Any) -> None:
        self.service = service
        self.switch.app = web
        self._watch_service(service)

    async def start_service(self, cfg: HostConfig) -> None:
        """Build the service for ``cfg`` and put it in place of the current one. If
        building fails, the current one keeps running and the error goes to the caller."""
        from .app import build_service
        from .github.webhooks import hint, verify_signature
        from .web.app import create_app

        new = await build_service(cfg, dev=self.dev)
        web = create_app(
            new,
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
            links_path=cfg.data_path / LINKS_FILE,
            gh_signin=_gh_signin(cfg),
            setup_steps=self.steps,
            on_host_change=self.start_service,
        )
        old = self.service
        if old is not None:
            await self._stop(old)
        await new.start()
        self._steps = None
        self.adopt(new, web)

    def _watch_service(self, service: Any) -> None:
        if self._watch is not None:
            self._watch.cancel()

        async def restart_when_deployed() -> None:
            await service.restart_requested.wait()
            typer.echo("a maintainer deployed new code; restarting")
            self.restart = True
            service.closing.set()
            if self._server is not None:
                self._server.should_exit = True

        self._watch = asyncio.create_task(restart_when_deployed())

    async def _stop(self, service: Any) -> None:
        try:
            await asyncio.wait_for(service.stop(), STOP_SECONDS)
        except TimeoutError:
            typer.echo(
                f"the service did not stop within {STOP_SECONDS:.0f} s; going on",
                err=True,
            )
        close = getattr(service.store, "close", None)
        if close is not None:
            await close()

    async def serve(self, host: str, port: int) -> None:
        import uvicorn

        loop = asyncio.get_running_loop()
        runner = self

        class Server(uvicorn.Server):
            def handle_exit(self, sig: int, frame: Any) -> None:
                if runner.service is not None:
                    loop.call_soon_threadsafe(runner.service.closing.set)
                super().handle_exit(sig, frame)
                # uvicorn raises the captured signals again when serve() returns,
                # which would cancel the service's shutdown below halfway; that
                # shutdown is bounded instead.
                self._captured_signals.clear()

        self._server = Server(
            uvicorn.Config(
                self.switch,
                host=host,
                port=port,
                log_level="info",
                timeout_graceful_shutdown=5,
            )
        )
        try:
            await self._server.serve()
        finally:
            if self._steps is not None:
                self._steps.gh_login.cancel()
            if self._watch is not None:
                self._watch.cancel()
            if self.service is not None:
                await self._stop(self.service)


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
        from .domain.settings import UserToken
        from .testing.demo import DEMO_USER, build_demo

        root = Path(tempfile.mkdtemp(prefix="am-demo-"))

        async def demo_main() -> None:
            # Build and serve in one event loop: the bus, engine, and actuator tasks
            # belong to the loop that created them.
            harness = await build_demo(root)
            service = harness.service
            service.settings.operators.add(DEMO_USER)
            # The demo's forge is a fake, so its maintainer can post and open fix
            # PRs "as themselves" without a GitHub token.
            await service.a.user_tokens.set(DEMO_USER, UserToken(token="demo"))
            # Agents take their steps slowly enough to watch them in the live view.
            harness.runner.step_delay = 0.7
            from .background import log_file

            web = create_app(
                service,
                data_dir=root,
                allow_dev_login=True,
                log_path=log_file(_cfg().data_path),
            )
            await service.start()
            bind = host or "0.0.0.0"
            url = f"http://{'127.0.0.1' if bind == '0.0.0.0' else bind}:{port or 7777}"
            typer.echo(
                f"demo data in {root}; sign in at {url}/auth/dev?login={DEMO_USER}"
            )
            runner = _Host()
            runner.adopt(service, web)
            await runner.serve(bind, port or 7777)

        asyncio.run(demo_main())
        return

    from .config import is_configured

    dev = dev or os.environ.get("AM_ENV") == "dev"
    defaults = _cfg() if is_configured() else HostConfig()
    if dev:
        typer.echo(
            f"dev mode: serving only {defaults.sandbox_repo}"
            if defaults.sandbox_repo
            else "dev mode: every repository action is held at shadow"
        )

    async def main() -> bool:
        from .web.setup import create_setup_app

        runner = _Host(dev)
        if is_configured():
            try:
                await runner.start_service(_cfg())
            except Exception as e:  # the GitHub account or memory repo does not work
                typer.echo(f"could not start with the current setup: {e}", err=True)
        if runner.service is None:
            steps = runner.steps()
            runner.switch.app = create_setup_app(
                steps,
                data_dir=defaults.data_path,
                on_finish=runner.start_service,
                cookie_secure=defaults.cookie_secure,
            )
            _print_setup_link(port or defaults.port, steps.code())
        await runner.serve(host or defaults.host, port or defaults.port)
        return runner.restart

    if asyncio.run(main()):
        from .selfupdate import RESTART_EXIT_CODE

        raise typer.Exit(RESTART_EXIT_CODE)


@app.command(
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True}
)
def supervise(
    ctx: typer.Context,
    log_file: Path | None = typer.Option(
        None, help="Write the service's output here, rotated by size."
    ),
) -> None:
    """Run ``am serve`` and keep it running: restart it after a deploy, roll back a
    deploy whose new code fails to start. Extra arguments go to ``am serve``."""
    import sys

    from .selfupdate.supervise import supervise as run_supervisor

    cfg = _cfg()
    command = [sys.executable, "-m", "auto_maintainer.cli", "serve", *ctx.args]
    raise typer.Exit(
        run_supervisor(
            command,
            _checkout(cfg),
            cfg.data_path / "deploys" / "deploy.json",
            log_file=log_file,
        )
    )


def _checkout(cfg: HostConfig) -> Path:
    return (
        Path(cfg.self_update.checkout).expanduser()
        if cfg.self_update.checkout
        else Path(__file__).resolve().parents[2]
    )


@app.command(
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True}
)
def start(ctx: typer.Context) -> None:
    """Run the service in the background, supervised: it keeps running when the
    terminal closes. Extra arguments go to ``am serve``. Follow it with ``am logs -f``;
    stop it with ``am stop``."""
    import sys

    from . import background

    cfg = _cfg()
    data = cfg.data_path
    log = background.log_file(data)
    command = [
        sys.executable,
        "-m",
        "auto_maintainer.cli",
        "supervise",
        "--log-file",
        str(log),
        *ctx.args,
    ]
    try:
        pid = background.start(command, data, _checkout(cfg))
    except RuntimeError as e:
        typer.echo(str(e))
        raise typer.Exit(1) from e
    time.sleep(3)
    if background.running(data) is None:
        typer.echo("the service stopped right after starting; its last lines:")
        for line in background.tail(log, 30) or background.tail(
            background.start_log(data), 30
        ):
            typer.echo(f"  {line}")
        raise typer.Exit(1)
    typer.echo(f"auto-maintainer runs in the background (pid {pid}).")
    typer.echo(f"  dashboard: {_serving_url(cfg, background.tail(log, 200))}")
    typer.echo(f"  logs:      {log}   (am logs -f to follow)")
    typer.echo("  stop:      am stop")
    for line in background.tail(log, 200):
        if "/setup/start?code=" in line:
            typer.echo(f"  not set up yet; open: {line.split()[-1]}")


def _serving_url(cfg: HostConfig, lines: list[str]) -> str:
    """The configured address, with the port the server last said it listens on
    (``--port`` may differ from the configuration)."""
    from urllib.parse import urlsplit

    for line in reversed(lines):
        if "Uvicorn running on http" in line:
            port = line.split("Uvicorn running on ")[1].split()[0].rsplit(":", 1)[-1]
            base = urlsplit(cfg.base_url)
            if port.isdigit() and str(base.port or "") != port:
                return f"{base.scheme}://{base.hostname}:{port}"
            break
    return cfg.base_url


@app.command()
def stop() -> None:
    """Stop the service that ``am start`` runs in the background."""
    from . import background

    data = _cfg().data_path
    pid = background.running(data)
    if pid is None:
        typer.echo("the service does not run in the background")
        raise typer.Exit(1)
    typer.echo(f"stopping (pid {pid}); running jobs get up to 20 s to finish")
    background.stop(data)
    typer.echo("stopped")


@app.command()
def status() -> None:
    """Whether the service runs in the background, and where its logs are."""
    from . import background

    cfg = _cfg()
    data = cfg.data_path
    pid = background.running(data)
    log = background.log_file(data)
    if pid is None:
        typer.echo("not running in the background (start it with am start)")
    else:
        since = datetime.fromtimestamp(background.pid_file(data).stat().st_mtime, UTC)
        typer.echo(f"running (pid {pid}) since {since:%Y-%m-%d %H:%M} UTC")
        typer.echo(f"  dashboard: {_serving_url(cfg, background.tail(log, 400))}")
    typer.echo(f"  logs:      {log}")
    for line in background.tail(log, 5):
        typer.echo(f"  | {line}")


@app.command()
def logs(
    follow: bool = typer.Option(
        False, "--follow", "-f", help="Keep printing new lines."
    ),
    lines: int = typer.Option(
        100, "--lines", "-n", help="How many lines to show first."
    ),
) -> None:
    """Show the log of the service that ``am start`` runs in the background."""
    from . import background

    log = background.log_file(_cfg().data_path)
    if not log.exists():
        typer.echo(
            f"no log yet at {log}; the service writes one when started with am start"
        )
        raise typer.Exit(1)
    for line in background.tail(log, lines):
        typer.echo(line)
    if follow:
        try:
            for line in background.follow(log):
                typer.echo(line)
        except KeyboardInterrupt:
            pass


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
                repo, login, await service.profiles.active(repo), person=True
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
            await service.reload_settings()
            merged = await service.loader.candidate(repo)
            items = await run_doctor(
                merged.profile, service.a.forge, set(service.a.labs), service.machines
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
                    repo, actor, await service.profiles.active(repo), person=True
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

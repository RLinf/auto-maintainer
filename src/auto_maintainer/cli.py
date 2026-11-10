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

from .config import HTTPS_PORT, HostConfig, load_host_config, serves_https

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
    """The host config; a GitHub CLI the service installed is put on ``PATH``."""
    from .ghcli import add_to_path

    cfg = load_host_config()
    add_to_path(cfg.data_path)
    return cfg


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
    """The caller's own login on the main forge, from that forge's CLI on this host
    (``gh``, ``glab``, or ``AM_USER_TOKEN``); it identifies them to the gate."""
    from .forges.registry import kind

    forge = _cfg().default_forge
    token = os.environ.get("AM_USER_TOKEN")
    if not token and forge.kind == "gitlab":
        from .forges.gitlab.client import GlabToken

        token = await GlabToken(forge.host).token(None)
    elif not token:
        from .forges.github.auth import StaticTokenProvider

        token = await StaticTokenProvider("AM_USER_TOKEN").token(None)
    who = kind(forge.kind).who
    if not token or who is None:
        typer.echo(f"sign in with the {forge.kind} CLI on this host first", err=True)
        raise typer.Exit(1)
    return (await who(forge.api, token))["login"]


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


def _ssl_options(cfg: HostConfig) -> tuple[dict[str, str], Path | None]:
    """uvicorn's TLS options for serving ``cfg`` over HTTPS, and the private
    authority's certificate to offer at ``/tls/ca.crt`` (``None`` for a certificate
    of the host's own, from ``tls``).

    Without ``tls``, the certificate in ``<config>/tls/`` is made on the first start
    and renewed before it expires (``tls.ensure``). Exits with a message when a
    configured file is missing or the certificate cannot be made, before anything
    listens.
    """
    from . import tls as certs
    from .config import CONFIG_DIR

    if cfg.tls.cert:
        files = {
            "ssl_certfile": os.path.expanduser(cfg.tls.cert),
            "ssl_keyfile": os.path.expanduser(cfg.tls.key or cfg.tls.cert),
        }
        for path in files.values():
            if not Path(path).is_file():
                typer.echo(
                    f"tls in local.yaml names {path}, which does not exist; remove "
                    "tls to serve the certificate the service makes itself",
                    err=True,
                )
                raise typer.Exit(1)
        return files, None
    try:
        made = certs.ensure(CONFIG_DIR / "tls", certs.default_names(cfg.base_url))
    except certs.TlsError as e:
        typer.echo(f"cannot serve HTTPS: {e}", err=True)
        raise typer.Exit(1) from e
    if made.made:
        _announce(made, cfg.base_url)
    return {"ssl_certfile": str(made.cert), "ssl_keyfile": str(made.key)}, made.ca_cert


def _announce(files: Any, base_url: str) -> None:
    """Say what a new certificate means for the people who open the dashboard."""
    from urllib.parse import urlsplit

    typer.echo(f"HTTPS certificate for {', '.join(files.names)} in {files.folder}")
    fingerprint = _sha256_fingerprint(files.ca_cert)
    if files.new_authority:
        typer.echo(
            "Everyone who opens the dashboard installs the private authority once, "
            "as a trusted root in their system or browser:\n"
            f"  {files.ca_cert}, also at {base_url}/tls/ca.crt\n"
            f"  SHA-256 {fingerprint} (compare it before trusting the file)\n"
            "  It can sign only for the addresses above."
        )
    else:
        typer.echo(f"  under the same authority, SHA-256 {fingerprint}")
    if (urlsplit(base_url).hostname or "") not in files.names:
        typer.echo(
            f"  base_url ({base_url}) names another address: run am tls with it",
            err=True,
        )


def _sha256_fingerprint(cert: Path) -> str:
    import hashlib
    import ssl

    der = ssl.PEM_cert_to_DER_cert(cert.read_text(encoding="ascii"))
    digest = hashlib.sha256(der).hexdigest().upper()
    return ":".join(digest[i : i + 2] for i in range(0, len(digest), 2))


def _check_port(host: str, port: int) -> None:
    """Exit with what to do when this user may not listen on ``port``."""
    import socket

    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((host, port))
    except PermissionError as e:
        typer.echo(
            f"this user may not listen on port {port}. Allow it once, as root:\n"
            f"  sysctl -w net.ipv4.ip_unprivileged_port_start={port}\n"
            f"  echo net.ipv4.ip_unprivileged_port_start={port} "
            "> /etc/sysctl.d/60-auto-maintainer.conf\n"
            "or serve plain HTTP on another port: am serve --port 7777",
            err=True,
        )
        raise typer.Exit(1) from e
    except OSError:
        pass  # in use, or another reason: uvicorn reports it


def _url(scheme: str, host: str, port: int) -> str:
    default = 443 if scheme == "https" else 80
    return f"{scheme}://{host}" + ("" if port == default else f":{port}")


def _print_setup_link(
    port: int, code: str, https: bool = False, *, set_up: bool = False
) -> None:
    hosts = ["127.0.0.1", *([ip] if (ip := _lan_ip()) else [])]
    typer.echo(
        "Open this in a browser to change the host setup, or to take ownership as an "
        "admin of the service's repository when its owner cannot:"
        if set_up
        else "auto-maintainer is not set up yet. Open this in a browser to set it up:"
    )
    scheme = "https" if https else "http"
    for h in hosts:
        typer.echo(f"  {_url(scheme, h, port)}/setup/start?code={code}")
    typer.echo(
        "The code proves you are at this host; it stops working once setup is saved."
    )


@app.command()
def setup(port: int | None = None) -> None:
    """Print the link to the setup page, where the host is set up on the web; on a
    host already set up, it lets an admin of the service's repository take
    ownership."""
    from .config import is_configured, legacy_github
    from .hostsetup import HostSetup

    set_up = is_configured() and not legacy_github()
    port = port or (_cfg().port if is_configured() else HostConfig().port)
    _print_setup_link(port, HostSetup().code(), serves_https(port), set_up=set_up)


@app.command(hidden=True)
def init() -> None:
    """Older name of ``am setup``."""
    setup()


@app.command()
def login() -> None:
    """Print a one-time link that signs you in to the dashboard as the account the
    main forge's CLI on this host is logged in to (``gh``, ``glab``)."""
    from .web.auth import LINK_MINUTES, create_link

    cfg = _cfg()
    who = asyncio.run(_whoami())
    url = create_link(cfg.data_path / LINKS_FILE, who, cfg.base_url)
    typer.echo(
        f"Open this to sign in as {who}; it works once, for {LINK_MINUTES} minutes:"
    )
    typer.echo(f"  {url}")


@app.command()
def tls(
    names: list[str] = typer.Argument(
        None,
        help="Addresses people open, IP addresses or host names; by default those of "
        "the current authority, else the host in base_url, this host's address, and "
        "its name.",
    ),
    new_authority: bool = typer.Option(
        False,
        "--new-authority",
        help="Replace the private authority; everyone installs its ca.crt again.",
    ),
) -> None:
    """Make the dashboard's HTTPS certificate again, for other addresses or under a
    new private authority. The service makes one on its first start by itself."""
    from . import tls as certs
    from .config import CONFIG_DIR

    cfg = load_host_config()
    folder = CONFIG_DIR / "tls"
    if not names:
        kept = None if new_authority else certs.authority_names(folder)
        names = list(kept or certs.default_names(cfg.base_url))
    try:
        files = certs.make(folder, names, new_authority=new_authority)
    except certs.TlsError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1) from e
    _announce(files, cfg.base_url)
    if cfg.tls.cert:
        typer.echo(
            f"  tls in local.yaml names {cfg.tls.cert}, which the service serves "
            "instead; remove tls to serve this one",
            err=True,
        )
    elif not serves_https(cfg.port):
        typer.echo(
            f"  the service listens on port {cfg.port}, which serves plain HTTP; "
            f"HTTPS is served on port {HTTPS_PORT}",
            err=True,
        )
    typer.echo("Restart the service to serve it: am stop && am start")


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
    return lambda flow_id: GhLogin(
        root / flow_id, cfg.default_forge.web, throwaway=True
    )


def _webhooks(cfg: HostConfig) -> dict[str, Any]:
    """How each forge's webhook deliveries are checked (``/webhooks/<forge>``)."""
    from .forges.registry import kind
    from .web.app import WebhookCheck

    checks = {}
    for forge in cfg.forges:
        k = kind(forge.kind)
        if k.verify_webhook and k.webhook_target:
            checks[forge.id] = WebhookCheck(
                k.verify_webhook,
                k.webhook_target,
                forge.webhook_secret_env,
                k.webhook_headers[0].lower(),
                k.webhook_headers[1].lower(),
            )
    return checks


def _forge_signins(cfg: HostConfig) -> dict[str, Any]:
    """Signing in with an account on each forge but a GitHub main forge, whose people
    sign in with the GitHub CLI (``_gh_signin``): the forge CLI's device code, each in
    a throwaway folder, or a personal access token."""
    from functools import partial

    from .forges.registry import kind
    from .hostsetup import cli_login
    from .web.auth import ForgeSignIn

    root = cfg.data_path / "sign-in"
    out = {}
    for i, forge in enumerate(cfg.forges):
        if i == 0 and forge.kind == "github":
            continue
        k = kind(forge.kind)
        if k.who is None:
            continue
        device_ok = (
            forge.kind != "gitlab"
            or forge.web == "https://gitlab.com"
            or bool(forge.oauth_client_id)
        )
        out[forge.id] = ForgeSignIn(
            forge=forge.id,
            name=f"{k.title} ({forge.host})",
            who=partial(k.who, forge.api),
            device=(
                lambda flow_id, f=forge: cli_login(
                    f.kind,
                    root / flow_id,
                    f.web,
                    throwaway=True,
                    client_id=f.oauth_client_id,
                )
            )
            if device_ok
            else None,
        )
    return out


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

    def __init__(self, dev: bool = False, https: bool = False) -> None:
        self.dev = dev
        self.https = https
        """Whether the server speaks HTTPS; cookies are then sent only over it."""
        self.ca_cert: Path | None = None
        """The private authority's certificate the dashboard offers, with HTTPS."""
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
        from .config import CONFIG_DIR
        from .hostsetup import ForgeSteps
        from .web.app import create_app

        new = await build_service(cfg, dev=self.dev)
        web = create_app(
            new,
            data_dir=cfg.data_path,
            api_url=cfg.default_forge.api,
            cookie_secure=cfg.cookie_secure or self.https,
            webhooks=_webhooks(cfg),
            forge_signins=_forge_signins(cfg),
            links_path=cfg.data_path / LINKS_FILE,
            gh_signin=_gh_signin(cfg) if cfg.default_forge.kind == "github" else None,
            setup_steps=self.steps,
            on_host_change=self.start_service,
            forge_steps=ForgeSteps(
                CONFIG_DIR,
                cfg.data_path,
                Path(cfg.self_update.checkout).expanduser()
                if cfg.self_update.checkout
                else None,
            ),
            ca_cert=self.ca_cert,
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

    async def serve(
        self, host: str, port: int, ssl: dict[str, str] | None = None
    ) -> None:
        """Serve until stopped: HTTPS with uvicorn's ``ssl`` options."""
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
                **(ssl or {}),
            )
        )
        try:
            await self._server.serve()
        finally:
            if self._steps is not None:
                self._steps.cancel()
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

    from .config import is_configured, legacy_github
    from .ghcli import add_to_path

    dev = dev or os.environ.get("AM_ENV") == "dev"
    defaults = _cfg() if is_configured() else HostConfig()
    add_to_path(defaults.data_path)
    if dev:
        typer.echo(
            f"dev mode: serving only {defaults.sandbox_repo}"
            if defaults.sandbox_repo
            else "dev mode: every repository action is held at shadow"
        )
    bind, listen = host or defaults.host, port or defaults.port
    https = serves_https(listen)
    _check_port(bind, listen)
    ssl, ca_cert = _ssl_options(defaults) if https else ({}, None)

    async def main() -> bool:
        from .web.setup import create_setup_app

        runner = _Host(dev, https=https)
        runner.ca_cert = ca_cert
        legacy = legacy_github() if is_configured() else None
        if legacy:
            typer.echo(
                f"this host acted on GitHub through {legacy}, which is no longer "
                "supported: connect the GitHub CLI on the setup page (the other "
                "answers are kept)",
                err=True,
            )
        elif is_configured():
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
                cookie_secure=defaults.cookie_secure or https,
                ca_cert=ca_cert,
            )
            _print_setup_link(listen, steps.code(), https)
        await runner.serve(bind, listen, ssl)
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
    """The configured address, with the scheme and port the server last said it
    listens on (``--port`` may differ from the configuration)."""
    from urllib.parse import urlsplit

    for line in reversed(lines):
        if "Uvicorn running on http" in line:
            served = urlsplit(line.split("Uvicorn running on ")[1].split()[0])
            base = urlsplit(cfg.base_url)
            if served.port is None or base.hostname is None:
                break
            base_port = base.port or (443 if base.scheme == "https" else 80)
            if (served.scheme, served.port) != (base.scheme, base_port):
                return _url(served.scheme, base.hostname, served.port)
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
    """Check agent CLIs, forge access, the memory repo, the sandbox, and labs."""
    cfg_sandbox = _cfg().sandbox

    async def main() -> None:
        async with _service() as service:
            typer.echo(f"forge login: {service.settings.login}")
            typer.echo(f"memory head: {await service.a.memory.head()}")
            for health in await service.a.agents.health():
                mark = "ok " if health.available else "-- "
                typer.echo(
                    f"{mark} agent {health.name}: {health.version or ''} {health.detail}"
                )
            if cfg_sandbox.enabled:
                from .agents.container import ContainerSandbox, image_tag

                box = ContainerSandbox(cfg_sandbox.image or image_tag())
                typer.echo(
                    f"ok  sandbox: agents run in containers of {box.image}"
                    if await box.ready()
                    else f"--  sandbox: {box.image} is missing; it builds when the "
                    "service starts, or run `am sandbox` (needs docker)"
                )
            else:
                typer.echo("--  sandbox: off; agents run on the host")
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


@app.command("sandbox")
def sandbox_build(
    force: bool = typer.Option(False, help="Build again even if the image is there."),
) -> None:
    """Build the image agents run in, one fresh container per task (design 5.7)."""
    from .agents.container import ContainerSandbox, image_tag

    cfg = _cfg()

    async def main() -> None:
        sandbox = ContainerSandbox(cfg.sandbox.image or image_tag())
        if not force and await sandbox.ready():
            typer.echo(f"{sandbox.image} is there")
            return
        typer.echo(f"building {sandbox.image} ...")
        typer.echo(await sandbox.build())
        typer.echo(f"built {sandbox.image}")

    asyncio.run(main())


@repo_app.command("add")
def repo_add(repo: str) -> None:
    """Start onboarding a repository. Needs your identity (the main forge's CLI
    login on this host) to pass the gate."""

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


@repo_app.command("rename")
def repo_rename(old: str, new: str) -> None:
    """Rename a repository to its spelling on GitHub when they differ only in letter
    case: its memory folder and every record. Stop the service first (`am stop`).
    Needs your GitHub identity (gh auth) to pass the configuration gate."""
    from .background import running

    if running(_cfg().data_path):
        typer.echo("The service is running; stop it first with `am stop`.")
        raise typer.Exit(1)

    async def main() -> None:
        login = await _whoami()
        async with _service() as service:
            try:
                changed = await service.rename_repo(old, new, login)
            except ValueError as e:
                typer.echo(str(e))
                raise typer.Exit(1) from e
            typer.echo(f"renamed {old} to {new}: {changed} records")

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

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

"""HTTPS from a private authority (``am tls``): what a browser that trusts the
authority accepts, what it refuses, and how the host config serves it."""

from __future__ import annotations

import shutil
import ssl
import subprocess
from pathlib import Path

import httpx
import pytest
import typer
import yaml
from typer.testing import CliRunner

from auto_maintainer import cli, config, tls
from auto_maintainer.config import HostConfig, TlsConfig, serves_https
from auto_maintainer.testing.demo import build_demo
from auto_maintainer.web.app import create_app

needs_openssl = pytest.mark.skipif(
    shutil.which("openssl") is None, reason="openssl is not installed"
)


def _handshake(files: tls.TlsFiles, ca: Path, name: str) -> None:
    """A TLS handshake in memory between the server certificate and a client that
    trusts only ``ca`` and opens ``name``; raises ``ssl.SSLError`` when refused."""
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(files.cert, files.key)
    client_ctx = ssl.create_default_context(cafile=str(ca))
    c_in, c_out, s_in, s_out = (ssl.MemoryBIO() for _ in range(4))
    client = client_ctx.wrap_bio(c_in, c_out, server_hostname=name)
    server = server_ctx.wrap_bio(s_in, s_out, server_side=True)
    done = {"client": False, "server": False}
    for _ in range(20):
        for side, obj in (("client", client), ("server", server)):
            if not done[side]:
                try:
                    obj.do_handshake()
                    done[side] = True
                except ssl.SSLWantReadError:
                    pass
        s_in.write(c_out.read())
        c_in.write(s_out.read())
        if all(done.values()):
            return
    raise AssertionError("handshake did not finish")


def test_names_are_normalized_and_checked() -> None:
    assert tls.normalize(
        ["10.239.6.35", "Review.Example.", "[::1]", "10.239.6.35"]
    ) == (
        "10.239.6.35",
        "review.example",
        "::1",
        "127.0.0.1",
        "localhost",
    )
    for bad in ("under_score", "a..b", "-lead", "x" * 64):
        with pytest.raises(tls.TlsError):
            tls.normalize([bad])


@needs_openssl
def test_a_client_trusting_the_authority_accepts_only_its_addresses(
    tmp_path: Path,
) -> None:
    files = tls.make(tmp_path / "tls", ["10.239.6.35", "rlinf-review"])
    assert files.new_authority
    assert files.names == ("10.239.6.35", "rlinf-review", "127.0.0.1", "localhost")
    for name in files.names:
        _handshake(files, files.ca_cert, name)
    with pytest.raises(ssl.SSLCertVerificationError):
        _handshake(files, files.ca_cert, "10.0.0.9")
    assert (files.folder.stat().st_mode & 0o777) == 0o700
    for key in (files.key, files.folder / "ca.key"):
        assert (key.stat().st_mode & 0o777) == 0o600


@needs_openssl
def test_the_authority_cannot_vouch_for_other_names(tmp_path: Path) -> None:
    """Even someone holding ``ca.key`` makes no certificate a trusting browser takes
    for another site."""
    files = tls.make(tmp_path / "tls", ["10.239.6.35"])
    work = tmp_path / "forged"
    work.mkdir()
    run = {"cwd": work, "check": True, "capture_output": True}
    subprocess.run(
        ["openssl", "req", "-new", "-nodes", "-newkey", "ec",
         "-pkeyopt", "ec_paramgen_curve:prime256v1", "-keyout", "x.key",
         "-out", "x.csr", "-subj", "/CN=github.com"],
        **run,
    )  # fmt: skip
    (work / "x.cnf").write_text(
        "[s]\nsubjectAltName = DNS:github.com, IP:1.1.1.1\n"
        "extendedKeyUsage = serverAuth\n"
    )
    subprocess.run(
        ["openssl", "x509", "-req", "-in", "x.csr", "-CA", str(files.ca_cert),
         "-CAkey", str(files.folder / "ca.key"), "-set_serial", "7", "-days", "30",
         "-extfile", "x.cnf", "-extensions", "s", "-out", "x.crt"],
        **run,
    )  # fmt: skip
    forged = tls.TlsFiles(
        folder=work,
        ca_cert=files.ca_cert,
        cert=work / "x.crt",
        key=work / "x.key",
        names=("github.com",),
        new_authority=False,
    )
    with pytest.raises(ssl.SSLCertVerificationError):
        _handshake(forged, files.ca_cert, "github.com")


@needs_openssl
def test_a_new_certificate_keeps_the_authority_unless_an_address_is_new(
    tmp_path: Path,
) -> None:
    folder = tmp_path / "tls"
    first = tls.make(folder, ["10.239.6.35", "rlinf-review"])
    ca = first.ca_cert.read_bytes()
    again = tls.make(folder, ["rlinf-review"])
    assert not again.new_authority
    assert first.ca_cert.read_bytes() == ca
    _handshake(again, first.ca_cert, "rlinf-review")
    with pytest.raises(tls.TlsError, match="--new-authority"):
        tls.make(folder, ["10.0.0.9"])

    replaced = tls.make(folder, ["10.0.0.9"], new_authority=True)
    assert replaced.new_authority
    assert replaced.ca_cert.read_bytes() != ca
    assert (folder / "ca.crt.prev").read_bytes() == ca
    assert tls.authority_names(folder) == ("10.0.0.9", "127.0.0.1", "localhost")
    _handshake(replaced, replaced.ca_cert, "10.0.0.9")


@needs_openssl
def test_the_first_start_makes_the_certificate_and_later_ones_keep_it(
    tmp_path: Path, monkeypatch
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setattr(config, "CONFIG_DIR", cfg_dir)
    cfg = HostConfig(base_url="https://10.239.6.35")
    ssl_options, ca = cli._ssl_options(cfg)
    folder = cfg_dir / "tls"
    assert ssl_options == {
        "ssl_certfile": str(folder / "server.crt"),
        "ssl_keyfile": str(folder / "server.key"),
    }
    assert ca == folder / "ca.crt"
    made = tls.ensure(folder, ["10.0.0.9"])
    assert not made.made
    assert "10.239.6.35" in made.names and "10.0.0.9" not in made.names
    _handshake(made, ca, "10.239.6.35")

    # am tls makes it again under the same authority, for the authority's addresses.
    ca_bytes = ca.read_bytes()
    result = CliRunner().invoke(cli.app, ["tls"])
    assert result.exit_code == 0, result.output
    assert "under the same authority" in result.output
    assert ca.read_bytes() == ca_bytes


@needs_openssl
def test_a_certificate_near_its_end_is_renewed_under_the_same_authority(
    tmp_path: Path,
) -> None:
    folder = tmp_path / "tls"
    short = tls.make(folder, ["10.239.6.35"], days=tls.RENEW_DAYS - 1)
    ca = short.ca_cert.read_bytes()
    renewed = tls.ensure(folder, ["10.0.0.9"])
    assert renewed.made and not renewed.new_authority
    assert renewed.names == short.names
    assert short.ca_cert.read_bytes() == ca
    assert not tls.ensure(folder, ["10.0.0.9"]).made


def test_a_certificate_of_the_hosts_own_is_served_as_it_is(tmp_path: Path) -> None:
    cert, key = tmp_path / "server.crt", tmp_path / "server.key"
    own = HostConfig(tls=TlsConfig(cert=str(cert), key=str(key)))
    with pytest.raises(typer.Exit):
        cli._ssl_options(own)
    cert.write_text("x")
    key.write_text("x")
    assert cli._ssl_options(own) == (
        {"ssl_certfile": str(cert), "ssl_keyfile": str(key)},
        None,
    )


def test_https_is_served_on_443_only() -> None:
    assert HostConfig().port == 443 and serves_https(HostConfig().port)
    assert HostConfig().base_url.startswith("https://")
    assert not serves_https(7777)


def test_an_earlier_config_keeps_plain_http_on_7777(tmp_path: Path) -> None:
    """A ``local.yaml`` from before versions (no ``version``) kept the old defaults."""
    (tmp_path / "local.yaml").write_text("operators: [maria]\n")
    old = config.load_host_config(tmp_path)
    assert (old.port, old.base_url) == (7777, "http://127.0.0.1:7777")
    config.save_host_config(old, tmp_path)
    saved = yaml.safe_load((tmp_path / "local.yaml").read_text())
    assert saved["version"] == config.CONFIG_VERSION and saved["port"] == 7777
    assert config.load_host_config(tmp_path).port == 7777

    (tmp_path / "local.yaml").write_text("version: 2\n")
    assert config.load_host_config(tmp_path).port == 443
    assert config.load_host_config(tmp_path / "new").port == 443  # not set up yet
    (tmp_path / "local.yaml").write_text("version: 99\n")
    with pytest.raises(ValueError, match="version 99"):
        config.load_host_config(tmp_path)


def test_a_port_this_user_may_not_open_says_what_to_do(monkeypatch, capsys) -> None:
    import socket

    def refuse(self, address) -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(socket.socket, "bind", refuse)
    with pytest.raises(typer.Exit):
        cli._check_port("0.0.0.0", 443)
    err = capsys.readouterr().err
    assert "net.ipv4.ip_unprivileged_port_start=443" in err
    assert "--port 7777" in err


async def test_the_setup_page_and_dashboard_offer_the_authority(tmp_path: Path) -> None:
    from auto_maintainer.web.setup import create_setup_app

    h = await build_demo(tmp_path / "demo")
    ca = tmp_path / "ca.crt"
    ca.write_text("-----BEGIN CERTIFICATE-----\n")

    async def on_finish(_cfg) -> None:
        return None

    def setup(**kw):
        return create_setup_app(object(), data_dir=tmp_path, on_finish=on_finish, **kw)

    try:
        for app, offered in (
            (create_app(h.service, data_dir=tmp_path, ca_cert=ca), True),
            (setup(ca_cert=ca), True),
            (create_app(h.service, data_dir=tmp_path), False),
            (setup(), False),
        ):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as anon:
                r = await anon.get("/tls/ca.crt")
            if offered:
                assert r.status_code == 200
                assert "x509-ca-cert" in r.headers["content-type"]
                assert r.text.startswith("-----BEGIN CERTIFICATE-----")
            else:
                assert "x509" not in r.headers.get("content-type", "")
    finally:
        await h.close()


def test_status_shows_the_address_people_open() -> None:
    def shown(base_url: str, line: str) -> str:
        return cli._serving_url(HostConfig(base_url=base_url), [line])

    running = "INFO:     Uvicorn running on {} (Press CTRL+C to quit)"
    assert shown("https://10.239.6.35", running.format("https://0.0.0.0:443")) == (
        "https://10.239.6.35"
    )
    assert (
        shown("http://h:7777", running.format("http://0.0.0.0:7777")) == "http://h:7777"
    )
    assert (
        shown("http://h:7777", running.format("http://0.0.0.0:7790")) == "http://h:7790"
    )
    # --port 7777 on a host set up for 443 serves plain HTTP there.
    assert shown("https://h", running.format("http://0.0.0.0:7777")) == "http://h:7777"

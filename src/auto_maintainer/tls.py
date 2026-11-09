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

"""The certificate the dashboard serves HTTPS with when the host brings none.

A host reached by an IP address, or a name only an internal DNS knows, cannot get a
certificate from a public authority. ``make`` instead creates a private authority in
``<config>/tls/`` and a server certificate it signs; people install the authority's
``ca.crt`` in their browser or system once. ``am serve`` calls ``ensure``, which makes
them on the first start and renews the server certificate before it expires; ``am
tls`` makes them again for other addresses.

The authority carries name constraints: it can sign only for the addresses it was
made for, so a browser that trusts it trusts nothing else, even if its key leaks.
Adding an address therefore needs a new authority, and everyone installs the new
``ca.crt``. A new server certificate for the same addresses keeps the authority.

Keys are created in a folder only the owner can open, with ``openssl``.
"""

from __future__ import annotations

import ipaddress
import os
import re
import secrets
import shutil
import socket
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

CA_DAYS = 3650
SERVER_DAYS = 825
"""The longest validity Apple's platforms accept for a server certificate."""
RENEW_DAYS = 30
"""``ensure`` renews a server certificate that expires within this many days."""
CURVE = "prime256v1"
NAMES_FILE = "ca.names"
"""The addresses the authority may sign for, one per line, beside ``ca.crt``."""


_LABEL = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")


class TlsError(Exception):
    """The certificates could not be made; the message says why."""


@dataclass(frozen=True)
class TlsFiles:
    """The certificate files in ``folder``, and what the last call changed."""

    folder: Path
    ca_cert: Path
    cert: Path
    key: Path
    names: tuple[str, ...]
    """The addresses the server certificate is for."""
    new_authority: bool
    """The authority is new: everyone installs its ``ca.crt``."""
    made: bool = True
    """The server certificate is new; ``ensure`` kept the one there otherwise."""


def normalize(names: list[str]) -> tuple[str, ...]:
    """The addresses in a stable form, without duplicates, ``localhost`` and
    ``127.0.0.1`` added so the host itself can open the dashboard.

    Raises ``TlsError`` for an entry that is neither an IP address nor a host name.
    """
    out: list[str] = []
    for raw in [*names, "127.0.0.1", "localhost"]:
        name = raw.strip().lower().rstrip(".")
        if not name:
            continue
        if _is_ip(name.strip("[]")):
            name = str(ipaddress.ip_address(name.strip("[]")))
        elif not all(_LABEL.fullmatch(label) for label in name.split(".")):
            raise TlsError(f"{raw!r} is neither an IP address nor a host name")
        if name not in out:
            out.append(name)
    return tuple(out)


def default_names(base_url: str) -> list[str]:
    """The host in ``base_url`` unless it is this host's loopback address, the host's
    address on its network, and its name."""
    from urllib.parse import urlsplit

    host = urlsplit(base_url).hostname
    names = [host] if host and host not in ("127.0.0.1", "localhost", "::1") else []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("10.255.255.255", 1))
            names.append(str(sock.getsockname()[0]))
    except OSError:
        pass
    names.append(socket.gethostname())
    return names


def ensure(folder: Path, names: list[str]) -> TlsFiles:
    """The certificate in ``folder``. With none, it is made for the authority's
    addresses, or for ``names`` with a new authority; one that expires within
    ``RENEW_DAYS`` is made again for the addresses it had, under the same authority.

    Raises ``TlsError`` as ``make`` does.
    """
    cert, key = folder / "server.crt", folder / "server.key"
    if not (cert.is_file() and key.is_file()):
        return make(folder, list(authority_names(folder) or names))
    had = served_names(cert)
    if authority_names(folder) is None or _expires_within(cert, RENEW_DAYS):
        return make(folder, list(had) or names)
    return TlsFiles(
        folder=folder,
        ca_cert=folder / "ca.crt",
        cert=cert,
        key=key,
        names=had,
        new_authority=False,
        made=False,
    )


def served_names(cert: Path) -> tuple[str, ...]:
    """The addresses in the certificate's subject alternative names."""
    out = subprocess.run(
        ["openssl", "x509", "-in", str(cert), "-noout", "-ext", "subjectAltName"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    names = []
    for line in out.splitlines()[1:]:
        for entry in line.split(","):
            kind, _, value = entry.strip().partition(":")
            if kind in ("DNS", "IP Address") and value:
                names.append(value)
    try:
        return normalize(names)
    except TlsError:
        return ()


def _expires_within(cert: Path, days: int) -> bool:
    return (
        subprocess.run(
            ["openssl", "x509", "-in", str(cert), "-noout",
             "-checkend", str(days * 86400)],
            capture_output=True,
            check=False,
        ).returncode
        != 0
    )  # fmt: skip


def make(
    folder: Path,
    names: list[str],
    *,
    new_authority: bool = False,
    days: int = SERVER_DAYS,
) -> TlsFiles:
    """Write ``server.crt`` and ``server.key`` for ``names`` into ``folder``, signed by
    the authority there, which is created first when missing or ``new_authority``.

    Raises ``TlsError`` when ``openssl`` is missing or fails, or when the existing
    authority may not sign for one of ``names`` and ``new_authority`` is false.
    """
    if shutil.which("openssl") is None:
        raise TlsError("openssl is not installed on this host")
    wanted = normalize(names)
    folder.mkdir(parents=True, exist_ok=True)
    folder.chmod(0o700)
    ca_cert, ca_key = folder / "ca.crt", folder / "ca.key"
    allowed = authority_names(folder)
    fresh = new_authority or allowed is None
    if not fresh and (missing := [n for n in wanted if n not in allowed]):
        raise TlsError(
            f"the authority in {folder} signs only for {', '.join(allowed)}; for "
            f"{', '.join(missing)}, make a new one with --new-authority, and everyone "
            "installs its ca.crt again"
        )
    with tempfile.TemporaryDirectory(dir=folder) as tmp:
        work = Path(tmp)
        if fresh:
            _openssl(
                work,
                "req", "-x509", "-new", "-nodes",
                "-newkey", "ec", "-pkeyopt", f"ec_paramgen_curve:{CURVE}",
                "-keyout", "ca.key", "-out", "ca.crt", "-days", str(CA_DAYS),
                "-subj", f"/O=auto-maintainer/CN=auto-maintainer on {wanted[0]}",
                "-config", _write(work / "ca.cnf", _ca_config(wanted)),
                "-extensions", "ca",
            )  # fmt: skip
        else:
            shutil.copy(ca_cert, work / "ca.crt")
            shutil.copy(ca_key, work / "ca.key")
        _openssl(
            work,
            "req", "-new", "-nodes",
            "-newkey", "ec", "-pkeyopt", f"ec_paramgen_curve:{CURVE}",
            "-keyout", "server.key", "-out", "server.csr",
            "-subj", f"/CN={wanted[0]}",
            "-config", _write(work / "req.cnf", _REQ),
        )  # fmt: skip
        _openssl(
            work,
            "x509", "-req", "-in", "server.csr", "-CA", "ca.crt", "-CAkey", "ca.key",
            "-set_serial", "0x" + secrets.token_hex(16), "-days", str(days),
            "-sha256", "-extfile", _write(work / "ext.cnf", _server_ext(wanted)),
            "-extensions", "server", "-out", "server.crt",
        )  # fmt: skip
        keep = ["server.crt", "server.key"]
        if fresh:
            _write(work / NAMES_FILE, "\n".join(wanted) + "\n")
            keep += ["ca.crt", "ca.key", NAMES_FILE]
            for old in (ca_cert, ca_key):
                if old.exists():
                    os.replace(old, old.with_name(old.name + ".prev"))
        for name in keep:
            (work / name).chmod(0o600 if name.endswith(".key") else 0o644)
            os.replace(work / name, folder / name)
    return TlsFiles(
        folder=folder,
        ca_cert=ca_cert,
        cert=folder / "server.crt",
        key=folder / "server.key",
        names=wanted,
        new_authority=fresh,
    )


def authority_names(folder: Path) -> tuple[str, ...] | None:
    """The names the authority in ``folder`` may sign for, or ``None`` without one."""
    path = folder / NAMES_FILE
    if not ((folder / "ca.crt").is_file() and (folder / "ca.key").is_file()):
        return None
    if not path.is_file():
        return ()
    return tuple(ln for ln in path.read_text().split() if ln)


def _san(names: tuple[str, ...]) -> str:
    return ",".join(f"IP:{n}" if _is_ip(n) else f"DNS:{n}" for n in names)


def _is_ip(name: str) -> bool:
    try:
        ipaddress.ip_address(name)
    except ValueError:
        return False
    return True


def _ca_config(names: tuple[str, ...]) -> str:
    permitted = []
    for n in names:
        if _is_ip(n):
            ip = ipaddress.ip_address(n)
            mask = ipaddress.ip_network(f"{ip}/{ip.max_prefixlen}").netmask
            permitted.append(f"permitted;IP:{ip}/{mask}")
        else:
            permitted.append(f"permitted;DNS:{n}")
    return (
        _REQ + "[ca]\n"
        "basicConstraints = critical, CA:TRUE, pathlen:0\n"
        "keyUsage = critical, keyCertSign, cRLSign\n"
        "subjectKeyIdentifier = hash\n"
        f"nameConstraints = critical, {', '.join(permitted)}\n"
    )


def _server_ext(names: tuple[str, ...]) -> str:
    return (
        "[server]\n"
        "basicConstraints = critical, CA:FALSE\n"
        "keyUsage = critical, digitalSignature\n"
        "extendedKeyUsage = serverAuth\n"
        "subjectKeyIdentifier = hash\n"
        "authorityKeyIdentifier = keyid\n"
        f"subjectAltName = {_san(names)}\n"
    )


_REQ = "[req]\ndistinguished_name = dn\nprompt = no\n[dn]\n"


def _write(path: Path, text: str) -> str:
    path.write_text(text, encoding="utf-8")
    return path.name


def _openssl(cwd: Path, *args: str) -> None:
    result = subprocess.run(
        ["openssl", *args], cwd=cwd, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise TlsError(f"openssl {args[0]} failed: {result.stderr.strip()[-500:]}")

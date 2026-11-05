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

"""Auth providers, Actions CI, and webhooks."""

from __future__ import annotations

import hashlib
import hmac

import httpx
import jwt
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from auto_maintainer.github import (
    AuthError,
    GitHubActionsCi,
    GitHubAppTokenProvider,
    GitHubClient,
    GitHubReader,
    StaticTokenProvider,
    hint,
    verify_signature,
)
from auto_maintainer.github.auth import READONLY_PERMISSIONS
from auto_maintainer.github.ci import MAX_LOG_BYTES

API = "https://api.github.com"


@pytest.fixture
def rsa_key():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return pem, key.public_key()


async def test_static_provider_prefers_explicit_then_env(monkeypatch):
    assert await StaticTokenProvider(token="x").token("a/b") == "x"
    monkeypatch.setenv("AM_TEST_TOKEN", "from-env")
    provider = StaticTokenProvider("AM_TEST_TOKEN")
    assert await provider.token(None) == "from-env"
    assert await provider.readonly_token("a/b") is None


async def test_static_provider_without_any_token(monkeypatch):
    monkeypatch.delenv("AM_NO_TOKEN", raising=False)
    with pytest.raises(AuthError):
        await StaticTokenProvider("AM_NO_TOKEN", gh_binary="definitely-not-gh").token(
            None
        )


async def test_app_provider_caches_installation_tokens(rsa_key):
    pem, public_key = rsa_key
    now = [1_000_000.0]
    with respx.mock(base_url=API) as router:
        lookup = router.get("/repos/acme/widget/installation").respond(
            200, json={"id": 7}
        )
        mint = router.post("/app/installations/7/access_tokens").mock(
            side_effect=lambda req: httpx.Response(
                201,
                json={
                    "token": f"inst-{len(mint.calls)}",
                    "expires_at": "1970-01-12T14:46:40Z",
                },
            )
        )
        provider = GitHubAppTokenProvider(42, pem, clock=lambda: now[0])
        first = await provider.token("acme/widget")
        assert first == await provider.token("acme/widget")
        assert lookup.call_count == 1 and mint.call_count == 1
        auth = mint.calls[0].request.headers["Authorization"].removeprefix("Bearer ")
        claims = jwt.decode(
            auth,
            public_key,
            algorithms=["RS256"],
            options={"verify_exp": False, "verify_iat": False},
        )
        assert claims["iss"] == "42" and claims["exp"] - claims["iat"] <= 600
        now[0] = 1_000_000.0 + 3600 - 200  # inside the five-minute refresh margin
        assert await provider.token("acme/widget") != first
        ro = await provider.readonly_token("acme/widget")
        body = mint.calls[-1].request.content
        assert ro.startswith("inst-") and b'"repositories":["widget"]' in body
        for perm, level in READONLY_PERMISSIONS.items():
            assert f'"{perm}":"{level}"'.encode() in body
        await provider.aclose()


async def test_app_provider_needs_a_repo(rsa_key):
    provider = GitHubAppTokenProvider(1, rsa_key[0])
    with pytest.raises(AuthError):
        await provider.token(None)
    await provider.aclose()


async def test_app_mode_service_login(rsa_key):
    with respx.mock(base_url=API) as router:
        app = router.get("/app").respond(200, json={"slug": "maint-helper"})
        provider = GitHubAppTokenProvider(1, rsa_key[0])
        client = GitHubClient(provider)
        assert await GitHubReader(client).service_login() == "maint-helper[bot]"
        assert app.calls[0].request.headers["Authorization"].startswith("Bearer ey")
        await client.aclose()
        await provider.aclose()


async def test_job_log_follows_redirect_without_credentials(client, router):
    router.get("/repos/acme/widget/actions/jobs/5/logs").respond(
        302, headers={"Location": "https://blob.example/log5"}
    )
    blob = router.get("https://blob.example/log5").respond(
        200, content=b"x" * (MAX_LOG_BYTES + 10) + b"END"
    )
    router.get("/repos/acme/widget/actions/jobs/6/logs").respond(410)
    ci = GitHubActionsCi(client)
    log = await ci.get_job_log("acme/widget", 5)
    assert log.endswith("END") and len(log) == MAX_LOG_BYTES
    assert "Authorization" not in blob.calls[0].request.headers
    assert await ci.get_job_log("acme/widget", 6) is None
    await ci.aclose()


async def test_rerun_and_runners(client, router):
    rerun = router.post(
        "/repos/acme/widget/actions/runs/555/rerun-failed-jobs"
    ).respond(201)
    router.get("/repos/acme/widget/actions/runners").respond(
        200,
        json={
            "runners": [
                {
                    "name": "gpu-1",
                    "status": "online",
                    "busy": True,
                    "labels": [{"name": "self-hosted"}, {"name": "gpu"}],
                }
            ]
        },
    )
    router.get("/repos/acme/other/actions/runners").respond(403)
    ci = GitHubActionsCi(client)
    await ci.rerun_failed_jobs("acme/widget", 555)
    assert rerun.called
    runners = await ci.list_runners("acme/widget")
    assert (
        runners[0].name == "gpu-1"
        and runners[0].busy
        and runners[0].labels == ["self-hosted", "gpu"]
    )
    assert await ci.list_runners("acme/other") == []
    await ci.aclose()


def test_verify_signature():
    secret, body = b"s3cret", b'{"zen": "hi"}'
    good = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
    assert verify_signature(secret, body, good)
    assert not verify_signature(secret, body + b" ", good)
    assert not verify_signature(secret, body, None)
    assert not verify_signature(secret, body, "sha1=abc")
    assert not verify_signature(b"", body, good)


def test_hint():
    repo = {"repository": {"full_name": "acme/widget"}}
    assert hint("pull_request", {**repo, "pull_request": {"number": 3}}) == (
        "acme/widget",
        3,
    )
    assert hint("pull_request_review", {**repo, "pull_request": {"number": 4}}) == (
        "acme/widget",
        4,
    )
    assert hint("issue_comment", {**repo, "issue": {"number": 5}}) == ("acme/widget", 5)
    assert hint(
        "check_run", {**repo, "check_run": {"pull_requests": [{"number": 6}]}}
    ) == ("acme/widget", 6)
    assert hint("workflow_run", {**repo, "workflow_run": {"pull_requests": []}}) == (
        "acme/widget",
        None,
    )
    assert hint("push", repo) == ("acme/widget", None)
    assert hint("ping", {"zen": "x"}) is None

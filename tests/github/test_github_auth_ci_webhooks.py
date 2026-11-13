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

import pytest

from auto_maintainer.forges.github import (
    AuthError,
    GitHubActionsCi,
    GitHubReader,
    StaticTokenProvider,
    hint,
    verify_signature,
)
from auto_maintainer.forges.github.ci import MAX_LOG_BYTES

API = "https://api.github.com"


async def test_static_provider_prefers_explicit_then_env(monkeypatch):
    assert await StaticTokenProvider(token="x").token("a/b") == "x"
    monkeypatch.setenv("AM_TEST_TOKEN", "from-env")
    provider = StaticTokenProvider("AM_TEST_TOKEN")
    assert await provider.token(None) == "from-env"
    assert await provider.readonly_token("a/b") is None


async def test_the_service_login_is_the_cli_accounts(client, router):
    router.get("/user").respond(200, json={"login": "maint-bot"})
    assert await GitHubReader(client).service_login() == "maint-bot"


async def test_static_provider_without_any_token(monkeypatch):
    monkeypatch.delenv("AM_NO_TOKEN", raising=False)
    with pytest.raises(AuthError):
        await StaticTokenProvider("AM_NO_TOKEN", gh_binary="definitely-not-gh").token(
            None
        )


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


async def test_a_read_the_docs_builds_log_comes_from_its_api(client, router):
    build = router.get("https://readthedocs.org/api/v2/build/35842899/").respond(
        200,
        json={
            "success": False,
            "error": None,
            "commands": [
                {"command": "git clone ...", "exit_code": 0, "output": "cloned"},
                {
                    "command": "git checkout --force 0562492",
                    "exit_code": 128,
                    "output": "fatal: unable to read tree (0562492)",
                },
            ],
        },
    )
    ci = GitHubActionsCi(client)
    log = await ci.get_status_log(
        "acme/widget", "https://app.readthedocs.org/projects/rlinf/builds/35842899/"
    )
    assert log is not None and "failed" in log
    assert "unable to read tree" in log and "cloned" not in log
    assert "Authorization" not in build.calls[0].request.headers
    # Any other link, whoever set it, is never fetched.
    assert await ci.get_status_log("acme/widget", "http://169.254.169.254/x") is None
    assert (
        await ci.get_status_log(
            "acme/widget", "https://evil.example/projects/rlinf/builds/1/"
        )
        is None
    )
    await ci.aclose()


async def test_a_read_the_docs_build_of_a_stale_pull_ref_says_so(client, router):
    head = "0562492444af5479b5325bc4f0c14a1b527fd7c3"
    old = "5d322a1cb1c699ea99c7ca6850754c84284e64c8"

    def build(n: int) -> dict:
        return {
            "success": False,
            "commit": head,
            "version_type": "external",
            "version_slug": str(n),
            "commands": [{"command": f"git checkout --force {head}", "exit_code": 128}],
        }

    router.get("https://readthedocs.org/api/v2/build/1/").respond(200, json=build(7))
    router.get("https://readthedocs.org/api/v2/build/2/").respond(200, json=build(8))
    router.get("/repos/acme/widget/git/ref/pull/7/head").respond(
        200, json={"object": {"sha": old}}
    )
    router.get("/repos/acme/widget/git/ref/pull/8/head").respond(
        200, json={"object": {"sha": head}}
    )
    ci = GitHubActionsCi(client)
    stale = await ci.get_status_log(
        "acme/widget", "https://app.readthedocs.org/projects/rlinf/builds/1/"
    )
    assert stale is not None
    assert "refs/pull/7/head is still at 5d322a1cb1" in stale
    assert "no change to them fixes it" in stale
    # A ref at the head: the checkout failed for another reason, and the log says
    # only what Read the Docs ran.
    current = await ci.get_status_log(
        "acme/widget", "https://app.readthedocs.org/projects/rlinf/builds/2/"
    )
    assert current is not None and "refs/pull/" not in current
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


async def test_a_refusal_for_saml_single_sign_on_says_where_to_authorize(
    client, router
):
    from auto_maintainer.forges.github.client import GitHubError

    router.put("/repos/acme/widget/pulls/7/merge").respond(
        403,
        json={"message": "Resource protected by organization SAML enforcement."},
        headers={
            "x-github-sso": "required; url=https://github.com/orgs/acme/sso?authorization_request=x"
        },
    )
    with pytest.raises(GitHubError) as e:
        await client.put(
            "/repos/acme/widget/pulls/7/merge", repo="acme/widget", json={}
        )
    assert "SAML single sign-on" in str(e.value)
    assert "https://github.com/orgs/acme/sso?authorization_request=x" in str(e.value)

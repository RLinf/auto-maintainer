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

from __future__ import annotations

from pathlib import Path

from auto_maintainer.agents.sandbox import build_env, scrub
from auto_maintainer.domain.agents import AgentTask, TaskKind


def _task(**env: str) -> AgentTask:
    return AgentTask(kind=TaskKind.REVIEW, prompt="x", workdir=Path("/tmp"), env=env)


PARENT = {
    "PATH": "/usr/bin",
    "HOME": "/home/am",
    "LC_ALL": "C.UTF-8",
    "HTTPS_PROXY": "http://proxy:8080",
    "AWS_SECRET_ACCESS_KEY": "secret",
    "OPENAI_API_KEY": "sk-test",
    "GITHUB_TOKEN": "ghp_parentparentparentparent",
    "GH_TOKEN": "gho_parentparentparentparent",
    "AM_SESSION_SECRET": "s3",
}


def test_only_allowlisted_names_pass_through() -> None:
    env = build_env(_task(), base=PARENT)
    assert env == {
        "PATH": "/usr/bin",
        "HOME": "/home/am",
        "LC_ALL": "C.UTF-8",
        "HTTPS_PROXY": "http://proxy:8080",
    }


def test_extra_names_and_patterns_pass_through() -> None:
    env = build_env(_task(), allow_extra=["OPENAI_*"], base=PARENT)
    assert env["OPENAI_API_KEY"] == "sk-test"
    assert "AWS_SECRET_ACCESS_KEY" not in env


def test_forge_tokens_never_inherit_even_when_allowed() -> None:
    env = build_env(_task(), allow_extra=["GITHUB_TOKEN", "GH_*"], base=PARENT)
    assert "GITHUB_TOKEN" not in env and "GH_TOKEN" not in env


def test_task_env_can_supply_a_scoped_token() -> None:
    env = build_env(_task(GH_TOKEN="ghs_scopedscopedscopedscoped"), base=PARENT)
    assert env["GH_TOKEN"] == "ghs_scopedscopedscopedscoped"


def test_scrub_redacts_tokens_keys_and_url_credentials() -> None:
    text = (
        "token ghp_abcdefghijklmnopqrstuvwxyz0123 and github_pat_11ABCDEFG0123456789_abcdefghij "
        "key sk-ant-api03-abcdefghijklmnopqrstuv header Bearer abc.def.ghi-jklmnop "
        "remote https://user:ghp_secretsecretsecretsecret@github.com/o/r.git "
        "Authorization: token gho_abcdefghijklmnopqrstuvwxyz"
    )
    out = scrub(text)
    for secret in (
        "ghp_abcdef",
        "github_pat_11",
        "sk-ant-api03",
        "abc.def.ghi",
        "user:ghp",
        "gho_abcdef",
    ):
        assert secret not in out
    assert "https://[REDACTED]@github.com/o/r.git" in out
    assert out.count("[REDACTED]") >= 5


def test_scrub_leaves_ordinary_text_alone() -> None:
    text = "Fixed the token parser in src/auth.py; see https://github.com/o/r/pull/12."
    assert scrub(text) == text

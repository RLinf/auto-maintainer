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

"""Each agent's tokens and what its CLI says is left of its limits (``/usage``)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from auto_maintainer.agents.adapters.antigravity import agy_quota
from auto_maintainer.agents.adapters.claude_code import claude_quota
from auto_maintainer.domain.agents import AgentQuota, QuotaBucket
from auto_maintainer.domain.permissions import Permission
from auto_maintainer.domain.records import AgentRunRecord
from auto_maintainer.testing.demo import build_demo
from auto_maintainer.web.app import create_app

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)

AGY_USAGE = {
    "status": "SUCCESS",
    "num_turns": 0,
    "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
    "command": {
        "name": "usage",
        "data": {
            "description": "Within each group, models share a weekly limit and a 5-hour limit.",
            "groups": [
                {
                    "name": "Gemini Models",
                    "buckets": [
                        {
                            "id": "gemini-weekly",
                            "name": "Weekly Limit Remaining",
                            "window": "weekly",
                            "remaining_fraction": 0.9080712795257568,
                            "reset_time": "2026-11-16T02:36:29Z",
                        },
                        {
                            "id": "gemini-5h",
                            "name": "Five Hour Limit Remaining",
                            "window": "5h",
                            "remaining_fraction": 0.7011399865150452,
                            "reset_time": "2026-11-09T12:36:29Z",
                        },
                    ],
                },
                {
                    "name": "Claude and GPT models",
                    "buckets": [
                        {
                            "id": "3p-weekly",
                            "name": "Weekly Limit Remaining",
                            "remaining_fraction": 1,
                            "reset_time": "2026-11-16T11:57:28Z",
                        }
                    ],
                },
            ],
        },
    },
}

CLAUDE_USAGE = """You are currently using your subscription to power your Claude Code usage

Current session: 7% used · resets Nov 10, 12:10am (Asia/Shanghai)
Current week (all models): 39% used · resets Nov 13, 8am (Asia/Shanghai)

What's contributing to your usage?
Last 24h · 1488 requests · 1 session
  100%  Give simple subagents a cheaper model
"""


def test_agy_usage_becomes_its_limits() -> None:
    quota = agy_quota("antigravity", AGY_USAGE, NOW)
    assert [(b.group, b.name) for b in quota.buckets] == [
        ("Gemini Models", "Weekly Limit Remaining"),
        ("Gemini Models", "Five Hour Limit Remaining"),
        ("Claude and GPT models", "Weekly Limit Remaining"),
    ]
    five_hours = quota.buckets[1]
    assert round(five_hours.remaining or 0, 2) == 0.70
    assert five_hours.resets_at == datetime(2026, 11, 9, 12, 36, 29, tzinfo=UTC)
    assert quota.buckets[2].remaining == 1.0
    assert not agy_quota("antigravity", {"status": "SUCCESS"}, NOW).buckets


def test_claude_usage_text_becomes_its_limits() -> None:
    quota = claude_quota("claude-code", CLAUDE_USAGE, NOW)
    assert [(b.name, round(b.remaining or 0, 2)) for b in quota.buckets] == [
        ("Current session", 0.93),
        ("Current week (all models)", 0.61),
    ]
    assert quota.buckets[0].resets == "Nov 10, 12:10am (Asia/Shanghai)"
    assert not claude_quota("claude-code", "Nothing to show.", NOW).buckets


class _Runtime:
    """A CLI whose /usage answers; counts how often it was asked."""

    def __init__(self) -> None:
        self.asked = 0

    async def quota(self) -> AgentQuota:
        self.asked += 1
        return AgentQuota(
            runtime="antigravity",
            read_at=NOW,
            buckets=[QuotaBucket(group="Gemini Models", name="weekly", remaining=0.5)],
        )


async def test_usage_counts_tokens_per_agent_and_reads_each_cli_sparingly(
    tmp_path: Path,
) -> None:
    h = await build_demo(tmp_path)
    try:
        cli = _Runtime()
        h.runner.runtimes = {"antigravity": cli, "codex": _Runtime()}
        h.runner.ready = lambda name: name == "antigravity"
        now = h.service.clock.now()
        for i, (repo, tokens) in enumerate(
            [("acme/widget", 1000), ("acme/widget", 500), ("acme/secret", 9000)]
        ):
            await h.store.add_agent_run(
                AgentRunRecord(
                    id=f"run_{i}",
                    repo=repo,
                    runtime="antigravity",
                    task_kind="review",
                    ok=True,
                    input_tokens=tokens,
                    output_tokens=10,
                    created_at=now - timedelta(hours=1),
                )
            )
        h.forge.set_permission("acme/widget", "walt", Permission.WRITE)
        app = create_app(h.service, data_dir=tmp_path, allow_dev_login=True)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as walt:
            await walt.get("/auth/dev?login=walt")
            first = (await walt.get("/api/agents/usage?days=7")).json()
            again = (await walt.get("/api/agents/usage?days=7&fresh=true")).json()
        [agy] = first["agents"]  # codex is not installed here, so it is left out
        assert not first["everything"]
        assert (agy["runs"], agy["input_tokens"]) == (2, 1500), "only repos walt sees"
        assert agy["quota"]["buckets"][0]["remaining"] == 0.5
        assert again["agents"] == first["agents"]
        assert cli.asked == 1, "a fresh reading within half a minute is the kept one"
    finally:
        await h.close()

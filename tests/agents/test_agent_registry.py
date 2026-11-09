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

import pytest
from pydantic import ValidationError

from auto_maintainer.agents.adapters.claude_code import ClaudeCodeRuntime
from auto_maintainer.agents.adapters.dsh import DshRuntime
from auto_maintainer.agents.adapters.generic import GenericRuntime
from auto_maintainer.agents.registry import (
    DEFAULT_RUNTIMES,
    RuntimeConfig,
    budgets_of,
    build_runtime,
    build_runtimes,
    merge_configs,
)
from auto_maintainer.domain.ports import AgentRuntime


def test_defaults_cover_every_cli_and_keep_placeholders_off() -> None:
    assert set(DEFAULT_RUNTIMES) == {
        "claude-code",
        "codex",
        "antigravity",
        "opencode",
        "gemini",
        "pi",
        "dsh",
    }
    assert not DEFAULT_RUNTIMES["pi"].enabled and DEFAULT_RUNTIMES["dsh"].enabled
    runtimes = build_runtimes(merge_configs())
    assert set(runtimes) == {
        "claude-code",
        "codex",
        "antigravity",
        "opencode",
        "gemini",
        "dsh",
    }
    assert isinstance(runtimes["dsh"], DshRuntime)
    assert all(isinstance(r, AgentRuntime) for r in runtimes.values())


def test_overrides_merge_and_enable_generic_runtimes(tmp_path: Path) -> None:
    configs = merge_configs(
        {
            "pi": {"enabled": True, "command": ["pi", "go", "{prompt}"]},
            "claude-code": {
                "model": "opus",
                "options": {"setting_sources": ""},
                "budget": {"daily_usd": 20},
            },
        }
    )
    runtimes = build_runtimes(configs, runs_dir=tmp_path)
    pi = runtimes["pi"]
    assert isinstance(pi, GenericRuntime) and pi.command == ["pi", "go", "{prompt}"]
    claude = runtimes["claude-code"]
    assert (
        isinstance(claude, ClaudeCodeRuntime)
        and claude.model == "opus"
        and claude.setting_sources == ""
    )
    assert claude.runs_dir == tmp_path
    assert budgets_of(configs)["claude-code"].daily_usd == 20


def test_bad_config_is_rejected() -> None:
    with pytest.raises(ValidationError):
        RuntimeConfig.model_validate({"adapter": "nonesuch"})
    with pytest.raises(ValidationError):
        RuntimeConfig.model_validate({"adapter": "codex", "colour": "blue"})
    with pytest.raises(ValueError, match="no command"):
        build_runtime("x", RuntimeConfig(adapter="generic"))

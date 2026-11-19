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

"""The settings of each agent CLI that the service passes for every run (design
5.7): a sandbox, network access, web search, compaction, attribution, and the like.

The service's owner sets them for the service's accounts (``RuntimeConfig.options``,
the Settings page), and each person for their own account (``PersonalAgent.options``,
My agents), where an option they leave unset takes the service's. An adapter applies
only what is set, and only to that run: a command-line flag, settings passed inline,
an environment variable, or a file in the run's own folder. Nothing is written to a
CLI's own configuration, so nobody's choice changes another person's runs or the
CLI's defaults, and an option nobody set leaves the CLI's default in force.

Which permission mode a task runs in (auto, restricted, bypass) stays the service's
to decide (``effective_permissions``); no option here grants what a mode withholds.
Each default below was read from the CLI version the adapter names.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class OptionChoice(BaseModel):
    model_config = ConfigDict(frozen=True)

    value: str
    label: str


class AgentOption(BaseModel):
    """One setting of an agent CLI, as the dashboard offers it."""

    model_config = ConfigDict(frozen=True)

    key: str
    label: str
    help: str
    kind: Literal["bool", "choice", "number", "text", "list"]
    default: str
    """What applies when nobody sets it, in words: ``on``, ``request-review``, ``300``."""
    by_service: bool = False
    """Whether ``default`` is the service's own choice; the CLI's is ``cli_default``."""
    cli_default: str | None = None
    choices: tuple[OptionChoice, ...] = ()
    minimum: float | None = None
    maximum: float | None = None


def _c(*pairs: tuple[str, str]) -> tuple[OptionChoice, ...]:
    return tuple(OptionChoice(value=v, label=label) for v, label in pairs)


ANTIGRAVITY_TOOL_POLICIES = (
    "always-proceed",
    "request-review",
    "strict",
    "proceed-in-sandbox",
)
"""Antigravity's tool execution policies (its settings key ``autoExecutionPolicy``)."""

CLAUDE_CODE = (
    AgentOption(
        key="sandbox",
        label="Sandbox for commands",
        help="Runs its shell commands in Claude Code's own sandbox (bubblewrap on "
        "Linux): writes only in the checkout, network only to allowed domains.",
        kind="bool",
        default="off",
    ),
    AgentOption(
        key="sandbox_auto_allow",
        label="Sandboxed commands run without asking",
        help="With the sandbox on, a command that runs inside it needs no approval.",
        kind="bool",
        default="off",
    ),
    AgentOption(
        key="sandbox_weaker_nested",
        label="Sandbox inside a container",
        help="Lets the sandbox start inside an unprivileged container, such as the "
        "one each agent run gets here, with weaker isolation.",
        kind="bool",
        default="off",
    ),
    AgentOption(
        key="sandbox_allowed_domains",
        label="Domains sandboxed commands may reach",
        help="Hosts such as pypi.org or *.github.com, one per line.",
        kind="list",
        default="none",
    ),
    AgentOption(
        key="thinking",
        label="Extended thinking",
        help="Whether the model thinks before it answers.",
        kind="bool",
        default="on",
    ),
    AgentOption(
        key="auto_compact",
        label="Compact the conversation when it fills up",
        help="Summarizes earlier turns once the context window is nearly full.",
        kind="bool",
        default="on",
    ),
    AgentOption(
        key="attribution",
        label="Credit Claude in commits and PRs",
        help="A Co-Authored-By trailer on its commits and a line in PRs it writes.",
        kind="bool",
        default="on",
    ),
    AgentOption(
        key="fallback_model",
        label="Fallback model",
        help="The model to switch to when the main one is busy or unavailable.",
        kind="text",
        default="none",
    ),
    AgentOption(
        key="web_fetch",
        label="Fetch web pages",
        help="The WebFetch tool; web search is not affected.",
        kind="bool",
        default="on",
    ),
    AgentOption(
        key="bash_timeout_seconds",
        label="Command time limit (seconds)",
        help="How long a shell command may run when the model sets no limit.",
        kind="number",
        default="120",
        minimum=1,
        maximum=86_400,
    ),
    AgentOption(
        key="bash_max_timeout_seconds",
        label="Longest command time limit (seconds)",
        help="The most the model may give a shell command.",
        kind="number",
        default="600",
        minimum=1,
        maximum=86_400,
    ),
    AgentOption(
        key="nonessential_traffic",
        label="Telemetry, error reports, and update checks",
        help="Off sends none of them.",
        kind="bool",
        default="on",
    ),
)
"""Claude Code 2.1.328: ``--settings`` inline JSON (``sandbox.*``,
``alwaysThinkingEnabled``, ``autoCompactEnabled``, ``attribution``), ``--fallback-model``,
and its environment (``CLAUDE_CODE_DISABLE_WEB_FETCH``, ``BASH_DEFAULT_TIMEOUT_MS``,
``BASH_MAX_TIMEOUT_MS``, ``CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC``). Runs load no
settings file (``--setting-sources``), so these defaults are the CLI's built-in ones."""

CODEX = (
    AgentOption(
        key="network_access",
        label="Network in the sandbox",
        help="Whether commands in Codex's workspace-write sandbox, which tasks that "
        "may change their checkout run in, may reach the network.",
        kind="bool",
        default="off",
    ),
    AgentOption(
        key="web_search",
        label="Web search",
        help="Cached searches an index OpenAI keeps; live searches the web now.",
        kind="choice",
        default="cached",
        choices=_c(("disabled", "Off"), ("cached", "Cached"), ("live", "Live")),
    ),
    AgentOption(
        key="reasoning_summary",
        label="Reasoning summaries",
        help="How much of its reasoning the model summarizes in the live view.",
        kind="choice",
        default="auto",
        choices=_c(
            ("auto", "Auto"),
            ("concise", "Concise"),
            ("detailed", "Detailed"),
            ("none", "None"),
        ),
    ),
    AgentOption(
        key="verbosity",
        label="Answer length",
        help="How long the model's answers run, on models that take it.",
        kind="choice",
        default="the model's own",
        choices=_c(("low", "Low"), ("medium", "Medium"), ("high", "High")),
    ),
    AgentOption(
        key="approvals_reviewer",
        label="Who decides what it asks",
        help="In auto mode, Codex's reviewer agent decides the escalations it asks "
        "about, or each goes to a reviewer on the dashboard.",
        kind="choice",
        default="auto_review",
        by_service=True,
        cli_default="user",
        choices=_c(("auto_review", "Codex's reviewer agent"), ("user", "A reviewer")),
    ),
)
"""Codex 0.170 app server: ``-c`` overrides (``sandbox_workspace_write.network_access``,
``web_search``, ``model_reasoning_summary``, ``model_verbosity``) and the thread's
``approvalsReviewer``."""

ANTIGRAVITY = (
    AgentOption(
        key="sandbox",
        label="Terminal sandbox",
        help="Runs its terminal commands with restrictions (--sandbox). On hosts "
        "where agy's sandbox server cannot start, every command then fails.",
        kind="choice",
        default="read_only",
        by_service=True,
        cli_default="off",
        choices=_c(
            ("read_only", "For read-only tasks"),
            ("always", "For every task"),
            ("off", "Off"),
        ),
    ),
    AgentOption(
        key="tool_policy",
        label="Tool execution",
        help="Whether its terminal commands run without asking. With Always proceed, "
        "auto mode runs them without bypassing permissions.",
        kind="choice",
        default="request-review",
        choices=_c(
            ("always-proceed", "Always proceed"),
            ("proceed-in-sandbox", "Proceed in its sandbox"),
            ("request-review", "Ask for review"),
            ("strict", "Strict"),
        ),
    ),
)
"""Antigravity 1.5.12 ACP: ``--sandbox`` and ``--settings`` (``autoExecutionPolicy``,
for that session only, never saved). Its default policy is what it logged for every
service run with none set on the host (request-review)."""

GEMINI = (
    AgentOption(
        key="tool_sandboxing",
        label="Sandbox each tool",
        help="Isolates each tool call instead of the whole CLI.",
        kind="bool",
        default="off",
    ),
    AgentOption(
        key="sandbox_network",
        label="Network in the sandbox",
        help="Whether sandboxed tools may reach the network.",
        kind="bool",
        default="off",
    ),
    AgentOption(
        key="checkpointing",
        label="Checkpoints",
        help="Saves a snapshot before each file change, for restoring later.",
        kind="bool",
        default="off",
    ),
    AgentOption(
        key="max_session_turns",
        label="Most turns in a session",
        help="Turns of the model, its tools, and the prompt; -1 is no limit.",
        kind="number",
        default="-1",
        minimum=-1,
        maximum=10_000,
    ),
    AgentOption(
        key="compression_threshold",
        label="Compress the conversation at",
        help="The share of the context window, from 0 to 1, at which earlier turns "
        "are compressed.",
        kind="number",
        default="0.5",
        minimum=0,
        maximum=1,
    ),
    AgentOption(
        key="respect_gitignore",
        label="Respect .gitignore when searching",
        help="Leaves files that .gitignore names out of its searches.",
        kind="bool",
        default="on",
    ),
    AgentOption(
        key="shell_inactivity_timeout",
        label="Stop a silent command after (seconds)",
        help="How long a shell command may print nothing before it is stopped.",
        kind="number",
        default="300",
        minimum=1,
        maximum=86_400,
    ),
    AgentOption(
        key="loop_detection",
        label="Stop when it loops",
        help="Detects the model repeating itself and ends the turn.",
        kind="bool",
        default="on",
    ),
    AgentOption(
        key="usage_statistics",
        label="Usage statistics",
        help="Whether it sends Google usage statistics.",
        kind="bool",
        default="on",
    ),
    AgentOption(
        key="auto_update",
        label="Automatic updates",
        help="Whether it updates itself; the service installs the versions it runs.",
        kind="bool",
        default="on",
    ),
)
"""Gemini CLI 0.68.0: a settings file of the run's own folder, named by
``GEMINI_CLI_SYSTEM_SETTINGS_PATH`` (``security.toolSandboxing``,
``tools.sandboxNetworkAccess``, ``general.checkpointing.enabled``,
``model.maxSessionTurns``, ``model.compressionThreshold``,
``context.fileFiltering.respectGitIgnore``, ``tools.shell.inactivityTimeout``,
``model.disableLoopDetection``, ``privacy.usageStatisticsEnabled``,
``general.enableAutoUpdate``). Its full-process sandbox needs docker or podman, which
agent containers lack, so it is not offered."""

OPENCODE = (
    AgentOption(
        key="share",
        label="Session sharing",
        help="Auto publishes every session at a public link; Off turns sharing off.",
        kind="choice",
        default="manual",
        choices=_c(
            ("manual", "On request"), ("auto", "Every session"), ("disabled", "Off")
        ),
    ),
    AgentOption(
        key="snapshot",
        label="Snapshots for undo",
        help="Records the checkout's state so changes can be undone.",
        kind="bool",
        default="on",
    ),
    AgentOption(
        key="auto_compact",
        label="Compact the conversation when it fills up",
        help="Summarizes earlier turns once the context window is full.",
        kind="bool",
        default="on",
    ),
    AgentOption(
        key="prune",
        label="Prune old tool output",
        help="Drops the output of old tool calls from the conversation.",
        kind="bool",
        default="off",
    ),
    AgentOption(
        key="update_check",
        label="Update checks",
        help="Whether it checks for new versions; it never installs one itself.",
        kind="bool",
        default="on",
    ),
)
"""OpenCode 1.19.6: its inline config (``OPENCODE_CONFIG_CONTENT``: ``share``,
``snapshot``, ``compaction.auto``, ``compaction.prune``, ``autoupdate``)."""

AGENT_OPTIONS: dict[str, tuple[AgentOption, ...]] = {
    "claude-code": CLAUDE_CODE,
    "codex": CODEX,
    "antigravity": ANTIGRAVITY,
    "gemini": GEMINI,
    "opencode": OPENCODE,
    "pi": (),
    "dsh": (),
    "generic": (),
}
"""Each adapter's options. pi and dsh have none beyond the model and reasoning level:
pi's tools are the service's to choose, and dsh's ACP server offers no others."""


def options_of(adapter: str) -> tuple[AgentOption, ...]:
    return AGENT_OPTIONS.get(adapter, ())


def option_problems(adapter: str, options: Mapping[str, Any]) -> list[str]:
    """What is wrong with ``options`` for ``adapter``'s CLI: keys it has no option
    for, and values of the wrong kind, outside a choice, or out of range."""
    known = {o.key: o for o in options_of(adapter)}
    problems: list[str] = []
    for key, value in options.items():
        option = known.get(key)
        if option is None:
            problems.append(f"{adapter} has no option {key!r}")
            continue
        if value is None:
            continue
        if option.kind == "bool" and not isinstance(value, bool):
            problems.append(f"{key} is true or false")
        elif option.kind == "choice" and value not in {c.value for c in option.choices}:
            problems.append(
                f"{key} is one of {', '.join(c.value for c in option.choices)}"
            )
        elif option.kind == "number":
            if isinstance(value, bool) or not isinstance(value, int | float):
                problems.append(f"{key} is a number")
            elif (option.minimum is not None and value < option.minimum) or (
                option.maximum is not None and value > option.maximum
            ):
                problems.append(
                    f"{key} is from {option.minimum:g} to {option.maximum:g}"
                )
        elif option.kind == "text" and (not isinstance(value, str) or len(value) > 200):
            problems.append(f"{key} is a short text")
        elif option.kind == "list" and (
            not isinstance(value, list)
            or len(value) > 100
            or not all(isinstance(x, str) and 0 < len(x) <= 253 for x in value)
        ):
            problems.append(f"{key} is a list of short texts")
    return problems


def normalized(adapter: str, options: Mapping[str, Any]) -> dict[str, Any]:
    """``options`` with values an older settings file wrote in an earlier form:
    Antigravity's ``sandbox`` was true (read-only tasks) or false."""
    out = dict(options)
    if adapter == "antigravity" and isinstance(out.get("sandbox"), bool):
        out["sandbox"] = "read_only" if out["sandbox"] else "off"
    return out


def set_options(adapter: str, options: Mapping[str, Any]) -> dict[str, Any]:
    """``options`` without unset values, keeping only ``adapter``'s own keys."""
    known = {o.key for o in options_of(adapter)}
    return {k: v for k, v in options.items() if k in known and v not in (None, "", [])}

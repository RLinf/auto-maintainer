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

"""The exec transport: one subprocess per task, streaming its output.

``ExecRuntime`` owns process lifecycle, timeouts, transcripts, and failure
classification. An adapter subclass supplies three things: the command line
(``build_invocation``), how one line of output becomes events (``parse_line``),
and its ``capabilities``.

``parse_line`` may return ``final`` events with ``result=None``. These are partial
updates to the run's result, not events for the caller: their ``data`` keys
``text``, ``structured``, ``session``, ``usage``, ``is_error``, ``error``, and
``error_kind`` are merged into the result, and they are not yielded.

An adapter whose CLI talks back (Claude Code's stream-json control protocol, the
Codex app-server, ACP) returns a ``Conversation`` from ``conversation``. Standard
input then stays open: the runtime writes the conversation's opening lines, feeds it
every output line, writes what it answers, and resolves the questions it raises,
first through ``auto_answer`` and then through the reviewers' question desk. The
task's timeout stops counting while a question waits for a reviewer.

A conversation with ``takes_messages`` also carries what reviewers say while the agent
works (design 5.13): for a task with ``messages`` set, the runtime opens the job's box
in the router's ``Inbox`` and hands each message to ``Conversation.tell`` as it comes.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shlex
import shutil
import signal
import tempfile
import time
from collections.abc import AsyncIterator, Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from ..domain.agents import (
    AgentAnswer,
    AgentChoices,
    AgentEvent,
    AgentEventType,
    AgentLogin,
    AgentQuestion,
    AgentQuota,
    AgentResult,
    AgentTask,
    Capabilities,
    QuestionKind,
    RuntimeHealth,
    Usage,
    denial,
)
from ..domain.ports import AgentQuestions
from ..domain.settings import ProviderBinding
from .approver import POSTING_ANSWER, posting_request
from .login import LoginProcess
from .sandbox import build_env, scrub
from .structured import NO_JSON, extract_json

log = logging.getLogger(__name__)

DEFAULT_RUNS_DIR = Path(tempfile.gettempdir()) / "auto-maintainer-runs"
SEARCH_DIRS: tuple[str, ...] = (
    "~/.npm-global/bin",
    "~/.local/bin",
    "/usr/local/bin",
    "~/.bun/bin",
    "~/.cargo/bin",
)
"""Where CLIs are looked for when they are not on ``PATH``; service managers often start
with a minimal ``PATH``."""

BACKGROUND_ANSWER = (
    "This task runs in the background and only reads the code, so nobody is asked and "
    "the request is denied. Work from what you can read without it."
)

MAX_ARG_PROMPT_BYTES = 100_000
"""Prompts longer than this are passed through a file; Linux limits one argument to 128 KiB."""

LAB_CLI = Path(__file__).with_name("lab_cli.py")
"""The ``lab`` command a run with a reproduction session gets."""

PROMPT_CONTINUES = (
    "\n\n[This task is {size} KB, more than this agent takes in one message, so it "
    "is cut here. The whole task, this part included, is in the file {path}: read the "
    "rest of it before you start, and answer in the format it asks for.]"
)

_STDERR_KEEP = 1_000_000
_LINE_LIMIT = 64 * 1024 * 1024

_AUTH_RE = re.compile(
    r"not logged in|log ?in required|please (run )?(/)?login|unauthori[sz]ed|\b401\b|authenticat|"
    r"invalid api key|api key (is )?(missing|invalid)|credentials|oauth|ProjectIdRequired|GOOGLE_CLOUD_PROJECT|"
    r"permission denied \(publickey\)|token (has )?expired",
    re.IGNORECASE,
)
_QUOTA_RE = re.compile(
    r"rate.?limit|\b429\b|quota|usage limit|insufficient.?credit|credit balance|limit reached|"
    r"too many requests|billing|exceeded your",
    re.IGNORECASE,
)
_CRASH_RE = re.compile(
    r"Traceback \(most recent call last\)|panic:|Segmentation fault|FATAL ERROR|Unhandled|core dumped",
    re.IGNORECASE,
)


def classify_failure(exit_code: int | None, stderr: str, error_text: str = "") -> str:
    """Classify a failed run as ``auth``, ``quota``, ``crash``, or ``other``.

    Args:
        exit_code: The process exit code; negative for a signal, ``None`` if unknown.
        stderr: The process's standard error.
        error_text: Error messages the CLI reported in its own output.
    """
    text = f"{error_text}\n{stderr}"
    if _AUTH_RE.search(text):
        return "auth"
    if _QUOTA_RE.search(text):
        return "quota"
    if (exit_code is not None and exit_code < 0) or _CRASH_RE.search(text):
        return "crash"
    return "other"


def resolve_binary(binary: str, search_dirs: Iterable[str] = SEARCH_DIRS) -> str | None:
    """Return an executable path for ``binary``: itself if it is a path, else from ``PATH``
    or ``search_dirs``."""
    if os.sep in binary:
        return binary if os.access(binary, os.X_OK) else None
    found = shutil.which(binary)
    if found:
        return found
    for directory in search_dirs:
        candidate = Path(directory).expanduser() / binary
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


@dataclass(frozen=True)
class RunFiles:
    """Files of one run, in its own directory under the runs directory."""

    dir: Path
    prompt: Path
    schema: Path
    last_message: Path
    transcript: Path
    stderr: Path
    result: Path

    @classmethod
    def create(cls, runs_dir: Path, run_id: str) -> RunFiles:
        directory = runs_dir / run_id
        directory.mkdir(parents=True, exist_ok=True)
        return cls(
            dir=directory,
            prompt=directory / "prompt.md",
            schema=directory / "schema.json",
            last_message=directory / "last_message.txt",
            transcript=directory / "transcript.jsonl",
            stderr=directory / "stderr.txt",
            result=directory / "result.json",
        )


@dataclass
class Invocation:
    """A command line and what to feed its standard input (``None`` closes it)."""

    argv: list[str]
    stdin: str | None = None


@dataclass
class RunState:
    """What a run has produced so far; adapters update it through partial ``final`` events."""

    messages: list[str] = field(default_factory=list)
    text: str | None = None
    structured: dict[str, Any] | None = None
    session: str | None = None
    usage: Usage = field(default_factory=Usage)
    usage_final: bool = False
    is_error: bool = False
    error: str | None = None
    error_kind: str | None = None

    def merge(self, data: dict[str, Any]) -> None:
        if data.get("text") is not None:
            self.text = str(data["text"])
        if isinstance(data.get("structured"), dict):
            self.structured = data["structured"]
        if data.get("session"):
            self.session = str(data["session"])
        if isinstance(data.get("usage"), dict):
            self.usage = Usage.model_validate(data["usage"])
            self.usage_final = True
        if data.get("is_error"):
            self.is_error = True
        if data.get("error"):
            self.error = str(data["error"])
        if data.get("error_kind"):
            self.error_kind = str(data["error_kind"])

    def add_usage(self, data: dict[str, Any]) -> None:
        if self.usage_final:
            return
        self.usage = Usage(
            input_tokens=self.usage.input_tokens + int(data.get("input_tokens") or 0),
            output_tokens=self.usage.output_tokens
            + int(data.get("output_tokens") or 0),
            cost_usd=(
                (self.usage.cost_usd or 0.0) + float(data["cost_usd"])
                if data.get("cost_usd") is not None
                else self.usage.cost_usd
            ),
        )


@dataclass
class Ask:
    """A question the CLI is blocked on; ``token`` is the conversation's own reference."""

    question: AgentQuestion
    token: Any = None


@dataclass
class Step:
    """What one output line led to."""

    events: list[AgentEvent] = field(default_factory=list)
    send: list[str] = field(default_factory=list)
    """Lines to write to the CLI's standard input."""
    asks: list[Ask] = field(default_factory=list)
    done: bool = False
    """The exchange is over: standard input is closed so the CLI exits."""


@dataclass(frozen=True)
class Told:
    """A reviewer's message for an agent at work: ``text`` is the whole prompt that
    brings it, written by the core; ``by`` is the reviewer's login."""

    by: str
    text: str


class Inbox:
    """Messages for agents at work, by job: a reviewer talks to the agent of a review
    while it runs. Only runs that listen open a box; a message for a job with none is
    not kept."""

    def __init__(self) -> None:
        self._boxes: dict[str, list[asyncio.Queue[Told]]] = {}

    @contextlib.contextmanager
    def open(self, job_id: str) -> Iterator[asyncio.Queue[Told]]:
        """A box for one run of ``job_id``, open while the context lasts."""
        box: asyncio.Queue[Told] = asyncio.Queue()
        self._boxes.setdefault(job_id, []).append(box)
        try:
            yield box
        finally:
            boxes = self._boxes.get(job_id, [])
            if box in boxes:
                boxes.remove(box)
            if not boxes:
                self._boxes.pop(job_id, None)

    def listening(self, job_id: str) -> bool:
        return bool(self._boxes.get(job_id))

    def put(self, job_id: str, told: Told) -> bool:
        """Deliver ``told`` to every run of ``job_id`` that listens; whether one did."""
        boxes = self._boxes.get(job_id, [])
        for box in boxes:
            box.put_nowait(told)
        return bool(boxes)


class Conversation:
    """A two-way exchange with one running CLI; adapters subclass it."""

    takes_messages: ClassVar[bool] = False
    """Whether ``tell`` brings a reviewer's message to the agent while it works."""

    def opening(self) -> list[str]:
        """Lines to write as soon as the process starts."""
        return []

    def feed(self, line: str) -> Step:
        """Handle one line of standard output."""
        raise NotImplementedError

    def answer(self, ask: Ask, answer: AgentAnswer) -> list[str]:
        """Lines that deliver ``answer`` to the CLI."""
        return []

    def tell(self, text: str) -> list[str]:
        """Lines that bring ``text``, a reviewer's message, to the agent now; none when
        the conversation holds it until the agent's turn ends. The agent then takes
        it into account and finishes with its result as before."""
        return []


def _gh_through_proxy(config_dir: Path) -> None:
    """``gh`` settings that send its requests through the GitHub read proxy."""
    from .container import SOCKET

    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.yml").write_text(
        f"http_unix_socket: {SOCKET}\nprompt: disabled\n", encoding="utf-8"
    )


def isolation_env(run_dir: Path) -> dict[str, str]:
    """Keep ``gh`` and ``git`` from finding the host's stored credentials.

    This stops an agent from pushing or commenting through credentials it did not
    ask for. It does not stop a process that reads token files directly; confine
    that with ``command_prefix`` (design 5.7).
    """
    gh = run_dir / "gh-config"
    gh.mkdir(parents=True, exist_ok=True)
    gitconfig = run_dir / "gitconfig"
    if not gitconfig.exists():
        gitconfig.write_text("[safe]\n\tdirectory = *\n", encoding="utf-8")
    return {
        "GH_CONFIG_DIR": str(gh),
        "GIT_CONFIG_GLOBAL": str(gitconfig),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "",
        "SSH_ASKPASS": "",
    }


class ExecRuntime:
    """Base class for CLIs that run one subprocess per task.

    Args:
        name: The runtime name used in routes; defaults to ``default_name``.
        binary: Executable name or path; defaults to ``default_binary``.
        runs_dir: Where per-run directories go.
        env_allow: Extra environment names or patterns the process may inherit.
        model: Default model when the task does not name one.
        effort: Default reasoning level when the task's route names none.
        extra_args: Arguments appended to every command line.
        search_dirs: Directories searched for ``binary`` when it is not on ``PATH``.
        kill_grace_seconds: Time between SIGTERM and SIGKILL on timeout.
    """

    default_name: ClassVar[str] = "exec"
    default_binary: ClassVar[str] = ""
    default_env_allow: ClassVar[tuple[str, ...]] = ()
    line_mode: ClassVar[bool] = True
    """Parse output line by line; otherwise the whole output is parsed at the end."""
    max_prompt_bytes: ClassVar[int | None] = None
    """The longest prompt the CLI takes whole in one message, when it cuts longer
    ones silently; ``fitted_prompt`` keeps a longer prompt's start and points at the
    whole of it in the run folder."""
    known_choices: ClassVar[AgentChoices | None] = None
    """The models and reasoning levels this CLI accepts, for CLIs that cannot list
    them; adapters set it."""

    def __init__(
        self,
        name: str | None = None,
        *,
        binary: str | None = None,
        runs_dir: Path | str | None = None,
        env_allow: Iterable[str] = (),
        model: str | None = None,
        effort: str | None = None,
        extra_args: Iterable[str] = (),
        search_dirs: Iterable[str] = SEARCH_DIRS,
        kill_grace_seconds: float = 5.0,
        allow_bypass: bool = False,
        command_prefix: Iterable[str] = (),
        questions: AgentQuestions | None = None,
        provider: ProviderBinding | None = None,
        permissions: str | None = None,
        approve_commands: Iterable[str] = (),
    ) -> None:
        self.name = name or self.default_name
        self.approve_commands = [shlex.split(c) for c in approve_commands]
        """Commands the service approves for this agent without asking, as words
        (``RuntimeConfig.approve_commands``)."""
        self.binary = binary or self.default_binary
        self.runs_dir = Path(runs_dir) if runs_dir else DEFAULT_RUNS_DIR
        self.env_allow = [*self.default_env_allow, *env_allow]
        self.provider = provider
        """The model endpoint the CLI uses instead of its own sign-in, if any."""
        self.model = model or (
            provider.models[0] if provider and provider.models else None
        )
        self.effort = effort
        self.extra_args = list(extra_args)
        self.search_dirs = tuple(search_dirs)
        self.kill_grace_seconds = kill_grace_seconds
        self.allow_bypass = allow_bypass
        self.own_permissions = permissions
        """The mode the service settings give this agent for every task, or ``None``
        to follow each route (``RuntimeConfig.permissions``)."""
        self.command_prefix = list(command_prefix)
        self.questions = questions
        """Where questions go that ``auto_answer`` does not settle; set by the router."""
        self.inbox: Inbox | None = None
        """Where reviewers' messages for running tasks wait; set by the router."""

    # -- adapter hooks -----------------------------------------------------------

    def capabilities(self) -> Capabilities:
        """What this CLI supports; adapters override."""
        return Capabilities()

    def bypass_env(self) -> dict[str, str]:
        """Environment a CLI needs to accept its bypass flag; adapters override."""
        return {}

    def task_env(self, task: AgentTask) -> dict[str, str]:
        """Environment for one task, such as inline CLI config; adapters override."""
        return {}

    def provider_env(self) -> dict[str, str | None]:
        """Environment that points the CLI at ``self.provider``; ``None`` removes a name
        the process would otherwise inherit. Adapters that support providers override."""
        return {}

    def build_invocation(
        self, task: AgentTask, files: RunFiles, binary: str
    ) -> Invocation:
        """Return the command line for ``task``; adapters override."""
        raise NotImplementedError

    def parse_line(self, line: str) -> list[AgentEvent]:
        """Turn one line of standard output into events. The default treats it as text."""
        return (
            [AgentEvent(type=AgentEventType.MESSAGE, text=line)] if line.strip() else []
        )

    def parse_document(self, text: str) -> list[AgentEvent]:
        """Parse the whole output, for runtimes with ``line_mode = False``."""
        events: list[AgentEvent] = []
        for line in text.splitlines():
            events.extend(self.parse_line(line))
        return events

    def finish(
        self, task: AgentTask, files: RunFiles, state: RunState, stderr: str
    ) -> None:
        """Last chance to fill ``state`` from files the CLI wrote; adapters override."""

    def conversation(self, task: AgentTask, files: RunFiles) -> Conversation | None:
        """A two-way exchange for CLIs that talk back; ``None`` for one-shot CLIs."""
        return None

    def auto_answer(
        self, task: AgentTask, question: AgentQuestion
    ) -> AgentAnswer | None:
        """Settle a question without a reviewer, or return ``None``; adapters override."""
        return None

    container_sandbox: Any = None
    """A ``ContainerSandbox`` to run every task in a container of its own, where the
    agent runs in bypass mode; set by the composition root once its image is ready."""

    home_paths: ClassVar[tuple[str, ...]] = ()
    """The CLI's own login and settings, relative to the home folder, that its
    container mounts (nothing else of the home folder is there)."""

    login_args: ClassVar[tuple[str, ...]] = ()
    """The arguments that start the CLI's own sign-in on a host without a browser
    (``agents/login.py``); empty for a CLI that has none and runs on a model API."""
    login_marker: ClassVar[str] = ""
    """The file, relative to the home folder, the CLI writes when it signed in."""

    account: str | None = None
    """The person whose own agent account this runtime runs on; ``None`` for the
    service's. Set on a copy made for that person's tasks (``AgentRouter``)."""
    account_home: Path | None = None
    """That person's home folder for the CLI: what the container mounts as
    ``home_paths`` instead of the service's own."""

    def start_login(self, home: Path, state: AgentLogin) -> LoginProcess:
        """Start the CLI's sign-in for the account whose home folder is ``home``.

        Raises:
            RuntimeError: The CLI is missing or has no sign-in.
        """
        binary = resolve_binary(self.binary, self.search_dirs)
        if binary is None or not self.login_args:
            raise RuntimeError(f"{self.name} cannot sign in here")
        home.mkdir(parents=True, exist_ok=True)
        env = build_env(
            AgentTask(kind="probe", prompt="", workdir=home), self.env_allow
        )
        env["HOME"] = str(home)
        marker = home / self.login_marker if self.login_marker else None
        process = LoginProcess([binary, *self.login_args], env, state, marker)
        process.start(home)
        return process

    async def signed_in(self, home: Path | None = None) -> bool | None:
        """Whether the CLI is signed in for the account whose home folder is ``home``
        (the service's own by default); ``None`` when it cannot tell."""
        return None

    async def resolve(self, task: AgentTask, question: AgentQuestion) -> AgentAnswer:
        """Answer a question the CLI is blocked on.

        A request to post to GitHub, or a question about whether to, is turned down
        at once: agents never post. The adapter's automatic rules come next. Then a
        read-only task nobody asked for (triage, scoring, a scheduled review), a
        ``restricted`` run, or a runtime without a question desk is denied, since
        nobody waits on it; otherwise a reviewer decides, preferably the one who asked
        for the work.
        """
        question = question.model_copy(
            update={
                "runtime": self.name,
                "run_id": task.id,
                "task_kind": str(task.kind),
                "repo": task.repo,
                "pr": task.pr,
                "job_id": task.job_id,
                "asked_for": task.requested_by,
                "untrusted_input": task.untrusted_input,
            }
        )
        if posting_request(question):
            return denial(POSTING_ANSWER)
        # What the agent asks a person (an input question) is never answered by a
        # rule or by bypass mode; only who may run what is.
        automatic = (
            None
            if question.kind == QuestionKind.INPUT
            else self.auto_answer(task, question)
        )
        if automatic is not None:
            return automatic
        if task.requested_by is None and task.mode == "read_only":
            return denial(BACKGROUND_ANSWER)
        if task.permissions == "restricted":
            return denial(
                "This run is restricted, so requests are denied without asking."
            )
        if self.questions is None:
            return denial(
                "No reviewer can be asked from this run, so the request was denied."
            )
        try:
            return await self.questions.ask(question, task.question_timeout_seconds)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return denial(f"The question could not reach a reviewer ({exc!r}).")

    # -- helpers for adapters ----------------------------------------------------------

    def write_schema(
        self, task: AgentTask, files: RunFiles, schema: dict[str, Any] | None = None
    ) -> Path | None:
        """Write the task's output schema (or ``schema``) to the run directory."""
        data = schema if schema is not None else task.output_schema
        if data is None:
            return None
        files.schema.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return files.schema

    def fitted_prompt(self, text: str, files: RunFiles) -> str:
        """``text`` as one message the CLI takes whole: itself, or, past
        ``max_prompt_bytes``, its start up to a line break and where the whole of it
        is. Prompts put what the agent must act on first, so the start carries it."""
        raw = text.encode("utf-8")
        limit = self.max_prompt_bytes
        if limit is None or len(raw) <= limit:
            return text
        whole = files.dir / "task.md"
        whole.write_text(text, encoding="utf-8")
        head = raw[: max(0, limit - 1_000)].decode("utf-8", "ignore")
        cut = head.rfind("\n")
        head = head[:cut] if cut > len(head) // 2 else head
        return head + PROMPT_CONTINUES.format(size=len(raw) // 1024, path=whole)

    def argv_prompt(self, task: AgentTask, files: RunFiles) -> str:
        """The prompt to put on the command line, pointing at the prompt file when it is too long."""
        if len(task.prompt.encode("utf-8")) <= MAX_ARG_PROMPT_BYTES:
            return task.prompt
        return (
            f"Your full task is in the file {files.prompt}. Read that whole file first, "
            "then do exactly what it says."
        )

    # -- AgentRuntime ------------------------------------------------------------------

    async def choices(self) -> AgentChoices | None:
        """The models and reasoning levels this CLI offers; ``None`` when unknown."""
        return self.known_choices

    def cached_choices(self) -> AgentChoices | None:
        """What ``choices`` last found, or the adapter's known list, without asking
        the CLI again."""
        cached = getattr(self, "_choices", None)
        return cached[1] if cached is not None else self.known_choices

    async def quota(self) -> AgentQuota | None:
        """What the CLI says is left of its account's limits, as its own ``/usage``
        shows; ``None`` for a CLI that cannot report them without running a task."""
        return None

    async def cli_output(
        self,
        *args: str,
        timeout: float = 30,
        home: Path | None = None,
        check: bool = True,
        stderr: bool = False,
    ) -> str | None:
        """Run the CLI on this host with ``args`` and the agent's own environment, in
        an empty folder, and return what it printed; ``None`` when it is missing or
        fails (or, with ``check`` off, only when it is missing). ``home`` is the
        account's home folder, this runtime's account's by default; ``stderr`` keeps
        what the CLI writes there too. For quick questions that run no task, such
        as ``/usage``."""
        binary = resolve_binary(self.binary, self.search_dirs)
        if binary is None:
            return None
        with tempfile.TemporaryDirectory(prefix="am-cli-") as folder:
            env = build_env(
                AgentTask(kind="probe", prompt="", workdir=Path(folder)),
                self.env_allow,
            )
            if (home or self.account_home) is not None:
                env["HOME"] = str(home or self.account_home)
            proc: asyncio.subprocess.Process | None = None
            try:
                proc = await asyncio.create_subprocess_exec(
                    binary,
                    *args,
                    cwd=folder,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT
                    if stderr
                    else asyncio.subprocess.DEVNULL,
                    env=env,
                    start_new_session=True,
                )
                out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except (TimeoutError, OSError) as exc:
                if proc is not None:
                    _kill_group(proc.pid, signal.SIGKILL)
                    with contextlib.suppress(Exception):
                        await proc.wait()
                log.warning("%s %s failed: %r", self.name, " ".join(args), exc)
                return None
        if check and proc.returncode != 0:
            return None
        return out.decode("utf-8", "replace")

    def installed(self) -> bool:
        """Whether the CLI's binary is on this host; the router skips the runtime
        when it is not."""
        return resolve_binary(self.binary, self.search_dirs) is not None

    async def probe(self) -> RuntimeHealth:
        """Run ``<binary> --version``. Authentication is not checked."""
        binary = resolve_binary(self.binary, self.search_dirs)
        if binary is None:
            return RuntimeHealth(
                name=self.name, available=False, detail=f"{self.binary!r} not found"
            )
        start = time.monotonic()
        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                binary,
                "--version",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=build_env(
                    AgentTask(kind="probe", prompt="", workdir=Path.cwd()),
                    self.env_allow,
                ),
                start_new_session=True,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        except (TimeoutError, OSError) as exc:
            if proc is not None:
                _kill_group(proc.pid, signal.SIGKILL)
            return RuntimeHealth(
                name=self.name, available=False, detail=f"--version failed: {exc!r}"
            )
        text = out.decode("utf-8", "replace").strip()
        version_match = re.search(r"\d+\.\d+(?:\.\d+)?(?:[-+.\w]*)?", text)
        latency = round(time.monotonic() - start, 3)
        choices: AgentChoices | None = None
        if proc.returncode == 0:
            try:
                choices = await self.choices()
            except Exception as exc:  # the CLI still runs; its options stay unknown
                log.warning("could not read %s's models: %r", self.name, exc)
        return RuntimeHealth(
            name=self.name,
            available=proc.returncode == 0,
            version=version_match.group(0)
            if version_match
            else (text.splitlines()[0] if text else None),
            latency_seconds=latency,
            detail="" if proc.returncode == 0 else text[-500:],
            choices=choices,
        )

    async def run(self, task: AgentTask) -> AsyncIterator[AgentEvent]:
        """Run ``task`` and stream its events; the last event carries the result."""
        start = time.monotonic()
        files = RunFiles.create(self.runs_dir, task.id)
        files.prompt.write_text(task.prompt, encoding="utf-8")
        state = RunState()
        binary = resolve_binary(self.binary, self.search_dirs)
        if binary is None:
            yield self._final(
                task,
                files,
                state,
                start,
                ok=False,
                error=f"{self.binary!r} not found",
                kind="crash",
            )
            return
        if not task.effort and self.effort:
            task = task.model_copy(update={"effort": self.effort})
        if task.permissions == "bypass" and not (
            (
                self.container_sandbox is not None
                or self.allow_bypass
                or self.own_permissions == "bypass"
            )
            and self.capabilities().bypass
        ):
            task = task.model_copy(update={"permissions": "restricted"})
        invocation = self.build_invocation(task, files, binary)
        talk = self.conversation(task, files)
        if self.command_prefix and self.container_sandbox is None:
            fill = {"workdir": str(task.workdir), "run_dir": str(files.dir)}
            prefix = [part.format(**fill) for part in self.command_prefix]
            invocation = Invocation(
                argv=[*prefix, *invocation.argv], stdin=invocation.stdin
            )
        env = build_env(task, self.env_allow)
        env.update(isolation_env(files.dir))
        env.update(self.task_env(task))
        for name, value in self.provider_env().items():
            if value is None:
                env.pop(name, None)
            else:
                env[name] = value
        if task.permissions == "bypass":
            env.update(self.bypass_env())
        if task.lab_session:
            # The agent's door to its reproduction session (``labs/sessions.py``).
            bin_dir = files.dir / "bin"
            bin_dir.mkdir(exist_ok=True)
            lab = bin_dir / "lab"
            lab.write_text(LAB_CLI.read_text(encoding="utf-8"), encoding="utf-8")
            lab.chmod(0o755)
            env["AM_LAB_SESSION"] = task.lab_session
            env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
        else:
            env.pop("AM_LAB_SESSION", None)
            env.pop("AM_LAB_SOCKET", None)
        container: str | None = None
        if self.container_sandbox is not None:
            if self.container_sandbox.github_socket is not None:
                _gh_through_proxy(files.dir / "gh-config")
            prepared = self.container_sandbox.prepare(
                task.id,
                Path(task.workdir),
                files.dir,
                binary,
                invocation.argv,
                env,
                rw=task.extra_dirs,
                ro=task.skill_dirs,
                home_paths=self.home_paths,
                home_source=self.account_home,
            )
            invocation = Invocation(argv=prepared.argv, stdin=invocation.stdin)
            env = prepared.env
            container = prepared.name
        try:
            proc = await asyncio.create_subprocess_exec(
                *invocation.argv,
                stdin=asyncio.subprocess.PIPE
                if invocation.stdin is not None or talk is not None
                else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(task.workdir),
                env=env,
                start_new_session=True,
                limit=_LINE_LIMIT,
            )
        except OSError as exc:
            yield self._final(
                task,
                files,
                state,
                start,
                ok=False,
                error=f"could not start {binary}: {exc}",
                kind="crash",
            )
            return

        stderr_buf = bytearray()
        stderr_task = asyncio.create_task(_drain(proc.stderr, stderr_buf))
        stdin_task = (
            asyncio.create_task(_feed(proc.stdin, invocation.stdin))
            if invocation.stdin is not None and talk is None
            else None
        )
        if talk is not None:
            await _write_lines(proc.stdin, talk.opening())
        loop = asyncio.get_running_loop()
        deadline = loop.time() + task.timeout_seconds
        timed_out = False
        document: list[str] = []
        listens = (
            talk is not None
            and talk.takes_messages
            and task.messages
            and task.job_id is not None
            and self.inbox is not None
        )
        box_context = (
            self.inbox.open(task.job_id)  # type: ignore[union-attr,arg-type]
            if listens
            else contextlib.nullcontext(None)
        )
        reading: asyncio.Future[bytes] | None = None
        waiting: asyncio.Future[Told] | None = None
        try:
            with (
                files.transcript.open("w", encoding="utf-8") as transcript,
                box_context as box,
            ):
                while True:
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        timed_out = True
                        break
                    if reading is None:
                        reading = asyncio.ensure_future(proc.stdout.readline())  # type: ignore[union-attr]
                    if box is not None and waiting is None:
                        waiting = asyncio.ensure_future(box.get())
                    ready, _ = await asyncio.wait(
                        [f for f in (reading, waiting) if f is not None],
                        timeout=remaining,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if not ready:
                        timed_out = True
                        break
                    if waiting is not None and waiting in ready:
                        told = waiting.result()
                        waiting = None
                        assert talk is not None
                        transcript.write(
                            json.dumps({"told_by": told.by}, ensure_ascii=False) + "\n"
                        )
                        yield AgentEvent(
                            type=AgentEventType.MESSAGE,
                            text=f"[a message from {told.by} reached the agent]",
                            data={"told_by": told.by},
                        )
                        await _write_lines(proc.stdin, talk.tell(told.text))
                        if reading not in ready:
                            continue
                    raw = reading.result()
                    reading = None
                    if not raw:
                        break
                    line = raw.decode("utf-8", "replace").rstrip("\n")
                    transcript.write(_transcript_line(line) + "\n")
                    if talk is not None:
                        step = talk.feed(line)
                        for event in self._absorb(step.events, state):
                            yield event
                        await _write_lines(proc.stdin, step.send)
                        stopped = False
                        for ask in step.asks:
                            asked_at = loop.time()
                            answer = await self._resolve_while_alive(
                                task, ask.question, proc
                            )
                            deadline += loop.time() - asked_at
                            if answer is None or proc.returncode is not None:
                                # The CLI ended while a person was being asked (its
                                # container stopped, say): nothing would read the
                                # answer, and its question is withdrawn.
                                state.is_error = True
                                state.error = (
                                    "the agent stopped while it waited for an answer "
                                    f"to: {ask.question.title[:200]}"
                                )
                                state.error_kind = "crash"
                                stopped = True
                                break
                            transcript.write(
                                scrub(
                                    json.dumps(
                                        {
                                            "question": ask.question.model_dump(
                                                mode="json",
                                                include={
                                                    "kind",
                                                    "title",
                                                    "detail",
                                                    "tool",
                                                },
                                            ),
                                            "answer": answer.model_dump(mode="json"),
                                        },
                                        ensure_ascii=False,
                                    )
                                )
                                + "\n"
                            )
                            yield AgentEvent(
                                type=AgentEventType.PERMISSION_REQUEST,
                                text=ask.question.title,
                                data={
                                    "allow": answer.allow,
                                    "by": answer.by,
                                    "automatic": answer.automatic,
                                },
                            )
                            await _write_lines(proc.stdin, talk.answer(ask, answer))
                        if stopped:
                            break
                        if step.done:
                            _close_stdin(proc)
                        continue
                    if not self.line_mode:
                        document.append(line)
                        continue
                    for event in self._absorb(self.parse_line(line), state):
                        yield event
                if not self.line_mode:
                    for event in self._absorb(
                        self.parse_document("\n".join(document)), state
                    ):
                        yield event
            if talk is not None:
                _close_stdin(proc)
            if not timed_out:
                try:
                    await asyncio.wait_for(
                        proc.wait(), max(0.1, deadline - loop.time())
                    )
                except TimeoutError:
                    timed_out = True
        finally:
            for pending in (reading, waiting):
                if pending is not None and not pending.done():
                    pending.cancel()
                    with contextlib.suppress(BaseException):
                        await pending
            if proc.returncode is None:
                await self._terminate(proc)
            if container is not None and self.container_sandbox is not None:
                # Stopping ``docker run`` leaves its container running.
                with contextlib.suppress(Exception):
                    await self.container_sandbox.stop(container)
            if stdin_task is not None:
                stdin_task.cancel()
                with contextlib.suppress(BaseException):
                    await stdin_task
            with contextlib.suppress(Exception):
                await asyncio.wait_for(stderr_task, 5)

        stderr = stderr_buf.decode("utf-8", "replace")
        files.stderr.write_text(scrub(stderr), encoding="utf-8")
        self.finish(task, files, state, stderr)

        if timed_out:
            yield self._final(
                task,
                files,
                state,
                start,
                ok=False,
                error=f"timed out after {task.timeout_seconds} s",
                kind="timeout",
            )
            return
        exit_code = proc.returncode
        ok = exit_code == 0 and not state.is_error
        error = state.error
        kind = state.error_kind
        if not ok:
            if error is None:
                tail = stderr.strip()[-2000:]
                error = f"exited with code {exit_code}" + (f": {tail}" if tail else "")
            kind = kind or classify_failure(exit_code, stderr, state.error or "")
        yield self._final(
            task,
            files,
            state,
            start,
            ok=ok,
            error=error if not ok else None,
            kind=kind if not ok else None,
        )

    # -- internals -----------------------------------------------------------------------

    async def _resolve_while_alive(
        self,
        task: AgentTask,
        question: AgentQuestion,
        proc: asyncio.subprocess.Process,
    ) -> AgentAnswer | None:
        """``resolve`` the question, unless the CLI ends first: then the wait is
        cancelled, which withdraws the question from the desk, and ``None``."""
        waiting = asyncio.ensure_future(self.resolve(task, question))
        ended = asyncio.ensure_future(proc.wait())
        try:
            done, _ = await asyncio.wait(
                {waiting, ended}, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            if not ended.done():
                ended.cancel()
        if waiting in done:
            return waiting.result()
        waiting.cancel()
        with contextlib.suppress(BaseException):
            await waiting
        return None

    def _absorb(self, events: list[AgentEvent], state: RunState) -> list[AgentEvent]:
        visible = []
        for event in events:
            if event.type == AgentEventType.FINAL and event.result is None:
                known = state.session
                state.merge(event.data)
                if state.session and state.session != known:
                    visible.append(
                        AgentEvent(
                            type=AgentEventType.SESSION,
                            data={"session": state.session},
                        )
                    )
                continue
            if event.type == AgentEventType.MESSAGE and event.text:
                state.messages.append(event.text)
            elif event.type == AgentEventType.USAGE:
                state.add_usage(event.data)
            elif event.type == AgentEventType.ERROR and event.text:
                state.error = event.text
                if event.data.get("error_kind"):
                    state.error_kind = str(event.data["error_kind"])
            visible.append(event)
        return visible

    def _final(
        self,
        task: AgentTask,
        files: RunFiles,
        state: RunState,
        start: float,
        *,
        ok: bool,
        error: str | None,
        kind: str | None,
    ) -> AgentEvent:
        text = (
            state.text
            if state.text is not None
            else (state.messages[-1] if state.messages else "")
        )
        structured = state.structured
        if ok and task.output_schema is not None and structured is None:
            structured = extract_json(text)
            if structured is None:
                ok, error, kind = (
                    False,
                    NO_JSON,
                    "invalid_output",
                )
        result = AgentResult(
            runtime=self.name,
            account=self.account,
            ok=ok,
            text=text,
            structured=structured,
            session=state.session,
            usage=state.usage,
            error=scrub(error) if error else None,
            error_kind=kind if not ok else None,  # type: ignore[arg-type]
            transcript_path=str(files.transcript),
            duration_seconds=round(time.monotonic() - start, 3),
        )
        with contextlib.suppress(OSError):
            files.result.write_text(
                scrub(result.model_dump_json(indent=2)), encoding="utf-8"
            )
        return AgentEvent(type=AgentEventType.FINAL, text=text, result=result)

    async def _terminate(self, proc: asyncio.subprocess.Process) -> None:
        _kill_group(proc.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(proc.wait(), self.kill_grace_seconds)
        except TimeoutError:
            _kill_group(proc.pid, signal.SIGKILL)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(proc.wait(), 5)


def _kill_group(pid: int, sig: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, sig)


async def _drain(stream: asyncio.StreamReader | None, buf: bytearray) -> None:
    if stream is None:
        return
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            return
        if len(buf) < _STDERR_KEEP:
            buf.extend(chunk[: _STDERR_KEEP - len(buf)])


async def _write_lines(stream: asyncio.StreamWriter | None, lines: list[str]) -> None:
    if stream is None or not lines or stream.is_closing():
        return
    try:
        for line in lines:
            stream.write(line.encode("utf-8") + b"\n")
        await stream.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass


def _close_stdin(proc: asyncio.subprocess.Process) -> None:
    if proc.stdin is not None and not proc.stdin.is_closing():
        with contextlib.suppress(Exception):
            proc.stdin.close()


async def _feed(stream: asyncio.StreamWriter | None, text: str) -> None:
    if stream is None:
        return
    try:
        stream.write(text.encode("utf-8"))
        await stream.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass
    finally:
        with contextlib.suppress(Exception):
            stream.close()


def _transcript_line(line: str) -> str:
    stripped = line.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        return scrub(stripped)
    return scrub(json.dumps({"text": line}, ensure_ascii=False))

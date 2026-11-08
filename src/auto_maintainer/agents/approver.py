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

"""Automatic answers for CLIs that have no auto mode of their own (the ACP runtimes).

The rules approve what is clearly safe for the task and nothing else: reading and
searching inside the run's directories, read-only shell commands there (also several
joined with ``&&``, ``||``, ``;``, or ``|`` when each one is), and, for tasks that
may write, edits inside the worktree. What a task may execute follows its
``run_code``: ``none`` reads only; ``skills`` (every task on a target repository) also
runs static linters and the scripts in the repository's skill folders; ``any`` (the
service's own code) also runs tests. Below ``any``, everything else is denied at once
(``NO_CODE_ANSWER`` for commands), since a repository's code runs only on its test
machines; for ``any``, it goes to a reviewer. That includes any path outside the
run's directories, network access, and shell syntax that can hide a command
(substitution, redirection other than discarding output to ``/dev/null`` or ``2>&1``,
subshells, a background ``&``).
"""

from __future__ import annotations

import os
import re
import shlex
from collections.abc import Sequence
from pathlib import Path

from ..domain.agents import (
    NO_CODE_ANSWER,
    REPO_SKILL_FOLDERS,
    AgentAnswer,
    AgentQuestion,
    AgentTask,
    QuestionKind,
)

READ_COMMANDS = frozenset(
    {
        "ls", "cat", "head", "tail", "wc", "grep", "rg", "find", "fd", "tree",
        "pwd", "stat", "file", "du", "sort", "uniq", "cut", "diff", "jq", "realpath",
        "basename", "dirname", "nl", "which", "true", "echo", "printf", "cd",
    }
)  # fmt: skip
GIT_READ = frozenset(
    {
        "diff", "log", "show", "status", "blame", "grep", "ls-files", "ls-tree",
        "rev-parse", "branch", "describe", "shortlog", "cat-file", "merge-base",
    }
)  # fmt: skip
GIT_WRITE = frozenset(
    {"add", "commit", "checkout", "switch", "restore", "stash", "apply", "mv", "rm"}
)
TEST_COMMANDS = frozenset({"pytest"})
SCRIPT_RUNNERS = frozenset({"python", "python3", "bash", "sh"})
"""Interpreters that may run a script from a skill folder, given the script's path."""
OUTSIDE_ANSWER = (
    "Only the run's own folders and reading commands are open to this task; continue "
    "without it."
)
_FIND_ACTIONS = frozenset({"-exec", "-execdir", "-delete", "-ok", "-okdir", "-fprint"})
_SHELL_META = ("$(", "`", "\n", "\\")
"""Text that can hide a command anywhere in a command line."""
_JOINS = frozenset({"&&", "||", ";", "|"})
"""Operators that run one command after another; each command is checked alone."""
_HARMLESS_REDIRECTS = re.compile(
    r"(?<![^\s;|&])(?:[12]?>|&>)\s*/dev/null(?![^\s;|&])|(?<![^\s;|&])2>&1(?![^\s;|&])"
)
"""Redirections that only discard output or join it with errors, which agents add to
read-only commands; any other redirection still goes to a reviewer."""
_GIT_REMOTE_READ = frozenset({"-v", "--verbose"})


POSTING_ANSWER = (
    "Do not post, comment, push, or publish anything, and do not ask whether to: "
    "return your result. The service shows it to the maintainer, who edits it and "
    "decides what to post."
)

_POSTING_COMMAND = re.compile(
    r"\bgh\s+(?:pr|issue)\s+(?:comment|review|edit|close|merge|create|ready|reopen)\b"
    r"|\bgh\s+api\b.*(?:-X|--method)\s*(?:POST|PATCH|PUT|DELETE)"
    r"|\bgit\s+push\b"
    r"|\bcurl\b.*api\.github\.com.*(?:-X|--request)\s*(?:POST|PATCH|PUT|DELETE)",
    re.IGNORECASE | re.DOTALL,
)
_POSTING_WORDS = re.compile(
    r"\b(?:post|publish|submit|leave|add|send|write)\w*\b[^.?!\n]{0,60}"
    r"\b(?:comments?|reviews?|replies|reply|feedback|findings)\b"
    r"|\b(?:comment|reply)\s+on\s+(?:the\s+)?(?:pr|pull request|github)\b"
    r"|发布|发表|提交评审|评论到",
    re.IGNORECASE,
)


def posting_request(question: AgentQuestion) -> bool:
    """Whether ``question`` asks to post to GitHub, or whether to: agents never post,
    so such a question is answered without bothering anyone (``POSTING_ANSWER``)."""
    if question.kind == QuestionKind.PERMISSION:
        return bool(question.detail and _POSTING_COMMAND.search(question.detail))
    texts = [question.title, question.detail]
    for q in question.inputs:
        texts += [q.header, q.question]
    return any(_POSTING_WORDS.search(t or "") for t in texts)


def auto_answer(
    task: AgentTask,
    question: AgentQuestion,
    approved: Sequence[Sequence[str]] = (),
) -> AgentAnswer | None:
    """An automatic answer for ``question``, or ``None`` when a reviewer must decide.
    ``approved`` are the leading words of commands the agent's settings approve
    (``RuntimeConfig.approve_commands``); they count only for a task that may run
    any code. Below that, what the rules do not allow is denied without asking."""
    if question.kind != QuestionKind.PERMISSION:
        return None
    roots = [task.workdir, *task.skill_dirs, *task.extra_dirs]
    skills = [task.workdir / folder for folder in REPO_SKILL_FOLDERS]
    skills += list(task.skill_dirs)
    kind = question.tool
    paths = question.paths
    undecided = None if task.run_code == "any" else _deny(OUTSIDE_ANSWER)
    if kind in ("edit", "delete", "move"):
        if task.mode == "read_only":
            return _deny("This is a read-only task, so it may not change files.")
        if paths and all(_inside(p, [task.workdir]) for p in paths):
            return _allow()
        return undecided
    if kind in ("read", "search", "think"):
        if all(_inside(p, roots) for p in paths):
            return _allow()
        return undecided
    if kind == "execute" and question.detail:
        if safe_command(
            question.detail,
            task.mode,
            roots,
            approved if task.run_code == "any" else (),
            run_code=task.run_code,
            skill_roots=skills,
        ):
            return _allow()
        return None if task.run_code == "any" else _deny(NO_CODE_ANSWER)
    return undecided


def _commands(command: str) -> list[list[str]] | None:
    """The simple commands in ``command`` joined by ``&&``, ``||``, ``;``, or ``|``,
    each as its words; ``None`` when it uses any other shell operator (redirection,
    a background ``&``, a subshell) or does not parse."""
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return None
    commands: list[list[str]] = [[]]
    for token in tokens:
        if token in _JOINS:
            commands.append([])
        elif token and set(token) <= set(lexer.punctuation_chars):
            return None  # >, <, &, (, ) and their combinations
        else:
            commands[-1].append(token)
    return commands


def safe_command(
    command: str,
    mode: str,
    roots: list[Path],
    approved: Sequence[Sequence[str]] = (),
    *,
    run_code: str = "any",
    skill_roots: Sequence[Path] = (),
) -> bool:
    """Whether ``command`` only does what ``run_code`` allows inside ``roots``: reads;
    with ``skills`` or more, static linters and scripts inside ``skill_roots``; with
    ``any``, tests too (for writing tasks), or one of ``approved`` (leading words)
    with any further arguments. Paths among the arguments stay inside ``roots``."""
    if any(token in command for token in _SHELL_META):
        return False
    commands = _commands(_HARMLESS_REDIRECTS.sub(" ", command))
    if commands is None:
        return False
    for words in commands:
        if not words or "=" in words[0]:
            return False
        if not (
            _safe_words(words, mode, run_code, skill_roots, roots[0])
            or _approved(words, approved)
        ):
            return False
        for word in words[1:]:
            value = _path_value(word)
            if value is None:
                continue
            exists = os.path.lexists(roots[0] / os.path.expanduser(value))
            if (exists or _looks_like_path(value)) and not _inside(value, roots):
                return False
    return True


def _approved(words: list[str], approved: Sequence[Sequence[str]]) -> bool:
    return any(
        len(words) >= len(p) and list(words[: len(p)]) == list(p) for p in approved if p
    )


def _safe_words(
    words: list[str],
    mode: str,
    run_code: str = "any",
    skill_roots: Sequence[Path] = (),
    cwd: Path | None = None,
) -> bool:
    name = os.path.basename(words[0])
    if name == "git":
        if any(
            w in ("-c", "-C", "--exec-path", "--git-dir", "--work-tree") for w in words
        ):
            return False
        sub = next((w for w in words[1:] if not w.startswith("-")), "")
        if sub in GIT_READ:
            return True
        if sub == "remote":  # listing remotes; adding or changing one is not reading
            rest = words[words.index("remote") + 1 :]
            return all(w in _GIT_REMOTE_READ for w in rest) or rest[:1] == ["get-url"]
        return mode == "workspace_write" and sub in GIT_WRITE
    if name == "find":
        return not any(w in _FIND_ACTIONS for w in words)
    if name in READ_COMMANDS:
        return True
    if run_code == "none":
        return False
    if name == "ruff" and _static_ruff(words[1:], mode):
        return True
    if cwd is not None and _skill_script(words, skill_roots, cwd):
        return True
    if run_code != "any" or mode != "workspace_write":
        return False
    if name in TEST_COMMANDS:
        return True
    return name == "uv" and words[1:2] == ["run"] and words[2:3] == ["pytest"]


def _static_ruff(args: list[str], mode: str) -> bool:
    """Ruff that only reads: ``check`` without ``--fix``, ``format`` with ``--check``
    or ``--diff``; in a writing task, also the fixes, which stay in the worktree."""
    sub = args[0] if args else ""
    if mode == "workspace_write":
        return sub in ("check", "format")
    if sub == "check":
        return not any(a.startswith(("--fix", "--unsafe-fixes")) for a in args)
    return sub == "format" and any(a in ("--check", "--diff") for a in args)


def _skill_script(words: list[str], skill_roots: Sequence[Path], cwd: Path) -> bool:
    """A script inside a skill folder, run by path or through an interpreter; a
    relative path is taken from ``cwd``, where the command runs."""
    if not skill_roots:
        return False
    roots = list(skill_roots)
    if os.path.basename(words[0]) in SCRIPT_RUNNERS:
        script = next((w for w in words[1:] if not w.startswith("-")), None)
        flags = words[1 : words.index(script)] if script else []
        return script is not None and not flags and _inside(str(cwd / script), roots)
    return "/" in words[0] and _inside(str(cwd / words[0]), roots)


def _path_value(word: str) -> str | None:
    """The path-like part of an argument: the word itself, or the value of ``--opt=value``."""
    if word.startswith("-"):
        _, sep, value = word.partition("=")
        return value if sep and value else None
    return word


def _looks_like_path(word: str) -> bool:
    return word.startswith(("/", "~")) or ".." in word.split("/")


def _inside(path: str, roots: list[Path]) -> bool:
    """Whether ``path``, with symlinks resolved, lies in one of ``roots``.

    Symlinks are resolved because a PR can commit a link that points out of the
    worktree, for example at the host's credentials.
    """
    candidate = Path(os.path.expanduser(path))
    if not candidate.is_absolute():
        candidate = roots[0] / candidate
    resolved = Path(os.path.realpath(candidate))
    for root in roots:
        base = Path(os.path.realpath(root))
        if resolved == base or base in resolved.parents:
            return True
    return False


def _allow() -> AgentAnswer:
    return AgentAnswer(allow=True, by="service", automatic=True)


def _deny(message: str) -> AgentAnswer:
    return AgentAnswer(allow=False, message=message, by="service", automatic=True)

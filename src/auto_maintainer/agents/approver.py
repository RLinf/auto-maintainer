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
searching inside the run's directories, read-only shell commands there, and, for
tasks that may write, edits inside the worktree and running tests or linters. Edits
in a read-only task are refused. Everything else, including any path outside the
run's directories, network access, and shell syntax that can hide a second command,
goes to a reviewer.
"""

from __future__ import annotations

import os
import re
import shlex
from pathlib import Path

from ..domain.agents import AgentAnswer, AgentQuestion, AgentTask, QuestionKind

READ_COMMANDS = frozenset(
    {
        "ls", "cat", "head", "tail", "wc", "grep", "rg", "find", "fd", "tree",
        "pwd", "stat", "file", "du", "sort", "uniq", "cut", "diff", "jq", "realpath",
        "basename", "dirname", "nl",
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
TEST_COMMANDS = frozenset({"pytest", "ruff"})
_FIND_ACTIONS = frozenset({"-exec", "-execdir", "-delete", "-ok", "-okdir", "-fprint"})
_SHELL_META = ("$(", "`", ">", "<", "&", ";", "\n", "\\")


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


def auto_answer(task: AgentTask, question: AgentQuestion) -> AgentAnswer | None:
    """An automatic answer for ``question``, or ``None`` when a reviewer must decide."""
    if question.kind != QuestionKind.PERMISSION:
        return None
    roots = [task.workdir, *task.skill_dirs, *task.extra_dirs]
    kind = question.tool
    paths = question.paths
    if kind in ("edit", "delete", "move"):
        if task.mode == "read_only":
            return _deny("This is a read-only task, so it may not change files.")
        if paths and all(_inside(p, [task.workdir]) for p in paths):
            return _allow()
        return None
    if kind in ("read", "search", "think"):
        if all(_inside(p, roots) for p in paths):
            return _allow()
        return None
    if kind == "execute" and question.detail:
        if safe_command(question.detail, task.mode, roots):
            return _allow()
        return None
    return None


def safe_command(command: str, mode: str, roots: list[Path]) -> bool:
    """Whether ``command`` only reads (or, for writing tasks, tests) inside ``roots``."""
    if any(token in command for token in _SHELL_META):
        return False
    for segment in command.split("|"):
        try:
            words = shlex.split(segment)
        except ValueError:
            return False
        if not words or "=" in words[0]:
            return False
        if not _safe_words(words, mode):
            return False
        for word in words[1:]:
            value = _path_value(word)
            if value is None:
                continue
            exists = os.path.lexists(roots[0] / os.path.expanduser(value))
            if (exists or _looks_like_path(value)) and not _inside(value, roots):
                return False
    return True


def _safe_words(words: list[str], mode: str) -> bool:
    name = os.path.basename(words[0])
    if name == "git":
        if any(
            w in ("-c", "-C", "--exec-path", "--git-dir", "--work-tree") for w in words
        ):
            return False
        sub = next((w for w in words[1:] if not w.startswith("-")), "")
        if sub in GIT_READ:
            return True
        return mode == "workspace_write" and sub in GIT_WRITE
    if name == "find":
        return not any(w in _FIND_ACTIONS for w in words)
    if name in READ_COMMANDS:
        return True
    if mode != "workspace_write":
        return False
    if name in TEST_COMMANDS:
        return True
    return (
        name == "uv" and words[1:2] == ["run"] and words[2:3] in (["pytest"], ["ruff"])
    )


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

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

"""A runtime defined by configuration alone, for CLIs without a native adapter.

The command is a list of arguments with placeholders: ``{prompt}``,
``{prompt_file}``, ``{workdir}``, and ``{schema_file}``. When the command has
neither ``{prompt}`` nor ``{prompt_file}``, the prompt is fed on standard input.
The output is read whole when the process exits, as plain text, one JSON
document, or JSON lines; ``final_path`` is a dot path into the (last) JSON object
that holds the final message.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import Any, Literal

from ...domain.agents import AgentEvent, AgentEventType, AgentTask, Capabilities
from ..exec import ExecRuntime, Invocation, RunFiles
from . import partial

OutputFormat = Literal["text", "json", "jsonl"]


class GenericRuntime(ExecRuntime):
    """A CLI described by a command template.

    Args:
        name: The runtime name.
        command: The argument list with placeholders; the first item is the binary.
        output_format: ``text``, ``json``, or ``jsonl``.
        final_path: Dot path to the final message in a JSON output, such as ``result.text``.
        **kwargs: Passed to ``ExecRuntime``.
    """

    default_name = "generic"
    line_mode = False

    def __init__(
        self,
        name: str,
        *,
        command: Iterable[str],
        output_format: OutputFormat = "text",
        final_path: str | None = None,
        bypass_args: Iterable[str] = (),
        **kwargs: Any,
    ) -> None:
        self.command = list(command)
        self.bypass_args = list(bypass_args)
        if not self.command:
            raise ValueError(f"generic runtime {name!r} needs a non-empty command")
        kwargs.setdefault("binary", self.command[0])
        super().__init__(name, **kwargs)
        self.output_format: OutputFormat = output_format
        self.final_path = final_path

    def capabilities(self) -> Capabilities:
        return Capabilities(bypass=bool(self.bypass_args))

    def provider_env(self) -> dict[str, str | None]:
        """``AM_PROVIDER_*`` for any CLI, plus the usual names for its protocol."""
        p = self.provider
        if p is None:
            return {}
        env: dict[str, str | None] = {
            "AM_PROVIDER_PROTOCOL": p.protocol,
            "AM_PROVIDER_BASE_URL": p.base_url,
            "AM_PROVIDER_MODEL": self.model,
            "AM_PROVIDER_API_KEY": p.api_key,
        }
        if p.protocol == "anthropic":
            env |= {"ANTHROPIC_BASE_URL": p.base_url, "ANTHROPIC_AUTH_TOKEN": p.api_key}
        else:
            env |= {"OPENAI_BASE_URL": p.base_url, "OPENAI_API_KEY": p.api_key}
        return {k: v for k, v in env.items() if v is not None}

    def build_invocation(
        self, task: AgentTask, files: RunFiles, binary: str
    ) -> Invocation:
        schema_path = self.write_schema(task, files)
        values = {
            "prompt": self.argv_prompt(task, files),
            "prompt_file": str(files.prompt),
            "workdir": str(task.workdir),
            "schema_file": str(schema_path or ""),
        }
        uses_prompt = any(
            "{prompt}" in arg or "{prompt_file}" in arg for arg in self.command
        )
        argv = [
            binary,
            *(_fill(arg, values) for arg in self.command[1:]),
            *(self.bypass_args if task.permissions == "bypass" else []),
            *self.extra_args,
        ]
        return Invocation(argv=argv, stdin=None if uses_prompt else task.prompt)

    def parse_document(self, text: str) -> list[AgentEvent]:
        if self.output_format == "text":
            stripped = text.strip()
            return (
                [
                    AgentEvent(type=AgentEventType.MESSAGE, text=stripped),
                    partial(text=stripped),
                ]
                if stripped
                else []
            )
        objects: list[Any] = []
        if self.output_format == "json":
            try:
                objects = [json.loads(text)]
            except json.JSONDecodeError:
                objects = []
        else:
            for line in text.splitlines():
                try:
                    objects.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        objects = [o for o in objects if isinstance(o, dict)]
        if not objects:
            return [
                AgentEvent(
                    type=AgentEventType.ERROR,
                    text=f"no JSON object in {self.output_format} output",
                )
            ]
        value = _dig(objects[-1], self.final_path)
        if isinstance(value, dict):
            final = json.dumps(value)
            return [
                AgentEvent(type=AgentEventType.MESSAGE, text=final),
                partial(text=final, structured=value),
            ]
        final = "" if value is None else str(value)
        return [
            AgentEvent(type=AgentEventType.MESSAGE, text=final),
            partial(text=final),
        ]


_PLACEHOLDER = re.compile(r"\{(prompt|prompt_file|workdir|schema_file)\}")


def _fill(arg: str, values: dict[str, str]) -> str:
    return _PLACEHOLDER.sub(lambda m: values[m.group(1)], arg)


def _dig(obj: Any, path: str | None) -> Any:
    if not path:
        return obj
    for part in path.split("."):
        if isinstance(obj, dict):
            obj = obj.get(part)
        elif isinstance(obj, list) and part.lstrip("-").isdigit():
            idx = int(part)
            obj = obj[idx] if -len(obj) <= idx < len(obj) else None
        else:
            return None
    return obj

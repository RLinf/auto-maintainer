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

"""Structured output: extracting JSON from agent text, validating it, and asking for repairs."""

from __future__ import annotations

import copy
import json
import re
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

M = TypeVar("M", bound=BaseModel)

_FENCE_RE = re.compile(r"```(?:json|JSON)?[ \t]*\n(.*?)\n?```", re.DOTALL)


def extract_json(text: str) -> dict[str, Any] | None:
    """Return the JSON object in an agent's final message, or ``None``.

    A fenced ``json`` block wins; the last parseable fenced block is used when there
    are several. Otherwise the last top-level JSON object in the text is used.
    """
    if not text:
        return None
    for block in reversed(_FENCE_RE.findall(text)):
        parsed = _loads_object(block)
        if parsed is not None:
            return parsed
    decoder = json.JSONDecoder()
    found: dict[str, Any] | None = None
    pos = text.find("{")
    while pos != -1:
        try:
            value, end = decoder.raw_decode(text, pos)
        except json.JSONDecodeError:
            pos = text.find("{", pos + 1)
            continue
        if isinstance(value, dict):
            found = value
        pos = text.find("{", end)
    return found


def _loads_object(candidate: str) -> dict[str, Any] | None:
    try:
        value = json.loads(candidate)
    except (json.JSONDecodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def validate(
    data: dict[str, Any] | None, model: type[M]
) -> tuple[M | None, str | None]:
    """Validate ``data`` against ``model``.

    Returns:
        ``(instance, None)`` on success, or ``(None, error)`` where ``error`` says
        what was expected and what was found.
    """
    if data is None:
        return None, NO_JSON
    try:
        return model.model_validate(data), None
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['msg']}"
            for err in exc.errors()[:20]
        )
        return None, f"the JSON object does not match the schema: {problems}"


def schema_instructions(schema: dict[str, Any]) -> str:
    """A prompt suffix asking for one fenced JSON object that matches ``schema``."""
    return (
        "\n\n## Output format\n\n"
        "End your answer with exactly one JSON object in a fenced ```json block. "
        "It must validate against this JSON schema; put nothing after the block.\n\n"
        f"```json\n{json.dumps(schema, indent=2, ensure_ascii=False)}\n```\n"
    )


NO_JSON = "expected one JSON object in the final message, found none"
"""The error of a final message without a result: the agent stopped, perhaps before it
was done."""


def repair_prompt(error: str, previous_text: str, schema: dict[str, Any]) -> str:
    """A follow-up turn asking the agent to fix an invalid structured answer."""
    excerpt = previous_text.strip()
    if len(excerpt) > 4000:
        excerpt = excerpt[-4000:]
    return (
        "Your previous answer could not be used: "
        f"{error}.\n\n"
        "Previous answer (end of it):\n\n"
        f"<previous_answer>\n{excerpt}\n</previous_answer>\n"
        + (
            "Your turn ended without the result. If the task is not done, finish it "
            "now: wait for every command you started until it ends, and do the steps "
            "left. Then end your turn with only the JSON object."
            if error == NO_JSON
            else "Reply again with only the corrected JSON object."
        )
        + schema_instructions(schema)
    )


def strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Convert a Pydantic JSON schema to the strict form some CLIs require.

    Every object gets ``additionalProperties: false`` and lists all its properties
    as required, and ``default`` keywords are removed. A field that was optional
    stays nullable only if it already allowed ``null``; otherwise the agent must
    give it a value, which the original model also accepts.
    """
    out = copy.deepcopy(schema)
    maps = ("properties", "$defs", "definitions", "patternProperties")

    def visit(node: Any, is_map: bool = False) -> None:
        if isinstance(node, list):
            for item in node:
                visit(item)
            return
        if not isinstance(node, dict):
            return
        if is_map:
            for value in node.values():
                visit(value)
            return
        node.pop("default", None)
        if node.get("type") == "object":
            if isinstance(node.get("properties"), dict):
                node["required"] = list(node["properties"])
            node["additionalProperties"] = False
        for key, value in node.items():
            visit(value, is_map=key in maps)

    visit(out)
    return out

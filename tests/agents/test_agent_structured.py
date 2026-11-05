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

from pydantic import BaseModel

from auto_maintainer.agents.structured import (
    extract_json,
    repair_prompt,
    schema_instructions,
    strict_schema,
    validate,
)


class Item(BaseModel):
    name: str
    note: str | None = None


class Answer(BaseModel):
    ok: bool
    items: list[Item] = []
    summary: str = ""


def test_extract_prefers_the_last_fenced_block() -> None:
    text = 'Draft:\n```json\n{"ok": false}\n```\nFinal:\n```json\n{"ok": true}\n```\n'
    assert extract_json(text) == {"ok": True}


def test_extract_falls_back_to_outer_braces() -> None:
    assert extract_json('Here it is: {"ok": true, "items": []} done') == {
        "ok": True,
        "items": [],
    }


def test_extract_skips_unparseable_fences_and_non_objects() -> None:
    assert extract_json('```json\n{"ok": tru\n```\n{"ok": true}') == {"ok": True}
    assert extract_json("[1, 2]") is None
    assert extract_json("no json here") is None
    assert extract_json("") is None


def test_validate_reports_expected_and_found() -> None:
    obj, err = validate({"ok": True, "items": [{"name": "a"}]}, Answer)
    assert err is None and obj is not None and obj.items[0].name == "a"
    obj, err = validate({"items": "nope"}, Answer)
    assert obj is None and err is not None and "ok" in err and "items" in err
    obj, err = validate(None, Answer)
    assert obj is None and "found none" in (err or "")


def test_schema_instructions_and_repair_embed_the_schema() -> None:
    schema = Answer.model_json_schema()
    assert '"ok"' in schema_instructions(schema)
    prompt = repair_prompt("missing field ok", "x" * 5000, schema)
    assert "missing field ok" in prompt and '"ok"' in prompt
    assert prompt.count("x") <= 4100


def test_strict_schema_requires_everything() -> None:
    class WithDefaultField(BaseModel):
        default: str
        ok: bool = True

    strict = strict_schema(Answer.model_json_schema())
    assert strict["additionalProperties"] is False
    assert set(strict["required"]) == {"ok", "items", "summary"}
    assert strict["properties"]["summary"] == {"title": "Summary", "type": "string"}
    item = strict["$defs"]["Item"]
    assert item["additionalProperties"] is False and set(item["required"]) == {
        "name",
        "note",
    }
    assert {"type": "null"} in item["properties"]["note"]["anyOf"]
    # A property named "default" is kept; only the keyword is dropped.
    other = strict_schema(WithDefaultField.model_json_schema())
    assert (
        "default" in other["properties"] and "default" not in other["properties"]["ok"]
    )

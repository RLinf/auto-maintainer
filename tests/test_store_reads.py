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

"""What the store saves the dashboard from parsing (``store/sqlite.py``): a
snapshot is parsed again only when it changed, and a list that reads a few fields of
many actions gets only those fields."""

from __future__ import annotations

from pathlib import Path

from auto_maintainer.domain.actions import ActionKind, ReviewPostPayload, make_action
from auto_maintainer.domain.records import ActionRecord, ActionStatus
from auto_maintainer.store.sqlite import SqliteStore
from auto_maintainer.testing.fakes import make_pr


async def test_a_snapshot_is_parsed_again_only_when_it_changed(tmp_path: Path) -> None:
    store = await SqliteStore.open(tmp_path / "am.db")
    pr = make_pr(number=7, sha="a" * 40, title="first")
    await store.save_snapshot(pr)
    [listed] = await store.list_snapshots(pr.repo)
    assert listed is await store.get_snapshot(pr.repo, 7), "one parse for every reader"
    await store.save_snapshot(pr.model_copy(update={"title": "second"}))
    [again] = await store.list_snapshots(pr.repo)
    assert again.title == "second" and again is not listed
    fresh = await SqliteStore.open(tmp_path / "am.db")
    assert (await fresh.get_snapshot(pr.repo, 7)).title == "second"  # type: ignore[union-attr]


async def test_a_list_gets_only_the_payload_fields_it_reads(tmp_path: Path) -> None:
    store = await SqliteStore.open(tmp_path / "am.db")
    action = make_action(
        ActionKind.REVIEW_POST,
        "acme/widget",
        ReviewPostPayload(
            body="x" * 10_000,
            event="COMMENT",
            kind="followup",
            result={"quality": {"score": 80, "reason": "fine"}, "findings": [1]},
        ),
        number=7,
        head_sha="a" * 40,
        discriminator="r",
    )
    await store.add_action(ActionRecord(action=action, status=ActionStatus.EXECUTED))
    [slim] = await store.list_actions(
        repo="acme/widget", payload=["kind", "result.quality"]
    )
    assert slim.action.payload == {
        "kind": "followup",
        "result": {"quality": {"score": 80, "reason": "fine"}},
    }
    assert slim.status == ActionStatus.EXECUTED and slim.action.id == action.id
    [bare] = await store.list_actions(repo="acme/widget", payload=[])
    assert bare.action.payload == {}
    [full] = await store.list_actions(repo="acme/widget")
    assert full.action.payload["body"] == "x" * 10_000, "untouched without payload"

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

"""An agent's answer as the service shows or posts it (``playbooks/answers.py``)."""

from __future__ import annotations

from auto_maintainer.playbooks.answers import ELLIPSIS, checkout_paths, clip

ROOT = "/home/svc/.local/share/auto-maintainer/repos/RLinf/RLinf/pr-1796-4f3d97-4ae5e5"


def test_a_long_answer_keeps_its_start_and_ends_at_a_line() -> None:
    answer = "### Conflict Resolutions\n\n" + "\n".join(
        f"- file{i}.py: kept both sides of the hunk" for i in range(200)
    )
    short = clip(answer, 3000)
    assert short.startswith("### Conflict Resolutions"), "the heading survives"
    assert short.endswith(ELLIPSIS) and len(short) <= 3000
    assert short[: -len(ELLIPSIS)].endswith("kept both sides of the hunk"), (
        "cut at a line, never mid-word"
    )
    assert clip("  short answer  ", 3000) == "short answer"


def test_links_into_the_checkout_become_repository_paths() -> None:
    text = (
        f"- [huggingface_worker.py](file://{ROOT}/rlinf/workers/rollout/hf/"
        "huggingface_worker.py)\n"
        f"  - In [`_serve_step`](file://{ROOT}/rlinf/x.py#L907-L936), grouped them.\n"
        f"- Also see file://{ROOT}/tests/test_utils.py and "
        "[notes](file:///home/svc/elsewhere/notes.md)."
    )
    out = checkout_paths(text, ROOT)
    assert "file://" not in out and "/home/svc" not in out
    assert "- `rlinf/workers/rollout/hf/huggingface_worker.py`" in out
    assert "[`_serve_step`] (`rlinf/x.py:907-936`)" not in out
    assert "`_serve_step` (`rlinf/x.py:907-936`)" in out
    assert "see tests/test_utils.py and notes." in out

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

"""Running a subprocess while handing its output to someone watching."""

from __future__ import annotations

from auto_maintainer.subproc import run


async def test_each_line_is_heard_as_it_comes_and_the_output_is_kept() -> None:
    heard: list[str] = []

    async def hear(line: str) -> None:
        heard.append(line)

    script = "echo one; sleep 0.2; echo two >&2; cat; exit 3"
    result = await run(["bash", "-c", script], stdin="three\n", on_line=hear)
    assert result.exit_code == 3
    assert heard == ["one", "two", "three"]
    assert result.stdout == "one\nthree\n" and result.stderr == "two\n"


async def test_a_failing_listener_never_stops_the_run_and_timeouts_still_kill() -> None:
    async def broken(line: str) -> None:
        raise RuntimeError("the dashboard went away")

    ok = await run(["bash", "-c", "echo fine"], on_line=broken)
    assert ok.exit_code == 0 and ok.stdout == "fine\n"
    slow = await run(
        ["bash", "-c", "echo start; sleep 30"], timeout=0.5, on_line=broken
    )
    assert slow.timed_out and slow.stdout == "start\n"

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

"""Shared fixtures for the GitHub adapter tests."""

from __future__ import annotations

import pytest
import respx
from gh_testutil import API, no_sleep

from auto_maintainer.forges.github import GitHubClient, StaticTokenProvider


@pytest.fixture
def router():
    with respx.mock(base_url=API, assert_all_called=False) as mock:
        yield mock


@pytest.fixture
async def client(router):
    gh = GitHubClient(StaticTokenProvider(token="tok"), sleep=no_sleep, max_retries=3)
    yield gh
    await gh.aclose()

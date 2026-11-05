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

"""The GitHub forge adapter: ``ForgeReader``, ``ForgeWriter``, and ``CiProvider``."""

from .auth import AuthError, GitHubAppTokenProvider, StaticTokenProvider, TokenProvider
from .ci import GitHubActionsCi
from .client import GitHubClient, GitHubError, GraphQLError, RateLimited
from .reader import GitHubReader, snapshot_from_graphql
from .webhooks import hint, verify_signature
from .writer import GitHubWriter

__all__ = [
    "AuthError",
    "GitHubActionsCi",
    "GitHubAppTokenProvider",
    "GitHubClient",
    "GitHubError",
    "GitHubReader",
    "GitHubWriter",
    "GraphQLError",
    "RateLimited",
    "StaticTokenProvider",
    "TokenProvider",
    "hint",
    "snapshot_from_graphql",
    "verify_signature",
]

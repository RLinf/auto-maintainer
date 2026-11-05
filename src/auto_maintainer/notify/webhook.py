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

"""Notification channels (design 5.14): Slack and Feishu/Lark webhooks, and the log."""

from __future__ import annotations

import logging
import os
from typing import Literal

import httpx
from pydantic import BaseModel

log = logging.getLogger(__name__)


class ChannelConfig(BaseModel):
    type: Literal["slack", "feishu", "log"] = "log"
    url_env: str | None = None
    """Environment variable holding the webhook URL, which is a secret."""


class WebhookNotifier:
    """Implements ``Notifier``."""

    def __init__(
        self,
        channels: dict[str, ChannelConfig],
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.channels = channels
        self._client = client or httpx.AsyncClient(timeout=15, trust_env=True)

    async def send(self, channel: str, text: str, event: str) -> None:
        cfg = self.channels.get(channel, ChannelConfig())
        if cfg.type == "log" or not cfg.url_env:
            log.info("[%s] %s: %s", channel, event, text)
            return
        url = os.environ.get(cfg.url_env)
        if not url:
            raise RuntimeError(
                f"channel {channel!r} needs the environment variable {cfg.url_env}"
            )
        if cfg.type == "slack":
            payload: dict = {"text": f"*{event}*\n{text}"}
        else:
            payload = {"msg_type": "text", "content": {"text": f"[{event}] {text}"}}
        response = await self._client.post(url, json=payload)
        response.raise_for_status()

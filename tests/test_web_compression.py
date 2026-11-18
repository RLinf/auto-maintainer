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

"""What travels to a browser far from the host: large answers compressed, and the
build's hashed files kept by the browser (``web/app.py``)."""

from __future__ import annotations

import gzip
from pathlib import Path

import httpx
from fastapi import FastAPI

from auto_maintainer.testing.demo import build_demo
from auto_maintainer.web.app import GZIP_MIN_BYTES, HashedAssets, create_app


async def test_large_answers_go_out_compressed(tmp_path: Path) -> None:
    h = await build_demo(tmp_path)
    try:
        app = create_app(h.service, data_dir=tmp_path, allow_dev_login=True)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as c:
            await c.get("/auth/dev?login=maria")
            plain = await c.get("/api/prs", headers={"accept-encoding": "identity"})
            assert len(plain.content) > GZIP_MIN_BYTES
            assert "content-encoding" not in plain.headers
            # httpx would undo the compression; read the bytes as they travel.
            req = c.build_request(
                "GET", "/api/prs", headers={"accept-encoding": "gzip"}
            )
            packed = await c.send(req, stream=True)
            raw = b"".join([part async for part in packed.aiter_raw()])
            assert packed.headers["content-encoding"] == "gzip"
            assert len(raw) < len(plain.content)
            assert gzip.decompress(raw) == plain.content
    finally:
        await h.close()


async def test_hashed_build_files_are_kept_by_the_browser(tmp_path: Path) -> None:
    (tmp_path / "index-abc123.js").write_text("console.log(1)")
    app = FastAPI()
    app.mount("/assets", HashedAssets(directory=tmp_path), name="assets")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as c:
        r = await c.get("/assets/index-abc123.js")
        assert r.status_code == 200
        assert "immutable" in r.headers["cache-control"]


async def test_the_event_stream_passes_through_at_once() -> None:
    """The live stream is never held back to be packed: each event reaches the page
    as it is sent, uncompressed; only complete answers are compressed (in a worker
    thread)."""
    import asyncio

    from starlette.responses import JSONResponse

    from auto_maintainer.web.app import EVENT_STREAM, ThreadedGZip

    release = asyncio.Event()

    async def stream_app(scope, receive, send):  # type: ignore[no-untyped-def]
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", EVENT_STREAM.encode())],
            }
        )
        first = b"event: ping\ndata: " + b"x" * 4000 + b"\n\n"
        await send({"type": "http.response.body", "body": first, "more_body": True})
        await release.wait()
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    sent: list[dict] = []

    async def send(message):  # type: ignore[no-untyped-def]
        sent.append(message)

    async def receive():  # type: ignore[no-untyped-def]
        return {"type": "http.disconnect"}

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/events",
        "headers": [(b"accept-encoding", b"gzip")],
    }
    running = asyncio.create_task(ThreadedGZip(stream_app)(scope, receive, send))
    for _ in range(100):
        if len(sent) >= 2:
            break
        await asyncio.sleep(0.01)
    assert [m["type"] for m in sent] == ["http.response.start", "http.response.body"]
    assert sent[1]["body"].startswith(b"event: ping"), "before the stream ends"
    assert b"content-encoding" not in dict(sent[0]["headers"])
    release.set()
    await running

    big = b"y" * 5000
    packed: list[dict] = []

    async def answer_app(scope, receive, send):  # type: ignore[no-untyped-def]
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", JSONResponse.media_type.encode()),
                    (b"content-length", str(len(big)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": big})

    async def keep(message):  # type: ignore[no-untyped-def]
        packed.append(message)

    await ThreadedGZip(answer_app)(scope, receive, keep)
    headers = dict(packed[0]["headers"])
    assert headers[b"content-encoding"] == b"gzip"
    assert gzip.decompress(packed[1]["body"]) == big
    assert headers[b"content-length"] == str(len(packed[1]["body"])).encode()

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx
from websockets.asyncio.client import connect


@dataclass(frozen=True)
class MattermostPost:
    id: str
    channel_id: str
    user_id: str
    root_id: str
    post_type: str
    props: dict[str, Any]


class MattermostClient:
    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.bot_user_id: str | None = None
        self._http = httpx.AsyncClient(
            base_url=f"{self.base_url}/api/v4",
            headers={"Authorization": f"Bearer {token}"},
            timeout=20.0,
        )

    async def authenticate(self) -> str:
        response = await self._http.get("/users/me")
        response.raise_for_status()
        self.bot_user_id = response.json()["id"]
        return self.bot_user_id

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        websocket_url = _websocket_url(self.base_url)
        async with connect(
            websocket_url,
            open_timeout=20,
            ping_interval=20,
            ping_timeout=20,
            max_size=2**20,
        ) as websocket:
            await websocket.send(
                json.dumps(
                    {
                        "seq": 1,
                        "action": "authentication_challenge",
                        "data": {"token": self.token},
                    }
                )
            )
            async for raw_message in websocket:
                message = json.loads(raw_message)
                if message.get("status") == "FAIL":
                    raise PermissionError(
                        f"Mattermost WebSocket authentication failed: {message}"
                    )
                if message.get("event"):
                    yield message

    async def create_thread_reply(
        self,
        channel_id: str,
        root_id: str,
        message: str,
    ) -> str:
        response = await self._http.post(
            "/posts",
            json={
                "channel_id": channel_id,
                "root_id": root_id,
                "message": message,
            },
        )
        response.raise_for_status()
        return response.json()["id"]

    async def close(self) -> None:
        await self._http.aclose()


def parse_posted_event(event: dict[str, Any]) -> MattermostPost | None:
    if event.get("event") != "posted":
        return None
    data = event.get("data")
    if not isinstance(data, dict):
        return None
    raw_post = data.get("post")
    if isinstance(raw_post, str):
        try:
            post = json.loads(raw_post)
        except json.JSONDecodeError:
            return None
    elif isinstance(raw_post, dict):
        post = raw_post
    else:
        return None

    required = ("id", "channel_id", "user_id")
    if not all(isinstance(post.get(key), str) and post[key] for key in required):
        return None
    props = post.get("props")
    return MattermostPost(
        id=post["id"],
        channel_id=post["channel_id"],
        user_id=post["user_id"],
        root_id=post.get("root_id") or post["id"],
        post_type=str(post.get("type") or ""),
        props=props if isinstance(props, dict) else {},
    )


def _websocket_url(base_url: str) -> str:
    if base_url.startswith("https://"):
        websocket_base = f"wss://{base_url.removeprefix('https://')}"
    elif base_url.startswith("http://"):
        websocket_base = f"ws://{base_url.removeprefix('http://')}"
    else:
        raise ValueError("Mattermost URL must start with http:// or https://")
    return f"{websocket_base.rstrip('/')}/api/v4/websocket"


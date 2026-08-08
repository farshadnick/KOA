import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from bot import AfterHoursBot
from schedule import ContractRule
from sheets import RuleStore


class FakeMattermostClient:
    def __init__(self) -> None:
        self.bot_user_id = "bot-user"
        self.replies: list[dict[str, str]] = []

    async def create_thread_reply(
        self,
        channel_id: str,
        root_id: str,
        message: str,
    ) -> str:
        self.replies.append(
            {
                "channel_id": channel_id,
                "root_id": root_id,
                "message": message,
            }
        )
        return f"reply-{len(self.replies)}"


def make_store(tmp_path: Path) -> RuleStore:
    rule = ContractRule.model_validate(
        {
            "channel_id": "customer-channel",
            "customer_name": "Acme",
            "timezone": "UTC",
            "support_days": "Mon,Tue,Wed,Thu,Fri",
            "open_time": "09:00",
            "close_time": "17:00",
            "support_user_ids": "support-user",
            "closed_message": "Closed until {next_open_date} {next_open_time}.",
        }
    )
    store = RuleStore(tmp_path / "rules.json")
    store.replace([rule])
    return store


def posted_event(
    post_id: str = "post-1",
    user_id: str = "customer-user",
    channel_id: str = "customer-channel",
    root_id: str = "",
    post_type: str = "",
    props: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "event": "posted",
        "data": {
            "post": json.dumps(
                {
                    "id": post_id,
                    "channel_id": channel_id,
                    "user_id": user_id,
                    "root_id": root_id,
                    "type": post_type,
                    "props": props or {},
                }
            )
        },
    }


@pytest.mark.asyncio
async def test_each_after_hours_message_gets_one_threaded_reply(
    tmp_path: Path,
) -> None:
    client = FakeMattermostClient()
    bot = AfterHoursBot(
        client,  # type: ignore[arg-type]
        make_store(tmp_path),
        now=lambda: datetime(2026, 7, 20, 18, 0, tzinfo=UTC),
    )

    assert await bot.handle_event(posted_event("post-1"))
    assert await bot.handle_event(posted_event("post-2", root_id="thread-root"))
    assert not await bot.handle_event(posted_event("post-1"))

    assert len(client.replies) == 2
    assert client.replies[0] == {
        "channel_id": "customer-channel",
        "root_id": "post-1",
        "message": "Closed until 2026-07-21 09:00.",
    }
    assert client.replies[1]["root_id"] == "thread-root"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event",
    [
        posted_event(user_id="support-user"),
        posted_event(user_id="bot-user"),
        posted_event(channel_id="unconfigured-channel"),
        posted_event(post_type="system_join_channel"),
        posted_event(props={"from_bot": "true"}),
        posted_event(props={"from_webhook": "true"}),
        {"event": "typing", "data": {}},
    ],
)
async def test_non_customer_posts_are_ignored(
    tmp_path: Path,
    event: dict[str, Any],
) -> None:
    client = FakeMattermostClient()
    bot = AfterHoursBot(
        client,  # type: ignore[arg-type]
        make_store(tmp_path),
        now=lambda: datetime(2026, 7, 20, 18, 0, tzinfo=UTC),
    )
    assert not await bot.handle_event(event)
    assert client.replies == []


@pytest.mark.asyncio
async def test_open_hours_message_does_not_get_reply(tmp_path: Path) -> None:
    client = FakeMattermostClient()
    bot = AfterHoursBot(
        client,  # type: ignore[arg-type]
        make_store(tmp_path),
        now=lambda: datetime(2026, 7, 20, 12, 0, tzinfo=UTC),
    )
    assert not await bot.handle_event(posted_event())
    assert client.replies == []


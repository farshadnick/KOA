from __future__ import annotations

import asyncio
import json
import logging
import sys
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from mattermost import MattermostClient, MattermostPost, parse_posted_event
from schedule import is_open, render_closed_message
from settings import Settings
from sheets import GoogleSheetSource, RuleStore, SheetSynchronizer

logger = logging.getLogger(__name__)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload)


class AfterHoursBot:
    def __init__(
        self,
        client: MattermostClient,
        rules: RuleStore,
        now: Callable[[], datetime] | None = None,
        dedup_capacity: int = 10_000,
    ):
        self.client = client
        self.rules = rules
        self.now = now or (lambda: datetime.now(UTC))
        self.dedup_capacity = dedup_capacity
        self._seen_ids: set[str] = set()
        self._seen_order: deque[str] = deque()

    async def handle_event(self, event: dict[str, Any]) -> bool:
        post = parse_posted_event(event)
        if post is None or self._ignore_post(post):
            return False
        if not self._remember(post.id):
            return False

        rule = self.rules.get(post.channel_id)
        if rule is None or post.user_id in rule.support_user_ids:
            return False

        instant = self.now()
        if is_open(rule, instant):
            return False

        message = render_closed_message(rule, instant)
        await self.client.create_thread_reply(
            channel_id=post.channel_id,
            root_id=post.root_id,
            message=message,
        )
        logger.info(
            "Sent after-hours reply channel_id=%s post_id=%s",
            post.channel_id,
            post.id,
        )
        return True

    def _ignore_post(self, post: MattermostPost) -> bool:
        if not self.client.bot_user_id:
            raise RuntimeError("Mattermost client must be authenticated first")
        if post.user_id == self.client.bot_user_id or post.post_type:
            return True
        return _truthy(post.props.get("from_bot")) or _truthy(
            post.props.get("from_webhook")
        )

    def _remember(self, post_id: str) -> bool:
        if post_id in self._seen_ids:
            return False
        self._seen_ids.add(post_id)
        self._seen_order.append(post_id)
        if len(self._seen_order) > self.dedup_capacity:
            self._seen_ids.remove(self._seen_order.popleft())
        return True

    async def listen_forever(self) -> None:
        delay = 1.0
        while True:
            try:
                async for event in self.client.events():
                    delay = 1.0
                    try:
                        await self.handle_event(event)
                    except Exception:
                        logger.exception("Failed to process Mattermost event")
                raise ConnectionError("Mattermost WebSocket closed")
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Mattermost connection failed; reconnecting in %.1f seconds",
                    delay,
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60.0)


async def run(settings: Settings) -> None:
    store = RuleStore(settings.rule_cache_path)
    cache_loaded = store.load_cache()
    source = GoogleSheetSource(
        settings.google_credentials_file,
        settings.google_sheet_id,
        settings.google_sheet_tab,
    )
    synchronizer = SheetSynchronizer(
        store,
        source.fetch_rows,
        settings.sheet_refresh_seconds,
    )
    refreshed = await synchronizer.refresh_once()
    if not refreshed and not cache_loaded:
        logger.warning(
            "No valid contract rules are available; the bot will not send replies"
        )

    client = MattermostClient(str(settings.mattermost_url), settings.mattermost_bot_token)
    refresh_task: asyncio.Task[None] | None = None
    try:
        bot_user_id = await client.authenticate()
        logger.info("Authenticated Mattermost bot user_id=%s", bot_user_id)
        refresh_task = asyncio.create_task(
            synchronizer.run_forever(),
            name="google-sheet-refresh",
        )
        await AfterHoursBot(client, store).listen_forever()
    finally:
        if refresh_task:
            refresh_task.cancel()
            await asyncio.gather(refresh_task, return_exceptions=True)
        await client.close()


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        handlers=[handler],
        force=True,
    )


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in {"true", "yes", "1"}


def main() -> None:
    settings = Settings()
    configure_logging(settings.log_level)
    asyncio.run(run(settings))


if __name__ == "__main__":
    main()


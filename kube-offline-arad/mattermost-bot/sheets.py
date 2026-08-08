from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import gspread

from schedule import ContractRule

logger = logging.getLogger(__name__)

SHEET_COLUMNS = {
    "enabled",
    "channel_id",
    "customer_name",
    "timezone",
    "support_days",
    "open_time",
    "close_time",
    "support_24x7",
    "support_user_ids",
    "closed_message",
}


class GoogleSheetSource:
    def __init__(self, credentials_file: Path, sheet_id: str, tab_name: str):
        self.credentials_file = credentials_file
        self.sheet_id = sheet_id
        self.tab_name = tab_name

    def fetch_rows(self) -> list[dict[str, Any]]:
        client = gspread.service_account(
            filename=str(self.credentials_file),
            scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"],
        )
        worksheet = client.open_by_key(self.sheet_id).worksheet(self.tab_name)
        return worksheet.get_all_records(
            default_blank="",
            numericise_ignore=["all"],
        )


class RuleStore:
    def __init__(self, cache_path: Path):
        self.cache_path = cache_path
        self._rules: dict[str, ContractRule] = {}

    def get(self, channel_id: str) -> ContractRule | None:
        rule = self._rules.get(channel_id)
        return rule if rule and rule.enabled else None

    def replace(self, rules: list[ContractRule]) -> None:
        replacement: dict[str, ContractRule] = {}
        for rule in rules:
            if rule.channel_id in replacement:
                raise ValueError(f"duplicate channel_id {rule.channel_id!r}")
            replacement[rule.channel_id] = rule
        self._persist(replacement)
        self._rules = replacement

    def load_cache(self) -> bool:
        if not self.cache_path.exists():
            return False
        try:
            payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
            rules = [ContractRule.model_validate(item) for item in payload["rules"]]
            replacement = {rule.channel_id: rule for rule in rules}
            if len(replacement) != len(rules):
                raise ValueError("cache contains duplicate channel IDs")
            self._rules = replacement
            logger.info("Loaded %d contract rules from cache", len(replacement))
            return True
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            logger.exception("Ignoring invalid contract rule cache at %s", self.cache_path)
            return False

    def _persist(self, rules: Mapping[str, ContractRule]) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.cache_path.with_suffix(self.cache_path.suffix + ".tmp")
        payload = {
            "rules": [
                rule.model_dump(mode="json")
                for rule in sorted(rules.values(), key=lambda item: item.channel_id)
            ]
        }
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.cache_path)


class SheetSynchronizer:
    def __init__(
        self,
        store: RuleStore,
        fetch_rows: Callable[[], list[dict[str, Any]]],
        refresh_seconds: float,
    ):
        self.store = store
        self.fetch_rows = fetch_rows
        self.refresh_seconds = refresh_seconds

    async def refresh_once(self) -> bool:
        try:
            rows = await asyncio.to_thread(self.fetch_rows)
            rules = parse_sheet_rows(rows)
            self.store.replace(rules)
            logger.info("Loaded %d contract rules from Google Sheets", len(rules))
            return True
        except Exception:
            logger.exception(
                "Google Sheet refresh failed; retaining the last valid rules"
            )
            return False

    async def run_forever(self) -> None:
        while True:
            await asyncio.sleep(self.refresh_seconds)
            await self.refresh_once()


def parse_sheet_rows(rows: list[dict[str, Any]]) -> list[ContractRule]:
    rules: list[ContractRule] = []
    seen: set[str] = set()
    for row_number, source in enumerate(rows, start=2):
        row = {
            str(key).strip().lower(): value
            for key, value in source.items()
            if str(key).strip()
        }
        if not any(str(value).strip() for value in row.values()):
            continue
        missing = SHEET_COLUMNS - row.keys()
        if missing:
            raise ValueError(
                f"row {row_number} is missing columns: {sorted(missing)}"
            )
        try:
            rule = ContractRule.model_validate(
                {column: row[column] for column in SHEET_COLUMNS}
            )
        except Exception as exc:
            raise ValueError(f"invalid sheet row {row_number}: {exc}") from exc
        if rule.channel_id in seen:
            raise ValueError(
                f"duplicate channel_id {rule.channel_id!r} at row {row_number}"
            )
        seen.add(rule.channel_id)
        rules.append(rule)
    return rules


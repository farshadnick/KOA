import json
from pathlib import Path

import pytest

from sheets import RuleStore, SheetSynchronizer, parse_sheet_rows


def sheet_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "enabled": "TRUE",
        "channel_id": "channel-1",
        "customer_name": "Acme",
        "timezone": "UTC",
        "support_days": "Mon,Tue,Wed,Thu,Fri",
        "open_time": "09:00",
        "close_time": "17:00",
        "support_24x7": "FALSE",
        "support_user_ids": "support-1,support-2",
        "closed_message": "Closed until {next_open_date} at {next_open_time}.",
    }
    row.update(overrides)
    return row


def test_parse_sheet_rows_normalizes_values() -> None:
    rules = parse_sheet_rows([sheet_row()])
    assert len(rules) == 1
    assert rules[0].channel_id == "channel-1"
    assert rules[0].support_days == frozenset({0, 1, 2, 3, 4})
    assert rules[0].support_user_ids == frozenset({"support-1", "support-2"})


def test_entire_sheet_is_rejected_for_bad_or_duplicate_rows() -> None:
    with pytest.raises(ValueError, match="row 3"):
        parse_sheet_rows([sheet_row(), sheet_row(channel_id="")])
    with pytest.raises(ValueError, match="duplicate channel_id"):
        parse_sheet_rows([sheet_row(), sheet_row()])
    with pytest.raises(ValueError, match="missing columns"):
        parse_sheet_rows([{"channel_id": "channel-1"}])


def test_rule_store_round_trips_cache(tmp_path: Path) -> None:
    cache = tmp_path / "rules.json"
    original = RuleStore(cache)
    original.replace(parse_sheet_rows([sheet_row()]))

    restored = RuleStore(cache)
    assert restored.load_cache()
    assert restored.get("channel-1") == original.get("channel-1")


def test_invalid_cache_is_ignored(tmp_path: Path) -> None:
    cache = tmp_path / "rules.json"
    cache.write_text(json.dumps({"rules": [{"bad": "data"}]}), encoding="utf-8")
    assert not RuleStore(cache).load_cache()


@pytest.mark.asyncio
async def test_refresh_replaces_rules_only_after_full_validation(
    tmp_path: Path,
) -> None:
    store = RuleStore(tmp_path / "rules.json")
    store.replace(parse_sheet_rows([sheet_row()]))

    synchronizer = SheetSynchronizer(
        store,
        lambda: [
            sheet_row(channel_id="channel-2"),
            sheet_row(channel_id=""),
        ],
        refresh_seconds=300,
    )
    assert not await synchronizer.refresh_once()
    assert store.get("channel-1") is not None
    assert store.get("channel-2") is None

    synchronizer.fetch_rows = lambda: [sheet_row(channel_id="channel-2")]
    assert await synchronizer.refresh_once()
    assert store.get("channel-1") is None
    assert store.get("channel-2") is not None


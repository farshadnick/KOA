from datetime import UTC, datetime

import pytest

from schedule import ContractRule, is_open, next_opening, render_closed_message


def rule(**overrides: object) -> ContractRule:
    values: dict[str, object] = {
        "channel_id": "channel-1",
        "customer_name": "Acme",
        "timezone": "UTC",
        "support_days": "Mon,Tue,Wed,Thu,Fri",
        "open_time": "09:00",
        "close_time": "17:00",
        "support_24x7": False,
        "support_user_ids": "support-1,support-2",
        "closed_message": (
            "{customer_name}: next opening is {next_open_date} "
            "{next_open_time} {timezone}"
        ),
    }
    values.update(overrides)
    return ContractRule.model_validate(values)


def test_normal_window_boundaries_and_weekend() -> None:
    contract = rule()
    assert is_open(contract, datetime(2026, 7, 20, 9, 0, tzinfo=UTC))
    assert is_open(contract, datetime(2026, 7, 20, 16, 59, tzinfo=UTC))
    assert not is_open(contract, datetime(2026, 7, 20, 17, 0, tzinfo=UTC))
    assert not is_open(contract, datetime(2026, 7, 19, 12, 0, tzinfo=UTC))


def test_overnight_window_uses_opening_day() -> None:
    contract = rule(
        support_days="Mon",
        open_time="18:00",
        close_time="02:00",
    )
    assert is_open(contract, datetime(2026, 7, 20, 23, 0, tzinfo=UTC))
    assert is_open(contract, datetime(2026, 7, 21, 1, 59, tzinfo=UTC))
    assert not is_open(contract, datetime(2026, 7, 21, 2, 0, tzinfo=UTC))


def test_24x7_contract_is_always_open() -> None:
    contract = rule(support_24x7=True)
    assert is_open(contract, datetime(2026, 7, 19, 2, 0, tzinfo=UTC))


def test_next_opening_skips_non_support_days_and_renders_message() -> None:
    contract = rule(support_days="Mon,Wed")
    instant = datetime(2026, 7, 20, 18, 0, tzinfo=UTC)
    opening = next_opening(contract, instant)
    assert opening.isoformat() == "2026-07-22T09:00:00+00:00"
    assert render_closed_message(contract, instant) == (
        "Acme: next opening is 2026-07-22 09:00 UTC"
    )


def test_timezone_conversion() -> None:
    contract = rule(timezone="Asia/Tehran")
    # 05:30 UTC is 09:00 in Tehran.
    assert is_open(contract, datetime(2026, 7, 20, 5, 30, tzinfo=UTC))


def test_nonexistent_dst_opening_is_normalized_forward() -> None:
    contract = rule(
        timezone="America/New_York",
        support_days="Sun",
        open_time="02:30",
        close_time="17:00",
    )
    instant = datetime(2025, 3, 9, 6, 0, tzinfo=UTC)  # 01:00 local
    opening = next_opening(contract, instant)
    assert opening.isoformat() == "2025-03-09T03:30:00-04:00"


def test_invalid_schedule_and_template_are_rejected() -> None:
    with pytest.raises(ValueError, match="cannot match"):
        rule(open_time="09:00", close_time="09:00")
    with pytest.raises(ValueError, match="unsupported placeholders"):
        rule(closed_message="Call us at {unknown}")


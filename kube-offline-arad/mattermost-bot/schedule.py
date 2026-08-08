from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from string import Formatter
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


DAY_NAMES = {
    "mon": 0,
    "monday": 0,
    "tue": 1,
    "tues": 1,
    "tuesday": 1,
    "wed": 2,
    "wednesday": 2,
    "thu": 3,
    "thur": 3,
    "thurs": 3,
    "thursday": 3,
    "fri": 4,
    "friday": 4,
    "sat": 5,
    "saturday": 5,
    "sun": 6,
    "sunday": 6,
}
ALLOWED_TEMPLATE_FIELDS = {
    "customer_name",
    "next_open_date",
    "next_open_time",
    "timezone",
}
DEFAULT_CLOSED_MESSAGE = (
    "We are currently closed. We will contact you when support reopens on "
    "{next_open_date} at {next_open_time} ({timezone})."
)


def parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"true", "yes", "y", "1"}:
        return True
    if normalized in {"false", "no", "n", "0", ""}:
        return False
    raise ValueError(f"expected a boolean, got {value!r}")


def parse_days(value: Any) -> frozenset[int]:
    if isinstance(value, (list, tuple, set, frozenset)):
        parts = list(value)
    else:
        parts = str(value).replace(";", ",").split(",")

    days: set[int] = set()
    for part in parts:
        if isinstance(part, int) and 0 <= part <= 6:
            days.add(part)
            continue
        normalized = str(part).strip().lower()
        if not normalized:
            continue
        if normalized in DAY_NAMES:
            days.add(DAY_NAMES[normalized])
            continue
        if normalized.isdigit() and 1 <= int(normalized) <= 7:
            days.add(int(normalized) - 1)
            continue
        raise ValueError(f"unknown support day {part!r}")
    if not days:
        raise ValueError("support_days must contain at least one day")
    return frozenset(days)


def parse_user_ids(value: Any) -> frozenset[str]:
    if isinstance(value, (list, tuple, set, frozenset)):
        parts = value
    else:
        parts = str(value or "").replace(";", ",").split(",")
    return frozenset(str(part).strip() for part in parts if str(part).strip())


class ContractRule(BaseModel):
    model_config = ConfigDict(frozen=True)

    enabled: bool = True
    channel_id: str = Field(min_length=1)
    customer_name: str = Field(min_length=1)
    timezone: str
    support_days: frozenset[int]
    open_time: time
    close_time: time
    support_24x7: bool = False
    support_user_ids: frozenset[str] = frozenset()
    closed_message: str = DEFAULT_CLOSED_MESSAGE

    @field_validator("enabled", "support_24x7", mode="before")
    @classmethod
    def validate_bool(cls, value: Any) -> bool:
        return parse_bool(value)

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        normalized = str(value).strip()
        try:
            ZoneInfo(normalized)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown IANA timezone {normalized!r}") from exc
        return normalized

    @field_validator("support_days", mode="before")
    @classmethod
    def validate_days(cls, value: Any) -> frozenset[int]:
        return parse_days(value)

    @field_validator("support_user_ids", mode="before")
    @classmethod
    def validate_users(cls, value: Any) -> frozenset[str]:
        return parse_user_ids(value)

    @field_validator("closed_message")
    @classmethod
    def validate_template(cls, value: str) -> str:
        message = str(value).strip() or DEFAULT_CLOSED_MESSAGE
        fields = {
            field_name
            for _, field_name, _, _ in Formatter().parse(message)
            if field_name
        }
        unknown = fields - ALLOWED_TEMPLATE_FIELDS
        if unknown:
            raise ValueError(
                f"closed_message has unsupported placeholders: {sorted(unknown)}"
            )
        return message

    @model_validator(mode="after")
    def validate_window(self) -> ContractRule:
        if not self.support_24x7 and self.open_time == self.close_time:
            raise ValueError(
                "open_time and close_time cannot match; use support_24x7 instead"
            )
        return self

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


def is_open(rule: ContractRule, instant: datetime) -> bool:
    if rule.support_24x7:
        return True
    local = _as_aware(instant).astimezone(rule.zone)
    current_time = local.time().replace(tzinfo=None)
    weekday = local.weekday()

    if rule.open_time < rule.close_time:
        return (
            weekday in rule.support_days
            and rule.open_time <= current_time < rule.close_time
        )

    previous_weekday = (weekday - 1) % 7
    return (
        weekday in rule.support_days and current_time >= rule.open_time
    ) or (
        previous_weekday in rule.support_days and current_time < rule.close_time
    )


def next_opening(rule: ContractRule, instant: datetime) -> datetime:
    if rule.support_24x7:
        return _as_aware(instant).astimezone(rule.zone)

    local = _as_aware(instant).astimezone(rule.zone)
    for offset in range(8):
        candidate_date = local.date() + timedelta(days=offset)
        if candidate_date.weekday() not in rule.support_days:
            continue
        candidate = _valid_local_datetime(candidate_date, rule.open_time, rule.zone)
        if candidate > local:
            return candidate
    raise ValueError("could not find the next support opening")


def render_closed_message(rule: ContractRule, instant: datetime) -> str:
    opening = next_opening(rule, instant)
    return rule.closed_message.format(
        customer_name=rule.customer_name,
        next_open_date=opening.date().isoformat(),
        next_open_time=opening.strftime("%H:%M"),
        timezone=rule.timezone,
    )


def _valid_local_datetime(day: date, at: time, zone: ZoneInfo) -> datetime:
    candidate = datetime.combine(day, at, tzinfo=zone)
    # Normalize nonexistent wall times during a daylight-saving transition.
    return candidate.astimezone(UTC).astimezone(zone)


def _as_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("schedule checks require a timezone-aware datetime")
    return value

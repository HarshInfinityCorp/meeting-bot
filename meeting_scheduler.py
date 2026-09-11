"""Persistent, UTC meeting scheduling primitives used by the Discord bot."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

UTC = timezone.utc


class ScheduleValidationError(ValueError):
    """Raised when a Discord-provided meeting time is invalid."""


def parse_utc_time(value: str) -> datetime:
    """Parse an ISO-8601 timestamp, treating a timezone-free value as UTC."""
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ScheduleValidationError(
            "Use ISO-8601 time, for example `2026-09-15 14:30` (UTC)."
        ) from exc

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


@dataclass(frozen=True)
class MeetingSchedule:
    guild_id: int
    voice_channel_id: int
    notification_channel_id: int
    start_at: datetime
    end_at: datetime

    @classmethod
    def create(
        cls,
        *,
        guild_id: int,
        voice_channel_id: int,
        notification_channel_id: int,
        start_time: str,
        end_time: str,
        now: datetime | None = None,
    ) -> MeetingSchedule:
        start_at = parse_utc_time(start_time)
        end_at = parse_utc_time(end_time)
        current_time = (now or datetime.now(UTC)).astimezone(UTC)

        if start_at <= current_time:
            raise ScheduleValidationError("The start time must be in the future.")
        if end_at <= start_at:
            raise ScheduleValidationError("The end time must be after the start time.")

        return cls(
            guild_id=guild_id,
            voice_channel_id=voice_channel_id,
            notification_channel_id=notification_channel_id,
            start_at=start_at,
            end_at=end_at,
        )

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["start_at"] = self.start_at.isoformat()
        result["end_at"] = self.end_at.isoformat()
        return result

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> MeetingSchedule:
        return cls(
            guild_id=int(value["guild_id"]),
            voice_channel_id=int(value["voice_channel_id"]),
            notification_channel_id=int(value["notification_channel_id"]),
            start_at=parse_utc_time(value["start_at"]),
            end_at=parse_utc_time(value["end_at"]),
        )


class MeetingStateStore:
    """Small JSON store with atomic writes so schedules survive bot restarts."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._state = self._load()

    def _load(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {"schedules": {}, "active": {}}
        try:
            with self.path.open(encoding="utf-8") as state_file:
                state = json.load(state_file)
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Cannot read meeting state file {self.path}: {exc}") from exc
        state.setdefault("schedules", {})
        state.setdefault("active", {})
        return state

    def _save(self) -> None:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", dir=self.path.parent, text=True
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as state_file:
                json.dump(self._state, state_file, indent=2, sort_keys=True)
                state_file.write("\n")
            os.replace(temporary_name, self.path)
        except BaseException:
            Path(temporary_name).unlink(missing_ok=True)
            raise

    def get_schedule(self, guild_id: int) -> MeetingSchedule | None:
        raw = self._state["schedules"].get(str(guild_id))
        return MeetingSchedule.from_dict(raw) if raw else None

    def schedules(self) -> list[MeetingSchedule]:
        return [MeetingSchedule.from_dict(raw) for raw in self._state["schedules"].values()]

    def replace_schedule(self, schedule: MeetingSchedule) -> None:
        self._state["schedules"][str(schedule.guild_id)] = schedule.to_dict()
        self._save()

    def remove_schedule(self, guild_id: int) -> None:
        self._state["schedules"].pop(str(guild_id), None)
        self._save()

    def get_active(self, guild_id: int) -> dict[str, Any] | None:
        return self._state["active"].get(str(guild_id))

    def set_active(self, guild_id: int, value: dict[str, Any]) -> None:
        self._state["active"][str(guild_id)] = value
        self._save()

    def clear_active(self, guild_id: int) -> None:
        self._state["active"].pop(str(guild_id), None)
        self._save()

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from meeting_scheduler import (
    IST,
    MeetingSchedule,
    MeetingStateStore,
    ScheduleValidationError,
    parse_meeting_time,
)


class MeetingSchedulerTests(unittest.TestCase):
    def test_timezone_free_input_is_ist(self):
        self.assertEqual(
            parse_meeting_time("2026-09-15 14:30"),
            datetime(2026, 9, 15, 14, 30, tzinfo=IST),
        )

    def test_timezone_aware_input_is_converted_to_ist(self):
        self.assertEqual(
            parse_meeting_time("2026-09-15T09:00:00Z"),
            datetime(2026, 9, 15, 14, 30, tzinfo=IST),
        )

    def test_end_must_follow_start(self):
        with self.assertRaises(ScheduleValidationError):
            MeetingSchedule.create(
                guild_id=1,
                voice_channel_id=2,
                notification_channel_id=3,
                start_time="2026-09-15 14:30",
                end_time="2026-09-15 14:30",
                now=datetime(2026, 9, 1, tzinfo=timezone.utc),
            )

    def test_replace_persists_only_one_schedule_per_guild(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            store = MeetingStateStore(path)
            first = MeetingSchedule.create(
                guild_id=1,
                voice_channel_id=2,
                notification_channel_id=3,
                start_time="2026-09-15 14:30",
                end_time="2026-09-15 15:30",
                now=datetime(2026, 9, 1, tzinfo=timezone.utc),
            )
            replacement = MeetingSchedule.create(
                guild_id=1,
                voice_channel_id=4,
                notification_channel_id=5,
                start_time="2026-09-16 14:30",
                end_time="2026-09-16 15:30",
                now=datetime(2026, 9, 1, tzinfo=timezone.utc),
            )
            store.replace_schedule(first)
            store.replace_schedule(replacement)

            restored = MeetingStateStore(path)
            self.assertEqual(len(restored.schedules()), 1)
            self.assertEqual(restored.get_schedule(1), replacement)


if __name__ == "__main__":
    unittest.main()

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from btc_timesfm.research.event_calendar import (
    CATEGORY_CPI,
    CATEGORY_FOMC,
    EVENT_CALENDAR_VERSION,
    CalendarEvent,
    EventCalendar,
    default_calendar,
)


def iso(year: int, month: int, day: int, hour: int = 0) -> str:
    return datetime(year, month, day, hour, tzinfo=timezone.utc).isoformat()


def event(
    event_id: str = "test-event",
    *,
    timestamp: str | None = None,
    known_scheduled: bool = True,
    category: str = CATEGORY_FOMC,
) -> dict[str, object]:
    return {
        "event_id": event_id,
        "name": "Test macro release",
        "category": category,
        "timestamp": timestamp or iso(2026, 1, 28, 19),
        "known_scheduled": known_scheduled,
        "source": "test-source-v1",
    }


class EventCalendarTests(unittest.TestCase):
    def test_default_calendar_is_versioned_and_reproducible(self) -> None:
        first = default_calendar()
        second = default_calendar()
        self.assertEqual(first.schema_version, EVENT_CALENDAR_VERSION)
        self.assertEqual(len(first.events), len(second.events))
        self.assertEqual([e.to_dict() for e in first.events], [e.to_dict() for e in second.events])
        self.assertEqual(first.validate(), [])
        self.assertGreaterEqual(len({event.event_id for event in first.events}), len(first.events))

    def test_default_events_are_utc_aware_and_sorted(self) -> None:
        calendar = default_calendar()
        timestamps = [event.timestamp for event in calendar.events]
        self.assertTrue(all(event.timestamp.tzinfo is not None for event in calendar.events))
        self.assertEqual(timestamps, sorted(timestamps))
        self.assertTrue(all(event.known_scheduled for event in calendar.events))

    def test_defaults_cover_cpi_and_fomc(self) -> None:
        categories = {event.category for event in default_calendar().events}
        self.assertIn(CATEGORY_CPI, categories)
        self.assertIn(CATEGORY_FOMC, categories)
        cpi_events = [e for e in default_calendar().events if e.category == CATEGORY_CPI]
        self.assertTrue(all(e.timestamp.hour == 13 for e in cpi_events))

    def test_add_custom_event_and_unscheduled_classification(self) -> None:
        calendar = EventCalendar()
        unscheduled = event("emergency-fed", timestamp=iso(2026, 3, 1, 12), known_scheduled=False)
        scheduled = event("nfp-2026-04", timestamp=iso(2026, 4, 3, 13))
        calendar.add_events([unscheduled, scheduled])
        self.assertEqual(len(calendar.events), 2)
        self.assertFalse(calendar.by_id("emergency-fed").known_scheduled)
        self.assertTrue(calendar.by_id("nfp-2026-04").known_scheduled)
        self.assertEqual(calendar.events[0].event_id, "emergency-fed")

    def test_duplicate_event_id_is_rejected(self) -> None:
        calendar = EventCalendar([event("same")])
        with self.assertRaises(ValueError):
            calendar.add_event(event("same"))
        with self.assertRaises(ValueError):
            EventCalendar([event("same"), event("same")])

    def test_json_round_trip_preserves_events_and_version(self) -> None:
        calendar = default_calendar()
        calendar.add_event(event("nfp-custom", timestamp=iso(2026, 5, 1, 13)))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "calendar.json"
            calendar.to_json(path)
            loaded = EventCalendar.from_json(path)
        self.assertEqual(
            [e.to_dict() for e in loaded.events], [e.to_dict() for e in calendar.events]
        )
        self.assertEqual(loaded.schema_version, calendar.schema_version)
        self.assertIsNotNone(loaded.by_id("nfp-custom"))

    def test_loads_events_from_plain_json_payload(self) -> None:
        payload = {"schema_version": EVENT_CALENDAR_VERSION, "events": [event("custom")]}
        calendar = EventCalendar.from_dict(payload)
        self.assertEqual(calendar.by_id("custom").event_id, "custom")

    def test_rejects_malformed_timestamps_and_payloads(self) -> None:
        with self.assertRaises(ValueError):
            EventCalendar([event("naive", timestamp="2026-01-01T00:00:00")])
        with self.assertRaises(ValueError):
            EventCalendar([event("garbage", timestamp="not-a-timestamp")])
        with self.assertRaises(ValueError):
            EventCalendar(["not-a-dict"])
        with self.assertRaises(ValueError):
            EventCalendar.from_dict({"version": 1})
        with self.assertRaises(ValueError):
            EventCalendar.from_dict({"schema_version": 0, "events": []})

    def test_timezone_with_z_and_offset_is_normalized_to_utc(self) -> None:
        zulu = EventCalendar([event("z", timestamp="2026-01-01T00:00:00Z")])
        self.assertEqual(zulu.by_id("z").timestamp, datetime(2026, 1, 1, tzinfo=timezone.utc))
        offset = EventCalendar([event("off", timestamp="2026-01-01T05:30:00+05:30")])
        self.assertEqual(offset.by_id("off").timestamp, datetime(2026, 1, 1, tzinfo=timezone.utc))

    def test_nearest_event_returns_signed_offset_and_tie_breaks_to_earlier(self) -> None:
        calendar = EventCalendar(
            [event("e1", timestamp=iso(2026, 3, 5, 12)), event("e2", timestamp=iso(2026, 3, 5, 18))]
        )
        future = calendar.nearest_event(datetime(2026, 3, 5, 6, tzinfo=timezone.utc))
        self.assertEqual(future[0].event_id, "e1")
        self.assertGreater(future[1].total_seconds(), 0)
        past = calendar.nearest_event(datetime(2026, 3, 5, 20, tzinfo=timezone.utc))
        self.assertEqual(past[0].event_id, "e2")
        self.assertLess(past[1].total_seconds(), 0)
        tied = calendar.nearest_event(datetime(2026, 3, 5, 15, tzinfo=timezone.utc))
        self.assertEqual(tied[0].event_id, "e1")

    def test_events_between_is_inclusive_exclusive(self) -> None:
        calendar = EventCalendar([event("early", timestamp=iso(2026, 4, 1, 9))])
        self.assertEqual(
            len(
                calendar.events_between(
                    datetime(2026, 4, 1, 9, tzinfo=timezone.utc),
                    datetime(2026, 4, 1, 10, tzinfo=timezone.utc),
                )
            ),
            1,
        )
        self.assertEqual(
            len(
                calendar.events_between(
                    datetime(2026, 4, 1, 10, tzinfo=timezone.utc),
                    datetime(2026, 4, 1, 11, tzinfo=timezone.utc),
                )
            ),
            0,
        )

    def test_accepts_calendar_event_instances_directly(self) -> None:
        parsed = CalendarEvent.from_dict(event("direct"))
        calendar = EventCalendar([parsed])
        self.assertEqual(calendar.by_id("direct").name, "Test macro release")
        self.assertTrue(json.dumps(parsed.to_dict()))


if __name__ == "__main__":
    unittest.main()

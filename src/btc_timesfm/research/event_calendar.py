#!/usr/bin/env python3
"""Timestamp-safe, versioned calendar of scheduled macro-release events.

The calendar records *when* a macroeconomic release is scheduled, never what it
contains. Release calendars (US CPI, FOMC meeting dates, ...) are published well
in advance, so the timestamps here are public information before any forecast is
made. Classifying historical forecasts by event proximity is therefore
reproducible and cannot smuggle an event outcome into a pre-event evaluation.

Each event is classified as one of:

* ``known_scheduled`` -- the exact release/decision time is fixed and announced
  ahead of the release (e.g. FOMC statement dates, the BLS CPI release
  schedule).
* ``known_scheduled=False`` -- the release time was not fixed in advance (e.g.
  an emergency meeting or an ad-hoc data revision).

The curated defaults are embedded under a versioned schema and can be extended,
replaced, or corrected by loading a JSON file or adding custom events, so the
calendar stays reproducible across runs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

EVENT_CALENDAR_VERSION = 1
CATEGORY_CPI = "cpi"
CATEGORY_FOMC = "fomc"

# Curated, versioned release dates.
#
# CPI dates follow the published BLS release schedule for the prior reference
# month, released at 08:30 ET (13:30 UTC). FOMC dates are the official meeting
# dates; the statement comes at 14:00 ET (19:00 UTC in winter, 18:00 UTC during
# US daylight saving time).
_CPI_RELEASES_2025 = (
    (2025, 1, 14),
    (2025, 2, 12),
    (2025, 3, 12),
    (2025, 4, 10),
    (2025, 5, 13),
    (2025, 6, 11),
    (2025, 7, 15),
    (2025, 8, 12),
    (2025, 9, 11),
    (2025, 10, 14),
    (2025, 11, 12),
    (2025, 12, 10),
)
_FOMC_STATEMENTS = (
    (2025, 1, 29, 19),
    (2025, 3, 19, 18),
    (2025, 5, 7, 18),
    (2025, 6, 18, 18),
    (2025, 7, 30, 18),
    (2025, 9, 17, 18),
    (2025, 10, 29, 18),
    (2025, 12, 10, 19),
    (2026, 1, 28, 19),
    (2026, 3, 18, 18),
    (2026, 4, 29, 18),
    (2026, 6, 17, 18),
    (2026, 7, 29, 18),
    (2026, 9, 16, 18),
    (2026, 10, 28, 18),
    (2026, 12, 9, 19),
)


def _curated_default_events() -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for year, month, day in _CPI_RELEASES_2025:
        timestamp = datetime(year, month, day, 13, 30, tzinfo=timezone.utc)
        events.append(
            {
                "event_id": f"cpi-{year}-{month:02d}-{day:02d}",
                "name": "US CPI release",
                "category": CATEGORY_CPI,
                "timestamp": timestamp.isoformat(),
                "known_scheduled": True,
                "source": "us_cpi_release_schedule_2025",
                "notes": "BLS CPI for the prior reference month; 08:30 ET.",
            }
        )
    for year, month, day, hour in _FOMC_STATEMENTS:
        timestamp = datetime(year, month, day, hour, 0, tzinfo=timezone.utc)
        events.append(
            {
                "event_id": f"fomc-{year}-{month:02d}-{day:02d}",
                "name": "FOMC statement",
                "category": CATEGORY_FOMC,
                "timestamp": timestamp.isoformat(),
                "known_scheduled": True,
                "source": f"us_fomc_meeting_schedule_{year}",
                "notes": "FOMC policy statement and decision; 14:00 ET.",
            }
        )
    return events


DEFAULT_EVENTS = _curated_default_events()


def _parse_event_timestamp(value: Any) -> datetime:
    """Parse an ISO-8601 event timestamp that must be explicit UTC."""
    if not isinstance(value, str) or not value:
        raise ValueError("event timestamp must be a non-empty ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"event timestamp is not valid ISO-8601: {value!r}") from exc
    if parsed.tzinfo is None:
        raise ValueError("event timestamps must be timezone-aware; use +00:00 or Z suffix")
    return parsed.astimezone(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class CalendarEvent:
    """One scheduled macro release with a fixed, versioned UTC timestamp."""

    event_id: str
    name: str
    category: str
    timestamp: datetime
    known_scheduled: bool
    source: str
    notes: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "name": self.name,
            "category": self.category,
            "timestamp": self.timestamp.isoformat(),
            "known_scheduled": self.known_scheduled,
            "source": self.source,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "CalendarEvent":
        event_id = raw.get("event_id")
        name = raw.get("name")
        category = raw.get("category")
        source = raw.get("source")
        if not isinstance(event_id, str) or not event_id:
            raise ValueError("event_id must be a non-empty string")
        if not isinstance(name, str) or not name:
            raise ValueError("name must be a non-empty string")
        if not isinstance(category, str) or not category:
            raise ValueError("category must be a non-empty string")
        if not isinstance(source, str) or not source:
            raise ValueError("source must be a non-empty string")
        known_scheduled = raw.get("known_scheduled")
        if not isinstance(known_scheduled, bool):
            raise ValueError("known_scheduled must be a boolean")
        notes = raw.get("notes")
        if notes is not None and not isinstance(notes, str):
            raise ValueError("notes must be a string or null")
        return cls(
            event_id=event_id,
            name=name,
            category=category,
            timestamp=_parse_event_timestamp(raw.get("timestamp")),
            known_scheduled=known_scheduled,
            source=source,
            notes=notes,
        )


class EventCalendar:
    """A versioned, loadable, and extendable macro-release calendar."""

    def __init__(
        self,
        events: Iterable[Any] | None = None,
        *,
        schema_version: int = EVENT_CALENDAR_VERSION,
    ) -> None:
        self.schema_version = schema_version
        self._events: list[CalendarEvent] = []
        if events is not None:
            self.add_events(list(events))

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "EventCalendar":
        raw_events = payload.get("events")
        if not isinstance(raw_events, list):
            raise ValueError("calendar payload must contain an events list")
        version = payload.get("schema_version", EVENT_CALENDAR_VERSION)
        if not isinstance(version, int) or version < 1:
            raise ValueError("schema_version must be a positive integer")
        return cls(
            events=[item for item in raw_events if isinstance(item, dict)], schema_version=version
        )

    @classmethod
    def from_json(cls, path: Path | str) -> "EventCalendar":
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("event calendar JSON must be an object with an events list")
        return cls.from_dict(payload)

    def add_event(self, event: Any) -> None:
        if isinstance(event, CalendarEvent):
            parsed = event
        elif isinstance(event, dict):
            parsed = CalendarEvent.from_dict(event)
        else:
            raise ValueError("event must be a CalendarEvent or a dict")
        if any(existing.event_id == parsed.event_id for existing in self._events):
            raise ValueError(f"duplicate event_id: {parsed.event_id}")
        self._events.append(parsed)

    def add_events(self, events: Iterable[Any]) -> None:
        for event in events:
            self.add_event(event)

    @property
    def events(self) -> list[CalendarEvent]:
        return sorted(self._events, key=lambda event: (event.timestamp, event.event_id))

    def event_ids(self) -> list[str]:
        return [event.event_id for event in self.events]

    def by_id(self, event_id: str) -> CalendarEvent | None:
        return next((event for event in self._events if event.event_id == event_id), None)

    def events_between(self, start: datetime, end: datetime) -> list[CalendarEvent]:
        start_utc = _as_utc(start)
        end_utc = _as_utc(end)
        return [event for event in self.events if start_utc <= event.timestamp < end_utc]

    def nearest_event(self, timestamp: datetime) -> tuple[CalendarEvent, timedelta] | None:
        """Return the nearest event and its signed offset from ``timestamp``.

        A positive offset means the event is still in the future; a negative
        offset means it already happened. Ties resolve to the earlier event.
        """
        origin = _as_utc(timestamp)
        best: tuple[CalendarEvent, timedelta] | None = None
        for event in self._events:
            delta = event.timestamp - origin
            if best is None:
                best = (event, delta)
                continue
            best_event, best_delta = best
            if abs(delta.total_seconds()) < abs(best_delta.total_seconds()) or (
                abs(delta.total_seconds()) == abs(best_delta.total_seconds())
                and event.timestamp < best_event.timestamp
            ):
                best = (event, delta)
        return best

    def validate(self) -> list[str]:
        issues: list[str] = []
        seen: set[str] = set()
        for event in self.events:
            if event.event_id in seen:
                issues.append(f"duplicate event_id: {event.event_id}")
            seen.add(event.event_id)
        return issues

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_count": len(self.events),
            "events": [event.to_dict() for event in self.events],
        }

    def to_json(self, path: Path | str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(
            json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )


def default_calendar() -> EventCalendar:
    """Return a fresh calendar built from the embedded curated defaults."""
    return EventCalendar(DEFAULT_EVENTS)

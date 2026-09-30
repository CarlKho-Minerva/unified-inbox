import datetime
import json

import pytest

from unified_inbox.calendars import (
    CalendarUnavailableError,
    fetch_pushed_events,
    _ics_field,
    _normalize_google_event,
    _parse_zoho_ics,
    _to_epoch,
)


def _utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> float:
    return datetime.datetime(year, month, day, hour, minute, tzinfo=datetime.timezone.utc).timestamp()


# --- _to_epoch: the three input shapes it has to handle ---


def test_to_epoch_parses_iso_datetime_with_offset() -> None:
    assert _to_epoch("2026-07-19T14:00:00+00:00", is_date=False) == _utc(2026, 7, 19, 14)


def test_to_epoch_parses_ics_zulu_datetime() -> None:
    assert _to_epoch("20260719T140000Z", is_date=False) == _utc(2026, 7, 19, 14)


def test_to_epoch_parses_all_day_date_both_spellings() -> None:
    assert _to_epoch("2026-07-19", is_date=True) == _utc(2026, 7, 19)
    assert _to_epoch("20260719", is_date=True) == _utc(2026, 7, 19)


def test_to_epoch_returns_a_time_for_garbage_rather_than_raising() -> None:
    assert isinstance(_to_epoch("not a date", is_date=False), float)


# --- ICS parsing ---


def test_ics_field_reads_a_value_ignoring_params() -> None:
    block = "SUMMARY:Standup\nDTSTART;TZID=UTC:20260719T140000Z\n"
    assert _ics_field(block, "SUMMARY") == "Standup"
    assert _ics_field(block, "DTSTART") == "20260719T140000Z"
    assert _ics_field(block, "MISSING") == ""


_SAMPLE_ICS = (
    "BEGIN:VCALENDAR\r\n"
    "BEGIN:VEVENT\r\n"
    "UID:evt-1@zoho\r\n"
    "SUMMARY:Timed meeting\r\n"
    "DTSTART:20260719T140000Z\r\n"
    "DTEND:20260719T150000Z\r\n"
    "LOCATION:Room 5\r\n"
    "END:VEVENT\r\n"
    "BEGIN:VEVENT\r\n"
    "UID:evt-2@zoho\r\n"
    "SUMMARY:All day thing\r\n"
    "DTSTART;VALUE=DATE:20260720\r\n"
    "END:VEVENT\r\n"
    "BEGIN:VEVENT\r\n"
    "SUMMARY:No start so skipped\r\n"
    "END:VEVENT\r\n"
    "END:VCALENDAR\r\n"
)


def test_parse_zoho_ics_extracts_events_and_skips_ones_without_a_start() -> None:
    events = _parse_zoho_ics(_SAMPLE_ICS)
    assert [e["title"] for e in events] == ["Timed meeting", "All day thing"]
    timed, all_day = events
    assert timed["id"] == "zoho:evt-1@zoho"
    assert timed["all_day"] is False
    assert timed["start"] == _utc(2026, 7, 19, 14)
    assert timed["end"] == _utc(2026, 7, 19, 15)
    assert timed["location"] == "Room 5"
    assert all_day["all_day"] is True
    assert all_day["start"] == _utc(2026, 7, 20)


def test_parse_zoho_ics_on_empty_body() -> None:
    assert _parse_zoho_ics("") == []


# --- Google normalization ---


def test_normalize_google_event_timed() -> None:
    e = {
        "id": "abc",
        "summary": "Sprint review",
        "start": {"dateTime": "2026-07-19T14:00:00+00:00"},
        "end": {"dateTime": "2026-07-19T15:00:00+00:00"},
        "location": "HQ",
        "htmlLink": "https://calendar.google.com/event?eid=abc",
    }
    out = _normalize_google_event("cal@x", "Work", "#2f6bff", e)
    assert out["id"] == "gcal:cal@x:abc"
    assert out["calendar"] == "Work"
    assert out["all_day"] is False
    assert out["start"] == _utc(2026, 7, 19, 14)
    assert out["color"] == "#2f6bff"
    assert out["url"] == "https://calendar.google.com/event?eid=abc"


def test_normalize_google_event_all_day_and_missing_fields() -> None:
    e = {"id": "d1", "start": {"date": "2026-07-20"}, "end": {"date": "2026-07-21"}}
    out = _normalize_google_event("cal@x", "Work", "#123456", e)
    assert out["all_day"] is True
    assert out["title"] == "(no title)"
    assert out["location"] == ""
    assert out["url"] == "https://calendar.google.com/"


def test_pushed_events_fresh_and_stale(tmp_path) -> None:
    p = tmp_path / "pushed_events.json"
    p.write_text(json.dumps({"pushed_at": 1000.0, "events": [{"title": "x", "start": 2000.0}]}))
    assert fetch_pushed_events(p, now=1100.0)[0]["title"] == "x"
    with pytest.raises(CalendarUnavailableError, match="min old"):
        fetch_pushed_events(p, now=1000.0 + 7200)

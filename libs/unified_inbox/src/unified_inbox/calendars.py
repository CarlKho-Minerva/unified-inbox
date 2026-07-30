"""Fetch upcoming events from Google Calendar (all shown calendars) and Zoho
Calendar (via CalDAV with the stored app password), normalized into one shape:

  {id, calendar, title, start (epoch), end (epoch), all_day, location, url, color}
"""

import base64
import datetime
import json
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path

# A JSON fetcher takes a URL and returns the parsed body (or None on failure).
JsonFetcher = Callable[[str], dict | list | None]

MAIL_ACCOUNTS = Path("runtime/mail/accounts.json")
WINDOW_DAYS = 45  # how far ahead the agenda looks
ZOHO_EVENTS_URL = "https://calendar.zoho.com/caldav/YOUR_ZOHO_CALDAV_ACCOUNT_ID/events/"

# Distinct fallback colors for Google calendars that don't report one.
_FALLBACK = ["#2f6bff", "#12a150", "#9333ea", "#e0533d", "#0891b2", "#c9820a", "#be185d"]


def _lk_json(url: str) -> dict | list | None:
    out = subprocess.run(["latchkey", "curl", "-s", url], capture_output=True, text=True, timeout=60)
    if not out.stdout.strip():
        return None
    try:
        return json.loads(out.stdout)
    except json.JSONDecodeError:
        return None


def _to_epoch(value: str, is_date: bool) -> float:
    """Parse an ISO datetime (Google) or ICS datetime/date to epoch seconds."""
    try:
        if is_date:  # all-day: YYYY-MM-DD or YYYYMMDD
            v = value.replace("-", "")
            return datetime.datetime.strptime(v[:8], "%Y%m%d").replace(
                tzinfo=datetime.timezone.utc
            ).timestamp()
        if "T" in value and value[0].isdigit() and "-" not in value[:8]:
            # ICS form: 20260719T140000Z or 20260719T140000
            fmt = "%Y%m%dT%H%M%SZ" if value.endswith("Z") else "%Y%m%dT%H%M%S"
            dt = datetime.datetime.strptime(value, fmt)
            if value.endswith("Z"):
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            else:
                dt = dt.replace(tzinfo=datetime.timezone.utc)  # treat floating as UTC
            return dt.timestamp()
        return datetime.datetime.fromisoformat(value).timestamp()
    except (ValueError, TypeError):
        return time.time()


# --------------------------------------------------------------------------
# Google Calendar (via latchkey)
# --------------------------------------------------------------------------
def _normalize_google_event(cid: str, name: str, color: str, e: dict) -> dict:
    start = e.get("start", {})
    end = e.get("end", {})
    is_date = "date" in start
    return {
        "id": f"gcal:{cid}:{e.get('id')}",
        "calendar": name,
        "title": e.get("summary", "(no title)"),
        "start": _to_epoch(start.get("dateTime") or start.get("date", ""), is_date),
        "end": _to_epoch(end.get("dateTime") or end.get("date", ""), is_date),
        "all_day": is_date,
        "location": e.get("location", ""),
        "url": e.get("htmlLink", "https://calendar.google.com/"),
        "color": color,
    }


def fetch_google_events(get_json: JsonFetcher = _lk_json) -> list[dict]:
    cal_list = get_json("https://www.googleapis.com/calendar/v3/users/me/calendarList?maxResults=50")
    if not isinstance(cal_list, dict) or "items" not in cal_list:
        return []
    now = datetime.datetime.now(datetime.timezone.utc)
    time_min = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    time_max = (now + datetime.timedelta(days=WINDOW_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = []
    for idx, cal in enumerate(cal_list["items"]):
        if cal.get("selected") is False:
            continue  # respect the calendars the user has hidden
        cid = cal["id"]
        color = cal.get("backgroundColor") or _FALLBACK[idx % len(_FALLBACK)]
        name = cal.get("summaryOverride") or cal.get("summary", "Calendar")
        events = get_json(
            f"https://www.googleapis.com/calendar/v3/calendars/{urllib.parse.quote(cid)}/events"
            f"?maxResults=50&singleEvents=true&orderBy=startTime&timeMin={time_min}&timeMax={time_max}"
        )
        if not isinstance(events, dict):
            continue
        out.extend(_normalize_google_event(cid, name, color, e) for e in events.get("items", []))
    return out


# --------------------------------------------------------------------------
# Zoho Calendar (via CalDAV, using the stored app password)
# --------------------------------------------------------------------------
def _zoho_auth() -> str | None:
    if not MAIL_ACCOUNTS.exists():
        return None
    accts = {a["email"]: a for a in json.loads(MAIL_ACCOUNTS.read_text())}
    z = accts.get("you@yourdomain.example")
    if not z:
        return None
    return base64.b64encode(f"{z['email']}:{z['password']}".encode()).decode()


def _ics_field(block: str, name: str) -> str:
    m = re.search(rf"^{name}(?:;[^:\n]*)?:(.+)$", block, re.MULTILINE)
    return m.group(1).strip() if m else ""


def _parse_zoho_ics(body: str) -> list[dict]:
    """Parse a CalDAV multistatus body into normalized events. Split on VEVENT
    boundaries and pull the fields we surface; skip blocks with no DTSTART."""
    out = []
    for block in body.split("BEGIN:VEVENT")[1:]:
        block = block.split("END:VEVENT")[0]
        dtstart = _ics_field(block, "DTSTART")
        if not dtstart:
            continue
        is_date = "VALUE=DATE" in block.split("DTSTART")[1].split(":")[0] if "DTSTART" in block else False
        out.append(
            {
                "id": f"zoho:{_ics_field(block, 'UID') or dtstart}",
                "calendar": "Zoho",
                "title": _ics_field(block, "SUMMARY") or "(no title)",
                "start": _to_epoch(dtstart, is_date),
                "end": _to_epoch(_ics_field(block, "DTEND") or dtstart, is_date),
                "all_day": is_date,
                "location": _ics_field(block, "LOCATION"),
                "url": "https://calendar.zoho.com/",
                "color": "#fb7185",
            }
        )
    return out


def fetch_zoho_events() -> list[dict]:
    auth = _zoho_auth()
    if not auth:
        return []
    now = datetime.datetime.now(datetime.timezone.utc)
    start = (now - datetime.timedelta(days=1)).strftime("%Y%m%dT%H%M%SZ")
    end = (now + datetime.timedelta(days=WINDOW_DAYS)).strftime("%Y%m%dT%H%M%SZ")
    report = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
        "<d:prop><c:calendar-data/></d:prop>"
        '<c:filter><c:comp-filter name="VCALENDAR"><c:comp-filter name="VEVENT">'
        f'<c:time-range start="{start}" end="{end}"/>'
        "</c:comp-filter></c:comp-filter></c:filter></c:calendar-query>"
    )
    req = urllib.request.Request(
        ZOHO_EVENTS_URL,
        data=report.encode(),
        method="REPORT",
        headers={"Authorization": f"Basic {auth}", "Depth": "1", "Content-Type": "application/xml; charset=utf-8"},
    )
    try:
        body = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError):
        return []
    return _parse_zoho_ics(body)


def fetch_all_events() -> tuple[list[dict], dict]:
    """Fetch both calendars, merge, sort by start. Returns (events, status)."""
    status = {}
    events = []
    for key, fn in [("google_calendar", fetch_google_events), ("zoho_calendar", fetch_zoho_events)]:
        try:
            evs = fn()
            events.extend(evs)
            status[key] = {"ok": True, "count": len(evs)}
        except Exception as exc:  # noqa: BLE001 - record and continue
            status[key] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    events.sort(key=lambda e: e.get("start", 0))
    return events, status

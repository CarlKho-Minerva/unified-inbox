"""Fetch upcoming San Francisco events from Luma's public discover API and
normalize them for the "Luma SF" view.

Luma's discover feed is a public, unauthenticated endpoint on api.luma.com
(the legacy api.lu.ma host 404s these paths). Network access is injected via
``get_json`` so the normalization can be tested against fixtures without a
network, matching the pattern used elsewhere in this service.
"""

import datetime
import html
import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

JsonFetcher = Callable[[str], dict | None]

# slug=sf self-scopes to San Francisco (place slugs need no lat/lng).
LUMA_SF_URL = "https://api.luma.com/discover/get-paginated-events?slug=sf"
# the user's registered events (needs his session cookie). guest_info.approval_status
# gives the RSVP state we surface as a filter.
LUMA_HOME_URL = "https://api.luma.com/home/get-events"
LUMA_TOKEN_FILE = Path("runtime/mail/luma_token")

# Map Luma's raw approval_status to the user-facing filter buckets.
_STATUS_MAP = {
    "approved": "going",
    "pending_approval": "pending",
    "waitlist": "pending",
    "invited": "invited",
}


def _load_luma_token() -> str:
    try:
        return LUMA_TOKEN_FILE.read_text().strip()
    except OSError:
        return ""


def _cookie_json(url: str, token: str) -> dict | None:
    req = urllib.request.Request(
        url,
        headers={"accept": "application/json", "user-agent": "Mozilla/5.0",
                 "cookie": f"luma.auth-session-key={token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError):
        return None


def _http_json(url: str) -> dict | None:
    req = urllib.request.Request(url, headers={"accept": "application/json", "user-agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError):
        return None


def _epoch(iso: str) -> float:
    try:
        return datetime.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, AttributeError):
        return 0.0


def _normalize(entry: dict, status: str = "discover") -> dict | None:
    e = entry.get("event") or {}
    if not e.get("name"):
        return None
    geo = e.get("geo_address_info") or {}
    location = (
        "Virtual"
        if e.get("location_type") == "virtual"
        else (geo.get("sublocality") or geo.get("city_state") or geo.get("city") or "San Francisco")
    )
    slug = e.get("url", "")
    return {
        "id": f"luma:{e.get('api_id', slug)}",
        "name": e.get("name", "(untitled)"),
        "start": _epoch(e.get("start_at", "")),
        "end": _epoch(e.get("end_at", "")),
        "cover": e.get("cover_url") or e.get("social_image_url") or "",
        "url": f"https://lu.ma/{slug}" if slug else "https://lu.ma/sf",
        "location": location,
        "status": status,  # going | pending | invited | discover
    }


def fetch_my_luma_events(token: str, get_cookie_json: Callable[[str, str], dict | None] = _cookie_json) -> list[dict]:
    """the user's registered/invited events, tagged with their RSVP status. Empty if
    no token or the session expired. Injectable for tests."""
    if not token:
        return []
    data = get_cookie_json(LUMA_HOME_URL, token)
    if not isinstance(data, dict) or "entries" not in data:
        return []
    out = []
    for entry in data["entries"]:
        raw = ((entry.get("guest_info") or {}).get("approval_status")) or ""
        ev = _normalize(entry, _STATUS_MAP.get(raw, "invited"))
        if ev:
            out.append(ev)
    return out


def fetch_luma_sf(get_json: JsonFetcher = _http_json) -> list[dict]:
    data = get_json(LUMA_SF_URL)
    if not isinstance(data, dict) or "entries" not in data:
        return []
    events = [ev for ev in (_normalize(x) for x in data["entries"]) if ev]
    events.sort(key=lambda ev: ev.get("start", 0))
    return events


# Inline marks Luma's rich-text editor applies to text nodes -> HTML wrappers.
_PM_MARKS = {"bold": ("<strong>", "</strong>"), "italic": ("<em>", "</em>"),
             "strike": ("<s>", "</s>"), "code": ("<code>", "</code>")}


def _pm_href(mark: dict) -> str:
    """A sanitized href from a link mark (only http/https/mailto survive)."""
    href = ((mark.get("attrs") or {}).get("href")) or ""
    return href if href[:7] in ("http://", "mailto:") or href[:8] == "https://" else ""


def _pm_text(node: dict) -> str:
    """One text node with its marks applied, HTML-escaped."""
    out = html.escape(node.get("text") or "")
    link = ""
    for mark in node.get("marks") or []:
        mt = mark.get("type")
        if mt == "link":
            link = _pm_href(mark)
        elif mt in _PM_MARKS:
            open_tag, close_tag = _PM_MARKS[mt]
            out = f"{open_tag}{out}{close_tag}"
    if link:
        out = f'<a href="{html.escape(link)}" target="_blank" rel="noopener">{out}</a>'
    return out


def _pm_inline(nodes: list) -> str:
    """The inline content of a block node (text + hard breaks)."""
    parts = []
    for n in nodes or []:
        if n.get("type") == "text":
            parts.append(_pm_text(n))
        elif n.get("type") == "hardBreak":
            parts.append("<br>")
    return "".join(parts)


def _pm_node(node: dict) -> str:
    """Render one ProseMirror block node to HTML (recursively)."""
    t = node.get("type")
    kids = node.get("content") or []
    if t == "paragraph":
        inner = _pm_inline(kids)
        return f"<p>{inner}</p>" if inner else ""
    if t == "heading":
        level = min(3, max(1, ((node.get("attrs") or {}).get("level")) or 1)) + 2  # h1->h3, keep panel scale
        return f"<h{level}>{_pm_inline(kids)}</h{level}>"
    if t in ("bulletList", "orderedList"):
        tag = "ul" if t == "bulletList" else "ol"
        return f"<{tag}>{''.join(_pm_node(c) for c in kids)}</{tag}>"
    if t == "listItem":
        return f"<li>{''.join(_pm_node(c) for c in kids)}</li>"
    if t == "blockquote":
        return f"<blockquote>{''.join(_pm_node(c) for c in kids)}</blockquote>"
    if t == "horizontalRule":
        return "<hr>"
    if t == "image":
        src = ((node.get("attrs") or {}).get("src")) or ""
        return f'<img src="{html.escape(src)}" alt="" loading="lazy">' if src.startswith("https://") else ""
    # Unknown block: fall back to its inline text so nothing is silently dropped.
    return f"<p>{_pm_inline(kids)}</p>" if kids and kids[0].get("type") == "text" else ""


def _prosemirror_to_html(doc: object) -> str:
    """Convert Luma's ``description_mirror`` rich-text doc into safe HTML. Returns
    empty string for a missing/malformed doc."""
    if not isinstance(doc, dict) or doc.get("type") != "doc":
        return ""
    return "".join(_pm_node(n) for n in (doc.get("content") or []) if isinstance(n, dict))


def _authed_or_public_json(url: str) -> dict | None:
    """Use the user's session cookie when present (so private/invited events resolve),
    else the plain public fetch."""
    token = _load_luma_token()
    return _cookie_json(url, token) if token else _http_json(url)


def fetch_luma_event(event_id: str, get_json: JsonFetcher = _authed_or_public_json) -> dict:
    """Fetch a single event's full detail (hosts, guest count, location, calendar,
    and the complete prose description) for the detail panel. The description
    comes from Luma's ``description_mirror`` rich-text doc, rendered to HTML."""
    eid = event_id.split(":", 1)[-1] if ":" in event_id else event_id
    data = get_json(f"https://api.luma.com/event/get?event_api_id={eid}")
    if not isinstance(data, dict):
        return {}
    ev = data.get("event") or {}
    geo = ev.get("geo_address_info") or {}
    cal = data.get("calendar") or {}
    hosts = [h.get("name") for h in (data.get("hosts") or []) if h.get("name")]
    location = (
        geo.get("full_address") or geo.get("address") or geo.get("city_state") or geo.get("city")
        or ("Online" if ev.get("location_type") == "virtual" else "")
    )
    description_html = _prosemirror_to_html(data.get("description_mirror"))
    return {
        "hosts": hosts,
        "guest_count": data.get("guest_count") or 0,
        "location": location,
        "timezone": ev.get("timezone") or "",
        "calendar": cal.get("name", ""),
        "description_html": description_html,
        # The calendar blurb is a short fallback when an event has no prose body.
        "description": cal.get("description_short", ""),
    }


def fetch_luma(
    get_json: JsonFetcher = _http_json,
    get_cookie_json: Callable[[str, str], dict | None] = _cookie_json,
    token: str | None = None,
) -> tuple[list[dict], dict]:
    """Fetch the SF discover feed + (if a session token is present) the user's own
    registered/invited events, merged. His RSVP status wins on dedupe, so an
    event he's going to shows as "going" rather than "discover". Returns
    (events, status). Injectable for tests.
    """
    started = time.time()
    try:
        discover = fetch_luma_sf(get_json)
        tok = token if token is not None else _load_luma_token()
        mine = fetch_my_luma_events(tok, get_cookie_json)
        by_id = {ev["id"]: ev for ev in discover}
        by_id.update({ev["id"]: ev for ev in mine})  # my status overrides discover
        events = sorted(by_id.values(), key=lambda ev: ev.get("start", 0))
        counts = {}
        for ev in events:
            counts[ev["status"]] = counts.get(ev["status"], 0) + 1
        return events, {"ok": True, "count": len(events), "mine": len(mine),
                        "by_status": counts, "signed_in": bool(mine),
                        "ms": int((time.time() - started) * 1000)}
    except Exception as exc:  # noqa: BLE001 - record and continue
        return [], {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

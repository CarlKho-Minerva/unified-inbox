"""Deep search across the user's full history -- beyond the ~250 messages cached for
the live inbox. Searches the providers directly:

- Gmail API (the primary account): full-mailbox `q=` search.
- IMAP accounts (personal Gmails + Zoho): INBOX-wide TEXT search.
- Slack: `search.messages` (user token) -> deep-link results.

Discord is intentionally EXCLUDED -- its user-token reads trip abuse detection.
Email results are openable in the reader (they carry source + native_id, and the
detail route can reconstruct the body from the id); Slack results deep-link out.

Network access (`get_json`) and the IMAP-credentials loader are injected so the
result normalization can be tested against fixtures without a network.
"""

import email
import imaplib
import urllib.parse
from collections.abc import Callable

from unified_inbox.fetchers import (
    JsonFetcher,
    _gmail_fetch_message,
    _imap_normalize,
    _lk,
    _load_imap_creds,
)
from unified_inbox.sources import SOURCES

GMAIL_LIMIT = 15
IMAP_LIMIT = 12
SLACK_LIMIT = 15
MAX_QUERY_LEN = 256  # bound the query so a pathological input can't build a huge provider request

# The IMAP connection factory (host, port) -> connection. Injected so the
# result-normalization path can be tested against a fake connection offline.
ImapConnect = Callable[[str, int], imaplib.IMAP4]

# Email sources by fetch path, so deep search hits the right provider.
_GMAIL_API_SOURCES = [k for k, c in SOURCES.items() if c.get("via") == "gmail_api"]
_IMAP_SOURCES = [k for k, c in SOURCES.items() if c.get("via") == "imap"]


def _search_gmail_api(key: str, query: str, get_json: JsonFetcher) -> list[dict]:
    q = urllib.parse.quote(query)
    listing = get_json(
        f"https://gmail.googleapis.com/gmail/v1/users/me/messages?maxResults={GMAIL_LIMIT}&q={q}"
    )
    if not isinstance(listing, dict) or "messages" not in listing:
        return []
    out = []
    for stub in listing["messages"]:
        row = _gmail_fetch_message(get_json, key, stub["id"])
        if row:
            row["deep"] = True
            out.append(row)
    return out


def _search_imap(key: str, creds: dict, query: str, connect: ImapConnect = imaplib.IMAP4_SSL) -> list[dict]:
    conn = connect(creds["host"], creds["port"])
    conn.login(creds["email"], creds["password"])
    try:
        conn.select("INBOX", readonly=True)
        # imaplib does not quote arguments, so a multi-word query passed as one
        # arg (`SEARCH TEXT foo bar`) makes the server treat the second word as a
        # separate, invalid search key and reject the command. Emit one TEXT term
        # per word instead, ANDing them -- what a user expects from search.
        criteria: list[str] = []
        for word in query.split():
            criteria += ["TEXT", word]
        if not criteria:
            return []
        typ, data = conn.search(None, *criteria)
        if typ != "OK" or not data or not data[0]:
            return []
        uids = data[0].split()[-IMAP_LIMIT:][::-1]  # most-recent matches first
        out = []
        for uid in uids:
            typ, fetched = conn.fetch(uid, "(FLAGS RFC822.HEADER)")
            if typ != "OK" or not fetched or not isinstance(fetched[0], tuple):
                continue
            flags = imaplib.ParseFlags(fetched[0][0])
            msg = email.message_from_bytes(fetched[0][1])
            row = _imap_normalize(key, uid.decode(), msg, flags)
            row["snippet"] = ""  # header-only fetch; body loads when opened
            row["deep"] = True
            out.append(row)
        return out
    finally:
        try:
            conn.logout()
        except OSError:
            pass


def _search_slack(query: str, get_json: JsonFetcher) -> list[dict]:
    q = urllib.parse.quote(query)
    res = get_json(f"https://slack.com/api/search.messages?query={q}&count={SLACK_LIMIT}")
    if not isinstance(res, dict) or not res.get("ok"):
        return []
    out = []
    for m in res.get("messages", {}).get("matches", []):
        ch = m.get("channel", {}) or {}
        name = ch.get("name") or "dm"
        out.append(
            {
                "id": f"slack-deep:{m.get('iid') or m.get('ts')}",
                "source": "slack",
                "kind": "chat",
                "who": f"#{name}" if ch.get("is_channel") else (m.get("username") or "Slack"),
                "addr": "Slack",
                "subject": m.get("username") or "message",
                "snippet": (m.get("text") or "").replace("\n", " ")[:200],
                "ts": float(m.get("ts", "0") or 0),
                "unread": False,
                "is_dm": not ch.get("is_channel", False),
                "is_mention": False,
                "url": m.get("permalink", "https://app.slack.com/"),
                "native_id": m.get("ts"),
                "channel_id": ch.get("id"),
                "deep": True,
                "open_external": True,  # Slack search results deep-link out
            }
        )
    return out


def search_all(
    query: str,
    get_json: JsonFetcher = _lk,
    load_imap_creds: Callable[[], dict[str, dict]] = _load_imap_creds,
    imap_connect: ImapConnect = imaplib.IMAP4_SSL,
) -> list[dict]:
    """Search full history across email + Slack. Returns normalized rows sorted
    newest-first. Best-effort: a failing source is skipped, not fatal."""
    query = query.strip()[:MAX_QUERY_LEN]
    if not query:
        return []
    results: list[dict] = []
    imap_creds = load_imap_creds()

    for key in _GMAIL_API_SOURCES:
        try:
            results.extend(_search_gmail_api(key, query, get_json))
        except Exception:  # noqa: BLE001 - isolate one source's failure
            continue
    for key in _IMAP_SOURCES:
        creds = imap_creds.get(SOURCES[key].get("email", ""))
        if not creds:
            continue
        try:
            results.extend(_search_imap(key, creds, query, imap_connect))
        except Exception:  # noqa: BLE001 - isolate one source's failure
            continue
    try:
        results.extend(_search_slack(query, get_json))
    except Exception:  # noqa: BLE001 - isolate one source's failure
        pass

    results.sort(key=lambda r: r.get("ts", 0), reverse=True)
    return results

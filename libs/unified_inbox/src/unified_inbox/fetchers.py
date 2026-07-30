"""Fetch recent messages from all six sources and normalize them into one
schema. Each fetcher is defensive: a failure in one source is recorded in meta
and does not stop the others, and the sources are fetched concurrently so the
background refresh is bounded by the slowest source rather than their sum.

Normalized message dict:
  id, source, kind ("email"|"chat"|"github"), who, addr, subject, snippet,
  ts (epoch), unread (bool), is_dm (bool), is_mention (bool), url, native_id,
  channel_id

Network access is injected: every fetcher that reads a third-party HTTP API
takes a ``get_json`` callable (defaulting to the real latchkey-backed ``_lk``),
so the normalization logic can be exercised against fixtures without a network.
"""

import base64
import datetime
import email
import email.message
import imaplib
import json
import subprocess
import time
import urllib.parse
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from email.header import decode_header, make_header
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path

from unified_inbox.calendars import fetch_all_events
from unified_inbox.luma import fetch_luma
from unified_inbox.obsidian import fetch_tasks
from unified_inbox.sources import SOURCES
from unified_inbox.store import Store
from unified_inbox.telegram import fetch_telegram, fetch_telegram_thread

# A JSON fetcher takes a URL and returns the parsed body (or None on any
# failure). The real implementation shells out through latchkey; tests pass a
# fixture-backed stand-in.
JsonFetcher = Callable[[str], dict | list | None]

# Per-source list caps -- keep the background refresh bounded and API-friendly.
EMAIL_LIMIT = 50  # deeper history so fuzzy search reaches further back

# Chat is scoped to what the user actually cares about (not the DM firehose):
#   Slack  -> a specific allowlist of channels.
#   Discord -> recently-active text channels in these servers, capped per server.
# Both are empty by default: set them to your own channel IDs / servers.
SLACK_CHANNEL_ALLOW: list[str] = []  # add your own channel IDs, e.g. ["C0XXXXXXX"]
SLACK_DM_LIMIT = 20  # most-recent direct + group DMs to surface (read-only)
DISCORD_GUILDS: dict[str, str] = {}  # e.g. {"<guild_id>": "<label>"}
DISCORD_CHANNELS_PER_GUILD = 40
DISCORD_ACTIVE_DAYS = 180  # inbox = recently-active channels; dormant archives surface via search
DISCORD_EPOCH_MS = 1420070400000  # Discord snowflake epoch
DISCORD_PACE_SECONDS = 0.5  # gentle pacing: user-token reads can trip Discord's abuse detection
# Your own Discord username, so your own messages aren't marked unread. Set it
# to the username on your account (a message from anyone else won't match).
DISCORD_SELF_USERNAME = "your-discord-username"

# Concurrency bounds. Sources hit distinct endpoints, so fetching them together
# is safe; the Gmail metadata fan-out stays well under Gmail's per-user quota.
SOURCE_WORKERS = 8
GMAIL_FETCH_WORKERS = 8

MAIL_ACCOUNTS = Path("runtime/mail/accounts.json")


def _iso_ts(value: str) -> float:
    """Parse a Discord/ISO-8601 timestamp to epoch seconds."""
    try:
        return datetime.datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError):
        return time.time()


def _snowflake_ms(sid: str) -> int:
    return (int(sid) >> 22) + DISCORD_EPOCH_MS


def _lk(url: str) -> dict | list | None:
    """Run an authenticated request through latchkey and parse JSON."""
    out = subprocess.run(["latchkey", "curl", "-s", url], capture_output=True, text=True, timeout=60)
    if not out.stdout.strip():
        return None
    try:
        return json.loads(out.stdout)
    except json.JSONDecodeError:
        return None


def _decode(value: str) -> str:
    try:
        return str(make_header(decode_header(value)))
    except (ValueError, LookupError):
        return value


# --------------------------------------------------------------------------
# Email: Gmail API (the primary account -- credentials via latchkey, no password stored)
# --------------------------------------------------------------------------
def _gmail_normalize(key: str, msg: dict) -> dict:
    headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
    name, addr = parseaddr(headers.get("from", ""))
    mid = msg.get("id", "")
    ts = int(msg.get("internalDate", "0")) / 1000 or time.time()
    return {
        "id": f"{key}:{mid}",
        "source": key,
        "kind": "email",
        "who": _decode(name) or addr,
        "addr": addr,
        "subject": _decode(headers.get("subject", "(no subject)")),
        "snippet": msg.get("snippet", ""),
        "ts": ts,
        "unread": "UNREAD" in msg.get("labelIds", []),
        "is_dm": False,
        "is_mention": False,
        "url": f"https://mail.google.com/mail/u/0/#inbox/{mid}",
        "native_id": mid,
        "channel_id": None,
    }


def _gmail_fetch_message(get_json: JsonFetcher, key: str, mid: str) -> dict | None:
    msg = get_json(
        f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{mid}"
        "?format=metadata&metadataHeaders=From&metadataHeaders=Subject&metadataHeaders=Date"
    )
    if not isinstance(msg, dict):
        return None
    return _gmail_normalize(key, msg)


def fetch_gmail_api(key: str, get_json: JsonFetcher = _lk) -> list[dict]:
    listing = get_json(
        f"https://gmail.googleapis.com/gmail/v1/users/me/messages?maxResults={EMAIL_LIMIT}&q=in:inbox"
    )
    if not isinstance(listing, dict) or "messages" not in listing:
        return []
    stubs = listing["messages"]
    # The per-message metadata reads are independent, so fan them out; map keeps
    # input order, and we drop any that failed to load.
    with ThreadPoolExecutor(max_workers=GMAIL_FETCH_WORKERS) as pool:
        results = list(pool.map(lambda stub: _gmail_fetch_message(get_json, key, stub["id"]), stubs))
    return [r for r in results if r is not None]


# --------------------------------------------------------------------------
# Email: IMAP (the two personal Gmails + Zoho -- app passwords stored locally)
# --------------------------------------------------------------------------
def _load_imap_creds() -> dict[str, dict]:
    if not MAIL_ACCOUNTS.exists():
        return {}
    return {a["email"]: a for a in json.loads(MAIL_ACCOUNTS.read_text())}


def _payload_text(part: email.message.Message) -> str:
    """Decode a leaf part's bytes payload to text, or '' if it has no decodable
    bytes (a multipart container, or an unknown charset)."""
    raw = part.get_payload(decode=True)
    if not isinstance(raw, bytes):
        return ""
    try:
        return raw.decode(part.get_content_charset() or "utf-8", "replace")
    except LookupError:
        return ""


def _snippet_from_email(msg: email.message.Message) -> str:
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and not part.get_filename():
                body = _payload_text(part)
                if body:
                    break
    else:
        body = _payload_text(msg)
    return " ".join(body.split())[:200]


def _imap_normalize(key: str, uid: str, msg: email.message.Message, flags: tuple[bytes, ...]) -> dict:
    name, addr = parseaddr(msg.get("From", ""))
    try:
        ts = parsedate_to_datetime(msg.get("Date", "")).timestamp()
    except (TypeError, ValueError):
        ts = time.time()
    return {
        "id": f"{key}:{uid}",
        "source": key,
        "kind": "email",
        "who": _decode(name) or addr,
        "addr": addr,
        "subject": _decode(msg.get("Subject", "(no subject)")),
        "snippet": _snippet_from_email(msg),
        "ts": ts,
        "unread": rb"\Seen" not in flags,
        "is_dm": False,
        "is_mention": False,
        "url": "https://mail.zoho.com/" if key == "zoho" else "https://mail.google.com/",
        "native_id": uid,
        "channel_id": None,
    }


def fetch_imap(key: str, creds: dict) -> list[dict]:
    conn = imaplib.IMAP4_SSL(creds["host"], creds["port"])
    conn.login(creds["email"], creds["password"])
    try:
        conn.select("INBOX", readonly=True)
        typ, data = conn.search(None, "ALL")
        ids = data[0].split()
        recent = ids[-EMAIL_LIMIT:][::-1]
        out = []
        for uid in recent:
            typ, fetched = conn.fetch(uid, "(FLAGS RFC822)")
            if typ != "OK" or not fetched or not isinstance(fetched[0], tuple):
                continue
            flags = imaplib.ParseFlags(fetched[0][0])
            msg = email.message_from_bytes(fetched[0][1])
            out.append(_imap_normalize(key, uid.decode(), msg, flags))
        return out
    finally:
        try:
            conn.logout()
        except OSError:
            pass


# --------------------------------------------------------------------------
# Slack: one allowlisted channel, latest message; unread via last_read compare
# --------------------------------------------------------------------------
def _slack_user_names(user_ids: set[str], get_json: JsonFetcher) -> dict[str, str]:
    names = {}
    for uid in user_ids:
        info = get_json(f"https://slack.com/api/users.info?user={uid}")
        if isinstance(info, dict) and info.get("ok"):
            u = info["user"]
            names[uid] = u.get("real_name") or u.get("profile", {}).get("display_name") or u.get("name", uid)
    return names


def _slack_row(key: str, cid: str, team: str, who: str, is_dm: bool, m: dict, last_read: float, get_json: JsonFetcher) -> dict:
    author = _slack_user_names({m["user"]}, get_json).get(m["user"], "someone") if m.get("user") else "someone"
    ts = float(m.get("ts", "0") or 0)
    return {
        "id": f"{key}:{cid}:{m.get('ts')}",
        "source": key,
        "kind": "chat",
        "who": who,
        "addr": "Slack",
        "subject": author if is_dm and who == "Group DM" else ("Direct message" if is_dm else author),
        "snippet": (m.get("text") or "").replace("\n", " ")[:200],
        "ts": ts,
        "unread": ts > last_read > 0,
        "is_dm": is_dm,
        "is_mention": False,
        # Deep link opens the exact channel/DM in Slack (read-only from here).
        "url": f"https://app.slack.com/client/{team}/{cid}",
        "native_id": m.get("ts"),
        "channel_id": cid,
    }


def fetch_slack(key: str, get_json: JsonFetcher = _lk, channels_allow: list[str] | None = None) -> list[dict]:
    channels_allow = SLACK_CHANNEL_ALLOW if channels_allow is None else channels_allow
    auth = get_json("https://slack.com/api/auth.test")
    team = auth.get("team_id", "-") if isinstance(auth, dict) else "-"
    out = []

    # Allow-listed channels.
    for cid in channels_allow:
        info = get_json(f"https://slack.com/api/conversations.info?channel={cid}")
        name, last_read = "channel", 0.0
        if isinstance(info, dict) and info.get("ok"):
            name = info["channel"].get("name", "channel")
            last_read = float(info["channel"].get("last_read", "0") or 0)
        hist = get_json(f"https://slack.com/api/conversations.history?channel={cid}&limit=1")
        if isinstance(hist, dict) and hist.get("ok") and hist.get("messages"):
            out.append(_slack_row(key, cid, team, f"#{name}", False, hist["messages"][0], last_read, get_json))

    # Direct + group DMs (read-only). users.conversations returns the user's IMs
    # and multi-person DMs; each row deep-links to that DM in Slack.
    convos = get_json(f"https://slack.com/api/users.conversations?types=im,mpim&limit={SLACK_DM_LIMIT}")
    channels = convos.get("channels", []) if isinstance(convos, dict) and convos.get("ok") else []
    for c in channels:
        cid = c["id"]
        info = get_json(f"https://slack.com/api/conversations.info?channel={cid}")
        last_read = 0.0
        if isinstance(info, dict) and info.get("ok"):
            last_read = float(info["channel"].get("last_read", "0") or 0)
        hist = get_json(f"https://slack.com/api/conversations.history?channel={cid}&limit=1")
        if not isinstance(hist, dict) or not hist.get("ok") or not hist.get("messages"):
            continue
        if c.get("is_im"):
            who = _slack_user_names({c["user"]}, get_json).get(c.get("user"), "Direct message")
        else:
            who = "Group DM"
        out.append(_slack_row(key, cid, team, who, True, hist["messages"][0], last_read, get_json))
    return out


# --------------------------------------------------------------------------
# Discord: recently-active channels, latest message; unread via local seen cursor
# --------------------------------------------------------------------------
def fetch_discord(
    key: str,
    seen: dict,
    get_json: JsonFetcher = _lk,
    pace_seconds: float = DISCORD_PACE_SECONDS,
    guilds: dict[str, str] | None = None,
) -> tuple[list[dict], dict]:
    guilds = DISCORD_GUILDS if guilds is None else guilds
    seen = dict(seen)
    first_run = not seen  # seed as read on first population to avoid a wall of unread
    cutoff_ms = (time.time() - DISCORD_ACTIVE_DAYS * 86400) * 1000
    out = []
    for gid, gname in guilds.items():
        channels = get_json(f"https://discord.com/api/v10/guilds/{gid}/channels")
        if not isinstance(channels, list):
            continue
        # Text/announcement channels with recent activity, most-recent first.
        active = [
            c for c in channels
            if c.get("type") in (0, 5) and c.get("last_message_id")
            and _snowflake_ms(c["last_message_id"]) >= cutoff_ms
        ]
        active.sort(key=lambda c: int(c["last_message_id"]), reverse=True)
        for c in active[:DISCORD_CHANNELS_PER_GUILD]:
            cid = c["id"]
            msgs = get_json(f"https://discord.com/api/v10/channels/{cid}/messages?limit=1")
            time.sleep(pace_seconds)  # deliberate rate-limit pacing (see DISCORD_PACE_SECONDS)
            if not isinstance(msgs, list) or not msgs:
                continue  # skips channels we lack permission to read
            m = msgs[0]
            author = m.get("author", {}).get("global_name") or m.get("author", {}).get("username", "someone")
            ts = _iso_ts(m.get("timestamp", ""))
            is_self = m.get("author", {}).get("username") == DISCORD_SELF_USERNAME
            unread = (not first_run) and (seen.get(cid) != m["id"]) and not is_self
            seen[cid] = m["id"]
            out.append(
                {
                    "id": f"{key}:{cid}:{m['id']}",
                    "source": key,
                    "kind": "chat",
                    "who": f"#{c.get('name', 'channel')}",
                    "addr": f"{gname} · Discord",
                    "subject": f"{gname} · {author}",
                    "snippet": (m.get("content") or "[attachment]").replace("\n", " ")[:200],
                    "ts": ts,
                    "unread": unread,
                    "is_dm": False,
                    "is_mention": False,
                    "url": f"https://discord.com/channels/{gid}/{cid}",
                    "native_id": m["id"],
                    "channel_id": cid,
                }
            )
    return out, seen


# --------------------------------------------------------------------------
# GitHub: notifications + open items you're assigned/mentioned/asked to review
# --------------------------------------------------------------------------
def _gh_html_url(api_url: str) -> str:
    if not api_url:
        return "https://github.com/notifications"
    return (
        api_url.replace("https://api.github.com/repos/", "https://github.com/")
        .replace("/pulls/", "/pull/")
    )


def fetch_github(key: str, get_json: JsonFetcher = _lk) -> list[dict]:
    out, seen = [], set()
    me = get_json("https://api.github.com/user")
    login = me.get("login") if isinstance(me, dict) else None

    notifs = get_json("https://api.github.com/notifications?per_page=30")
    if isinstance(notifs, list):
        for n in notifs:
            subj = n.get("subject", {})
            html = _gh_html_url(subj.get("url", ""))
            seen.add(html)
            out.append(
                {
                    "id": f"{key}:{html}",
                    "source": key,
                    "kind": "github",
                    "who": n.get("repository", {}).get("full_name", "GitHub"),
                    "addr": n.get("repository", {}).get("full_name", ""),
                    "subject": subj.get("title", "(notification)"),
                    "snippet": f"{n.get('reason', '').replace('_', ' ')} · {subj.get('type', '')}",
                    "ts": _iso_ts(n.get("updated_at", "")),
                    "unread": bool(n.get("unread")),
                    "is_dm": False,
                    "is_mention": n.get("reason") == "mention",
                    "url": html,
                    "native_id": subj.get("url", ""),
                    "channel_id": None,
                }
            )

    if login:
        queries = [
            (f"review-requested:{login} is:open", "review requested"),
            (f"assignee:{login} is:open", "assigned"),
            (f"involves:{login} is:open", "involved"),
        ]
        for q, label in queries:
            res = get_json(
                "https://api.github.com/search/issues?q="
                + urllib.parse.quote(q)
                + "&sort=updated&per_page=15"
            )
            if not isinstance(res, dict) or not res.get("items"):
                continue
            for it in res["items"]:
                html = it.get("html_url", "")
                if not html or html in seen:
                    continue
                seen.add(html)
                repo = "/".join(it.get("repository_url", "").split("/")[-2:])
                kind = "PR" if it.get("pull_request") else "Issue"
                out.append(
                    {
                        "id": f"{key}:{html}",
                        "source": key,
                        "kind": "github",
                        "who": repo,
                        "addr": repo,
                        "subject": it.get("title", "(untitled)"),
                        "snippet": f"{label} · {kind} #{it.get('number', '')} · {it.get('state', '')}",
                        "ts": _iso_ts(it.get("updated_at", "")),
                        "unread": False,
                        "is_dm": False,
                        "is_mention": label == "involved",
                        "url": html,
                        "native_id": it.get("url", ""),
                        "channel_id": None,
                    }
                )
    return out


def fetch_github_detail(api_url: str, get_json: JsonFetcher = _lk) -> dict:
    if not api_url:
        return {"body": "", "is_html": False}
    item = get_json(api_url)
    if not isinstance(item, dict):
        return {"body": "(could not load this item)", "is_html": False}
    header = f"{item.get('title', '')}\n{item.get('state', '').upper()} · opened by {item.get('user', {}).get('login', '')}\n\n"
    return {"body": header + (item.get("body") or "(no description)"), "is_html": False}


# --------------------------------------------------------------------------
# Detail: full email body / chat thread (fetched live when a row is opened)
# --------------------------------------------------------------------------
def _b64url(data: str) -> str:
    try:
        return base64.urlsafe_b64decode(data + "===").decode("utf-8", "replace")
    except (ValueError, TypeError):
        return ""


def _gmail_extract_body(payload: dict) -> tuple[str, bool]:
    """Return (body, is_html). Prefer text/html, fall back to text/plain."""
    html, text = "", ""
    stack = [payload]
    while stack:
        part = stack.pop()
        mime = part.get("mimeType", "")
        body = part.get("body", {})
        if mime == "text/html" and body.get("data"):
            html = _b64url(body["data"])
        elif mime == "text/plain" and body.get("data"):
            text = _b64url(body["data"])
        stack.extend(part.get("parts", []))
    if html:
        return html, True
    return text, False


def _gmail_attachments(payload: dict) -> list[dict]:
    out, stack = [], [payload]
    while stack:
        part = stack.pop()
        fname = part.get("filename") or ""
        body = part.get("body", {})
        if fname and body.get("attachmentId"):
            out.append(
                {
                    "name": fname,
                    "mime": part.get("mimeType", "application/octet-stream"),
                    "size": body.get("size", 0),
                    "ref": body["attachmentId"],
                }
            )
        stack.extend(part.get("parts", []))
    return out


def _extract_imap_content(msg: email.message.Message) -> dict:
    """Pull body (preferring HTML) and attachment stubs out of a parsed email.

    Attachment ``ref`` is the positional index of the attachment part, matching
    how ``fetch_email_attachment`` re-walks the message to stream the bytes.
    """
    html, text, attachments, idx = "", "", [], 0
    for part in msg.walk() if msg.is_multipart() else [msg]:
        ctype = part.get_content_type()
        fname = part.get_filename()
        if fname:
            raw = part.get_payload(decode=True)
            size = len(raw) if isinstance(raw, bytes) else 0
            attachments.append(
                {"name": _decode(fname), "mime": ctype, "size": size, "ref": str(idx)}
            )
            idx += 1
            continue
        decoded = _payload_text(part)
        if not decoded:
            continue
        if ctype == "text/html":
            html = decoded
        elif ctype == "text/plain":
            text = decoded
    if html:
        return {"body": html, "is_html": True, "attachments": attachments}
    return {"body": text, "is_html": False, "attachments": attachments}


def fetch_email_body(source_key: str, native_id: str, get_json: JsonFetcher = _lk) -> dict:
    cfg = SOURCES[source_key]
    if cfg["via"] == "gmail_api":
        msg = get_json(f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{native_id}?format=full")
        if not isinstance(msg, dict):
            return {"body": "", "is_html": False, "attachments": []}
        body, is_html = _gmail_extract_body(msg.get("payload", {}))
        return {"body": body, "is_html": is_html, "attachments": _gmail_attachments(msg.get("payload", {}))}
    # IMAP
    creds = _load_imap_creds().get(cfg["email"])
    if not creds:
        return {"body": "", "is_html": False, "attachments": []}
    conn = imaplib.IMAP4_SSL(creds["host"], creds["port"])
    conn.login(creds["email"], creds["password"])
    try:
        conn.select("INBOX", readonly=True)
        typ, fetched = conn.fetch(native_id, "(RFC822)")
        if typ != "OK" or not fetched or not isinstance(fetched[0], tuple):
            return {"body": "", "is_html": False, "attachments": []}
        msg = email.message_from_bytes(fetched[0][1])
        return _extract_imap_content(msg)
    finally:
        try:
            conn.logout()
        except OSError:
            pass


def fetch_email_attachment(source_key: str, native_id: str, ref: str, get_json: JsonFetcher = _lk) -> bytes:
    cfg = SOURCES[source_key]
    if cfg["via"] == "gmail_api":
        data = get_json(
            f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{native_id}/attachments/{ref}"
        )
        if isinstance(data, dict) and data.get("data"):
            return base64.urlsafe_b64decode(data["data"] + "===")
        return b""
    creds = _load_imap_creds().get(cfg["email"])
    if not creds:
        return b""
    conn = imaplib.IMAP4_SSL(creds["host"], creds["port"])
    conn.login(creds["email"], creds["password"])
    try:
        conn.select("INBOX", readonly=True)
        typ, fetched = conn.fetch(native_id, "(RFC822)")
        if typ != "OK" or not fetched or not isinstance(fetched[0], tuple):
            return b""
        msg = email.message_from_bytes(fetched[0][1])
        idx = 0
        for part in msg.walk() if msg.is_multipart() else [msg]:
            if part.get_filename():
                if str(idx) == ref:
                    raw = part.get_payload(decode=True)
                    return raw if isinstance(raw, bytes) else b""
                idx += 1
        return b""
    finally:
        try:
            conn.logout()
        except OSError:
            pass


def fetch_chat_thread(source_key: str, channel_id: str, limit: int = 20, get_json: JsonFetcher = _lk) -> list[dict]:
    if source_key == "telegram":
        return fetch_telegram_thread(channel_id, limit)
    if source_key == "slack":
        hist = get_json(f"https://slack.com/api/conversations.history?channel={channel_id}&limit={limit}")
        if not isinstance(hist, dict) or not hist.get("ok"):
            return []
        msgs = list(reversed(hist.get("messages", [])))
        uids = {m.get("user") for m in msgs if m.get("user")}
        names = _slack_user_names(uids, get_json)
        me = get_json("https://slack.com/api/auth.test")
        my_id = me.get("user_id") if isinstance(me, dict) else None
        return [
            {
                "who": names.get(m.get("user"), m.get("username", "unknown")),
                "text": m.get("text", ""),
                "ts": float(m.get("ts", "0") or 0),
                "me": m.get("user") == my_id,
                # Slack file URLs are private (need the token), so no inline URL -- name only.
                "attachments": [
                    {"name": f.get("name", "file"), "mime": f.get("mimetype", ""), "url": None,
                     "is_image": (f.get("mimetype", "") or "").startswith("image")}
                    for f in m.get("files", [])
                ],
                "embeds": [],
            }
            for m in msgs
        ]
    if source_key == "discord":
        msgs = get_json(f"https://discord.com/api/v10/channels/{channel_id}/messages?limit={limit}")
        if not isinstance(msgs, list):
            return []
        return [
            {
                "who": m.get("author", {}).get("global_name") or m.get("author", {}).get("username", "unknown"),
                "text": m.get("content", "") or "",
                "ts": _iso_ts(m.get("timestamp", "")),
                "me": m.get("author", {}).get("username") == DISCORD_SELF_USERNAME,
                "attachments": [
                    {"name": a.get("filename", "file"), "mime": a.get("content_type", ""), "url": a.get("url"),
                     "is_image": (a.get("content_type", "") or "").startswith("image")}
                    for a in m.get("attachments", [])
                ],
                "embeds": [
                    {"title": e.get("title", ""), "description": (e.get("description") or "")[:220],
                     "url": e.get("url", ""), "provider": (e.get("provider") or {}).get("name", ""),
                     "image": (e.get("image") or {}).get("url") or (e.get("thumbnail") or {}).get("url")}
                    for e in m.get("embeds", [])
                ],
            }
            for m in reversed(msgs)
        ]
    return []


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------
def _fetch_source(
    key: str, cfg: dict, get_json: JsonFetcher, imap_creds: dict[str, dict], seen: dict,
    slack_channels: list[str], discord_guilds: dict[str, str],
) -> tuple[list[dict], dict, dict | None]:
    """Fetch one source in isolation. Returns (messages, status, new_seen).

    ``new_seen`` is the updated read cursor for chat sources that track one
    (Discord), else None. The broad catch is deliberate: this is the per-source
    isolation boundary, so any single source's failure (a timeout, a malformed
    payload, an auth error) is recorded and the other sources still refresh.
    """
    started = time.time()
    via = cfg["via"]
    try:
        new_seen: dict | None = None
        if via == "gmail_api":
            msgs = fetch_gmail_api(key, get_json)
        elif via == "imap":
            creds = imap_creds.get(cfg["email"])
            if not creds:
                return [], {"ok": False, "error": "no stored credentials"}, None
            msgs = fetch_imap(key, creds)
        elif via == "slack":
            msgs = fetch_slack(key, get_json, channels_allow=slack_channels)
        elif via == "discord":
            msgs, new_seen = fetch_discord(key, seen, get_json, guilds=discord_guilds)
        elif via == "github":
            msgs = fetch_github(key, get_json)
        elif via == "telegram":
            msgs = fetch_telegram(key)
        else:
            msgs = []
        return msgs, {"ok": True, "count": len(msgs), "ms": int((time.time() - started) * 1000)}, new_seen
    except Exception as exc:  # noqa: BLE001 - isolate a single source's failure (see docstring)
        return [], {"ok": False, "error": f"{type(exc).__name__}: {exc}"}, None


def refresh_all(
    store: Store,
    get_json: JsonFetcher = _lk,
    fetch_events: Callable[[], tuple[list[dict], dict]] = fetch_all_events,
    fetch_luma_events: Callable[[], tuple[list[dict], dict]] = fetch_luma,
    fetch_vault_tasks: Callable[[], tuple[list[dict], dict]] = fetch_tasks,
    load_imap_creds: Callable[[], dict[str, dict]] = _load_imap_creds,
    due_sources: set[str] | None = None,
    refresh_aux: bool = True,
    slack_channels: list[str] | None = None,
    discord_guilds: dict[str, str] | None = None,
) -> dict:
    """Fetch sources, both calendars, the Luma SF feed, and the Obsidian vault
    tasks concurrently, merge, sort newest-first, persist, return meta.

    ``due_sources`` selects which message sources to actually hit the network
    for this cycle (``None`` = all). Sources not in the set keep their
    previously-cached messages and status untouched -- this lets the caller poll
    cheap/latency-sensitive sources (email, GitHub) often while leaving
    rate-sensitive ones (Discord, whose user-token reads trip abuse detection)
    on a slower cadence. ``refresh_aux`` likewise gates the calendars / Luma /
    tasks fetches; when False they carry forward from the store.
    """
    imap_creds = load_imap_creds()
    slack_channels = SLACK_CHANNEL_ALLOW if slack_channels is None else slack_channels
    discord_guilds = DISCORD_GUILDS if discord_guilds is None else discord_guilds
    seen = store.get_seen()
    fetch_keys = [k for k in SOURCES if due_sources is None or k in due_sources]

    prev_meta = store.get_meta() or {}
    prev_status = prev_meta.get("sources", {}) if isinstance(prev_meta, dict) else {}
    # Messages from sources we're NOT refreshing this cycle carry forward as-is.
    carried = [m for m in store.get_messages() if m.get("source") not in fetch_keys]

    results: dict[str, tuple[list[dict], dict, dict | None]] = {}
    with ThreadPoolExecutor(max_workers=SOURCE_WORKERS) as pool:
        futures = {
            pool.submit(_fetch_source, key, SOURCES[key], get_json, imap_creds, dict(seen),
                        slack_channels, discord_guilds): key
            for key in fetch_keys
        }
        cal_future = pool.submit(fetch_events) if refresh_aux else None
        luma_future = pool.submit(fetch_luma_events) if refresh_aux else None
        tasks_future = pool.submit(fetch_vault_tasks) if refresh_aux else None
        for fut in as_completed(futures):
            results[futures[fut]] = fut.result()

    # Merge deterministically in SOURCES order (completion order is nondeterministic).
    fresh: list[dict] = []
    status: dict[str, dict] = {}
    for key in SOURCES:
        if key in results:
            msgs, src_status, new_seen = results[key]
            fresh.extend(msgs)
            status[key] = src_status
            if new_seen is not None:
                seen = new_seen
        elif key in prev_status:
            status[key] = prev_status[key]  # preserve last status for a skipped source

    all_msgs = carried + fresh
    all_msgs.sort(key=lambda m: m.get("ts", 0), reverse=True)
    store.set_messages(all_msgs)
    store.set_seen(seen)

    if refresh_aux:
        events, cal_status = cal_future.result()
        luma_events, luma_status = luma_future.result()
        tasks, tasks_status = tasks_future.result()
        store.set_events(events)
        store.set_luma(luma_events)
        store.set_tasks(tasks)
        status.update(cal_status)
        status["luma"] = luma_status
        status["tasks"] = tasks_status
        events_n, luma_n, tasks_n = len(events), len(luma_events), len(tasks)
    else:
        # Carry forward the aux data and its status from the previous full cycle.
        for key in ("google_calendar", "zoho_calendar", "luma", "tasks"):
            if key in prev_status:
                status[key] = prev_status[key]
        events_n = prev_meta.get("events", len(store.get_events()))
        luma_n = prev_meta.get("luma", len(store.get_luma()))
        tasks_n = prev_meta.get("tasks", len(store.get_tasks()))

    meta = {
        "last_refresh": time.time(),
        "sources": status,
        "total": len(all_msgs),
        "events": events_n,
        "luma": luma_n,
        "tasks": tasks_n,
    }
    store.set_meta(meta)
    return meta

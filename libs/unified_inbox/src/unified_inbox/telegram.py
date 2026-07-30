"""Read-only Telegram via Telethon (MTProto), using the user's own logged-in session.

Unlike the other sources this does NOT go through latchkey -- Telegram personal
chats need a real user client. Credentials (api_id/api_hash) and the auth session
live locally under runtime/telegram/ (gitignored), created by the one-time login.

Each call spins a fresh client under its own asyncio loop (``asyncio.run``) so it
is safe from the Flask worker threads, serialized by a module lock because the
SQLite session file must not be touched concurrently. It only ever READS.
"""

import asyncio
import json
import threading
from pathlib import Path

from telethon import TelegramClient

CREDS_FILE = Path("runtime/telegram/creds.json")
SESSION = "runtime/telegram/session"
DIALOG_LIMIT = 20   # most-recent conversations to surface
THREAD_LIMIT = 25   # messages when a chat is opened

_lock = threading.Lock()  # serialize access to the single SQLite session


def _load_creds() -> dict | None:
    try:
        return json.loads(CREDS_FILE.read_text())
    except (OSError, ValueError):
        return None


def _entity_url(entity) -> str:
    username = getattr(entity, "username", None)
    return f"https://t.me/{username}" if username else "https://web.telegram.org/"


def _doc_info(document) -> tuple[str, str]:
    """(mime, filename) for a Telegram document, best-effort."""
    mime = getattr(document, "mime_type", "") or ""
    fname = ""
    for a in getattr(document, "attributes", []) or []:
        if hasattr(a, "file_name") and a.file_name:
            fname = a.file_name
    return mime, fname


def _media_label(m) -> str:
    """A human label for a message's media, e.g. '[photo]', '[file: x.pdf]'."""
    if getattr(m, "photo", None):
        return "[photo]"
    doc = getattr(m, "document", None)
    if doc:
        mime, fname = _doc_info(doc)
        if mime.startswith("image"):
            return "[image]"
        if mime.startswith("video"):
            return "[video]"
        if "gif" in mime:
            return "[GIF]"
        if mime.startswith("audio") or "voice" in mime:
            return "[voice]"
        if "webp" in mime or getattr(m, "sticker", None):
            return "[sticker]"
        return f"[file: {fname}]" if fname else "[file]"
    if getattr(m, "web_preview", None) or "MediaWebPage" in type(getattr(m, "media", None)).__name__:
        return "[link]"
    return "[media]" if getattr(m, "media", None) else ""


def _msg_attachment(m, chat_id: int) -> dict | None:
    """A renderable attachment for the reader: an inline image URL for photos /
    image docs, else a labeled chip. None if the message has no media."""
    is_image = bool(getattr(m, "photo", None))
    if not is_image:
        doc = getattr(m, "document", None)
        if doc and (getattr(doc, "mime_type", "") or "").startswith("image"):
            is_image = True
    if is_image:
        return {"name": "photo", "mime": "image/jpeg",
                "url": f"api/telegram-media/{chat_id}/{m.id}", "is_image": True}
    label = _media_label(m)
    if label and label != "[link]":
        return {"name": label.strip("[]"), "mime": "", "url": None, "is_image": False}
    return None


async def _read_dialogs(creds: dict, limit: int) -> list[dict]:
    client = TelegramClient(SESSION, creds["api_id"], creds["api_hash"])
    await client.connect()
    try:
        if not await client.is_user_authorized():
            return []
        out = []
        async for d in client.iter_dialogs(limit=limit):
            m = d.message
            if m is None:
                continue
            is_dm = bool(d.is_user)
            who = d.name or "Telegram"
            text = (getattr(m, "message", "") or _media_label(m) or "[media]").replace("\n", " ")[:200]
            sender = "You" if getattr(m, "out", False) else who
            ts = m.date.timestamp() if getattr(m, "date", None) else 0.0
            out.append({
                "id": f"telegram:{d.id}:{m.id}",
                "source": "telegram",
                "kind": "chat",
                "who": who if is_dm else f"# {who}",
                "addr": "Telegram DM" if is_dm else "Telegram group",
                "subject": sender if is_dm else who,
                "snippet": text,
                "ts": ts,
                "unread": (d.unread_count or 0) > 0,
                "is_dm": is_dm,
                "is_mention": bool(getattr(d, "unread_mentions_count", 0)),
                "url": _entity_url(d.entity),
                "native_id": str(m.id),
                "channel_id": str(d.id),
            })
        return out
    finally:
        await client.disconnect()


async def _read_thread(creds: dict, chat_id: int, limit: int) -> list[dict]:
    client = TelegramClient(SESSION, creds["api_id"], creds["api_hash"])
    await client.connect()
    try:
        if not await client.is_user_authorized():
            return []
        msgs = []
        async for m in client.iter_messages(chat_id, limit=limit):
            sender = await m.get_sender()
            name = getattr(sender, "first_name", None) or getattr(sender, "title", None) or "?"
            att = _msg_attachment(m, chat_id)
            msgs.append({
                "who": "You" if getattr(m, "out", False) else name,
                "text": getattr(m, "message", "") or "",
                "ts": m.date.timestamp() if getattr(m, "date", None) else 0,
                "me": bool(getattr(m, "out", False)),
                "attachments": [att] if att else [],
                "embeds": [],
            })
        return list(reversed(msgs))  # oldest-first for reading
    finally:
        await client.disconnect()


def fetch_telegram(key: str = "telegram") -> list[dict]:
    """Recent Telegram conversations, latest message each (read-only)."""
    creds = _load_creds()
    if not creds:
        return []
    with _lock:
        return asyncio.run(_read_dialogs(creds, DIALOG_LIMIT))


def fetch_telegram_thread(chat_id: str, limit: int = THREAD_LIMIT) -> list[dict]:
    """The recent messages of one Telegram chat, for the reader panel."""
    creds = _load_creds()
    if not creds:
        return []
    try:
        cid = int(chat_id)
    except (TypeError, ValueError):
        return []
    with _lock:
        return asyncio.run(_read_thread(creds, cid, limit))


async def _download_media(creds: dict, chat_id: int, msg_id: int) -> bytes:
    client = TelegramClient(SESSION, creds["api_id"], creds["api_hash"])
    await client.connect()
    try:
        if not await client.is_user_authorized():
            return b""
        m = await client.get_messages(chat_id, ids=msg_id)
        if not m or not m.media:
            return b""
        return await client.download_media(m, file=bytes) or b""
    finally:
        await client.disconnect()


def download_telegram_media(chat_id: str, msg_id: str) -> bytes:
    """Download one message's image bytes (for inline display). Read-only."""
    creds = _load_creds()
    if not creds:
        return b""
    try:
        cid, mid = int(chat_id), int(msg_id)
    except (TypeError, ValueError):
        return b""
    with _lock:
        return asyncio.run(_download_media(creds, cid, mid))

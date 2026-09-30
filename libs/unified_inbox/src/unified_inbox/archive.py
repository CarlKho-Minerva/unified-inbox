"""Archive / unarchive a message in its real mailbox. Nothing is ever deleted.

- Gmail over IMAP: MOVE from INBOX to [Gmail]/All Mail (Gmail's archive).
- Zoho over IMAP: MOVE from INBOX to Archive.
- Gmail API accounts: remove the INBOX label (needs gmail.modify on the grant).

IMAP messages are located by Message-ID, never by the cached sequence number,
and every action refuses unless exactly one message matches.
"""

import imaplib
import json
import re
import subprocess
from collections.abc import Callable

from unified_inbox.fetchers import _load_imap_creds
from unified_inbox.sources import SOURCES

ARCHIVE_FOLDER = {"imap.gmail.com": '"[Gmail]/All Mail"'}
DEFAULT_ARCHIVE_FOLDER = "Archive"


class ArchiveError(Exception):
    pass


def _connect(creds: dict) -> imaplib.IMAP4:
    conn = imaplib.IMAP4_SSL(creds["host"], creds["port"])
    conn.login(creds["email"], creds["password"])
    return conn


def _move_by_message_id(conn: imaplib.IMAP4, src: str, dst: str, message_id: str) -> None:
    if not re.fullmatch(r'<[^<>\s"]+>', message_id):
        raise ArchiveError("no usable Message-ID (refresh and try again)")
    typ, _ = conn.select(src)
    if typ != "OK":
        raise ArchiveError(f"cannot open {src}")
    typ, data = conn.uid("SEARCH", None, "HEADER", "Message-ID", f'"{message_id}"')
    uids = data[0].split() if typ == "OK" and data and data[0] else []
    if len(uids) != 1:
        raise ArchiveError(f"expected 1 match in {src}, found {len(uids)}; did nothing")
    typ, resp = conn.uid("MOVE", uids[0].decode(), dst)
    if typ != "OK":
        raise ArchiveError(f"move failed: {resp}")


def set_archived(
    msg: dict,
    archived: bool,
    connect: Callable[[dict], imaplib.IMAP4] = _connect,
    post_json: Callable[[str, dict], object] | None = None,
    load_creds: Callable[[], dict[str, dict]] = _load_imap_creds,
    sources: dict[str, dict] = SOURCES,
) -> None:
    """archived=True moves it out of the inbox; False puts it back. Raises ArchiveError."""
    cfg = sources.get(msg.get("source", ""), {})
    if msg.get("kind") != "email":
        raise ArchiveError("only email can be archived")
    if cfg.get("via") == "gmail_api":
        body = {"removeLabelIds": ["INBOX"]} if archived else {"addLabelIds": ["INBOX"]}
        post = post_json or _gmail_post
        r = post(f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{msg['native_id']}/modify", body)
        if not isinstance(r, dict) or r.get("error"):
            raise ArchiveError(f"Gmail refused: {str(r)[:200]}")
        return
    creds = load_creds().get(cfg.get("email", ""))
    if not creds:
        raise ArchiveError("no stored credentials for this account")
    folder = ARCHIVE_FOLDER.get(creds["host"], DEFAULT_ARCHIVE_FOLDER)
    conn = connect(creds)
    try:
        src, dst = ("INBOX", folder) if archived else (folder, "INBOX")
        _move_by_message_id(conn, src, dst, msg.get("message_id", ""))
    finally:
        try:
            conn.logout()
        except OSError:
            pass


def _gmail_post(url: str, body: dict) -> object:
    try:
        r = subprocess.run(
            ["latchkey", "curl", "-s", "-m", "20", "-X", "POST", "-H", "Content-Type: application/json",
             "-d", json.dumps(body), url],
            capture_output=True, text=True, timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        return {"error": f"{type(e).__name__}: Gmail did not answer"}
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return {"error": r.stdout[:200] or r.stderr[:200]}

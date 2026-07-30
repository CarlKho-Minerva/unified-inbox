import base64
import datetime
import email
import time
from email.message import EmailMessage

from unified_inbox import fetchers
from unified_inbox.fetchers import (
    JsonFetcher,
    _b64url,
    _extract_imap_content,
    _gh_html_url,
    _gmail_attachments,
    _gmail_extract_body,
    _gmail_normalize,
    _imap_normalize,
    _iso_ts,
    _snippet_from_email,
    _snowflake_ms,
    fetch_chat_thread,
    fetch_discord,
    fetch_github,
    fetch_github_detail,
    fetch_gmail_api,
    fetch_slack,
    refresh_all,
)
from unified_inbox.store import Store

# The published defaults for the Slack allow-list and Discord guilds are empty
# (an adopter fills them in), so the tests inject their own sample scopes.
_SAMPLE_SLACK_CHANNELS = ["C0SAMPLE"]
_SAMPLE_DISCORD_GUILDS = {"111111111111111111": "TestServer"}


def _utc(year: int, month: int, day: int, hour: int = 0) -> float:
    return datetime.datetime(year, month, day, hour, tzinfo=datetime.timezone.utc).timestamp()


# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------
def test_iso_ts_parses_and_falls_back() -> None:
    assert _iso_ts("2026-07-19T14:00:00+00:00") == _utc(2026, 7, 19, 14)
    assert isinstance(_iso_ts(""), float)  # falls back to now, no raise


def test_snowflake_ms_matches_discord_epoch() -> None:
    # A zero-shift snowflake resolves to exactly the Discord epoch.
    assert _snowflake_ms(str(0 << 22)) == fetchers.DISCORD_EPOCH_MS


def test_gh_html_url_rewrites_api_urls() -> None:
    assert _gh_html_url("https://api.github.com/repos/o/r/pulls/7") == "https://github.com/o/r/pull/7"
    assert _gh_html_url("https://api.github.com/repos/o/r/issues/3") == "https://github.com/o/r/issues/3"
    assert _gh_html_url("") == "https://github.com/notifications"


def test_b64url_decodes_and_tolerates_bad_input() -> None:
    encoded = base64.urlsafe_b64encode(b"hello").decode().rstrip("=")
    assert _b64url(encoded) == "hello"
    assert _b64url("!!!not base64!!!") == ""


# --------------------------------------------------------------------------
# Gmail: body/attachment extraction from an API payload tree
# --------------------------------------------------------------------------
def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode()


def test_gmail_extract_body_prefers_html() -> None:
    payload = {
        "mimeType": "multipart/alternative",
        "parts": [
            {"mimeType": "text/plain", "body": {"data": _b64("plain text")}},
            {"mimeType": "text/html", "body": {"data": _b64("<p>rich</p>")}},
        ],
    }
    body, is_html = _gmail_extract_body(payload)
    assert is_html is True
    assert body == "<p>rich</p>"


def test_gmail_extract_body_falls_back_to_plain() -> None:
    payload = {"mimeType": "text/plain", "body": {"data": _b64("only plain")}}
    body, is_html = _gmail_extract_body(payload)
    assert (body, is_html) == ("only plain", False)


def test_gmail_attachments_are_collected_from_nested_parts() -> None:
    payload = {
        "parts": [
            {"mimeType": "text/html", "body": {"data": _b64("hi")}},
            {"filename": "report.pdf", "mimeType": "application/pdf",
             "body": {"attachmentId": "att-1", "size": 2048}},
        ],
    }
    atts = _gmail_attachments(payload)
    assert atts == [{"name": "report.pdf", "mime": "application/pdf", "size": 2048, "ref": "att-1"}]


def test_gmail_normalize_maps_headers_and_flags() -> None:
    msg = {
        "id": "m1",
        "internalDate": str(1_700_000_000_000),
        "labelIds": ["INBOX", "UNREAD"],
        "snippet": "a preview",
        "payload": {"headers": [
            {"name": "From", "value": "Alice <alice@example.com>"},
            {"name": "Subject", "value": "Hello"},
        ]},
    }
    out = _gmail_normalize("primary", msg)
    assert out["id"] == "primary:m1"
    assert out["who"] == "Alice"
    assert out["addr"] == "alice@example.com"
    assert out["subject"] == "Hello"
    assert out["unread"] is True
    assert out["ts"] == 1_700_000_000.0
    assert out["url"].endswith("#inbox/m1")


def test_gmail_normalize_survives_a_message_missing_payload_and_headers() -> None:
    # A bare/malformed Gmail message (no payload, no headers, no dates) must
    # normalize to safe defaults rather than raising.
    out = _gmail_normalize("primary", {"id": "m9"})
    assert out["id"] == "primary:m9"
    assert out["subject"] == "(no subject)"
    assert out["who"] == "" and out["addr"] == ""
    assert out["unread"] is False
    assert isinstance(out["ts"], float) and out["ts"] > 0  # falls back to "now"


# --------------------------------------------------------------------------
# Gmail list fetch: parallel fan-out preserves order and drops failures
# --------------------------------------------------------------------------
def test_fetch_gmail_api_preserves_order_and_skips_failed_reads() -> None:
    def fake(url: str) -> dict | list | None:
        if "maxResults" in url:
            return {"messages": [{"id": "a"}, {"id": "b"}, {"id": "c"}]}
        for mid in ("a", "b", "c"):
            if f"/messages/{mid}?" in url:
                if mid == "b":
                    return None  # simulate a per-message read failure
                return {"id": mid, "internalDate": "0", "payload": {"headers": []}}
        return None

    out = fetch_gmail_api("primary", fake)
    # b failed, so only a and c survive -- and in the original listing order.
    assert [m["native_id"] for m in out] == ["a", "c"]


def test_fetch_gmail_api_empty_listing() -> None:
    assert fetch_gmail_api("primary", lambda url: {}) == []


# --------------------------------------------------------------------------
# IMAP email parsing from a real serialized message
# --------------------------------------------------------------------------
def _build_email() -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = "Bob Sender <bob@example.com>"
    msg["Subject"] = "Quarterly report"
    msg["Date"] = "Sun, 19 Jul 2026 14:00:00 +0000"
    msg.set_content("This is the plain text body with several words in it.")
    msg.add_alternative("<html><body><h1>Hi</h1><p>rich body</p></body></html>", subtype="html")
    msg.add_attachment(b"PDFBYTES", maintype="application", subtype="pdf", filename="q.pdf")
    return msg


def _roundtrip(msg: EmailMessage) -> email.message.Message:
    return email.message_from_bytes(msg.as_bytes())


def test_extract_imap_content_prefers_html_and_lists_attachments() -> None:
    parsed = _roundtrip(_build_email())
    content = _extract_imap_content(parsed)
    assert content["is_html"] is True
    assert "rich body" in content["body"]
    assert len(content["attachments"]) == 1
    att = content["attachments"][0]
    assert att["name"] == "q.pdf"
    assert att["ref"] == "0"
    assert att["size"] == len(b"PDFBYTES")


def test_snippet_from_email_uses_plain_text() -> None:
    parsed = _roundtrip(_build_email())
    snippet = _snippet_from_email(parsed)
    assert snippet.startswith("This is the plain text body")
    assert len(snippet) <= 200


def test_imap_normalize_reads_headers_and_seen_flag() -> None:
    parsed = _roundtrip(_build_email())
    out = _imap_normalize("personal", "42", parsed, (rb"\Seen",))
    assert out["id"] == "personal:42"
    assert out["who"] == "Bob Sender"
    assert out["addr"] == "bob@example.com"
    assert out["subject"] == "Quarterly report"
    assert out["unread"] is False  # \Seen present
    assert out["ts"] == _utc(2026, 7, 19, 14)
    unread = _imap_normalize("personal", "42", parsed, ())
    assert unread["unread"] is True


# --------------------------------------------------------------------------
# Slack
# --------------------------------------------------------------------------
def test_fetch_slack_normalizes_the_latest_message() -> None:
    cid = _SAMPLE_SLACK_CHANNELS[0]
    responses = {
        "conversations.info": {"ok": True, "channel": {"name": "luma-sf", "last_read": "100.0"}},
        "conversations.history": {"ok": True, "messages": [{"user": "U1", "ts": "150.5", "text": "hey there"}]},
        "users.info": {"ok": True, "user": {"real_name": "Dana"}},
    }

    def fake(url: str) -> dict | list | None:
        for needle, payload in responses.items():
            if needle in url:
                return payload
        return None

    out = fetch_slack("slack", fake, channels_allow=_SAMPLE_SLACK_CHANNELS)
    assert len(out) == 1
    m = out[0]
    assert m["who"] == "#luma-sf"
    assert m["subject"] == "Dana"
    assert m["snippet"] == "hey there"
    assert m["channel_id"] == cid
    assert m["unread"] is True  # ts 150.5 > last_read 100.0


def test_fetch_slack_includes_direct_messages_with_deep_links() -> None:
    def fake(url: str) -> dict | list | None:
        if "auth.test" in url:
            return {"ok": True, "team_id": "T99"}
        if "users.conversations" in url:
            return {"ok": True, "channels": [{"id": "D1", "is_im": True, "user": "U9"}]}
        if "conversations.info" in url:
            return {"ok": True, "channel": {"name": "achan", "last_read": "0"}}
        if "conversations.history" in url:
            return {"ok": True, "messages": [{"user": "U9", "ts": "200.0", "text": "dm hi"}]}
        if "users.info" in url:
            return {"ok": True, "user": {"real_name": "Kanjun Qiu"}}
        return None

    out = fetch_slack("slack", fake)
    dms = [m for m in out if m["is_dm"]]
    assert len(dms) == 1
    dm = dms[0]
    assert dm["who"] == "Kanjun Qiu"
    assert dm["subject"] == "Direct message"
    assert dm["channel_id"] == "D1"
    assert "T99/D1" in dm["url"]  # deep-links straight to the DM in Slack


def test_slack_row_normalizes_a_message_missing_user_ts_and_text() -> None:
    # A Slack message stub with no user, timestamp, or text (a bot/system post,
    # or a trimmed payload) must normalize without raising.
    row = fetchers._slack_row("slack", "C1", "T1", "#chan", False, {}, 0.0, lambda url: {})
    assert row["source"] == "slack" and row["channel_id"] == "C1"
    assert row["subject"] == "someone"  # unknown author falls back
    assert row["snippet"] == ""
    assert row["ts"] == 0.0 and row["unread"] is False
    assert row["url"] == "https://app.slack.com/client/T1/C1"


# --------------------------------------------------------------------------
# Discord: seen-cursor unread logic
# --------------------------------------------------------------------------
def _now_snowflake() -> str:
    return str((int(time.time() * 1000) - fetchers.DISCORD_EPOCH_MS) << 22)


def _discord_fake(message_id: str, username: str = "alice") -> JsonFetcher:
    guild = next(iter(_SAMPLE_DISCORD_GUILDS))
    channels = [{"id": "chan1", "type": 0, "last_message_id": _now_snowflake(), "name": "general"}]
    message = [{
        "id": message_id,
        "author": {"username": username, "global_name": username.title()},
        "timestamp": "2026-07-01T00:00:00+00:00",
        "content": "hello world",
    }]

    def fake(url: str) -> dict | list | None:
        if f"/guilds/{guild}/channels" in url:
            return channels
        if "/channels/chan1/messages" in url:
            return message
        return None  # other guild / anything else

    return fake


def test_fetch_discord_first_run_seeds_as_read() -> None:
    out, seen = fetch_discord("discord", {}, _discord_fake("msg1"), pace_seconds=0.0,
                              guilds=_SAMPLE_DISCORD_GUILDS)
    assert len(out) == 1
    assert out[0]["unread"] is False  # first run seeds, no wall of unread
    assert seen["chan1"] == "msg1"


def test_fetch_discord_flags_new_message_as_unread() -> None:
    _, seen = fetch_discord("discord", {}, _discord_fake("msg1"), pace_seconds=0.0,
                            guilds=_SAMPLE_DISCORD_GUILDS)
    out, seen2 = fetch_discord("discord", seen, _discord_fake("msg2"), pace_seconds=0.0,
                               guilds=_SAMPLE_DISCORD_GUILDS)
    assert out[0]["unread"] is True  # cursor moved: msg2 != stored msg1
    assert seen2["chan1"] == "msg2"


def test_fetch_discord_own_messages_never_unread() -> None:
    _, seen = fetch_discord("discord", {}, _discord_fake("msg1"), pace_seconds=0.0,
                            guilds=_SAMPLE_DISCORD_GUILDS)
    out, _ = fetch_discord("discord", seen,
                           _discord_fake("msg2", username=fetchers.DISCORD_SELF_USERNAME),
                           pace_seconds=0.0, guilds=_SAMPLE_DISCORD_GUILDS)
    assert out[0]["unread"] is False  # authored by the user


# --------------------------------------------------------------------------
# GitHub
# --------------------------------------------------------------------------
def test_fetch_github_merges_notifications_and_search_without_duplicates() -> None:
    responses = {
        "/user": {"login": "octocat"},
        "/notifications": [{
            "subject": {"title": "Fix the bug", "url": "https://api.github.com/repos/o/r/issues/1", "type": "Issue"},
            "repository": {"full_name": "o/r"},
            "reason": "mention",
            "unread": True,
            "updated_at": "2026-07-19T10:00:00+00:00",
        }],
        "/search/issues": {"items": [
            {"html_url": "https://github.com/o/r/issues/1", "title": "dup"},  # already seen -> dropped
            {"html_url": "https://github.com/o/r/pull/2", "title": "New PR",
             "number": 2, "state": "open", "pull_request": {}, "repository_url": "https://api.github.com/repos/o/r",
             "updated_at": "2026-07-18T10:00:00+00:00"},
        ]},
    }

    def fake(url: str) -> dict | list | None:
        # /notifications also contains "/user"? no. Order longest-specific first.
        if "/notifications" in url:
            return responses["/notifications"]
        if "/search/issues" in url:
            return responses["/search/issues"]
        if "/user" in url:
            return responses["/user"]
        return None

    out = fetch_github("github", fake)
    urls = [m["url"] for m in out]
    assert "https://github.com/o/r/issues/1" in urls
    assert "https://github.com/o/r/pull/2" in urls
    assert len(urls) == len(set(urls))  # the duplicate issue was deduped
    notif = next(m for m in out if m["url"].endswith("/issues/1"))
    assert notif["is_mention"] is True
    assert notif["unread"] is True


def test_fetch_github_detail_builds_a_header() -> None:
    def fake(url: str) -> dict | list | None:
        return {"title": "T", "state": "open", "user": {"login": "octocat"}, "body": "the body"}

    detail = fetch_github_detail("https://api.github.com/repos/o/r/issues/1", fake)
    assert "T" in detail["body"]
    assert "OPEN" in detail["body"]
    assert "the body" in detail["body"]
    assert detail["is_html"] is False


def test_fetch_github_detail_handles_missing_url() -> None:
    assert fetch_github_detail("", lambda url: None) == {"body": "", "is_html": False}


# --------------------------------------------------------------------------
# Chat thread detail
# --------------------------------------------------------------------------
def test_fetch_chat_thread_discord_shape() -> None:
    def fake(url: str) -> dict | list | None:
        return [
            {"author": {"username": "alice", "global_name": "Alice"}, "content": "first",
             "timestamp": "2026-07-01T00:00:00+00:00", "attachments": [], "embeds": []},
            {"author": {"username": fetchers.DISCORD_SELF_USERNAME}, "content": "reply",
             "timestamp": "2026-07-01T00:01:00+00:00", "attachments": [], "embeds": []},
        ]

    thread = fetch_chat_thread("discord", "chan1", limit=20, get_json=fake)
    # Returned oldest-first (the fetcher reverses the API's newest-first list).
    assert [t["text"] for t in thread] == ["reply", "first"]
    assert thread[0]["me"] is True  # the self-username marks the user's own message


# --------------------------------------------------------------------------
# Orchestration: refresh_all
# --------------------------------------------------------------------------
def _orchestration_fake() -> JsonFetcher:
    guild = next(iter(_SAMPLE_DISCORD_GUILDS))
    channels = [{"id": "chan1", "type": 0, "last_message_id": _now_snowflake(), "name": "general"}]

    def fake(url: str) -> dict | list | None:
        if "gmail.googleapis.com" in url and "maxResults" in url:
            return {"messages": [{"id": "g1"}]}
        if "gmail.googleapis.com" in url and "/messages/g1" in url:
            return {"id": "g1", "internalDate": str(3000 * 1000), "payload": {"headers": []}}
        if "conversations.info" in url:
            return {"ok": True, "channel": {"name": "luma-sf", "last_read": "0"}}
        if "conversations.history" in url:
            return {"ok": True, "messages": [{"user": "U1", "ts": "2000.0", "text": "slack msg"}]}
        if "users.info" in url:
            return {"ok": True, "user": {"real_name": "Dana"}}
        if f"/guilds/{guild}/channels" in url:
            return channels
        if "/channels/chan1/messages" in url:
            return [{"id": "d1", "author": {"username": "alice"}, "timestamp": "2026-07-01T00:00:00+00:00",
                     "content": "discord msg"}]
        if url.rstrip("/").endswith("/user"):
            return {"login": "octocat"}
        if "/notifications" in url:
            return [{"subject": {"title": "gh", "url": "", "type": "Issue"}, "repository": {"full_name": "o/r"},
                     "reason": "assign", "unread": False, "updated_at": "2026-07-10T00:00:00+00:00"}]
        if "/search/issues" in url:
            return {"items": []}
        return None

    return fake


def _fake_events() -> tuple[list[dict], dict]:
    return [{"id": "cal1", "start": 500}], {"google_calendar": {"ok": True, "count": 1},
                                            "zoho_calendar": {"ok": True, "count": 0}}


def _fake_luma() -> tuple[list[dict], dict]:
    return [{"id": "luma:1", "name": "Mixer", "start": 600}], {"ok": True, "count": 1}


def _fake_tasks() -> tuple[list[dict], dict]:
    return [{"id": "task:F:0", "text": "do it", "file": "F"}], {"ok": True, "count": 1}


def _refresh(store: Store, **overrides: object) -> dict:
    # Always inject the Luma/vault fetchers so the suite never reaches the live
    # Luma discover feed or the WebDAV endpoint.
    kwargs = {
        "get_json": _orchestration_fake(),
        "fetch_events": _fake_events,
        "fetch_luma_events": _fake_luma,
        "fetch_vault_tasks": _fake_tasks,
        "load_imap_creds": lambda: {},
        "slack_channels": _SAMPLE_SLACK_CHANNELS,
        "discord_guilds": _SAMPLE_DISCORD_GUILDS,
    }
    kwargs.update(overrides)
    return refresh_all(store, **kwargs)


def test_refresh_all_merges_sorts_and_isolates(store: Store) -> None:
    meta = _refresh(store)
    msgs = store.get_messages()
    # Sorted newest-first by ts.
    timestamps = [m["ts"] for m in msgs]
    assert timestamps == sorted(timestamps, reverse=True)
    # gmail + slack + discord + github all contributed.
    sources = {m["source"] for m in msgs}
    assert {"primary", "slack", "discord", "github"} <= sources
    # IMAP accounts with no creds are isolated, recorded, and skipped.
    assert meta["sources"]["personal"] == {"ok": False, "error": "no stored credentials"}
    # Calendar results and counts flow into meta.
    assert store.get_events() == [{"id": "cal1", "start": 500}]
    assert meta["events"] == 1
    assert meta["total"] == len(msgs)
    assert meta["sources"]["google_calendar"] == {"ok": True, "count": 1}


def test_refresh_all_persists_luma_and_tasks_with_their_status(store: Store) -> None:
    meta = _refresh(store)
    # Luma and vault tasks land in their own store slots and meta counts/status.
    assert store.get_luma() == [{"id": "luma:1", "name": "Mixer", "start": 600}]
    assert store.get_tasks() == [{"id": "task:F:0", "text": "do it", "file": "F"}]
    assert meta["luma"] == 1
    assert meta["tasks"] == 1
    assert meta["sources"]["luma"] == {"ok": True, "count": 1}
    assert meta["sources"]["tasks"] == {"ok": True, "count": 1}


def test_refresh_all_isolates_a_failing_luma_or_tasks_fetch(store: Store) -> None:
    def bad_luma() -> tuple[list[dict], dict]:
        return [], {"ok": False, "error": "ConnectionError: luma down"}

    meta = _refresh(store, fetch_luma_events=bad_luma)
    # A failed Luma fetch is recorded but the messages still refresh.
    assert store.get_luma() == []
    assert meta["sources"]["luma"]["ok"] is False
    assert store.get_messages()  # other sources unaffected


def test_refresh_all_is_deterministic(store: Store) -> None:
    fake = _orchestration_fake()
    _refresh(store, get_json=fake)
    first = [m["id"] for m in store.get_messages()]
    _refresh(store, get_json=fake)
    second = [m["id"] for m in store.get_messages()]
    assert first == second


def test_refresh_all_records_a_source_that_raises(store: Store) -> None:
    def exploding(url: str) -> dict | list | None:
        if "gmail.googleapis.com" in url:
            raise ConnectionError("gmail is down")
        return _orchestration_fake()(url)

    meta = _refresh(store, get_json=exploding)
    assert meta["sources"]["primary"]["ok"] is False
    assert "ConnectionError" in meta["sources"]["primary"]["error"]
    # Other sources still succeeded despite gmail blowing up.
    assert meta["sources"]["slack"]["ok"] is True


# --------------------------------------------------------------------------
# Two-cadence refresh: due_sources gates which message sources are fetched and
# refresh_aux gates the calendars/Luma/tasks fetches, so a fast tick can poll
# email+GitHub often while leaving rate-sensitive chat (Discord) on the slow
# full cycle.
# --------------------------------------------------------------------------


def test_refresh_all_due_sources_skips_others_and_carries_them_forward(store: Store) -> None:
    _refresh(store)  # a full cycle populates every source
    discord_before = [m for m in store.get_messages() if m["source"] == "discord"]
    assert discord_before, "the full cycle should have loaded Discord messages"
    discord_status_before = store.get_meta()["sources"]["discord"]
    assert discord_status_before["ok"] is True

    # A get_json that refuses any non-GitHub URL proves the skipped sources are
    # never contacted on this cycle; a re-fetched Discord would drop its carried
    # messages and flip its status to ok=False instead.
    def github_only(url: str) -> dict | list | None:
        if "/notifications" in url or "/search/issues" in url or url.rstrip("/").endswith("/user"):
            return _orchestration_fake()(url)
        raise AssertionError(f"a non-due source was fetched: {url}")

    meta = _refresh(store, due_sources={"github"}, refresh_aux=False, get_json=github_only)
    # Discord carried forward untouched, with its previous status preserved.
    assert [m for m in store.get_messages() if m["source"] == "discord"] == discord_before
    assert meta["sources"]["discord"] == discord_status_before
    # GitHub was the one source actually refreshed this cycle.
    assert meta["sources"]["github"]["ok"] is True


def test_refresh_all_refresh_aux_false_carries_aux_forward_without_fetching(store: Store) -> None:
    _refresh(store)  # a full cycle populates events / Luma / tasks

    calls = {"n": 0}

    def exploding_aux() -> tuple[list[dict], dict]:
        calls["n"] += 1
        raise AssertionError("aux fetchers must not run when refresh_aux is False")

    meta = _refresh(
        store, due_sources={"github"}, refresh_aux=False,
        fetch_events=exploding_aux, fetch_luma_events=exploding_aux, fetch_vault_tasks=exploding_aux,
    )
    assert calls["n"] == 0
    # Aux data, its counts, and its status all carry from the previous full cycle.
    assert store.get_events() == [{"id": "cal1", "start": 500}]
    assert store.get_luma() == [{"id": "luma:1", "name": "Mixer", "start": 600}]
    assert store.get_tasks() == [{"id": "task:F:0", "text": "do it", "file": "F"}]
    assert meta["events"] == 1 and meta["luma"] == 1 and meta["tasks"] == 1
    assert meta["sources"]["luma"] == {"ok": True, "count": 1}
    assert meta["sources"]["tasks"] == {"ok": True, "count": 1}
    assert meta["sources"]["google_calendar"] == {"ok": True, "count": 1}


def test_refresh_all_fast_tick_surfaces_a_new_due_message_while_carrying_chat(store: Store) -> None:
    _refresh(store)  # full cycle
    n_before = len(store.get_messages())
    discord_before = [m for m in store.get_messages() if m["source"] == "discord"]
    assert discord_before

    # A second GitHub notification has arrived since the full cycle.
    def with_new_github(url: str) -> dict | list | None:
        if "/notifications" in url:
            return [
                {"subject": {"title": "gh", "url": "", "type": "Issue"}, "repository": {"full_name": "o/r"},
                 "reason": "assign", "unread": False, "updated_at": "2026-07-10T00:00:00+00:00"},
                {"subject": {"title": "brand new", "url": "", "type": "Issue"}, "repository": {"full_name": "o/r"},
                 "reason": "mention", "unread": True, "updated_at": "2026-07-20T00:00:00+00:00"},
            ]
        return _orchestration_fake()(url)

    _refresh(store, due_sources={"github"}, refresh_aux=False, get_json=with_new_github)
    after = store.get_messages()
    # The new GitHub item shows up on the fast tick (one more message than before)...
    assert len(after) == n_before + 1
    assert any("brand new" in m.get("subject", "") for m in after if m["source"] == "github")
    # ...and Discord messages are carried forward untouched.
    assert [m for m in after if m["source"] == "discord"] == discord_before

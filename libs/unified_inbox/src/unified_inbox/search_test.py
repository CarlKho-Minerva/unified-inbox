import urllib.parse

from unified_inbox.search import MAX_QUERY_LEN, _search_imap, _search_slack, search_all


def _fake_get_json(url: str) -> dict | list | None:
    if "gmail.googleapis.com" in url and "/messages?" in url:
        return {"messages": [{"id": "g1"}]}
    if "gmail.googleapis.com" in url and "/messages/g1" in url:
        return {
            "id": "g1",
            "internalDate": "1700000000000",
            "labelIds": ["INBOX"],
            "snippet": "old stripe email",
            "payload": {"headers": [
                {"name": "From", "value": "Stripe <no-reply@stripe.com>"},
                {"name": "Subject", "value": "Your payout"},
            ]},
        }
    if "slack.com/api/search.messages" in url:
        return {"ok": True, "messages": {"matches": [{
            "ts": "1699999999.0", "text": "stripe payout hit", "permalink": "https://slack/x",
            "username": "someone", "channel": {"id": "C1", "name": "finance", "is_channel": True},
        }]}}
    return None


def test_search_all_returns_deep_email_and_slack_results() -> None:
    # No IMAP creds -> IMAP sources skipped; gmail + slack come from the fake.
    results = search_all("stripe", get_json=_fake_get_json, load_imap_creds=lambda: {})
    by_source = {r["source"] for r in results}
    assert "primary" in by_source  # gmail-api source
    assert "slack" in by_source
    assert all(r["deep"] for r in results)

    gmail_row = next(r for r in results if r["source"] == "primary")
    assert gmail_row["subject"] == "Your payout"
    assert "native_id" in gmail_row  # so it can open its body from the id

    slack_row = next(r for r in results if r["source"] == "slack")
    assert slack_row["open_external"] is True
    assert slack_row["url"] == "https://slack/x"


def test_search_all_empty_query_returns_nothing() -> None:
    assert search_all("   ", get_json=_fake_get_json, load_imap_creds=lambda: {}) == []


def test_search_all_isolates_a_failing_source() -> None:
    def boom(url: str) -> dict | list | None:
        raise RuntimeError("provider down")

    # A throwing provider must not raise out of search_all.
    assert search_all("x", get_json=boom, load_imap_creds=lambda: {}) == []


# --- _search_imap: parse a real RFC822-header IMAP fetch into deep rows ---


class _FakeImap:
    """Minimal stand-in for imaplib.IMAP4_SSL exposing only what _search_imap
    touches, so the RFC822-header parse path runs offline against fixtures."""

    def __init__(self, matches: dict[bytes, tuple[bytes, bytes]]) -> None:
        self._matches = matches
        self.selected_readonly: bool | None = None
        self.logged_out = False
        self.last_criteria: tuple[str, ...] = ()

    def login(self, email_addr: str, password: str) -> None:
        pass

    def select(self, mailbox: str, readonly: bool = False) -> tuple[str, list]:
        self.selected_readonly = readonly
        return "OK", [b""]

    def search(self, charset, *criteria: str) -> tuple[str, list[bytes]]:
        self.last_criteria = criteria
        return "OK", [b" ".join(self._matches)]

    def fetch(self, uid: bytes, spec: str) -> tuple[str, list]:
        return "OK", [self._matches[uid]]

    def logout(self) -> tuple[str, list]:
        self.logged_out = True
        return "BYE", [b""]


def _rfc822(subject: str, sender: str, seen: bool) -> tuple[bytes, bytes]:
    flags = rb"1 (FLAGS (\Seen))" if seen else rb"1 (FLAGS ())"
    header = f"From: {sender}\r\nSubject: {subject}\r\nDate: Mon, 20 Jul 2026 10:00:00 +0000\r\n\r\n"
    return flags, header.encode()


def test_search_imap_normalizes_header_only_matches_newest_first() -> None:
    fake = _FakeImap({
        b"1": _rfc822("Older payout", "Stripe <no-reply@stripe.com>", seen=True),
        b"2": _rfc822("Newer payout", "Stripe <no-reply@stripe.com>", seen=False),
    })
    creds = {"host": "imap.example.com", "port": 993, "email": "a@b.com", "password": "x"}
    rows = _search_imap("personal", creds, "payout", connect=lambda host, port: fake)

    assert fake.selected_readonly is True  # never mutate the mailbox
    assert fake.logged_out is True
    assert [r["subject"] for r in rows] == ["Newer payout", "Older payout"]  # UID 2 then 1
    assert all(r["deep"] and r["snippet"] == "" for r in rows)  # header-only: body loads on open
    assert rows[0]["unread"] is True and rows[1]["unread"] is False


def test_search_imap_returns_empty_when_no_matches() -> None:
    fake = _FakeImap({})
    creds = {"host": "h", "port": 993, "email": "a@b.com", "password": "x"}
    assert _search_imap("personal", creds, "nothing", connect=lambda host, port: fake) == []


class _ErrorImap(_FakeImap):
    def search(self, charset, *criteria: str) -> tuple[str, list]:
        return "NO", [b"parse error"]  # server rejected the search


def test_search_imap_returns_empty_when_the_server_rejects_the_search() -> None:
    # A non-OK SEARCH response must yield [] (not raise, not treat the error text
    # as a match list). Distinct from the zero-match case above.
    creds = {"host": "h", "port": 993, "email": "a@b.com", "password": "x"}
    fake = _ErrorImap({b"1": _rfc822("x", "a@b.com", seen=False)})
    assert _search_imap("personal", creds, "boom", connect=lambda host, port: fake) == []


# --- odd queries: special characters, unicode/emoji, and length bounding ---


def test_search_slack_url_encodes_special_characters() -> None:
    captured: dict = {}

    def fake(url: str) -> dict:
        captured["url"] = url
        return {"ok": True, "messages": {"matches": []}}

    _search_slack('a "quoted" b/c é \U0001f600', fake)
    # The raw special chars must be percent-encoded into the query string, never
    # interpolated verbatim (which would break the URL or the provider request).
    assert '"' not in captured["url"] and " " not in captured["url"]
    assert urllib.parse.quote('\U0001f600') in captured["url"]


def test_search_imap_tolerates_unicode_and_special_char_words() -> None:
    # Each whitespace-delimited token (including emoji / punctuation) becomes its
    # own TEXT term; nothing raises on non-ascii input.
    fake = _FakeImap({})
    creds = {"host": "h", "port": 993, "email": "a@b.com", "password": "x"}
    _search_imap("personal", creds, 'café "quote" \U0001f600', connect=lambda host, port: fake)
    assert fake.last_criteria == ("TEXT", "café", "TEXT", '"quote"', "TEXT", "\U0001f600")


def test_search_all_bounds_a_pathologically_long_query() -> None:
    seen: dict = {}

    def fake(url: str) -> dict | None:
        if "slack.com/api/search.messages" in url:
            seen["slack_url_len"] = len(url)
            return {"ok": True, "messages": {"matches": []}}
        return None  # gmail listing -> nothing

    search_all("x" * 5000, get_json=fake, load_imap_creds=lambda: {})
    # The query is capped, so the request URL can't grow without bound.
    assert seen["slack_url_len"] < 5000
    assert MAX_QUERY_LEN == 256


# --- provider payloads that are ok:false, None, or the wrong type ---


def test_search_slack_ignores_not_ok_and_non_dict_payloads() -> None:
    assert _search_slack("q", lambda url: {"ok": False, "error": "ratelimited"}) == []
    assert _search_slack("q", lambda url: None) == []
    assert _search_slack("q", lambda url: ["unexpected", "list"]) == []


def test_search_all_skips_a_source_returning_ok_false_or_none() -> None:
    def fake(url: str) -> dict | list | None:
        if "gmail.googleapis.com" in url:
            return None  # gmail listing returns nothing
        if "slack.com/api/search.messages" in url:
            return {"ok": False}  # slack declined
        return None

    # Neither bad provider response raises; the combined result is just empty.
    assert search_all("stripe", get_json=fake, load_imap_creds=lambda: {}) == []


def test_search_imap_splits_a_multi_word_query_into_per_word_terms() -> None:
    # imaplib does not quote args, so a multi-word query must become one TEXT
    # term per word (ANDed) -- otherwise the server rejects the whole command and
    # every IMAP account silently returns nothing.
    fake = _FakeImap({})
    creds = {"host": "h", "port": 993, "email": "a@b.com", "password": "x"}
    _search_imap("personal", creds, "  stripe   payout ", connect=lambda host, port: fake)
    assert fake.last_criteria == ("TEXT", "stripe", "TEXT", "payout")

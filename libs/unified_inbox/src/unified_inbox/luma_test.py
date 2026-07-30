import datetime

from unified_inbox.luma import (
    LUMA_HOME_URL,
    LUMA_SF_URL,
    _epoch,
    _normalize,
    _prosemirror_to_html,
    fetch_luma,
    fetch_luma_event,
    fetch_luma_sf,
    fetch_my_luma_events,
)


def _utc(year: int, month: int, day: int, hour: int = 0) -> float:
    return datetime.datetime(year, month, day, hour, tzinfo=datetime.timezone.utc).timestamp()


# --- _epoch: the ISO shapes Luma emits ---


def test_epoch_parses_zulu_and_offset() -> None:
    assert _epoch("2026-07-19T14:00:00Z") == _utc(2026, 7, 19, 14)
    assert _epoch("2026-07-19T14:00:00+00:00") == _utc(2026, 7, 19, 14)


def test_epoch_returns_zero_for_garbage_rather_than_raising() -> None:
    assert _epoch("") == 0.0
    assert _epoch("not a date") == 0.0
    assert _epoch(None) == 0.0  # type: ignore[arg-type]


# --- _normalize: one discover entry -> the view's flat shape ---


def _entry(**event: object) -> dict:
    return {"event": event}


def test_normalize_maps_the_core_fields() -> None:
    entry = _entry(
        api_id="evt-123",
        name="SF Founders Mixer",
        start_at="2026-07-20T18:00:00Z",
        end_at="2026-07-20T21:00:00Z",
        cover_url="https://cdn.lu.ma/cover.jpg",
        url="sf-founders-mixer",
        geo_address_info={"sublocality": "SoMa"},
    )
    out = _normalize(entry)
    assert out == {
        "id": "luma:evt-123",
        "name": "SF Founders Mixer",
        "start": _utc(2026, 7, 20, 18),
        "end": _utc(2026, 7, 20, 21),
        "cover": "https://cdn.lu.ma/cover.jpg",
        "url": "https://lu.ma/sf-founders-mixer",
        "location": "SoMa",
        "status": "discover",  # default status when not one of the user's registered events
    }


def test_normalize_carries_the_registration_status() -> None:
    entry = _entry(api_id="e", name="My Event", start_at="2026-07-20T18:00:00Z")
    assert _normalize(entry, "going")["status"] == "going"


def test_normalize_skips_entries_without_a_name() -> None:
    assert _normalize(_entry(api_id="x")) is None
    assert _normalize({}) is None


def test_normalize_virtual_events_are_labelled_virtual() -> None:
    out = _normalize(_entry(name="Online talk", location_type="virtual", geo_address_info={"city": "SF"}))
    assert out is not None
    assert out["location"] == "Virtual"


def test_normalize_location_falls_back_through_geo_fields() -> None:
    # city_state is used when sublocality is absent.
    out = _normalize(_entry(name="E", geo_address_info={"city_state": "San Francisco, CA"}))
    assert out is not None
    assert out["location"] == "San Francisco, CA"
    # No geo info at all -> the default city.
    plain = _normalize(_entry(name="E"))
    assert plain is not None
    assert plain["location"] == "San Francisco"


def test_normalize_falls_back_to_social_image_and_generic_url() -> None:
    out = _normalize(_entry(name="E", social_image_url="https://cdn.lu.ma/social.jpg"))
    assert out is not None
    assert out["cover"] == "https://cdn.lu.ma/social.jpg"
    # No slug -> a generic lu.ma/sf link rather than a broken one.
    assert out["url"] == "https://lu.ma/sf"


# --- fetch_luma_sf: the injected-fetcher entry point ---


def test_fetch_luma_sf_normalizes_sorts_and_drops_bad_entries() -> None:
    payload = {
        "entries": [
            {"event": {"api_id": "b", "name": "Later", "start_at": "2026-07-21T10:00:00Z", "url": "later"}},
            {"event": {"name": "", "start_at": "2026-07-01T10:00:00Z"}},  # no name -> dropped
            {"event": {"api_id": "a", "name": "Sooner", "start_at": "2026-07-20T10:00:00Z", "url": "sooner"}},
        ]
    }
    events = fetch_luma_sf(lambda url: payload)
    # Bad entry dropped; the rest sorted soonest-first by start.
    assert [e["name"] for e in events] == ["Sooner", "Later"]


def test_fetch_luma_sf_returns_empty_on_missing_or_malformed_data() -> None:
    assert fetch_luma_sf(lambda url: None) == []
    assert fetch_luma_sf(lambda url: {"no_entries_key": 1}) == []
    assert fetch_luma_sf(lambda url: {"entries": []}) == []


def test_fetch_luma_sf_targets_the_sf_discover_feed() -> None:
    seen = {}

    def capture(url: str) -> dict:
        seen["url"] = url
        return {"entries": []}

    fetch_luma_sf(capture)
    assert seen["url"] == LUMA_SF_URL
    assert "slug=sf" in seen["url"]


# --- fetch_luma: the (events, status) wrapper used by the refresh ---


def test_fetch_luma_reports_ok_status_with_a_count() -> None:
    payload = {"entries": [{"event": {"api_id": "a", "name": "Party", "start_at": "2026-07-20T10:00:00Z", "url": "p"}}]}
    events, status = fetch_luma(lambda url: payload)
    assert [e["name"] for e in events] == ["Party"]
    assert status["ok"] is True
    assert status["count"] == 1
    assert "ms" in status


def test_fetch_luma_records_a_failure_without_raising() -> None:
    def boom(url: str) -> dict:
        raise ConnectionError("luma is down")

    events, status = fetch_luma(boom)
    assert events == []
    assert status["ok"] is False
    assert "ConnectionError" in status["error"]


# --- fetch_my_luma_events: the user's registered events, tagged by RSVP status ---


def _mine_entry(status: object, api_id: str = "e", name: str = "My Event") -> dict:
    guest = {"guest_info": {"approval_status": status}} if status is not None else {}
    return {**guest, "event": {"api_id": api_id, "name": name, "start_at": "2026-07-20T18:00:00Z", "url": api_id}}


def test_fetch_my_luma_events_maps_each_approval_status() -> None:
    payload = {"entries": [
        _mine_entry("approved", "a"),
        _mine_entry("pending_approval", "b"),
        _mine_entry("waitlist", "c"),
        _mine_entry("invited", "d"),
        _mine_entry("some_new_status", "e"),  # unknown -> invited
        _mine_entry(None, "f"),               # missing guest_info -> invited
    ]}
    events = fetch_my_luma_events("tok", lambda url, token: payload)
    assert {e["id"]: e["status"] for e in events} == {
        "luma:a": "going", "luma:b": "pending", "luma:c": "pending",
        "luma:d": "invited", "luma:e": "invited", "luma:f": "invited",
    }


def test_fetch_my_luma_events_without_a_token_makes_no_request() -> None:
    called = {"n": 0}

    def fetcher(url: str, token: str) -> dict:
        called["n"] += 1
        return {"entries": []}

    assert fetch_my_luma_events("", fetcher) == []
    assert called["n"] == 0  # no token -> never hits the network


def test_fetch_my_luma_events_passes_token_to_the_home_endpoint() -> None:
    seen: dict = {}

    def fetcher(url: str, token: str) -> dict:
        seen["url"], seen["token"] = url, token
        return {"entries": []}

    fetch_my_luma_events("secret-cookie", fetcher)
    assert seen["url"] == LUMA_HOME_URL
    assert seen["token"] == "secret-cookie"


def test_fetch_my_luma_events_empty_on_expired_session_or_malformed() -> None:
    assert fetch_my_luma_events("tok", lambda url, token: None) == []
    assert fetch_my_luma_events("tok", lambda url, token: {"no_entries": 1}) == []


# --- fetch_luma: merging his registered events over the discover feed ---


def test_fetch_luma_merges_his_status_over_discover_with_counts() -> None:
    discover = {"entries": [
        {"event": {"api_id": "shared", "name": "Shared", "start_at": "2026-07-20T10:00:00Z", "url": "shared"}},
        {"event": {"api_id": "disc", "name": "Discover only", "start_at": "2026-07-21T10:00:00Z", "url": "disc"}},
    ]}
    mine = {"entries": [
        _mine_entry("approved", "shared", "Shared"),  # same id -> his "going" wins over "discover"
        _mine_entry("pending_approval", "mine", "Mine only"),
    ]}
    events, status = fetch_luma(lambda url: discover, lambda url, token: mine, token="tok")
    by_id = {e["id"]: e["status"] for e in events}
    assert by_id == {"luma:shared": "going", "luma:disc": "discover", "luma:mine": "pending"}
    assert status["ok"] is True
    assert status["count"] == 3
    assert status["mine"] == 2
    assert status["signed_in"] is True
    assert status["by_status"] == {"going": 1, "discover": 1, "pending": 1}


def test_fetch_luma_without_a_token_is_discover_only_and_not_signed_in() -> None:
    discover = {"entries": [
        {"event": {"api_id": "d", "name": "D", "start_at": "2026-07-20T10:00:00Z", "url": "d"}},
    ]}
    calls = {"cookie": 0}

    def cookie_fetch(url: str, token: str) -> dict:
        calls["cookie"] += 1
        return {"entries": []}

    events, status = fetch_luma(lambda url: discover, cookie_fetch, token="")
    assert [e["status"] for e in events] == ["discover"]
    assert status["signed_in"] is False
    assert status["mine"] == 0
    assert calls["cookie"] == 0  # empty token short-circuits before the cookie call


# --- fetch_luma_event: the detail-panel payload from Luma's event/get API ---


def _event_get_payload() -> dict:
    # The nested shape Luma's /event/get returns, as consumed by the detail panel.
    return {
        "event": {"geo_address_info": {"full_address": "500 Terry Francois Blvd, SF"}},
        "calendar": {"name": "SF Tech Week", "description_short": "A week of talks."},
        "hosts": [{"name": "Ada"}, {"name": "Grace"}, {"no_name": True}],
        "guest_count": 212,
    }


def test_fetch_luma_event_parses_the_detail_shape() -> None:
    captured: dict = {}

    def fake(url: str) -> dict:
        captured["url"] = url
        return _event_get_payload()

    detail = fetch_luma_event("luma:evt-42", fake)
    assert "event_api_id=evt-42" in captured["url"]  # strips the "luma:" prefix
    assert detail["hosts"] == ["Ada", "Grace"]  # hosts without a name are dropped
    assert detail["guest_count"] == 212
    assert detail["location"] == "500 Terry Francois Blvd, SF"
    assert detail["calendar"] == "SF Tech Week"
    assert detail["description"] == "A week of talks."


def test_fetch_luma_event_renders_the_full_prose_description() -> None:
    payload = {
        "description_mirror": {
            "type": "doc",
            "content": [
                {"type": "heading", "attrs": {"level": 1},
                 "content": [{"type": "text", "text": "Overview", "marks": [{"type": "bold"}]}]},
                {"type": "paragraph", "content": [{"type": "text", "text": "Join us in SF."}]},
            ],
        },
        "event": {"timezone": "America/Los_Angeles"},
    }
    detail = fetch_luma_event("evt-1", lambda url: payload)
    assert detail["description_html"] == "<h3><strong>Overview</strong></h3><p>Join us in SF.</p>"
    assert detail["timezone"] == "America/Los_Angeles"


def test_fetch_luma_event_marks_virtual_events_as_online() -> None:
    payload = {"event": {"location_type": "virtual"}}
    assert fetch_luma_event("evt-1", lambda url: payload)["location"] == "Online"


def test_fetch_luma_event_location_falls_back_through_geo_fields() -> None:
    payload = {"event": {"geo_address_info": {"city": "San Francisco"}}}
    detail = fetch_luma_event("evt-9", lambda url: payload)
    assert detail["location"] == "San Francisco"  # no full_address -> city fallback
    assert detail["hosts"] == []
    assert detail["guest_count"] == 0


def test_fetch_luma_event_returns_empty_on_malformed_response() -> None:
    assert fetch_luma_event("evt-1", lambda url: None) == {}
    assert fetch_luma_event("evt-2", lambda url: ["unexpected"]) == {}


# --- _prosemirror_to_html: Luma's rich-text doc -> safe HTML ---


def test_prosemirror_renders_marks_lists_and_links() -> None:
    doc = {"type": "doc", "content": [
        {"type": "paragraph", "content": [
            {"type": "text", "text": "plain "},
            {"type": "text", "text": "bold", "marks": [{"type": "bold"}]},
            {"type": "text", "text": " and ", "marks": []},
            {"type": "text", "text": "link", "marks": [{"type": "link", "attrs": {"href": "https://lu.ma/x"}}]},
        ]},
        {"type": "bulletList", "content": [
            {"type": "listItem", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "one"}]}]},
        ]},
    ]}
    html = _prosemirror_to_html(doc)
    assert "plain <strong>bold</strong> and " in html
    assert '<a href="https://lu.ma/x" target="_blank" rel="noopener">link</a>' in html
    assert "<ul><li><p>one</p></li></ul>" in html


def test_prosemirror_escapes_text_and_strips_unsafe_links() -> None:
    doc = {"type": "doc", "content": [
        {"type": "paragraph", "content": [
            {"type": "text", "text": "<script>", "marks": [{"type": "link", "attrs": {"href": "javascript:alert(1)"}}]},
        ]},
    ]}
    html = _prosemirror_to_html(doc)
    assert "&lt;script&gt;" in html  # text is escaped
    assert "javascript:" not in html  # unsafe scheme dropped, so no <a> wrapper
    assert "<a " not in html


def test_prosemirror_renders_the_remaining_block_nodes() -> None:
    doc = {"type": "doc", "content": [
        {"type": "heading", "attrs": {"level": 2},
         "content": [{"type": "text", "text": "Line 1"}, {"type": "hardBreak"},
                     {"type": "text", "text": "Line 2"}]},
        {"type": "orderedList", "content": [
            {"type": "listItem", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "first"}]}]},
        ]},
        {"type": "blockquote", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "quoted"}]}]},
        {"type": "horizontalRule"},
        {"type": "image", "attrs": {"src": "https://cdn.lu.ma/pic.png"}},
        {"type": "image", "attrs": {"src": "http://insecure/pic.png"}},  # non-https dropped
        {"type": "callout", "content": [{"type": "text", "text": "unknown-block text"}]},  # fallback
    ]}
    html = _prosemirror_to_html(doc)
    assert "<h4>Line 1<br>Line 2</h4>" in html  # level 2 -> h4, hard break kept
    assert "<ol><li><p>first</p></li></ol>" in html
    assert "<blockquote><p>quoted</p></blockquote>" in html
    assert "<hr>" in html
    assert '<img src="https://cdn.lu.ma/pic.png" alt="" loading="lazy">' in html
    assert "http://insecure" not in html  # non-https image dropped
    assert "<p>unknown-block text</p>" in html  # unknown block falls back to its inline text


def test_prosemirror_returns_empty_for_a_missing_or_malformed_doc() -> None:
    assert _prosemirror_to_html(None) == ""
    assert _prosemirror_to_html({"type": "paragraph"}) == ""
    assert _prosemirror_to_html("not a doc") == ""

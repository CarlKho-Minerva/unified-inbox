"""Route tests. The runner's module-level store points at a throwaway directory
(set in conftest before import), so these never touch the live cache. The
`client` fixture resets that store before each test for isolation."""

from collections.abc import Iterator
from time import monotonic
from types import SimpleNamespace

import pytest
from flask.testing import FlaskClient

from unified_inbox import runner

# Captured before any test swaps them, so the fixture can restore the production
# callables between tests (same injection mechanism the app uses for _chat_complete).
_REAL_CHAT_COMPLETE = runner._chat_complete
_REAL_AGENT_STREAM = runner.agent_answer_stream
_REAL_FETCH_CHAT_THREAD = runner.fetch_chat_thread
_REAL_FETCH_EMAIL_BODY = runner.fetch_email_body
_REAL_BUILD_GRAPH = runner.build_graph
_REAL_DOWNLOAD_MEDIA = runner.download_telegram_media
_REAL_REFRESH_ALL = runner.refresh_all


@pytest.fixture
def client() -> Iterator[FlaskClient]:
    runner.store.set_messages([])
    runner.store.set_events([])
    runner.store.set_luma([])
    runner.store.set_tasks([])
    runner.store.set_graph({"nodes": [], "links": []})
    runner.store.set_meta({"last_refresh": None, "sources": {}})
    runner._state["refreshing"] = False
    runner._graph_state["building"] = False
    runner._graph_state["last_built"] = 0.0
    runner._email_cache.clear()
    runner._detail_cache.clear()
    runner._chat_complete = _REAL_CHAT_COMPLETE
    runner.agent_answer_stream = _REAL_AGENT_STREAM
    runner.build_graph = _REAL_BUILD_GRAPH
    runner.download_telegram_media = _REAL_DOWNLOAD_MEDIA
    runner.refresh_all = _REAL_REFRESH_ALL
    runner._tg_media_cache.clear()
    with runner.app.test_client() as c:
        yield c


def test_health(client: FlaskClient) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.get_json() == {"status": "ok"}


def test_sources_endpoint_returns_the_config(client: FlaskClient) -> None:
    data = client.get("/api/sources").get_json()
    assert set(data) == {"primary", "personal", "secondary", "zoho", "slack", "discord", "github", "telegram"}
    assert data["slack"]["color"].startswith("#")


def test_messages_endpoint_reports_empty_and_refreshing_flag(client: FlaskClient) -> None:
    body = client.get("/api/messages").get_json()
    assert body["messages"] == []
    assert body["meta"]["refreshing"] is False


def test_messages_endpoint_returns_stored_rows(client: FlaskClient) -> None:
    runner.store.set_messages([{"id": "personal:1", "kind": "email", "ts": 5}])
    body = client.get("/api/messages").get_json()
    assert body["messages"][0]["id"] == "personal:1"


def test_status_endpoint_is_meta_only_without_the_message_payload(client: FlaskClient) -> None:
    # The poll target must stay lightweight: meta + refreshing flag, no messages.
    runner.store.set_messages([{"id": "personal:1", "kind": "email", "ts": 5}])
    body = client.get("/api/status").get_json()
    assert body["refreshing"] is False
    assert "last_refresh" in body
    assert "messages" not in body


def test_events_endpoint_returns_stored_events(client: FlaskClient) -> None:
    runner.store.set_events([{"id": "cal1", "start": 10}])
    body = client.get("/api/events").get_json()
    assert body["events"] == [{"id": "cal1", "start": 10}]


def test_luma_endpoint_returns_stored_events(client: FlaskClient) -> None:
    runner.store.set_luma([{"id": "luma:1", "name": "Mixer", "start": 20}])
    body = client.get("/api/luma").get_json()
    assert body["events"] == [{"id": "luma:1", "name": "Mixer", "start": 20}]
    assert "meta" in body


def test_luma_endpoint_empty_by_default(client: FlaskClient) -> None:
    assert client.get("/api/luma").get_json()["events"] == []


def test_tasks_endpoint_returns_stored_tasks(client: FlaskClient) -> None:
    runner.store.set_tasks([{"id": "task:This Week:0", "text": "Ship it", "file": "This Week"}])
    body = client.get("/api/tasks").get_json()
    assert body["tasks"][0]["text"] == "Ship it"
    assert "meta" in body


def test_tasks_endpoint_empty_by_default(client: FlaskClient) -> None:
    assert client.get("/api/tasks").get_json()["tasks"] == []


def test_chat_endpoint_answers_via_the_mocked_model(client: FlaskClient) -> None:
    # Point the module-level completion singleton at a stand-in so the route never
    # touches the live model/proxy (the fixture restores it). The route must
    # forward the cached data into answer() and return its result verbatim.
    captured: dict = {}

    def fake_complete(**kwargs: object) -> SimpleNamespace:
        captured.update(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="grounded reply"))])

    runner.store.set_tasks([{"id": "task:F:0", "text": "Ship the release", "file": "F"}])
    runner._chat_complete = fake_complete
    resp = client.post("/api/chat", json={"question": "what should I do?", "history": []})
    assert resp.status_code == 200
    assert resp.get_json() == {"answer": "grounded reply"}
    # The cached task was grounded into the system prompt the route built.
    assert "Ship the release" in captured["messages"][0]["content"]


def test_chat_endpoint_returns_a_graceful_answer_when_the_model_fails(client: FlaskClient) -> None:
    def boom(**kwargs: object) -> SimpleNamespace:
        raise RuntimeError("proxy exploded")

    runner._chat_complete = boom
    resp = client.post("/api/chat", json={"question": "hello"})
    # A model failure is surfaced as a clean 200 answer, never a 500.
    assert resp.status_code == 200
    assert "answer" in resp.get_json()


def test_chat_endpoint_rejects_a_blank_question(client: FlaskClient) -> None:
    assert client.post("/api/chat", json={"question": "   "}).status_code == 400
    assert client.post("/api/chat", json={"question": ""}).status_code == 400
    assert client.post("/api/chat", json={}).status_code == 400


def test_message_detail_uses_the_email_body_cache(client: FlaskClient) -> None:
    # Pre-seed the immutable-email cache so the route returns without any network.
    runner.store.set_messages([{"id": "personal:1", "kind": "email", "source": "personal", "native_id": "1"}])
    runner._email_cache["personal:1"] = {"body": "<p>cached</p>", "is_html": True, "attachments": []}
    detail = client.get("/api/message/personal:1").get_json()
    assert detail["body"] == "<p>cached</p>"
    assert detail["is_html"] is True


def test_message_detail_404_for_unknown_id(client: FlaskClient) -> None:
    resp = client.get("/api/message/does-not-exist")
    assert resp.status_code == 404


def test_attachment_404_when_message_missing(client: FlaskClient) -> None:
    resp = client.get("/api/attachment/nope?ref=0")
    assert resp.status_code == 404


def test_attachment_404_without_a_ref(client: FlaskClient) -> None:
    runner.store.set_messages([{"id": "personal:1", "kind": "email", "source": "personal", "native_id": "1"}])
    resp = client.get("/api/attachment/personal:1")  # no ?ref
    assert resp.status_code == 404


def test_attachment_404_for_non_email(client: FlaskClient) -> None:
    runner.store.set_messages([{"id": "slack:1", "kind": "chat", "source": "slack", "channel_id": "c"}])
    resp = client.get("/api/attachment/slack:1?ref=0")
    assert resp.status_code == 404


def test_index_serves_the_app_shell(client: FlaskClient) -> None:
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"<!doctype html>" in resp.data.lower()


def test_search_endpoint_empty_query_returns_empty(client: FlaskClient) -> None:
    assert client.get("/api/search?q=").get_json() == {"results": []}


def test_reconstructs_a_deep_email_from_its_id() -> None:
    # A deep-search email is not in the live store; the route reconstructs a
    # minimal message from its id (source:native_id) so its body can still load.
    msg = runner._email_msg_from_id("primary:abc123")
    assert msg == {"id": "primary:abc123", "source": "primary", "native_id": "abc123", "kind": "email"}


def test_reconstruct_rejects_non_email_and_malformed_ids() -> None:
    assert runner._email_msg_from_id("slack:c:ts") is None  # chat source, not email
    assert runner._email_msg_from_id("primary") is None      # no native_id


def test_agent_stream_rejects_a_blank_question(client: FlaskClient) -> None:
    # The streaming route must reject an empty ask before spawning any agent.
    assert client.post("/api/agent-stream", json={"question": "   "}).status_code == 400
    assert client.post("/api/agent-stream", json={}).status_code == 400


def test_agent_stream_route_streams_tokens(client: FlaskClient) -> None:
    # With the agent stubbed (no live claude), the route streams the tokens
    # verbatim as a text/plain body.
    runner.agent_answer_stream = lambda *a, **k: iter(["Hel", "lo ", "the user"])
    resp = client.post("/api/agent-stream", json={"question": "hi"})
    assert resp.status_code == 200
    assert resp.mimetype == "text/plain"
    assert resp.get_data(as_text=True) == "Hello the user"


def test_agent_stream_route_appends_a_clean_message_on_failure(client: FlaskClient) -> None:
    # A crash mid-stream must not 500 or leak a traceback: the partial text is
    # delivered and a clean trailing apology is appended.
    def dying(*a, **k):
        yield "partial answer"
        raise RuntimeError("boom")

    runner.agent_answer_stream = dying
    resp = client.post("/api/agent-stream", json={"question": "hi"})
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert body.startswith("partial answer")
    assert "hit an error" in body
    assert "Traceback" not in body and "RuntimeError" not in body


# --- _cached_detail: TTL expiry re-fetches, fresh hits don't, LRU bounds size ---


def test_cached_detail_serves_a_fresh_hit_without_rebuilding(client: FlaskClient) -> None:
    calls = {"n": 0}

    def build() -> dict:
        calls["n"] += 1
        return {"v": calls["n"]}

    first = runner._cached_detail("k", build)
    second = runner._cached_detail("k", build)  # within TTL -> cached
    assert first == second == {"v": 1}
    assert calls["n"] == 1


def test_cached_detail_rebuilds_after_the_ttl_expires(client: FlaskClient) -> None:
    calls = {"n": 0}

    def build() -> dict:
        calls["n"] += 1
        return {"v": calls["n"]}

    runner._cached_detail("k", build)  # v1
    # Force expiry by backdating the stored timestamp beyond the TTL.
    _, value = runner._detail_cache["k"]
    runner._detail_cache["k"] = (monotonic() - runner._DETAIL_TTL - 1, value)
    again = runner._cached_detail("k", build)
    assert again == {"v": 2}  # rebuilt
    assert calls["n"] == 2


def test_cached_detail_evicts_oldest_past_the_cap(client: FlaskClient) -> None:
    for i in range(runner._DETAIL_CACHE_MAX + 10):
        runner._cached_detail(f"k{i}", lambda i=i: {"v": i})
    assert len(runner._detail_cache) == runner._DETAIL_CACHE_MAX
    assert "k0" not in runner._detail_cache  # the very first entry was evicted
    assert f"k{runner._DETAIL_CACHE_MAX + 9}" in runner._detail_cache  # newest kept


# --- _warm_details: empty store, Discord skip, unknown kind ---


def test_warm_details_no_op_on_empty_store(client: FlaskClient) -> None:
    runner.store.set_messages([])
    runner._warm_details()  # must not raise
    assert len(runner._detail_cache) == 0


def test_warm_details_skips_discord(client: FlaskClient) -> None:
    calls = []
    runner.fetch_chat_thread = lambda source, cid: calls.append(source) or []
    try:
        runner.store.set_messages([
            {"id": "discord:1", "source": "discord", "kind": "chat", "channel_id": "c1"},
            {"id": "slack:1", "source": "slack", "kind": "chat", "channel_id": "c2"},
        ])
        runner._warm_details()
    finally:
        runner.fetch_chat_thread = _REAL_FETCH_CHAT_THREAD
    assert calls == ["slack"]  # Discord never warmed; Slack was
    assert "discord:1" not in runner._detail_cache


def test_warm_details_survives_an_unknown_kind(client: FlaskClient) -> None:
    # An unrecognized kind falls through to the chat path; the warmer's best-effort
    # guard must keep the refresh alive even if that fetch blows up.
    def boom(source, cid):
        raise RuntimeError("no such thread")

    runner.fetch_chat_thread = boom
    try:
        runner.store.set_messages([{"id": "weird:1", "source": "weird", "kind": "mystery", "channel_id": "c"}])
        runner._warm_details()  # must not raise
    finally:
        runner.fetch_chat_thread = _REAL_FETCH_CHAT_THREAD
    assert "weird:1" not in runner._detail_cache


# --- api_message: deep-email fetch failure, chat id not in store, malformed id ---


def test_message_route_returns_clean_error_when_body_fetch_fails(client: FlaskClient) -> None:
    # A deep-search email (reconstructed from its id) whose body fetch dies must
    # yield a clean error, never a 500 with a traceback.
    def boom(source, native_id):
        raise RuntimeError("imap down")

    runner.fetch_email_body = boom
    try:
        resp = client.get("/api/message/primary:deep-xyz")
    finally:
        runner.fetch_email_body = _REAL_FETCH_EMAIL_BODY
    assert resp.status_code == 502
    assert resp.get_json() == {"error": "Couldn't load this message."}


def test_message_route_404_for_a_chat_id_not_in_the_store(client: FlaskClient) -> None:
    # A chat id (not an email) that isn't cached can't be reconstructed -> 404.
    resp = client.get("/api/message/slack:C1:1.0")
    assert resp.status_code == 404


def test_message_route_404_for_a_malformed_id_without_a_colon(client: FlaskClient) -> None:
    resp = client.get("/api/message/primary")  # no native_id
    assert resp.status_code == 404


def test_graph_endpoint_returns_cached_graph(client: FlaskClient) -> None:
    runner.store.set_graph({"nodes": [{"id": "A", "label": "A", "val": 1}], "links": []})
    body = client.get("/api/graph").get_json()
    assert body["nodes"][0]["id"] == "A"
    assert body["building"] is False


def test_graph_endpoint_reports_empty_before_build(client: FlaskClient) -> None:
    runner.store.set_graph({"nodes": [], "links": []})
    runner._graph_state["building"] = True  # pretend a build is already running
    body = client.get("/api/graph").get_json()
    assert body["nodes"] == []
    assert body["building"] is True
    runner._graph_state["building"] = False


def test_build_graph_async_caches_the_result_and_clears_the_building_flag(client: FlaskClient) -> None:
    built = {"nodes": [{"id": "N", "label": "N", "val": 0}], "links": []}
    runner.build_graph = lambda: built
    runner._build_graph_async()
    assert runner.store.get_graph() == built
    assert runner._graph_state["building"] is False  # cleared in the finally


def test_build_graph_async_is_a_noop_when_a_build_is_already_running(client: FlaskClient) -> None:
    calls = {"n": 0}

    def fake() -> dict:
        calls["n"] += 1
        return {"nodes": [], "links": []}

    runner.build_graph = fake
    runner._graph_state["building"] = True  # a build is already in flight
    runner._build_graph_async()
    assert calls["n"] == 0  # guard prevented a second concurrent crawl
    runner._graph_state["building"] = False


def test_graph_rebuild_route_kicks_off_a_background_build(client: FlaskClient) -> None:
    built = {"nodes": [{"id": "R", "label": "R", "val": 0}], "links": []}
    runner.build_graph = lambda: built
    resp = client.post("/api/graph/rebuild")
    assert resp.status_code == 200
    assert resp.get_json() == {"started": True}
    # The build runs on a daemon thread; wait (bounded) for the cache to land so
    # the assertion doesn't race the thread's write.
    deadline = monotonic() + 3.0
    while runner.store.get_graph() != built and monotonic() < deadline:
        pass
    assert runner.store.get_graph() == built


def test_graph_endpoint_kicks_off_a_build_when_the_cache_is_empty(client: FlaskClient) -> None:
    built = {"nodes": [{"id": "K", "label": "K", "val": 0}], "links": []}
    runner.build_graph = lambda: built
    # Empty cache and no build running -> the GET starts a background build and
    # returns the (still empty) cache with building surfaced.
    body = client.get("/api/graph").get_json()
    assert body["nodes"] == []
    deadline = monotonic() + 3.0
    while runner.store.get_graph() != built and monotonic() < deadline:
        pass
    assert runner.store.get_graph() == built


def test_build_graph_async_swallows_a_failing_crawl(client: FlaskClient) -> None:
    def boom() -> dict:
        raise RuntimeError("vault unreachable")

    runner.build_graph = boom
    runner._build_graph_async()  # best-effort: a failed crawl must not raise
    assert runner._graph_state["building"] is False  # flag still cleared
    assert runner.store.get_graph() == {"nodes": [], "links": []}  # cache untouched


def test_build_graph_async_stamps_the_last_built_time(client: FlaskClient) -> None:
    runner.build_graph = lambda: {"nodes": [], "links": []}
    runner._graph_state["last_built"] = 0.0
    runner._build_graph_async()
    assert runner._graph_state["last_built"] > 0.0  # stamped even for an empty vault


def test_maybe_rebuild_graph_rebuilds_once_the_interval_has_elapsed(client: FlaskClient) -> None:
    built = {"nodes": [{"id": "T", "label": "T", "val": 0}], "links": []}
    runner.build_graph = lambda: built
    # Pretend the last build was long enough ago that the interval has elapsed.
    runner._graph_state["last_built"] = monotonic() - runner.GRAPH_REBUILD_INTERVAL - 1
    runner._maybe_rebuild_graph()
    deadline = monotonic() + 3.0
    while runner.store.get_graph() != built and monotonic() < deadline:
        pass
    assert runner.store.get_graph() == built


def test_maybe_rebuild_graph_skips_when_recently_built(client: FlaskClient) -> None:
    calls = {"n": 0}

    def fake() -> dict:
        calls["n"] += 1
        return {"nodes": [], "links": []}

    runner.build_graph = fake
    runner._graph_state["last_built"] = monotonic()  # just built -> not yet due
    runner._maybe_rebuild_graph()
    assert calls["n"] == 0


def test_maybe_rebuild_graph_skips_while_a_build_is_in_flight(client: FlaskClient) -> None:
    calls = {"n": 0}

    def fake() -> dict:
        calls["n"] += 1
        return {"nodes": [], "links": []}

    runner.build_graph = fake
    runner._graph_state["building"] = True  # a crawl is already running
    runner._graph_state["last_built"] = 0.0  # interval elapsed, but guarded
    runner._maybe_rebuild_graph()
    assert calls["n"] == 0
    runner._graph_state["building"] = False


def test_telegram_media_route_serves_bytes_and_caches(client: FlaskClient) -> None:
    calls = {"n": 0}

    def fake_download(chat_id: str, msg_id: str) -> bytes:
        calls["n"] += 1
        return b"\xff\xd8jpegbytes"

    runner.download_telegram_media = fake_download
    resp = client.get("/api/telegram-media/42/7")
    assert resp.status_code == 200
    assert resp.mimetype == "image/jpeg"
    assert resp.data == b"\xff\xd8jpegbytes"
    # A second request for the same media is served from cache, not re-downloaded.
    again = client.get("/api/telegram-media/42/7")
    assert again.status_code == 200 and again.data == b"\xff\xd8jpegbytes"
    assert calls["n"] == 1


def test_telegram_media_route_404s_when_there_is_nothing_to_serve(client: FlaskClient) -> None:
    def fake_download(chat_id: str, msg_id: str) -> bytes:
        return b""

    runner.download_telegram_media = fake_download
    resp = client.get("/api/telegram-media/1/2")
    assert resp.status_code == 404
    assert resp.get_json() == {"error": "no media"}


def test_telegram_media_cache_is_bounded(client: FlaskClient) -> None:
    def fake_download(chat_id: str, msg_id: str) -> bytes:
        return b"x"

    runner.download_telegram_media = fake_download
    for i in range(runner._TG_MEDIA_MAX + 10):
        assert client.get(f"/api/telegram-media/0/{i}").status_code == 200
    assert len(runner._tg_media_cache) == runner._TG_MEDIA_MAX


# --- two-cadence refresh: the fast tick must never pull rate-sensitive chat ---


def test_fast_sources_are_only_email_and_github() -> None:
    # Discord's user-token reads trip abuse detection, so no chat source may ever
    # ride the fast tick. Only cheap email + GitHub polls are allowed.
    assert {"primary", "personal", "secondary", "zoho", "github"} == set(runner.FAST_SOURCES)
    assert not ({"slack", "discord", "telegram"} & runner.FAST_SOURCES)


def test_do_refresh_maps_full_and_fast_ticks_to_the_right_refresh_args(client: FlaskClient) -> None:
    captured: list[dict] = []

    def fake_refresh(store: object, due_sources: object = None, refresh_aux: bool = True) -> dict:
        captured.append({"due_sources": due_sources, "refresh_aux": refresh_aux})
        return {}

    runner.refresh_all = fake_refresh
    runner._do_refresh(full=True)
    runner._do_refresh(full=False)

    # A full cycle hits every source (due_sources=None) and all aux data.
    assert captured[0] == {"due_sources": None, "refresh_aux": True}
    # A fast tick restricts to the fast sources and skips the aux fetches.
    assert captured[1] == {"due_sources": set(runner.FAST_SOURCES), "refresh_aux": False}

"""Unified inbox across email, Slack, and Discord.

A synchronous Flask app served by the threaded Werkzeug server, plus a
background refresher thread (the "daemon") that pulls all six sources into a
local JSON cache every few minutes so the UI loads instantly.

State lives under DATA_DIR (default runtime/unified-inbox/, overridable via
UNIFIED_INBOX_DATA_DIR). The listen port is PORT (default assigned, overridable
via UNIFIED_INBOX_PORT). See the update-service skill for why both are env-driven.
"""

import os
import signal
import threading
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path
from time import monotonic as _monotonic
from types import FrameType

from flask import Flask, Response, jsonify, request
from litellm import completion
from werkzeug.serving import make_server

from unified_inbox.chat import agent_answer, agent_answer_stream, answer
from unified_inbox.fetchers import (
    fetch_chat_thread,
    fetch_email_attachment,
    fetch_email_body,
    fetch_github_detail,
    refresh_all,
)
from unified_inbox.luma import fetch_luma_event
from unified_inbox.obsidian_graph import build_graph
from unified_inbox.search import search_all
from unified_inbox.sources import SOURCES
from unified_inbox.store import Store
from unified_inbox.telegram import download_telegram_media

DATA_DIR = Path(os.environ.get("UNIFIED_INBOX_DATA_DIR", "runtime/unified-inbox"))
PORT = int(os.environ.get("UNIFIED_INBOX_PORT", "8080"))
# Two cadences. The FAST tick polls only cheap, latency-sensitive sources (email
# + GitHub) so a new email shows up within a minute. The FULL cycle refreshes
# everything -- including the rate-sensitive chat sources (Discord's user-token
# reads trip abuse detection, so they must NOT be polled aggressively) plus the
# calendars, Luma, and vault tasks -- on a slower cadence.
REFRESH_INTERVAL = int(os.environ.get("UNIFIED_INBOX_REFRESH_SECONDS", "60"))
FULL_REFRESH_INTERVAL = int(os.environ.get("UNIFIED_INBOX_FULL_REFRESH_SECONDS", "180"))
# Sources safe to poll on the fast tick: email (Gmail API + IMAP) and GitHub.
# Chat sources (Slack/Discord/Telegram) refresh only on the full cycle.
FAST_SOURCES = frozenset(k for k, c in SOURCES.items() if c.get("kind") in ("email", "github"))
# The vault graph crawl is slow (~60s over WebDAV), so it rebuilds on a much
# coarser cadence than the message refresh -- every 30 minutes by default.
GRAPH_REBUILD_INTERVAL = int(os.environ.get("UNIFIED_INBOX_GRAPH_SECONDS", "1800"))

ASSETS = Path(__file__).parent / "assets"
app = Flask("unified_inbox", static_folder=None)
store = Store(DATA_DIR)

_refresh_lock = threading.Lock()
_state = {"refreshing": False}
# Set to break the refresher loop; also lets the interval wait return early on
# shutdown instead of blocking a full interval.
_stop_refresher = threading.Event()

# Email bodies are immutable once sent, so cache them forever (in memory) to make
# re-opening (and client prefetch) instant. Bounded LRU to keep memory in check.
_email_cache: OrderedDict[str, dict] = OrderedDict()
_EMAIL_CACHE_MAX = 400

# Chat threads and GitHub items DO change, but a short-TTL cache still makes
# re-opening the same conversation feel instant without hammering the third-party
# APIs (important for Discord, whose user-token reads trip abuse detection).
_detail_cache: OrderedDict[str, tuple[float, dict]] = OrderedDict()
_DETAIL_CACHE_MAX = 200
_DETAIL_TTL = 240.0  # seconds -- longer than the refresh interval so warmed
# threads/GitHub items stay hot between background warms.

# How many top messages to pre-warm after each refresh so first-open is instant.
# Discord is deliberately EXCLUDED: its user-token reads trip abuse detection,
# and it already recovered from one invalidation -- never pre-warm it.
_WARM_COUNT = 15
_WARM_SKIP_SOURCES = {"discord"}


def _cached_detail(mid: str, build: "Callable[[], dict]") -> dict:
    now = _monotonic()
    hit = _detail_cache.get(mid)
    if hit is not None and now - hit[0] < _DETAIL_TTL:
        _detail_cache.move_to_end(mid)
        return hit[1]
    value = build()
    _detail_cache[mid] = (now, value)
    _detail_cache.move_to_end(mid)
    while len(_detail_cache) > _DETAIL_CACHE_MAX:
        _detail_cache.popitem(last=False)
    return value

# The completion callable the Ask route uses. A module-level singleton (mirroring
# store / _state / _email_cache) so route tests can point it at a fake and reset
# it in the fixture, without reaching the live model/proxy.
_chat_complete = completion


def _warm_details() -> None:
    """Pre-fetch the detail for the top messages (except Discord) so the first
    open is instant. Best-effort: any single failure is skipped."""
    warmed = 0
    for msg in store.get_messages():
        if warmed >= _WARM_COUNT:
            break
        if msg.get("source") in _WARM_SKIP_SOURCES:
            continue
        try:
            _detail_for(msg)
            warmed += 1
        except Exception:  # noqa: BLE001 - warming is best-effort; never sink the refresh
            continue


def _do_refresh(full: bool = True) -> None:
    if not _refresh_lock.acquire(blocking=False):
        return
    _state["refreshing"] = True
    try:
        # Full cycle hits every source + aux; a fast tick only the FAST_SOURCES.
        refresh_all(store, due_sources=None if full else set(FAST_SOURCES), refresh_aux=full)
        _warm_details()
    finally:
        _state["refreshing"] = False
        _refresh_lock.release()


def _refresher_loop() -> None:
    # Initial fill if the cache is empty, then refresh on an interval. Event.wait
    # returns True only when the stop event is set, so the loop exits promptly on
    # shutdown; otherwise it times out after REFRESH_INTERVAL and ticks again.
    if not store.get_messages():
        _do_refresh(full=True)
    else:
        _warm_details()  # cache exists from disk on restart; warm details now
    # Build the vault graph once now if the cache is missing/empty; otherwise
    # start the interval clock so the first rebuild is a full interval away.
    if not store.get_graph().get("nodes"):
        _build_graph_in_thread()
    else:
        _graph_state["last_built"] = _monotonic()
    last_full = _monotonic()
    # Wake every fast tick; promote to a full cycle once the full interval has
    # elapsed. So email/GitHub refresh every REFRESH_INTERVAL while chat + aux
    # (and Discord in particular) stay on the slower FULL_REFRESH_INTERVAL.
    while not _stop_refresher.wait(REFRESH_INTERVAL):
        now = _monotonic()
        full = (now - last_full) >= FULL_REFRESH_INTERVAL
        _do_refresh(full=full)
        if full:
            last_full = now
            _maybe_rebuild_graph()  # gated to a far coarser cadence than the refresh


@app.route("/")
def index() -> Response:
    return Response((ASSETS / "app.html").read_text(), mimetype="text/html")


@app.route("/health")
def health() -> Response:
    return Response('{"status": "ok"}', mimetype="application/json")


@app.route("/api/sources")
def api_sources() -> Response:
    return jsonify(SOURCES)


@app.route("/api/status")
def api_status() -> Response:
    # Lightweight poll target: just the refresh metadata (a few hundred bytes),
    # so the client can cheaply detect a change without re-downloading the full
    # message list every few seconds.
    meta = store.get_meta()
    meta["refreshing"] = _state["refreshing"]
    return jsonify(meta)


@app.route("/api/messages")
def api_messages() -> Response:
    meta = store.get_meta()
    meta["refreshing"] = _state["refreshing"]
    return jsonify({"messages": store.get_messages(), "meta": meta})


@app.route("/api/events")
def api_events() -> Response:
    return jsonify({"events": store.get_events(), "meta": store.get_meta()})


@app.route("/api/luma")
def api_luma() -> Response:
    return jsonify({"events": store.get_luma(), "meta": store.get_meta()})


@app.route("/api/tasks")
def api_tasks() -> Response:
    return jsonify({"tasks": store.get_tasks(), "meta": store.get_meta()})


# "building" single-flights the crawl; "last_built" is the monotonic timestamp
# of the last completed attempt, used to space the background rebuild cadence.
_graph_state = {"building": False, "last_built": 0.0}


def _build_graph_async() -> None:
    if _graph_state["building"]:
        return
    _graph_state["building"] = True
    try:
        store.set_graph(build_graph())
    except Exception:  # noqa: BLE001 - graph build is best-effort
        pass
    finally:
        # Stamp regardless of outcome so a failed crawl waits out the interval
        # instead of hammering WebDAV; the empty-cache paths still retry sooner.
        _graph_state["last_built"] = _monotonic()
        _graph_state["building"] = False


def _build_graph_in_thread() -> None:
    """Kick off a graph build on a daemon thread so the ~60s crawl never blocks
    the caller (a request handler or the message-refresh loop)."""
    threading.Thread(target=_build_graph_async, daemon=True).start()


def _maybe_rebuild_graph() -> None:
    """Rebuild the graph if the coarse interval has elapsed and no build is in
    flight. Runs off the message-refresh loop but is gated independently so it
    never overlaps a build or slows the message refresh."""
    if _graph_state["building"]:
        return
    if _monotonic() - _graph_state["last_built"] >= GRAPH_REBUILD_INTERVAL:
        _build_graph_in_thread()


@app.route("/api/graph")
def api_graph() -> Response:
    graph = store.get_graph()
    # Reading the whole vault is slow, so build it in the background and serve
    # the cache. Kick off a build if we have nothing yet.
    if not graph.get("nodes") and not _graph_state["building"]:
        _build_graph_in_thread()
    return jsonify({**graph, "building": _graph_state["building"]})


@app.route("/api/graph/rebuild", methods=["POST"])
def api_graph_rebuild() -> Response:
    _build_graph_in_thread()
    return jsonify({"started": True})


@app.route("/api/luma-event/<path:eid>")
def api_luma_event(eid: str) -> Response:
    return jsonify(fetch_luma_event(eid))


def _detail_for(msg: dict) -> dict:
    """Build (and cache) the detail payload for a message. Email bodies are
    cached forever; chat threads + GitHub items use the short-TTL cache. Shared
    by the /api/message route and the background warmer."""
    mid = msg["id"]
    detail = dict(msg)
    if msg["kind"] == "email":
        cached = _email_cache.get(mid)
        if cached is None:
            cached = fetch_email_body(msg["source"], msg["native_id"])
            _email_cache[mid] = cached
            _email_cache.move_to_end(mid)
            while len(_email_cache) > _EMAIL_CACHE_MAX:
                _email_cache.popitem(last=False)
        detail.update(cached)
    elif msg["kind"] == "github":
        detail.update(_cached_detail(mid, lambda: fetch_github_detail(msg["native_id"])))
    else:
        detail.update(_cached_detail(mid, lambda: {"thread": fetch_chat_thread(msg["source"], msg["channel_id"])}))
    return detail


def _email_msg_from_id(mid: str) -> dict | None:
    """Reconstruct a minimal email message from its id (``source:native_id``) so
    a deep-search result -- not in the live cache -- can still open its body."""
    source, _, native_id = mid.partition(":")
    if not native_id or SOURCES.get(source, {}).get("kind") != "email":
        return None
    return {"id": mid, "source": source, "native_id": native_id, "kind": "email"}


@app.route("/api/message/<path:mid>")
def api_message(mid: str) -> Response | tuple[Response, int]:
    msg = next((m for m in store.get_messages() if m["id"] == mid), None)
    if msg is None:
        msg = _email_msg_from_id(mid)  # deep-search email not in the live cache
    if msg is None:
        return jsonify({"error": "not found"}), 404
    try:
        return jsonify(_detail_for(msg))
    except Exception:  # noqa: BLE001 - a provider/body-fetch failure (esp. a deep
        # email whose IMAP fetch dies) becomes a clean error, never a 500. The
        # client treats an {error} payload as "couldn't load" and offers the source.
        return jsonify({"error": "Couldn't load this message."}), 502


@app.route("/api/search")
def api_search() -> Response:
    query = (request.args.get("q") or "").strip()
    if not query:
        return jsonify({"results": []})
    return jsonify({"results": search_all(query)})


@app.route("/api/attachment/<path:mid>")
def api_attachment(mid: str) -> Response | tuple[Response, int]:
    # Streams an email attachment's bytes (used for inline images + downloads).
    ref = request.args.get("ref", "")
    mime = request.args.get("mime", "application/octet-stream")
    msg = next((m for m in store.get_messages() if m["id"] == mid), None)
    if msg is None or msg["kind"] != "email" or not ref:
        return jsonify({"error": "not found"}), 404
    data = fetch_email_attachment(msg["source"], msg["native_id"], ref)
    if not data:
        return jsonify({"error": "empty"}), 404
    headers = {}
    if request.args.get("dl"):
        name = request.args.get("name", "attachment")
        headers["Content-Disposition"] = f'attachment; filename="{name}"'
    return Response(data, mimetype=mime, headers=headers)


@app.route("/api/chat", methods=["POST"])
def api_chat() -> Response:
    data = request.get_json(silent=True) or {}
    question = (data.get("question") or "").strip()
    if not question:
        return jsonify({"error": "empty question"}), 400
    history = data.get("history", [])
    try:
        result = answer(
            question,
            history,
            store.get_messages(),
            store.get_events(),
            store.get_tasks(),
            store.get_luma(),
            complete=_chat_complete,
        )
        return jsonify(result)
    except Exception as exc:  # noqa: BLE001 - surface a clean error to the UI
        return jsonify({"answer": f"Sorry, I couldn't answer that ({type(exc).__name__}). Try again."}), 200


@app.route("/api/agent", methods=["POST"])
def api_agent() -> Response:
    # The floating chatbot: a real agent that can read the user's accounts live.
    data = request.get_json(silent=True) or {}
    question = (data.get("question") or "").strip()
    if not question:
        return jsonify({"error": "empty question"}), 400
    result = agent_answer(
        question,
        data.get("history", []),
        store.get_messages(),
        store.get_events(),
        store.get_tasks(),
        store.get_luma(),
    )
    return jsonify(result)


@app.route("/api/agent-stream", methods=["POST"])
def api_agent_stream() -> Response | tuple[Response, int]:
    # Streaming variant of /api/agent: the reply is sent token-by-token so the
    # chat panel types it out live instead of waiting ~15s for the whole answer.
    data = request.get_json(silent=True) or {}
    question = (data.get("question") or "").strip()
    if not question:
        return jsonify({"error": "empty question"}), 400
    history = data.get("history", [])
    # Snapshot the cache OUTSIDE the generator so it doesn't touch the request.
    snapshot = (store.get_messages(), store.get_events(), store.get_tasks(), store.get_luma())

    def generate():
        try:
            yield from agent_answer_stream(question, history, *snapshot)
        except Exception:  # noqa: BLE001 - never leak a stack trace into the stream
            yield "\n\n(Sorry, the assistant hit an error.)"

    return Response(generate(), mimetype="text/plain; charset=utf-8")


_tg_media_cache: OrderedDict[str, bytes] = OrderedDict()
_TG_MEDIA_MAX = 60


@app.route("/api/telegram-media/<chat_id>/<msg_id>")
def api_telegram_media(chat_id: str, msg_id: str) -> Response | tuple[Response, int]:
    # Serve a Telegram photo's bytes for inline display (cached; downloads are slow).
    key = f"{chat_id}/{msg_id}"
    data = _tg_media_cache.get(key)
    if data is None:
        data = download_telegram_media(chat_id, msg_id)
        if data:
            _tg_media_cache[key] = data
            _tg_media_cache.move_to_end(key)
            while len(_tg_media_cache) > _TG_MEDIA_MAX:
                _tg_media_cache.popitem(last=False)
    if not data:
        return jsonify({"error": "no media"}), 404
    return Response(data, mimetype="image/jpeg")


@app.route("/api/refresh", methods=["POST"])
def api_refresh() -> Response:
    threading.Thread(target=_do_refresh, daemon=True).start()
    return jsonify({"started": True})


def main() -> None:
    threading.Thread(target=_refresher_loop, daemon=True).start()
    server = make_server("127.0.0.1", PORT, app, threaded=True)

    def _shutdown(_signum: int, _frame: FrameType | None) -> None:
        # supervisord stops the service with SIGTERM; wake the refresher's
        # interval wait and stop accepting requests so the process exits cleanly.
        # server.shutdown() blocks until serve_forever() returns, so it must run
        # off the main thread (which is inside serve_forever) to avoid deadlock.
        _stop_refresher.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    server.serve_forever()


if __name__ == "__main__":
    main()

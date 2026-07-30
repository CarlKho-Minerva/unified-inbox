from pathlib import Path

from imbue.imbue_common.ratchet_testing import standard_ratchet_checks as rc
from inline_snapshot import snapshot

_DIR = Path(__file__).parent


# --- Code safety ---


def test_prevent_todos() -> None:
    rc.check_todos(_DIR, snapshot(0))


def test_prevent_exec_usage() -> None:
    rc.check_exec(_DIR, snapshot(0))


def test_prevent_eval_usage() -> None:
    rc.check_eval(_DIR, snapshot(0))


def test_prevent_while_true() -> None:
    rc.check_while_true(_DIR, snapshot(0))


def test_prevent_time_sleep() -> None:
    # One justified use: fetch_discord paces its per-channel reads by a fixed
    # delay so user-token reads stay under Discord's abuse-detection threshold.
    # There is no external condition to poll -- the wait is the rate limit
    # itself -- so this is a genuine sleep, not a poll-in-disguise.
    rc.check_time_sleep(_DIR, snapshot(1))


def test_prevent_global_keyword() -> None:
    rc.check_global_keyword(_DIR, snapshot(0))


def test_prevent_bare_print() -> None:
    rc.check_bare_print(_DIR, snapshot(0))


# --- Exception handling ---


def test_prevent_bare_except() -> None:
    rc.check_bare_except(_DIR, snapshot(0))


def test_prevent_broad_exception_catch() -> None:
    # Five justified catches, all graceful-degradation boundaries. Four isolate a
    # single data source during the concurrent refresh: _fetch_source (one message
    # source), fetch_all_events (one calendar), fetch_luma (the Luma SF feed), and
    # fetch_tasks (the Obsidian vault). A refresh spans many independent third-party
    # APIs; one failing for any reason (timeout, malformed payload, auth) must be
    # recorded and skipped without sinking the others, and the set of failure types
    # is deliberately open. The fifth is the /api/chat route, which turns any Ask
    # panel failure (proxy error, litellm error, network) into a clean message to
    # the UI instead of a 500. The sixth is agent_answer, which likewise turns any
    # failure of the spawned `claude` agent (crash, bad output, env issue) into a
    # clean chat message instead of a 500. The seventh is the detail warmer, whose
    # per-message pre-fetch is best-effort: one message failing to warm must never
    # sink the background refresh. Eight through ten are deep search's per-source
    # isolation boundaries (Gmail-API, IMAP, Slack): one provider failing during a
    # full-history search must not sink the rest. The eleventh is the streaming
    # agent route's generator, which must never leak a stack trace into the
    # response stream -- any failure becomes a clean trailing message. The twelfth
    # is the /api/message route, which turns a body/detail-fetch failure (notably a
    # deep-search email whose IMAP fetch dies) into a clean error instead of a 500.
    # The thirteenth is the background vault-graph builder, which is best-effort:
    # a failed crawl must not crash the daemon thread.
    rc.check_broad_exception_catch(_DIR, snapshot(13))


def test_prevent_builtin_exception_raises() -> None:
    rc.check_builtin_exception_raises(_DIR, snapshot(0))


# --- Import style ---


def test_prevent_inline_imports() -> None:
    rc.check_inline_imports(_DIR, snapshot(0))


def test_prevent_relative_imports() -> None:
    rc.check_relative_imports(_DIR, snapshot(0))


# --- Banned libraries and patterns ---


def test_prevent_asyncio_import() -> None:
    # One justified asyncio import: telegram.py bridges Telethon, which is
    # async-only, into this sync service. Each read runs in its own loop via
    # asyncio.run (isolated per call, serialized by a lock, read-only) -- the
    # correct pattern for an async-only dependency, not stray async in sync code.
    rc.check_asyncio_import(_DIR, snapshot(1))


def test_prevent_dataclasses_import() -> None:
    rc.check_dataclasses_import(_DIR, snapshot(0))


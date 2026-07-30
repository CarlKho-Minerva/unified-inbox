"""The "Ask" panel: a grounded assistant that answers questions about the user's
inbox (email, chat, GitHub, calendar, tasks, Luma).

TWO BACKENDS, tried in this order (see ``agent_answer`` / ``agent_answer_stream``):

  1. The ``claude`` CLI (RECOMMENDED, and the default when it is on PATH). Runs a
     real agent that can use ``latchkey`` to read the user's connected accounts
     LIVE, not just the cached snapshot. Slower to start (~15s) but far more
     capable. No API key needed -- it uses the CLI's own auth.

  2. An ``ANTHROPIC_API_KEY`` fast fallback (used only when the ``claude`` CLI is
     NOT available). A one-shot litellm completion grounded on the cached inbox
     data. Quick, but WARNING: it is NOT agentic -- it can only see the data
     already cached in this inbox and cannot read anything live.

If NEITHER is configured the assistant returns clear setup instructions instead
of failing silently (see ``_SETUP_HELP``).

It is intentionally read-only: it answers and summarizes, it does not send mail
or take actions.
"""

import datetime
import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path

from litellm import completion

MODEL = "claude-haiku-4-5"  # fast + cheap for grounded Q&A; swap to claude-sonnet-5 for deeper reasoning

# The keyed fallback routes through litellm. Pass api_base explicitly (stripped
# of a trailing slash) -- relying on the env var directly yields a double-slashed
# path that some proxies 404. NOTE: this deployment ships WITHOUT an
# ANTHROPIC_API_KEY, so the keyed path is dormant here and the CLI path is used;
# the fallback exists for portability (e.g. an adopter who has a key but not the
# `claude` CLI). See the module docstring.
_API_BASE = (os.environ.get("ANTHROPIC_BASE_URL") or "").rstrip("/") or None
_API_KEY = os.environ.get("ANTHROPIC_API_KEY") or None

# Shown when neither backend is configured -- clear, actionable setup steps.
_SETUP_HELP = (
    "The Ask assistant isn't configured yet. It needs ONE of these:\n\n"
    "1. The `claude` CLI on your PATH (recommended) — lets the assistant read your "
    "connected accounts live. Install it from https://docs.claude.com/claude-code, "
    "then restart this service.\n\n"
    "2. An `ANTHROPIC_API_KEY` environment variable (faster to start, but it can "
    "ONLY see the data already cached in this inbox — no live account access). "
    "Get a key at https://console.anthropic.com and set it in the service's "
    "environment, then restart.\n\n"
    "Until one of these is set, the assistant can't answer."
)


def _claude_cli_available() -> bool:
    """True when the `claude` CLI is on PATH (the primary, agentic backend)."""
    return shutil.which("claude") is not None

# Caps keep the injected context bounded regardless of cache size.
MAX_MESSAGES = 60
MAX_EVENTS = 25
MAX_TASKS = 80
MAX_LUMA = 20

SYSTEM = (
    "You are the user's personal assistant, embedded in their unified inbox. "
    "Answer using ONLY the data provided below (their email accounts, Slack/Discord, "
    "GitHub, calendar, Obsidian tasks, and Luma SF events). Be concise, specific, "
    "and skimmable. When useful, name the source (which account, calendar, or file). "
    "If the answer is not in the provided data, say so plainly rather than guessing. "
    "It is now {now}."
)


def _fmt_ts(ts: float) -> str:
    if not ts:
        return "?"
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%d %H:%MZ")


def build_context(messages: list[dict], events: list[dict], tasks: list[dict], luma: list[dict]) -> str:
    parts = []

    msgs = messages[:MAX_MESSAGES]
    if msgs:
        lines = [
            f"- [{m.get('source')}] {'UNREAD ' if m.get('unread') else ''}{m.get('who','')} — "
            f"{m.get('subject','')} :: {m.get('snippet','')[:140]} ({_fmt_ts(m.get('ts', 0))})"
            for m in msgs
        ]
        parts.append("## Recent messages (email / chat / github)\n" + "\n".join(lines))

    evs = events[:MAX_EVENTS]
    if evs:
        lines = [
            f"- {_fmt_ts(e.get('start', 0))} {e.get('title','')} [{e.get('calendar','')}]"
            f"{' @ ' + e['location'] if e.get('location') else ''}"
            for e in evs
        ]
        parts.append("## Upcoming calendar events\n" + "\n".join(lines))

    tks = tasks[:MAX_TASKS]
    if tks:
        lines = [f"- [{t.get('file','')}] {t.get('text','')}" for t in tks]
        parts.append("## Open tasks (Obsidian vault)\n" + "\n".join(lines))

    lm = luma[:MAX_LUMA]
    if lm:
        lines = [f"- {_fmt_ts(e.get('start', 0))} {e.get('name','')} @ {e.get('location','')}" for e in lm]
        parts.append("## Upcoming Luma SF events\n" + "\n".join(lines))

    return "\n\n".join(parts) if parts else "(No data is currently cached.)"


def answer(
    question: str,
    history: list[dict],
    messages: list[dict],
    events: list[dict],
    tasks: list[dict],
    luma: list[dict],
    complete: Callable = completion,
    api_base: str | None = _API_BASE,
    api_key: str | None = _API_KEY,
) -> dict:
    """Answer a question grounded in the cached inbox data. `history` is a list
    of prior {role, content} turns (user/assistant). Returns {answer}.

    ``complete`` / ``api_base`` / ``api_key`` are injectable so the call can be
    exercised without hitting the live model or proxy. Passing ``api_base``
    explicitly is required against the keyed deployment -- the proxy 404s
    otherwise (see the module docstring)."""
    context = build_context(messages, events, tasks, luma)
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%A %Y-%m-%d %H:%MZ")
    system = SYSTEM.format(now=now) + "\n\n# The user's data\n\n" + context
    convo = [{"role": "system", "content": system}]
    convo.extend(history[-8:])  # keep the last few turns for continuity
    convo.append({"role": "user", "content": question})
    kwargs = {"model": MODEL, "messages": convo, "max_tokens": 1000, "temperature": 0.3}
    if api_base:
        kwargs["api_base"] = api_base
    if api_key:
        kwargs["api_key"] = api_key
    resp = complete(**kwargs)
    return {"answer": resp.choices[0].message.content}


# --------------------------------------------------------------------------
# Agentic mode: the floating chatbot runs a real `claude` agent that can use
# latchkey to read the user's accounts and act on their behalf (read-and-advise only).
# --------------------------------------------------------------------------
AGENT_SYSTEM = (
    "You are the user's personal assistant, embedded as a floating chatbot inside their unified inbox. "
    "You can run the `latchkey curl` CLI to read their connected accounts LIVE (Gmail, Slack, Discord, "
    "GitHub, Google Calendar, Zoho) and read their local files, to answer questions and help them plan and draft. "
    "A compacted view of their workspace (who they are, their accounts, connected services) and a snapshot of their "
    "current inbox are included below for grounding; use latchkey for anything live or deeper. "
    "STRICT RULES: be concise and direct (a few short paragraphs max). You are READ-AND-ADVISE ONLY -- "
    "never send an email or message, never post/write to any external account, never modify or delete their files "
    "or this app's code. If they ask you to send/act externally, explain you're read-only for safety and offer a draft instead."
)

# The workspace memory files are the "compacted workspace": a durable digest of
# who the user is, their accounts, and what's connected -- so the agent has the same
# grounding the main workspace agent does, without being the heavyweight thing.
WORKSPACE_MEMORY_DIR = Path("runtime/memory")
_WORKSPACE_DIGEST_MAX = 12000  # chars, keeps the prompt bounded


def load_workspace_digest(memory_dir: Path = WORKSPACE_MEMORY_DIR) -> str:
    """Concatenate the memory markdown files into a compact workspace digest."""
    if not memory_dir.exists():
        return ""
    parts = []
    for path in sorted(memory_dir.glob("*.md")):
        if path.name == "MEMORY.md":  # the index; the individual files carry the content
            continue
        try:
            parts.append(f"### {path.stem}\n{path.read_text()}")
        except OSError:
            continue
    return "\n\n".join(parts)[:_WORKSPACE_DIGEST_MAX]


def _build_agent_prompt(question: str, history: list[dict], messages: list[dict],
                        events: list[dict], tasks: list[dict], luma: list[dict]) -> str:
    context = build_context(messages, events, tasks, luma)
    digest = load_workspace_digest()
    hist = "\n".join(f"{h.get('role')}: {h.get('content')}" for h in history[-6:])
    workspace = f"# The user's workspace (compacted)\n{digest}\n\n" if digest else ""
    return f"{workspace}# The user's inbox snapshot\n{context}\n\n# Recent conversation\n{hist}\n\n# The user's message\n{question}"


def _agent_argv(prompt: str, output_format: str) -> list[str]:
    argv = [
        "claude", "-p", prompt,
        "--append-system-prompt", AGENT_SYSTEM,
        "--permission-mode", "bypassPermissions",
        "--model", "claude-haiku-4-5",
        "--output-format", output_format,
    ]
    if output_format == "stream-json":
        argv += ["--verbose", "--include-partial-messages"]
    return argv


def agent_answer(
    question: str,
    history: list[dict],
    messages: list[dict],
    events: list[dict],
    tasks: list[dict],
    luma: list[dict],
    run: Callable | None = None,
    cli_available: bool | None = None,
    api_key: str | None = _API_KEY,
    answer_fn: Callable = answer,
) -> dict:
    """Answer via a real `claude` agent (tools enabled) so it can act across the
    user's accounts. Runs in a throwaway dir OUTSIDE the repo so the repo's
    CLAUDE.md / hooks don't hijack it.

    Backend precedence (see the module docstring): the `claude` CLI first; if it
    is absent, a keyed `ANTHROPIC_API_KEY` fallback (cached-data-only); if
    neither, a setup-instructions message. `run` / `cli_available` / `api_key` /
    `answer_fn` are injectable for tests."""
    have_cli = _claude_cli_available() if cli_available is None else cli_available
    try:
        if not have_cli and run is None:
            # No agentic CLI: use the fast keyed fallback if a key is present,
            # else explain how to configure a backend. The keyed call is inside
            # this try so its errors surface as a clean message too (the
            # /api/agent route has no handler of its own).
            if api_key:
                return answer_fn(question, history, messages, events, tasks, luma)
            return {"answer": _SETUP_HELP}
        prompt = _build_agent_prompt(question, history, messages, events, tasks, luma)
        argv = _agent_argv(prompt, "text")

        def _default_run() -> subprocess.CompletedProcess:
            # The throwaway working dir is removed once the agent finishes so it
            # doesn't accumulate in the system temp dir across invocations.
            with tempfile.TemporaryDirectory(prefix="ask-agent-") as workdir:
                return subprocess.run(
                    argv, cwd=workdir, capture_output=True,
                    text=True, timeout=240, env=os.environ.copy(),
                )

        runner = run or _default_run
        result = runner()
        out = (getattr(result, "stdout", "") or "").strip()
        return {"answer": out or "(the assistant returned nothing)"}
    except subprocess.TimeoutExpired:
        return {"answer": "That took too long to work through — try a more specific ask."}
    except Exception as exc:  # noqa: BLE001 - surface a clean error to the UI
        return {"answer": f"Sorry, the assistant hit an error ({type(exc).__name__})."}


def _stream_agent_lines(argv: list[str]):
    """Spawn the agent and yield its stdout lines as they arrive."""
    # The throwaway working dir must outlive the streaming subprocess, so it is
    # created explicitly and removed in the finally once the process is done.
    workdir = tempfile.mkdtemp(prefix="ask-agent-")
    proc = subprocess.Popen(
        argv, cwd=workdir,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env=os.environ.copy(),
    )
    try:
        assert proc.stdout is not None
        yield from proc.stdout
    finally:
        proc.stdout and proc.stdout.close()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(workdir, ignore_errors=True)


def agent_answer_stream(
    question: str,
    history: list[dict],
    messages: list[dict],
    events: list[dict],
    tasks: list[dict],
    luma: list[dict],
    stream_lines: Callable[[list[str]], object] | None = None,
    cli_available: bool | None = None,
    api_key: str | None = _API_KEY,
    answer_fn: Callable = answer,
):
    """Yield the agent's visible reply token-by-token, by parsing claude's
    stream-json events (only `content_block_delta` text deltas are surfaced --
    tool-use noise is dropped).

    Backend precedence (see the module docstring): the `claude` CLI first; if it
    is absent, a keyed `ANTHROPIC_API_KEY` fallback that yields the whole answer
    in one chunk (cached-data-only, not agentic); if neither, a setup message.
    `stream_lines` / `cli_available` / `api_key` / `answer_fn` are injectable for
    tests -- an injected `stream_lines` forces the CLI path."""
    have_cli = _claude_cli_available() if cli_available is None else cli_available
    if not have_cli and stream_lines is None:
        if api_key:
            # Fast fallback: one keyed completion, emitted as a single chunk.
            result = answer_fn(question, history, messages, events, tasks, luma)
            yield result.get("answer") or "(the assistant returned nothing)"
        else:
            yield _SETUP_HELP
        return
    prompt = _build_agent_prompt(question, history, messages, events, tasks, luma)
    argv = _agent_argv(prompt, "stream-json")
    lines = (stream_lines or _stream_agent_lines)(argv)
    emitted = False
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            evt = json.loads(line)
        except (ValueError, TypeError):
            continue
        if evt.get("type") != "stream_event":
            continue
        e = evt.get("event", {})
        if e.get("type") == "content_block_delta" and e.get("delta", {}).get("type") == "text_delta":
            text = e["delta"].get("text", "")
            if text:
                emitted = True
                yield text
    if not emitted:
        yield "(the assistant returned nothing)"

# unified-inbox

One fast, keyboard-driven place for everything: email, chat, GitHub, calendar,
tasks, San Francisco events — and an assistant that answers questions about all
of it. A Superhuman/Raycast-style personal command center.

Open it from the workspace as the **unified-inbox** tab (served at
`/service/unified-inbox/`). If the workspace has Cloudflare tunneling on, it's
also reachable at a public URL — read it from the browser's address bar with the
tab open.

## What's in it

Four views, switched from the segmented control in the top bar, plus a floating
Ask assistant (opened with `⌘J`):

- **Inbox** — one merged, newest-first stream across every source, with
  per-source color tags, unread markers, and an instant reading pane.
  - **Email**: Gmail API (the primary account) + IMAP (two Gmails + Zoho).
  - **Slack**: an allow-listed channel. **Discord**: recently-active channels
    in chosen servers (gently paced — user-token reads).
  - **GitHub**: notifications plus issues/PRs you're assigned, involved in, or
    asked to review.
  - Attachments and rich link previews render inline (images, YouTube/LinkedIn
    embed cards); email bodies render in a sandboxed frame.
- **Calendar** — a week / 3-day time-grid merging Google Calendar (API) and Zoho
  (CalDAV) and overlaying Luma SF events, with a detail panel. The same event
  subscribed in several calendars collapses to one (showing "also in ...").
- **Luma SF** — upcoming San Francisco events from Luma's public discover feed,
  as cover-art cards linking out to lu.ma.
- **Tasks** — open tasks (`- [ ]`) read from the user's live iCloud Obsidian vault
  (`YourVault/Today at Home.md` + `YourVault/Tasks.md`) via the file-sharing WebDAV
  endpoint, grouped by file, with each task's due date (overdue in red) and first
  tag surfaced. The right panel renders an Obsidian-style force-directed graph of
  the vault's `[[wikilinks]]` (notes as nodes, links as edges, degree as size);
  the note matching the selected task is highlighted. Because the crawl is slow
  (~60s over WebDAV) it's built in the background and cached, rebuilt on a slow
  cadence by the refresher (every ~30 min, `UNIFIED_INBOX_GRAPH_SECONDS`), with a
  "rebuild" control in the panel header to force a fresh crawl on demand.
- **Ask** — a floating, read-and-advise assistant (the FAB in the corner, or
  `⌘J`). It runs a real agent that can read the user's connected accounts *live* (via
  latchkey) on top of the cached inbox snapshot, to answer questions and help him
  plan and draft. It is read-only for safety: it never sends mail, posts, or takes
  external actions — it offers drafts instead. Its reply streams in token by token.

  **Backends (in precedence order):**
  1. **`claude` CLI (recommended, default when on PATH).** Runs the live agent
     above. No API key needed — it uses the CLI's own auth. Slower to start
     (~15s) but can read your accounts live.
  2. **`ANTHROPIC_API_KEY` fast fallback** (used only when the `claude` CLI is
     absent). A one-shot keyed completion. **Warning:** it is *not* agentic —
     it can only see the data already cached in this inbox, not anything live.
     Set the env var (get a key at <https://console.anthropic.com>) and restart.
  3. **Neither configured** → the assistant replies with these exact setup
     instructions instead of failing silently.

## How it works

A background "daemon" thread refreshes every source concurrently into a local
JSON cache every few minutes, so the UI opens instantly (with client-side
prefetch on top). Reads are served from the cache; message/thread detail and
attachments are fetched lazily. Third-party credentials are injected server-side
by the Minds gateway (latchkey) or stored locally under `runtime/` — never in
the repo.

Fuzzy search runs instantly over the cache; when a query is active you can also
trigger a **deep search** across full email + Slack history (Gmail API + IMAP +
Slack `search.messages`; Discord is deliberately excluded).

Keyboard: `j`/`k`/arrows to move (previews as you go), `Enter` to open, `/` or
`⌘F` to fuzzy-search, `⌘K` for a jump-to-anything palette, `⌘J` for the Ask
assistant, `c` to toggle Calendar. All state lives under `DATA_DIR` (default
`runtime/unified-inbox/`).

## Layout

| File | Role |
|---|---|
| `sources.py` | The message sources + their colors/labels |
| `fetchers.py` | Per-source fetch + normalize; message/thread/attachment detail; concurrent `refresh_all` |
| `calendars.py` | Google + Zoho calendar events |
| `luma.py` | Luma SF discover feed + single-event detail |
| `obsidian.py` | Open tasks from the live iCloud vault (WebDAV) |
| `obsidian_graph.py` | The vault's `[[wikilink]]` node/link graph (cached) |
| `search.py` | Deep full-history search across Gmail API + IMAP + Slack |
| `chat.py` | The Ask assistant (cached-only litellm answer + the live `claude` agent) |
| `store.py` | JSON cache under `DATA_DIR` |
| `runner.py` | Flask app + the refresher daemon + all API routes |
| `assets/app.html` | The single-page UI |

Network access in every fetcher (and the model call) is dependency-injected, so
the test suite runs fully offline.

## Develop

```bash
uv run unified-inbox                 # run locally (binds 127.0.0.1:8080)
cd libs/unified_inbox && uv run pytest   # full suite + ratchets (offline)
```

Changes go through the `update-service` skill (apply → refresh the tab →
verify → background hardening).

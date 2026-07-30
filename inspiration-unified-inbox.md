---
title: Unified Inbox
description: A keyboard-driven, read-only command center that unifies email, chat, calendar, events, and tasks into one inbox, with a live AI assistant and an Obsidian knowledge-graph view.
thumbnail: inspiration-unified-inbox.svg
format: v1
---

# Unified Inbox

This file is the manifest for the **Unified Inbox** inspiration (slug:
`unified-inbox`). It is the one document a future agent reads to understand,
present, and adapt this inspiration. If you are an agent in a mind that was
created from this inspiration, this file is your script: read all of it, then
follow "How to adapt it" below.

## What it is

Unified Inbox is a single, keyboard-first web app that collapses everything
you would otherwise check across a dozen tabs -- four email accounts, Slack,
Discord, Telegram, GitHub, two calendars, a San Francisco events feed, and your
Obsidian task vault -- into one fast, read-only command center in the spirit of
Superhuman or Raycast. A background daemon pulls every source into a local JSON
cache every minute or so, so the app opens instantly to a merged, newest-first
inbox with per-source color tags and unread markers. You move with `j`/`k`,
open with `Enter`, fuzzy-search the cache with `/`, jump anywhere with `Cmd-K`,
and trigger a full-history deep search across email and Slack when a query is
active. A week / 3-day Calendar overlays Google Calendar, Zoho (CalDAV), and
Luma events (all-day items as spanning bars); a Luma SF view shows upcoming
events with full detail; a Tasks view reads open checkboxes (`- [ ]`) from an
Obsidian vault and renders an interactive force-directed graph of its
`[[wikilinks]]`; and a floating "Ask" assistant (`Cmd-J`) runs a real agent
that can read the connected accounts live to answer questions and draft
replies. It is deliberately read-and-advise only: it surfaces, summarizes, and
drafts -- it never sends mail, posts, or takes any external action.

## How it works

The snapshot includes these paths (each is a repo-root-relative path copied
from the original mind onto a clean default-workspace-template base):

- `libs/unified_inbox`

`libs/unified_inbox` is the whole app: a Python library (`src/unified_inbox/`)
plus its tests. The runtime is a single synchronous Flask app served by the
threaded Werkzeug server (`runner.py`), with a background "refresher" daemon
thread that fetches every source concurrently into a local JSON cache so the UI
loads instantly. The pieces:

- `sources.py` -- the source registry (the four email accounts + Slack, Discord,
  Telegram, GitHub) with each source's color/label; served to the UI at
  `/api/sources`.
- `fetchers.py` -- per-source fetch + normalize into one message schema, and the
  concurrent `refresh_all`. Every third-party HTTP read goes through
  `latchkey curl` (dependency-injected as `get_json`), except IMAP (direct
  `imaplib` with stored app passwords) and Telegram.
- `calendars.py` -- Google Calendar (latchkey) + Zoho Calendar (CalDAV, Basic
  auth from a stored app password).
- `luma.py` -- Luma's public SF discover feed + (with a session cookie) your own
  registered/invited events + single-event detail.
- `obsidian.py` / `obsidian_graph.py` -- open tasks and the `[[wikilink]]`
  node/link graph, read from a local Obsidian vault over the minds file proxy.
- `search.py` -- deep full-history search across Gmail API + IMAP + Slack.
- `chat.py` -- the "Ask" assistant: primary path shells out to the `claude` CLI
  (a live agent); a keyed `ANTHROPIC_API_KEY` litellm fallback if the CLI is
  absent; setup instructions if neither is present.
- `store.py` -- the JSON cache under `DATA_DIR`.
- `assets/app.html` -- the entire single-page vanilla-JS UI.

At runtime the `[program:unified-inbox]` supervisord program (in
`supervisord.conf`) launches it with `uv run unified-inbox` (the
`unified-inbox` console script -> `runner:main`). It binds `127.0.0.1:8080`
(overridable via `UNIFIED_INBOX_PORT`) and registers that port under the
`unified-inbox` name via `scripts/forward_port.py`, so it shows up as the
`unified-inbox` workspace tab (served at `/service/unified-inbox/`). State
lives under `DATA_DIR` (default `runtime/unified-inbox/`, gitignored). The root
`pyproject.toml` carries it as a `[tool.uv.workspace]` member and a
`unified-inbox` dependency/source so `uv sync --all-packages` builds it.

## Prerequisites

Activation requirements: what the adopting agent must SET UP -- and must
INITIATE ITSELF during setup, before asking how to adapt -- for this
inspiration to run against the new user's own accounts/data. One line per
requirement, in this machine-readable form (greppable by `requires_`):

The message/calendar/GitHub sources read third-party APIs through
`latchkey curl`, so each needs a user-approved latchkey connector. The Obsidian
Tasks view reads a local vault through the minds file proxy. Email over IMAP,
the Zoho calendar, Luma's private feed, and Telegram use locally-stored secrets
rather than latchkey. The adopting agent must INITIATE each permission request
itself during setup (via the `latchkey` skill), not merely mention it.

- requires_permission: google-api / gmail.readonly (user-approved; adopting
  agent initiates during setup -- reads the primary Gmail account's inbox via
  the Gmail API, and deep-searches full mail history)
- requires_permission: google-api / calendar.readonly (user-approved; adopting
  agent initiates during setup -- reads all shown Google Calendars for the
  Calendar view)
- requires_permission: slack-api / slack-read-all (user-approved; adopting agent
  initiates during setup -- reads channel/DM history + `search.messages` for the
  Slack source and deep search; read-only)
- requires_permission: discord-api / discord-user-read (user-approved; adopting
  agent initiates during setup -- reads guild channels + latest messages with
  the user's OWN token; see the abuse-detection caution under Holes)
- requires_permission: github-api / github-read (user-approved; adopting agent
  initiates during setup -- reads `/user`, notifications, and issue/PR search)
- requires_permission: minds-api-proxy / files-read (user-approved; adopting
  agent initiates during setup -- reads the local Obsidian vault files via the
  file-sharing WebDAV endpoint for the Tasks view + knowledge graph)
- requires_secret: runtime/mail/accounts.json (JSON array of
  `{email, host, port, password}` entries -- app passwords for the two IMAP
  Gmail accounts and the Zoho account; the Zoho entry's app password ALSO
  authenticates the Zoho CalDAV calendar fetch)
- requires_secret: runtime/mail/luma_token (OPTIONAL -- your Luma
  `luma.auth-session-key` session cookie; only needed to overlay your own
  RSVP'd/invited Luma events. Without it the public SF discover feed still works)
- requires_secret: runtime/telegram/creds.json + runtime/telegram/session
  (OPTIONAL -- Telethon `{api_id, api_hash, phone}` plus the session file from a
  one-time phone login; the Telegram source stays absent until these exist)
- requires_secret: ANTHROPIC_API_KEY (OPTIONAL -- only the keyed fallback for the
  "Ask" assistant; the default path shells out to the `claude` CLI and needs no
  key)

## How to adapt it

Instructions for the NEXT agent -- the one adapting this inspiration into a
new mind. This is the `use-inspiration` skill's template path; in short:

1. Read this entire file first, especially "Prerequisites" and "Holes"
   below -- Prerequisites are your SETUP agenda, Holes are your ADAPTATION
   agenda.
2. Present the inspiration to the user in plain, non-technical language: what
   it is, what it does, and what it needs from them (name the Prerequisites).
3. Ask whether they want to use the same connectors (e.g. their own Slack).
   If YES: ACTIVATE FIRST -- initiate every `requires_permission` line NOW
   via a latchkey permission request (see the `latchkey` skill; the request
   opens the approval/login flow in the minds app), wire up any
   `requires_secret` values, start the services, and get the app showing
   THE USER'S OWN DATA. Done for a data-backed app means the user can open it
   and see their own data -- NOT that a service starts or an endpoint returns
   200. Then tell them it is live and to take a look.
4. Only AFTER that (or immediately, if they chose different connectors -- the
   swap is then the first adaptation) ask: "How do you want to adapt it?"
5. Work through each hole interactively, one at a time. Translate each into
   plain language, ask for a decision only when you genuinely need one, and
   resolve the obvious ones yourself.
6. When done, append a dated entry to "Adaptation history" below (never
   rewrite earlier entries) and commit.

## Holes

Every account/channel/server identifier was replaced with a neutral
placeholder. Before the app shows real data, the adopter must point these at
their own accounts:

- **Email accounts** (`sources.py`, `SOURCES`): the four entries are
  placeholders -- `primary` (Gmail API), `personal` and `secondary` (IMAP
  Gmail), and `zoho` (IMAP/CalDAV), with `label`/`email` set to
  `you@example.com`, `personal@example.com`, `secondary@example.com`, and
  `you@yourdomain.example`. Change each `label`/`email` to a real address, and
  add a matching `{email, host, port, password}` entry in
  `runtime/mail/accounts.json` for the three IMAP accounts. Keep the account
  *structure* (one Gmail-API + two IMAP Gmail + one Zoho) or restructure to fit
  the new user; the Gmail-API account is the one wired for full-history deep
  search.
- **Slack channels** (`fetchers.py`, `SLACK_CHANNEL_ALLOW`): empty by default.
  Add the channel IDs you want in the inbox, e.g. `["C0XXXXXXX"]`. DMs surface
  regardless of this list.
- **Discord servers + self** (`fetchers.py`, `DISCORD_GUILDS`,
  `DISCORD_SELF_USERNAME`): the guilds dict is empty (`{}`) -- add
  `{"<guild_id>": "<label>"}` for each server to scan, and set
  `DISCORD_SELF_USERNAME` to your own Discord username so your own messages are
  not marked unread.
- **Zoho CalDAV account id** (`calendars.py`, `ZOHO_EVENTS_URL`): the URL
  contains the literal placeholder `YOUR_ZOHO_CALDAV_ACCOUNT_ID`. Replace it
  with your Zoho CalDAV account id (keep the surrounding URL shape).
- **Obsidian vault** (`obsidian.py`, `VAULT_BASE` + `TASK_FILES`): the path
  points at `/Users/you/.../Documents/YourVault`. Set the user directory and
  vault folder name to match the machine hosting the vault; `TASK_FILES`
  defaults to `Today at Home.md` and `Tasks.md` -- point these at your own note
  files. The graph crawls the same vault, so it follows automatically.
- **Discord user-token caution**: Discord is read with the user's OWN token,
  which can trip Discord's abuse detection. The code already paces reads
  (`DISCORD_PACE_SECONDS`), keeps Discord OFF the fast refresh tick, and never
  pre-warms it -- keep those guards if you touch the refresh cadence.
- **The "Ask" assistant** (`chat.py`): the primary path shells out to the
  `claude` CLI (`claude -p`), which must be on `PATH`; this deployment ships
  WITHOUT an `ANTHROPIC_API_KEY`, so the keyed fallback is dormant. If the
  adopting mind has no `claude` CLI, either install it or set
  `ANTHROPIC_API_KEY` (see Prerequisites) to use the cached-data-only fallback.

## Adaptation history

Each mind that adapts this inspiration appends one dated entry below. Earlier
entries are never rewritten.

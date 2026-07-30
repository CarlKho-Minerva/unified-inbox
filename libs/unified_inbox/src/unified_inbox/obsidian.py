"""Read open tasks from the user's live Obsidian vault (the iCloud vault maintained
by voice-to-obsidian) via the Minds file-sharing WebDAV endpoint, so the inbox
can surface them in a Tasks view.

Tasks are read from a couple of files in the vault (see ``TASK_FILES``). Task
lines carry wiki-links, a ``**Label:**`` prefix, due-date/time/energy metadata,
tags, and a trailing ``<!-- voice-task:... -->`` marker -- all cleaned off for
display, with the due date and first tag surfaced as fields.

Only open checkboxes (``- [ ]``) are surfaced; done items (``- [x]``) are
skipped. File reads are injected so parsing can be tested without a real vault.
"""

import re
import subprocess
from collections.abc import Callable

# Point this at your own iCloud Obsidian vault folder (spaces URL-encoded):
# set the user directory and the vault folder name to match your machine.
VAULT_BASE = (
    "http://latchkey-self.invalid/minds-api-proxy/api/v1/files"
    "/Users/you/Library/Mobile%20Documents/iCloud~md~obsidian/Documents/YourVault"
)
# (url filename, display label) -- sample defaults; point these at your own note
# files. "Today at Home" leads the view; "Tasks" is the full list.
TASK_FILES = [
    ("Today%20at%20Home.md", "Today at Home"),
    ("Tasks.md", "Tasks"),
]

FileReader = Callable[[str], str]

_OPEN_TASK = re.compile(r"^\s*[-*]\s+\[ \]\s+(.+?)\s*$")
_WIKI = re.compile(r"\[\[([^\]|]+\|)?([^\]]+)\]\]")   # [[a|b]] -> b ; [[a]] -> a
_MDLINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")         # [t](u) -> t
_HTML_COMMENT = re.compile(r"<!--.*?-->")
_LABEL_PREFIX = re.compile(r"^\*\*[^*]+:\*\*\s*")      # **Must:** / **1 · easiest:**
_DUE = re.compile(r"📅\s*(\d{4}-\d{2}-\d{2})")
_TAG = re.compile(r"(?:^|\s)#([A-Za-z0-9_-]+)")
# Trailing task metadata to strip from the visible text: emoji markers, an
# "energy: x" note, time estimates, and hashtags.
_META_TAIL = re.compile(r"(📅\s*\d{4}-\d{2}-\d{2}|⏱️\s*\S+|·\s*\d+\s*min|🔺|🔼|🔽|⏬|energy:\s*\w+|#[A-Za-z0-9_-]+)")


def _dav_read(url: str) -> str:
    out = subprocess.run(["latchkey", "curl", "-s", url], capture_output=True, text=True, timeout=40)
    return out.stdout


def _clean(text: str) -> str:
    text = _HTML_COMMENT.sub("", text)
    text = _LABEL_PREFIX.sub("", text)
    text = _WIKI.sub(lambda m: m.group(2), text)
    text = _MDLINK.sub(lambda m: m.group(1), text)
    text = _META_TAIL.sub("", text)
    text = text.replace("**", "").replace("`", "")
    return " ".join(text.split()).strip(" ·-")


def parse_tasks(label: str, url: str, body: str) -> list[dict]:
    tasks = []
    for i, line in enumerate(body.splitlines()):
        m = _OPEN_TASK.match(line)
        if not m:
            continue
        raw = m.group(1)
        text = _clean(raw)
        if not text:
            continue
        due = _DUE.search(raw)
        tag = _TAG.search(raw)
        tasks.append({
            "id": f"task:{label}:{i}",
            "text": text,
            "file": label,
            "href": url,
            "line": i,
            "due": due.group(1) if due else "",
            "tag": tag.group(1) if tag else "",
        })
    return tasks


def fetch_obsidian_tasks(read_file: FileReader = _dav_read) -> list[dict]:
    out = []
    for fname, label in TASK_FILES:
        body = read_file(f"{VAULT_BASE}/{fname}")
        if body:
            out.extend(parse_tasks(label, f"{VAULT_BASE}/{fname}", body))
    return out


def fetch_tasks(read_file: FileReader = _dav_read) -> tuple[list[dict], dict]:
    """Fetch open vault tasks; return (tasks, status) for the refresh meta.
    ``read_file`` is injectable so the wrapper can be tested without WebDAV."""
    try:
        tasks = fetch_obsidian_tasks(read_file)
        return tasks, {"ok": True, "count": len(tasks)}
    except Exception as exc:  # noqa: BLE001 - isolate this source's failure
        return [], {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

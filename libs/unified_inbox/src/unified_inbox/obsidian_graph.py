"""Build a node/link graph of the user's Obsidian vault from its ``[[wikilinks]]``,
so the Tasks view can render an Obsidian-style graph.

Nodes are notes (by name); an edge is a wiki-link from one note to another.
Reading the whole vault is slow (hundreds of files over WebDAV), so the runner
builds this in the background and caches it. Listing + file reads are injected
so the parsing can be tested against fixtures without a real vault.
"""

import re
import subprocess
import urllib.parse
from collections.abc import Callable

# The graph crawls the same iCloud vault the Tasks view reads, so it shares the
# one vault-base constant instead of repeating the URL.
from unified_inbox.obsidian import VAULT_BASE as VAULT_ROOT

FileLister = Callable[[str], list[str]]
FileReader = Callable[[str], str]

_WIKILINK = re.compile(r"\[\[([^\]]+)\]\]")
MAX_FILES = 220  # bound the crawl


def _note_name(href_or_link: str) -> str:
    """The Obsidian note name: last path segment, no extension, no ``|alias`` or
    ``#heading``, URL-decoded."""
    s = href_or_link.split("|", 1)[0].split("#", 1)[0].rstrip("/")
    s = s.split("/")[-1]
    s = re.sub(r"\.md$", "", s, flags=re.I)
    return urllib.parse.unquote(s).strip()


def _list_md(root: str) -> list[str]:
    out = subprocess.run(
        ["latchkey", "curl", "-s", "-X", "PROPFIND", "-H", "Depth: infinity", root + "/"],
        capture_output=True, text=True, timeout=90,
    )
    hrefs = re.findall(r"<[^>]*href>([^<]+)</[^>]*href>", out.stdout, re.I)
    return [h for h in hrefs if h.lower().endswith(".md")]


def _rel_under_vault(href: str) -> str:
    """The path relative to the vault root, from a (lowercased) PROPFIND href --
    so the read URL can be rebuilt on the correctly-cased VAULT_ROOT. The marker
    is the vault folder name from VAULT_ROOT (lowercased)."""
    marker = "/" + VAULT_ROOT.rstrip("/").split("/")[-1].lower() + "/"
    low = href.lower()
    idx = low.find(marker)
    return href[idx + len(marker):] if idx >= 0 else href.rstrip("/").split("/")[-1]


def _read(href: str) -> str:
    url = f"{VAULT_ROOT}/{_rel_under_vault(href)}"
    out = subprocess.run(["latchkey", "curl", "-s", url], capture_output=True, text=True, timeout=30)
    return out.stdout


def build_graph(list_files: FileLister = _list_md, read_file: FileReader = _read) -> dict:
    """Return ``{nodes: [{id, label, val}], links: [{source, target}]}`` for the
    vault. Nodes are note names; links are resolved wiki-links between notes."""
    hrefs = list_files(VAULT_ROOT)[:MAX_FILES]
    names = [_note_name(h) for h in hrefs]
    known = set(names)
    degree: dict[str, int] = {n: 0 for n in names}
    edges: set[tuple[str, str]] = set()

    for href, name in zip(hrefs, names):
        body = read_file(href)
        if not body:
            continue
        for raw in _WIKILINK.findall(body):
            target = _note_name(raw)
            if not target or target == name:
                continue
            edges.add((name, target))
            degree[name] = degree.get(name, 0) + 1
            degree[target] = degree.get(target, 0) + 1
            known.add(target)  # keep link targets even if their file wasn't crawled

    nodes = [{"id": n, "label": n, "val": degree.get(n, 0)} for n in sorted(known)]
    links = [{"source": s, "target": t} for (s, t) in sorted(edges)]
    return {"nodes": nodes, "links": links}

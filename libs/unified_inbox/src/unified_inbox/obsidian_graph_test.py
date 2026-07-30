from unified_inbox.obsidian import VAULT_BASE
from unified_inbox.obsidian_graph import (
    MAX_FILES,
    VAULT_ROOT,
    _note_name,
    _rel_under_vault,
    build_graph,
)


def test_graph_and_tasks_share_one_vault_base() -> None:
    # The vault location is defined once and reused, not duplicated per module.
    assert VAULT_ROOT == VAULT_BASE


def test_note_name_strips_path_extension_alias_and_heading() -> None:
    assert _note_name("Task Database/Follow up with EF.md") == "Follow up with EF"
    assert _note_name("Foo|the alias") == "Foo"
    assert _note_name("Foo#a-heading") == "Foo"
    assert _note_name("Some%20Note.md") == "Some Note"  # URL-decoded


def test_rel_under_vault_extracts_path_after_the_vault_root() -> None:
    href = "/users/you/library/mobile%20documents/x/documents/yourvault/Task%20Database/A.md"
    assert _rel_under_vault(href) == "Task%20Database/A.md"
    # No marker -> fall back to the bare filename.
    assert _rel_under_vault("/somewhere/else/B.md") == "B.md"


def test_build_graph_resolves_wikilinks_into_nodes_and_edges() -> None:
    files = ["/personal/Tasks.md", "/personal/A.md", "/personal/B.md"]
    bodies = {
        "/personal/Tasks.md": "- [[A]]\n- [[Task Database/B|see B]]",
        "/personal/A.md": "links to [[B]]",
        "/personal/B.md": "a leaf note, no links",
    }
    g = build_graph(list_files=lambda root: files, read_file=lambda h: bodies[h])
    ids = {n["id"] for n in g["nodes"]}
    assert {"Tasks", "A", "B"} <= ids
    assert {"source": "Tasks", "target": "A"} in g["links"]
    assert {"source": "Tasks", "target": "B"} in g["links"]  # aliased + pathed link resolves to B
    assert {"source": "A", "target": "B"} in g["links"]
    # Degree drives node size: B is linked from Tasks and A.
    b = next(n for n in g["nodes"] if n["id"] == "B")
    assert b["val"] == 2


def test_build_graph_ignores_self_links_and_unreadable_files() -> None:
    files = ["/personal/A.md", "/personal/B.md"]
    bodies = {"/personal/A.md": "self ref [[A]] and [[B]]", "/personal/B.md": ""}
    g = build_graph(list_files=lambda root: files, read_file=lambda h: bodies.get(h, ""))
    assert {"source": "A", "target": "A"} not in g["links"]  # self-link dropped
    assert {"source": "A", "target": "B"} in g["links"]


def test_build_graph_empty_vault() -> None:
    g = build_graph(list_files=lambda root: [], read_file=lambda h: "")
    assert g == {"nodes": [], "links": []}


def test_build_graph_bounds_the_crawl_at_max_files() -> None:
    # The vault can hold hundreds of notes over slow WebDAV; the crawl must stop
    # at MAX_FILES so a big vault never reads unboundedly.
    files = [f"/personal/N{i}.md" for i in range(MAX_FILES + 50)]
    reads: list[str] = []

    def read_file(href: str) -> str:
        reads.append(href)
        return ""

    build_graph(list_files=lambda root: files, read_file=read_file)
    assert len(reads) == MAX_FILES

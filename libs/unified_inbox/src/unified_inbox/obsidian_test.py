from unified_inbox.obsidian import (
    TASK_FILES,
    VAULT_BASE,
    _clean,
    fetch_obsidian_tasks,
    fetch_tasks,
    parse_tasks,
)

# --- _clean: strip links, labels, metadata, and emphasis from task text ---


def test_clean_unwraps_wiki_links() -> None:
    assert _clean("Follow up with [[Alice]]") == "Follow up with Alice"
    # Aliased wiki-link keeps the display text (right of the pipe).
    assert _clean("See [[2026-07-20 Notes|today's notes]]") == "See today's notes"


def test_clean_unwraps_markdown_links_and_drops_emphasis() -> None:
    assert _clean("Read [the doc](https://example.com/x)") == "Read the doc"
    assert _clean("**Urgent** ship the `patch`") == "Urgent ship the patch"


def test_clean_strips_label_prefix_html_comment_and_metadata() -> None:
    line = (
        "**Must:** [[Task Database/Follow up with EF|Follow up with EF]] "
        "📅 2026-07-16 ⏱️ 30m energy: medium #personal <!-- voice-task:vt-1 -->"
    )
    assert _clean(line) == "Follow up with EF"


def test_clean_strips_trailing_minute_estimate() -> None:
    assert _clean("Start writing essays like Kanjun · 10 min") == "Start writing essays like Kanjun"


def test_clean_strips_priority_and_time_metadata() -> None:
    # Obsidian priority markers (🔺🔼🔽⏬), a ⏱️ time estimate, and hashtags are
    # all trailing metadata that must not show up in the visible text.
    assert _clean("Ship the build 🔺 ⏱️ 2h #work") == "Ship the build"


# --- parse_tasks: only open checkboxes, cleaned, with due/tag fields ---


def test_parse_tasks_extracts_only_open_checkboxes() -> None:
    body = "\n".join([
        "# Heading",
        "- [ ] Buy milk",
        "- [x] Already done",
        "* [ ] Star-bulleted task",
        "  - [ ] Indented task",
        "- [ ] ",  # empty after the box -> skipped
        "Just a line",
    ])
    tasks = parse_tasks("Tasks", "https://vault/Tasks.md", body)
    assert [t["text"] for t in tasks] == ["Buy milk", "Star-bulleted task", "Indented task"]


def test_parse_tasks_extracts_due_date_and_tag() -> None:
    body = "- [ ] [[Ship it]] 📅 2026-07-24 ⏱️ 60m #coding <!-- voice-task:x -->"
    task = parse_tasks("Tasks", "u", body)[0]
    assert task["text"] == "Ship it"
    assert task["due"] == "2026-07-24"
    assert task["tag"] == "coding"


def test_parse_tasks_records_source_reference() -> None:
    task = parse_tasks("Tasks", "https://vault/Tasks.md", "- [ ] Ship it")[0]
    assert task["id"] == "task:Tasks:0"
    assert task["file"] == "Tasks"
    assert task["href"] == "https://vault/Tasks.md"
    assert task["due"] == "" and task["tag"] == ""


def test_parse_tasks_surfaces_the_first_tag_only() -> None:
    task = parse_tasks("Tasks", "u", "- [ ] Plan the trip #travel #personal")[0]
    assert task["text"] == "Plan the trip"  # both tags stripped from the text
    assert task["tag"] == "travel"           # first tag surfaced


def test_parse_tasks_drops_a_line_that_is_only_label_and_metadata() -> None:
    # A checkbox whose content is nothing but a **Label:**, a tag, and a
    # voice-task marker cleans to empty and must not surface as a blank task.
    body = "- [ ] **Waiting:** #blocked <!-- voice-task:x -->"
    assert parse_tasks("Tasks", "u", body) == []


# --- fetch_obsidian_tasks: reads the two live files via injected read_file ---


def test_fetch_obsidian_tasks_reads_the_configured_files_in_order() -> None:
    read_urls = []
    bodies = {
        TASK_FILES[0][0]: "- [ ] today one\n- [ ] today two",   # Today at Home
        TASK_FILES[1][0]: "- [ ] full list item",               # Tasks
    }

    def read_file(url: str) -> str:
        read_urls.append(url)
        return bodies.get(url.rstrip("/").split("/")[-1], "")

    tasks = fetch_obsidian_tasks(read_file)
    # Today-at-Home file leads, then the full Tasks list.
    assert [t["file"] for t in tasks] == ["Today at Home", "Today at Home", "Tasks"]
    assert [t["text"] for t in tasks] == ["today one", "today two", "full list item"]
    assert read_urls == [f"{VAULT_BASE}/{TASK_FILES[0][0]}", f"{VAULT_BASE}/{TASK_FILES[1][0]}"]


def test_fetch_obsidian_tasks_skips_empty_or_unreadable_files() -> None:
    assert fetch_obsidian_tasks(read_file=lambda url: "") == []


# --- fetch_tasks: the (tasks, status) wrapper used by the refresh ---


def test_fetch_tasks_reports_ok_status_with_a_count() -> None:
    tasks, status = fetch_tasks(read_file=lambda url: "- [ ] one\n- [ ] two")
    assert status == {"ok": True, "count": len(tasks)}
    # Both configured files get the same fake body, so each task appears twice.
    assert [t["text"] for t in tasks] == ["one", "two", "one", "two"]


def test_fetch_tasks_records_a_failure_without_raising() -> None:
    def boom(url: str) -> str:
        raise TimeoutError("webdav timeout")

    tasks, status = fetch_tasks(read_file=boom)
    assert tasks == []
    assert status["ok"] is False
    assert "TimeoutError" in status["error"]

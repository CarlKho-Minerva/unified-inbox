from pathlib import Path

from unified_inbox.store import Store


def test_messages_round_trip(store: Store) -> None:
    msgs = [{"id": "a", "ts": 2}, {"id": "b", "ts": 1}]
    store.set_messages(msgs)
    assert store.get_messages() == msgs


def test_events_and_seen_and_meta_round_trip(store: Store) -> None:
    store.set_events([{"id": "e1", "start": 10}])
    store.set_seen({"chan": "msg123"})
    store.set_meta({"last_refresh": 5.0, "sources": {"slack": {"ok": True}}})
    assert store.get_events() == [{"id": "e1", "start": 10}]
    assert store.get_seen() == {"chan": "msg123"}
    assert store.get_meta()["sources"]["slack"]["ok"] is True


def test_luma_and_tasks_round_trip(store: Store) -> None:
    store.set_luma([{"id": "luma:1", "name": "Mixer", "start": 20}])
    store.set_tasks([{"id": "task:This Week:0", "text": "Ship it", "file": "This Week"}])
    assert store.get_luma() == [{"id": "luma:1", "name": "Mixer", "start": 20}]
    assert store.get_tasks() == [{"id": "task:This Week:0", "text": "Ship it", "file": "This Week"}]


def test_defaults_when_nothing_has_been_written(store: Store) -> None:
    assert store.get_messages() == []
    assert store.get_events() == []
    assert store.get_luma() == []
    assert store.get_tasks() == []
    assert store.get_seen() == {}
    assert store.get_meta() == {"last_refresh": None, "sources": {}}


def test_corrupt_file_falls_back_to_default(store: Store) -> None:
    store.set_messages([{"id": "a"}])
    store.messages_path.write_text("{ this is not valid json")
    assert store.get_messages() == []


def test_write_is_atomic_via_a_temp_file(store: Store, tmp_path: Path) -> None:
    # The writer replaces the target from a sibling .tmp; after a successful
    # write no stray temp file should remain.
    store.set_messages([{"id": "a"}])
    leftovers = list(store.dir.glob("*.tmp"))
    assert leftovers == []


def test_unicode_survives_the_round_trip(store: Store) -> None:
    store.set_messages([{"id": "a", "who": "Café · 日本語"}])
    assert store.get_messages()[0]["who"] == "Café · 日本語"

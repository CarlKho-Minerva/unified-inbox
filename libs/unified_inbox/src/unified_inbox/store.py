"""JSON-backed cache for the unified inbox. Small enough (a few hundred
messages) that a JSON snapshot is simpler and fast enough; no DB needed.

Files under DATA_DIR:
- messages.json : the merged, sorted list the UI reads
- meta.json     : last refresh time + per-source status/errors
- seen.json     : local read cursors for chat sources (Slack/Discord have no
                  simple REST unread flag, so we track "last seen" ourselves)
"""

import json
from pathlib import Path
from typing import Any


class Store:
    def __init__(self, data_dir: Path) -> None:
        self.dir = data_dir
        self.messages_path = data_dir / "messages.json"
        self.meta_path = data_dir / "meta.json"
        self.seen_path = data_dir / "seen.json"

    def _read(self, path: Path, default: Any) -> Any:
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return default

    def _write(self, path: Path, value: Any) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(value, ensure_ascii=False, indent=0))
        tmp.replace(path)

    def get_messages(self) -> list[dict]:
        return self._read(self.messages_path, [])

    def set_messages(self, messages: list[dict]) -> None:
        self._write(self.messages_path, messages)

    def get_events(self) -> list[dict]:
        return self._read(self.dir / "events.json", [])

    def set_events(self, events: list[dict]) -> None:
        self._write(self.dir / "events.json", events)

    def get_luma(self) -> list[dict]:
        return self._read(self.dir / "luma.json", [])

    def set_luma(self, events: list[dict]) -> None:
        self._write(self.dir / "luma.json", events)

    def get_tasks(self) -> list[dict]:
        return self._read(self.dir / "tasks.json", [])

    def set_tasks(self, tasks: list[dict]) -> None:
        self._write(self.dir / "tasks.json", tasks)

    def get_graph(self) -> dict:
        return self._read(self.dir / "graph.json", {"nodes": [], "links": []})

    def set_graph(self, graph: dict) -> None:
        self._write(self.dir / "graph.json", graph)

    def get_meta(self) -> dict:
        return self._read(self.meta_path, {"last_refresh": None, "sources": {}})

    def set_meta(self, meta: dict) -> None:
        self._write(self.meta_path, meta)

    def get_seen(self) -> dict:
        return self._read(self.seen_path, {})

    def set_seen(self, seen: dict) -> None:
        self._write(self.seen_path, seen)

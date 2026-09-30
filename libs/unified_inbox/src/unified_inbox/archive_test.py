import functools

import pytest

from unified_inbox.archive import ArchiveError, set_archived

CREDS = {"a@gmail.com": {"host": "imap.gmail.com", "port": 993, "email": "a@gmail.com", "password": "x"}}
SRC = {"t": {"via": "imap", "email": "a@gmail.com"}, "g": {"via": "gmail_api"}}
archive_imap = functools.partial(set_archived, load_creds=lambda: CREDS, sources=SRC)


class FakeImap:
    """Folders map name -> {uid: message_id}. Records moves."""

    def __init__(self, folders: dict[str, dict[str, str]]) -> None:
        self.folders, self.cur, self.moves = folders, None, []

    def select(self, name: str):
        self.cur = name.strip('"')
        return ("OK", [b"1"]) if self.cur in self.folders else ("NO", [b""])

    def uid(self, cmd: str, *args):
        if cmd == "SEARCH":
            want = args[-1].strip('"')
            hits = [u for u, mid in self.folders[self.cur].items() if mid == want]
            return "OK", [" ".join(hits).encode()]
        if cmd == "MOVE":
            uid, dst = args
            mid = self.folders[self.cur].pop(uid)
            self.folders[dst.strip('"')][uid] = mid
            self.moves.append((self.cur, dst.strip('"'), mid))
            return "OK", [b""]
        raise AssertionError(cmd)

    def logout(self) -> None:
        pass


def _msg(message_id: str = "<m1@x>") -> dict:
    return {"id": "t:5", "source": "t", "kind": "email", "native_id": "5", "message_id": message_id}


def test_archive_moves_by_message_id_and_undo_restores() -> None:
    fake = FakeImap({"INBOX": {"11": "<m0@x>", "12": "<m1@x>"}, "[Gmail]/All Mail": {}})
    archive_imap(_msg(), True, connect=lambda c: fake)
    assert fake.folders["INBOX"] == {"11": "<m0@x>"}
    archive_imap(_msg(), False, connect=lambda c: fake)
    assert fake.folders["INBOX"]["12"] == "<m1@x>"


def test_refuses_when_not_exactly_one_match() -> None:
    fake = FakeImap({"INBOX": {"11": "<m0@x>"}, "[Gmail]/All Mail": {}})
    with pytest.raises(ArchiveError, match="found 0"):
        archive_imap(_msg(), True, connect=lambda c: fake)
    assert fake.moves == []


def test_refuses_without_message_id() -> None:
    with pytest.raises(ArchiveError, match="Message-ID"):
        archive_imap(_msg(""), True, connect=lambda c: FakeImap({"INBOX": {}}))


def test_gmail_api_removes_inbox_label() -> None:
    calls = []
    m = {"id": "g:1", "source": "g", "kind": "email", "native_id": "abc"}
    archive_imap(m, True, post_json=lambda url, body: calls.append((url, body)) or {"id": "abc"})
    assert calls[0][1] == {"removeLabelIds": ["INBOX"]} and calls[0][0].endswith("/abc/modify")
    with pytest.raises(ArchiveError, match="refused"):
        archive_imap(m, True, post_json=lambda url, body: {"error": {"code": 403}})

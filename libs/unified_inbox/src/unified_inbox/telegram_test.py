from types import SimpleNamespace

from unified_inbox import telegram
from unified_inbox.telegram import (
    _entity_url,
    _media_label,
    _msg_attachment,
    fetch_telegram,
    fetch_telegram_thread,
)


def _doc(mime: str = "", file_name: str = "") -> SimpleNamespace:
    attrs = [SimpleNamespace(file_name=file_name)] if file_name else []
    return SimpleNamespace(mime_type=mime, attributes=attrs)


class _MediaWebPage:
    """Stands in for Telethon's MessageMediaWebPage (matched by class name)."""


def test_entity_url_prefers_username_else_web() -> None:
    assert _entity_url(SimpleNamespace(username="someone")) == "https://t.me/someone"
    assert _entity_url(SimpleNamespace(username=None)) == "https://web.telegram.org/"


def test_media_label_covers_each_kind() -> None:
    assert _media_label(SimpleNamespace(photo=object())) == "[photo]"
    assert _media_label(SimpleNamespace(document=_doc(mime="image/png"))) == "[image]"
    assert _media_label(SimpleNamespace(document=_doc(mime="video/mp4"))) == "[video]"
    assert _media_label(SimpleNamespace(document=_doc(mime="image/gif"))) == "[image]"
    assert _media_label(SimpleNamespace(document=_doc(mime="application/gif"))) == "[GIF]"
    assert _media_label(SimpleNamespace(document=_doc(mime="audio/ogg"))) == "[voice]"
    assert _media_label(SimpleNamespace(document=_doc(mime=""), sticker=object())) == "[sticker]"
    assert _media_label(SimpleNamespace(document=_doc(mime="application/pdf", file_name="x.pdf"))) == "[file: x.pdf]"
    assert _media_label(SimpleNamespace(document=_doc(mime="application/octet-stream"))) == "[file]"


def test_media_label_webpage_and_none() -> None:
    # A link preview -> "[link]", via either the web_preview flag or the media class name.
    assert _media_label(SimpleNamespace(web_preview=object())) == "[link]"
    assert _media_label(SimpleNamespace(media=_MediaWebPage())) == "[link]"
    # Unknown media present -> generic "[media]"; nothing at all -> "".
    assert _media_label(SimpleNamespace(media=object())) == "[media]"
    assert _media_label(SimpleNamespace()) == ""


def test_msg_attachment_inline_image_for_photo_and_image_doc() -> None:
    photo = _msg_attachment(SimpleNamespace(photo=object(), id=7), chat_id=42)
    assert photo == {"name": "photo", "mime": "image/jpeg",
                     "url": "api/telegram-media/42/7", "is_image": True}
    doc = _msg_attachment(SimpleNamespace(document=_doc(mime="image/png"), id=9), chat_id=42)
    assert doc is not None
    assert doc["is_image"] is True and doc["url"] == "api/telegram-media/42/9"


def test_msg_attachment_chip_for_file_and_none_for_link_or_empty() -> None:
    chip = _msg_attachment(SimpleNamespace(document=_doc(mime="application/pdf", file_name="x.pdf"), id=1), chat_id=5)
    assert chip == {"name": "file: x.pdf", "mime": "", "url": None, "is_image": False}
    # A bare link and a message with no media both yield no attachment.
    assert _msg_attachment(SimpleNamespace(web_preview=object(), id=2), chat_id=5) is None
    assert _msg_attachment(SimpleNamespace(id=3), chat_id=5) is None


def test_fetch_returns_empty_without_creds(tmp_path, monkeypatch) -> None:
    # No creds file -> no crash, empty result (source stays absent, never errors).
    monkeypatch.setattr(telegram, "CREDS_FILE", tmp_path / "missing.json")
    assert fetch_telegram() == []
    assert fetch_telegram_thread("123") == []


def test_thread_rejects_a_non_numeric_chat_id(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(telegram, "CREDS_FILE", tmp_path / "c.json")
    (tmp_path / "c.json").write_text('{"api_id": 1, "api_hash": "x", "phone": "+1"}')
    assert fetch_telegram_thread("not-an-int") == []

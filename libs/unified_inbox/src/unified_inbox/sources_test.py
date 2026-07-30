from unified_inbox.sources import SOURCES

_VALID_KINDS = {"email", "chat", "github"}
_VALID_VIAS = {"gmail_api", "imap", "slack", "discord", "github", "telegram"}
_REQUIRED_KEYS = {"label", "short", "kind", "provider", "via", "color", "group"}


def test_every_source_has_the_required_metadata() -> None:
    for key, cfg in SOURCES.items():
        missing = _REQUIRED_KEYS - cfg.keys()
        assert not missing, f"{key} is missing {missing}"
        assert cfg["kind"] in _VALID_KINDS, f"{key} has bad kind {cfg['kind']}"
        assert cfg["via"] in _VALID_VIAS, f"{key} has bad via {cfg['via']}"
        assert cfg["color"].startswith("#"), f"{key} color is not a hex string"


def test_email_sources_declare_the_account_they_map_to() -> None:
    # The IMAP path and the detail fetchers look each email source up by its
    # "email" field, so every email source must carry one.
    for key, cfg in SOURCES.items():
        if cfg["kind"] == "email":
            assert cfg.get("email"), f"email source {key} has no email address"


def test_the_expected_message_sources_are_present() -> None:
    assert set(SOURCES) == {"primary", "personal", "secondary", "zoho", "slack", "discord", "github", "telegram"}

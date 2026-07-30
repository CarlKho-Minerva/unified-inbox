"""Central definition of the inbox message sources. The frontend fetches this
same config via /api/sources so colors and labels stay in one place.

The four email accounts are placeholders an adopter points at their own
accounts: one Gmail-API account (`primary`), two IMAP Gmails (`personal`,
`secondary`), and one IMAP/CalDAV account (`zoho`). Set the `label`/`email`
fields (and the matching entries in the mail-accounts store) to your own."""

# Each source: key -> metadata. `via` picks the fetch path; `email` links an
# IMAP source to its entry in the mail accounts store.
SOURCES = {
    "primary": {
        "label": "you@example.com",
        "short": "PRIMARY",
        "kind": "email",
        "provider": "Gmail",
        "via": "gmail_api",
        "email": "you@example.com",
        "color": "#2f6bff",
        "group": "Email",
    },
    "personal": {
        "label": "personal@example.com",
        "short": "PERSONAL",
        "kind": "email",
        "provider": "Gmail",
        "via": "imap",
        "email": "personal@example.com",
        "color": "#12a150",
        "group": "Email",
    },
    "secondary": {
        "label": "secondary@example.com",
        "short": "SECONDARY",
        "kind": "email",
        "provider": "Gmail",
        "via": "imap",
        "email": "secondary@example.com",
        "color": "#c9820a",
        "group": "Email",
    },
    "zoho": {
        "label": "you@yourdomain.example",
        "short": "ZOHO",
        "kind": "email",
        "provider": "Zoho",
        "via": "imap",
        "email": "you@yourdomain.example",
        "color": "#e0533d",
        "group": "Email",
    },
    "slack": {
        "label": "Slack",
        "short": "SLACK",
        "kind": "chat",
        "provider": "Slack",
        "via": "slack",
        "color": "#9333ea",
        "group": "Chat",
    },
    "discord": {
        "label": "Discord",
        "short": "DISCORD",
        "kind": "chat",
        "provider": "Discord",
        "via": "discord",
        "color": "#5865f2",
        "group": "Chat",
    },
    "telegram": {
        "label": "Telegram",
        "short": "TELEGRAM",
        "kind": "chat",
        "provider": "Telegram",
        "via": "telegram",
        "color": "#229ed9",
        "group": "Chat",
    },
    "github": {
        "label": "GitHub",
        "short": "GITHUB",
        "kind": "github",
        "provider": "GitHub",
        "via": "github",
        "color": "#57606a",
        "group": "Dev",
    },
}

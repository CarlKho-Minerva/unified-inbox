"""Calm triage: sort every message into one of three lanes.

- ``act``      -- something needs you: a person waiting on a reply, OR an
                  admin system asking you to do something (Expensify "submit
                  your report", a scholarship form, a verification deadline).
- ``upcoming`` -- events, invites, RSVPs, calendar noise. Shown as a thin strip.
- ``later``    -- receipts, newsletters, notifications. Collapsed to a count.

The judgment is "does this need an ACTION from me", not "is it from a human".
Admin mail that asks for an action outranks a human FYI.

Three layers, cheapest first:
1. Rules (always on).
2. Your own behaviour: every move/reply/archive you make is appended to
   ``signals.jsonl`` and a manual lane move is a sticky override. That file is
   the training set for layer 3.
3. AnyJev PREVIEW (off unless ``UNIFIED_INBOX_ANYJEV_URL`` is set): a local
   typed-judgment model scoring ``needs_action``. Its probability is shown but
   only overrides the rules when it is confident. Once enough signals exist,
   fit an L2 head on them (``Decider.fit_head``).
"""

import json
import os
import re
import threading
import time
from pathlib import Path

LANES = ("act", "upcoming", "later")

# Admin systems asking you to DO something. Beats every other rule: this is the
# Expensify / travel-fund / scholarship class that must never hide in "later".
_STRONG = re.compile(
    r"\b(action (required|needed)|please (submit|review|approve|confirm|complete|sign)|submit (your|the)|"
    r"awaiting your|needs? your (approval|signature|review|attention)|expense report|expensify report|"
    r"report (is )?(due|rejected|returned|needs)|missing receipts?|reimburse(ment)?|travel fund|scholarship|"
    r"(pending|awaiting|needs|requires) (your )?approval|approval (required|needed)|report (was )?rejected|sign (the|your)|docusign|signature requested|"
    r"complete your (application|profile|registration|form|onboarding)|payment (failed|declined)|past due|overdue|"
    r"respond by|due (by|on)|deadline|final notice|i-?765|uscis|dso|sevp|83\(b\)|irs|franchise tax)\b",
    re.I,
)
_PROMO = re.compile(
    r"(% off|\bsale\b|coupon|deal|wishlist|offers?\b|recommended|did .{0,60} meet your|rate your|review your purchase|"
    r"new arrivals|limited time|hurry|don't miss|webinar|newsletter|digest|weekly|unsubscribe|"
    r"security code|verification code|one-time code|\botp\b|password reset|new sign-?in|security alert|"
    r"receipt|your order|order confirm|shipped|delivered|payment received|we've received your payment|statement is ready|"
    r"winners are live|new message in|sign-?in code|authentication|two-step|price cut|points|this week at|interest form|new text message)",
    re.I,
)
_EVENT = re.compile(
    r"\b(invitation:|invited you to (an? )?event|you're invited|you are invited|rsvp|register(ed)? for|registration (approved|confirmed|pending)|"
    r"event|meetup|happy hour|mixer|summit|conference|hackathon|hack\b|launch party|pop-?up|demo day|"
    r"tomorrow:|tonight:|luma|partiful|eventbrite|accepted:|declined:|updated invitation|starts (in|at|tomorrow)|join us|techweek)\b",
    re.I,
)
_ASK = re.compile(r"\b(can you|could you|would you|let me know|are you free|thoughts\?|quick question|following up|circling back)\b", re.I)
_NOREPLY = re.compile(
    r"(no-?reply|do-?not-?reply|notifications?@|notify@|mailer|news(letter)?@|marketing@|updates?@|hello@|team@|info@|"
    r"support@|billing@|offers@|events?@|community@|digest@|@.*(substack|beehiiv|mailchimp|luma-mail|lu\.ma|partiful|eventbrite))",
    re.I,
)
_BRANDY = re.compile(r"(\bfrom\b|\bteam\b|\binc\b|\bhq\b|\bevents?\b|\bnotifications\b|\||@|&|[^\x00-\x7f])", re.I)


def _looks_personal(m: dict) -> bool:
    name, addr = (m.get("who") or "").strip(), m.get("addr") or ""
    if _NOREPLY.search(addr) or _BRANDY.search(name):
        return False
    parts = name.replace('"', "").split()
    return 2 <= len(parts) <= 4 and all(p[:1].isupper() for p in parts)


_DATE = re.compile(
    r"\b(?:by|before|due|until|on)\s+((?:mon|tue|wed|thu|fri|sat|sun)\w*,?\s+)?"
    r"((?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\.?\s+\d{1,2}|\d{1,2}/\d{1,2})",
    re.I,
)


def summarize(m: dict) -> str:
    """One calm line: the first real sentence of the snippet, trimmed."""
    s = re.sub(r"\s+", " ", (m.get("snippet") or "")).strip()
    s = re.sub(r"^(hi|hey|hello|dear)\b[^,.!]{0,40}[,.!]\s*", "", s, flags=re.I)
    s = s.replace("—", "-").replace("–", "-")
    cut = re.split(r"(?<=[.!?])\s", s, maxsplit=1)[0]
    return (cut[:140] + "...") if len(cut) > 140 else cut


# Carl's own addresses: mail from himself (forwards, notes-to-self, agent reports) never "needs him".
_SELF = frozenset({"carl@somach.life", "kho@uni.minerva.edu", "carlkho.cvk@gmail.com", "carlcrafters@gmail.com",
                   "carl@themildlyusefulcompany.com"})


def rule_lane(m: dict) -> tuple[str, str]:
    """(lane, reason) from rules alone."""
    if (m.get("addr") or "").lower() in _SELF:
        return "later", "from you"
    text = f"{m.get('subject', '')} {m.get('snippet', '')}"
    sender = f"{m.get('who', '')} {m.get('addr', '')}"
    kind = m.get("kind")
    if kind == "chat":
        if m.get("is_dm") or m.get("is_mention"):
            return "act", "dm or mention"
        return "later", "channel chatter"
    if kind == "github":
        return ("act", "review requested") if re.search(r"review|assigned|mention", text, re.I) else ("later", "notification")
    subject = m.get("subject") or ""
    # Action phrases count only in the subject: bodies and snippets of marketing
    # mail are full of "deadline", "expires", "complete your profile".
    if _STRONG.search(subject) and not re.search(r"registration (pending|approved)|waitlist|webinar|statement is ready", subject, re.I):
        return "act", "asks for an action"
    personal = _looks_personal(m)
    if personal and (_ASK.search(text) or re.match(r"\s*re:", m.get("subject") or "", re.I)):
        return "act", "a person is waiting"
    if _PROMO.search(text) or _NOREPLY.search(m.get("addr") or ""):
        return "later", "automated"
    if _EVENT.search(text):
        return "upcoming", "event or invite"
    if personal:
        return "act", "from a person"
    return "later", "automated"


def deadline_hint(m: dict) -> str | None:
    hit = _DATE.search(f"{m.get('subject', '')} {m.get('snippet', '')}")
    return hit.group(2) if hit else None


def _needs_action_question():
    from anyjev import Question  # type: ignore[import-not-found]

    return Question.noul(
        "Does this email need an action from the recipient: a reply to a person, a form, a submission, "
        "an approval, a payment, or a deadline? Event invites, receipts, codes and newsletters that only inform do not.",
        name="needs_action",
    )


class Triage:
    """Rules + your overrides + optional AnyJev preview. Thread-safe enough for Flask."""

    def __init__(self, data_dir: Path) -> None:
        self.dir = data_dir
        self.signals_path = data_dir / "signals.jsonl"
        self.overrides_path = data_dir / "lane_overrides.json"
        self.scores_path = data_dir / "anyjev_scores.json"
        self._lock = threading.Lock()
        self.anyjev_url = os.environ.get("UNIFIED_INBOX_ANYJEV_URL", "").strip()
        self.anyjev_error: str | None = None
        self._decider = None
        self.anyjev_trusted = os.environ.get("UNIFIED_INBOX_ANYJEV_TRUST") == "1"

    # ---- persistence ----
    def _read(self, path: Path, default):
        try:
            return json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return default

    def _write(self, path: Path, value) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(value))
        tmp.replace(path)

    def overrides(self) -> dict:
        return self._read(self.overrides_path, {})

    def record(self, m: dict | None, mid: str, action: str, lane: str | None = None) -> None:
        """Append one behaviour signal. A lane move is also a sticky override."""
        row = {"ts": time.time(), "id": mid, "action": action, "lane": lane}
        if m:
            row.update(
                subject=m.get("subject"), who=m.get("who"), addr=m.get("addr"),
                snippet=(m.get("snippet") or "")[:400], rule_lane=rule_lane(m)[0],
            )
        with self._lock:
            self.dir.mkdir(parents=True, exist_ok=True)
            with self.signals_path.open("a") as f:
                f.write(json.dumps(row) + "\n")
            if action == "done":
                # Done = handled: it leaves Needs you for good (still visible under All mail).
                ov = self.overrides()
                ov[mid] = "done"
                self._write(self.overrides_path, ov)
            if action == "move" and lane in LANES:
                ov = self.overrides()
                ov[mid] = lane
                # One move teaches the whole sender: the next mail from them
                # lands in the same lane unless a rule says it asks for action.
                if m and m.get("addr"):
                    ov.setdefault("_senders", {})[m["addr"].lower()] = lane
                self._write(self.overrides_path, ov)

    def signal_count(self) -> int:
        try:
            with self.signals_path.open() as f:
                return sum(1 for _ in f)
        except OSError:
            return 0

    # ---- AnyJev preview ----
    def score_new(self, messages: list[dict]) -> None:
        """Score unscored emails with AnyJev. No-op unless the env var is set.
        Failures are recorded and surfaced in /api/triage, never swallowed."""
        if not self.anyjev_url:
            return
        try:
            from anyjev import Decider, Question  # type: ignore[import-not-found]
            from anyjev.backends.vllm import VLLMBackend  # type: ignore[import-not-found]
        except ImportError:
            self.anyjev_error = "anyjev not installed (pip install anyjev)"
            return
        scores = self._read(self.scores_path, {})
        todo = [m for m in messages if m.get("kind") == "email" and m["id"] not in scores][:64]
        if not todo:
            return
        try:
            if self._decider is None:
                backend = VLLMBackend(
                    self.anyjev_url,
                    os.environ.get("UNIFIED_INBOX_ANYJEV_MODEL", "gemma-4-31b-it"),
                    tokenizer_name=os.environ.get("UNIFIED_INBOX_ANYJEV_TOKENIZER", "QuantTrio/gemma-4-31B-it-AWQ"),
                    workers=8,
                )
                self._decider = Decider(backend, level="L0")
            states = [
                {"from": f"{m.get('who')} <{m.get('addr')}>", "subject": m.get("subject"), "preview": m.get("snippet")}
                for m in todo
            ]
            for m, dc in zip(todo, self._decider.decide_batch(states, _needs_action_question())):
                scores[m["id"]] = float(dc.probs[0])
            self.anyjev_error = None
        except Exception as e:  # surfaced in the page footer, never swallowed
            self.anyjev_error = f"{type(e).__name__}: {e}"[:200]
        self._write(self.scores_path, scores)

    # ---- the view ----
    def lanes(self, messages: list[dict]) -> dict:
        ov = self.overrides()
        senders = ov.get("_senders", {})
        scores = self._read(self.scores_path, {})
        out = {lane: [] for lane in LANES}
        for m in messages:
            lane, why = rule_lane(m)
            p = scores.get(m["id"])
            # Zero-shot L0 scores saturate (a GPU promo scored 0.99, a real reply
            # 0.00 on 2026-09-29), so the model only moves mail once trusted,
            # i.e. after an L2 head is fit on signals.jsonl.
            if self.anyjev_trusted and p is not None and p >= 0.85 and lane != "act":
                lane, why = "act", f"model {p:.2f}"
            elif self.anyjev_trusted and p is not None and p <= 0.15 and why == "from a person":
                lane, why = "later", f"model {p:.2f}"
            sender_lane = senders.get((m.get("addr") or "").lower())
            if sender_lane and why != "asks for an action":
                lane, why = sender_lane, "you moved this sender"
            if ov.get(m["id"]) == "done":
                continue
            if m["id"] in ov:
                lane, why = ov[m["id"]], "you moved it"
            out[lane].append({
                "id": m["id"], "source": m.get("source"), "kind": m.get("kind"),
                "who": m.get("who"), "subject": m.get("subject"), "summary": summarize(m),
                "ts": m.get("ts"), "unread": m.get("unread"), "url": m.get("url"),
                "why": why, "p": p, "due": deadline_hint(m),
            })
        return {
            "lanes": out,
            "preview": {
                "anyjev": bool(self.anyjev_url), "anyjev_error": self.anyjev_error,
                "signals": self.signal_count(), "fit_after": 150,
            },
        }

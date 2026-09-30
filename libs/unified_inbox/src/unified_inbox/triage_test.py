from pathlib import Path

from unified_inbox.triage import Triage, deadline_hint, rule_lane, summarize


def _email(who: str, addr: str, subject: str, snippet: str = "", mid: str = "g:1") -> dict:
    return {"id": mid, "kind": "email", "who": who, "addr": addr, "subject": subject, "snippet": snippet, "ts": 1.0}


def test_admin_action_mail_needs_you_even_from_noreply() -> None:
    m = _email("Expensify", "concierge@expensify.com", "Action required: submit your expense report")
    assert rule_lane(m) == ("act", "asks for an action")


def test_scholarship_approval_needs_you() -> None:
    m = _email("Travel Fund Request", "noreply@linuxfoundation.org", "Dan Kohn Scholarship Approval - KubeCon")
    assert rule_lane(m)[0] == "act"


def test_person_reply_thread_needs_you() -> None:
    assert rule_lane(_email("Josh Albrecht", "josh@imbue.com", "Re: Thank you Josh!!")) == ("act", "a person is waiting")


def test_event_invite_is_upcoming() -> None:
    assert rule_lane(_email("Karissa P.", "k@x.com", "You are invited to Claude Founder House"))[0] == "upcoming"


def test_registration_pending_is_not_an_action() -> None:
    m = _email("Daytona", "events@daytona.io", "Registration pending approval for Daytona AI Builders")
    assert rule_lane(m)[0] != "act"


def test_security_code_is_later() -> None:
    assert rule_lane(_email("Expensify", "noreply@expensify.com", "Your Expensify security code")) == ("later", "automated")


def test_chat_dm_needs_you_channel_does_not() -> None:
    assert rule_lane({"kind": "chat", "is_dm": True, "subject": "", "snippet": "hey"})[0] == "act"
    assert rule_lane({"kind": "chat", "is_dm": False, "is_mention": False, "subject": "", "snippet": "hey"})[0] == "later"


def test_summarize_drops_greeting_and_dashes() -> None:
    assert summarize({"snippet": "Hey Carl, the form is due Friday — thanks. More text."}) == "the form is due Friday - thanks."


def test_deadline_hint() -> None:
    assert deadline_hint(_email("A B", "a@b.c", "Please submit by Oct 3")) == "Oct 3"
    assert deadline_hint(_email("A B", "a@b.c", "hello")) is None


def test_move_is_sticky_and_teaches_the_sender(tmp_path: Path) -> None:
    t = Triage(tmp_path)
    first = _email("Pack Leaders", "news@pack.org", "This week's roundup", mid="g:1")
    second = _email("Pack Leaders", "news@pack.org", "Another roundup", mid="g:2")
    t.record(first, "g:1", "move", "upcoming")
    lanes = t.lanes([first, second])["lanes"]
    assert [m["id"] for m in lanes["upcoming"]] == ["g:1", "g:2"]
    assert lanes["upcoming"][1]["why"] == "you moved this sender"
    assert t.signal_count() == 1


def test_sender_lesson_never_hides_an_action_request(tmp_path: Path) -> None:
    t = Triage(tmp_path)
    t.record(_email("Expensify", "c@expensify.com", "Newsletter", mid="g:1"), "g:1", "move", "later")
    ask = _email("Expensify", "c@expensify.com", "Action required: missing receipts", mid="g:2")
    assert t.lanes([ask])["lanes"]["act"][0]["id"] == "g:2"


def test_anyjev_preview_is_off_without_env(tmp_path: Path) -> None:
    t = Triage(tmp_path)
    t.score_new([_email("A B", "a@b.c", "x")])
    preview = t.lanes([])["preview"]
    assert preview["anyjev"] is False and preview["anyjev_error"] is None

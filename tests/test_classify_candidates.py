#!/usr/bin/env python3
"""Offline unit test for classify_candidates in super-board-approval.py.

Proves the safety boundary of the human-comment classifier feed: the deterministic
merge/bounce signals are excluded, bots and untrusted users are excluded, comments
before the pinned request are excluded, and a free-text trusted human comment (even
an EDITED one) is surfaced for the orchestrator to classify. Run:
    python3 .claude/super-board/tests/test_classify_candidates.py
"""
import importlib.util
from datetime import datetime, timezone
from pathlib import Path

# One file, two homes: the canonical pack (tests/ beside scripts/) and an
# installed repo (.claude/super-board/tests/ beside .claude/bin/).
_here = Path(__file__).resolve()
_bin = next(p for p in (_here.parents[1] / "scripts" / "super-board-approval.py",
                        _here.parents[2] / "bin" / "super-board-approval.py") if p.exists())
_spec = importlib.util.spec_from_file_location("sb_approval", _bin)
approval = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(approval)

REQUEST_TIME = datetime(2026, 10, 11, 21, 49, 0, tzinfo=timezone.utc)
TRUST = {"john": "admin", "mac": "write", "bystander": "read"}


def perm(login):
    return TRUST.get(login)


def c(login, body, when="21:50:00", ctype="User", edited=False, cid=1):
    created = f"2026-10-11T{when}Z"
    return {"id": cid, "user": {"login": login, "type": ctype},
            "body": body, "created_at": created,
            "updated_at": f"2026-10-11T22:30:00Z" if edited else created}


def test_classify():
    natural = c("john", "hey, also mention the default year please", cid=10)
    edited_natural = c("mac", "and fix the typo in the header", edited=True, cid=11)
    # John's real shape: a preamble line, THEN a `changes:` line. Per-line CHANGES
    # catches it, so it must route to changes_request, NOT classify.
    preamble_changes = c("john", "(proposing one small change):\nchanges: mention --min-year", cid=12)
    comments = [
        c("john", "done", cid=1),                                   # approval -> excluded
        c("john", "changes: rename the flag", cid=2),               # changes line -> excluded
        c("john", "Reason tag: 🙋 needs you\napproval-request: {}", cid=3),  # machine block -> excluded
        c("octobot", "[builder] [report] built", ctype="Bot", cid=4),  # bot -> excluded
        c("bystander", "looks cool, also do X", cid=5),             # untrusted -> excluded
        c("john", "too early", when="21:40:00", cid=6),             # before request -> excluded
        c("john", "   ", cid=7),                                    # empty -> excluded
        preamble_changes,                                           # preamble + changes: -> excluded
        natural,                                                     # trusted free text -> INCLUDED
        edited_natural,                                              # edited trusted -> INCLUDED
    ]
    got = approval.classify_candidates(comments, REQUEST_TIME, perm)
    ids = [g["id"] for g in got]

    assert ids == [10, 11], f"expected [10, 11] (oldest-first), got {ids}"
    assert got[0]["by"] == "john" and "default year" in got[0]["body"]
    assert got[1]["by"] == "mac", "edited trusted comment must still surface (classify is safe)"
    assert not any("approval-request" in g["body"] for g in got)
    assert not any(g["id"] == 12 for g in got), "a preamble+changes: comment must NOT be in classify"

    print("test_classify: PASS (2/2 surfaced, 8 correctly excluded incl. preamble+changes:)")


def test_changes_per_line():
    # The original defect: a `changes:` line with ANY preamble was ignored because
    # the detector matched the whole body. Per-line detection must find it.
    preamble = c("john", "(proposing one small change for you):\n"
                 "changes: also note the --min-year default (2016) in the help",
                 when="21:55:00", cid=20)
    bare = c("mac", "changes: rename the flag to --count", when="21:50:00", cid=21)
    discussion = c("john", "looks great, thanks!", cid=22)   # no changes: -> not a request
    before = c("john", "changes: too early", when="21:40:00", cid=23)  # before request -> ignored
    untrusted = c("bystander", "changes: let me in", cid=24)  # untrusted -> ignored

    got = approval.changes_request([discussion, before, untrusted, bare, preamble],
                                   REQUEST_TIME, perm)
    assert got is not None, "a preamble+changes: comment MUST be detected (the headline fix)"
    assert got["by"] == "john" and got["id"] == 20, f"expected newest (john/20), got {got}"
    assert got["note"].startswith("also note the --min-year"), f"note was: {got['note']!r}"

    # No changes: line anywhere -> no request.
    assert approval.changes_request([discussion], REQUEST_TIME, perm) is None

    print("test_changes_per_line: PASS (preamble+changes: detected; discussion/early/untrusted ignored)")


def test_changes_author_text():
    # F1: deterministic signals come from the author's own text. A pasted log /
    # YAML in a fence, or a `> `-quoted line, must never bounce a card.
    fenced = c("john", "here is the failing log:\n```\nchanges: 3 files changed\n```\nlooks fine otherwise", cid=30)
    tilde = c("john", "~~~yaml\nchanges: oops\n~~~", when="21:51:00", cid=31)
    quoted = c("john", "> changes: rename the flag\nI disagree with the quoted ask above", when="21:52:00", cid=32)
    unclosed = c("john", "```\nchanges: swallowed by the open fence", when="21:53:00", cid=33)
    real = c("john", "```\nchanges: paste\n```\nchanges: actually rename it", when="21:56:00", cid=34)

    assert approval.changes_request([fenced, tilde, quoted, unclosed], REQUEST_TIME, perm) is None, \
        "fenced/quoted/pasted changes: must NOT be a change request"
    got = approval.changes_request([fenced, tilde, quoted, unclosed, real], REQUEST_TIME, perm)
    assert got is not None and got["id"] == 34 and got["note"] == "actually rename it"

    # The paste-only comments are still trusted free text -> classify, not dropped.
    ids = [g["id"] for g in approval.classify_candidates([fenced, tilde, quoted, unclosed], REQUEST_TIME, perm)]
    assert ids == [30, 31, 32, 33], f"paste-only comments must surface in classify, got {ids}"
    print("test_changes_author_text: PASS (fence/tilde/quote/unclosed never bounce; author-text changes: does)")


def test_report_filter_and_ack():
    # F3: the board's own [role] [label] comments post via the human PAT (= trusted
    # User) and must never read as human signals; acked ids stop re-surfacing.
    report = c("john", "[orchestrator] [report] 🔁 changes requested by john\n"
               "> changes: quoted verbatim\nNext: builder", cid=40)
    assert approval.changes_request([report], REQUEST_TIME, perm) is None, \
        "a machine report must never be a change request"
    assert approval.classify_candidates([report], REQUEST_TIME, perm) == [], \
        "a machine report must never be a classify candidate"

    natural = c("john", "hey, also mention the default year", cid=41)
    ack = c("john", "[orchestrator] [report] replied\nclassified: 41", when="21:57:00", cid=42)
    bad_ack = c("bystander", "[orchestrator] [report] replied\nclassified: 41", when="21:58:00", cid=43)
    fenced_ack = c("john", "[orchestrator] [report] log\n```\nclassified: 41\n```", when="21:59:00", cid=44)
    assert [g["id"] for g in approval.classify_candidates([natural], REQUEST_TIME, perm)] == [41]
    assert approval.classify_candidates([natural, ack], REQUEST_TIME, perm) == [], \
        "a trusted classified: ack must suppress the id"
    assert [g["id"] for g in approval.classify_candidates([natural, bad_ack], REQUEST_TIME, perm)] == [41], \
        "an untrusted ack must not suppress"
    assert [g["id"] for g in approval.classify_candidates([natural, fenced_ack], REQUEST_TIME, perm)] == [41], \
        "a fenced classified: line is a paste, not an ack"
    print("test_report_filter_and_ack: PASS (reports inert both paths; ack dedup trusted-only, author-text-only)")


def test_edited_changes_surfaces():
    # F6: an EDITED changes: comment is barred from the deterministic route by the
    # tamper guard — it must surface in classify flagged edited, never vanish.
    edited = c("john", "changes: rename the flag after all", edited=True, cid=50)
    assert approval.changes_request([edited], REQUEST_TIME, perm) is None, "tamper guard must hold"
    got = approval.classify_candidates([edited], REQUEST_TIME, perm)
    assert [g["id"] for g in got] == [50] and got[0]["edited"] is True, \
        f"edited changes: must surface in classify flagged edited, got {got}"
    fresh = c("john", "changes: rename the flag after all", cid=51)
    assert approval.classify_candidates([fresh], REQUEST_TIME, perm) == [], \
        "an unedited changes: belongs to the deterministic route only"
    print("test_edited_changes_surfaces: PASS (edited changes: -> classify flagged; unedited stays deterministic)")


REPO = "underdognoisy/pubmed-trusted-sources-demo"
HEAD = "a" * 40
SCOPE = "b" * 64


def gate_comment(tag="🙋 needs you", when="21:49:00", cid=100, login="john"):
    """The merge gate's block comment: reason tag + pinned request + blocked-by: -."""
    body = (f"[reviewer] [blocker] 🙋 Your turn on #6\n"
            f"Reason tag: {tag} · Owner: john\n"
            + approval.marker(approval.request_for(REPO, 5, HEAD, SCOPE))
            + "\nblocked-by: -")
    return c(login, body, when=when, cid=cid)


class StubGitHub:
    repo = REPO

    def __init__(self, comments, approved=False):
        self._comments, self._approved = comments, approved

    def comments(self, number):
        return list(self._comments)

    def permission(self, login):
        return perm(login)

    def check(self, pr, head=None, issue=None):
        return {"approved": self._approved, "why": "stub"}


def entry6():
    return {"number": 6, "needsYou": True, "humanGated": True,
            "parseable": True, "openBlockers": []}


def test_human_gate_clear():
    # F2: a trusted changes: clears humanGated ONLY when the gate-setting comment
    # (newest blocked-by: line) is the 🙋 approval hold. humanGated had no test before.
    gate = gate_comment()
    change = c("john", "changes: also note the --min-year default", when="21:55:00", cid=101)

    entry = entry6()
    approval.enrich_entry(entry, StubGitHub([gate, change]))
    assert entry["changesRequested"] is not None
    assert entry["humanGated"] is False and entry["runnable"] is True, \
        "a verified changes: must free the 🙋 approval hold"

    # A newer 🔐 credentials block owns the gate -> the clear must NOT fire.
    cred = c("john", "[builder] [blocker] 🛑 blocked · 🔐 missing API key\n"
             "Reason tag: 🔐 credentials\nblocked-by: -", when="21:52:00", cid=102)
    entry = entry6()
    approval.enrich_entry(entry, StubGitHub([gate, cred, change]))
    assert entry["changesRequested"] is not None, "the changes: request itself still registers"
    assert entry["humanGated"] is True and "runnable" not in entry, \
        "a 🔐 gate must never be freed by a comment — only a person moves the card"

    # Gate set by an UNTRUSTED comment claiming the 🙋 tag -> no clear either.
    fake = c("bystander", "Reason tag: 🙋 needs you\nblocked-by: -", when="21:53:00", cid=103)
    entry = entry6()
    approval.enrich_entry(entry, StubGitHub([gate, fake, change]))
    assert entry["humanGated"] is True, "an untrusted gate-source must not enable the clear"
    print("test_human_gate_clear: PASS (🙋 hold clears; 🔐 and untrusted gate-sources never do)")


def test_classify_holds_resume():
    # F5: a standing unclassified human comment supersedes a bare done — resume
    # holds until the orchestrator classifies AND acks (classified: <id>).
    gate = gate_comment()
    chat = c("john", "can you also log the counts?", when="21:53:00", cid=110)

    entry = entry6()
    approval.enrich_entry(entry, StubGitHub([gate, chat], approved=True))
    assert entry["needsYouDone"] is False and entry["classify"], \
        "resume must hold while a classify candidate is unacked"
    assert "classify" in entry["approvalWhy"]

    ack = c("john", "[orchestrator] [report] replied\nclassified: 110", when="21:57:00", cid=111)
    entry = entry6()
    approval.enrich_entry(entry, StubGitHub([gate, chat, ack], approved=True))
    assert entry["classify"] == [] and entry["needsYouDone"] is True, \
        "after the ack, the verified approval must register again"
    print("test_classify_holds_resume: PASS (unacked comment holds resume; ack releases it)")


def test_approval_vocab():
    # Explicit approvals that MUST authorize (whole-body, any case, trailing punct).
    for body in ["done", "Done.", "approve", "approved", "merge", "merge it", "Merge."]:
        assert approval.DONE.fullmatch(body), f"should approve: {body!r}"
    # Must NOT authorize — negations, conditionals, history, a word in a sentence,
    # or the casual tokens narrowed out on purpose (owner ruling 2026-10-12):
    # lgtm/ship route to classify, where approve-adjacent means hold-and-ask.
    for body in ["don't merge", "do not merge", "merge after tests pass",
                 "almost done", "not done yet", "merged already", "I merged it",
                 "looks good, merge when green", "please approve the budget too",
                 "lgtm", "LGTM!", "ship", "ship it"]:
        assert not approval.DONE.fullmatch(body), f"should NOT approve: {body!r}"
    # The narrowed-out tokens surface for classification instead of vanishing.
    lgtm = c("john", "lgtm", when="21:55:00", cid=60)
    got = approval.classify_candidates([lgtm], REQUEST_TIME, perm)
    assert [g["id"] for g in got] == [60], "a bare lgtm must land in classify (hold-and-ask)"
    print("test_approval_vocab: PASS (7 approve tokens; 13 non-approvals rejected; lgtm -> classify)")


def test_stale_done_notice():
    # F2 (2026-10-12): a trusted `done` posted BEFORE the pinned request never
    # approves, and the human is told once instead of being silently ignored.
    early = c("john", "done", when="21:48:00", cid=70)
    gate = gate_comment()

    entry = entry6()
    approval.enrich_entry(entry, StubGitHub([early, gate], approved=False))
    assert entry["needsYouDone"] is False, "a pre-request done must NEVER approve"
    assert entry["staleDone"] == {"by": "john", "id": 70, "at": "2026-10-11T21:48:00Z"}, \
        f"the early done must be reported for a notice, got {entry['staleDone']}"

    # The pure verdict agrees: validate() ignores the early token outright.
    def validate_with(comments):
        expected = {"repo": REPO, "pr": 5, "head": HEAD, "scope": SCOPE}
        return approval.validate([dict(x, source=6) for x in comments], expected, perm)
    assert validate_with([early, gate])["approved"] is False
    late = c("john", "done", when="21:51:00", cid=72)
    assert validate_with([early, gate, late])["approved"] is True, "a done AFTER the request still approves"

    # Once a trusted notice names the id, it is not reported again.
    notice = c("john", "[reviewer] [report] ⚠️ your `done` at 21:48 came before this request — "
               "comment `done` again to approve\nstale-done: 70", when="21:50:00", cid=71)
    entry = entry6()
    approval.enrich_entry(entry, StubGitHub([early, gate, notice]))
    assert entry["staleDone"] is None, "the notice posts once"
    # The notice is a machine report: never a classify candidate, never a block.
    assert approval.classify_candidates([notice], REQUEST_TIME, perm) == []
    assert not approval.BLOCK.search(notice["body"]), "a notice must never supersede the request"

    # An untrusted ack does not suppress; a bot or untrusted early done is not reported.
    fake = c("bystander", "stale-done: 70", when="21:50:00", cid=73)
    entry = entry6()
    approval.enrich_entry(entry, StubGitHub([early, gate, fake]))
    assert entry["staleDone"] and entry["staleDone"]["id"] == 70
    for who, kind in (("bystander", "User"), ("octobot", "Bot")):
        entry = entry6()
        approval.enrich_entry(entry, StubGitHub([c(who, "done", when="21:48:00", ctype=kind, cid=74), gate]))
        assert entry["staleDone"] is None, f"{who}/{kind} early done is not a human approval attempt"

    # A post-request token exists → validate owns it, no stale notice.
    entry = entry6()
    approval.enrich_entry(entry, StubGitHub([early, gate, late], approved=True))
    assert entry["staleDone"] is None and entry["needsYouDone"] is True
    print("test_stale_done_notice: PASS (early done never approves; noticed once; trusted-only)")


def test_classified_bounce_frees_gate():
    # 2026-10-15 #67: the orchestrator bounced on a classified free-text note
    # ("recheck this testing result"), but the dead 🙋 request kept the card
    # humanGated in Ready, so no wave ever picked it. The 🔁 report acking a
    # trusted human comment must free the gate, exactly like `changes:`.
    gate = gate_comment()
    note = c("john", "I need you to recheck this testing result", when="21:54:00", cid=110)
    bounce = c("john", "[orchestrator] [report] 🔁 changes requested by john\n"
               "> I need you to recheck this testing result\nNext: builder\n\nclassified: 110",
               when="21:56:00", cid=111)
    entry = entry6()
    approval.enrich_entry(entry, StubGitHub([gate, note, bounce], approved=True))
    assert entry["humanGated"] is False and entry["runnable"] is True, \
        f"a classified bounce must free the 🙋 hold, got {entry}"
    assert entry["needsYouDone"] is False, "a bounced request never resumes toward a merge"

    # A discussion ack (no 🔁 report) does NOT kill the request.
    reply = c("john", "[orchestrator] [report] replied\nclassified: 110", when="21:56:00", cid=112)
    entry = entry6()
    approval.enrich_entry(entry, StubGitHub([gate, note, reply]))
    assert entry["humanGated"] is True, "a discussion ack must leave the gate standing"

    # 2026-10-15 #70: the REVIEWER sent an approved head back for a rebase. Every
    # bounce header seen on the board frees the hold the same way.
    for cid, header in ((116, "[reviewer] [report] 🔁 rebase pass · round 3 · PR #87"),
                        (117, "[reviewer] [report] ❌ bounced · round 2 · PR #87"),
                        (118, "[reviewer] [report] ↩️ moved back · round 2 · PR #87")):
        lane = c("john", header + "\nNext: builder", when="21:56:00", cid=cid)
        entry = entry6()
        approval.enrich_entry(entry, StubGitHub([gate, lane], approved=True))
        assert entry["humanGated"] is False and entry["needsYouDone"] is False, \
            f"{header!r} must kill the request and free the 🙋 hold, got {entry}"

    # Ordinary reports after the request are not bounces.
    for cid, header in ((119, "[orchestrator] [report] ▶️ approval verified · back to Review"),
                        (120, "[reviewer] [report] ✅ merged · round 3 · PR #87")):
        entry = entry6()
        approval.enrich_entry(entry, StubGitHub([gate, c("john", header, when="21:56:00", cid=cid)]))
        assert entry["humanGated"] is True, f"{header!r} must leave the gate standing"

    # "bounced" deep in a body (quoted, not the header) is not a bounce report.
    quoted = c("john", "[reviewer] [report] ✅ merged · round 4\nprior round bounced on R1",
               when="21:56:00", cid=121)
    entry = entry6()
    approval.enrich_entry(entry, StubGitHub([gate, quoted]))
    assert entry["humanGated"] is True, "only the header line decides a bounce"

    # A 🔁 report from before the current request is history, not a bounce.
    old = c("john", "[orchestrator] [report] 🔁 changes requested by john\nclassified: 110",
            when="21:40:00", cid=113)
    entry = entry6()
    approval.enrich_entry(entry, StubGitHub([gate, note, old]))
    assert entry["humanGated"] is True, "a pre-request bounce report must not clear the gate"

    # An untrusted 🔁 report cannot free the gate.
    fake = c("bystander", "[orchestrator] [report] 🔁 changes requested\nclassified: 110",
             when="21:56:00", cid=114)
    entry = entry6()
    approval.enrich_entry(entry, StubGitHub([gate, note, fake]))
    assert entry["humanGated"] is True, "an untrusted bounce report must not clear the gate"

    # A non-🙋 gate (design ruling) is never cleared by a classified bounce.
    ruling = c("john", "[builder] [blocker] 🛑 blocked · 🧑 product call\n"
               "Reason tag: 🧑 product ruling\nblocked-by: -", when="21:52:00", cid=115)
    entry = entry6()
    approval.enrich_entry(entry, StubGitHub([gate, ruling, note, bounce]))
    assert entry["humanGated"] is True, "a 🧑 gate stays until a person moves it"
    print("test_classified_bounce_frees_gate: PASS (orchestrator + reviewer bounces free 🙋 hold; reply/merged/old/untrusted/🧑 do not)")


def test_unlinked_request_is_unverifiable():
    # 2026-10-16 #66: a status rewrite dropped `Closes #66`, check() refused
    # every `done` as "not linked", and the board kept asking for another.
    # The real check() must mark the entry unverifiable so the planner reports
    # the cause instead of re-asking; a linked PR approves the same `done`.
    done = c("john", "done", when="21:55:00", cid=120)

    class Unlinked(approval.GitHub):
        def __init__(self):
            self.repo, self.project = REPO, None

        def pr(self, number):
            return HEAD, set()

        def comments(self, number):
            return [dict(x, source=number) for x in (gate_comment(), done)] if number == 6 else []

        def permission(self, login):
            return perm(login)

    entry = entry6()
    approval.enrich_entry(entry, Unlinked())
    assert entry["needsYouDone"] is False, "an unlinked request never approves"
    assert entry["unverifiable"] == "request issue is not linked to this PR", entry
    assert entry["staleDone"] is None

    class Linked(Unlinked):
        def pr(self, number):
            return HEAD, {6}

    entry = entry6()
    approval.enrich_entry(entry, Linked())
    assert entry["unverifiable"] is None and entry["needsYouDone"] is True, entry
    print("test_unlinked_request_is_unverifiable: PASS (unlinked → unverifiable; linked done approves)")


if __name__ == "__main__":
    test_classify()
    test_changes_per_line()
    test_changes_author_text()
    test_report_filter_and_ack()
    test_edited_changes_surfaces()
    test_human_gate_clear()
    test_classify_holds_resume()
    test_approval_vocab()
    test_stale_done_notice()
    test_classified_bounce_frees_gate()
    test_unlinked_request_is_unverifiable()

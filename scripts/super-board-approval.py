#!/usr/bin/env python3
"""Read-only, head-bound human approval shared by the planner and merge gate.

A trusted request pins the code and human steps BEFORE a human replies `done` —
or, since v3.3.0, drags the card from Approve to Done on the board. Labels only
describe UI state. They never authorize a merge. No GitHub writes.

DRAG TO DONE (v3.3.0). The planner reads the card's live board evidence: the
Status field value is `Done`, the issue and PR are still OPEN, and the Status
value changed AFTER the pinned request (its `updatedAt`; or, when GitHub exposes
them, the newest `ProjectV2ItemStatusChangedEvent`, which also names the actor
and whether a project workflow made the move). The same validate() rules apply as
for a `done` comment: the newest trusted request, unedited, covering the current
head. The orchestrator then moves the card Done → Review and posts the planner's
deterministic `drag-approval: {…}` machine line, so the merge gate — which runs
after the card has left Done — can re-verify the approval bound to the same
request id, head and scope. That line is an attestation of a board read the
planner made, not a human act: see RELEASE-NOTES v3.3.0 for the trust caveat.
"""
from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path
from datetime import datetime
import hashlib
import json
import re
import subprocess
import sys

_reader_spec = importlib.util.spec_from_file_location("github_read", Path(__file__).with_name("super-board-github-read.py"))
github_read = importlib.util.module_from_spec(_reader_spec)
_reader_spec.loader.exec_module(github_read)

PREFIX = "approval-request: "
SHA = re.compile(r"[0-9a-f]{40}\Z")
SCOPE = re.compile(r"[0-9a-f]{64}\Z")
BLOCK = re.compile(r"Reason tag:[^\n]*🙋|^approval-request:", re.M)
# The explicit APPROVAL tokens: done | approve(d) | merge (it). Each is an
# unambiguous act, matched WHOLE-BODY (fullmatch) by trusted callers: "don't
# merge", "merge after tests", "almost done", "merged already" all have extra
# words and do NOT match. Casual thumbs-up tokens (lgtm, ship) are NOT approval
# on purpose (owner ruling 2026-10-12): a bare "lgtm" plausibly rates the work
# without intending the head-bound approval act. They land in classify, where
# the orchestrator holds and asks — approve-adjacent is unsure, never a merge.
DONE = re.compile(r"\s*(?:done|approved?|merge(?:\s+it)?)[.!]*\s*\Z", re.I)
# `changes:` on ANY line of a trusted comment — humans write comments, not tokens,
# so a preamble before the `changes:` line must not void the request. Still an
# explicit, deterministic token (not inference); the trust check lives in the
# callers. The merge signal (DONE) stays whole-body strict — missing it only
# stalls (safe); matching `changes:` loosely at most bounces (also safe).
CHANGES = re.compile(r"^[ \t]*changes[ \t]*:[ \t]*(\S.*?)[ \t]*$", re.I | re.M)
# Machine-format board comments (`[role] [report]`, `[role] [blocker]`) are the
# loop talking to itself. They post via the runner's PAT, so GitHub shows them as
# a trusted User — author identity cannot separate them. They are never a human
# signal: not a change request, not a classify candidate.
MACHINE = re.compile(r"\s*\[[A-Za-z][\w-]*\]\s*\[[A-Za-z][\w-]*\]")
# Orchestrator ack: `classified: 12, 14` machine line in a trusted comment names
# classify-candidate comment ids already handled, so they stop re-surfacing
# every wave. Suppression can only skip a re-handle, never cause a merge.
ACK = re.compile(r"^[ \t]*classified[ \t]*:[ \t]*([0-9]+(?:[ \t]*,[ \t]*[0-9]+)*)[ \t]*$", re.I | re.M)
# Orchestrator notice: `stale-done: 12` machine line in a trusted comment names a
# pre-request `done` the human was already told about, so the notice posts once.
# Like the ack, it can only suppress a repeat notice — never approve anything.
STALE_ACK = re.compile(r"^[ \t]*stale-done[ \t]*:[ \t]*([0-9]+(?:[ \t]*,[ \t]*[0-9]+)*)[ \t]*$", re.I | re.M)
# Drag record: the orchestrator's machine line after a VERIFIED drag to Done
# (planner `resume[].record`, built by drag_record()). It binds the request id,
# head and scope, so the merge gate can re-verify after the card left Done.
DRAG_LINE = re.compile(r"^[ \t]*drag-approval:[ \t]*(\{.*\})[ \t]*$", re.M)
DRAG_PREFIX = "drag-approval: "
# The newest `blocked-by:` line decides the human gate (same line
# super-board-deps.sh reads); its own comment says WHY the gate exists.
GATE_LINE = re.compile(r"^[ \t]*blocked-by[ \t]*:[ \t]*(.*?)[ \t]*$", re.I | re.M)
NEEDS_TAG = re.compile(r"Reason tag:[^\n]*🙋")
FENCE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")
QUOTED = re.compile(r"^[ \t]{0,3}>")
# GitHub populates closingIssuesReferences only for PRs into the default branch.
# Boards whose base_branch is staging (the recommended setup) get an empty list, so
# the card's own `Closes #N` line must link the issue too. Same-repo refs only; this
# widens which comment threads are SCANNED, never who may authorize (validate() still
# requires a trusted collaborator's pinned request matching this PR and head).
CLOSES = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s+(?:([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+))?#(\d+)", re.I)


def author_text(body):
    """The comment's own prose: fenced code blocks and `>`-quoted lines removed.

    A deterministic signal (`changes:` on a line) must come from what the author
    SAID, not from a log/YAML they pasted in a fence or a line they quoted —
    a pasted `changes: 3 files` must never bounce a card. An unclosed fence
    drops the rest of the body: conservative, the human can always re-comment.
    """
    out, fence = [], None
    for line in body.splitlines():
        stripped = line.strip()
        if fence is not None:
            if stripped.startswith(fence) and not stripped.strip(fence[0]):
                fence = None
            continue
        if FENCE.match(line):
            fence = stripped[0] * 3
            continue
        if QUOTED.match(line):
            continue
        out.append(line)
    return "\n".join(out)


def scope_for(plan):
    # Exclude UI-only `done`; a label change must not change what was approved.
    relevant = {key: plan.get(key, []) for key in ("human", "migrations", "run", "needs_you")}
    return hashlib.sha256(json.dumps(relevant, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def request_for(repo, pr, head, scope):
    return {"repo": repo.lower(), "pr": int(pr), "head": head, "scope": scope}


def marker(request):
    return PREFIX + json.dumps(request, sort_keys=True, separators=(",", ":"))


def parse_request(comment):
    lines = [line for line in (comment.get("body") or "").splitlines() if line.startswith(PREFIX)]
    if len(lines) != 1:
        return None
    try:
        value = json.loads(lines[0][len(PREFIX):])
        if (not isinstance(value, dict) or not isinstance(value.get("repo"), str)
                or not re.fullmatch(r"[^/\s]+/[^/\s]+", value["repo"])
                or type(value.get("pr")) is not int or value["pr"] < 1
                or not SHA.fullmatch(value.get("head", ""))
                or not SCOPE.fullmatch(value.get("scope", ""))):
            return None
        return value
    except (ValueError, TypeError):
        return None


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError("missing comment timestamp")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("comment timestamp lacks timezone")
    return result


def changes_request(comments, request_time, permission):
    """The newest trusted, unedited `changes: …` comment after the pinned request.

    A standing change request supersedes a `done` for the rest of that request
    cycle: approval after `changes:` needs the bounce to land and a FRESH pinned
    request on the new head. Conservative on purpose — the board must never
    merge against an open human change request.
    """
    newest = None
    for comment in comments:
        body = comment.get("body") or ""
        if MACHINE.match(body):
            continue
        match = CHANGES.search(author_text(body))
        if not match or not comment.get("id"):
            continue
        created = timestamp(comment.get("created_at"))
        if created <= request_time or timestamp(comment.get("updated_at")) != created:
            continue
        author = comment.get("user") or {}
        if (author.get("type") == "User" and author.get("login")
                and permission(author["login"]) in ("write", "maintain", "admin")):
            if newest is None or created > newest["time"]:
                newest = {"time": created, "by": author["login"],
                          "note": match.group(1).strip()[:2000], "id": comment.get("id")}
    return newest


def classify_candidates(comments, request_time, permission):
    """Trusted HUMAN comments after the pinned request that carry intent the
    deterministic signals do not: not a bare `done` (that is the explicit, SHA-
    bound approval), not an explicit `changes:` line (that is the regex fast
    path), not a machine block (approval request / reviewer blocker).

    These are the free-text comments the orchestrator must CLASSIFY into a small,
    SAFE action set — changes / spinoff / discussion / hold-and-ask — so a human
    need not write a perfect token. Classification can only ever bounce, spin off
    a ticket, or pause; it can NEVER cause a merge, so (unlike approval) an EDITED
    comment is allowed here: the tamper guard protects the merge path, not this.
    An EDITED `changes:` comment surfaces here flagged `edited` (the tamper guard
    keeps it out of the deterministic route; silently dropping it would be
    invisible). Machine-format `[role] [label]` comments are the board's own
    output and never surface — otherwise every wave would re-classify its own
    reports forever. Ids acked by a trusted `classified:` line are done, skipped.
    Oldest-first, so the orchestrator reads them in the order the human wrote them.
    """
    acked = acked_ids(comments, permission)
    out = []
    for comment in comments:
        body = comment.get("body") or ""
        if not comment.get("id") or not body.strip() or comment.get("id") in acked:
            continue
        if BLOCK.search(body) or DONE.fullmatch(body) or MACHINE.match(body):
            continue
        try:
            created = timestamp(comment.get("created_at"))
            edited = timestamp(comment.get("updated_at")) != created
        except (ValueError, TypeError):
            continue
        if created <= request_time:
            continue
        if CHANGES.search(author_text(body)) and not edited:
            continue  # the unedited changes: fast path owns it (changes_request)
        author = comment.get("user") or {}
        if (author.get("type") == "User" and author.get("login")
                and permission(author["login"]) in ("write", "maintain", "admin")):
            out.append({"by": author["login"], "id": comment.get("id"),
                        "at": comment.get("created_at"), "edited": edited,
                        "body": body.strip()[:2000]})
    out.sort(key=lambda c: c["at"])
    return out


def acked_ids(comments, permission):
    """Classify-candidate ids a trusted comment has acked via `classified: …`.

    The orchestrator ends each handling report with that machine line; without
    it the same comment re-surfaces every wave (held ON PURPOSE for `unsure`).
    Bot authors are accepted — reports post via the runner identity — because
    suppression can only ever skip a re-handle or hold, never authorize a merge.
    """
    out = set()
    for comment in comments:
        hits = ACK.findall(author_text(comment.get("body") or ""))
        if not hits:
            continue
        author = comment.get("user") or {}
        if (author.get("type") in ("User", "Bot") and author.get("login")
                and permission(author["login"]) in ("write", "maintain", "admin")):
            for hit in hits:
                out.update(int(x) for x in re.split(r"[ \t]*,[ \t]*", hit))
    return out


def stale_done(comments, request_time, permission):
    """The newest trusted human `done|approve|merge` that PREDATES the pinned request.

    It never counts as approval — validate() only accepts a token AFTER the
    request — but a human who replied early must be told so, once, instead of
    being silently ignored (2026-10-12: a `done` 46 s before the request sat
    unnoticed). Returns None when there is nothing to tell: no early token, a
    token after the request already exists (validate owns that), or a trusted
    `stale-done: <id>` notice already named this one. Read-only; it only ever
    produces a notice, so it can never cause a merge.
    """
    noticed = set()
    newest = None
    for comment in comments:
        body = comment.get("body") or ""
        author = comment.get("user") or {}
        trusted = bool(author.get("login")) and permission(author["login"]) in ("write", "maintain", "admin")
        hits = STALE_ACK.findall(author_text(body))
        if hits and trusted and author.get("type") in ("User", "Bot"):
            for hit in hits:
                noticed.update(int(x) for x in re.split(r"[ \t]*,[ \t]*", hit))
        if not comment.get("id") or not DONE.fullmatch(body):
            continue
        if not (trusted and author.get("type") == "User"):
            continue
        try:
            created = timestamp(comment.get("created_at"))
        except (ValueError, TypeError):
            continue
        if created > request_time:
            return None  # a post-request token exists; validate() decides it
        if newest is None or created > newest["time"]:
            newest = {"time": created, "by": author["login"], "id": comment.get("id"),
                      "at": comment.get("created_at")}
    if newest is None or newest["id"] in noticed:
        return None
    return {"by": newest["by"], "id": newest["id"], "at": newest["at"]}


def approval_hold(comments, trusted):
    """True only when the comment that SET the human gate — the newest
    `blocked-by:` line, the same line super-board-deps.sh reads — is the merge
    gate's 🙋 approval hold: a TRUSTED block comment carrying the 🙋 reason tag
    with `blocked-by: -`. Credentials / permissions / billing / design /
    product-ruling holds (🔐 🔑 💳 🎨 🧑) never match: those still wait for a
    person to move the card, whatever else gets commented.
    """
    source = None
    for comment in comments:
        if GATE_LINE.search(comment.get("body") or ""):
            source = comment
    if source is None:
        return False
    body = source.get("body") or ""
    value = re.sub(r"[ \t]", "", GATE_LINE.findall(body)[-1])
    return (value in ("-", "") and bool(NEEDS_TAG.search(body))
            and any(c.get("id") == source.get("id") for c in trusted))


def trusted_blocks(comments, permission):
    result = []
    for block in comments:
        if not BLOCK.search(block.get("body") or ""):
            continue
        user = block.get("user") or {}
        if not user.get("login") or user.get("type") not in ("User", "Bot"):
            raise ValueError("requester identity could not be verified")
        if permission(user["login"]) in ("write", "maintain", "admin"):
            result.append(block)
    return result


def drag_record(request, request_id, at, by):
    """The machine line the orchestrator posts after a verified drag to Done."""
    value = dict(request, request=request_id, at=at, by=by)
    return DRAG_PREFIX + json.dumps(value, sort_keys=True, separators=(",", ":"))


def parse_drag_record(body):
    hits = DRAG_LINE.findall(author_text(body or ""))
    if len(hits) != 1:
        return None
    try:
        value = json.loads(hits[0])
    except ValueError:
        return None
    if (not isinstance(value, dict) or type(value.get("request")) is not int
            or not isinstance(value.get("at"), str)
            or (value.get("by") is not None and not isinstance(value.get("by"), str))):
        return None
    return value


def validate(comments, expected, permission, drag=None):
    """Pure verdict; permission(login) is a current repository permission lookup.

    Approval is ONE of three acts after the newest trusted, unedited request
    that covers the current head: a trusted human `done` comment (via
    "comment"), live board evidence that the card was dragged to Done after the
    request (`drag`, read by the planner — via "drag"), or the orchestrator's
    `drag-approval:` record of that verified drag, bound to this request id,
    head and scope (via "drag" — what the merge gate sees once the card has
    moved Done → Review). Classification and free text never approve.
    """
    def hold(why):
        return {"approved": False, "why": why}

    if not SHA.fullmatch(expected.get("head", "")):
        return hold("current PR head is unavailable")
    try:
        blocks = trusted_blocks(comments, permission)
        if not blocks:
            return hold("no request from a trusted repository collaborator")
        # Any later human block supersedes the old request, including one with
        # missing/malformed metadata. Equal-second requests are ambiguous: hold.
        latest_time = max(timestamp(c.get("created_at")) for c in blocks)
        latest = [c for c in blocks if timestamp(c.get("created_at")) == latest_time]
        if len(latest) != 1:
            return hold("ambiguous newest human request")
        request_comment = latest[0]
        request = parse_request(request_comment)
        if request is None or any(request.get(k) != v for k, v in expected.items()):
            return hold("the latest request does not cover the current code and human steps")
        if timestamp(request_comment.get("updated_at")) != latest_time:
            return hold("approval request was edited; post a fresh request")
        requester = request_comment.get("user") or {}
        if not requester.get("login") or permission(requester["login"]) not in ("write", "maintain", "admin"):
            return hold("requester authority could not be verified")
        if not isinstance(request_comment.get("source"), int) or not request_comment.get("id"):
            return hold("request identity is missing")
        for comment in comments:
            if (not comment.get("id") or not DONE.fullmatch(comment.get("body") or "")
                    or comment.get("source") != request_comment.get("source")):
                continue
            created = timestamp(comment.get("created_at"))
            if created <= latest_time or timestamp(comment.get("updated_at")) != created:
                continue
            author = comment.get("user") or {}
            if (author.get("type") == "User" and author.get("login")
                    and permission(author["login"]) in ("write", "maintain", "admin")):
                return {"approved": True, "via": "comment",
                        "why": "trusted human confirmed the current request",
                        "request": request, "requestId": request_comment.get("id"),
                        "approvalId": comment.get("id"), "approver": author["login"]}
        if drag is not None:
            # Live board evidence (planner only). A refusal reason from the
            # board read is reported as-is; it never falls through to approval.
            if drag.get("refused"):
                return hold(drag["refused"])
            if drag.get("source") != request_comment.get("source"):
                return hold("the dragged card is not the card that carries the request")
            moved = timestamp(drag.get("at"))
            if moved <= latest_time:
                return hold("the card reached Done before the current request; drag it again to approve")
            if drag.get("by") is not None and permission(drag["by"]) not in ("write", "maintain", "admin"):
                return hold("the move to Done was not made by a trusted collaborator")
            return {"approved": True, "via": "drag",
                    "why": "trusted request; card dragged to Done after it",
                    "request": request, "requestId": request_comment.get("id"),
                    "dragAt": drag["at"], "dragBy": drag.get("by"),
                    "record": drag_record(request, request_comment.get("id"), drag["at"], drag.get("by"))}
        for comment in comments:
            record = parse_drag_record(comment.get("body"))
            if (record is None or not comment.get("id")
                    or comment.get("source") != request_comment.get("source")):
                continue
            created = timestamp(comment.get("created_at"))
            if created <= latest_time or timestamp(comment.get("updated_at")) != created:
                continue
            if record.get("request") != request_comment.get("id"):
                continue
            if any(record.get(k) != request.get(k) for k in ("repo", "pr", "head", "scope")):
                continue
            moved = timestamp(record["at"])
            if moved <= latest_time or moved > created:
                continue
            author = comment.get("user") or {}
            # The record is posted by the run identity (a User PAT or a Bot app).
            if (author.get("type") in ("User", "Bot") and author.get("login")
                    and permission(author["login"]) in ("write", "maintain", "admin")):
                return {"approved": True, "via": "drag",
                        "why": "verified drag to Done recorded for the current request",
                        "request": request, "requestId": request_comment.get("id"),
                        "approvalId": comment.get("id"), "dragAt": record["at"],
                        "dragBy": record.get("by")}
        return hold("waiting for a trusted human to comment done on the current request")
    except (ValueError, TypeError, KeyError):
        return hold("approval evidence is incomplete or unreadable")


def enrich_entry(entry, github):
    """Enrich one dependency-graph entry with the verified human signals."""
    entry["needsYouDone"] = False
    entry["approvalRefresh"] = False
    entry["changesRequested"] = None
    entry["classify"] = []
    entry["staleDone"] = None
    entry["approvedVia"] = None
    entry["drag"] = None
    if not entry.get("needsYou") or github is None:
        return
    try:
        comments = github.comments(entry["number"])
        blocks = trusted_blocks(comments, github.permission)
        latest = max(blocks, key=lambda c: timestamp(c.get("created_at"))) if blocks else {}
        request = parse_request(latest)
        if request is None or request["repo"] != github.repo:
            return
        try:
            # The drag route needs a board (`--project`); without one, check() is
            # called exactly as before v3.3.0.
            extra = {"drag": True} if getattr(github, "project", None) else {}
            result = github.check(request["pr"], head=request["head"], issue=entry["number"], **extra)
        except PRNotOpen as closed:
            # A merged or closed PR is never an approval — and a Done card whose
            # PR merged is not a drag waiting to be refused (prState says so).
            entry["prState"] = closed.state
            entry["approvalWhy"] = f"PR is {closed.state.lower()}"
            return
        entry["prState"] = "OPEN"
        entry["needsYouDone"] = result["approved"]
        if result["approved"]:
            entry["approvedVia"] = result.get("via", "comment")
            if entry["approvedVia"] == "drag":
                entry["drag"] = {"at": result.get("dragAt"), "by": result.get("dragBy"),
                                 "record": result.get("record")}
        entry["approvalRefresh"] = result.get("headChanged", False)
        entry["approvalWhy"] = result["why"]
        request_time = timestamp(latest.get("created_at"))
        change = changes_request(comments, request_time, github.permission)
        if change is not None:
            entry["needsYouDone"] = False
            entry["changesRequested"] = {"by": change["by"], "note": change["note"]}
            entry["approvalWhy"] = "human requested changes; bounce, rebuild, fresh request"
            # A VERIFIED trusted human asked for changes. That frees the human
            # gate ONLY when the gate itself is the merge gate's 🙋 approval
            # hold (approval_hold keys on the gate-setting comment's reason
            # tag): the person the hold waited for has moved it, so the bounced
            # card is runnable for the rebuild. A 🔐/🔑/💳/🎨/🧑 gate — the
            # deps.sh "only a person moves it" contract — is never cleared
            # here. Trust-bound, never label-driven; open blockers still gate.
            if entry.get("humanGated") and approval_hold(comments, blocks):
                entry["humanGated"] = False
                entry["runnable"] = (bool(entry.get("parseable"))
                                     and len(entry.get("openBlockers") or []) == 0)
        # Free-text human comments after the request the orchestrator must
        # classify (changes / spinoff / discussion / hold). Never a merge.
        entry["classify"] = classify_candidates(comments, request_time, github.permission)
        if not entry["needsYouDone"] and change is None:
            # A `done` the human wrote BEFORE this request: never approval, but
            # tell them once (planner → `staleDone`, orchestrator posts notice).
            entry["staleDone"] = stale_done(comments, request_time, github.permission)
        if not entry["needsYouDone"]:
            entry["approvedVia"] = None
            entry["drag"] = None
        if entry["classify"] and entry["needsYouDone"]:
            # A standing unclassified human comment supersedes a bare `done`:
            # resume holds until the orchestrator classifies and acks it
            # (`classified: <ids>`). Holding only ever stalls — never merges.
            entry["needsYouDone"] = False
            entry["approvedVia"] = None
            entry["drag"] = None
            entry["approvalWhy"] = "unclassified human comments stand; classify and ack them first"
    except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError):
        entry["approvalWhy"] = "approval evidence could not be verified"


class PRNotOpen(ValueError):
    def __init__(self, state):
        super().__init__("PR is not open")
        self.state = state


BOARD_QUERY = '''query($owner:String!,$repo:String!,$issue:Int!){
  repository(owner:$owner,name:$repo){issue(number:$issue){state
    projectItems(first:50){nodes{project{number owner{__typename ... on User{login} ... on Organization{login}}}
      fieldValueByName(name:"Status"){... on ProjectV2ItemFieldSingleSelectValue{name updatedAt}}}}
    timelineItems(last:50,itemTypes:[PROJECT_V2_ITEM_STATUS_CHANGED_EVENT]){nodes{
      ... on ProjectV2ItemStatusChangedEvent{createdAt status wasAutomated actor{__typename login}
        project{number owner{__typename ... on User{login} ... on Organization{login}}}}}}}}}'''


class GitHub:
    def __init__(self, repo, project=None):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
            raise ValueError("missing or invalid repository")
        self.repo = repo.lower()
        self.permissions = {}
        # (owner, number) of the board; None disables the drag route entirely.
        self.project = None
        if project:
            match = re.fullmatch(r"([A-Za-z0-9_.-]+)/([0-9]+)", project)
            if not match:
                raise ValueError("invalid --project (owner/number)")
            self.project = (match.group(1).lower(), int(match.group(2)))

    @staticmethod
    def read(*args):
        kind = ("board-item" if any("projectItems" in a for a in args)
                else "approval" if "graphql" in args else "comments" if "--paginate" in args
                else "permission" if args[-1].endswith("/permission") else "json")
        return github_read.read(list(args), kind)[1]

    def permission(self, login):
        if not re.fullmatch(r"[A-Za-z0-9_-]+(?:\[bot\])?", login):
            return None
        if login not in self.permissions:
            value = self.read("api", f"repos/{self.repo}/collaborators/{login}/permission")
            self.permissions[login] = value.get("permission")
        return self.permissions[login]

    def comments(self, number):
        pages = self.read("api", "--paginate", "--slurp",
                          f"repos/{self.repo}/issues/{number}/comments?per_page=100")
        if not isinstance(pages, list) or not pages or any(not isinstance(p, list) for p in pages):
            raise ValueError("unreadable comments")
        if any(not isinstance(c, dict) for page in pages for c in page):
            raise ValueError("unreadable comment")
        return [dict(comment, source=number) for page in pages for comment in page]

    def pr(self, number):
        # Paginate linked issues too: an omitted newer request must never make
        # an older approval authoritative. Remote-repository links are ignored.
        query = '''query($owner:String!,$repo:String!,$pr:Int!,$endCursor:String){
          repository(owner:$owner,name:$repo){pullRequest(number:$pr){headRefOid state body
            closingIssuesReferences(first:100,after:$endCursor){
              pageInfo{hasNextPage endCursor} nodes{number repository{nameWithOwner}}
            }}}}'''
        owner, name = self.repo.split("/")
        pages = self.read("api", "graphql", "--paginate", "--slurp", "-f", f"query={query}",
                          "-F", f"owner={owner}", "-F", f"repo={name}", "-F", f"pr={number}")
        heads, issues = set(), set()
        if not isinstance(pages, list) or not pages:
            raise ValueError("unreadable PR")
        for page in pages:
            if page.get("errors"):
                raise ValueError("incomplete PR response")
            pr = page["data"]["repository"]["pullRequest"]
            if pr["state"] != "OPEN":
                raise PRNotOpen(pr["state"])
            heads.add(pr["headRefOid"])
            links = pr["closingIssuesReferences"]
            for issue in links["nodes"]:
                if issue["repository"]["nameWithOwner"].lower() == self.repo:
                    issues.add(issue["number"])
            for repo_ref, number in CLOSES.findall(pr.get("body") or ""):
                if not repo_ref or repo_ref.lower() == self.repo:
                    issues.add(int(number))
        if links["pageInfo"]["hasNextPage"] or len(heads) != 1:
            raise ValueError("PR changed or issue links are incomplete")
        head = heads.pop()
        if not SHA.fullmatch(head):
            raise ValueError("unreadable PR head")
        return head, issues

    def drag_evidence(self, issue):
        """Live board evidence that `issue`'s card was dragged to Done, or None.

        None = the card is not in Done on this board (nothing to judge). A dict
        with `refused` = it IS in Done but the move cannot count. Otherwise
        {source, at, by}: `at` from the newest status-change timeline event when
        GitHub exposes them (it also names the actor and flags project-workflow
        moves), else from the Status value's `updatedAt` with `by` unknown.
        Verified 2026-10-12 on a user-owned board: timeline events came back
        EMPTY, so `updatedAt` is what the planner actually reads there.
        """
        if self.project is None:
            return None
        owner, number = self.project
        name_owner, name = self.repo.split("/")
        page = self.read("api", "graphql", "-f", f"query={BOARD_QUERY}", "-F", f"owner={name_owner}",
                         "-F", f"repo={name}", "-F", f"issue={issue}")
        if page.get("errors"):
            raise ValueError("incomplete board response")
        node = page["data"]["repository"]["issue"]
        if node["state"] != "OPEN":
            return None

        def ours(project):
            project = project or {}
            login = ((project.get("owner") or {}).get("login") or "").lower()
            return project.get("number") == number and login == owner

        items = [n for n in node["projectItems"]["nodes"] if ours(n.get("project"))]
        if len(items) != 1:
            return None
        value = items[0].get("fieldValueByName") or {}
        if value.get("name") != "Done":
            return None
        at, by = value.get("updatedAt"), None
        events = [e for e in node["timelineItems"]["nodes"] if e and ours(e.get("project"))]
        if events:
            newest = max(events, key=lambda e: timestamp(e.get("createdAt")))
            if newest.get("status") != "Done":
                return {"refused": "board history disagrees with the Done status"}
            if newest.get("wasAutomated"):
                return {"refused": "a project workflow moved the card to Done, not a person"}
            actor = newest.get("actor") or {}
            if actor.get("__typename") != "User" or not actor.get("login"):
                return {"refused": "the move to Done was not made by a person"}
            at, by = newest.get("createdAt"), actor["login"]
        timestamp(at)  # unreadable time → ValueError → no approval
        return {"source": issue, "at": at, "by": by}

    def check(self, number, head=None, scope=None, issue=None, drag=False):
        current_head, issues = self.pr(number)
        if issue is not None and issue not in issues:
            return {"approved": False, "why": "request issue is not linked to this PR"}
        if head is not None and current_head != head:
            return {"approved": False, "headChanged": True, "why": "PR head changed after review"}
        comments = []
        for source in sorted(issues | {number}):
            comments.extend(self.comments(source))
        expected = {"repo": self.repo, "pr": number, "head": current_head}
        if scope is not None:
            expected["scope"] = scope
        result = validate(comments, expected, self.permission)
        if result["approved"] or not drag or issue is None or self.project is None:
            return result
        # No comment/record approval: read the board once for a drag to Done.
        evidence = self.drag_evidence(issue)
        if evidence is None:
            return result
        return validate(comments, expected, self.permission, drag=evidence)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--pr", type=int)
    ap.add_argument("--head")
    ap.add_argument("--plan", help="JSON plan from merge-policy")
    ap.add_argument("--request", action="store_true", help="print a pinned request; no network or writes")
    ap.add_argument("--deps", action="store_true", help="enrich the dependency graph on stdin")
    ap.add_argument("--project", help="owner/number of the board; enables the drag-to-Done route in --deps")
    args = ap.parse_args()
    if args.request:
        if not args.pr or not args.head or not SHA.fullmatch(args.head) or not args.plan:
            ap.error("request requires --pr, full --head and --plan")
        print(marker(request_for(args.repo, args.pr, args.head, scope_for(json.loads(args.plan)))))
        return
    if args.deps:
        graph = json.load(sys.stdin)
        github = GitHub(args.repo, args.project) if args.repo else None
        for entry in graph.values():
            enrich_entry(entry, github)
        print(json.dumps(graph))
        return
    if not args.pr or not args.head or not args.plan:
        ap.error("check requires --pr, --head and --plan")
    try:
        result = GitHub(args.repo).check(args.pr, args.head, scope_for(json.loads(args.plan)))
    except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError):
        result = {"approved": False, "why": "approval evidence could not be verified"}
    print(json.dumps(result))


if __name__ == "__main__":
    try:
        main()
    except github_read.ReadHalted as error:
        print(f"GitHub read halt: {error}", file=sys.stderr)
        sys.exit(github_read.HALTED)

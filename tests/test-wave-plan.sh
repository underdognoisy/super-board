#!/usr/bin/env bash
# Tests super-board-wave-plan.sh against fixtures. No gh calls, no network —
# the dependency graph is injected with --deps.
#
# The contract these tests pin down changed on 2026-08-20. A wave used to be
# "one card per column, then fill to max_workers". It is now "every Ready card
# the dependency graph says is free, plus everything already in flight", with
# max_workers demoted to an optional throttle. Several scenarios below exist
# because the old contract shipped a board where five cards sat in Blocked long
# after their blockers had closed.
set -euo pipefail
cd "$(dirname "$0")"
PLAN="../scripts/super-board-wave-plan.sh"
CFG=fixtures/wave-config.json
ITEMS=fixtures/wave-items.json
DEPS=fixtures/wave-deps.json

fail() { echo "FAIL: $1" >&2; exit 1; }
plan() { "$PLAN" --config "$1" --items "$ITEMS" --deps "$DEPS"; }

# Uncapped is the default shape, so every scenario runs without a cap unless it
# is specifically testing the throttle.
NOCAP=$(jq 'del(.max_workers)' "$CFG")
OUT=$(plan <(echo "$NOCAP"))

# 1 — width follows the graph, not a knob. Free Ready cards are #12 alone:
#     #11 is assigned, #14 waits on open #12, #15 is unreadable, the draft is not
#     an issue. In flight: Review #10 and QA #13. Total 3.
echo "$OUT" | jq -e '.cards | length == 3' >/dev/null || fail "expected 3 cards, got: $OUT"
echo "$OUT" | jq -e '[.cards[].number] | sort == [10,12,13]' >/dev/null || fail "wrong cards: $OUT"

# 2 — downstream-first: in-flight cards lead, so work already begun finishes
#     before new work starts.
echo "$OUT" | jq -e '[.cards[].status] | index("Ready") > index("Review")' >/dev/null \
  || fail "in-flight cards must precede Ready cards: $OUT"

# 3 — a Ready card with an OPEN blocker is not dispatched. This is the whole
#     point: #14 would previously have been picked as backlog fill, hit its own
#     preflight, and parked in Blocked.
echo "$OUT" | jq -e '[.cards[].number] | index(14) == null' >/dev/null \
  || fail "#14 waits on open #12 and must not be dispatched: $OUT"

# 4 — an unreadable dependency line is fail-safe: never dispatched, always flagged.
echo "$OUT" | jq -e '[.cards[].number] | index(15) == null' >/dev/null \
  || fail "#15 has an unreadable Blocked-by line and must not be dispatched"
echo "$OUT" | jq -e '[.flag[].number] == [15]' >/dev/null \
  || fail "#15 must be flagged so a human can fix the line, got: $(echo "$OUT" | jq -c .flag)"
echo "$OUT" | jq -e '.flag[0].why | test("None")' >/dev/null \
  || fail "the flag must carry the reason, not just the number"

# 5 — the sweep: a Blocked card whose blockers have all closed comes back.
#     #16 waits on #9, which is closed. #17 waits on #12, which is open.
echo "$OUT" | jq -e '[.sweep[].number] == [16]' >/dev/null \
  || fail "expected only #16 swept, got: $(echo "$OUT" | jq -c .sweep)"
echo "$OUT" | jq -e '.sweep[0].clearedBy == [9]' >/dev/null \
  || fail "the sweep must name what cleared the card, for the comment it will post"

# 6 — a swept card is NOT also dispatched in the same plan. The orchestrator moves
#     it to Ready first; dispatching it straight from Blocked would skip that move
#     and leave the board lying about where the card is.
echo "$OUT" | jq -e '[.cards[].number] | index(16) == null' >/dev/null \
  || fail "#16 must be swept, not dispatched from Blocked"

# 7 — max_workers still throttles when set. It is a safety valve now, not the
#     wave-sizing mechanism.
OUT7=$(plan <(jq '.max_workers = 1' "$CFG"))
echo "$OUT7" | jq -e '(.cards | length) == 1 and .cards[0].number == 10' >/dev/null \
  || fail "max_workers=1 should keep only the leading in-flight card, got: $OUT7"

# 8 — the throttle does not suppress the sweep. A capped wave still frees cards,
#     because sweeping is a board correction and costs no worker.
echo "$OUT7" | jq -e '[.sweep[].number] == [16]' >/dev/null \
  || fail "a capped wave must still sweep, got: $(echo "$OUT7" | jq -c .sweep)"

# 9 — assignee is the cross-machine mutex and still wins over everything.
echo "$OUT" | jq -e '[.cards[].number] | index(11) == null' >/dev/null \
  || fail "#11 is assigned and must never be dispatched"

# 10 — reviews are no longer gated behind human_approves_merge. The merge race is
#      guarded where it happens (the merge lock plus the freshness gate inside the
#      wave workflow), so a second Review card is free to run here.
OUT10=$("$PLAN" --config <(echo "$NOCAP") --items fixtures/wave-items-review-heavy.json --deps "$DEPS")
echo "$OUT10" | jq -e '[.cards[] | select(.status == "Review")] | length >= 2' >/dev/null \
  || fail "reviews should run in parallel now, got: $OUT10"

# 11 — variant was removed in v3.0.0. A config still saying "qa-only" is refused
#      (exit 65) until onboard upgrades it: reading it as a build board would start
#      building on a board meant only for testing.
RC=0; "$PLAN" --config <(echo "$NOCAP" | jq '.variant = "qa-only"') --items "$ITEMS" --deps "$DEPS" >/dev/null 2>&1 || RC=$?
[ "$RC" -eq 65 ] || fail "a legacy qa-only config should exit 65, got $RC"

# 12 — an invalid variant fails loudly (exit 65); an absent one is the normal case.
RC=0; "$PLAN" --config <(jq '.variant = "fulll"' "$CFG") --items "$ITEMS" --deps "$DEPS" >/dev/null 2>&1 || RC=$?
[ "$RC" -eq 65 ] || fail "invalid variant should exit 65, got $RC"
"$PLAN" --config <(echo "$NOCAP" | jq 'del(.variant)') --items "$ITEMS" --deps "$DEPS" >/dev/null \
  || fail "a config with no variant must plan normally"

# 13 — empty board → empty everything (run-workflow.md's done condition depends
#      on this shape).
OUT13=$("$PLAN" --config <(echo "$NOCAP") --items <(echo '{"items":[]}') --deps "$DEPS")
echo "$OUT13" | jq -e '.cards == [] and .sweep == [] and .flag == [] and .stranded == []' >/dev/null \
  || fail "empty board should yield empty cards/sweep/flag/stranded, got: $OUT13"

# 14 — a card the graph has never heard of is not treated as free. An issue beyond
#      the fetch limit, or closed out from under the board, must not be dispatched
#      on the strength of a missing entry.
OUT14=$("$PLAN" --config <(echo "$NOCAP") --items "$ITEMS" --deps <(echo '{}'))
echo "$OUT14" | jq -e '[.cards[] | select(.status == "Ready")] | length == 0' >/dev/null \
  || fail "an empty graph must yield no Ready picks, got: $OUT14"

# 15 — a graph that cannot be derived is a hard failure, not an empty graph.
#      Without --deps and without config.repo.remote there is nothing to derive from.
RC=0; "$PLAN" --config <(jq 'del(.repo)' "$CFG") --items "$ITEMS" >/dev/null 2>&1 || RC=$?
[ "$RC" -eq 66 ] || fail "a missing repo remote should exit 66, got $RC"

# 16 — a Building card with no claim is stranded: a stopped wave left it there and
#      no lane selects from Building. A claimed one (#19) has a live worker.
echo "$OUT" | jq -e '[.stranded[].number] == [18]' >/dev/null \
  || fail "expected only #18 stranded, got: $(echo "$OUT" | jq -c .stranded)"
echo "$OUT" | jq -e '[.cards[].number] | index(18) == null' >/dev/null \
  || fail "#18 must be moved to Ready first, not dispatched from Building"

# 17 — label routing. A Ready card labelled `qa` skips the Builder (lane qa); `bug`,
#      `feature` and no label are built first (lane build). QA/Review cards keep their
#      column's lane. Labels come from the item (`labels`) or the content, any case.
LBL_ITEMS=$(jq -n '{items: [
  {status:"Ready", labels:["QA"], content:{type:"Issue", number:12, title:"qa card", assignees:[]}},
  {status:"Ready", labels:["bug"], content:{type:"Issue", number:10, title:"bug card", assignees:[]}},
  {status:"Ready", content:{type:"Issue", number:13, title:"no label", assignees:[], labels:[{name:"feature"}]}},
  {status:"Ready", content:{type:"Issue", number:9, title:"bare", assignees:[]}},
  {status:"QA", labels:["qa"], content:{type:"Issue", number:11, title:"in qa", assignees:[]}}]}')
LBL_DEPS=$(jq -n '[9,10,12,13] | map({(tostring): {runnable:true, parseable:true, blockers:[]}}) | add')
OUT17=$("$PLAN" --config <(echo "$NOCAP") --items <(echo "$LBL_ITEMS") --deps <(echo "$LBL_DEPS"))
lane() { echo "$OUT17" | jq -r --argjson n "$1" '.cards[] | select(.number == $n) | .lane'; }
[ "$(lane 12)" = qa ]    || fail "a qa-labelled Ready card must skip Building (lane qa), got: $OUT17"
[ "$(lane 10)" = build ] || fail "a bug card is built first, got $(lane 10)"
[ "$(lane 13)" = build ] || fail "a feature card (label on content) is built first, got $(lane 13)"
[ "$(lane 9)" = build ]  || fail "no label = Building, got $(lane 9)"
[ "$(lane 11)" = qa ]    || fail "a QA-column card keeps lane qa, got $(lane 11)"
echo "$OUT17" | jq -e '.cards[] | select(.number == 12) | .labels == ["qa"]' >/dev/null \
  || fail "labels must be passed through lower-cased, got: $OUT17"

# 18 — resume: a 🙋 Blocked card whose human step is done (#20) is reported for
#      the move back to Review; one still waiting (#21) is not. Neither is swept
#      to Ready or dispatched from Blocked.
echo "$OUT" | jq -e '[.resume[].number] == [20]' >/dev/null \
  || fail "expected only #20 resumed, got: $(echo "$OUT" | jq -c .resume)"
echo "$OUT" | jq -e '[.sweep[].number, .cards[].number] | (index(20) == null and index(21) == null)' >/dev/null \
  || fail "🙋 cards must not be swept to Ready or dispatched"

# 19 — a moved head while Blocked goes to request refresh, never approved resume.
OUT19=$("$PLAN" --config <(echo "$NOCAP") --items "$ITEMS" --deps <(jq '.["21"].approvalRefresh=true | .["21"].approvalWhy="PR head changed"' "$DEPS"))
echo "$OUT19" | jq -e '[.refreshApproval[].number] == [21]' >/dev/null || fail "stale approval must get a fresh request"
echo "$OUT19" | jq -e '[.resume[].number, .cards[].number, .sweep[].number] | index(21) == null' >/dev/null || fail "refresh is not permission to merge or build"

# 20–26 — the Approve column, waits-on and reason labels (v3.1.0). Inline board so
#     each shape is one card.
AP_ITEMS=$(jq -n '{items: [
  {status:"Approve", labels:["needs-you"], content:{type:"Issue", number:30, title:"approved", assignees:[]}},
  {status:"Approve", content:{type:"Issue", number:31, title:"changes asked", assignees:[]}},
  {status:"Approve", content:{type:"Issue", number:32, title:"free text", assignees:[]}},
  {status:"Approve", content:{type:"Issue", number:33, title:"early done", assignees:[]}},
  {status:"Blocked", labels:["needs-you"], content:{type:"Issue", number:34, title:"legacy gated", assignees:[]}},
  {status:"Blocked", labels:["blocked:decision"], content:{type:"Issue", number:35, title:"plain blocked", assignees:[]}},
  {status:"Ready", labels:["waits-on","feature"], content:{type:"Issue", number:40, title:"waits, open", assignees:[]}},
  {status:"Ready", labels:["waits-on"], content:{type:"Issue", number:41, title:"waits, cleared", assignees:[]}},
  {status:"Blocked", content:{type:"Issue", number:51, title:"no reason label", assignees:[]}},
  {status:"Blocked", labels:["blocked:other"], content:{type:"Issue", number:52, title:"swept with label", assignees:[]}},
  {status:"Approve", content:{type:"Issue", number:53, title:"approve, head moved", assignees:[]}}]}')
AP_DEPS=$(jq -n '
  def gate: {parseable:true, humanGated:true, runnable:false, blockers:[], openBlockers:[], needsYou:true,
             needsYouDone:false, changesRequested:null, classify:[], approvalRefresh:false, staleDone:null};
  { "30": (gate | .needsYouDone = true),
    "31": (gate | .changesRequested = {by:"john", note:"rename it"} | .humanGated = false | .runnable = true),
    "32": (gate | .classify = [{by:"john", id:7, at:"t", body:"also log it"}]),
    "33": (gate | .staleDone = {by:"john", id:9, at:"2026-10-12T20:59:37Z"}),
    "34": (gate | .needsYouDone = true),
    "35": {parseable:true, humanGated:true, runnable:false, blockers:[], openBlockers:[], needsYou:false,
           needsYouDone:true},
    "40": {parseable:true, runnable:false, blockers:[12], openBlockers:[12]},
    "41": {parseable:true, runnable:true, blockers:[12], openBlockers:[]},
    "51": {parseable:true, humanGated:true, runnable:false, blockers:[], openBlockers:[]},
    "52": {parseable:true, runnable:true, blockers:[9], openBlockers:[]},
    "53": (gate | .approvalRefresh = true | .approvalWhy = "PR head changed after review") }')
OUTA=$("$PLAN" --config <(echo "$NOCAP") --items <(echo "$AP_ITEMS") --deps <(echo "$AP_DEPS"))
nums() { echo "$OUTA" | jq -c "[.$1[].number] | sort"; }

# 20 — resume reads Approve cards, and still a legacy needs-you card in Blocked.
#      A plain Blocked card is never an approval route, whatever the graph says.
[ "$(nums resume)" = "[30,34]" ] || fail "resume must read Approve + legacy Blocked needs-you, got $(nums resume)"

# 21 — changes / classify / refreshApproval read Approve cards.
[ "$(nums changes)" = "[31]" ]          || fail "changes must read Approve cards, got $(nums changes)"
[ "$(nums classify)" = "[32]" ]         || fail "classify must read Approve cards, got $(nums classify)"
[ "$(nums refreshApproval)" = "[53]" ]  || fail "refreshApproval must read Approve cards, got $(nums refreshApproval)"

# 22 — a pre-request done is reported for the notice and is NEVER a resume.
[ "$(nums staleDone)" = "[33]" ] || fail "an early done must surface in staleDone, got $(nums staleDone)"
echo "$OUTA" | jq -e '.staleDone[0] | .by == "john" and .id == 9' >/dev/null || fail "staleDone must carry by/id"
echo "$OUTA" | jq -e '[.resume[].number] | index(33) == null' >/dev/null || fail "an early done must never resume"

# 23 — Approve cards are never dispatched or swept from the planner: only the
#      verified routes move them, and a runnable changes card goes via `changes`.
echo "$OUTA" | jq -e '[.cards[].number, .sweep[].number] as $x | [30,31,32,33,53] | all(. as $n | $x | index($n) == null)' >/dev/null \
  || fail "Approve cards must not be dispatched or swept, got $(echo "$OUTA" | jq -c '{cards,sweep}')"

# 24 — waits-on: not picked while its blocker is open; released (not picked) once
#      clear, so the label comes off before the card builds.
echo "$OUTA" | jq -e '[.cards[].number] as $x | [40,41] | all(. as $n | $x | index($n) == null)' >/dev/null \
  || fail "a waits-on Ready card must never be picked while labelled"
[ "$(nums release)" = "[41]" ] || fail "only the cleared waits-on card is released, got $(nums release)"
echo "$OUTA" | jq -e '.release[0].clearedBy == [12]' >/dev/null || fail "release must name what cleared it"

# 25 — once released (label gone, same graph) the card is an ordinary free Ready card.
OUT25=$("$PLAN" --config <(echo "$NOCAP") \
  --items <(echo "$AP_ITEMS" | jq '(.items[] | select(.content.number == 41) | .labels) = []') --deps <(echo "$AP_DEPS"))
echo "$OUT25" | jq -e '[.cards[].number] | index(41) != null' >/dev/null || fail "a released card must be picked next wave"
echo "$OUT25" | jq -e '[.release[].number] == []' >/dev/null || fail "a released card is not released twice"

# 26 — reason labels: an unlabelled Blocked card is reported (legacy needs-you and
#      labelled cards are not); a swept card names the reason label to drop.
[ "$(nums unlabelled)" = "[51]" ] || fail "only #51 lacks a reason label, got $(nums unlabelled)"
echo "$OUTA" | jq -e '.sweep[] | select(.number == 52) | .removeLabels == ["blocked:other"]' >/dev/null \
  || fail "sweep must name the reason label to remove, got $(echo "$OUTA" | jq -c .sweep)"

# 27–31 — drag to Done = approval (v3.3.0). The graph decides; the planner routes.
DR_ITEMS=$(jq -n '{items: [
  {status:"Done", labels:["needs-you"], content:{type:"Issue", number:60, title:"dragged, verified", assignees:[]}},
  {status:"Done", content:{type:"Issue", number:61, title:"dragged, changes stand", assignees:[]}},
  {status:"Done", content:{type:"Issue", number:62, title:"dragged, head moved", assignees:[]}},
  {status:"Done", content:{type:"Issue", number:63, title:"dragged too early", assignees:[]}},
  {status:"Done", content:{type:"Issue", number:64, title:"merged and closed", assignees:[]}},
  {status:"Done", content:{type:"Issue", number:65, title:"PR merged, issue open", assignees:[]}},
  {status:"Done", content:{type:"Issue", number:66, title:"dragged, unacked comment", assignees:[]}},
  {status:"Approve", content:{type:"Issue", number:67, title:"comment approval", assignees:[]}}]}')
DR_DEPS=$(jq -n '
  def gate: {parseable:true, humanGated:true, runnable:false, blockers:[], openBlockers:[], needsYou:true,
             needsYouDone:false, changesRequested:null, classify:[], approvalRefresh:false, staleDone:null,
             prState:"OPEN", approvedVia:null, drag:null};
  { "60": (gate | .needsYouDone = true | .approvedVia = "drag"
                | .drag = {at:"2026-10-12T23:02:26Z", by:null, record:"drag-approval: {\"request\":1}"}),
    "61": (gate | .changesRequested = {by:"john", note:"rename it"} | .humanGated = false | .runnable = true),
    "62": (gate | .approvalRefresh = true | .approvalWhy = "PR head changed after review"),
    "63": (gate | .approvalWhy = "the card reached Done before the current request; drag it again to approve"),
    "65": (gate | .prState = "MERGED" | .approvalWhy = "PR is merged"),
    "66": (gate | .classify = [{by:"john", id:8, at:"t", body:"wait, one question"}]),
    "67": (gate | .needsYouDone = true | .approvedVia = "comment") }')
OUTD=$("$PLAN" --config <(echo "$NOCAP") --items <(echo "$DR_ITEMS") --deps <(echo "$DR_DEPS"))
dnums() { echo "$OUTD" | jq -c "[.$1[].number] | sort"; }

# 27 — a verified drag resumes with via "drag" and the record line to post; a
#      comment approval says via "comment".
[ "$(dnums resume)" = "[60,67]" ] || fail "resume must hold the verified drag + the comment approval, got $(dnums resume)"
echo "$OUTD" | jq -e '.resume[] | select(.number == 60) | .via == "drag" and (.record | startswith("drag-approval: ")) and .at == "2026-10-12T23:02:26Z"' >/dev/null \
  || fail "a drag resume must carry via/at/record, got $(echo "$OUTD" | jq -c .resume)"
echo "$OUTD" | jq -e '.resume[] | select(.number == 67) | .via == "comment"' >/dev/null || fail "comment approval must say via comment"

# 28 — a standing changes: or an unacked classify comment blocks a drag; a moved
#      head goes to refreshApproval (→ Review), never to resume.
[ "$(dnums changes)" = "[61]" ]         || fail "changes must win over a drag, got $(dnums changes)"
[ "$(dnums classify)" = "[66]" ]        || fail "an unacked comment holds a drag in classify, got $(dnums classify)"
[ "$(dnums refreshApproval)" = "[62]" ] || fail "a moved head must refresh, got $(dnums refreshApproval)"

# 29 — a drag the graph refused (PR still open) goes back to Approve with its why;
#      cards another route owns are not double-routed.
[ "$(dnums dragRefused)" = "[63]" ] || fail "only the too-early drag is refused, got $(dnums dragRefused)"
echo "$OUTD" | jq -e '.dragRefused[0].why | test("before the current request")' >/dev/null || fail "dragRefused must carry the why"

# 30 — a Done card with a closed issue or a merged PR is merged work, never an
#      approval and never refused back to Approve.
echo "$OUTD" | jq -e '[.resume[], .dragRefused[], .changes[], .classify[], .refreshApproval[] | .number] | (index(64) == null and index(65) == null)' >/dev/null \
  || fail "merged Done cards must not be approval routes, got $OUTD"

# 31 — Done cards are never dispatched or swept.
echo "$OUTD" | jq -e '.cards == [] and .sweep == []' >/dev/null || fail "Done cards must not be dispatched, got $(echo "$OUTD" | jq -c '{cards,sweep}')"

echo "PASS: test-wave-plan.sh (31 scenarios)"

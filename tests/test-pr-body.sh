#!/usr/bin/env bash
# Tests scripts/super-board-pr-body.sh — one marker block rewritten, the rest
# untouched, nothing written when the PR head moved. `gh` is a stub on PATH.
#
#   bash tests/test-pr-body.sh
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPT="$REPO_ROOT/scripts/super-board-pr-body.sh"
WORK="$(mktemp -d)"
export SB_GITHUB_RETRY_DELAY=0 SB_GITHUB_HALT_FILE="$WORK/halt.json"
trap 'rm -rf "$WORK"' EXIT

PASS=0; FAIL=0
ok()  { PASS=$((PASS + 1)); printf '  ✅ %s\n' "$1"; }
bad() { FAIL=$((FAIL + 1)); printf '  ❌ %s\n     expected: %s\n     actual:   %s\n' "$1" "$2" "$3"; }
is()  { [ "$2" = "$3" ] && ok "$1" || bad "$1" "$2" "$3"; }
has() { case "$2" in *"$3"*) ok "$1" ;; *) bad "$1" "contains: $3" "$2" ;; esac; }
hasnt() { case "$2" in *"$3"*) bad "$1" "no '$3'" "found it" ;; *) ok "$1" ;; esac; }

# gh stub (REST): GET pulls/N serves $WORK/body.md and $HEAD_OID; PATCH pulls/N --input - stores the new body.
cat > "$WORK/gh" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$GH_LOG"
case "$*" in
  "api -X PATCH repos/{owner}/{repo}/pulls/"*" --input -") jq -j .body > "$BODY" ;;
  "api repos/{owner}/{repo}/pulls/"*)
    jq -n --arg h "$HEAD_OID" --rawfile b "$BODY" '{head: {sha: $h}, body: $b}' | jq '{headRefOid: .head.sha, body: (.body // "")}' ;;
  *) echo "unexpected gh call: $*" >&2; exit 1 ;;
esac
STUB
chmod +x "$WORK/gh"
export PATH="$WORK:$PATH" GH_LOG="$WORK/gh.log" BODY="$WORK/body.md" HEAD_OID="3f9c2a1aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

cat > "$BODY" <<'MD'
<!-- sb:status -->
> [!NOTE]
> ⏳ In QA · ✅ 0/2 AC · Closes #7 · head `3f9c2a1`
<!-- /sb:status -->

<!-- sb:problem -->
## Problem
- **Where:** `/x`
<!-- /sb:problem -->

<!-- sb:history -->
## Iteration history
| Lane | Done | Time | Details |
|---|---|---|---|
| 🔨 builder | ✅ | Oct 2, 10:02 EDT | draft |
<!-- /sb:history -->
MD

printf '> [!TIP]\n> ✅ Merged · `9c41e07`\n' > "$WORK/status.md"
printf '## Risk\n🟢 **Low** · one constant.\n' > "$WORK/risk.md"
printf '| 🔍 qa | ✅ v1 | Oct 2, 11:55 EDT | runs/issue-7-qa-v1/ |\n' > "$WORK/row.md"

run() { : > "$GH_LOG"; "$SCRIPT" "$@" 2>"$WORK/err"; }

echo "── replace one block, keep the rest"
is  "replace reports updated" "updated" "$(run --pr 7 --block status --expect-head 3f9c2a1 --body-file "$WORK/status.md")"
has "new status written"      "$(cat "$BODY")" "✅ Merged"
hasnt "old status gone"       "$(cat "$BODY")" "In QA"
has "problem block untouched" "$(cat "$BODY")" '- **Where:** `/x`'

echo "── idempotent"
is  "same content twice is a no-op" "unchanged" "$(run --pr 7 --block status --expect-head 3f9c2a1 --body-file "$WORK/status.md")"
hasnt "no edit call on a no-op"     "$(cat "$GH_LOG")" "PATCH"

echo "── missing block inserted in order"
run --pr 7 --block risk --expect-head 3f9c2a1 --body-file "$WORK/risk.md" >/dev/null
has "risk inserted" "$(cat "$BODY")" "<!-- sb:risk -->"
H=$(grep -n 'sb:history -->' "$BODY" | head -1 | cut -d: -f1); R=$(grep -n '<!-- sb:risk -->' "$BODY" | cut -d: -f1)
[ "$R" -gt "$H" ] && ok "risk lands after history" || bad "risk lands after history" "line > $H" "$R"
printf '## Solution\n- one change\n' > "$WORK/sol.md"
run --pr 7 --block solution --expect-head 3f9c2a1 --body-file "$WORK/sol.md" >/dev/null
P=$(grep -n '<!-- /sb:problem -->' "$BODY" | cut -d: -f1); S=$(grep -n '<!-- sb:solution -->' "$BODY" | cut -d: -f1)
H=$(grep -n '<!-- sb:history -->' "$BODY" | cut -d: -f1)
[ "$S" -gt "$P" ] && [ "$S" -lt "$H" ] && ok "solution lands between problem and history" || bad "solution between problem and history" "$P < x < $H" "$S"

echo "── append a history row, once"
run --pr 7 --block history --expect-head 3f9c2a1 --append-file "$WORK/row.md" >/dev/null
has "row appended" "$(cat "$BODY")" "runs/issue-7-qa-v1/"
is  "re-append is a no-op" "unchanged" "$(run --pr 7 --block history --expect-head 3f9c2a1 --append-file "$WORK/row.md")"
is  "row appears once" "1" "$(grep -c 'runs/issue-7-qa-v1/' "$BODY")"

echo "── refuses when the head moved"
cp "$BODY" "$WORK/before.md"
RC=0; run --pr 7 --block status --expect-head deadbee --body-file "$WORK/risk.md" >/dev/null || RC=$?
is  "moved head exits 6" "6" "$RC"
hasnt "no edit on a moved head" "$(cat "$GH_LOG")" "PATCH"
is  "body unchanged" "$(cat "$WORK/before.md")" "$(cat "$BODY")"

echo "── an edit never drops an issue link (#66, 2026-10-16)"
# The reviewer's 🙋 status rewrite said "request on #66" and lost "Closes #66";
# the approval gate then refused six `done`s as "not linked to this PR".
cat > "$BODY" <<'MD'
<!-- sb:status -->
> [!WARNING]
> 🔍 QA v2 → Review · ✅ 2/3 AC · Closes #66 · head `c5359ed`
<!-- /sb:status -->

<!-- sb:problem -->
## Problem
<!-- /sb:problem -->
MD
printf '> [!IMPORTANT]\n> 🙋 Review → Approve · request on #66 · head `c5359ed`\n' > "$WORK/gate.md"
run --pr 80 --block status --expect-head 3f9c2a1 --body-file "$WORK/gate.md" >/dev/null
has "dropped link kept"          "$(cat "$BODY")" "Closes #66"
has "stderr names the kept link" "$(cat "$WORK/err")" "kept #66"
L=$(grep -n '<!-- sb:links -->' "$BODY" | cut -d: -f1); S=$(grep -n '<!-- sb:status -->' "$BODY" | cut -d: -f1)
[ -n "$L" ] && [ "$L" -lt "$S" ] && ok "links block sits first" || bad "links block sits first" "< $S" "$L"
is "kept once on a repeat edit" "unchanged" "$(run --pr 80 --block status --expect-head 3f9c2a1 --body-file "$WORK/gate.md")"
printf '> 🙋 Review → Approve · Closes #66 · head `c5359ed`\n' > "$WORK/gate2.md"
run --pr 80 --block status --expect-head 3f9c2a1 --body-file "$WORK/gate2.md" >/dev/null
is "no new link when the edit keeps it" "" "$(cat "$WORK/err")"
printf 'Refs #66\n' > "$WORK/links.md"
printf '> 🙋 Review → Approve · head `c5359ed`\n' > "$WORK/gate3.md"
run --pr 80 --block status --expect-head 3f9c2a1 --body-file "$WORK/gate3.md" >/dev/null
run --pr 80 --block links --expect-head 3f9c2a1 --body-file "$WORK/links.md" >/dev/null
hasnt "a links rewrite changes links on purpose" "$(cat "$BODY")" "Closes #66"
has   "the deliberate link stands"               "$(cat "$BODY")" "Refs #66"

echo "── usage"
RC=0; run --pr 7 --block status --body-file "$WORK/risk.md" >/dev/null || RC=$?; is "expect-head required" 64 "$RC"
RC=0; run --pr 7 --block nope --expect-head x --body-file "$WORK/risk.md" >/dev/null || RC=$?; is "block is an enum" 64 "$RC"
RC=0; run --pr 7 --block ac --expect-head x >/dev/null || RC=$?; is "content file required" 64 "$RC"
OUT=$(run --pr 7 --block ac --dry-run --body-file "$WORK/risk.md"); has "dry run prints the body" "$OUT" "<!-- sb:ac -->"
hasnt "dry run edits nothing" "$(cat "$GH_LOG")" "PATCH"

echo "── skeleton"
SK=$("$SCRIPT" --skeleton)
for b in status problem solution ac history visual risk; do has "skeleton has $b" "$SK" "<!-- sb:$b -->"; done

echo "── time in config timezone"
echo '{"timezone":"UTC"}' > "$WORK/cfg.json"
has "time carries the zone" "$("$SCRIPT" --time --config "$WORK/cfg.json")" "UTC"

printf '\n%s passed, %s failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]

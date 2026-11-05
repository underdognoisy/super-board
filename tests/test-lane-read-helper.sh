#!/usr/bin/env bash
# Offline test: a lane's malformed read never halts the run, even from a card
# worktree whose base branch still carries an older read helper.
#
# 2026-11-04 wave 1 (PRI-628 demo): the 64 path for `--slurp --jq` existed on
# the demo's main checkout, but the lane ran the helper by its relative path
# from a worktree cut from origin/staging, whose committed copy predated that
# fix. The old copy retried the gh usage error 3 times and wrote the run halt.
# The wave prompt must point lanes at the main checkout's helper. Run:
#     bash tests/test-lane-read-helper.sh
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
pack="$(dirname "$here")"
helper="$pack/scripts/super-board-github-read.py"
[ -f "$helper" ] || helper="$pack/bin/super-board-github-read.py"   # installed repo: tests/ beside bin/
wave="$pack/workflows/super-board-wave.js"
fail() { echo "FAIL: $*" >&2; exit 1; }

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

# Stub gh: reject --slurp with --jq exactly as gh 2.96 does (stderr, exit 1).
mkdir "$tmp/stub"
cat > "$tmp/stub/gh" <<STUB
#!/bin/sh
echo x >> "$tmp/calls"
case " \$* " in
  *" --slurp "*"--jq "*|*" --jq "*"--slurp "*)
    echo 'the \`--slurp\` option is not supported with \`--jq\` or \`--template\`' >&2
    printf '\nUsage:  gh api <endpoint> [flags]\n' >&2
    exit 1 ;;
esac
echo '[[]]'
STUB
chmod +x "$tmp/stub/gh"

# Repo: main carries the current helper; staging (the board base) carries the
# pre-fix copy that treats a gh usage error as an outage.
repo="$tmp/repo"
git init -q -b main "$repo"
git -C "$repo" config user.email t@example.com
git -C "$repo" config user.name t
mkdir -p "$repo/.claude/bin"
python3 - "$helper" "$repo/.claude/bin/super-board-github-read.py" <<'PY'
import sys
src = open(sys.argv[1]).read()
old = 'if result.returncode and cli_usage_error(result.stderr):'
assert old in src, 'helper no longer has the usage-error check this test disables'
open(sys.argv[2], 'w').write(src.replace(old, 'if False:'))
PY
chmod +x "$repo/.claude/bin/super-board-github-read.py"
git -C "$repo" add -A && git -C "$repo" commit -qm 'onboard: old helper'
git -C "$repo" branch staging
cp "$helper" "$repo/.claude/bin/super-board-github-read.py"
git -C "$repo" commit -qam 'fix: usage errors exit 64'
git -C "$repo" worktree add -q "$repo/.claude/worktrees/issue-97" -b issue-97 staging

# The read command exactly as the wave prompt tells a build lane to run it.
cmd="$(node - "$wave" <<'JS'
const fs = require('fs')
const src = fs.readFileSync(process.argv[2], 'utf8').replace(/^export const meta/m, 'const meta')
const AsyncFunction = (async () => {}).constructor
const prompts = {}
const agent = async (prompt, opts) => {
  prompts[opts.label] = prompt
  if (opts.label.startsWith('preflight')) return { verdicts: [{ number: 97, verdict: 'proceed', detail: 'clean' }] }
  if (opts.label.startsWith('classify')) return { kind: 'feature', complexity: 'low' }
  return { status: 'failed', column: 'Building', detail: 'stop' }
}
const pipeline = async (items, ...stages) => Promise.all(items.map(async (it) => {
  let v = it
  for (const [i, st] of stages.entries()) v = i === 0 ? await st(it) : await st(v, it)
  return v
}))
new AsyncFunction('args', 'agent', 'pipeline', 'log', src)(
  { configPath: 'c.json', cards: [{ number: 97, status: 'Ready', title: 't' }] }, agent, pipeline, () => {})
  .then(() => {
    const p = prompts['build:#97'] || (console.error('build lane did not run'), process.exit(1))
    const m = p.match(/Read shapes \(helper: (.+?) --kind <kind>/)
    if (!m) { console.error('build prompt names no read helper command'); process.exit(1) }
    process.stdout.write(m[1])
  })
JS
)"

# Run it from inside the card worktree, the way a lane does.
set +e
out="$(cd "$repo/.claude/worktrees/issue-97" && env PATH="$tmp/stub:$PATH" SB_GITHUB_RETRY_DELAY=0 \
  sh -c "$cmd --kind json -- api 'repos/{owner}/{repo}/pulls/98/comments' --paginate --slurp --jq x" 2>&1)"
rc=$?
set -e
calls=$(wc -l < "$tmp/calls" 2>/dev/null | tr -d ' ' || echo 0)
[ "$rc" -eq 64 ] || fail "lane read from a stale-base worktree exited $rc, want 64 (helper: $cmd): $out"
[ ! -e "$repo/.claude/super-board/github-halt.json" ] || fail "lane read wrote the run halt: $(cat "$repo/.claude/super-board/github-halt.json")"
[ "$calls" = 1 ] || fail "expected one gh attempt, got $calls"
grep -q 'the run continues' <<<"$out" || fail "64 message must say the run continues: $out"
echo "ok: a lane's --slurp --jq read exits 64 from a stale-base worktree, no halt"

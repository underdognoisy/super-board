#!/usr/bin/env bash
# Tests the Builder pre-flight wiring in workflows/super-board-wave.js. No gh
# calls: the script body runs under node with agent/pipeline/log stubbed, and the
# pre-flight agent's verdicts are scripted per card.
#
# The rule: no Ready card reaches runLane('build') without a pre-flight verdict
# that says go. A hold, a sequence-behind, or a missing verdict ends the card's
# chain for this wave and shows up in the summary — never silently dropped.
set -euo pipefail
cd "$(dirname "$0")"

node - ../workflows/super-board-wave.js <<'JS'
const fs = require('fs')
const src = fs.readFileSync(process.argv[2], 'utf8').replace(/^export const meta/m, 'const meta')
const AsyncFunction = (async () => {}).constructor
const run = (args, verdicts) => {
  const labels = [], prompts = {}
  const agent = async (prompt, opts) => {
    labels.push(opts.label); prompts[opts.label] = prompt
    if (opts.label.startsWith('preflight')) {
      const nums = opts.label.slice('preflight:'.length).split(',').map((s) => Number(s.slice(1)))
      return { verdicts: nums.filter((n) => verdicts[n]).map((n) => ({ number: n, ...verdicts[n] })) }
    }
    if (opts.label.startsWith('classify')) return { kind: 'feature', complexity: 'low' }
    return { status: 'advanced', column: opts.label.startsWith('review') ? 'Done' : 'next', detail: 'ok' }
  }
  const pipeline = async (items, ...stages) => Promise.all(items.map(async (it) => {
    let v = it
    for (const [i, st] of stages.entries()) v = i === 0 ? await st(it) : await st(v, it)
    return v
  }))
  return new AsyncFunction('args', 'agent', 'pipeline', 'log', src)(args, agent, pipeline, () => {})
    .then((out) => ({ out, labels, prompts }))
}
const fail = (m) => { console.error('FAIL: ' + m); process.exit(1) }
const card = (number, status = 'Ready', extra = {}) => ({ number, status, title: `t${number}`, ...extra })
const by = (out, n) => out.cards.find((c) => c.number === n)

;(async () => {
  const { out, labels } = await run({ configPath: 'c.json',
    cards: [card(1), card(2), card(3), card(4), card(5), card(6, 'Review')] }, {
      1: { verdict: 'proceed', detail: 'clean' },
      2: { verdict: 'hold', column: 'Blocked', detail: 'duplicate of merged PR #31' },
      3: { verdict: 'sequence', column: 'Blocked', detail: 'behind #50 (PR #41 touches login.ts)' },
      4: { verdict: 'sequence', column: 'Ready', detail: 'expected conflict with PR #44 — merge gate rebases' },
      // 5: no verdict at all
    })

  // 1 — proceed builds.
  labels.includes('build:#1') || fail('#1 proceed must build')
  // 2 — hold never builds, and is reported as a pre-flight block in Blocked.
  labels.includes('build:#2') && fail('#2 hold must not build')
  ;(by(out, 2).lastLane === 'preflight' && by(out, 2).finalStatus === 'blocked' && by(out, 2).column === 'Blocked')
    || fail('#2 must be reported preflight:blocked in Blocked: ' + JSON.stringify(by(out, 2)))
  ;/#31/.test(by(out, 2).detail) || fail('#2 detail must name the duplicate')
  // 3 — sequence behind an issue parks the card; 4 — sequence with only a conflict note builds.
  labels.includes('build:#3') && fail('#3 sequenced behind #50 must not build this wave')
  labels.includes('build:#4') || fail('#4 conflict-note sequence must build')
  // 5 — a missing verdict is never built unchecked; it stays Ready for the next wave.
  labels.includes('build:#5') && fail('#5 had no verdict and must not build')
  ;(by(out, 5).finalStatus === 'failed' && by(out, 5).column === 'Ready') || fail('#5 must be failed@Ready: ' + JSON.stringify(by(out, 5)))
  // 6 — cards already past Ready are not pre-flighted.
  labels.some((l) => l.startsWith('preflight') && /#6\b/.test(l)) && fail('#6 in Review must not be pre-flighted')
  labels.includes('review:#6') || fail('#6 review must still run')

  // 7 — batching: 5 Ready cards fit one pre-flight agent.
  labels.filter((l) => l.startsWith('preflight')).length === 1 || fail('5 Ready cards = 1 pre-flight agent, got ' + labels)
  const seven = await run({ configPath: 'c.json', cards: [1, 2, 3, 4, 5, 6, 7].map((n) => card(n)) },
    Object.fromEntries([1, 2, 3, 4, 5, 6, 7].map((n) => [n, { verdict: 'proceed', detail: 'clean' }])))
  seven.labels.filter((l) => l.startsWith('preflight')).length === 2 || fail('7 Ready cards = 2 pre-flight agents')

  // 8 — a qa-labelled Ready card skips Building: no pre-flight, no Builder, Tester first,
  //     told to move the card Ready → QA itself. Lane from the planner or from labels.
  const qa = await run({ configPath: 'c.json', cards: [card(1, 'Ready', { lane: 'qa', labels: ['qa'] }), card(2, 'Ready', { labels: ['QA'] })] }, {})
  qa.labels.some((l) => l.startsWith('preflight')) && fail('qa cards must not pre-flight')
  qa.labels.some((l) => l.startsWith('build')) && fail('qa cards must not build')
  ;(qa.labels.includes('qa:#1') && qa.labels.includes('qa:#2')) || fail('qa Ready cards go to QA: ' + qa.labels)
  ;/Ready → QA/.test(qa.prompts['qa:#1']) || fail('the Tester must be told to move a qa card Ready → QA')

  // 9 — bug, feature and no label all build; the classifier keeps a label and labels a bare card.
  const mix = await run({ configPath: 'c.json', cards: [card(3, 'Ready', { lane: 'build', labels: ['bug'] }), card(4)] },
    { 3: { verdict: 'proceed', detail: 'ok' }, 4: { verdict: 'proceed', detail: 'ok' } })
  ;(mix.labels.includes('build:#3') && mix.labels.includes('build:#4')) || fail('bug and unlabelled cards build: ' + mix.labels)
  ;/label says "bug"/.test(mix.prompts['classify:#3']) || fail('classifier must keep the bug label')
  ;(/--add-label/.test(mix.prompts['classify:#4']) && /never qa/.test(mix.prompts['classify:#4']))
    || fail('classifier must label a bare card feature or bug, never qa')

  // 10 — a stale caller passing the removed qa-only variant is refused, not guessed.
  let threw = false
  try { await run({ configPath: 'c.json', variant: 'qa-only', cards: [card(1)] }, {}) } catch { threw = true }
  threw || fail('variant qa-only must be refused')

  // 11 — v3.2.0 waits-on: a sequence that names a blocker stays in Ready but never
  //      builds this wave; it is reported `sequenced@Ready`, not a Blocked card.
  //      The pre-flight agent is told to label it waits-on, never move it to Blocked.
  const seq = await run({ configPath: 'c.json', cards: [card(7), card(8)] }, {
    7: { verdict: 'sequence', column: 'Ready', blockedBy: [50], detail: 'waits on #50 (PR #41 touches login.ts)' },
    8: { verdict: 'sequence', column: 'Ready', blockedBy: [], detail: 'expected conflict with PR #44' },
  })
  seq.labels.includes('build:#7') && fail('#7 waits on #50 and must not build this wave')
  seq.labels.includes('build:#8') || fail('#8 has nothing to wait on and must build')
  ;(by(seq.out, 7).finalStatus === 'sequenced' && by(seq.out, 7).column === 'Ready')
    || fail('#7 must be sequenced@Ready: ' + JSON.stringify(by(seq.out, 7)))
  const pf = seq.prompts[seq.labels.find((l) => l.startsWith('preflight'))]
  ;(/waits-on/.test(pf) && /blocked-by: <N>/.test(pf) && /Never move it to Blocked/.test(pf))
    || fail('pre-flight must be told: waits-on label + blocked-by line, never Blocked')
  // A sequence with no column and no blockedBy at all is not built either (not a go).
  const bare = await run({ configPath: 'c.json', cards: [card(9)] }, { 9: { verdict: 'sequence', detail: 'unclear' } })
  bare.labels.includes('build:#9') && fail('#9 sequence with no blockedBy/column must not build')

  // 12 — exits 7/8 land in Approve, exit 3 in Blocked with blocked:github; the
  //      summary carries the per-column tally the orchestrator reports.
  const rv = await run({ configPath: 'c.json', cards: [card(6, 'Review')] }, {})
  const rp = rv.prompts['review:#6']
  ;(/Exit 7[^\n]*Approve \(NOT Blocked\)/.test(rp) && /Exit 8[^\n]*Approve/.test(rp) && /blocked:github/.test(rp))
    || fail('the Reviewer must route exits 7/8 to Approve and 3 to blocked:github')
  ;/\[ \]/.test(rp) && fail('the review prompt must not render a checkbox')
  ;(rv.out.columns && rv.out.columns.Done === 1) || fail('summary must tally columns: ' + JSON.stringify(rv.out.columns))
})().catch((e) => fail(e.stack))
JS
echo "PASS: test-wave-preflight.sh (12 scenarios)"

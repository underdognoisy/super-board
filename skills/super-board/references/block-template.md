# Block exit template

`Blocked` is an exit ramp for REAL stops, not a workflow step. There is no `Skipped` column
(removed in v3.0.0). Since v3.2.0 two states that used to share it have their own place:

- **Waiting on your merge decision** (merge gate exit 7 / 8) → the **`Approve`** column, with the
  🙋 request below. Not Blocked.
- **Waiting on another open card** (pre-flight `sequence`) → stays in **`Ready`** with the
  `waits-on` label. Not Blocked.

## When / who moves cards there

| Where | When | Who |
|---|---|---|
| Approve | Review passed; a human decides the merge (gate exit 7 / 8) | Reviewer |
| Ready + `waits-on` | Pre-flight: same files as an open card/PR — waits on that issue | Pre-flight / Builder |
| Blocked + `blocked:<reason>` | A real stop: decision, failed review, access, GitHub refusal, other | Any lane, from any workflow column |
| Done (closed as not planned) | Card dropped on purpose: out of scope, won't do | Any lane; 🤷 comment below |

Once moved, the card waits. **A card blocked only on other cards no longer waits for a human:**
the wave planner sweeps `Blocked` at the start of every wave, and any card whose `## Blocked by`
issues have all closed is moved back to `Ready` automatically, with a comment saying what cleared
it. Everything else — credentials, permissions, product decisions — still waits for a person.

That sweep is why the `blocked-by:` line below is mandatory. Before it existed, `Blocked` was
terminal: on 2026-08-20 five cards sat there long after their blockers had merged, because the only
record of what they were waiting for was English prose in a comment nobody re-read.

## Reason labels (mandatory on every transition into Blocked)

Every move to Blocked adds **exactly one** reason label, so the mini-card says why without being
opened. Pick it from the Block comment's reason tag:

| Label | Reason tags | Examples |
|---|---|---|
| `blocked:decision` | ❓ 🧑 🎨 🛡 (sign-off) · Builder spec verdict **dev-led** | spec gap, too big to build, product/design call, destructive change needing sign-off |
| `blocked:failed-review` | rebuild cap hit · 🛡 truth-check below threshold / inconclusive | same root cause N times, Reviewer could not confirm a finding |
| `blocked:needs-access` | 🔐 🔑 💳 | missing secret / env / credential, permission, paid quota |
| `blocked:github` | merge gate exit 3 | GitHub refused the merge (closed PR, branch protection, required check) |
| `blocked:other` | 👯 📦 · a legacy ⏳ card still in Blocked · anything else | duplicate in progress, wrong board |

When the card leaves Blocked — the sweep, a person dragging it, a `changes:` bounce — remove its
`blocked:*` label in the same step. The planner's `unlabelled` list names Blocked cards with none
(legacy cards) so the orchestrator can add one from the comment's tag; its `sweep` entries carry
`removeLabels`. A card never carries two reason labels, and an Approve card carries none.

## Required Block comment template (mandatory on every transition into Blocked)

The bot must write a structured comment on **both the issue and the PR** (if a PR exists) explaining *why* it moved the card and *what it couldn't safely decide*. Exception: a 🙋 merge approval uses one canonical issue request below; the PR links to it without duplicating its machine lines. Format:

```
[<role>] [blocker] 🛑 blocked · <reason emoji> <one-line reason>
Card:        #<N> <title>
PR:          #<P> (if exists)
Reason tag:  <emoji from table below>
Why blocked: <concrete; 1 line — name the specific thing that is missing or wrong>
Evidence:    <the command + output, file:line, or error that shows it — not a paraphrase>
Checked:     <what the bot verified before blocking, so nobody re-checks it — or "-">
What blocks: <what specific external action would change this — credentials, perms, decisions>
Why I (bot) cannot decide:
             <one line explaining the decision the bot refuses to make on its own —
              "involves billing config; this is a customer money decision",
              "requires choosing between two valid auth providers; ambiguous from spec",
              "would drop a Postgres table; destructive, needs human sign-off">
To unblock:  <concrete action the human can take, one plain step per line — no checkboxes;
             ticking a box is not a signal the board reads>
Owner:       <who acts next — the repo owner's GitHub login, "repo admin", "design" — never "someone", never a literal example name>
Move back:   drag this card to Ready after the steps above are done
blocked-by:  <comma-separated issue numbers, or "-" if nothing on this board clears it>
```

One line per field. The header is the comment header of writing-standard.md § 4: `<role>` is
the lane that writes it (`builder` · `qa` · `reviewer` · `collect` · `orchestrator`). The Block
template is the one comment allowed past 8 lines — its fields are what the human and the sweep
need. The reader is a human deciding in ten seconds whether this is theirs: lead with the fact,
show the evidence, name the owner. No narration of what the bot tried in what order. Run the
prose through `humanizer` when installed.

### The `blocked-by:` line is mandatory

Last line of the block, always present, machine-read. It sits alongside the other machine lines the
lanes already emit (`root-cause-hash:`, `gh-quota-on-exit:`, `move-mutation-result:`) and follows the
same rule: **prose above for the human, one parseable line below for the loop.**

- `blocked-by: 32, 91` — this card returns to `Ready` the moment both close. The sweep does it.
- `blocked-by: -` — nothing on this board clears it. It waits for a person, and the sweep leaves it
  alone. Use this for every `🔐`, `💳`, `🔑`, `🧑`, `🎨` block and every 🙋 request.

Write the numbers alone. **Never `blocked-by: none — but #26 must merge first`**: a line that says
none and then names an issue is read as *no blocker* by the sweep and as *one blocker* by a human,
and the sweep is the one that acts. That exact shape shipped on a real board and is the reason
`super-board-deps.sh` refuses to guess at it.

The same rule governs the issue body's `## Blocked by` section, which is where the sweep looks when
a card has no block comment yet. Bullets of the form `- #N — why`, or a single `- None.` — nothing
else parses.

A card dropped on purpose uses the same template with the header `[<role>] [report] 🤷 dropped ·
<reason>`, replaces `Why blocked` with `Why dropped` and `What blocks` with `Why out of scope`, is
closed as not planned (`gh issue close <N> --reason "not planned"`) and moves to Done.

## Reason emoji vocabulary

| Emoji | Class                       | Examples                                                                 |
|-------|-----------------------------|--------------------------------------------------------------------------|
| 🔐    | Credentials / secrets       | missing API key, expired token, no test login                            |
| 💳    | Billing / quota             | paid API rate-limit hit, free tier exhausted, requires plan upgrade      |
| 🔑    | Permissions / access        | gh scope denied, org admin required, write access missing                |
| ❓    | Ambiguity / spec gap        | two valid interpretations, AC contradicts PROJECT.md, dependency unclear |
| 🛡    | Safety / destructive        | would drop a table, would push to prod, would rotate live secrets        |
| 🧑    | Human review needed         | unresolved human PR comment, design decision, branding choice            |
| 🤷    | Out-of-scope                | wrong project, deferred to other milestone, manual-only ticket           |
| 📦    | Wrong-place                 | belongs on a different board / repo                                      |
| 🎨    | Pure design                 | no measurable AC; needs design pass first                                |
| 👯    | Duplicate                   | pre-flight: a merged PR already delivers it, or an open PR / card is building it |
| ⏳    | Sequenced                   | pre-flight: an open PR touches the same files — the card stays in **Ready** with `waits-on` (not a Block) |
| 🙋    | Needs you (human-only step) | merge gate exit 7 (merge_policy: money/auth/schema/size — review and merge it, or drag the card to Done / comment `done` to approve) or exit 8: a migration for a DB the robot may not touch, an allowed migrate command failed, a `needs-you:` step in the PR body, `migrations.human_steps` — the card goes to **Approve**, not Blocked |

## 🙋 Needs you — the Approve column

Merge gate exits 7 and 8 move the card to **`Approve`** (v3.2.0; before, Blocked), tagged 🙋, with
the `needs-you` label on the issue and the PR, and no `blocked:*` label. Exit 7: merge_policy routes
the merge to a human — review and merge it yourself, or approve it and let the next wave merge it:
drag the card to **Done** on the board, or comment `done`. Exit 8: migrations against a database
outside `migrations.allowed_envs`, an allowed migrate command that failed, a declared human step.
The column only says where the human looks; approval is still head-bound and trusted. Moving a card
INTO Approve grants nothing.

**Drag to Done = approval (v3.3.0).** A project card cannot carry a button, so the board gesture
is the approval: a card dragged Approve → Done after the request, issue and PR still open, with the
request's head still the PR head, is approved exactly like a `done` comment — the planner reads the
board, the orchestrator moves it Done → Review with a `drag-approval:` record, and the Reviewer
merges through the gate. A standing `changes:` or an unanswered free-text comment still blocks it;
a drag before the request, on an edited request, or after the head moved never approves (the card
returns to Approve or Review with a comment saying why). Until it merges the card shows as
`approved · merging`.

What to do first, why folded away. **No checkboxes.** On 2026-10-12 the owner ticked
`- [ ] Review and merge PR #18` and nothing happened: a ticked box is not a signal the board reads.
The request states the one signal in plain words. Exit 7 adds `Review PR #<P>, or merge it yourself.`
above it. Include any `needs-you:` commands printed with that policy hold too: the approval covers
those human steps as well as the code.

```
[reviewer] [blocker] 🙋 Your turn on #<N> — <title>
Run `<exact command 1, copy-paste ready>` on the **<env>** database.
<exact command 2, if any>
Drag this card to **Done** on the board, or comment `done` here, to approve and merge — or `changes: <what>` to send it back.
After a trusted human confirms this version, the next wave returns it to Review for verification.

<details><summary>Why, and what I checked</summary>

PR #<P> <one line — e.g. "adds prisma/migrations/0042_add_plan">. <env> isn't in the databases the robot may migrate.
Tests green on <base>@<sha>; migrated <test, staging>.
Evidence: <the gate's `needs-you:` lines, verbatim>
Reason tag: 🙋 needs you · Owner: <repo-owner login | repo admin>
approval-request: <copy the gate's exact JSON here>
blocked-by: -
</details>
```

Example (what the person sees on GitHub):

> **🙋 Your turn on #812 — Add plan column**
> Run `npx prisma migrate deploy` on the **live** database.
> Drag this card to **Done** on the board, or comment `done` here, to approve and merge — or `changes: <what>` to send it back.
>
> After a trusted human confirms this version, the next wave returns it to Review for verification.
> ▸ Why, and what I checked

The `Reason tag:` and `blocked-by: -` lines stay (inside the fold): the planner reads them
(`super-board-deps.sh` → `needsYou`, `humanGated`). The issue and the PR get the `needs-you` label.

**The commands are exact.** Copy them from the gate's `needs-you: <command>` lines;
never paraphrase ("run the migration on prod"). A placeholder such as `<your live migrate command>`
means the config has no command for that env — say so in the fold and name the config key
(`migrations.commands.live`).

**Approval request.** Copy the gate's `approval-request:` line verbatim into this
issue comment. It records the full PR head and the exact policy/human steps being
confirmed. The issue must be linked by the PR's closing reference (`Closes #N`).
For a standalone PR, post the canonical request on the PR instead. Keep a single
request location: the PR's status comment links to the issue request and does not
repeat `Reason tag: 🙋` or `approval-request:`. Never edit a request after posting;
post a new one when the code, steps or actual human question changes. Do not
repost the same pending request on every poll or retry. Generic credential or
product blocks without a PR remain manual; a bare `done` does not automate them.

**Resume.** A human with current repository write, maintain or admin permission
comments `done` after the newest request, in the same thread. This supports a solo
owner approving their own PR. The planner verifies identity, permission, linked
PR and current full head before putting the card in `resume`; the Reviewer then
re-runs the merge gate. The gate also verifies the current policy and human steps,
re-verifies against the base, re-runs allowed migrations, and rechecks approval
just before merging. Changed code, a newer human block, edited evidence, missing
permissions or unreadable GitHub evidence keep it on hold. A still-failing command
also sends it back here. Old bare comments and `needs-you:done` labels never count;
legacy blocked cards need one fresh pinned request and a fresh human reply. Move
those legacy cards to Review once to generate it. If a PR changes while still
in Approve, the planner's `refreshApproval` sends it to Review to create the new
request automatically; it does not mark the new code approved. A 🙋 card still
in Blocked from before v3.2.0 is read the same way as an Approve card.

**A `done` before the request does not count — and the human is told once.** A
trusted `done` / `approve` / `merge` posted BEFORE the current pinned request is
never approval (it may predate the code it would approve). The planner reports
it in `staleDone`; the orchestrator posts, once:

```
[reviewer] [report] ⚠️ your `done` at <time> came before this request — comment `done` again to approve
stale-done: <comment id>
```

The `stale-done:` line dedups the notice; it suppresses only a repeat notice,
never approves anything.

**Trust boundary.** GitHub Bot accounts cannot supply human approval. GitHub cannot
distinguish a person from an agent using that person's token; agents must never
write the human's `done` response. The final approval check is a fresh snapshot;
GitHub atomically protects the head at merge, but does not lock issue comments.
A human can still merge a PR manually under the repository's normal rules.

## Hard rule

**The bot is forbidden from moving any card to Blocked, or dropping it to Done, *without* this full template populated — and, for Blocked, exactly one `blocked:<reason>` label. A 1-line "needs creds" comment is a contract violation and fails Reviewer's thread gate.**

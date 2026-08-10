# 003 — The review gate's real guarantee is not the one it appears to give

```
status:   in-progress
kind:     fix
source:   /sdd-release v0.7.0 run, 2026-08-07; fourth hole from PR #21, 2026-08-08
issue:    #10
axis:     correctness
found:    2026-08-07
saving:   -  (correctness of the gate itself)
risk:     medium — the gate is the thing that is wrong, so a bad fix fails open
```

Four holes, one theme: what the gate **actually** guarantees and what a reader
takes it to guarantee have come apart. Three make it weaker than it looks; the
fourth makes it look stronger. The `show` fix (step 0) is two lines and
independent of the rest.

## Outcome — steps 2-5 done 2026-08-10

ADR: *2026-08-10 — The gate admits by PR, not by whatever is checked out*.
All four holes are now closed (step 0 landed earlier in PR #24).

**Measured after:** full suite green offscreen (**935 tests, exit 0**), `ruff check`
clean, `rules.py check` ok at 37 rules, `plan.py validate` ok.

**The reframing that shaped the fix.** This ticket described three holes as "fails
open — the merge proceeds". Under the opt-in redesign only one of them bites: a
*missing* verdict already merges, so a mis-bound `pass` is indistinguishable from no
verdict at all. What genuinely broke is a recorded **`block`** becoming findable only
by accident. Demonstrated before the fix, same PR and same recorded block: **exit 2
from the branch tip, exit 0 from `main`.** So the fix is ref-lookup first, sha-binding
second — the reverse of the emphasis here.

**Plan step 5 contradicted this ticket's own verification list.** Step 5 asked that a
verdict whose sha no longer matches "must block"; the verification list asked that
pushing a commit "reopens" the gate. The verification list won — blocking on staleness
would make a reviewed-then-amended PR harder to merge than an unreviewed one, the
perversity ticket 008 already reversed once. Step 5's wording is the pre-opt-in
assumption surviving inside the plan of the ticket whose fourth hole is that same
assumption surviving inside `show`.

**Not in the plan, and needed:**

- `show` had to be **rewritten rather than extended**. The first attempt listed the
  other verdicts and then printed "nothing will be blocked" underneath a live block —
  hole four reintroduced in the commit fixing the rest. Caught by running it.
- The tokeniser went to a new leaf `_shell.py`, not imported from `branch_lease`:
  that would drag `_session` into `review_gate`'s import path (26.6ms + a registry
  read), and `review_gate` imports `_session` lazily on purpose because a
  module-scope `ImportError` in a `PreToolUse` hook raises on every shell call.
- **Two registry rules added.** `rules.py scope .claude/hooks/review_gate.py`
  returned exactly one rule, and since ticket 010 the registry is the only thing
  `plan.py` reads — so an unenumerated discipline is invisible to every future brief.
  `HOOK-FAILS-OPEN` and `HOOK-CLASSIFIES-ARGV`, both scoped `.claude/hooks/**/*.py`.

**Verified against real `gh`, not only stubs** (every test stubs `pr_head` for
hermeticity): PR #27 resolved to head `92ef5c3` on `chore/close-ticket-002` while
local `HEAD` was `58dbf29` — the hole in one line — and a block recorded for #27
stopped `gh pr merge 27` from that checkout while leaving `gh pr merge 99` alone.

## The finding

`review_gate.py` keys a recorded verdict on `git rev-parse --short HEAD` in the
**local checkout** (`hook()` at `.claude/hooks/review_gate.py:87`, `cmd_record`
at `:152`). What it should key on is **the commit that would actually merge**.

Those are the same thing only when you happen to have the PR's branch checked
out. Standing anywhere else — most obviously on `main` — the verdict attests to
the *base*, and the gate opens for a PR whose content nothing reviewed.

**Two merges in the v0.7.0 run demonstrated both behaviours:**

| PR | checkout at merge time | verdict bound to | judged what merged? |
|---|---|---|---|
| #8 | `main` | `56c966a` (main's tip) | **no** — the panel judged `05898b7` on the branch |
| #9 | `chore/changelog-v0.7.0` | `618d339` (the branch tip) | yes |

The same gate gave two different guarantees on consecutive merges, decided
entirely by which directory the command ran from. Nothing in the output says
which one you got.

## The second, larger hole

`cmd_record` stores `ref` (the PR number, `:159`) and **`hook()` never reads
it.** The lookup is by sha alone. So a verdict recorded for PR #8 opens the gate
for merging PR #99, as long as `HEAD` has not moved. The `ref` field is
currently decoration.

Combined, the gate's real guarantee today is: *somebody recorded a verdict while
standing on this commit*. That is much weaker than it looks, and it fails
**open** — the merge proceeds — which is the worst direction for a gate to fail.

## A third hole in the same patterns: the guard false-positives

Found while filing this very ticket. `GUARDED` (`.claude/hooks/review_gate.py:42`)
matches `\bgh\b[^\n]*\bpr\b[^\n]*\bmerge\b` against the **whole command string**,
with no anchoring and `[^\n]*` spanning freely. So this was blocked:

```
gh pr create ... --body "... opens the gate for merging any other ..."
```

A `gh pr create` whose body happens to contain the word "merge" is treated as a
merge. Nothing was being merged. The workaround is `--body-file`, which keeps the
prose out of the command string — but a guard you route around is a guard that
teaches people to route around it.

The same looseness cuts the other way: `gh pr merge` split across a shell
variable, or invoked as `hub`/the API, is not matched at all. **The pattern is
simultaneously too broad and too narrow**, which is what happens when a security
boundary is a substring search over a command line.

Fixing this belongs with the rest of this ticket: the patterns have to be
rewritten anyway to capture the PR number.

## A fourth hole: `show` reports a guarantee the gate no longer makes

Found merging PR #21 on 2026-08-08. `review_gate.py show` said:

```
  no review on record — the gate will block a merge or a close
```

Then `gh pr merge 21 --squash` merged, unblocked. That reads as the gate failing
open — it was reported as such at first — and it is not. **The gate was right and
`show` was lying.**

`hook()` returns 0 when no verdict exists (`.claude/hooks/review_gate.py:174`),
with a comment saying exactly why: the opt-in redesign (ed4131d) made a missing
panel legal, and only a recorded `block` stops anything. `cmd_show` (`:370`) was
not updated with it and still prints the pre-opt-in promise. It also **exits 1**
for that state, so a scripted `show` treats the normal condition as a failure.

Nothing about the merge path is at fault: `GUARDED` matches `gh.exe pr merge …`
(`\bgh\b` matches inside `gh.exe` — the `\` and `.` are both boundaries), the
hook is registered for `Bash|PowerShell` in `.claude/settings.json`, it fired,
and it correctly allowed the call. `test_no_panel_on_record_does_not_block`
already asserts this.

This belongs in this ticket because it is the same defect class as the other
three: **the gate's actual guarantee and its apparent guarantee have drifted
apart.** Here the drift is in the direction that makes the gate look *stronger*
than it is, which is how a reviewer comes to trust it for something it never
promised — and it is the cheapest of the four to fix.

The fix is two lines and carries no design question, so it need not wait for the
rest of this ticket. Say what the gate does — no panel on record, so nothing will
be blocked; run the critics if this diff warrants one — and return 0.

## Area of impact

| File | Line | What changes |
|---|---|---|
| `.claude/hooks/review_gate.py` | 42-45 | `GUARDED` patterns: capture the PR number, and stop matching `gh pr create` whose body mentions merging |
| `.claude/hooks/review_gate.py` | 73-113 | `hook()` resolves the target of the merge instead of reading local `HEAD` |
| `.claude/hooks/review_gate.py` | 119-171 | `cmd_record` binds to the PR head sha, not local `HEAD` |
| `.claude/hooks/review_gate.py` | 364-378 | `cmd_show` states the post-opt-in guarantee and returns 0 with no verdict on record |
| `.claude/skills/sdd-ticket/SKILL.md`, `.claude/skills/sdd-release/SKILL.md` | — | the recorded panel instructions if the command shape changes |
| `tests/test_review_gate.py` | 22 tests | extend; **no longer new** — added by ed4131d, see the plan |

## Approach

**Chosen: bind to the PR head sha, and match the ref.**

1. At `record --scope pr-merge --ref N`, resolve the PR's head commit
   (`gh pr view N --json headRefOid`) and key the artifact on **that**, not local
   `HEAD`. Record time is not latency-critical, so a network call is affordable
   there.
2. Extend the `GUARDED` patterns to capture the PR number out of the command, so
   `hook()` knows which PR is being merged.
3. `hook()` looks the verdict up **by ref**, then checks the recorded head sha —
   offline, fast, deterministic, inside the hook's 20s budget.
4. `ticket-close` keeps binding to `HEAD`: closing a ticket is a statement about
   work that has already landed, so the checkout is the right referent. Say so in
   a comment rather than leaving it as an inconsistency someone later "fixes".

### Rejected alternatives

- **Have the hook resolve the PR head itself.** Correct, and it puts a network
  call on every guarded command inside a 20s timeout. A gate that intermittently
  times out is a gate people disable.
- **Match on `ref` alone and drop the sha.** Cheap, and it loses the property
  that pushing a new commit reopens the gate — which is the reason sha-binding
  exists.
- **Require the branch to be checked out before merging.** Would make the current
  behaviour correct, but it is an unenforceable convention, and the failure mode
  stays silent when someone forgets.
- **Leave it and document it.** What is in place now. The gap is invisible at the
  point of use, and a gate whose guarantee depends on your working directory is
  one that will be trusted more than it deserves.

## Plan

0. ~~**Do the `show` fix on its own, first.**~~ **Done** — `00e53d7..`, PR #24.
   `cmd_show` states what `hook()` actually does and returns 0 with no panel on
   record; it also now names the lanes that would block, so a verdict list is
   never again read without its consequence. Three tests in
   `tests/test_review_gate.py`. When step 3 lands, add the binding to this output.
1. ~~Add `tests/test_review_gate.py`. **There is none today**~~ — **done**, by
   ed4131d: 22 tests, including `test_no_panel_on_record_does_not_block`, which is
   the regression floor this ticket's step 0 needs. Steps 2-5 extend that file
   rather than create it.
2. Capture the PR number in `GUARDED`; keep the existing scope detection.
3. Resolve and store the PR head sha at `record`; fall back to local `HEAD` with
   an explicit `"binding": "local-head"` field in the artifact when `gh` is
   unavailable, so a weaker guarantee is legible rather than silent. **Decided —
   see above**; `show` prints the binding too.
4. `hook()` matches ref → recorded verdict → head sha. A `local-head` artifact
   attests to the checkout only: it may open the gate when `HEAD` is the PR head
   and must not otherwise.
5. Negative tests, per `CHECKER-NEEDS-NEGATIVE-TEST`: a verdict for PR #8 must
   **not** open a merge of PR #9; a verdict whose head sha no longer matches the
   PR must block; a `ticket-close` must still bind to `HEAD`; and
   `gh pr create --body "...merging..."` must **not** be guarded.

## Verification

- ~~`show` with nothing on record: exit 0, and the text says no panel was run
  rather than promising a block.~~ **Done in step 0.**
- A `local-head` artifact opens the gate when `HEAD` is the PR head, and blocks
  when it is not. `show` prints the binding either way.
- Record a verdict for one PR, attempt to merge a different one, confirm it
  blocks. This is the case that passes today and should not.
- Record, push a commit to the PR, confirm the gate reopens.
- `ticket-close` behaviour unchanged.
- Full suite green.

## Decided — `gh` unreachable at record time

**Record it with `"binding": "local-head"` and carry on.** Settled 2026-08-08.

Failing closed was the alternative: refuse to record a verdict the tool cannot
bind to the PR head. It is the stronger guarantee and the wrong trade here. This
app is offline-first and single-user, the panel is already opt-in, and the
failure lands on someone who has *just spent ~100k tokens* running four critics —
telling them the result cannot be recorded because GitHub is unreachable throws
that away and teaches them to route around the gate. A gate people disable is
worth less than a weak one they keep.

So the weak binding is admitted and **named in the artifact**, which is the point:
today's guarantee is exactly this weak and says nothing at all. Two consequences
follow, and they are what make it safe rather than merely convenient:

- `hook()` must treat a `local-head` artifact as attesting to the checkout only.
  It may open the gate when `HEAD` *is* the PR head, and must not when it is not —
  the case that silently passes today.
- `show` must print the binding, for the same reason step 0 exists: a weaker
  guarantee has to be legible at the point someone reads it.

This resolves the ticket's only open question; steps 2-5 are unblocked.

## Notes

- All four holes were found by *using* the gate, not reading it — as was the
  `**` scope bug fixed in PR #8. Every one was in the dispatch and admission
  machinery rather than in any critic. The critics have been the reliable part;
  the plumbing around them has not.
- The fourth is a **reporting** bug, not a gating one, and it was first written up
  as the gate failing open. Worth remembering when reading the other three: check
  what the code does before believing what it says about itself. In this instance
  the code was right and the message was wrong.
- Worth doing **before** the gate is trusted for anything consequential. Right
  now two tickets and one release have passed through it, all reviewed by a human
  who knew its limits.

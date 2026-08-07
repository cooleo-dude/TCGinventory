# 003 — The review gate binds a verdict to the checkout, not to what is being merged

```
status:   open
kind:     fix
source:   /sdd-release v0.7.0 run, 2026-08-07
issue:    #10
axis:     correctness
found:    2026-08-07
saving:   -  (correctness of the gate itself)
risk:     medium — the gate is the thing that is wrong, so a bad fix fails open
```

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

## Area of impact

| File | Line | What changes |
|---|---|---|
| `.claude/hooks/review_gate.py` | 42-45 | `GUARDED` patterns must also capture the PR number from the command |
| `.claude/hooks/review_gate.py` | 73-113 | `hook()` resolves the target of the merge instead of reading local `HEAD` |
| `.claude/hooks/review_gate.py` | 119-171 | `cmd_record` binds to the PR head sha, not local `HEAD` |
| `.claude/skills/sdd-ticket/SKILL.md`, `.claude/skills/sdd-release/SKILL.md` | — | the recorded panel instructions if the command shape changes |
| `tests/` | new | there is no test for `review_gate.py` at all today — see below |

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

1. Add `tests/test_review_gate.py`. **There is none today** — the gate was
   verified by hand with eleven piped-JSON cases that live only in a transcript.
   Port those, so the fix has something to regress against before it changes
   anything.
2. Capture the PR number in `GUARDED`; keep the existing scope detection.
3. Resolve and store the PR head sha at `record`; fall back to local `HEAD` with
   an explicit `"binding": "local-head"` field in the artifact when `gh` is
   unavailable, so a weaker guarantee is legible rather than silent.
4. `hook()` matches ref → recorded verdict → head sha.
5. Negative tests, per `CHECKER-NEEDS-NEGATIVE-TEST`: a verdict for PR #8 must
   **not** open a merge of PR #9; a verdict whose head sha no longer matches the
   PR must block; a `ticket-close` must still bind to `HEAD`.

## Verification

- Record a verdict for one PR, attempt to merge a different one, confirm it
  blocks. This is the case that passes today and should not.
- Record, push a commit to the PR, confirm the gate reopens.
- `ticket-close` behaviour unchanged.
- Full suite green.

## Open question — for the user

**What should the gate do when `gh` is unreachable at record time?** Failing
closed makes the gate unusable offline; failing open with a recorded
`"binding": "local-head"` keeps today's weaker guarantee but makes it visible in
the artifact. I lean to the second — a legible weak guarantee beats an invisible
one — but it is a judgement about how much friction the gate is allowed to add.

## Notes

- Found by using the gate on a real release, not by reading it. The same is true
  of the `**` scope bug fixed in PR #8; both were invisible failures in the
  dispatch and admission machinery rather than in any critic.
- Worth doing **before** the gate is trusted for anything consequential. Right
  now two tickets and one release have passed through it, all reviewed by a human
  who knew its limits.

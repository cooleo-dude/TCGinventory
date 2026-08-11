# The merge request is the ticket

```
status:   in-progress
kind:     feature
source:   /task-planning 2026-08-10
mr:       none
axis:     maintainability
found:    2026-08-10
saving:   -
risk:     medium — rewrites the ticket tool, the gate's ref handling and four skills
```

## The finding

One unit of work carries **three unrelated numbers**: our ticket `NNN`, the GitHub
issue `#N` that mirrors it, and the GitHub PR `#N` that lands it. Nothing derives
one from another, so every cross-reference is a lookup. Ticket `003` is the
worked example — issue `#10`, then PR `#35`, then a second PR `#39`.

Measured on the repo itself, the two GitHub sequences are one interleaved counter
with no overlap:

```
issues:  2  7 10 12 13 14 16 17 22 31 32 36
PRs:     1  3  4  5  6  8  9 11 15 18 19 20 21 23 24 25 26 27 28 29 30 33 34 35 37 38 39 40
```

That is not a convention problem, it is arithmetic: GitHub allocates issues and
PRs from a single per-repo counter, so a work item that creates **both** can never
have them agree. `sdd-ticket/SKILL.md` already documented this as a caveat to live
with ("Ticket numbers and issue numbers are **not** the same sequence").

The desync has two concrete costs beyond the reading friction:

1. **`review_gate.py` overloads one flag with two sequences.** `--ref` is a GitHub
   PR number under `--scope pr-merge` (`classify`, line 233) but our ticket number
   under `--scope ticket-close` (line 239), while admission keys on `(scope, ref)`.
   Undocumented, and the kind of thing ticket 003 was filed about.
2. **`claim` exists to work around a race that GitHub does not have.** Ticket
   numbers are allocated locally, so two sessions can both read `007`; the fix was
   `taken_numbers()` over every ref plus `O_EXCL` marker files in the git common
   dir. A number issued by GitHub cannot collide in the first place.

## Area of impact

| File | Line | What changes |
|---|---|---|
| `.claude/skills/sdd-ticket/scripts/ticket.py` | 138–162 | `taken_numbers()` deleted — GitHub allocates |
| `.claude/skills/sdd-ticket/scripts/ticket.py` | 165–172 | `next_number`/`cmd_next` deleted |
| `.claude/skills/sdd-ticket/scripts/ticket.py` | 175–213 | skeleton: `issue:` → `mr:`, heading carries the sigil |
| `.claude/skills/sdd-ticket/scripts/ticket.py` | 216–282 | `reservations_dir`/`cmd_claim` → `cmd_open` |
| `.claude/skills/sdd-ticket/scripts/ticket.py` | 285–321 | `cmd_check`: validate sigil and `mr:` agreement |
| `.claude/skills/sdd-ticket/scripts/ticket.py` | 328–378 | `cmd_push` → rewrite the MR body, not an issue |
| `.claude/skills/sdd-ticket/scripts/ticket.py` | 381–459 | `cmd_close`: verify merged, no file deletion dance |
| `.claude/hooks/review_gate.py` | 233, 239 | both scopes take one referent — the MR number |
| `.claude/hooks/review_gate.py` | 448–462 | `--inherit`'s same-ref guard simplifies |
| `tests/test_ticket_claim.py` | all | → `test_ticket.py`; race tests go with the machinery |
| `tests/test_review_gate.py` | ref tests | follow the referent collapse |
| `.claude/skills/sdd-ticket/SKILL.md` | 16–29, 227 | new *Numbering* section; the caveat stops being true |
| `.claude/context/tickets/README.md` | 52–106 | Format, Status, Resolving one |
| `.claude/skills/task-planning/SKILL.md` | 121–134, 151, 163 | the brief opens an MR; close-out drops the issue |
| `.claude/skills/sdd-release/SKILL.md` | 57, 60–91 | stale gate section corrected in the same pass |
| `.claude/context/app-map.json` | `.workflow.branch` | the branch/PR convention |

## Approach

**Chosen: the merge request *is* the ticket.** `X` is the GitHub PR number; `X#`
names the specification and `X!` names the change that satisfies it. They are the
same object, so they cannot drift — an identity rather than a convention someone
has to maintain.

A ticket is therefore a branch with a pushed draft MR carrying the ticket file.
`ticket.py open <slug>` writes `draft-<slug>.md`, commits, pushes, opens the draft
MR, and renames the file to the number GitHub handed back. Issues are retired;
`deferred` survives as a draft MR left open, which is a better backlog than an
issue because it carries the branch and the plan with it.

The ticket file **stays on its branch and never lands on `main`**: if the MR is
the ticket, merging the MR closes it and there is nothing to clean up. That also
retires the `Tickets: close N` follow-up commits (`#33`, `#38`, `#39`) which exist
only to delete a file from `main`.

### Rejected alternatives

- **`X` = the issue number** (keep the mirror, drop only our `NNN`). Halves the
  problem — ticket and issue finally agree — but `X!` stays a label whose number
  does not match the real PR, so the sigil is decorative. Rejected because the
  stated goal was alignment, not less misalignment.
- **Keep three counters, tie them by convention only** (`X#`/`X!` as labels over
  our own `NNN`). Costs nothing to adopt and fixes the reading friction, but
  leaves `claim`'s locking and the gate's overloaded `--ref` exactly as they are.
- **Reserve consecutive issue/PR pairs.** Impossible: the counter advances on
  every create, by anyone, including bots.
- **Drop GitHub entirely and keep only local files.** Loses the phone-visible
  queue, which is the stated reason the mirror exists.

## Plan

1. `ticket.py`: `open` replaces `claim`/`next`; delete the reservation machinery;
   `issue:` → `mr:` in `REQUIRED`; sigil in the skeleton, the MR title and the
   README index.
2. `ticket.py check`: validate the sigil form and that `mr:` agrees with the
   filename. `close`: refuse unless the MR is merged.
3. `tests/test_ticket.py`: negative tests first — malformed sigil, `mr:`
   disagreeing with the filename, `close` on an unmerged MR.
4. `review_gate.py`: collapse the two `--ref` referents; simplify the `--inherit`
   guard. Update `tests/test_review_gate.py`.
5. Docs: `sdd-ticket/SKILL.md` (new *Numbering* section), `tickets/README.md`,
   `task-planning/SKILL.md` steps 5–7, `app-map.json` `.workflow.branch`, and the
   stale review-gate section of `sdd-release/SKILL.md`.
6. Migrate: issues `#12`, `#13`, `#14` (deferred) and `#16` (open) become draft
   MRs; close the old issues pointing at them.
7. ADR in `docs/decisions.md`.

## Verification

- `ruff format .` and `ruff check .` clean.
- Full suite with `QT_QPA_PLATFORM=offscreen`, including the new negative tests.
- `python .claude/hooks/rules.py check` green — no rule seals any anchor edited
  here, which is why `app-map.json` `.workflow.branch` is safe to touch.
- `ticket.py check` passes against the migrated tickets.
- This ticket's own MR number is the first `X!`, so the flow is proven by use.

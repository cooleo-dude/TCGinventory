# 010 — Planning reads the rule registry, not the decision log

```
status:   done
kind:     optimization
source:   conversation, 2026-08-09 (while landing ticket 001)
issue:    none
axis:     maintainability
found:    2026-08-09
saving:   MEASURED across all 11 windows: ~31,900 -> ~2,774 tokens per planning
          run (-91.3%), and the cost no longer grows with project age
risk:     low — no app code; changes what a dossier prints and what the skill
          tells the agent to read
branch:   ticket/010-planning-reads-rules-not-log
```

## Result

Landed 2026-08-10. Mean dossier across every window: **31,899 → 2,774 tokens**.
The rules block is ~1,200 tokens of that and is flat in project age; the log is
no longer read at all.

Four things turned up that this ticket did not anticipate:

1. **Four windows had no spec of their own** — `price_lookup`, `auctions`,
   `models` and `operations` cite `docs/decisions.md` *as their spec*, so
   deleting the required-read row would have silently dropped their design notes.
   They now cite an **exact heading**, resolved to a line range, with
   `plan.py validate` failing if the anchor is missing or the heading moves.
   Matching by date was tried first and is far too coarse: `2026-07-10` alone
   carries ten entries, and a price-lookup dossier filled with ADRs about ripping
   intent and import/export.
2. **`rules.py show` had to be capped.** `BLAST-RADIUS` is anchored at the
   `windows` subtree of `app-map.json` — a single ~20,000-character JSON line.
   Printed whole it was ~21 KB, which would have made the replacement as
   expensive as the thing replaced. Capped at 80 lines / 400 chars per line,
   and it says what it withheld.
3. **The seal fired, correctly.** Editing the map broke `BLAST-RADIUS`'s
   `excerpt_sha256`. The rule ("windows share tables…") was unaffected — only
   `specs` strings changed, no window's `tables` or `services` — so it was
   resealed deliberately, which is what the mechanism is for.
4. **`rules.py` crashed on Windows before printing an anchor.** ADR headings
   carry `—` and `→`; the console defaults to cp1252. `show` is the first
   command to print anchors routinely, so it was the first to hit it. Both
   streams are now reconfigured to UTF-8 with `errors="replace"`.

**The `p/?l` regex bug is fixed here.** The window-resolution floor is **ticket
011** — see below.

## The finding

`plan.py`'s `reading_list()` puts one hardcoded row in every dossier:

```python
out.append((
    "docs/decisions.md",
    "ADR log, newest first — check nothing decided this already",
))
```

`docs/decisions.md` is **148 KB / 2,248 lines / 21,402 words / 89 entries ≈ 30k
tokens.** It is a required read on *every* planning run, and it is append-only,
so that cost rises with the age of the project and never falls.

Three separate problems, of which only the first was obvious:

1. **It is the single most expensive thing in the planning stage** — more than
   the window, its services and its tests combined.
2. **Its ordering has silently inverted.** The header says "Newest first." That
   holds for lines 7–1492; there are then **8 monotonicity breaks** and the tail
   runs oldest-first. The newest entries sit at the *bottom* (lines 2133, 2177),
   exactly where an agent following the instruction never looks. So the read is
   not merely expensive, it is misleading.
3. **It is the wrong artifact for the job.** Planning needs to know *what binds*,
   which is bounded. The log records *why things were decided*, which is not.

## The measurement

| artifact | size |
|---|---:|
| `docs/decisions.md` (89 entries) | ~30,000 tokens |
| `.claude/context/rules.json`, all 35 rules | ~1,819 tokens |
| **scoped to a recognition task's files (16 rules)** | **~738 tokens** |
| scoped to a money/migration task (18 rules) | ~1,004 tokens |
| scoped to this ticket's own files (2 rules) | ~94 tokens |

Scoping the *log* by keyword was tried and **rejected on measurement**: its
entries are prose essays that mention everything, so any honest filter keeps more
than half of them — 51/89 match recognition terms, 52/89 match ui/theme terms,
47/89 match money/schema terms. The log cannot be narrowed. The registry is
already narrow, because every rule carries a `scope` glob.

## Why the registry is bounded and the log is not

A log is **append-only**: a reversal adds a second entry and both stay forever, so
it is O(project age). A registry is **superseded in place** — a registry holding
two contradictory rules is a bug, where a log holding two contradictory entries
is just history. It therefore grows with the app's distinct invariants, not with
time: 35 rules for a V1-complete app of 11 windows, and ticket 002 has just taken
it to full coverage.

The registry also has an integrity property the log has not: each rule seals its
source with `excerpt_sha256`, so `rules.py check` **fails** when the cited text
moves. A rule cannot silently drift from what it cites.

## Area of impact

| File | Line | Role |
|---|---|---|
| `.claude/skills/task-planning/scripts/plan.py` | 218-223 | the hardcoded `docs/decisions.md` row — **this is the 30k** |
| `.claude/skills/task-planning/scripts/plan.py` | 203-248 | `reading_list()` builds the required-reads table |
| `.claude/skills/task-planning/SKILL.md` | Step 2 | tells the agent to read every row, then "check `docs/decisions.md` (newest first)" |
| `.claude/context/app-map.json` | `architecture.rules` | the dossier's "Rules that always bind" prose — duplicates `rules.json`, unscoped, unsealed |
| `.claude/hooks/rules.py` | 39-62 | `section_text(path, anchor)` — already extracts one anchored section |
| `.claude/hooks/rules.py` | argparse | subcommands are `check/list/scope/seal/validate`; **there is no `show`** |
| `.claude/context/rules.json` | — | 35 rules; 2 already cite `docs/decisions.md` by anchor |
| `tests/test_app_map.py` | — | asserts the map matches the tree; touched if the map's rules block moves |

## Approach

**Chosen: the dossier prints the *scoped* rules; the log becomes seek-only.**

Planning stops reading `docs/decisions.md` at all. Instead the dossier prints the
rules whose `scope` matches the files the task will touch — id, lane, the rule,
what a violation looks like, and where it is sourced from.

The log is then read **only by following a specific rule's anchor**, one section
at a time, at the moment a rule is actually in the way. `rules.py`'s
`section_text()` already does exactly this extraction; it needs a `show`
subcommand to expose it.

This is the bridge that already exists: `QT-MODEL-SORT-ROLE` and
`CHECKER-NEEDS-NEGATIVE-TEST` are sourced at `docs/decisions.md` with anchors
(commit `03123df`, "anchor the Qt sort-role rule at the ADR that states it").
When an ADR states something binding, it becomes a rule anchored at that ADR.
Everything else in the log is narrative, and narrative that binds nothing does
not earn a read before every task.

### Rejected alternatives

- **Shrink the ADR entries to the terse `###` bullet form.** 78 of 89 entries
  already use it (median 176 words) against 11 prose essays (median 388). Real
  drift, but it only changes the constant: the file still grows without bound and
  planning still reads all of it.
- **Generate a table-of-contents index (date · title · line) and seek into it.**
  893 words for all 89 entries — a 25× improvement, and still **O(entries)**. It
  reaches today's pain at ~900 entries instead of 89, which is a deferral, not a
  fix. It also has no integrity check: a title index just goes stale, where a
  sealed rule fails loudly.
- **Keyword-scope the log per window.** Rejected on measurement — see above.
- **Split the log into one file per ADR plus an index.** Trades one read for a
  directory listing plus N reads, and adds a drift surface. The registry gives
  the same benefit with no new filesystem layout.
- **Archive entries older than N months.** Defers the growth curve without
  changing it, and an old decision binds exactly as much as a new one.
- **Drop the log entirely.** The reasoning is worth keeping; it is the *reading
  of it at planning time* that costs. Seek-only preserves it at zero standing
  cost.

## Plan

1. `.claude/hooks/rules.py` — add a `show <ID>` subcommand over the existing
   `section_text()`: print the rule, its violation, its source `file#anchor`, and
   the cited section itself. This is what makes the log reachable *before*
   anything stops reading it wholesale.
2. `plan.py` — add `scoped_rules(root, win)`: load `rules.json`, match each
   rule's `scope` globs against the window's file, services and tests, return the
   hits grouped by lane. Fail the same way the map self-check does if
   `rules.json` is missing or unparseable — a dossier that silently prints no
   rules is the failure mode this ticket is most exposed to.
3. `plan.py` — replace the dossier's **Rules that always bind** block (today read
   from `app-map.json`, unscoped and unsealed) with that scoped output, each line
   naming its rule id so it can be looked up.
4. `plan.py` — **delete the `docs/decisions.md` row** from `reading_list()`
   (218-223).
5. `plan.py` — print one closing line under the rules block: *why a rule exists →
   `rules.py show <ID>`.*
6. **Coverage honesty.** If a required-read file matches **no** rule's scope, say
   so explicitly rather than printing an empty block — "no registry rule claims
   this path" is a real and useful statement, and silence would read as "nothing
   binds here". This is the safeguard against the one way this ticket can go
   wrong.
7. `SKILL.md` — Step 2 loses "then check `docs/decisions.md` (newest first)" and
   gains the scoped-rules instruction plus the `show` escape hatch. Step 3's
   *Rules this task must not break* section now cites rule ids.
8. `app-map.json` — the `architecture.rules` prose block becomes a pointer to the
   registry rather than a second copy of it. Update `tests/test_app_map.py` if it
   asserts on that block.
9. Tests: `scoped_rules` returns only in-scope rules for a known window; a
   window whose files match nothing produces the explicit "no rule claims this
   path" line, not an empty one; `plan.py brief` output contains no
   `docs/decisions.md` row; `rules.py show` resolves a `docs/decisions.md`-sourced
   rule (`QT-MODEL-SORT-ROLE`) to its ADR section.

## Verification

- `plan.py brief <window>` for two or three windows: the dossier no longer lists
  `docs/decisions.md`, and the rules block is scoped and non-empty.
- Token count of a dossier before and after — the claim is ~30k to ~740 for the
  ADR/rules step.
- `rules.py check` still passes (every source, anchor and seal resolves).
- `rules.py show QT-MODEL-SORT-ROLE` prints the ADR section, proving the log is
  still reachable.
- `plan.py validate` clean; full suite offscreen; `ruff format`, `ruff check`.

## Notes

- **This makes the log's length and ordering irrelevant** rather than fixing
  them. Nothing scans it, so the 8 monotonicity breaks and the essay-length
  entries stop costing anything. No reorder, no format policing, no shrinking of
  existing entries — all of which were considered and are dropped in favour of
  this.
- The dependency this ticket was going to carry — *the registry must be complete
  or a binding decision goes invisible* — was **closed by ticket 002** (`#26`,
  `#27`, landed 2026-08-09) which took the registry from 24 rules to 35 at full
  coverage. Step 6 exists so that any residual gap is announced rather than
  implied.
- Sources across the 35 rules: `CLAUDE.md` 10, `specs/02-data-model.md` 9,
  `app-map.json` 9, `docs/decisions.md` 2, plus five singletons. The log is
  already a minority source of what binds; this ticket makes the dossier reflect
  that.

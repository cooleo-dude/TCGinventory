# 011 — `plan.py` resolves the wrong window, confidently

```
status:   open
kind:     fix
source:   found by running /task-planning on ticket 010
issue:    none
axis:     correctness
found:    2026-08-09
saving:   -
risk:     medium — changes window resolution for every future planning request
```

## The finding

Planning ticket 010 produced a dossier for **History**, and exited `0`.

The request was *"ticket 010 — planning reads the rule registry, not the decision
log"*. `"log"` is one of History's aliases. It matched the request body, scored
`40 + len("log") = 43`, was the only window to score at all, and therefore won
outright. The dossier that came back described `HistoryView`, the `transaction`
and `audit_log` tables, and fourteen required reads — for a task that touches no
app code whatsoever.

`SKILL.md` says cross-cutting work with no window "will exit `2`. That is
correct." It does not, whenever any short alias appears anywhere in the sentence.

**Exit `0` wrong is the worst failure this tool has.** Exit `2` prints the window
list and costs one re-run; exit `0` is silently trusted, and everything the skill
does downstream is built on it.

## The deeper cause, which the obvious fix does not address

`split_request` returns `(body, body)` when the request has no separator. So for
a plain sentence — the commonest shape — `head == whole`, and **every** alias
match takes the `100 + len(name)` *head* branch at maximum confidence.

A body-only floor therefore never fires on plain sentences. Measured against a
17-case corpus, it plateaus at 12/17 no matter what threshold is chosen, and
leaves these wrong:

| request | resolves to | should be |
|---|---|---|
| `write the migration for the new column` | transaction (`write`) | no window |
| `audit the startup path for slow imports` | history (`audit`) | no window |
| `post a message to the log when a job fails` | transaction (`post`) | no window |
| `the tile shows a status while it runs` | calendar (`shows`) | no window |

The axis is not head-vs-body. It is **"the request names a window"** versus **"a
word that happens to be an alias appears in a sentence."**

## The measurement

| model | corpus |
|---|---:|
| today (no floor) | 12/17 |
| body-only alias floor ≥5 | 12/17 |
| body-only alias floor ≥6 | 12/17 |
| **explicit-head + incidental floor ≥6** | **17/17** |
| explicit-head + incidental floor ≥7 | 17/17 |
| explicit-head + incidental floor ≥8 | 17/17 |

`6` is the least restrictive threshold that passes, and it is the right cut on
inspection of the alias table: everything at ≤5 characters is a generic English
word (`log`, `ops`, `tx`, `ml`, `post`, `sale`, `write`, `shows`, `audit`,
`month`, `price`, `stock`, `home`, `grid`), while length 6+ holds the specific
ones (`vision`, `camera`, `photos`, `corpus`, `bidding`, `signals`, `metrics`).

## Area of impact

| File | Line | Role |
|---|---|---|
| `.claude/skills/task-planning/scripts/plan.py` | 187-212 | `resolve_window` — the scoring |
| `.claude/skills/task-planning/scripts/plan.py` | 169-184 | `split_request` — where `head == whole` originates |
| `tests/test_app_map.py` | `test_every_window_resolves_from_its_own_aliases` | asserts every alias resolves via `"<alias> — do a thing"`; that is the *explicit* form and must keep passing |
| `.claude/context/app-map.json` | `windows.*.aliases` | the generic short aliases are the raw material |

## Approach

**Chosen: distinguish an explicit naming from an incidental mention.**

```python
def explicit(head, whole, name):
    """The request NAMES this window: a distinct head that is, starts with, or
    ends with the alias."""
    if head == whole:
        return False
    h = head.strip().lower()
    return h == name or h.startswith(name + " ") or h.endswith(" " + name)
```

- `explicit` → `100 + len(name)`, no floor. *"Inventory — add a filter"*.
- otherwise, an alias found anywhere → `40 + len(name)`, **only if
  `len(name) >= 6`**. *"fix the recognition suggestions"* resolves;
  *"…the decision log"* does not.
- no window → exit `2`, which already prints every window and its aliases.

### Rejected alternatives

- **A minimum alias length on body matches only.** The obvious fix, and it scores
  12/17 — it never fires on plain sentences, which is most requests.
- **Require a margin between top and runner-up.** Does not help: History was the
  *only* window that scored, so the margin was infinite.
- **Require ≥2 distinct alias hits.** Too strict alone — *"fix the recognition
  suggestions"* has one hit on an unambiguous 11-character name.
- **Prune the generic aliases from the map.** `log`, `post`, `write` and `audit`
  are genuinely how someone refers to those screens in the *head* position
  ("log — add a filter"). The problem is only their appearance mid-sentence, so
  position is the right discriminator, not deletion.
- **Raise the floor to 7 or 8.** Also 17/17, but it would stop `vision`,
  `camera`, `photos` and `corpus` from resolving incidentally, and those are
  exactly the specific terms that should. Least restrictive wins.

## Plan

1. `plan.py` — add `explicit(head, whole, name)` and rewrite `resolve_window` to
   score explicit matches without a floor and incidental ones with `MIN_ALIAS = 6`.
2. `plan.py` — the `exit 2` message already lists every window and its aliases;
   add one line telling the caller to name the window explicitly (`"<window> —
   <task>"`), since that is now the reliable form.
3. Tests in `tests/test_app_map.py`, alongside:
   - the 17-case corpus, as a `parametrize` — the four wrong resolutions above
     must return "no window", and the explicit/strong-signal cases must keep
     working (`CHECKER-NEEDS-NEGATIVE-TEST` binds this: the guard needs cases
     that prove it refuses);
   - `test_every_window_resolves_from_its_own_aliases` must still pass unchanged
     — it uses `"<alias> — do a thing"`, which is the explicit form;
   - this ticket's own title resolves to no window.
4. `SKILL.md` — step 1's exit-code table gains a line: `2` now also means "the
   request only mentioned a window in passing; name it explicitly".

## Verification

- The corpus test is the verification: 17/17.
- `plan.py brief "ticket 011 — plan.py resolves the wrong window"` exits `2`.
- `plan.py brief "recognition — validate on real crops"` still exits `0`.
- Full suite offscreen, `ruff format`, `ruff check`.

## Notes

- Split out of ticket 010 deliberately. 010 is mechanical (what the dossier
  *reads*); this is behavioural (which window it *resolves to*), it affects every
  future request, and it carries a tuning constant. Bundling them would have held
  a 91% token reduction behind a change that may want adjusting in use.
- The corpus in this ticket is the one used to pick the model; it should ship as
  the test rather than being re-derived.

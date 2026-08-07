# 002 — Enumerate the rule registry to full coverage

```
status:   open
kind:     feature
source:   /sdd-review build-out, 2026-08-07
issue:    #7
axis:     correctness
found:    2026-08-07
saving:   -  (coverage, not performance)
risk:     low — additive; each rule is inert until a critic cites it
```

## The finding

`.claude/context/rules.json` is the closed set of rules the review panel may
judge against — a finding citing anything else is rejected by `rules.py
validate`. That closure is the point, and it is also the ceiling: **the gate can
only catch what is enumerated.**

The registry shipped with 24 rules covering what the five critics already
claimed. It was deliberately not an exhaustive sweep — a small sealed set that is
provably accurate beats a large one that is half-guessed, and every rule costs a
real anchor and a real seal. But the gap is now the limiting factor on how much
the gate is worth, and it should be closed on purpose rather than left to drift.

## The gap, measured

| source | rules it states | enumerated today |
|---|---:|---:|
| `specs/02-data-model.md` § Invariants (must always hold) | **9 numbered** | **1** (`INVARIANTS-DOC`, a catch-all over all nine) |
| `CLAUDE.md` § Non-negotiable architecture rules | 9 bullets | 8 |
| `CLAUDE.md` elsewhere (working style, dev environment, layout) | ~13 bullets | 0 |
| `.claude/context/app-map.json` § shared_ui | 10 conventions | 5 |
| `docs/decisions.md` | ~90 ADRs | 2 |

Current registry: 9 invariants · 5 risk · 4 test · 5 ui · 1 `UNENUMERATED`.

**`INVARIANTS-DOC` is the weakest entry and the reason to do this.** It points at
a section containing nine distinct, individually checkable invariants and asks a
critic to judge all of them under one id. A finding that cites it says almost
nothing about *which* invariant broke, and its seal covers the whole section, so
editing invariant 9 invalidates the seal for a change that had nothing to do with
invariants 1–8.

## Area of impact

| File | What changes |
|---|---|
| `.claude/context/rules.json` | new rule entries; `INVARIANTS-DOC` split and retired |
| `.claude/hooks/rules.py` | nothing, if anchors stay section-level — **see the open question** |
| `.claude/agents/tcgi-*.md` | nothing; they read the registry, they do not restate it |
| `tests/test_rules.py` | nothing structural; it validates whatever the registry holds |

## Approach

**Chosen: split `INVARIANTS-DOC` into nine, then sweep the remaining sources.**

One rule per numbered invariant — `INV-ONHAND-SUM`, `INV-CONSUMPTION-BALANCE`,
`INV-REMAINING-BOUNDS`, and so on — each with a `scope` narrowed to the services
and repositories that can actually break it. A finding then names the invariant
it broke, and a spec edit to one invariant invalidates one seal.

Then work outward in decreasing value: the `app-map.json` conventions the UI
critic already half-covers, then the ADRs that state a durable rule rather than
recording a one-off decision.

### Rejected alternatives

- **Enumerate everything in one pass.** ~130 candidate statements across four
  documents, most of which are guidance rather than checkable rules. The result
  would be a registry nobody trusts, and the critics would drown in rules that
  never fire.
- **Drop the closed set and let critics judge freely.** That is what this
  replaced. An unverifiable judgement cannot be allowed to block a merge.
- **Auto-generate rules from the specs.** Tempting and wrong: the value is in a
  human deciding *this sentence is a checkable rule with this scope and this
  severity*. A generated registry is a restatement of the specs, which is exactly
  the second-copy-of-the-truth problem the seal exists to prevent.

## Plan

1. Split `INVARIANTS-DOC` into nine rules, one per numbered invariant in
   `specs/02-data-model.md`. Give each a `scope` narrow enough that it fires on
   the code that could break it and stays silent elsewhere.
2. `rules.py seal` each, then `rules.py check`.
3. Sweep `.claude/context/app-map.json` § `shared_ui` for the five conventions
   the UI critic does not yet cite.
4. Sweep `CLAUDE.md` for the non-architecture bullets that are genuinely
   checkable, and skip the ones that are guidance.
5. Sweep `docs/decisions.md` for ADRs stating a durable rule. Most do not — an
   ADR recording *what was decided once* is not a rule.
6. For each new rule confirm `rules.py scope` fires on a file that should match
   and stays quiet on one that should not. A rule with a scope that matches
   everything is noise with a seal on it.

## Verification

- `rules.py check` exits 0 with every new rule sealed.
- `tests/test_rules.py` green.
- `rules.py scope src/tcginventory/services/allocation.py` names the money rules
  and not the UI ones; `rules.py scope docs/decisions.md` still reports nothing.
- Dispatch the panel over a known-bad diff and confirm a finding cites the
  specific new rule rather than the retired catch-all.

## Open question — for the user, not the implementer

**Should the seal be per-invariant rather than per-section?** Splitting
`INVARIANTS-DOC` nine ways gives nine rules that all seal *the same section*, so
any edit to any invariant invalidates all nine. Fixing that means teaching
`section_text()` to anchor on a list item, not just a heading. That is real work
and it may not be worth it — nine simultaneous reseals after a spec edit is
annoying but honest, and it does force a human to re-read all nine.

## Notes

- Additive and low-risk: a rule that exists but is never cited changes nothing.
  The failure mode is not breakage, it is **noise** — a registry full of rules
  that never fire trains people to stop reading findings.
- `UNENUMERATED` findings from real panel runs are the best evidence for what to
  add next. Prefer them over a speculative sweep.

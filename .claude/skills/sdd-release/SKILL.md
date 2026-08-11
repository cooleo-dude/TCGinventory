---
name: sdd-release
description: Cut a TCGinventory release — preconditions, changelog, tag, a clean-worktree build that is booted before it ships, and the GitHub release. Use when asked to release, cut a version, tag, publish, or build the distributable. Also use when the user types /sdd-release.
argument-hint: <check | notes | build | publish> — defaults to check
allowed-tools: [Read, Grep, Glob, Bash, PowerShell, Edit, Write]
---

# sdd-release

Part of the **`/sdd-*`** corpus: `/sdd-ticket` (work intake), `/sdd-version`
(the number), **`/sdd-release`** (this).

Request: **$ARGUMENTS** — if empty, run `check` and report whether a release is
possible.

```bash
PY=.venv/Scripts/python.exe; R=.claude/skills/sdd-release/scripts/release.py
$PY $R check              # clean tree, version sane, tag free, commits pushed
$PY $R notes              # what the changelog entry will say
$PY $R build [--zip]      # clean-worktree build, then boot it
$PY $R publish [--attach] # tag, CHANGELOG.md, GitHub release
```

## The one rule

**Build from a clean worktree at the ref, never from the working tree, and boot
the result before publishing.**

`build` does this for you. It is not caution for its own sake: on 2026-08-07 the
working tree held another session's half-finished `history.py`, and a build from
it would have shipped an exe that crashed constructing `MainWindow`. A shared
checkout means the tree is not yours alone — see the
`concurrent-agent-boundaries` memory.

The boot check launches the exe against a throwaway `TCGINVENTORY_DATA_DIR`,
waits, and confirms the process is alive and `inventory.db` exists — i.e. it
started, migrated and got a window up. It never touches real data.

When `check` refuses on a dirty tree it now also prints **who holds a lease on the
checkout**, so "clean this up" and "wait for the other session" are
distinguishable. There is deliberately no flag to skip it: ignoring the dirty tree
is how that half-finished `history.py` would have shipped.

## Order

1. **`check`.** Refuses on: a dirty tree, a version inconsistency
   (`/sdd-version check`), a tag that already exists, or unpushed commits. Fix
   whatever it names — do not work around it.
2. **`/sdd-version`** if the number is wrong. A release is a claim that a commit
   is worth naming, and the name has to be right first.
3. **`notes`.** Read them. Under 30 commits they are listed; above that they are
   grouped by the `Area:` prefix with the ten most recent spelled out, because
   the first release covers 313 commits and a wall of them says less than a count.
4. **`build`.** A few minutes, ~307 MB. `--zip` if you intend `--attach`.
5. **`publish`.** Tags, pushes the tag, prepends to `CHANGELOG.md`, creates the
   GitHub release.
6. **Close what shipped.** Tickets are closed on their own branch *before* the MR
   merges, so by the time a release is cut there should be nothing outstanding —
   `/sdd-ticket list` is the check. A ticket still open here means its MR never
   landed, and it is not part of this release.

## The review gate

**The critic panel is opt-in — never dispatch it unless the user asks.** A spawn
costs ~26k tokens before it judges anything. A release with no panel on record
proceeds normally.

What `.claude/hooks/review_gate.py` enforces as a `PreToolUse` hook is the other
half: **a panel that was run cannot be quietly ignored.** No verdict on record →
the merge or close proceeds. A recorded `block` → it stops until the finding is
fixed and committed, or cleared deliberately (`review_gate.py clear`).

`/sdd-ticket` § The review gate is the authority on how to run one — which lanes
the rule registry says a diff can even violate, the `≤150 changed lines`
mini-critic tier, and `--inherit` for re-judging only what got dirty. Do not
duplicate that guidance here; two copies of it drifted apart once already.

The one thing worth repeating: `--ref` is the **MR number**, and one verdict covers
landing it — closing the ticket and merging the MR share a number now.

```bash
python .claude/hooks/review_gate.py record --ref 41     --verdict invariants=pass --verdict risk=pass --verdict test=pass
```

Relay every finding to the user. A panel whose findings you summarise away is a
panel that did nothing.

## Binaries

`publish` is **tag and notes only** by default. The bundle is ~307 MB and the
repo is private, so every attached release costs storage against one consumer.
`--attach` uploads `dist/TCGinventory-<version>-windows.zip` when you actually
want a downloadable build; it requires `build --zip` first and will say so.

## What this does not do

- **It does not merge.** Releases are cut from `main` after PRs land. Check the
  branch `check` reports.
- **It does not decide the version.** That is `/sdd-version`, and the scheme
  lives there.
- **It does not write history.** `CHANGELOG.md` is generated from commit
  subjects; if an entry reads badly, the commit message was the problem and the
  fix is a better message next time, not hand-editing the changelog.

## Reporting

Version and tag, what `check` said, the build size and whether it booted, the
release URL, and which tickets you closed. If you stopped at `check`, say what
blocked it and nothing else — a half-cut release is worse than none.

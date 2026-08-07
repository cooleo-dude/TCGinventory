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
6. **Close what shipped** — `/sdd-ticket close <N>` for every ticket this release
   delivered, and record the measured result in each.

## The review gate

**A ticket cannot be closed and a PR cannot be merged without a green critic
panel.** This is enforced, not advised: `.claude/hooks/review_gate.py` runs as a
`PreToolUse` hook and blocks `gh pr merge` and `ticket.py close` when there is no
green verdict recorded for the current `HEAD`.

Dispatch the panel with the Agent tool, handing each critic the diff and nothing
else — they judge the change, never your account of it:

| when | dispatch |
|---|---|
| default | `tcgi-invariant-critic`, `tcgi-risk-critic`, `tcgi-test-critic` in parallel |
| the diff touches `ui/` | add `tcgi-ui-critic` |
| <=2 files, no migration, no money path, no `ui/` | `tcgi-mini-critic` alone |

Each ends in a verdict line. Record them:

```bash
python .claude/hooks/review_gate.py record --scope pr-merge --ref <n>     --verdict invariants=pass --verdict risk=pass --verdict test=pass
```

`revise` is a note to answer, not a failure. **`block` stops the gate** — fix the
findings, commit, and re-run the panel. Verdicts are bound to a sha, so a new
commit reopens the gate deliberately: the panel judged something that is no
longer what would merge.

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

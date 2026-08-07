---
name: sdd-ticket
description: File, sync and close TCGinventory tickets and their GitHub issues. Use when a sweep or a plan produces work that will not be done immediately, when asked to open an issue or ticket, to list what is outstanding, or to close one that has landed. Also use when the user types /sdd-ticket.
argument-hint: <list | check | file <what> | push [N] | close N> — defaults to list
allowed-tools: [Read, Grep, Glob, Bash, PowerShell, Edit, Write]
---

# sdd-ticket

Part of the **`/sdd-*`** corpus: **`/sdd-ticket`** (work intake), `/sdd-version`
(the number), `/sdd-release` (tag, build, publish).

Request: **$ARGUMENTS** — if empty, run `list` and report what is outstanding.

```bash
PY=.venv/Scripts/python.exe; T=.claude/skills/sdd-ticket/scripts/ticket.py
$PY $T list           # every ticket, its status, its issue
$PY $T next           # the number a new ticket takes
$PY $T check          # non-zero if malformed, unindexed or out of sync
$PY $T push 1         # create or update the GitHub issue
$PY $T close 1        # mark done and close the issue
```

## The one rule

**The local file is the truth. The GitHub issue is a mirror.**

`.claude/context/tickets/NNN-*.md` is versioned with the code, readable offline,
and actionable by an agent with no network and no auth. The issue exists so the
work is visible from a phone. When they disagree, the file wins — edit the file,
then `push`.

Never hand-edit an issue body on GitHub. `push` overwrites it from the file.

## Where tickets come from

Four skills feed this. Each ends by filing what qualifies; none of them invent
their own format.

| source | what becomes a ticket |
|---|---|
| `/optimization-sweep` grounded pass | findings it may not auto-apply: money path, migrations, stored precision, retention |
| `/optimization-sweep` research pass | `MECHANISM` proposals clearing all five bars |
| `/dead-code-sweep` | *Flagged for review* items — a symbol whose only reference could be dynamic, a handler whose failure mode was not pinned down |
| `/task-planning` | **always** — every approved brief, before any code is written |

Read `.claude/context/tickets/README.md` for the bar each must clear. The short
version: a found improvement needs a measurement, no regression, no accuracy
cost, no product decision, and a clean constraint checklist. Planned work from
`/task-planning` files unconditionally — the ticket is what the branch, the PR
and the commit point back at.

**Anything that does not qualify goes to the user in the calling skill's report,
not into this directory.** Minimal-impact wins, alternatives worth weighing and
`none` results are for judgement. If the user declines one, it belongs in
`.claude/skills/optimization-sweep/references/declined.md`.

## Filing one

1. `ticket.py next` for the number.
2. Write `.claude/context/tickets/NNN-<kebab-title>.md`. First line
   `# NNN — Title`, then the front block — `status kind source issue axis found
   saving risk` — then the finding and its evidence, an **area of impact** table
   of real `file:line`, the chosen approach **and the rejected alternatives with
   reasons**, a numbered plan, and how to verify.
3. `ticket.py check` must pass.
4. `ticket.py push N` files the issue and writes `issue: #N` back into the file.
   It also regenerates the README index, so do not maintain that by hand.

**Write for someone who was not in the conversation.** The measurement you did
not record is a measurement the next agent has to redo. The alternative you
rejected in your head will be re-proposed by someone else in a fortnight — that
is what the *rejected alternatives* section is for, and it is not optional.

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

## Closing one

`ticket.py close N` sets `status: done` and closes the mirrored issue — the
gate above must be green first, or the call is blocked. Then edit
the ticket to record the commit, the PR and the **measured after** — a ticket
that says what actually happened is the reason the code looks the way it does.

For a ticket that is abandoned rather than finished, set `status: dropped`, say
why, and mirror it into `declined.md` so no future sweep re-derives it.

## Notes that will bite you

- `gh` lives in the **machine** PATH. A shell opened before it was installed will
  not see it; the script falls back to the absolute path, but your own `gh`
  commands may need a fresh terminal.
- `gh auth login` is interactive and this harness redirects stdin — it cannot be
  run from a tool call. The user runs it in a real terminal.
- The repo is **private**, so a token needs the `repo` scope for issues and PRs.
- Ticket numbers and issue numbers are **not** the same sequence. PR #1 and issue
  #2 already share a counter on GitHub's side; the ticket number is ours.

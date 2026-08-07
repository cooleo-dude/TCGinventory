---
name: sdd-version
description: Read, check or change TCGinventory's version. Use when asked to bump the version, cut a version number, check whether the version is consistent, or when a release needs a version decided first. Also use when the user types /sdd-version.
argument-hint: <show | check | set X.Y.Z | bump major|minor|patch> — defaults to show
allowed-tools: [Read, Grep, Glob, Bash, PowerShell, Edit]
---

# sdd-version

Part of the **`/sdd-*`** corpus for git management: `/sdd-ticket` (work intake),
**`/sdd-version`** (this), `/sdd-release` (tag, build, publish).

Request: **$ARGUMENTS** — if empty, run `show` and report.

```bash
.venv/Scripts/python.exe .claude/skills/sdd-version/scripts/version.py show
.venv/Scripts/python.exe .claude/skills/sdd-version/scripts/version.py check
.venv/Scripts/python.exe .claude/skills/sdd-version/scripts/version.py set 0.8.0
.venv/Scripts/python.exe .claude/skills/sdd-version/scripts/version.py bump minor
```

## The one rule

**`src/tcginventory/__init__.py` is the only place the version is written.**

`pyproject.toml` declares `dynamic = ["version"]` and reads that literal through
`[tool.setuptools.dynamic]`. setuptools parses it statically — it does not import
the package — so packaging metadata, the window title (`main_window.py:118`), the
splash (`splash.py:53,107`) and the frozen exe cannot disagree.

Never add `version = "..."` back to `pyproject.toml`, and never assign
`__version__` in a second module. `version.py check` fails on both, because that
is exactly the state this replaced: the version lived in two files and read
`0.0.1` from the first commit through seven shipped milestones — the exe built in
August and the one built in July were indistinguishable.

## Scheme

`X.Y.Z`, no pre-release or build metadata. Nothing in this app parses them, and a
scheme nobody reads is a scheme that rots. The script rejects anything else.

- **major** — a break in the data model or a migration a user cannot roll back.
- **minor** — a shipped milestone or a new window. `0.7.0` is M0–M6 plus M-Ops1.
- **patch** — fixes and optimizations against a released minor.

`0.7.0` was chosen over `1.0.0` deliberately, even though `CLAUDE.md` calls V1
feature-complete: 1.0 is reserved. Do not "correct" this.

## Changing it

1. `version.py set X.Y.Z` (or `bump`). It rewrites one literal, nothing else.
2. `pip install -e . --no-deps` — refreshes the installed distribution metadata.
   Skip this and `check` will fail, correctly: your venv still advertises the old
   version.
3. `version.py check` must exit 0.
4. Commit the version change **on its own**, on a branch, and open a PR. The
   version bump is a fact about a release, not a rider on a feature.

Tagging is **not** this skill's job — `/sdd-release` owns tags, the build and the
changelog, because a tag should point at a commit that has already been verified.
`show` will tell you when the current version is untagged; that is information,
not a task for you here.

## What to check before bumping

- `git status` is clean, or the only changes are yours. This repo has had
  concurrent sessions; committing someone else's staged work inside a version
  bump would be hard to unpick.
- The suite passes. A version is a claim that this state is worth naming.
- If a milestone moved, `CLAUDE.md`'s status section should say so in the same
  PR — that file is the human-readable counterpart to the number.

## Reporting

Say the old and new version, what the checker returned, and what remains — nearly
always "not tagged; `/sdd-release` next". If you were only asked to `show` or
`check`, do not change anything.

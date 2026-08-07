---
name: dead-code-sweep
description: Find and remove dead code and unnecessary defensive programming in TCGinventory. Use when asked to clean up dead code, unused imports or functions, commented-out blocks, back-compat shims, unreachable branches, or over-defensive try/except and None guards. Also use when the user types /dead-code-sweep.
argument-hint: <area to scope the sweep, optional> — e.g. services/pricing.py, the Recognition window, my uncommitted diff
allowed-tools: [Read, Grep, Glob, Bash, PowerShell, Edit, Agent]
---

# Dead code sweep

Scope: **$ARGUMENTS** — if that is empty, sweep `src/tcginventory/` and `tests/`.

Delegate the sweep with the Agent tool (`subagent_type: "general-purpose"`,
`model: "sonnet"`), passing everything from *Step 0* down as the prompt with the
scope substituted in. Ask it to return the Step 8 summary verbatim; relay that to
the user and let them decide on anything under *Flagged*.

Be conservative. In a repo this dense with Qt callbacks and registry indirection,
a wrong removal is far more expensive than a missed one. **When a symbol's only
reference could be dynamic, flag it — do not remove it.**

---

## Step 0 — Ground rules

Read these before touching anything:

| File | Why |
|---|---|
| `CLAUDE.md` | The non-negotiable architecture rules. They outrank every heuristic below. |
| `specs/02-data-model.md` | The money/schema invariants. Guards that enforce these are **not** defensiveness. |
| `docs/decisions.md` (skim newest first) | Code that looks vestigial is often a settled ADR. |

The layout is known — you do not need to detect it:

```
src/tcginventory/   domain/  data/  services/  ui/  scheduling.py  app.py  config.py
                    models/  — bundled .onnx weights, not Python; skip it
tests/              pytest; conftest.py + fixtures/
packaging/          tcginventory.spec, entry.py
```

Interpreter and tools (Windows is primary; both paths given):

```bash
# Windows                          # POSIX
.venv/Scripts/python.exe           .venv/bin/python
.venv/Scripts/ruff.exe             .venv/bin/ruff
```

Search with the **Grep tool**, not shell pipelines. It is ripgrep and it honours
`.gitignore`, so `__pycache__/`, `.venv/`, `build/` and `dist/` are already
excluded — every `| grep -v __pycache__` in a hand-written pipeline is noise.

---

## Step 1 — Let the linter go first

Ruff runs over the whole repo with `select = ["E", "F", "I", "UP", "B"]`
(`pyproject.toml`), and the tree is currently clean. That means **F401 unused
imports, F811 redefinitions and F841 unused locals cannot already be present** —
hand-grepping for them wastes the sweep.

Confirm the baseline, then look only at what ruff cannot see:

```bash
.venv/Scripts/ruff.exe check .
```

If that reports anything, fix it first and note it separately — a dirty baseline
means someone is mid-edit and you should scope around their files.

Then run the checks ruff is *not* configured for, as a read-only probe:

```bash
.venv/Scripts/ruff.exe check --select F401,F811,F841,ARG,ERA --no-fix --statistics src tests
```

Baseline as of writing, so you can tell signal from noise:

| Rule | Count | What to do |
|---|---|---|
| `F401` / `F811` / `F841` | 0 | Confirms Step 1 — nothing to find. |
| `ERA001` commented-out code | 2 | **High signal.** Read both; likely true positives. |
| `ARG001/002/005` unused argument | 332 | **Noise.** Qt slot signatures must match their signal, and lambdas swallow Qt's arguments. Do not act on these without a specific reason. |

Both rules are deliberately absent from `pyproject.toml`'s `select` — treat their
hits as candidates, not verdicts.

---

## Step 2 — Hunt for dead code

### 2a. Unreachable statements

Statements after an unconditional `return`, `raise`, `break` or `continue` at the
*same* indentation. "Same indentation" needs a backreference, which the Grep
tool's engine rejects — run this one through Bash with PCRE2:

```bash
rg --pcre2 -U -n '\n(\s*)(return|raise|break|continue)\b[^\n]*\n\1(?!except|else|elif|finally|#|\))\S' src/ tests/
```

Currently one hit repo-wide, and it is a **false positive**: a `return f"""…`
whose multi-line string body looks like a following statement. Expect that shape
and read the block before removing anything — a `return` inside an `if` with a
dedented follow-on is perfectly reachable.

### 2b. Commented-out code

Grep: `^\s*#\s*(def |class |return |import |from |self\.|if |for |while )`

Remove runs of **2+ consecutive** commented-out statements. A single `#` line
explaining *why* is documentation, and this codebase leans on those heavily —
leave them. Leave the `# ---- section ----` banner comments too; they organise
`services/operations.py` and the larger view modules.

### 2c. Dead branches

Grep: `if (False|0|None):|while False:`

Remove the whole block including its body. Note that `if TYPE_CHECKING:` is not
a dead branch.

### 2d. Back-compat shims

Grep (case-insensitive): `#\s*(deprecated|back.?compat|legacy)|TODO.*remove|FIXME.*remove`

Read each hit. If the function body is only `return new_name(...)` with no added
logic, it is a shim: remove it and update the call sites.

**Data-shaped back-compat is different.** `services/pricing.py` carries a
back-compat branch for items stored under an older external catalog's set names.
That is not a shim — it is live data handling for rows already in users' SQLite
files. Same for any migration in `data/migrations/`: those are append-only
history and are **never** dead, however old.

### 2e. Uncalled functions and classes

There is no symbol-graph index in this repo, so this is grep plus judgement.
Two structural aids exist and are worth using first:

- `.claude/context/app-map.json` — the window/service/table map. `python
  .claude/skills/task-planning/scripts/plan.py validate` proves it matches the
  tree, and it will tell you which view owns a service.
- `tests/test_app_map.py` — fails when the map drifts, so it doubles as a check
  that you did not delete something the map still claims.

Enumerate definitions, then check each name for references:

```
Grep: ^\s*(async def |def |class )(\w+)     # in src/tcginventory
Grep: \b<name>\b                            # across src/, tests/, packaging/
```

Search **`packaging/` too** — `tcginventory.spec` names lazily-imported modules
in `hiddenimports` (`tcginventory.services.recognizer`, `PySide6.QtCharts`,
`httpx`, `onnxruntime`). A module referenced only there is load-bearing for the
frozen exe and invisible to a source-only grep.

---

## Step 3 — Never remove (this repo's dynamic-reference surfaces)

A single textual reference is enough to keep a symbol. These have **zero**:

**Qt / PySide6**

- Virtual overrides Qt calls by name: `data`, `rowCount`, `columnCount`,
  `headerData`, `flags`, `setData`, `paint`, `sizeHint`, `createEditor`,
  `setModelData`, `editorEvent`, `lessThan`, `filterAcceptsRow`, `eventFilter`,
  and the `*Event` handlers (`closeEvent`, `resizeEvent`, `showEvent`,
  `mousePressEvent`, `enterEvent`, `leaveEvent`).
- Anything decorated `@Slot(...)`, and any method named in a `.connect(...)`
  call — the connection may live in a different module.
- `Signal(...)` class attributes. They are declarations; nothing "assigns" them.
- Unused-looking parameters on slots: the signature must match the signal.

**Registries and data-driven wiring**

- `services/operations.py` → the `OPERATIONS` / `GROUPS` / `MODEL_GROUPS`
  entries and their counterpart runners in `OperationsView._runner`. The whole
  design is "registry entry + runner"; an operation `id` is a string key, so the
  runner has no textual caller.
- `services/__init__.py`'s `__all__` re-exports.
- `data/migrations/*.sql` — never touched by this sweep.
- `data/seed.py` (behind `TCGINVENTORY_SEED=1`) and `packaging/entry.py`.
- `app.py` / `__main__.py` entry points and the `gui-scripts` console entry.

**Python and pytest**

- Dunder methods, `@abstractmethod`, `@property`/`@x.setter` pairs.
- `# noqa` lines and imports inside `if TYPE_CHECKING:`.
- `tests/conftest.py` fixtures — a fixture is referenced by *parameter name*, not
  by call. `_drain_deferred_deletions` is `autouse` and has no reference at all;
  deleting it reintroduces a Qt access-violation crash.
- `tests/fixtures/` data files.

---

## Step 4 — Hunt for unnecessary defensiveness

The rule, from `CLAUDE.md`:

> Don't add error handling, fallbacks, or validation for scenarios that can't
> happen. Trust internal code and framework guarantees. Only validate at system
> boundaries.

**TCGinventory's boundaries** — handlers here are legitimate, leave them:

- `httpx` calls in `services/pricing.py`, `catalog.py`, `marketprices.py`,
  `ebay.py`, `cardimages.py`, `tcgplayer.py`, `cardshows.py`.
- `sqlite3` work in `data/` — the DB is a real file that can be locked or absent.
- `subprocess` in `scheduling.py`, `services/modeljobs.py`, `services/cardshows.py`.
- Model and image I/O: `onnxruntime`, `PIL`, `numpy` loads in `services/`
  (`recognizer.py`, `inference.py`, `detector.py`, `condition*.py`).
- File I/O in `data/backup.py` and `services/importexport.py`.
- Worker threads: an exception crossing a `QThread` boundary takes the app down,
  so a broad catch that reports into the run log is correct.

### 4a. Silent swallows

Grep (multiline): `except[^\n]*:\n\s*pass\b`

Flag only where the guarded code has no real failure mode — pure dict/list/string
work on internally-typed values.

**Do not touch `except RuntimeError: ... pass` around widget access.** That is
the standard PySide6 guard for a C++ object already deleted during teardown
(`ui/card_preview.py`, `ui/theme.py`, several views). It is a framework
guarantee working exactly as intended, and removing it produces crashes that only
appear on shutdown.

Also never flag `except (KeyboardInterrupt, SystemExit):`.

### 4b. Redundant `or {}` / `or []`

Grep: ` or \{\}| or \[\]`

26 hits — small enough to read every left-hand side. Redundant only when it is an
internal call annotated `-> dict` / `-> list` that provably never returns `None`.
Keep it on `os.getenv`, on anything crossing the network, on `getattr(x, "y",
None)`, and on Qt getters that return null objects.

**Leave `or ""` alone unless it is in `$ARGUMENTS`.** There are ~170 of them and
almost all are deliberate: this codebase normalises nullable text columns to the
empty string at the export and display seams. `services/importexport.py` alone
holds 26, and the rest cluster in `ui/views/` (`transaction.py`, `history.py`,
`item_editor.py`, `recognition.py`) and `ui/models/inventory_model.py`. Sweeping
them repo-wide is ~170 reads for close to zero true positives.

### 4c. Redundant `is not None` guards

There are ~320 of these. **Do not sweep them repo-wide** — the signal-to-noise is
hopeless and you will burn the budget reading true positives' neighbours. Scope
this check to the files named in `$ARGUMENTS`, or to the working diff:

```bash
git diff --name-only HEAD -- "*.py"
```

Flag one only when you have traced the variable to an origin that cannot be
`None` — a literal, a constructor, or an internal function annotated to return a
concrete type.

### 4d. Validation inside internal helpers

Grep: `isinstance\(|if not \w+:\s*raise|is None:\s*raise`

Flag `isinstance` / `TypeError` guards on private helpers reached only from typed
internal code.

**The carve-out that matters here.** These are domain invariants, not
defensiveness, and they stay even though every caller is internal:

- The typed raises in `services/exceptions.py` and their raise sites —
  `InsufficientQuantityError` (oversell), `RippingNotSellableError`,
  `TransactionStateError`, `InvalidTransactionError`, `EmptyTransactionError`.
- Anything enforcing an invariant from
  `specs/02-data-model.md#invariants-must-always-hold`: integer-cents money,
  whole quantities, FIFO lot drawdown, the void-not-delete rule, the
  market-value-weighted basis split and its rounding residual.
- The headless guard behind `tests/test_headless.py` (no Qt in domain/services).

User-facing entry points also keep their validation: the dialogs and views under
`ui/views/`, `services/importexport.py`'s import preview, and `config.py`'s
environment parsing (`TCGINVENTORY_DATA_DIR`, `TCGINVENTORY_DB`).

---

## Step 5 — Report before editing

Output the findings first, and do not start editing until they are written down.
The shape (paths below are illustrative — every line you emit must be a real
`file:line` you have read):

```
DEAD CODE
  <file>:<line>       — unreachable after `return` at <line-1>
  <file>:<start>-<end> — commented-out code block

DEFENSIVE PROGRAMMING
  <file>:<line> — redundant fallback `<expr>() or []`
                  (<expr> is annotated `-> list[str]`, never returns None)

FLAGGED FOR REVIEW (not auto-removed)
  <file>:<line> — `<name>` has no textual caller, but the Operations registry
                  dispatches by string id; confirm against `_runner` first
```

---

## Step 6 — Apply

Auto-remove, one edit at a time:

- Unreachable code after `return` / `raise`
- `if False:` / `if 0:` / `while False:` blocks
- Commented-out code runs of 2+ lines
- `except ...: pass` around provably pure logic (not the `RuntimeError` widget guard)
- Redundant `or {}` / `or []` on infallible internal calls
- Back-compat *shims* once every call site is updated

Flag, never auto-remove:

- Any function or class whose only possible reference is dynamic
- Exception handlers whose failure mode you could not pin down
- Anything in `domain/`, `data/migrations/`, or the money path in
  `services/transactions.py` / `allocation.py` — propose these in prose and let
  the user decide

Re-read the surrounding lines after each edit to confirm the block structure held.

---

## Step 7 — Verify

```bash
.venv/Scripts/ruff.exe format .
.venv/Scripts/ruff.exe check .
QT_QPA_PLATFORM=offscreen .venv/Scripts/python.exe -m pytest
```

PowerShell needs the env var set separately:

```powershell
$env:QT_QPA_PLATFORM = "offscreen"; .\.venv\Scripts\python.exe -m pytest
```

The suite runs parallel by default (`-n auto --dist loadfile`); add `-n0` when
iterating on one file. Two failures are especially informative after a sweep:
`tests/test_app_map.py` means you removed something `.claude/context/app-map.json`
still claims — fix the map in the same change — and `tests/test_headless.py`
means a Qt import reached `domain/` or `services/`.

Fix and re-run, up to 3 attempts. If a failure survives that, revert the edit
that caused it and move it to *Flagged*.

---

## Step 8 — Return

A concise summary:

- Dead code removed — count + files
- Defensive patterns removed — count + files
- Flagged for review — `file:line` each, one line of reasoning
- `ruff check` and `pytest` results, stated plainly (say so if either failed)

If the tree was clean: *"No dead code or unnecessary defensiveness found."*

## Step 9 — File the flagged items

**Every *Flagged for review* item becomes a ticket** — a symbol whose only
reference could be dynamic, an exception handler whose failure mode you could not
pin down, anything in `domain/`, `data/migrations/` or the money path. Those are
the findings this sweep is forbidden to act on alone, which is exactly why they
must outlive the conversation instead of scrolling away in a summary.

Use `/sdd-ticket`, `kind: dead-code`. One ticket per item, or one per cluster
when the same reasoning covers several. Each needs the `file:line`, why removal
*looks* safe, and the specific thing that has to be confirmed before it is —
"confirm against `OperationsView._runner`, which dispatches by string id" is a
ticket; "might be unused" is not.

Items you already removed need no ticket: they are in the diff.

Do not commit. Per this repo's branch + PR flow the user reviews the branch, and
rebuilds the exe after merge.

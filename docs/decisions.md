# Decision Log

Lightweight ADR-style record of significant decisions. Newest first.

---

### 2026-08-10 — The ticket lives on GitHub, and a pull request is an issue (46#)

- **Decision:** there is no local ticket storage. `.claude/context/tickets/` is
  deleted. A ticket is a GitHub **issue** — `X#`, the specification — and `start X`
  **converts that same issue into pull request `X`**, which is `X!`. One object, one
  number, two phases, and one copy of the text: the body.
- **Why this is the mechanism and not a convention.** `POST /repos/{o}/{r}/pulls`
  accepts an `issue` parameter in place of `title`/`body`, and converts the issue in
  place, keeping its number, body and comments. It is absent from the current REST
  documentation, so it was **verified before anything was built on it**: issue 46
  became pull request 46 on this repo, `gh issue list` stopped showing it, `gh pr
  view 46` started. The reason it works is the reason issues and PRs share a
  counter — **a pull request *is* an issue** with a head and base attached.
- **This replaces yesterday's answer, which was worse.** `41#` allocated the number
  by pushing a branch and opening a draft MR. That forced filing to happen after
  `EnterWorktree`, and would have needed an **empty commit** for the MR to have
  something to open against. Filing as an issue restores the rule `/task-planning`
  is emphatic about: the ticket exists before any code does.
- **One copy cannot disagree with itself.** The file-backed design put the number in
  three places — filename, heading sigil, `mr:` field — and needed a checker to keep
  them agreeing, because a disagreement meant `push` retitled one MR from a file
  describing another. That whole hazard class is gone, and `mr:` leaves the front
  block with it. What `check` validates instead is the **sigil against the actual
  phase**: a pull request must not still call itself a specification, which is
  reachable because `start` converts and retitles in two steps.
- **`review_gate.ticket_mr()` is deleted, one day after being added.** It read `mr:`
  out of the ticket file so a bare `ticket.py close` could still be bound to an MR.
  No file, no read — and it may not call `gh` instead, because it runs in a
  `PreToolUse` hook with a 20s budget (`HOOK-FAILS-OPEN`). So `close` takes its
  number again, which is the only form the hook can classify from argv alone.
- **Costs accepted, on the user's explicit call:** nothing works offline, `check`
  included; and ticket *prose* loses its edit history, since GitHub keeps no diffs
  of an issue body. An append-only log to preserve that history was offered and
  declined — the merged body is a permanent record, and the edit history of a brief
  has never been read.
- **Trade to remember:** the `issue` parameter is undocumented. If GitHub removes
  it, `start` breaks and the fallback is `41#`'s shape — open a normal PR and accept
  that its number differs from the issue's, or file no issue at all.

---

### 2026-08-10 — One work item, one number: the merge request is the ticket (41#)

- **Decision:** `X` is the **GitHub merge-request number**, and it is the only
  number a work item has. `X#` names the specification, `X!` the change that
  satisfies it. GitHub issues are retired: a ticket is a branch with a draft MR
  carrying the ticket file, and `deferred` work is a draft MR left open.
- **Why it could not be done as a convention over the old numbers.** Three
  sequences were in play — our ticket `NNN`, the mirrored issue `#N`, the PR `#N` —
  and GitHub allocates issues and pull requests from **one per-repo counter**.
  Measured here: issues took 2/7/10/12/13/14/16/17/22/31/32/36 while PRs took
  1/3/4/5/6/8/9/11/15/18…, one interleaved sequence with no overlap. A work item
  that files both can therefore *never* have them agree, whatever it is called.
  Alignment required dropping to one GitHub object per item; keeping the issue and
  renumbering tickets to match it (the rejected alternative) would have left `X!` a
  decorative label whose number was not the MR's.
- **The ticket file lives on its branch and never reaches `main`.** `close` runs
  *before* the merge: it rewrites the MR body with the finished write-up, then
  removes the file in the same branch. So the merge leaves nothing to sweep up, and
  the `Tickets: close N` follow-up commits (#33, #38, #39 were all just that) stop
  existing. Run after the merge, both guarantees are gone — the file is already on
  `main` — so `close` refuses in that state, with a test pinning it.
- **The number-reservation machinery is deleted, not ported.** `claim` existed
  because ticket numbers were minted locally and two sessions could both read
  `007`; it defended that with `taken_numbers()` over every ref plus `O_EXCL`
  markers in the git common dir. GitHub cannot issue one number twice, so the race
  it guarded no longer exists. `check` instead enforces that the three copies of the
  number on disk — filename, heading sigil, `mr:` field — agree, because a
  disagreement means `push` retitles one MR from a file describing another.
- **The gate's two referents collapse to one.** `review_gate.py --ref` used to mean
  a PR number under `--scope pr-merge` and our ticket number under
  `--scope ticket-close`, while admission keyed on `(scope, ref)` — two numbering
  schemes behind one flag, so landing one change needed two panels recorded.
  `--scope` is gone; `gh pr merge 41` and `ticket.py close` are two steps of
  landing `41!` and share a verdict. `close` no longer takes a number, so the hook
  reads it from the ticket file on the branch — a local read, never `gh`, because a
  `PreToolUse` call has a 20s budget.
- **The `ticket-close` binding carve-out expired with it.** It bound to local
  `HEAD` on the reasoning that closing a ticket described work that had already
  landed. `close` now runs on the branch before the merge, so the MR head is right
  for both, and one binding rule replaces a special case.
- **Costs accepted:** filing needs network and a pushed branch (`open` refuses on
  `main`); the branch is named for the slug, because the number does not exist until
  the branch is pushed; and `list` reads open MRs from GitHub, since `main` carries
  no ticket files for a local listing to find.

---

### 2026-08-09 — The rule registry names the invariant that broke (ticket 002)

- **Decision:** `INVARIANTS-DOC` is retired and replaced by **nine rules, one per
  numbered invariant** in `specs/02-data-model.md` — `INV-ONHAND-SUM`,
  `INV-CONSUMPTION-BALANCE`, `INV-REMAINING-BOUNDS`, `INV-REALIZED-RECONCILES`,
  `INV-EDIT-ATOMIC`, `INV-CONSIDERATION-BALANCE`, `INV-LOT-INTENT-REQUIRED`,
  `INV-RIPPING-NEVER-CONSUMED`, `INV-MISSING-EXCLUDED`. A finding now says *which*
  invariant broke instead of gesturing at a section containing nine of them.
- **Scopes are narrowed to what can actually break each one**, which is the half
  that pays. The catch-all was scoped `src/tcginventory/**/*.py`, so it fired on
  every UI file — a view that cannot issue SQL cannot break a ledger invariant.
  `ui/views/history.py` now draws zero `INV-*` rules; `services/allocation.py`
  draws exactly the two it can violate (4 and 6) rather than all nine.
- **The seals stay per-section, deliberately.** All nine hash the same block, so a
  spec edit invalidates all nine at once. That was the ticket's open question and
  the answer is that it is honest rather than annoying: unlike `docs/decisions.md`,
  which unrelated work appends to weekly, the invariants block changes only when
  the ledger model does — and on that day, re-reading all nine is the correct
  response. Teaching `section_text()` to anchor on a list item would add a schema
  field and a branch to the one function every rule depends on, to soften an alarm
  that should ring.
- **Three rules cited sources that never stated them.** The ticket found one
  (`CHECKER-NEEDS-NEGATIVE-TEST`, sealing all of `decisions.md`); checking the
  others found two more. `## Non-negotiable architecture rules` is seven bullets
  and says nothing about migrations or tests, yet `MIGRATIONS-FORWARD-ONLY` and
  `TEST-PROVES-BEHAVIOUR` both sealed it. A seal over prose that never made the
  claim can detect that *something* changed but never that *the rule* changed,
  which is the entire point of sealing an excerpt.
  - `MIGRATIONS-FORWARD-ONLY` → `specs/01-architecture.md § Data storage &
    migrations`, which states it verbatim. No new prose needed; it was simply
    pointed at the wrong file.
  - Both test rules → a new `## Testing rules` section in `CLAUDE.md`. Placed
    **outside** the non-negotiable block on purpose: eight rules share that one
    seal, so editing it would have forced eight reseals for an unrelated addition.
    Verified the existing seal `ac6adee038463cef` is byte-identical afterwards.
  - Rejected: anchoring at the critic's own definition. The registry exists so
    critics judge against *it* rather than their own prose, and such a seal only
    asserts that the critic still says what the critic says.
- **`anchor: null` on an append-only log is now rejected by `check`**, not left to
  be remembered. `rules.py anchor_problems()` fails any rule sourced from
  `docs/decisions.md` without a heading anchor. The ticket recorded this as a
  lesson for whoever swept next; a lesson in a ticket is not a mechanism, and this
  defect had already produced two reflex reseals in two days.
- **Added:** `UNIFIED-POSTING` (the one non-negotiable bullet nothing cited — type
  is inferred from which sides carry cards, never passed in), `ICONS-LUCIDE`, and
  `WORKER-RETIRE-NOT-REPLACE` from the 2026-08-07 startup-abort ADR. That last one
  is a genuine gap beside `QTHREAD-LIFETIME`: ownership and awaiting are not the
  same rule as *never overwrite a worker that may still be running*, which was the
  actual bug in three call sites.
- **Not added, and the count in the ticket was wrong.** It recorded five
  unenumerated `shared_ui` conventions; only `icons` states a rule. `charts`,
  `table_delegates` and `dialogs` are bare file lists, and `qt_models` is already
  covered by `QT-MODEL-SORT-ROLE`. Writing convention text into `app-map.json` so
  that something existed to seal would invert the registry's one discipline — it
  is an index, never the source. 24 rules → 35.
- **Consequences:** `rules.py scope` is meaningfully more selective, which matters
  because it decides how many critics get spawned. No app code changed; no
  migration. The nine share a seal, so expect nine reseals the day the invariants
  section is edited — that is the design, not drift.

### 2026-08-07 — One session, one worktree, one branch (ticket 008)

- **Decision:** a Claude session that intends to commit takes its **own git
  worktree** before its first edit, not in reaction to noticing a neighbour. A
  checkout has exactly one `HEAD`; since the move to a branch + PR flow, two
  sessions sharing the working tree means one session's `git switch` puts the
  other's commits on the wrong branch and into the wrong pull request.
- **Why not serialise instead.** A lock around branch switching would make the
  second session *wait* rather than corrupt — for hours, since branch ownership is
  long-lived. Worktrees let both sessions actually run. This also inverts the
  existing advice, which triggered on *detecting* another agent: detection is late
  by construction, because by the time a branch has moved under you it has already
  moved.
- **Enforced, not advised** — `.claude/hooks/branch_lease.py`, a `PreToolUse` hook
  in the same shape as the review gate. The condition is exactly one thing:
  **another live lease on this same worktree.** Alone in your own tree, `git add
  -A` and branch switching are perfectly safe and nothing fires. So the message it
  prints is never "be careful", it is "take your own worktree".
- **The lease registry lives in the git common directory**
  (`.git/tcgi-sessions/`), the one `.git` every worktree of a repository shares.
  `.claude/context/` would not do: each worktree has its own checkout of it, so
  two sessions would each read an empty registry and conclude they were alone —
  the exact failure the registry exists to detect. Stale leases are reaped after
  8 hours and released on `SessionEnd`, with `release --all` as a documented
  escape hatch, because a guard with no override gets switched off wholesale at
  the first false positive.
- **It tokenises rather than pattern-matches.** Ticket 003 recorded that the
  review gate's `GUARDED` regexes are "simultaneously too broad and too narrow" —
  they blocked a `gh pr create` whose *body prose* contained the word "merge".
  This guard runs on **every** shell call, where that defect would be far worse,
  so commands go through `shlex` with `punctuation_chars=True` (which keeps a `;`
  inside `-m "fix; the thing"` out of the separator set) and are classified by
  `argv[0]` and the subcommand. Read-only modes of destructive subcommands —
  `git stash list`, `git clean -n`, `git bisect log` — are explicitly allowed.
- **Fail open on confusion, closed on the detected condition.** Unparseable
  stdin, an unbalanced quote, no session id, an unreadable registry: all return 0.
  A `PreToolUse` hook that raises when it is confused stops every shell call in
  the session, which is worse than the collision it prevents.
- **The review gate resolves the repository from the caller's directory** instead
  of the hook file's location. Latent until now, and live the moment worktrees are
  adopted: `settings.json` invokes the hook through `$CLAUDE_PROJECT_DIR`, which
  stays at the launch directory, so the gate would have read the main clone's
  `HEAD` while the agent worked elsewhere — attesting to a commit nobody was
  merging. `out_of_scope` and `inherited` take the resolved repo for the same
  reason.
- **Session attribution is recorded but does not gate anything, and that is a
  reversal.** This ticket originally *blocked* a verdict recorded by another
  session, closing a real hole: a missing verdict used to stop a merge, so a
  foreign green one wrongly opened it and two sessions on one sha could trade gate
  passes. The opt-in redesign that landed the same day inverted the gate — a
  missing verdict now merges cleanly — which removes the hole entirely, because
  there is no longer anything to gain by trading. Keeping the block would have
  made a *reviewed* commit harder to merge than an unreviewed one. So `session`
  and `holders` stay in the artifact as attribution, and only `block` stops the
  gate. Worth recording as a decision rather than a deletion: the enforcement was
  correct under fail-closed semantics and became perverse under fail-open ones,
  which is not a thing you can see by reading either change alone.
- **The number collision happened while this was being written.** This work filed
  ticket 007; `main` independently filed *its own* ticket 007
  (`worker-threads-are-not-parked-at-shutdown`, issue #16) from another session,
  and the two only met as a merge conflict in the index. That is the failure this
  bullet describes, demonstrated rather than argued. Main's landed first, so this
  one renumbered to 008.
- **Ticket numbers are reserved, not read.** `ticket.py next` was `max(local) + 1`,
  and the local directory only holds tickets merged into the branch you are
  standing on. Numbers now come from every ref (`git log --all`), and `claim`
  reserves one with `O_EXCL`. The first implementation keyed the reservation on the
  *filename*, so `007-foo.md` and `007-bar.md` both succeeded — the collision
  rather than a fix for it. The test that forces the race caught it; the
  reservation keys on the number alone, in the shared git dir.
- **Verified against a real rival.** A second live session was holding this same
  checkout, on this same branch, while the work was being done — the guard blocked
  `git merge` with the other session's id and branch in the message, and left
  `git add <path>` and reads alone.
- **The risk critic found three things, all real, all fixed before merge.**
  (1) *Ambiguous ownership silently reopened the hole.* `owning_session()` returns
  `None` when **more than one** lease holds a tree, `record` wrote that as
  `"session": null`, and the gate's `if recorded and …` read null as "nothing to
  compare" — so in exactly the two-session case this feature targets, either
  session could use the other's verdict. The artifact now also carries `holders`,
  and a null session with several holders fails **closed**; a null with none stays
  admissible as the legacy case. (2) *`cd elsewhere && git commit` was a false
  positive* — rivals are detected against the tool's starting directory while
  classification is textual over the whole chain, so a commit in an unrelated
  scratch repo was blocked. Judging now stops at any directory change. The bypass
  that opens (`cd . && git switch`) is an accepted trade: this guard exists to stop
  an *unthinking* `git switch`. Notably the first attempt at this fix tried to
  resolve the `cd` target and failed, because posix tokenising eats Windows
  backslashes — the same reason the file reads flags but never path values.
  (3) *`git pull` was missing* from the HEAD-moving set beside `merge` and
  `rebase`; `apply` was too. Not excluded deliberately — never considered.
- **Consequences:** `.claude/worktrees/` is gitignored. Worktrees isolate code but
  not the app's SQLite files, so the lease records a suggested
  `TCGINVENTORY_DATA_DIR`. Merge conflicts do not go away — two sessions editing
  `main_window.py` still conflict, but visibly at merge time instead of silently
  at commit time.

### 2026-08-07 — Startup abort: a worker replaced while it was still running

- **Symptom:** the app died ~1s into startup, every time, with no traceback —
  `QThread: Destroyed while thread '' is still running` and exit `0xC0000409`
  (a Windows fast-fail, not a Python exception, so nothing was logged).
- **Cause:** `Inventory._prefetch_previews` and `History._start_resolver` /
  `_start_cropper` each called `requestInterruption()` and then **immediately
  overwrote** `self._worker`. Interruption only raises a flag; the thread runs
  to its next checkpoint. Overwriting dropped the last Python reference first,
  so the C++ `QThread` was destroyed underneath a live thread, which Qt answers
  by aborting the process. Dashboard, Calendar and Shows never had the bug —
  they *return early* when a worker is running instead of replacing it.
- **Why now:** latent for as long as the code existed. The optimization sweep
  cut view build from 2364ms to ~1205ms, and the faster startup meant the second
  refresh (`_warm_views` then the nav change) now landed while the first worker
  was still going. The instrumented run shows the shape exactly: four workers
  created, two of them replacements, then the abort.
- **Decision:** a shared `retire(worker, parked)` in `ui/views/_worker.py`.
  It interrupts, parks the worker in a per-view list, and lets `finished`
  release it. Kept out of the individual views because three sites had the bug
  and any future worker will want the same. **Not** `wait()` — blocking the UI
  on a mid-flight image fetch is what the original comment was avoiding, and
  that trade-off still holds.
- **Consequences:** interruption stays non-blocking; a retiring worker lives a
  moment longer than its view's reference to it. Regression test drives the real
  sequence (start → retire → drop the reference → `gc.collect()`), which aborted
  the interpreter before the fix.
- **Worth remembering:** a `QThread` GC'd while running kills the process with
  no Python-level trace, so it presents as "crashes at startup, no error." The
  test harness hit the same shape earlier the same day (`_drain_threads`).

### 2026-08-07 — History gets the auction editor's card preview, and the art memo keeps its URL

- **Decision:** the History detail pane splits horizontally — the per-lot table
  on the left, a **`CardGallery`** on the right showing every card in the
  selected transaction. Hovering a detail row focuses that one card
  (`show_single`); leaving falls back to the **selected** row, and with neither,
  to the full gallery. Same widget and the same two-mode toggle the auction
  editor and Price Lookup use (ADR 2026-07-16), so History inherits the
  rapid-rebuild fix rather than forking a preview.
- **Why:** the Cards column only ever showed three overlapping thumbnails plus a
  "+N" chip; a 32-lot purchase was unreadable as pictures. The leave-behaviour
  follows the platform-wide preview convention rather than the auction editor's
  simpler revert, because History's detail rows are *selectable* — a pinned row
  that vanished the moment the pointer left would read as a bug.
- **`_CARD_ID_MEMO` → `_ART_MEMO`.** The memo threw away the URL half of
  `art_for()`'s result and kept only the card id. The gallery needs the URL to
  fetch a card whose image isn't on disk yet, so the memo now stores the whole
  `(card_id, image_url)` tuple and `_art_id()` reads the id back out. One source
  of truth; the thumbnail resolver and the preview share every lookup.
- **The selected transaction's art resolves on the UI thread**, deliberately —
  the log's three-thumbnail resolve stays off-thread as before. `_show_detail`
  already runs several repo queries per line, `art_for` is two indexed lookups,
  the memo makes repeat selections free, and the image *bytes* still arrive
  through the shared bounded fetch queue. Measured on the real ledger: a 32-lot
  purchase resolves 26 distinct cards without a visible hitch. A third `QThread`
  in this file would have bought generation-guard bookkeeping and little else.
- **Consequences:** the detail table now shares its width, so its last two
  columns scroll rather than always fitting. The gallery carries a 226px minimum
  height so the default split never opens on a row of cards sliced off at the
  chin, and a 330px minimum width so two thumbnails fit per row.

### 2026-08-07 — Startup 2364ms → 1307ms, and a Qt model that was slow for the wrong reason

The first run of `/optimization-sweep`. Four fixes, each with a measured before
and after — and two of the sweep's own conclusions overturned on the way.

- **Three things the app did not need to do at all.** `index_status()` built a
  full ONNX inference session to read a version string that is
  `f"onnx:{model.stem}"` — 0.13s and ~110MB, on the UI thread, twice per Models
  build; `shared_embedder_version()` reads it from the path and defers to an
  already-built embedder so the corrupt-model fallback still reports itself.
  `env_size()` had a fingerprint cache that could not survive a restart, so the
  first Models refresh of every launch walked 37k files; the fingerprint still
  decides whether the number is good, it is just persisted now (0.5ms cold
  against 336ms). `scheduling.is_enabled()` shelled out to `schtasks` on every
  Operations refresh. **Models build 1155ms → ~200ms; Operations refresh 81ms →
  49ms; total build 2364ms → 1307ms.**
- **The Qt model was slow for a reason nobody would guess, including us.**
  `data()` fired 31,598 times for a 1,105-row grid, which reads as per-cell
  display derivation across 20 columns. It is not. **Every one of those calls is
  `Qt.UserRole`** — the sort role — over only **1,105 distinct cells**, because
  `QSortFilterProxyModel` re-reads both operands of every comparison and asked
  one cell **84 times**. `Qt.DisplayRole` is never called during a reset, so the
  display cache we set out to build would have bought nothing. The costs were the
  role-comparison chain (`role == Qt.SomeRole` is a rich comparison against a
  wrapped C++ enum) and the 96.5% repeat rate. Module-level `int` role constants,
  an early return for the sort role, and a `(row, column) -> key` cache cleared
  in `set_rows()`: **Inventory refresh 340ms → 108-139ms, build 306ms → 128ms.**
  Verified against a Python sort on all 19 sortable columns, plus a stale-cache
  probe.
- **Two measurement traps, both of which produced a confident wrong answer
  first.** A `data()` returning `None` from an empty dict looked like a 36.8ms
  "caching ceiling"; it was measuring the sort short-circuiting on invalid
  variants. And PySide6 binds virtual overrides when the class is created, so a
  `data()` counter monkeypatched afterwards is never called and silently reports
  zero — subclass instead. The rule now in the skill: **count the roles before
  designing a cache.**
- **The same shape in History, measured before it was assumed.** 5,132 calls for
  262 rows, again 100% sort role, 94.9% repeats, one cell asked 124 times. Same
  fix: `set_rows()` 40.0ms → 15.5ms, build 128ms → 87ms, refresh 151ms → 70ms.
  **`recognition_history_model` was left alone** — its model holds zero rows on
  this dataset, so there was nothing to measure, and a third model "fixed by
  analogy" is exactly the change this sweep exists to prevent.
- **Total view build is now 1205ms against 2364ms** at the start of the sweep.
- **The collection-value series stopped redoing its own work.**
  `_reconstruct_collection_series` was O(dates × lots) — 85 dates × 1,105 lots ≈
  94k iterations — re-testing each lot's intent/missing flags per date,
  re-summing its disposals from scratch, and walking lots not yet acquired. It
  now filters once, bisects a cumulative disposal series, and advances a pointer
  through lots in acquisition order. **59.3ms → 39.2ms**, and
  `collection_value_series()` 42.6ms → 30.1ms. Verified identical point-for-point
  across four intents × three cutoffs, with the `now` anchor checked separately —
  this draws money on the Dashboard, so "close" would not have been a result.
- **`statistics` was the wrong tool for the volatility column.**
  `_coefficient_of_variation` used `statistics.mean`/`pstdev`, which compute in
  exact `Fraction` arithmetic — correct, and far too slow for 797 short lists of
  integer cents on every Market refresh. Written out longhand: the 797 series
  (23,583 points) go **16.44ms → 1.68ms**, `signals()` 40.0ms → 24.3ms, and the
  Market refresh 94ms → 64ms. The two agree to within **3.6e-14 percentage
  points** and round identically at the 2dp the column shows, which is the only
  reason the swap is allowed — it is a displayed statistic, not money, and the
  money rule still forbids this anywhere near cents.
- **Deferring the vision imports off startup: measured and rejected.**
  `import main_window` is 383ms, of which `services.inference` is 159ms
  (onnxruntime 78 + numpy 70), pulled by one eager `write_back` import in
  `auctions.py` and `transaction.py`, plus a second numpy chain through
  `history.py → corpus → exemplars`. Deferring both does exactly what it
  promises — imports drop to 230ms and numpy, onnxruntime and PIL leave the
  startup path entirely — and total startup gets **worse**: with the eager import
  gone, `RecognitionView(prewarm=True)` loads onnxruntime itself on a background
  thread against the import lock, its build goes 35ms → 260ms, and TOTAL BUILD
  goes 1228ms → 1324ms over three runs each way. Reverted. The cost does not
  disappear, it moves somewhere worse — and it would only pay off alongside a
  change to prewarm, which is out of scope by decision.
- **Speeding up the build made every refresh look slower**, which is worth
  recording because it is not intuitive. Removing 950ms of construction let
  Recognition's ~2s background prewarm overlap the refresh phase, so the harness
  measured GIL contention: Dashboard read 177ms → 318ms with no change to
  Dashboard. Settling before timing puts it back at 184ms. The same
  async-misattribution trap already documented for RSS, now on the clock.

### 2026-08-07 — The ledger is not the slow part, and a skill that knows it

A new `/optimization-sweep` skill (`.claude/skills/optimization-sweep/`), built
the way `dead-code-sweep` was: a playbook grounded in numbers measured on this
repo, plus a harness (`scripts/perf.py`) that regenerates them. The harness
measures three axes separately — latency, resident memory, bytes on disk —
because they disagree, and a sweep watching only the stopwatch misses the largest
item in the app.

- **The headline finding is negative, and it is the reason the skill exists.**
  The ledger is ~1,000 rows and already fast; every real cost is in the catalog,
  the image cache and the model stack. `item` 903, `lot` 1,105, `transaction` 262
  against `card_catalog` 53,289, `card_embedding` 49,551,
  `catalog_price_snapshot` 223,893 and 50,546 cached PNGs. Measured:
  `all_positions()` is a textbook N+1 — two queries × 903 items — and costs
  **25 ms**, called by no view; every `ReportingService` method but
  `collection_value_series` (44 ms) runs in **0.2–3.5 ms**; VACUUM reclaims
  **nothing**, the freelist is 0 pages in all four databases. A generic
  performance pass would have spent its whole budget there. The skill's
  do-not-touch list is as long as its findings list, with a number beside each
  entry, because "measured, and not worth changing" is a complete result.
- **Where the time actually goes:** ~2.0–2.4 s of view construction on every
  launch, of which `ModelsView` is ~1.1 s (`catalog_index.index_status` called
  twice, each building an ONNX embedder; `trainingenv._walk_size` at 0.28 s
  across 6,139 `nt.scandir` calls); `inventory_model.data()` fires **31,598
  times** for a 1,105-row grid, re-deriving strings and fonts per role; and
  `InventoryView.refresh()` spends 0.37 s in `image_for()` querying the 53k-row
  catalog 27 times.
- **Memory earns its own axis.** `RecognitionView(prewarm=True)` costs **112 MB**
  resident, at startup, for every user whether or not they open Recognition —
  while its build time barely moves (198 ms vs 182 ms with prewarm off). No
  amount of latency work would have surfaced that. Total resident with all views
  built: 256 MB.
- **Storage likewise.** 8.7 GB of app data, of which the app owns 2,212 MB as
  **50,546 PNGs** (`services/cardimages.py` writes `.png` unconditionally).
  Re-encoding a 25-file sample: WebP q85 averages 19.7 KB against PNG's 48.8 KB —
  60% smaller, ~1.2 GB. Flagged rather than done: 50k existing files need a
  migration path, and that is the real cost of the idea. Same treatment for the
  float32 `card_embedding` (1,589 B/row, 78.8 MB) — halving it is easy, but
  ADR 2026-08-06 prices a re-embed at ~68 minutes and retrieval accuracy has to
  be benchmarked, not assumed.
- **Correction, same day — the two "redundant indexes" were load-bearing.** The
  first pass reported `card_catalog.idx_catalog_name` as a duplicate of
  `idx_card_catalog_name`, and `idx_card_catalog_set_name` as a prefix of
  `idx_catalog_set`. Both wrong, and wrong in a way worth keeping: the pairs
  differ by **collation**, which `PRAGMA index_info` does not report. SQLite
  chooses between a BINARY and a NOCASE index by the collation of the
  comparison, and plans confirm all four are live — `name = ?` uses one,
  `name = ? COLLATE NOCASE` the other. Dropping either would have sent a real
  query to a full scan of 53,289 rows. The detector now reads
  `PRAGMA index_xinfo`, which carries collation and direction, and reports no
  redundant indexes anywhere. The general rule is in the skill: never propose
  dropping an index on column names alone.
- **Four measurement traps are baked into the harness** because each one produced
  a wrong answer first: `data.db.connect()` defaults to `:memory:`, so a bare
  call profiles an empty database that answers instantly; Windows will not delete
  the staged copy while SQLite holds it; Qt teardown without an event loop exits
  with an access violation (the same total behind `conftest.py`'s
  deferred-deletion fixture); and **RSS deltas misattribute across async work** —
  the 112 MB above first appeared under `MarketView`, which allocates nothing,
  because prewarm lands a second after the constructor returns. `all` runs each
  axis in a fresh subprocess for the same reason.
- **Two ideas are ruled out by decision, and the skill says so** so they are not
  re-proposed each sweep: making view construction lazy (the ~2 s prize, but an
  architecture change touching the splash contract and the nav indexes), and a
  performance-regression test (the suite runs `-n auto`; a timing assertion in it
  would be flaky). Results go here instead.
- **Research is bounded, and it ends in a measurement.** Three in-repo questions
  first — can it be cached, can it move behind `_worker.TaskWorker`, can SQLite
  do it — each with precedent and no new dependency, each backed by a table of
  concrete mechanisms (covering/partial/expression indexes, FTS5, `executemany`,
  `blobopen`; streaming instead of `fetchall`, `__slots__`, numpy views;
  `canFetchMore`, `dataChanged` over `beginResetModel`). Only then outward, and
  then the candidate must pass packaging, offline, layer, platform and
  maintenance tests explicitly. A `capabilities` probe reports what is already
  paid for, because most "use library X" ideas are something SQLite, numpy,
  Pillow or Qt already ships — and it catches two-sided constraints a naive
  benchmark misses: **Pillow writes AVIF and Qt cannot read it**, so the
  smallest format is not a usable one. Above all, **a mechanism is not reported
  until a spike has run it against staged real data** and printed a before and
  an after. The worked example in the skill makes the point against itself: 300
  single-row lookups replaced by one `IN (…)` query measured 1.35 ms → 0.35 ms —
  3.8× faster, one millisecond, not worth shipping.

### 2026-08-07 — Recognition: a mode trap, not a broken Delete

- **The delete handler was always correct; nothing could reach it.** Both delete
  controls act on `QGraphicsScene.selectedItems()`, and `_Scene.mousePressEvent`
  accepted *every* left press while draw mode was on to start a rubber band —
  so a click on an existing box never reached Qt's selection handling. Draw mode
  is deliberately persistent across images, which means the state you are in
  right after drawing boxes is exactly the state where no box can be selected.
  Delete then ran against an empty selection and silently did nothing.
- **A press on a box is held, not swallowed.** In draw mode a press that lands on
  an existing box is deferred: released in place it selects, moved more than 4px
  it draws a new box over the old one. The slop is measured in **screen** pixels,
  not scene pixels — a scene-space threshold changes meaning with the zoom, and
  on a 4000px photo fitted to an 800px view any jitter would read as a drag.
  The alternative was single-shot draw (auto-return to Select after each box),
  rejected because you normally draw eight boxes in a row and re-arming each time
  costs more than it saves.
- **One place owns the box outline.** Selection and hover both wrote the pen, and
  hover-out reset it to the resting pen — so brushing the cursor over a selected
  box made the selection invisible while it was still selected. `_BoxItem.refresh_pen`
  now resolves it: selection (`info`) outranks hover (`accent_ink`) outranks rest
  (`accent`). It takes the new state explicitly because during `ItemSelectedChange`
  the flag is not applied yet and `isSelected()` still reports the old value.
- **Two more ways to delete, because one invisible way is none.** Delete/Backspace
  is bound to the canvas — parented to the view with `WidgetWithChildrenShortcut`
  so it cannot fire while a card name is being typed in the Segments panel — and
  every segment row grew a trash button, which needs no mode and no selection.
  An empty-selection Delete now says what to do instead of no-opping.
- **Survivors keep their numbers.** A card's front and back join on `(pair_group,
  box number)` (ADR 2026-08-05), so closing the gap left by a delete would
  silently re-pair every card past it. Deleting box 2 of 3 leaves `[1, 3]` and the
  next box drawn is 4.
- Boxes are only persisted by an explicit Save, which writes a fresh capture and
  then clears the session — so deletion is purely in-memory. No service, no
  repository, no migration.

---

### 2026-08-05 — Condition grading: one grade per card, ordinal regression, no torch

- **Measured first, and it changed the plan.** The shipped whitening heuristic
  scores **33% against 76% for always guessing NM** on the labelled backs, mean
  1.52 grades off, and grades all DMG cards as NM. It isn't weak — it measures the
  wrong thing: `_border_whitening` reads near-white pixels in the crop's border
  band, which tracks *how much mat the drawn box included* and the photo's
  lighting. Crops on a light mat scored 0.11–0.27 whitening while damaged cards on
  a dark background scored 0.007–0.107. `LearnedConditionModel` now takes
  precedence; the heuristic stays only as a last resort.
- **The unit is a physical card, not a region.** A card is photographed front and
  back, and it has one condition. Faces join on `(pair_group, which pair inside the
  group, box number)` — `pair_group` turns out to be allocated **per session, not
  per photo pair**, and getting that wrong collapsed 33 cards into 16.
- **Fronts are input views, not labels.** Every front in the corpus read `NM`
  while its paired back read MP/LP/DMG/HP, because `NM` is the default on a new
  box. So a back's grade is the label when present, the front is a fallback, and
  disagreements are reported rather than averaged. **The Recognition window now
  mirrors a condition edit onto the paired face (most recent edit wins)**, so the
  divergence cannot recur.
- **Two normalisations before any measurement:** tighten the crop to the card, and
  flatten exposure. Without them a model learns the photograph — the exact failure
  the heuristic demonstrates.
- **Ordinal regression, not five-way classification.** The scale is ordered:
  calling an NM card LP is a near miss, DMG is a disaster. One continuous wear
  score plus rounding shares evidence across grades instead of splitting a small
  corpus five ways, and the headline metric is **mean grade distance**.
- **Decision: this model does *not* use the training environment.** It is a ridge
  regression over ~20 named measurements (`condition_features.py`), which numpy
  fits in milliseconds, so it trains in-process as an ordinary background
  operation — no ultralytics, no 280 MB — and its artifact is a few KB of readable
  JSON coefficients rather than an ONNX. A coefficient like
  `front_edge_grad_max +0.73` states what the model believes. The CNN path stays
  open behind the same `assess()` interface for when the corpus earns it.
- **Validation is leave-one-capture-out**, since every photo has its own lighting
  and mat; a card must never be scored by a model that saw its own photo. Baselines
  are measured on the same folds so the log records whether the model beat doing
  nothing.
- **Promotion is kind-aware:** lower mean grade distance wins, the opposite of the
  detector's mAP, so condition cannot share `should_promote()`.
- **Gate:** each grade needs **8+ cards across 3+ separate photos**. Counting
  distinct photos is the part that matters — a grade seen in one photo teaches that
  photo's background. Measured on 44 graded cards the fitted model scores 14% exact
  and 1.52 grades off against a 0.84 baseline, i.e. **worse than guessing**, so the
  gate refuses and the Operations tile greys its button and lists what is missing.
  That is the framework working, not failing.

### 2026-08-04 — Detection is trained-only, and every training run is logged

- **Decision (part 1):** delete the **classic-CV proposer**. Detection is now
  exclusively the trained YOLO-nano model, with **no untrained fallback**:
  `build_pipeline` uses the model when one is installed and proposes nothing when
  it isn't, and the Recognition window keeps only the YOLO toggle.
- **Why:** the projection-profile proposer only worked on cards laid out with
  gaps on a plain mat, merged anything touching, and correcting its proposals
  cost more than drawing the boxes by hand. Owner's verdict: "garbage, and quite
  frankly doesn't have any reason to be in the system at all."
- **Consequence, accepted deliberately:** photo/clipboard autofill and the YOLO
  toggle **propose nothing until a detector is trained** (none exists yet). No
  detections beats wrong detections, and hand-drawn boxes are what feed the
  corpus that trains the model.
- **Decision (part 2):** training is **never** triggered by the app. Supplying
  images or saving a capture only grows the corpus — and re-embeds crop
  exemplars, which is why the *identifier* improves live while the *detector*
  does not. Retraining is an explicit act triggered from the **Operations**
  window ("Models & signals"), executed by `training/train_detector.py` outside
  the frozen app because ultralytics/torch are deliberately not shipped.
- **Correction (2026-08-04, measured):** the training stack is **~280 MB of
  downloads / ~1 GB installed**, not the ~2 GB quoted in earlier entries — the
  Windows PyPI `torch` wheel is CPU-only (122 MB, no CUDA). Also `onnx`,
  `onnxslim` and `onnxruntime` are **required and not** in a plain
  `pip install ultralytics` (they're behind its `export-base` extra); without
  them a run trains to completion and then fails at the ONNX export.
  `services.training.TRAINING_REQUIREMENTS` now holds the authoritative list.
- **Decision (part 3):** log every attempt in **`detector_run` + `detector_epoch`
  (vision.db)**: the corpus snapshot trained on (images / boxes / cards + a
  **fingerprint** of the exact (image, box) set), the split, the
  hyperparameters, mAP50 / mAP50-95 / precision / recall / best epoch, the
  artifact (path, sha256, bytes), and whether it was **promoted** — promotion
  assigning the incrementing **model version** shown as "detector v*N*".
  `services/training.py` reads it back Qt-free: `corpus_snapshot`,
  `detector_status` (installed? which version? trained on what? **stale** when the
  fingerprint no longer matches), `parse_results_csv`, `should_promote`.
- **Why a fingerprint and not just counts:** counts can't tell "20 images, two of
  them re-boxed" from "the same 20 images", and *stale* is the question the
  Operations tile has to answer. Relabeling a card deliberately does **not**
  change it — identity is the identifier's business, not the detector's.
- **Why a promotion gate:** otherwise the newest run always wins, and a run on a
  thin val split can quietly replace a better model. A first model always
  promotes; after that a candidate must beat the incumbent's mAP50-95 (by
  `--promote-margin` if set) or be forced with `--force-promote`.
- **Recorded before training, not after:** a killed or crashed run leaves a
  `running`/`failed` row with its reason instead of vanishing.
- **Caveat on the numbers:** at 20 images / 88 boxes the default 20% split leaves
  **4 validation images**, so mAP will swing hard. Metrics are worth *recording*
  now and worth *gating on* at ~100+ images.

### 2026-08-04 — Operations window (M-Ops1): the File menu becomes a screen

- **Problem:** ten maintenance jobs lived only in the File menu, where a label
  can't say that *Import items* adds cards while *Import purchases* posts
  transactions with cost basis, can't show when anything last ran, and leaves
  nowhere to put the next one. Long jobs reported through a status-bar message
  and a modal that was dismissed and lost.
- **Decision — operations are data.** `services/operations.py` (Qt-free) declares
  each job: id, group, title, blurb, verb, icon, kind (`dialog` / `background` /
  `toggle`), staleness window. `ui/views/operations.py` renders the screen from
  that registry and holds the runners, so **a future operation is one entry plus
  one runner** and cannot drift out of visual line. A `models` group is
  **reserved** for the coming metrics/triggers work; groups with no entries render
  nothing, so it costs nothing until used.
- **Run log now, not later** (owner's call): migration `0019_operation_run`
  records op, start/finish, ok, summary and **source** (`ui` | `scheduled` |
  `startup`). Imports and exports had no history at all; everything else derives
  from data that already existed (price snapshots, `card_catalog.updated_at`, the
  price-history file, the backup files, the scheduled task). The same table backs
  the Activity list across restarts, and `source` is what a later
  "since you last opened" digest will read.
  - **Not** added to `importexport._ALL_TABLES` — that list is the ledger
    (`card_catalog`, `card_show`, `calendar_entry` are already excluded); a run log
    is local telemetry and must not be wiped or replayed by a restore.
  - `app.refresh_prices_headless()` and the startup backup record their own runs,
    so the window reports what happened while the app was closed.
- **File menu trimmed to Operations + Exit.** Two live trigger paths for one job
  drift apart, and re-pointing "Backup database…" at a *window* breaks the promise
  that it opens a save dialog. The three `QThread` workers moved from
  `main_window` to the screen that shows them; the window is a shell again, and
  the nav index constants are now derived from `NAV_ITEMS` (inserting a
  destination silently invalidated the literals).
- **Status honesty:** a tile's state comes from the thing itself, not from the
  fact that a run happened — the price tile reads the latest snapshot, so a
  refresh that priced nothing doesn't claim success. A successful-but-old run can
  still read **Stale** (the full-backup export has a 14-day window).
- **Shipped in the same pass** (they were what the owner picked; scheduling them
  into a later milestone was the wrong read):
  - **Maintenance & diagnostics** group — `integrity_check` + `foreign_key_check`,
    VACUUM reporting what it reclaimed, and **re-index the recognition corpus**
    (`exemplars.reindex_all`), whose tile carries the corpus stats and flags
    labelled-but-unindexed cards. These arrive through one generic `_TaskWorker`
    that runs `task(conn) -> summary`, so the next such job is a function.
  - **Card-shows scrape** on demand, previously reachable only as a side effect of
    toggling a state in Calendar. It reports *stored upcoming* shows, not
    `upcoming()` (which counts only ones marked attending — 0 for most people).
  - **"While you were away" digest** — unacknowledged `scheduled`/`startup` runs,
    headed at the top of the screen, failures called out, dismissible.
    **Acknowledged by run id, not timestamp:** `now_iso` is second-precision and a
    nightly task logs several runs inside one second, which a timestamp cutoff
    silently swallowed (caught by a test).
- **Deferred to M-Ops2:** restore (both the `.db` swap and the existing unexposed
  `importexport.import_all`), behind a typed confirmation and a pre-restore
  backup, using the danger tile already built.

### 2026-08-04 — Card art: TCGplayer product photo where the catalog can't pin the printing

- **Bug:** *Dedenne-GX · Unbroken Bonds · 195a/214* displayed **sm10-57**, the
  plain GX. `195a` matches no catalog number, so `image_for` fell back to "any
  same-name card in this set" and `max()` over an all-equal tie-break key returned
  whichever row the candidate pool happened to hold first.
- **Why it regressed:** the EN catalog moved from the pokemontcg.io dump to the
  **TCGdex API** (ADR 2026-07-14), and TCGdex carries only mainline printings — no
  `sm10-195a` row, and `…/sm10/195a/low.png` 404s. Letter-suffixed alt arts had
  resolved directly before the swap. (pokemontcg.io's API now answers 500, so it
  is not a source to fall back to.)
- **Rejected: stripping the suffix** (`195a` → `195`). An alt art is a *different
  artwork*, not a variant of #195's, so the base full art is simply another wrong
  picture.
- **Decision:** keep `CardCatalogRepository.image_for` catalog-pure (its result is
  also used as a price-history key in `services/trends.py`) and add
  `cardimages.art_for(conn, name, set, number)`, which layers the sources:
  1. the **catalog** when it *pins* the printing (number agrees, or no number was
     given) — clean, consistent scans;
  2. else the **TCGplayer product photo of the matching ledger item**
     (`tcgplayer-cdn…/product/<id>_400w.jpg`, cache key `tcgp-<id>`). A product id
     names one exact printing, so it covers what the catalog lacks. The Japanese
     catalog already stores images in this form; when both routes name the same
     product the catalog entry is kept, so nothing is downloaded twice.
  3. Items disagreeing on the product → nothing, the same no-coin-flip rule the
     catalog matcher follows.
- **Also fixed in `image_for`:** an **exact (flattened) name beats containment**,
  and an ambiguous name-only match returns nothing. Containment runs both ways, so
  *"Giovanni"* matched *"Giovanni's Machamp"* and won the arbitrary tie — that item
  had been showing the Giovanni trainer card.
- **Measured against the live 890-item inventory:** 868 unchanged, **6 re-pointed
  from a wrong card to the right one** (both alt arts — Dedenne-GX 195a and
  Jirachi-GX 79a — plus three SVP promos and Giovanni's Machamp), **15 newly
  resolved** that had been blank (MEP promos, trainer-kit and promo cards), **0
  lost**. 805 EN catalog rows carry no image; the product fallback now covers the
  owned ones.
- **Pricing was already right** — these cards resolve to the genuine
  "Alternate Art Promos" product; only image lookup was affected. A consequence
  worth noting: art and price now come from the same product link, so a wrong
  image is a visible symptom of a wrong price link.

### 2026-08-04 — Dashboard highlights are links into the owning screen

- **Decision:** A Top Gainers / Losers / Sellers row opens that card **selected in
  Inventory**; an Upcoming Shows row opens that **day on the Calendar**. The
  Dashboard emits intent (`item_activated(item_id)` /
  `show_date_activated(iso)`); `MainWindow` routes it, and the destination screens
  own the "go here" verbs (`InventoryView.select_item`, `CalendarView.show_date`).
  Dashboard stays ignorant of the nav stack.
- **Selection, not a dialog:** selecting the row reuses the existing selection
  path, so the dossier (art, facts, price-history chart) fills in exactly as if
  the user had clicked the row — no second detail surface to maintain.
- **Three non-obvious choices:**
  - **Navigate first, then select.** Switching screens can refresh the grid,
    which rebuilds rows and drops the selection; selecting after the switch
    survives it.
  - **Filters that hide the target are cleared** (search, In-stock, Review,
    intent/sold/graded segments, via the widgets so the bar stays honest).
    Otherwise "show me this card" silently does nothing — e.g. a fully-sold Top
    Seller while the In-stock chip is on.
  - **An in-stock lot wins** when a card has several rows, so a partially-sold
    card lands on the copies still on hand.
- **Consequences:** highlight rows now carry a hover wash + pointing-hand cursor
  (`_ClickableRow`), which required `WA_StyledBackground` — that also fixed the
  rows' hairline separators, which had never actually painted.

### 2026-07-31 — Cleanup pass: reconcile spec 02 / CLAUDE.md with the code

A spec-conformance audit found the money core sound (Qt-free layers, integer
cents, unified posting, FIFO, $0 ripping/missing all hold) but `specs/02-data-model.md`
had drifted from later ADRs. Reconciled the docs (no code change) so the spec
matches reality:
- **Item identity now includes `holo_pattern` + `edition`** (migration `0011`) —
  a reverse-holo / 1st-edition copy is a distinct item. Added to the identity-key
  prose + `item` schema block.
- **`lot.sku` is non-unique, per-acquisition-line** (shared across the split
  lots of one line) — the UNIQUE index was dropped in `0016`. Fixed the schema
  block + SKU note.
- **Deal value of cards given out = current `item.market_value_cents`**, not a
  typed "agreed value" (ADR 2026-07-13). Annotated the cost formula + invariant 6.
- **Hard-delete carve-out:** the "never hard-deleted" rule now names the
  sanctioned `delete_inventory_entry` exception (ADR 2026-07-11) in both spec 02
  and CLAUDE.md.
- Invariant 6 now also states the **outgoing** reconciliation (`Σ out
  unit_price×qty == net_consideration_out`), made exact by the multi-qty
  disposal fix in the same cleanup pass.

### 2026-07-31 — M-Market: liquidity / markup / volatility analytics (completes spec 06)

- `services/market_signal.py` adds a "monetary significance" layer over the
  **ledger + per-item price snapshots** (no vision, no external feeds): per
  resale card — **liquidity** (sell-through = units sold / acquired,
  quantity-weighted days-to-sell from `lot.acquired_at` → sale
  `transaction.occurred_at`), **markup** (realized margin on sales, unrealized
  markup of on-hand stock vs FIFO basis), **volatility** (coefficient of
  variation of `price_snapshot` over a window). `overview()` aggregates the
  portfolio.
- **Deliberately separate from `trends.py`** (the concurrent trend framework,
  which is price *direction* / % change): these are orthogonal risk/quality
  signals, so M-Market is a new independent service + a new **Market** nav screen
  (`ui/views/market_signal.py`, sortable read-only table) rather than folded into
  trends/inventory. Reuses the proven `top_sellers` ledger joins and
  `PriceSnapshotRepository.history_for_item` (per-item series — no item→card_id
  bridge needed).
- **Spec 06 (recognition + market) is now feature-complete** end to end:
  identity/detection/condition each have a usable-now path + a drop-in trained
  model, the corpus self-builds and feeds back, and market analytics close it out.

### 2026-07-31 — M-V3: condition assessment from card backs (heuristic + scaffold)

- Condition grades on the NM/LP/MP/HP/DMG scale from the **back** (identity uses
  fronts). `services/condition.py` `assess_condition` returns grade + whitening
  fraction + confidence. **Usable-now heuristic:** counts near-white **and
  low-saturation** pixels in the border band — the low-saturation gate is the key
  trick so the card's own bright **yellow** border isn't scored as edge fraying.
  Weak but real; the trained `OnnxConditionModel` (5-class) supersedes it when
  `condition_model.onnx` is present (bundled via `models/*.onnx` like DINOv2).
- **Corpus built in the Recognition window:** a "Card backs (condition)" checkbox
  tags saved images `side='back'`; "Assess condition" grades each box; a
  Condition column persists onto the region. `export_condition_crops` (backed by
  `condition_regions`, which filters `side='back'` + a condition) +
  `training/train_condition.py` (offline, dev-only torch → ONNX) train it later;
  torch never enters the app.
- **Same usable-now + scaffold pattern** as the detector/identifier: heuristic
  works today, the model is a drop-in `.onnx` when a corpus exists.

### 2026-07-31 — Part 2 feedback: confirmed autofill writes back to the corpus

- Closes the learning loop: `services/corpus.py` `ingest_labeled_scene` stores a
  confirmed scene as a capture + a region per box (**detector** data) + a crop
  exemplar per identified box (**identifier** data). `inference.build_pipeline`
  now returns a `Pipeline` object exposing `repo` + `recognizer`, and
  `inference.write_back` feeds accepted cards back — **best-effort** (never
  disrupts the entry; no-op when the pipeline has no repo, e.g. a test fake).
- **Corrections are captured, not lost:** the `AutofillDialog` Name field
  autocompletes from the catalog, so re-picking a card on a disagreement yields a
  real `card_id` → a new exemplar for the *correct* card (the highest-value
  label). Free-typed text stays unidentified (region only). `resolved_id` keeps
  the id from an accepted-unedited guess or a re-pick.
- Every autofilled purchase/auction now also trains the models; the Recognition
  window remains the dedicated bulk-annotation path.

### 2026-07-31 — Part 2: image inference autofill in Transaction & Auction

- `services/inference.py` ties the detector + identity matcher together:
  `run_inference(image) -> [Detection]` (box + top-guess CardLabel + cosine +
  confident flag + alternatives), `build_pipeline(conn)` picks the trained YOLO
  detector when present (else classic-CV), the exemplar matcher, and a catalog
  label resolver. Degrades gracefully: **detect-only** on an empty corpus
  (boxes with no identity, review-flagged), identification sharpens as the
  corpus grows.
- **Shared review dialog** (`_autofill_dialog.AutofillDialog`): the scene with
  numbered boxes beside an editable table (name/set/number, confidence %, ⚠ low,
  include + qty). Returns `AcceptedCard`s — keeps the guessed `card_id` only when
  accepted **unedited**; flags low-confidence / edited / unidentified for review.
- Both entry windows get an **"Autofill from photo…"** button that runs the
  pipeline and adds confirmed rows, **reusing** each window's `apply_catalog_card`
  + `_request_price` (so identified cards get their TCGplayer price). The pipeline
  is lazily built (loads the ONNX models) and injectable for headless tests.
- **Scoped read-path first:** unidentified boxes are named in the dialog before
  they become rows (name-less rows are dropped). Writing confirmed entries back
  into the corpus (capture + regions + exemplars) is the next increment.

### 2026-07-31 — M-V2: crop-exemplar retrieval makes identification usable on ingest

- **The insight:** the matcher's weakness was comparing a photo **crop** against
  clean **catalog renders** (domain gap). The fix that needs *no training* and
  works from the first labels: embed each labeled crop and retrieve **crop-vs-
  crop**. So `Recognizer.suggest` now scores a card by its **best** vector —
  accumulated crop **exemplars** first (`region_embedding`, keyed by
  region + model_version), catalog reference only as a cold-start fallback for
  unlabeled cards. Every save auto-indexes its crops → suggestions improve with
  ingestion (nonparametric active learning). Backward compatible.
- **Fine-tune (Part 2) scaffolded, torch stays out of the app:**
  `exemplars.export_identity_crops` + `training/train_identifier.py` (offline,
  dev-only; ArcFace on DINOv2 → `identifier_finetuned.onnx`). `model_path()`
  prefers a fine-tuned model when present and bundles it via the `models/*.onnx`
  glob like DINOv2; a new embedder version re-embeds the caches automatically.
- **Why exemplars before fine-tune:** exemplar retrieval is the sample-efficient,
  immediately-usable form of "the flow of data improves it"; the parametric
  fine-tune is the higher-ceiling upgrade for when the corpus is large. Ship the
  usable path now; keep the ~2 GB training stack a manual, offline step.

### 2026-07-31 — M-V1.5 Phase B scaffolding: YOLO pipeline, torch stays out of the app

- Built the whole trained-detector path so a `card_yolo.onnx` can be dropped in
  and used, **without** adding torch to the app: `services/detector_dataset.py`
  (`export_yolo_dataset` — regions → YOLO images/labels/data.yaml, single `card`
  class), `training/train_detector.py` (offline, dev-only; guarded `ultralytics`
  import; export dataset → yolov8n `single_cls` → ONNX → copy to
  `models/card_yolo.onnx`), and `OnnxCardDetector` + `yolo_model_path()` for
  CPU inference (bundled via the existing `models/*.onnx` glob + LFS, like
  DINOv2).
- **Design:** YOLO postprocessing is factored into pure functions (`_decode_yolo`
  handles `[1,5,N]`/`[1,N,5]`, undoes letterbox, normalises; `_nms` greedy IoU)
  so it's unit-tested without a model. The window tracks proposals **per source**
  (CV teal / YOLO mauve); the **YOLO boxes** toggle enables only when a model is
  present and a bad model degrades to zero proposals rather than breaking.
- **Why now, train later:** the pipeline + wiring are the reusable work; the
  actual train needs a labeled corpus (being built in the window) and one manual
  `pip install ultralytics` in a training env. torch (~2 GB) never enters the
  shipped app.

### 2026-07-31 — M-V1.5 Phase A: classic-CV box proposer (detector, no training)

- Built the **immediately-available** detector half first (the ratified design
  says auto-boxing is not gated on training): `services/detector.py`
  `propose_card_boxes(image)` returns normalised `(x,y,w,h)` boxes via a
  **projection-profile** segmentation — background level from the image border,
  foreground = deviation past an Otsu threshold (with a noise floor), split into
  columns then rows, each cell tightened to its bbox. Pure numpy/Pillow (no new
  deps, no OpenCV), deterministic, Qt-free.
- **Why projection-profile over OpenCV contours:** the primary use case is cards
  laid out with gaps on a plain mat, which this segments well with zero new
  dependency footprint. Touching/overlapping cards merge — accepted, since the
  trained YOLO proposer (Phase B) is meant to cover clutter. Revisit OpenCV only
  if the CV proposer proves inadequate.
- **Window:** the **Classic-CV boxes** toggle overlays proposals (dashed,
  selectable); **Accept proposals** promotes the selected (or all) into numbered
  boxes. Proposals never persist as regions unless accepted. **YOLO boxes** stays
  disabled pending Phase B.
- **Phase B (pending owner go-ahead):** train single-class YOLO-nano from the
  drawn boxes. Needs a labeled corpus (now being built in the window) and a
  **dev-only** `ultralytics`/`torch` training path (~2 GB install; the exported
  `.onnx` ships via LFS + PyInstaller like DINOv2). Deferred so torch isn't
  pulled until the corpus and the decision are in place.

### 2026-07-29 — Auto eBay links via a derived sold-listings search

- Every card now has an eBay link with no data entry: `services/ebay.py`
  `sold_search_url(item)` builds an eBay search from the card identity —
  `Name Set # Condition/Grade` — with the Sold Items filter
  (`https://www.ebay.com/sch/i.html?_nkw=…&_sacat=0&LH_Sold=1`). Dropped the
  session/tracking params from the sample URL.
- **Derived, not stored** (chosen over backfilling `item.ebay_url`): the link
  always tracks the current identity, needs no migration, and can't go stale.
  A manually-entered `item.ebay_url` still wins at the call site.
- The Inventory links panel's eBay button uses `ebay_url or sold_search_url()`
  (so it's always enabled); the item editor shows the derived URL as the eBay
  field's placeholder. Graded cards use "PSA 10"-style grade in the query.
- **Auction editor (2026-07-30):** each card row gained an **eBay button** beside
  the TCGplayer button, opening the derived sold-search from the row's fields.
  Refactored `ebay.py` to expose a parts-based `search_url(name, set, number,
  cond)` that `sold_search_url(item)` now wraps. A **Condition dropdown** was
  then added to the auction row (persisted on `AuctionCard.condition`), feeding
  both the eBay search and the exported Transaction incoming card.

### 2026-07-28 — Modern promo autofill gap: leave as-is (don't merge sources)

- The **EN autofill catalog is built from pokemontcg.io only**; pricing uses
  tcgcsv. Some newest promo sets (e.g. **"ME: Mega Evolution Promo"** — Tyrunt
  #070) exist on tcgcsv but **not yet in pokemontcg.io**, so they price correctly
  but don't appear in autofill.
- Considered supplementing the EN catalog from tcgcsv, but the two sources name
  sets differently (tcgcsv "SM - Unified Minds" vs pokemontcg.io "Unified
  Minds"; tcgcsv "…Promo Cards" vs pokemontcg.io "…Black Star Promos"), so a
  merge risks duplicate sets in autofill. **Owner chose to leave as-is** — wait
  for pokemontcg.io to catalogue them.
- **Workaround for such a card:** enter it manually and paste its TCGplayer
  product URL into the item's TCGplayer link; the price refresh then resolves it
  from tcgcsv by product id.

### 2026-07-28 — Pricing: subset-by-name resolution + alt-art number fallback

Two more autofill price/link gaps for special printings:
- **Subset sets sharing the parent's code.** "Hidden Fates Shiny Vault"
  (`sma`, code "HIF") is a separate tcgcsv group ("Hidden Fates: Shiny Vault")
  but shares code "HIF" with the main set, so the code-first exact match resolved
  the wrong group and SV-numbered cards found no product. Fix: `_resolve_group`
  now tries the **set_name exact match before set_code** (name is the more
  specific key; the parent's code collides). Also covers Generations/RC.
- **"a"-suffixed alt-art reprints.** Unified Minds Jirachi-GX is "79a" in the
  catalog but tcgcsv lists only "79", so the exact number match failed. Fix:
  `price_for_card` falls back to the **base number** (`_number_base`: "79a" →
  "79", prefixes like "sv79" untouched) when no exact-number product matches —
  still name-verified. Exact matches still win.
- Verified live: Silvally SV79 → Shiny Vault product; Jirachi 79a → the base
  #79 product.

### 2026-07-28 — Auction autofill price: prefer live over cached history

- **Bug:** Meowth Wizards Black Star Promos autofilled $5.52 (should be ~$60).
  The auction price-on-pick was **history-first** and only fell back to the live
  price when history was absent. `pricehistory.db` had a stale/wrong value
  (basep-10 = 552¢) recorded by an earlier catalog sweep that mis-resolved the
  "PR" promo collision. The Transaction window (live-only) already showed the
  correct $62.31.
- **Fix:** the auction now treats the cached history value as an **instant
  provisional**, then the live tcgcsv `price_for_card` (name-checked, set_code
  aware) **corrects the price to the authoritative product value** — unless the
  user hand-edited it (`market_is_manual`). Brings the auction window in line
  with the Transaction window and is robust to a stale/mis-resolved cache.
- **Still needed:** the `pricehistory.db` cache holds wrong values for promo/
  older cards from pre-fix sweeps; it self-heals as the sweep re-runs (the
  resolution is fixed, dedup-on-change rewrites the value). Run File ▸ Update
  market prices to refresh sooner — this also fixes the Price Lookup graph/trend
  for those cards.

### 2026-07-28 — Autofill pricing: use set_code + verify product name

- **Bug:** older cards with generic set names got wrong TCGplayer links. The
  autofill price-on-pick only passed **set_name** to `price_for_card`, and
  "Base" fuzzy-substring-matched a modern group ("Scarlet & Violet Base Set"),
  whose #10 is Vivillon — so Mewtwo Base 10/102 resolved to a Vivillon URL.
- **Fix 1 — thread `set_code` through** the autofill workers into
  `price_for_card` → `_resolve_group`. An exact code match (Base "BS" =
  tcgcsv group 604) beats the fuzzy name match. (The RC subset branch still
  wins first, so Radiant Collection is unaffected.)
- **Fix 2 — verify the product name** (safety net). `price_for_card` now matches
  a product on **number AND name** (normalized containment); if a card name is
  given and the number-matched product's name disagrees, it returns **None**
  (better no link than a wrong one). `_products_for_group`/`products()` now carry
  the product name (4-tuple; `marketprices.sweep` updated). Verified live: Base
  Mewtwo #10 + code "BS" → correct URL; without the code the wrong Vivillon match
  is now rejected.
- **Follow-up — promo alias must win over set_code.** Passing `set_code` then
  regressed promo sets: "Wizards Black Star Promos" has the generic code "PR",
  shared by ~20 tcgcsv groups, so an exact "PR" match hit the wrong group. Moved
  the curated `_PROMO_SET_ALIASES` (by set-name) **ahead of** the code/name exact
  match in `_resolve_group`. Verified live: Meowth Wizards Black Star Promos #10
  → `wotc-promo-meowth`.

### 2026-07-28 — Auctions: Export to Transaction, Archive; minimal row (revised)

- **Row stays minimal.** Briefly tried full Transaction-field parity on the
  auction card row, but reverted (per owner) to the original minimal columns —
  Name / Set / # / Market / Qty / TCGplayer(button). The full card details are
  entered in the Transaction window after export. (`auction_card`/`AuctionCard`
  keep the extra parity columns from the experiment — harmless, forward-only —
  they just default and aren't edited by the minimal row.)
- **Export to Transaction:** a button in the editor footer and per watchlist row
  emits `(title, [IncomingCard])`; the main window clears the Transaction form,
  adds the incoming rows, sets counterparty "eBay" + reference = auction title,
  and switches to the Transaction tab. **Cash out is left blank** — the auction
  stores card market values, not the hammer price, so the user types the actual
  winning bid.
- **Manual market edits carry through.** The row tracks whether Market was
  hand-edited away from the auto-fetched price (`market_is_manual`, persisted).
  On export such a card is **noted** (a "Market manually set to $X" note),
  **marked for review**, and its value **overwrites** the item's current market
  price on post. To make the overwrite land even on an already-owned item,
  `_match_or_create_item` now overwrites when the acquisition *deliberately*
  carries a reviewed value (`inc.review`) — while a non-review re-acquisition
  still never clobbers a user-maintained reviewed value.
- **Archive:** `auction.archived` flag; the watchlist shows active auctions with
  a "Show archived" toggle, and each row has Archive/Unarchive. Archived
  auctions persist and can still be exported.

### 2026-07-27 — Dashboard collection-value chart reconciles with the tile

- The "Collection Value" **tile** uses current `item.market_value_cents` (incl.
  reviewed/manual/graded), while the **chart line** reconstructed value from raw
  poll `price_snapshot`s only — so the line excluded never-polled/graded/manual
  cards and used NM polls for reviewed cards, ending well below the tile
  (observed ~$1.25k gap). Confusing: two "Collection Value" figures disagreeing.
- **Fix 1 (reconcile):** `collection_value_series` now values each held lot at
  each date using the recorded poll for polled cards and the card's **current**
  value (reviewed/manual) for reviewed/never-polled cards; ripping/missing = $0;
  samples at acquisition + poll dates; and **appends a final point = the tile
  figure** (`collection_value_cents`) so the endpoint always equals the tile.
  Historical values for manual/reviewed cards are back-projected flat (honest:
  we lack their past prices).
- **Fix 2 (accurate going forward):** new `collection_value_snapshot` table
  (migration 0015) records the daily effective tile value per intent
  (`record_collection_value`, deduped per day), written on startup, after a
  price refresh (GUI + headless). The series prefers recorded points and
  back-fills older history with the reconstruction.
- Also clarified for the user: **net cash falls when buying** (purchases are
  cash out) — the money-movement line trending down is correct, not a bug.

### 2026-07-27 — Stolen / missing items: reversible $0 impairment (not a transaction)

- Right-click an Inventory row → **mark as stolen / missing** (and unmark). Backed
  by a reversible per-lot flag `lot.missing` (migration `0014`), **not** a
  transaction — the user wanted a lightweight toggle.
- **Accounting = impairment, mirroring ripping** (chosen over a realized
  write-off, confirmed by owner): a missing lot is valued at **$0 market** while
  **keeping its cost basis**, so unrealized P/L = `0 − cost` → it shows a loss
  equal to its purchase price, and portfolio collection value / unrealized drop
  accordingly. The realized-profit line is untouched. Picked for consistency with
  the existing $0-lot pattern and a small, contained code footprint; a stricter
  realized write-off (hitting the realized series, needing a loss date) is a
  possible later refinement.
- Missing lots are excluded from on-hand, the sell/give picker, and FIFO
  disposal (can't sell what's gone); the **Sold** column reads "Missing". Each
  toggle writes an append-only `audit_log` `edit` entry on the lot (money moved,
  no transaction). New invariant 9 in [spec 02](../specs/02-data-model.md#invariants-must-always-hold).

### 2026-07-24 — M-V1c: identity suggestions wired into the Recognition window

- A **Suggest labels** button crops each drawn box (in-memory, via the box's
  normalised rect against the full-res image) and ranks catalog matches with
  `Recognizer.suggest`. Each segment row gains a **Suggestion** dropdown; picking
  an entry copies that identity into the box + name field.
- **Candidate pool = the labeled corpus** (`labeled_card_ids()` over saved
  regions) **+ current-session labels** — open-vocabulary retrieval that grows as
  you annotate (active learning), not a fixed classifier. Empty pool → a notice
  that suggestions improve as you label; references cache in `vision.db`.
- Embedders now accept a `PIL.Image` as well as a path, so crops never spill to
  disk. **Known limitation:** frozen DINOv2 on photo-crop-vs-catalog-render is a
  weak baseline (advisory ⚠ under 0.6 cosine); the M-V2 fine-tune fixes accuracy
  with no further UI work. The recognizer is injectable for headless UI tests.

### 2026-07-23 — DINOv2 identifier provisioned: LFS-tracked, bundled, active by default

- The fp32 **DINOv2-small ONNX** encoder (~88 MB) is now committed to the repo
  as package data (`src/tcginventory/models/dinov2_small.onnx`) via **Git LFS**
  and **bundled into the PyInstaller exe** (spec `datas` + `recognizer`/
  `onnxruntime` hidden imports). Rationale: LFS delivers the blob to clones and
  the build; bundling makes the frozen app self-contained and offline. So the
  DINOv2 identifier is **active out of the box**, not gated on a manual download.
- **onnxruntime promoted to a core dependency** (the `[vision]` extra is gone);
  `model_path()` resolves the bundled package copy first (via
  `importlib.resources`, mirroring the app icon), then a user-dropped data-dir
  file, else `None` → PixelEmbedder fallback.
- Fixed `OnnxEmbedder` to use the **CLS token** (`last_hidden_state[0,0]`,
  384-dim) as the image embedding rather than flattening the full patch grid.
- Verified: 384-dim unit-norm embeddings, identical-image cosine ≈ 1; frozen
  exe (now larger) boots and the model + onnxruntime ship in `_internal/`.

### 2026-07-23 — M-V1a matcher engine + pluggable embedder (DINOv2 / fallback)

- `services/recognizer.py`: `Recognizer.suggest(image, candidates)` embeds a
  query and ranks candidates by cosine similarity against per-card **reference
  embeddings** cached in `vision.db.card_embedding` (keyed by card + embedder
  version; open-vocabulary, no retrain to grow the catalog). Headless,
  pool-agnostic; reference-image resolution is injectable.
- **Embedder is pluggable & version-tagged:** **DINOv2-small via onnxruntime**
  (optional `vision` extra + model at `<data_dir>/vision/models/dinov2_small.onnx`)
  or a dependency-free **PixelEmbedder** fallback (down-scaled grayscale) so the
  engine runs/tests before the ~90 MB model is provisioned — changing embedders
  re-embeds (cache key includes the version).
- Core deps added: **numpy, Pillow**; **onnxruntime** kept optional. The DINOv2
  weights are a provisioning artifact (not committed). Next: M-V1c wires
  per-box suggestions into the Recognition window.

### 2026-07-23 — Detection: classic-CV + YOLO-nano as toggleable box proposers

- Both detectors are first-class **box proposers**, shown as canvas overlays the
  user accepts/edits into regions. A **checkbox per method** toggles that
  method's boxes on/off (view either, compare both). Classic CV needs no
  training (usable day one, blank backgrounds); YOLO-nano is trained on the
  drawn boxes and improves clutter/overlap coverage. Same proposers feed Part 2
  inference. So auto-boxing is **not gated** on training — it improves with it.

### 2026-07-23 — Recognition models chosen: DINOv2-small identifier + YOLO-nano detector

- **Identifier:** frozen **DINOv2-small** (ONNX) embeddings + cosine NN retrieval
  (open-vocabulary) for M-V1 → fine-tuned (ArcFace/contrastive) on the corpus for
  M-V2. **Detector:** a trained **single-class "card" YOLO-nano** (ONNX/CPU) from
  the Recognition window's drawn boxes (classic CV only an optional annotation
  assist). **Not RL** — supervised metric-learning + active learning.
- **Consequence:** the learned detector needs annotated boxes first, so the
  manual-annotation Recognition window (Part 1) is built before Part 2's
  auto-detect. See [`specs/06-recognition.md`](../specs/06-recognition.md).

### 2026-07-23 — Recognition engine architecture ratified (spec 06)

- **Two-part system** (see [`specs/06-recognition.md`](../specs/06-recognition.md)):
  a **Recognition window** = human-in-the-loop **training baseline** (annotate
  scenes: draw numbered boxes, adjustable I-Give/They-Give zones or a single
  listing lot, label each via catalog autofill; saves localized **regions**);
  and **inference autofill** in Transaction/Auction (image → auto-detect +
  autofill with confidence + missed-detection flags), built once a model exists.
- **Data model:** `vision.db` gains a `region` table (box geometry + identity +
  side), superseding the image-level `capture_card`. The interim per-entry 📷
  photo buttons are **superseded** (removed when the window is built).
- **Model paradigm:** **supervised metric-learning + active learning**, not RL
  (the data is labeled boxes/identities, not a reward signal). Decompose into a
  **class-agnostic card detector** (classic CV now → small trained detector on
  drawn boxes) + an **embedding identifier** (open-vocab NN retrieval; frozen
  DINOv2-small to start → fine-tune on the corpus). Exact model TBD in the
  model discussion; fully local/offline (onnxruntime inference, offline-script
  training).

### 2026-07-22 — Vision: optional front/back photo pairing within a capture

- A photo can be tagged **front**/**back** and two photos **linked** as the two
  faces of the same cards; pairing is optional (not every photo has a partner).
- Stored on `image` as `side` ('front'|'back'|null) + `pair_group` (a small int
  shared by a linked front+back within the capture; additive migration on open).
  `services/vision.ingest_capture` now takes `(path, side, pair_group)` photo
  specs; the `ImageCaptureButton` dialog links a selected pair (topmost = front).

### 2026-07-22 — Vision M-V0 flow: capture-grouped scene photos (supersedes same-day entry)

- **Reframe:** a supplied photo is a **scene of one or more cards**, and a single
  transaction/auction typically has **several photos** documenting the *same*
  purchase — not one photo per card. So photos attach to the **whole entry**
  (one `📷 Photos` button in the Transaction/Auction footer), never per row.
- **Model — the `capture`:** on commit, all the entry's photos + all its cards
  are recorded as one **capture** in `vision.db`
  (`capture` → many `image`, many `capture_card`). This keeps a lot's photos
  grouped and documents *which cards the capture shows* without falsely claiming
  every card appears in every photo (image-level multi-label at the capture
  granularity; per-photo bounding boxes are a later enhancement). Replaces the
  earlier per-image `image_label` schema; `services/vision.ingest_capture`
  replaces `ingest`. `ImageLabel` → `CardLabel`.
- **Labels:** transaction captures document the **received** cards (the "photograph
  what you acquired" flow; a pure sale ingests nothing); auction captures
  document all the auction's cards. Files stay content-addressed on disk; the
  experimental pre-capture schema is dropped-and-recreated on open (throwaway).
- **Verified:** ruff clean; vision tests cover capture grouping, multiple photos
  per entry, on-disk dedup, and ingest-on-commit for both screens; full suite green.

### 2026-07-22 — Vision engine, M-V0: image ingestion + labeling corpus

- **Goal (multi-phase):** a reactive engine that learns from listings/sales to
  determine a card's **identity** and **condition** from an image, plus
  **monetary significance** (liquidity, average markup, volatility). These are
  three different problems: identity (image matching — has a no-training
  baseline vs the catalog reference images), condition (hard, data-hungry), and
  monetary (pure analytics on data we already have — parallelizable).
- **M-V0 (this increment):** let the user attach card **photos** on the
  Transaction (receive) and Auction (card) rows; on commit each photo + the
  identity/condition the user assigned = one labeled training example. No ML
  yet — this builds the corpus everything else learns from.
- **Decisions (ratified with owner):**
  - **Storage:** a separate **`vision.db`** beside the ledger (`data/vision.py`)
    for image + label metadata, with image files **content-addressed** on disk
    at `<data_dir>/vision/images/<sha256><ext>` (identical photos dedup to one
    file/row). Keeps bulky ML data out of the ledger's exports/keep-10 backup —
    mirrors `pricehistory.db` / `auctions.db`.
  - **Compute:** **fully local / offline** (no cloud vision APIs, no keys),
    consistent with the app. Model options to investigate for M-V1+ (all
    local): perceptual hashing → pretrained embedding + NN via `onnxruntime` →
    fine-tuned on the collected corpus.
  - **Ingest on commit:** photos are held in memory on the row
    (`ui/image_capture.py` `ImageCaptureButton`) and ingested (`services/vision.py`)
    when the transaction is submitted / auction saved, labelled from the row's
    final identity. Never fails the ledger op; auction rows clear after ingest
    so repeated saves don't duplicate labels. Qt-free data/service layers.
- **Milestones ahead:** M-V1 baseline identity matcher (no training, vs catalog
  images) → M-V2 learned identity (train on corpus) → M-V3 condition → M-Market
  liquidity/markup analytics (buildable now, independent of vision).
- **M-V0 limitation:** re-opening a saved transaction/auction doesn't re-load
  previously-attached photos (ingest is one-way into the corpus); a corpus
  browser/manager is a later increment.

### 2026-07-16 — Auction editor: multi-card display reuses Price Lookup's gallery

- **Decision:** The auction editor's preview is a **`CardGallery`** (the reusable
  widget the Price Lookup window uses), showing **every card in the auction** as
  a wrapping thumbnail grid; hovering a card row focuses that single card and
  leaving reverts to the full gallery (the same `show_single` / `show_gallery`
  toggle). The gallery is rebuilt (`set_cards`) **only when the card set
  changes** — pick / remove / load — so thumbnails load once and persist, per the
  Price Lookup pattern. Entry table and gallery are a **50/50** horizontal split.
- **Why:** the earlier single-`CardImagePane` preview showed one card at a time;
  an auction is a bundle, so seeing all its cards at once is the point. Reusing
  `CardGallery` inherits the fix for rapid-rebuild thumbnail pile-up
  (synchronous `setParent(None)` before `deleteLater`).
- **Note:** only catalog-matched cards (with a `card_id` + image URL) get a
  thumbnail; manually-typed cards without a match simply don't appear in the
  gallery (they still count toward value / max bid).

### 2026-07-16 — eBay auction max-bid planner (new screen, ephemeral store)

- **Decision:** Add an **eBay Auctions** nav screen for planning max bids on
  auctions you're watching. Two stacked panes: a **watchlist** table (title,
  link, total card value, and a max-bid breakdown at **70/80/90/100%**, plus
  shipping/tax and delete) over a **transaction-adjacent editor** (title + URL,
  catalog-autofilled cards with auto-priced-but-editable market values, flat
  shipping, and an editable sales-tax rate defaulting to **6%**). Clicking a
  watchlist row loads it into the editor; Save upserts.
- **Max-bid math** (`services/auctions.py`, pure/integer-cents, Qt-free): the
  percentage is an *all-in* ceiling — bid + shipping + tax ≤ `p%` of the cards'
  value. Tax is charged on **bid + shipping** (standard eBay base), so
  `max_bid(p) = p·V/(1+rate) − shipping`, clamped ≥ 0. Tax rate stored as
  integer **basis points** (600 = 6.00%) to preserve the integer-money rule.
- **Storage:** a **separate, ephemeral `auctions.db`** beside the main DB
  (`data/auctions.py`, mirroring the `pricehistory.db` precedent). Kept out of
  the ledger entirely — no audit/soft-void, and never in the ledger's CSV
  export / `import_all` / keep-10 backup. Persists across sessions; **manual
  delete only** (nothing prunes it). `auction` + child `auction_card` (cascade).
- **Reuse:** the Transaction window's inline catalog-autofill row pattern,
  `CardImagePane` hover preview, `CardCatalogRepository.search`, and
  price-on-pick — but sourced **history-first** (`pricehistory.current_price`,
  instant/offline) with a live tcgcsv fetch only as a fallback for un-swept
  cards. Verified: ruff clean, 13 new tests (math + repo + offscreen view),
  full suite green, and an end-to-end MainWindow smoke.
- **Not stored as inventory:** these are scratch bid estimates, *not* lots — no
  linkage to holdings. Winning an auction is still entered as a normal purchase
  in the Transaction window.

### 2026-07-13 — Transaction value derived from the deal (drop "agreed value")

- **Decision:** Remove the per-outgoing-card manual "agreed value". A card's
  value is now derived from the whole transaction:
  - **Cards received** get cost basis from `cash_out + Σ(given cards' current
    market value) − cash_in` (allocated by market weight, unchanged mechanism).
  - **Cards given** realize P&L against `cash_in + Σ(received cards' market
    value) − cash_out`, split across them by market weight;
    `OUT line.unit_price_cents` = per-unit proceeds (what reporting reads).
- **Why:** the owner wanted the give side to carry no manual value — "the price
  is determined by all the cards involved." Given cards are valued at their
  current market value; received cards at their entered market value.
- **Behaviour:** pure sale → proceeds = the cash in (unchanged P&L); pure
  purchase → unchanged; **trades change** — a received card's basis now uses
  the given card's *market* value (not an arbitrary agreed number), e.g. give a
  $20-market card + get $10 back → received card basis $10 (was whatever agreed
  value implied). Helpers: `_deal_values`, `_allocate_amount` (money math,
  tested). `OutgoingCard.agreed_value_cents` removed.
- **UI:** give-side "Agreed Value" column gone; add/remove are now a `＋` and a
  per-row `✕`. (Full inline receive-entry with catalog autocomplete is next.)

---

### 2026-07-13 — Card-show attendance status (denied/pending/attending/walking)

- **Decision:** Each show can be marked with an attendance status —
  denied (red), pending (orange), attending (green), walking (blue) — via four
  colour buttons per show in the day list. Statused shows sort to the top of a
  day's cell and render with a coloured highlight bar; the day list mirrors the
  ordering.
- **Storage:** a separate `card_show_status(source_id, status)` table (migration
  0007) keyed by tcdb's show id, **not** a `card_show` FK — so it survives the
  wholesale delete+reinsert a state refresh does. Joined onto `CardShow.status`
  on read; absence = no status.

---

### 2026-07-13 — Card-shows calendar: data layer (scrape tcdb via curl)

- **Decision:** Add a shows data layer for the planned Dashboard calendar:
  migration `0006_card_show`, a `CardShow` model + `CardShowRepository`
  (upsert-by-tcdb-id, date-range/state queries, past pruning), and
  `services/cardshows.py` — a Qt-free parser + `CardShowService.refresh(states)`
  that replaces each state's stored shows with a fresh scrape.
- **Source:** tcdb `CardShows.cfm?MODE=Location&State=<code>&Display=<name>`.
  The list page alone yields date, name, venue, city, time range, and a detail
  link per show — no per-show fetch needed.
- **Fetch via `curl`, not httpx:** tcdb is behind Cloudflare, which 403s Python
  HTTP clients on TLS fingerprint (even with browser headers) but lets the
  system curl through. `_fetch_html` shells out to curl; the injected-fetcher
  design keeps parsing/refresh unit-testable offline and makes swapping to
  `curl_cffi` a one-function change if curl is later blocked.
- **Scope:** background refresh covers **only the user-toggled states**. UI
  (month calendar + per-cell show names, state multi-select, "TCG only" toggle)
  and the daily-task integration come next; this increment is source-only
  (the packaged exe isn't rebuilt until the UI lands).

---

### 2026-07-13 — Continuous background price updates (headless + Task Scheduler)

- **Decision:** Prices now refresh in the background even when the GUI is
  closed. A headless `--refresh-prices` entry point (`app.refresh_prices_headless`,
  no Qt/display) does one poll-and-record pass; a **Windows Scheduled Task**
  (`scheduling.py`, via `schtasks`) runs it **once daily**. The packaged app
  **self-registers the task on startup by default** (default ON) and exposes a
  File-menu toggle ("Automatic daily price updates") to turn it off; the choice
  persists in `QSettings` so a user who opts out isn't re-enrolled.
- **Context / why:** Refresh only happened on GUI launch (throttled) or manual
  click, so price history had gaps whenever the app wasn't opened. The owner
  wanted continuous history with no manual step.
- **Why daily:** tcgcsv mirrors TCGplayer roughly once a day, so one snapshot
  per day captures all real movement; more frequent polls would mostly dupe.
- **Scoping choices:** startup self-registration is gated to the **frozen** app
  so running from source / the test suite never shells out to `schtasks`; the
  task's command points at `sys.executable` (the exe) so it survives rebuilds
  in place. Windows-only (macOS/Linux would need launchd/cron — deferred).

---

### 2026-07-11 — Hard-delete a mistaken inventory entry (exception to soft-void)

- **Decision:** Add `TransactionService.delete_inventory_entry(lot_id)` — a
  true hard delete of a single lot, invoked from a "Delete entry…" button in
  the Inventory view. It removes the rows for good (not a void) and is recorded
  in `audit_log` as a new `AuditAction.DELETE`.
- **Context / why:** The architecture rule is "transactions are never
  hard-deleted." But the owner needs to erase *blatant/junk entries* entirely;
  a soft-void leaves them cluttering History. Real purchases here are
  multi-card, so deleting the whole transaction to remove one bad row would
  destroy good data — hence per-lot granularity.
- **No business guardrails (revised same day):** the first cut blocked deletes
  when the lot had been sold or came from a trade / cash-in transaction. That
  got in the way of cleanup, so it was removed — the owner is trusted to prune
  their own data. Delete now always succeeds; the only work is keeping the DB
  internally consistent: consumption records that drew *from* the lot are
  removed (the sale stays, minus its cost-basis link), a plain cash-out
  purchase has its cash reduced by the lot's basis, the acquiring line/txn are
  removed once empty, and orphaned items are dropped.
- **Consequences:** History no longer perfectly reproduces every entry, and a
  deleted lot that had sales against it leaves that sale's realized P/L as a
  historical figure with no live basis link. The append-only `audit_log` keeps
  a `before_json` record so the deletion itself stays traceable. For surgical,
  reconciled changes (rather than deletion), Edit remains the tool.

### 2026-07-11 — Closest-match set→group resolution for pricing

- **Decision:** When resolving an item's set to a tcgcsv group, try an exact
  normalized match first (on `set_code` then `set_name`, plus the name after any
  `SVxx:` prefix), then fall back to the **closest fuzzy match**
  (`difflib` ratio ≥ 0.6, with substring hits treated as strong). 
- **Context / why:** tcgcsv names sets inconsistently ("SV09: Journey Together")
  and users type them loosely ("Journey Together"), so exact matching silently
  dropped whole sets from pricing.
- **Why it's safe:** the price join is by **product ID** (from the item's
  TCGplayer URL), and a product belongs to exactly one group — so a wrong fuzzy
  match yields *no* price, never a wrong one. Fuzzy matching only ever helps.
- **Consequences:** far fewer unmapped items; the group lookup is just a
  performance narrowing over which price file to download.

---

### 2026-07-10 — "Mark for Review" (manual, poll-protected prices)

- New `item.review` flag (migration `0005`), set by a checkbox at card entry.
  For a reviewed item, auto-refresh **still records price_snapshots** (history/
  chart keep filling) but **does not overwrite its shown Market Price Current** —
  the value is user-maintained (for cards tcgcsv can't price: LP/MP, JP, graded,
  sealed).
- Inventory current source: reviewed items use their manual
  `item.market_value_cents`; others use the latest poll. Reviewed cells are
  amber-highlighted and the Market Price Current cell is **inline-editable**.
- Each reviewed cell has a **two-stage button**: click 1 arms it (previews the
  unmark — highlight off, label → "Refresh"); click 2 commits (unmark + adopt the
  latest polled price). It reverts to stage one if the selection moves or the
  page changes. Implemented as a `QStyledItemDelegate` (survives sort/filter/
  scroll, unlike per-cell index widgets).

---

### 2026-07-10 — Live market pricing via tcgcsv + price history

- **Source: tcgcsv.com**, a free JSON mirror of TCGplayer's pricing, joined by
  the TCGplayer **product ID** parsed from `item.tcgplayer_url` (set → tcgcsv
  group resolved from the item's set code/name). Chosen over scraping
  tcgplayer.com (JS-rendered, ToS, Cloudflare, heavy) and over pokemontcg.io.
  The `PriceProvider` abstraction lets another source slot in later.
  - **Limits:** product-level market price only (≈ NM English); no per-condition
    (LP/MP) and no Japanese; ripping stays $0.
- **History stored** in `price_snapshot` (migration `0004`) — one row per item
  per refresh. Drives the live "Market Price Current"/"Unrealized P/L" columns
  and the Inventory price-history chart, and will unlock the deferred
  collection-value-over-time dashboard series.
- **Refresh: both** — a throttled auto-refresh on startup (skipped if the newest
  snapshot is <12h old) plus a manual **File ▸ Refresh prices**. Runs on a
  background `QThread` with its own DB connection so the UI never blocks;
  network/parse failures are captured, never fatal.
- **Inventory price plot:** selecting a card shows its market price over the last
  90 days with flat **Cost** and **Market at ToP** reference lines.
- Dependency added: `httpx` (plain JSON; no headless browser).

---

### 2026-07-10 — Per-copy SKUs on the lot (auto-generated)

- **SKUs moved from the item to the lot.** A SKU identifies an acquired *copy*,
  formatted `{NAME3}-{SET}-{NUMBER}-{COND|GRADE}-{LANG}-{NNNN}` (e.g.
  `CHA-CPA-074-NM-EN-0001`). This matches the user's spec: each new card gets a
  new SKU, and `NNNN` sequences per identity (NM separate from LP). Because our
  model merges identical copies into one *item* (with multiple lots), the SKU
  had to live on the **lot** for the sequence to mean "how many copies exist."
- **The old manual `item.sku` was retired** (migration `0003_lot_sku.sql`:
  `lot.sku` + partial unique index; `item.sku` dropped). Existing lots are
  backfilled with SKUs on startup (`services/sku.backfill_skus`).
- **Sequence** = distinct existing SKUs for the identity + 1, bumped on the rare
  cross-identity collision (from name truncation) to stay globally unique.
- **Lookup**: `LotRepository.find_by_sku`; the give dialog resolves a typed SKU
  to its card, and the Inventory search matches SKUs. Set/number/grade segments
  fall back to `NA`/`GR` when absent (e.g. a sealed box → `BRI-NA-NA-SEALED-EN-…`).
- Work is on `main` directly (no feature branch), per the new workflow.

---

### 2026-07-10 — M6 implementation: packaging & polish

- **PyInstaller onedir** is the default build (`packaging/tcginventory.spec`);
  faster/more debuggable than onefile, still a zip-and-share distributable. A
  `ONEFILE` flag flips to a single exe. The spec bundles the SQL migrations and
  icon as data and pins `PySide6.QtCharts` as a hidden import.
- **App icon** generated programmatically (indigo→teal "TCG"), committed as
  `icon.png` + `icon.ico`; loaded via `importlib.resources` so it works framed
  and frozen.
- Polish: empty-state notes, **Ctrl+S** to submit, accessibility tooltips/names,
  finalized user guide. Verified the frozen exe boots on its own.

---

### 2026-07-10 — M5 implementation: import/export & backup

- **Two export families.** Human-readable flat CSVs (items/inventory/
  transactions, money in dollars) for spreadsheets, and a **full dump/restore**
  (`export_all`/`import_all`) that writes every table verbatim so the dataset
  round-trips exactly (the exit criterion). Empty CSV cells load as SQL NULL
  (repos never store empty strings, so this is faithful); integer columns rely on
  SQLite affinity to reconvert.
- **Portable backup** = the full CSV dump **and** a one-file DB copy via SQLite's
  online-backup API (`backup_database`). We still never hard-delete, so backup is
  copy-only.
- **Import column mapping is header-based** (case-insensitive aliases) with a
  validation preview, rather than an interactive column-mapper UI — pragmatic for
  V1; the preview surfaces per-row errors before any write.
- **Startup auto-backup** (F-DA-6) writes a timestamped copy to
  `<data_dir>/backups/`, keeps the newest 10, and never blocks launch on failure.

---

### 2026-07-10 — M4 implementation: dialog-based card entry

- The unified Transaction window adds cards via **modal add-card dialogs**
  (give / receive) that append to summary tables, rather than fully inline
  editable table rows. Same ratified behavior and validation (market value
  required, condition greys out when graded, oversell blocks submit, ripping
  excluded from the give picker) — the dialog form just handles the conditional
  grade fields far more robustly than in-cell editors. Inline row editing can be
  a later refinement.
- **Edit** pre-loads the transaction and reconstructs the cash split single-
  sided from the stored net `cash_delta` (only the net is stored); this is exact
  because allocation and cash both depend only on cards-out and net cash.
- Live type-inference badge and the summary/margin footer are driven by
  `TransactionService.preview()` (headless, tested).

---

### 2026-07-10 — M2 implementation decisions (services & money math)

Three judgment calls made while building the service layer (all in code + tests):

- **Indivisible qty>1 allocation splits a line into two lots.** When a line's
  allocated total doesn't divide evenly by its quantity, it yields two lots —
  `qty − r` units at the base unit cost and `r` at base+1 — so
  `Σ(unit_cost × qty)` still equals the total exactly (invariant 6). Quantity-1
  lines (the common case) always yield a single lot. Keeps unit costs integer
  without rounding drift.
- **Negative `net_consideration_in` clamps to $0.** If you're paid more cash than
  the value you gave up (rare), incoming cards get a $0 basis rather than a
  negative one. The cash is still captured in `cash_delta`. Revisit if real usage
  wants negative basis.
- **Historical collection-value series deferred to M3.** The two exact,
  event-based dashboard series (money-movement, realized-profit) are implemented
  now; a true market-value-of-holdings-over-time series needs a market-value
  history the schema doesn't record. `collection_value_cents` gives the current
  point-in-time figure. To be built with the chart in M3.

---

### 2026-07-10 — Margin surfacing + trade summary; two market values clarified

- **Clarified** that the schema holds **two** market values doing different jobs:
  `lot.acquired_market_value_cents` (frozen market-at-purchase, used only to set
  cost basis; preserves e.g. $10 vs $15 per lot) and `item.market_value_cents` /
  `market_value_asof` (current estimate, used only for unrealized valuation on
  cards still held). Neither is used for margin.
- **Margin** = realized P/L on cards given up (`proceeds − cost basis`), already
  stored per `out` line in `realized_pl_cents` — **no new column**. It is
  measured against cost basis (what you paid), not market value. Displayed as
  **profit $ + gross-margin %** (`profit ÷ proceeds`); the % is derived, not
  stored.
- **Trade summary overview** added to the Transaction window (Spec 04, F-TX-5): a
  live panel showing value given/received, net cash, cost basis drawn (FIFO), and
  margin, repeated on the success screen.
- Spec/docs only (surfacing, not schema). Propagated to specs 02/03/04 and the
  glossary.

---

### 2026-07-10 — Add Card # and Language to item identity

- **Language** is a new per-item field (enum: EN | JP | CN | KO | FR | DE | IT |
  ES | PT | TH | ID | RU; default EN) and joins the **item identity key** — the
  same card in a different language is a distinct item. Added to the DB via
  migration `0002_add_language.sql` (additive `ALTER TABLE`, forward-only).
- **Card #** already existed on `item` and in the identity key; this change
  surfaces it (and Language) as explicit **Transaction-window row fields** and an
  Inventory column. Both are filterable/sortable under F-VW-9.
- Done at M1 (data layer). Propagated to specs 02/03/04, the enums/models/repos,
  the glossary, and tests.

---

### 2026-07-10 — Spec 05 ratified; baseline complete

- Roadmap reworked to the ratified model (unified posting algorithm, intents incl.
  ripping, void/edit/audit, dashboard charts via QtCharts, full History, CSV
  import+export). Post-V1 backlog now includes an explicit Rip/Open event, the
  shows/auctions calendar, and intent-change history.
- **All six spec sections (00–05) are ratified.** Baseline is complete; next step
  is implementation at **M0**.

### 2026-07-10 — Added "Ripping" intent

- Intent enum extended to **personal | resale | ripping**. Ripping = sealed
  product bought to be opened, never sold.
- **Assumed opened no matter what**, so the loss is certain: a ripping lot is
  valued at **$0** and shows in inventory as an **unrealized loss = cost basis**.
  It stays visible in inventory, is **excluded from the sellable picker**, and
  never touches the realized-P/L line.
- Cards pulled from opening are entered **separately** as an ordinary acquisition
  in the Transaction window — **no linkage** to the ripped lot.
- Propagated across Specs 00/02/03/04 (F-TX-4, F-VW-10, invariant 8) + glossary
  and user guide.

### 2026-07-10 — Spec 04 ratified (interaction layer)

- **Live type-inference badge** on the Transaction window (PURCHASE/SALE/TRADE),
  plus a live totals/estimated-realized-P/L footer.
- **Validation defaults:** market value required on every incoming row (submit
  disabled otherwise); oversell blocked inline with a live remaining indicator;
  errors inline, never post-hoc modals.
- **Success screen** shows inferred per-card cost + realized P/L, then returns to
  a fresh Transaction with a toast linking to History.
- **Void asks for confirmation; Edit warns when realized P/L changes.**
- Keyboard-first entry (Enter adds a row, Ctrl+S submits); accessibility — color
  is never the only signal.

### 2026-07-10 — Spec 03 ratified

- **Items section reframed** around auto match-or-create (no manual "add item"
  step); TCGplayer/eBay links are item fields; duplicate-as-template dropped.
- **Transaction log / History is a full V1 feature (MUST)** — not deferrable —
  since it's the surface for voiding/editing misentries. Added as a nav view in
  Spec 04.
- **CSV import and export both included in V1** (export MUST; item import MUST;
  transaction import SHOULD). User is not necessarily migrating a spreadsheet but
  wants the capability.

---

### 2026-07-10 — Unified transaction window, profit at disposal, audit trail

- **Profit is recorded at disposal, never at purchase.** A purchase records cash
  out + cost basis; realized profit is the difference captured when cards leave
  (sale/trade). Dashboard series: money-movement = cumulative net cash;
  realized-profit = cumulative realized P/L; collection-value = market value
  over time.
- **Correction/audit.** Transactions are never hard-deleted. **Void** reverses
  all effects atomically (soft `status='voided'`, stays in history); **Edit** =
  void + re-post atomically. An append-only `audit_log` records every
  create/edit/void with before/after snapshots.
- **Keep item + lot; each acquired card is its own lot row.** Rejected a fully
  flat one-row-per-card table. The item holds shared identity + one current
  market value; the lot holds per-acquisition cost — so the same card bought at
  $90 and $75 is two lots (distinct basis), one item.
- **Merged Windows 2 & 3 into one bidirectional Transaction window.** A purchase
  is "cash out → cards in," a sale is "cards out → cash in," a trade is both.
  Type is **inferred** from which sides carry cards. Incoming cost basis =
  **consideration given** (cash out + agreed value of cards out) split across
  incoming cards by market value — the purchase rule is the cash-only corner
  case. App is now **three windows**: Dashboard, Transaction, Inventory.
- **Window 4 (Inventory): every field filterable and sortable** (incl. sold
  status, intent, value ranges).

---

### 2026-07-10 — Dual-purpose ledger, per-lot intent, market-value cost allocation

- **Dual purpose.** The platform serves personal collecting *and* business/resale
  simultaneously. Every card brought in is recorded regardless of resale intent.
- **Intent is per card / per lot** (not per acquisition, not per item). Each row
  in an acquisition is tagged **Personal** or **Resale**, so one lot purchase can
  mix keepers and resale cards, and the same item can be held under both intents.
- **Cost basis is inferred by market-value weighting, not entered.** The user
  enters a **total purchase price** for the acquisition and a **market value**
  per card; each card's cost basis =
  `total_price × (market_value / Σ market_values)`. This replaces the earlier
  "enter unit price + pro-rata shipping" approach.
- **Edge cases (resolved 2026-07-10):**
  - Market value is **required** on every row (submit blocked otherwise) — no
    missing-value fallback needed.
  - Rounding: allocated costs sum **exactly** to the total; residual cent(s) go
    to the highest-value card.
  - Intent change is a **plain editable toggle** (no dated history in V1).
  - **Condition greys out when a card is graded** (grade identifies the card).
  - Items **auto match-or-create** by identity key (name + set + condition +
    grade + grading company/number); no separate catalog step.

---

### 2026-07-10 — Baseline stack & scope

- **GUI framework: PySide6 (Qt 6).** Best-in-class table/model-view for an
  inventory grid; native look; LGPL is fine for personal use. Alternatives
  (Tkinter, Flet) rejected for weaker tables / less conventional CRUD.
- **Persistence: SQLite.** Zero-config, transactional, single portable file the
  owner can back up. Money stored as **integer cents** to avoid float errors.
- **V1 scope: core inventory + transactions.** Purchases, sales, trades on
  single-card or lot basis; cost basis & P/L; search/filter; CSV import/export.
  **No external price APIs in V1.**
- **Domain focus: Pokémon**, with a schema generic enough to add other games
  later.
- **Cost-basis method: FIFO** for V1; `lot_consumption` table keeps
  specific-lot / LIFO / average open for later without schema changes.
- **Layering:** domain + services are Qt-free and unit-tested headlessly; UI is
  a thin layer that never does SQL or money math.

---

_Template for new entries:_

```
### YYYY-MM-DD — <short title>
- **Decision:** …
- **Context / why:** …
- **Alternatives considered:** …
- **Consequences:** …
```

### 2026-07-14 — Card images: lazy on-disk cache, not a bulk pre-download
- **Decision:** Store each catalog card's `images.small` URL (from the
  pokemontcg.io dataset, migration 0009 + `card_catalog.image_small`) and fetch
  the PNG on demand the first time a card is hovered, caching it under
  `<data_dir>/card_images/<card_id>.png`. A single shared frameless preview
  popup (`ui/card_preview.py`) shows it while hovering autofill matches, the
  inventory grid, and transaction rows.
- **Context / why:** Measured `images.small` at ~165–170 KB each; caching all
  ~20k cards would be ~3.3 GB on disk + a 20k-request download. You only ever
  touch a small subset, so lazy caching gives the same "hover shows a local
  image" result for tens of MB.
- **Alternatives considered:** Bulk pre-download (rejected: 3.3 GB, hammers the
  free CDN); storing image bytes as SQLite BLOBs (rejected: bloats the DB and
  slows every query — files on disk keyed by card_id instead).
- **Consequences:** First hover of a new card needs network (fetched off the UI
  thread; silently skipped when offline); afterwards it's instant from disk.
  Inventory/given cards resolve their image by identity via
  `CardCatalogRepository.image_for`.

### 2026-07-14 — Japanese cards, Phase 1: catalog + images via TCGdex
- **Decision:** Add Japanese-card support in phases. Phase 1 pulls a Japanese
  catalog from **TCGdex** (`api.tcgdex.net/v2/ja`) into `card_catalog` alongside
  the English (pokemontcg.io) rows, tagged by a new `language` column (migration
  0012). Images use TCGdex's asset CDN (`{card.image}/low.png`); collector number
  = `localId`; printed total = `cardCount.official`; set code = TCGdex set id.
- **Context / why:** pokemontcg.io is English-only. TCGdex is the multilingual
  equivalent — free, no key, per-set JSON — so it mirrors our English pull.
  Catalogs coexist via `replace_for_language`, so re-pulling one language doesn't
  wipe the other. File menu now has separate English/Japanese update actions.
- **Alternatives considered:** JustTCG/paid APIs (rejected: not the catalog need,
  and condition pricing was a separate, rate-limited dead end); TCGdex's built-in
  `pricing` field (Cardmarket EUR, per-card — deferred to tcgcsv cat 85 in USD).
- **Consequences:** ~6.2k JP cards load today; ~53% have TCGdex images (older JP
  sets lack assets). Japanese names are Japanese script, so entry is by JP name
  and/or set-code+number (search now also matches `set_code`). Phase 2 = JP
  pricing via tcgcsv category 85 (link by set code → number). Phase 3 = romaji /
  cross-language search.

### 2026-07-14 — Japanese catalog source: tcgcsv cat 85, not TCGdex
- **Decision:** Source the Japanese catalog + images from **tcgcsv "Pokemon
  Japan" (category 85)** instead of TCGdex. Products with a collector Number
  become catalog cards (sealed boxes/packs skipped); `image_small` = the
  TCGplayer-CDN `imageUrl`; set_code = group abbreviation; card_id = `jp-<pid>`.
- **Context / why:** TCGdex JP was thin (~6.2k cards, ~53% with images) and used
  Japanese-script names. tcgcsv cat 85 has 448 sets → **~27k cards (more than
  English), 100% with images**, **English/romaji names** (searchable without a
  Japanese IME), full collector numbers, and it's the *same* source we'll use for
  JP pricing — so catalog + images + prices come from one bulk free feed.
  (Evaluated artofpkm.com per request: it's a Rails art gallery — no API, signed
  ephemeral ActiveStorage images, ~70 sets — not usable as a data source.)
- **Consequences:** Names are English/romaji, not Japanese script (fine for a
  US inventory app; Japanese names could be layered in from TCGdex later).
  Phase 2 (JP pricing) now just parameterizes TcgcsvProvider to category 85.

### 2026-07-14 — Japanese cards Phase 2: JP pricing via tcgcsv category 85
- **Decision:** Make `TcgcsvProvider` category-aware so it prices both English
  (category 3) and Japanese (category 85) cards. Caches are keyed by category;
  `_category_for(item)` selects by `item.language`; `_resolve_group` and
  `price_for_card(..., category=)` take the category. The transaction
  price-on-pick passes 85 for Japanese picks.
- **Context / why:** The JP catalog was pulled from tcgcsv cat 85, so a card's
  stored set name matches the tcgcsv group name exactly — group resolution is
  trivial, no set-code bridge needed. One provider, one code path, both markets.
- **Consequences:** JP items priced-on-pick get a TCGplayer JP product URL, so
  the daily refresh joins them by product id like English. Prices are USD market
  (≈ NM). Verified live.

### 2026-07-16 — Catalog-wide price history (market outlook)
- **Decision:** Record a daily market-price series for every catalog card in a
  **separate SQLite file** (`pricehistory.db`, beside the main DB), populated by
  a per-set sweep of tcgcsv (EN cat 3 via products number-join, JP cat 85 via the
  productId embedded in `jp-<id>`). **Dedup-on-change** (append a snapshot only
  when the price moves; `catalog_price_latest` holds the current price + drives
  dedup). Live retention ~1 year (rolling prune); the history is backed up
  separately, **trimmed to ~3 months, keep 3**.
- **Context / why:** A full daily table for ~47.7k cards would be ~17M rows and
  ~1.7 GB/yr, and the ledger's keep-10 auto-backup would balloon to ~19 GB. The
  separate file keeps the ledger backup small/fast (history is excluded from it),
  dedup + 1yr prune keep the live file to hundreds of MB, and the trimmed 3-month
  history backup caps its own footprint. Total ~1–2 GB.
- **Wiring:** the daily headless task runs the sweep after the owned-item refresh
  and writes a trimmed history backup; File ▸ "Update market prices" triggers it
  manually. Owned-card sets are swept first. Foundation for the market-trend
  feature (surfacing per-card history in the UI is a follow-on).

### 2026-08-04 — History thumbnails: session-scoped pixmap + card-id caches
- **Decision:** The Cards column in both History pages (Transaction card art,
  Recognition region crops) now paints through a shared, process-wide
  `ui/thumbcache.py` — Qt's `QPixmapCache` holding the *pre-scaled* 24×32
  thumbnails, keyed by `namespace:key:WxH`, with a miss set that background
  workers clear via `forget_misses()` as new files land. Catalog art resolution
  is memoised for the session too (`_CARD_ID_MEMO`: (name, set, number) →
  card_id), so a rebuild pre-fills each row's `thumb_ids` and only queues rows
  that still need a lookup or a download. History's mode toggle is stale-gated
  per page (mirroring the nav-level gate), so switching Transaction ⇄ Recognition
  re-shows a loaded page as-is.
- **Context / why:** The delegate decoded each PNG from disk *inside* `paint()`,
  so every repaint — a scroll, a hover, or navigating back to History — re-read
  and re-scaled dozens of files, and each refresh threw away the resolved card
  ids and re-queried the catalog. The thumbnails visibly re-loaded on every
  visit. Measured on the real DB (260 transactions): 19 decodes on a cold render,
  **0 on every later repaint**.
- **Alternatives considered:** A per-view dict (rejected: dies with the view, so
  a rebuild still re-decodes — this is what the Inventory grid does); caching the
  full-size source pixmaps (rejected: rescaling per paint stays, and card art is
  ~50× the bytes of the 24×32 thumbnail).
- **Consequences:** `thumbcache` is the convention for any future grid painting
  small cached images. Un-downloaded art is still retried on a later refresh (the
  memo doesn't suppress the fetch), so art that missed while offline appears once
  it's fetched. Cache limit raised to 32 MiB (~thousands of thumbnails).

### 2026-08-04 — Recognition History can re-export a stored capture
- **Decision:** Recognition History gets **Export to Transaction** / **Export to
  Auction** buttons acting on the selected capture, routed through the *same*
  `MainWindow._export_recognition_to_{transaction,auction}` handlers the live
  Recognition window uses (overwrite confirmation, nav switch, unmatched-give
  warning all reused). The payload is rebuilt from the corpus by a new Qt-free
  `services/corpus.capture_export_cards(repo, capture_id)` returning `ExportCard`s
  — unlabeled boxes skipped, back-side images not double-counted, exact duplicates
  aggregated into a quantity, mirroring the live window's `_collect_export_cards`.
- **Context / why:** The corpus already stores everything an export needs per
  region (identity, side, condition, grading), so a batch you annotated days ago
  is re-usable without re-annotating it. Both windows' `import_recognized` are
  duck-typed, so a neutral service-layer carrier feeds them directly — no
  view→view import from History into the Recognition window.
- **Placement:** the buttons live **in the row**, in the Export column that
  replaced Note (a painted `_ExportDelegate`, not embedded widgets, so sorting and
  filtering can't strand a stale button): two small icon pills — ⇆ Transaction and
  a gavel for Auction, echoing the nav glyphs — dimmed and inert on captures with
  nothing labeled. The note keeps its content as the Date/Source cell tooltip.
  Per-button hover needs an event filter on the viewport: `editorEvent` never sees
  plain mouse moves.
- **Consequences:** `corpus.ExportCard` and `recognition.RecognizedCard` are now
  two carriers with identical fields. `ExportCard` is the layer-correct one (a
  service, headlessly testable); **follow-up:** have the Recognition window emit
  `ExportCard` and delete `RecognizedCard`. A listing-source capture exported to
  Transaction has `side=None`, which Transaction already treats as incoming.

### 2026-08-04 — Autofill popups: a fitter that survives the keystroke
- **Bug:** every autofill popup in the app opened exactly as wide as its input
  box, so card names were truncated — worst in a table cell (the Transaction
  receive-row Name field measured **76px** against labels needing **613px**;
  Auction 189px; Recognition 240px, which is why only Recognition looked
  acceptable). `show_fitted` was doing the right thing and being *undone*:
  `QLineEdit::keyPressEvent` changes the text (emitting `textEdited`, where call
  sites resize the popup) and *then* calls its own `completer->complete()` with no
  rect, which `QCompleterPrivate::showPopup` reads as "use `widget->width()`" and
  applies via `setGeometry`. The resize was overwritten on every keystroke.
- **Decision:** the first `show_fitted` call installs a persistent `_PopupFitter`
  on the completer (found back via `findChild`, so once per completer) that
  filters the popup's own `Show`/`Resize` events and re-applies the fitted width —
  whoever sizes it, content wins. The fixed 620px cap is replaced by a
  screen-aware bound (available width − 40px, with the popup pulled back on screen
  when widening would overflow); `max_width` stays as an opt-in override. The
  scrollbar's width is reserved whenever the row count exceeds `maxVisibleItems`,
  since it reports hidden before the popup is shown.
- **Why in the helper:** all five autofill entries already route through
  `show_fitted`, so no call site changed — the fix lands everywhere at once.
- **Testing note:** calling the `textEdited` slot directly never reproduces this;
  the tests type with `QTest.keyClicks`, and one disables the fitter's re-apply as
  a negative control to prove the popup collapses back to the field width.
- **Follow-up (queued):** only two of the five entries (Transaction receive,
  Auction card) share `_card_entry.CardEntryMixin`; Recognition's `_NameField`,
  Transaction's `_GiveRow` and the image-autofill dialog each roll their own
  completer/popup/hover wiring — which is why the hover-preview bug existed in one
  and not the others. Consolidate them onto one backing so behaviour is inherited,
  not re-implemented. Deferred while those four files are under concurrent edit.

## 2026-08-05 — Identification Layer 0/1: a benchmark, then the bottom band

- **Context:** identification searched only cards the user had already labelled
  (43 of 53,289). Opening it to the whole catalog needed two things first: a way
  to tell whether any change helps, and an answer to the reprint problem — a
  global embedding sees *artwork*, and 33% of catalog cards have a same-named
  card as their nearest neighbour.
- **Layer 0 — `services/identity_eval.py`, built before the layers, not after.**
  Queries are the labelled crops (real photos, known answers); the pool is the
  live catalog index, so the number means what it says. It reports top-1 **and**
  top-5 (a reliable shortlist is a usable product), and splits failures into
  **wrong printing** vs **wrong card** — different fixes, and without the split a
  layer looks useless when it fixed exactly what it aimed at. Runs are logged to
  `model_run` with `kind='identity'` beside the detector and condition models.
- **Full-catalog sweep:** 49,551 of 53,289 embedded, 3,738 failed (no art from
  the source), 50,546 PNGs / **2.21 GB** on disk. That is far under the ~6.9 GB
  estimated when lossless PNG was chosen — the source renders are ~44 KB each, so
  the storage argument for lossy encoding was moot.
- **Layer 1 — `services/identity_patches.py`.** Re-rank the embedding's shortlist
  on the strip along the bottom of the card, where the collector number, set
  symbol and rarity live. A band rather than per-era templates: placement moved
  between eras, but the band contains all of it in every era, so no era detection
  is needed to be correct.
- **Phase correlation, not plain NCC — decided by measurement.** Direct
  correlation of a fixed bottom crop needs sub-1% framing accuracy: a 2px slip in
  a 627px canonical card, or 1% of scale error, cuts the own-vs-reprint margin
  from 0.023 to 0.006. A translation search does *not* rescue it — the search
  locks onto the large pale strip both printings share. Whitening the spectrum
  discards exactly that shared low-frequency bulk and leaves the fine structure
  that differs; same misframing, margin +0.13 to +0.46.
- **Scores are centred on the shortlist's median** before the weight applies. The
  absolute peak level reflects the photograph (focus, glare, framing) and moves
  every candidate together, so only the spread carries information — and centring
  makes the weight mean one fixed thing across queries.
- **Measured on the full index (58 labelled crops, 49,551-card pool):** top-1
  **20.7% → 44.8%**, top-5 **74.1% → 77.6%**, wrong-printing errors **36 → 21**
  with wrong-card flat at 10-11. The split confirms the layer fixed what it
  targeted rather than something incidental.
- **Weight 0.5, not the peak.** Top-1 keeps climbing to 50% at weight 1.5, but
  top-5 falls back *under* the baseline and wrong-*card* errors start rising —
  the band stops breaking ties and starts overruling the embedding. 0.5 is the
  last point where both metrics improve, and top-5 is what the UI shows.
- **`Suggestion.score` stays a plain cosine**; the band reorders and is reported
  separately as `Suggestion.band`, so the UI's low-confidence threshold keeps
  meaning one thing.
- **Still open:** the 10 wrong-*card* misses are untouched by this layer and need
  a better encoder (Layer 3). Many wrong answers are Japanese cards outranking
  English ones, worth a look on its own.

## 2026-08-05 — Suggestions rank against the catalog, scoped per card by language

- **The bug:** Recognition's candidate pool was `labeled_card_ids()` — the matcher
  could only ever propose a card that had already been identified once before, so
  it was useless for the thing it exists to do. With the catalog indexed
  (49,551 cards) the pool is now the real catalog, via
  `catalog_index.candidate_pool`.
- **What that exposed:** the catalog holds **27,014 Japanese cards against 22,537
  English**, and a release shares its artwork across regions exactly. A global
  embedding sees artwork, so an English card's nearest neighbour is routinely its
  Japanese twin — **42 of 46 wrong answers were cross-language**, and most sat at
  rank 2, right behind the twin.
- **Language scoping is worth more than every scoring layer combined:**

  | configuration | top-1 | top-5 |
  |---|---|---|
  | catalog pool, all languages | 20.7% | 74.1% |
  | + bottom band (Layer 1) | 44.8% | 77.6% |
  | EN scope | 67.2% | 79.3% |
  | **EN scope + band** | **67.2%** | **84.5%** |

- **Inferring the language was tried and rejected.** A linear probe over the
  existing card embeddings scores **99.8% on held-out catalog renders and 51.7%
  on real photographs** — worse than always guessing English. It had learned
  properties of clean renders that do not survive photography. Trusting the
  held-out number would have shipped something worse than a constant; this is the
  case for benchmarking on the real query domain rather than a convenient split.
- **So the scope is stated, not predicted** — and it lives **per box**, not per
  scan, because one photo routinely holds both catalogs. New boxes inherit the
  last choice (a single-language scan costs one change, not one per card), and
  the two faces of a card share a language exactly as they share a condition.
- **Labelled cards stay in the pool whatever the scope.** A card the user has
  already identified is known-relevant and carries crop exemplars shot under the
  same conditions as the query; dropping one for a scope setting would make the
  matcher forget something it had been taught.
- **This reverses the planned Layer 2.** Number OCR was going to be next, on the
  reasoning that the collector number separates printings. It does — the format
  alone is nearly a perfect region marker (93% of EN numbers are bare digits, 84%
  of JP are `N/M`, and no EN card uses `N/M`) — but scoping captures that signal
  exactly and for free. After scoping, only **3** wrong-printing errors remain
  while wrong-*card* errors stand at 14, so the next real lever is a better
  encoder (the ArcFace fine-tune), not OCR. OCR is demoted, not discarded.

## 2026-08-06 — Identity Layer 3: carrying a photograph into render domain

- **Root cause, finally measured.** A photo sits at median cosine **0.792** from
  its own card's render but **0.877** from the nearest *wrong* card's. The
  encoder was ordering the pool backwards; top-1 of 20.7% was never a tuning
  problem. Queries are photographs and the index is clean renders — a domain gap,
  not a similarity gap.
- **The augmentation was validated before it was trusted.** Real photos sit at
  median 0.792 from their own render; renders degraded with perspective, blur,
  glare, exposure and a loose crop on a mat sit at 0.792 too. Only because the
  synthetic gap matches the real one is it legitimate to train on it — and the
  validation set stays the labelled photo crops throughout, never renders. This
  is the same setup that produced a 99.8%/51.7% embarrassment for the language
  probe, used the right way round.
- **A rotation, not a free linear map.** Orthogonal Procrustes can only rotate,
  never rescale or discard directions, so it cannot trade away the structure that
  keeps the rest of the pool separable. Unconstrained ridge collapses: at 6,000
  pairs it takes top-5 from 74.1% to 69.0%, and at λ=100 the ranking is destroyed
  entirely. The constraint is doing real work, not decoration.
- **Fit size was measured, not guessed.** 800 pairs *hurt* top-5 (83.3% → 77.8%
  under EN scope) — the earlier read that this layer was a dead end was data
  starvation, not a flaw. 6,000 pairs improves both metrics; 20,000 is flat
  against 6,000. The Operations job samples 4,000 cards (8,000 pairs).
- **Applied per domain, which is the part easy to get wrong.** `suggest` compares
  against crop exemplars (photographs, like the query) *and* catalog renders. The
  map is applied only to the render comparison; projecting the query for both
  would score exemplars in a space they were never in.
- **Measured end to end through the service path:**

  | configuration | top-1 | top-5 | wrong printing / card |
  |---|---|---|---|
  | raw encoder | 20.7% | 74.1% | 36 / 10 |
  | + projection | 58.6% | 82.8% | 17 / 7 |
  | EN scope + projection | 70.7% | 81.0% | 4 / 13 |
  | **EN scope + projection + band** | **75.9%** | **84.5%** | 6 / 8 |

- **No training environment, no torch.** The artifact is a 384×384 float32 matrix
  in the data dir, fitted in minutes from catalog art alone and applied as one
  matmul. The ArcFace fine-tune (3c) stays available for the 8 remaining
  wrong-card misses, but is no longer the only route to them.
- **Honest limit:** the benchmark is 54–58 labelled crops, so a few points is a
  couple of queries. What makes the result credible is that the mechanism was
  measured directly (the 0.792/0.877 inversion), the fix is principled, and the
  gain shows up on both metrics and both pool slices — not the headline number
  alone.
- **Side-fix, found the hard way.** The new tile named an icon (`wand-sparkles`)
  that is not bundled. `icons.pixmap` raised `FileNotFoundError` part-way through
  building a widget, and the symptom was not a missing glyph but an **access
  violation in an unrelated auction test fourteen tests later** — Qt was left
  holding a half-constructed object. `pixmap` now returns a blank pixmap for an
  unknown name and always ends its painter, so a typo (or a stripped asset in the
  frozen build) costs a glyph rather than a window. `tests/test_icons.py` asserts
  every icon named in the operations registry is actually bundled.

## 2026-08-06 — The photo map is a model, so it is reported like one

- **Prompted by a fair question:** why is "fit photo matching" treated
  differently from the detector and condition models? It is learned from data and
  it changes accuracy — calling it a maintenance job was underselling it.
- **What actually distinguishes it** (all three are learned): it needs no
  training environment (closed-form SVD, in-process), it does not invalidate the
  catalog index, and it cannot diverge. That last pair matters most — a real
  fine-tune produces `identifier_finetuned.onnx`, which flips the embedder
  version and orphans all 49,551 indexed embeddings, costing a ~68-minute
  re-sweep. The projection is applied to the query at search time, so installing
  or reverting it is free.
- **What was genuinely inconsistent, and is now fixed:** it logs to `model_run`
  (`kind='identity'`, `source='projection'`) like everything else, the tile
  states when it was fitted and what it scored, it has a stale window, and
  **Remove photo matching** reverts it. Renamed from "Teach recognition what a
  photograph looks like" to **Fit photo matching** — the old name described the
  effect and hid that it fits a model, which is exactly what caused the question.
- **It now learns from the labelled corpus too**, at the user's suggestion, and
  the corpus pairs are strictly better data: a crop actually photographed against
  the art of the card it was labelled as — the real relationship rather than an
  imitation. Backs are excluded, since every card back is identical and pairing
  one with a specific card's front art teaches the map nonsense.
- **Which forces held-out evaluation.** The corpus pairs *are* the benchmark's
  queries, so training on them makes the usual number meaningless.
  `cross_validate` holds out a whole **capture session** at a time, not a single
  crop — two photos from one session share lighting, mat and camera, so a
  per-crop split would leave near-copies of the held-out card in the training
  half.
- **Weight chosen by that measurement, and my guess was wrong** (I had picked
  40):

  | corpus weight | held-out top-1 | top-5 |
  |---|---|---|
  | 0 (synthetic only) | 58.6% | 81.0% |
  | **10** | **63.8%** | **82.8%** |
  | 40 | 60.3% | 79.3% |
  | 400 | 55.2% | 70.7% |

  Real pairs are worth +5.2 top-1, and they cannot carry the fit: a few dozen do
  not constrain a 384×384 rotation, so leaning harder trades away the breadth the
  synthetic pairs give. Expect the weight to want raising as the corpus grows.
- **Caveat stated plainly:** the weight was selected on the same leave-one-out
  that reports it, over 58 pairs. The monotone shape either side of the peak is
  what makes it credible, not the peak itself.
- Shipped fit (4,000 cards + 63 corpus pairs): held-out top-1 **20.6% → 66.7%**,
  top-5 **76.2% → 82.5%**.

## 2026-08-06 — Models becomes its own destination; the loss curve becomes real

- **Decided:** the four model groups (training environment, region, condition,
  identity) **move out of Operations entirely**, with no pointers left behind.
  Operations keeps ledger work — imports, exports, prices, reference data,
  automation, maintenance. Models is the ML subsystem: what is served, what it
  scores, how to train it, and what the corpus is feeding.
- **Nav icon:** Lucide `cpu`, added to `ui/assets/icons`. It reads as the machine
  side of the app at 18px, and collides with nothing already in the set
  (`sliders-horizontal` is Operations, `scan-line` Recognition, `package`
  Inventory).
- **The stair-stepped loss curve was not our bug.** I had guessed an off-by-one
  in `parse_results_csv`; the parser is faithful, and so is `add_epochs`. The
  duplication is in **ultralytics' own `results.csv`**: it does not validate on
  every epoch and repeats the previous row's `val/*` figures on the ones it
  skips, so run #4's 100 epochs hold only 51 distinct validation values, in
  pairs. `train/box_loss` moves every epoch — 100 distinct values over the same
  run.
- **No config fixes it**: ultralytics 8.4.115 exposes no `val_period`, and
  `val=True` already. So the fix is on our side and is better anyway — record
  the **training** loss beside the validation loss. `model_epoch` gains
  `train_box_loss` / `train_cls_loss` (added by `_ensure_columns`, so existing
  vision DBs gain them on open), and `_METRIC_COLUMNS` maps `train/*` to them.
  Future runs carry an honest per-epoch curve; the validation figures stay,
  because they are what the model is judged on.
- **Existing rows are left alone** — the data was never wrong, only partial, and
  the CSVs are still on disk if a backfill is ever wanted.

## 2026-08-06 — Models becomes a destination of its own

- **Built** `ui/views/models.py`: Overview, Region detection, Condition, Card
  identity, Training environment, Data corpus, behind an in-page navigator that
  mirrors the Operations screen so the two maintenance surfaces read as siblings.
- **The four model groups left Operations outright**, with no pointers behind
  them — `GROUPS` is now the ledger's sections and `MODEL_GROUPS` the Models
  screen's. One registry still backs both, so the run log, statuses and staleness
  keep working off the same ids.
- **Deliberately not a tile grid.** A detector has a hundred epochs of loss; the
  grader has a gate it has not passed; identification has no single artifact at
  all. Repeating one tile shape would flatten exactly the differences worth
  seeing, so each model gets the shape its evidence needs — metrics and a curve,
  a coverage chart, a component table.
- **New seams:** `services/modeljobs.py` holds the ten jobs (Qt-free, so a script
  or a scheduled task can call them), `services/modelsummary.py` assembles what
  each section says, and `ui/views/_worker.py` is the background worker both
  screens share. The view holds no SQL and makes no judgement about "stale" or
  "blocked" — those are testable claims now, and they are tested.
- **A layered identity benchmark** (`modeljobs.benchmark_identity`) scores the
  encoder, then each correction on top of it, and stores the breakdown in
  `metrics_json`. That is what lets the screen say what each layer is worth
  rather than quoting one number for the stack.
- **It found a leak, and the screen says so.** Measured through the layers, the
  stack reads **top-1 93%** — but photo matching is fitted partly on the labelled
  crops, and those crops *are* the benchmark's queries. Anything downstream of it
  is scoring data it has already seen. The run records `in_sample_from`, and the
  overview prints the caveat next to the honest held-out figure (66.7%). A
  dashboard that quietly reported 93% would be worse than no dashboard.
- **Also fixed:** `detector_summary` assumed a promoted model always has a corpus
  snapshot; it does not before anything is trained. And `QChartView` paints its
  own pale viewport, which framed the dark loss chart in white until it was made
  transparent — `theme.style_chart` handles the chart, not the view around it.

## 2026-08-07 — "The train buttons do not work" — three real defects behind it

- **The wiring was fine.** A click did start the job; the screen simply never
  said so. `refresh_running` set an attribute and nothing else. Detector
  training runs for minutes on a worker thread, so the screen was byte-for-byte
  identical before and after the click — which is indistinguishable from a dead
  button. There is now a **busy banner** naming the job and roughly how long it
  takes, with an indeterminate progress bar, and every action disables until it
  finishes.
- **A disabled button now says why.** Condition's *Train* is gated on the corpus;
  greyed with no explanation it read as broken. It carries the shortfall:
  "Needs 13 more graded cards across MP, HP, DMG (8 per grade, over 3 photos)".
- **`refresh()` left the previous render's widgets parented.** `deleteLater()`
  defers to the event loop, so a walk over the children could still find last
  render's buttons — the disable-everything pass would then miss the real ones.
  They are unparented immediately.
- **The encoding trap, for the third time in this project.**
  `subprocess.run(..., text=True)` decodes the trainer's output as the console
  codepage (cp1252 here); ultralytics emits a byte outside it, the reader thread
  dies, and *the whole of the worker's stdout is lost* — including the
  `TCGI_RESULT` line carrying the metrics. All three call sites now pass
  `encoding="utf-8", errors="replace"`.
- **Retraining can lose, and the screen used to hide it.** A run on the full
  corpus scored mAP50-95 **0.95522** against the served model's **0.96996**, so
  `should_promote` correctly refused it — and the staleness banner, which
  measures against the *promoted* model, said "stale" again. That is a loop:
  click Retrain, wait six minutes, get told to retrain. `DetectorSummary.declined`
  now carries the newer-but-worse run, and the section says so and points at more
  labelled photos instead.
- **Verified end to end on a real run** (#11): 100 epochs recorded, and
  `train_box_loss` holds **100 distinct values** where the validation column
  still repeats at 75 — the per-epoch fix works on live data, and the chart has
  its full four lines.
- **A performance worry that was not real:** `environment_summary()` looked
  expensive (it probes ultralytics, torch/CUDA and `nvidia-smi`, and reads the
  environment's size) but measures 0.82s cold and 0.07s warm, because `training`
  already caches its probes. No change made.

## 2026-08-07 — A finished run gets a window, not a toast

- Training the detector, training the grader and benchmarking identification now
  end in a themed **result window** rather than a one-line message box. A run has
  metrics, a promotion decision and usually a reason it went the way it did;
  those are worth reading once, at the moment the run lands.
- **Assembled from the run log, not from the job's return string**
  (`modelsummary.run_result`). The log already holds the metrics, the promotion
  outcome and the corpus it trained on — parsing a sentence back apart to show
  any of it would have been absurd, and would have coupled the window to
  wording.
- **Each kind reports what it actually has.** The detector shows mAP50/mAP50-95/
  precision/recall and, when it loses, both sides of the comparison plus the
  advice that follows from it ("95.5% against 97.0%, so v1 keeps serving —
  training again will not change that, more labelled photos will"). The grader
  shows exact against always-NM, and says plainly when it is *worse* than the
  constant guess. The benchmark shows the layer bars, the split of remaining
  misses, and the in-sample caveat when photo matching was fitted on the same
  crops.
- **Only jobs with a run behind them open it.** Indexing the catalog, re-indexing
  crops and building the environment report a sentence and nothing more, so they
  keep the plain message — an empty results window would be worse than none.
- A `failed` run gets the window too, with the error, rather than the outcome
  being a warning box that looks unrelated to the model.

## 2026-08-07 — What makes a detector better, and when a model is stale

The Models window could not replace the detector and would not stop asking. Both
came from the same root: **mAP50-95 was the whole promotion gate**, and staleness
was measured against whatever that gate had last promoted.

- **The metric was wrong for the job.** mAP50-95 averages average-precision
  across IoU 0.5→0.95, so above about 0.75 IoU it scores how tightly a *human*
  drew the training boxes rather than whether a card was found. On this corpus it
  was also saturated: mAP50 pinned at 0.995 and recall at 1.000 for every run.
  Five consecutive retrains, each on 39% more data and each with **five points
  better precision** (0.949 → 0.999), lost by 0.0147 of the one metric that
  happened to disagree.
- **The number was read off the luckiest epoch.** `best.pt` is the maximum of a
  hundred noisy samples. The incumbent's 0.970 sat **2.1 standard deviations
  above its own tail mean** (0.931, σ 0.018) — the rejection margin was smaller
  than one run's epoch-to-epoch noise.
- **The two numbers were never comparable anyway.** The exporter shuffled the
  whole corpus and sliced off the first 20%, so growing from 28 images to 39
  replaced the validation set wholesale. v1 was scored on five held-out images;
  every candidate since was scored on seven different ones.

**What replaced it** (`services/fitness.py`, one module for a judgement that had
been living in three places and disagreeing with itself):

- **A composite score, per kind.** For the detector, 0.40·recall + 0.30·precision
  + 0.20·mAP50 + 0.10·mAP50-95 — a card not found is a card not identified, a
  phantom box is a row to delete by hand, and crop tightness only has to be good
  enough for the encoder. Deliberately not ultralytics' own fitness (0.1·mAP50 +
  0.9·mAP50-95), which is tuned for leaderboards where tightness is the game. On
  the real log this scores the refused run **0.9942 against v1's 0.9807** — it
  wins outright, not on a tiebreak. The grader's score is *lift over always
  guessing the commonest grade*, so "must beat guessing" needs no threshold;
  identity's is 0.7·top-1 + 0.3·top-5.
- **A noise floor from the run's own tail.** Consecutive late epochs are
  near-identical models on an identical split, so their spread is measurement
  noise. Guarded at both ends: fewer than five epochs is not a tail, and a spread
  wider than 0.05 means the run never converged — both fall back to a flat
  minimum, because an unusable noise estimate must not become a licence to ignore
  real differences.
- **Inside the band, more data wins.** Indistinguishable models are separated by
  which saw more of the corpus. This is what lets the detector move at all once
  the metrics saturate.
- **A stable validation split** (`_val_rank`): membership decided per image by
  hash of its id, so an image in val stays in val as the corpus grows. Runs are
  stamped with `split_policy`; runs from before it are held **incomparable** and
  the corpus decides instead. The split directories are also cleared before each
  export — they were only ever added to, so an image that moved between splits
  left its old copy behind and could sit in *both*.
- **The incumbent is re-scored on the candidate's own split.** The worker takes
  `--baseline` and runs one val pass over the outgoing model, which is the only
  strictly like-for-like comparison there is. Best-effort: a baseline that fails
  to load must not lose a good training run.

**Staleness now means "there is something better to be had"** — labels no run has
seen, or a run already in the log that beats what is serving. Measured against
the newest *finished* run rather than the promoted one, because a run that read
every photo and then lost on score has still read every photo. Both conditions
clear the moment you retrain; the old one could never clear once a retrain was
refused, which is what produced five identical runs.

**Two display bugs found alongside.** The grader's `precision` column holds
macro-F1, but the window read it as the always-NM baseline — so "it beats
guessing Near Mint every time" was comparing exact accuracy against an unrelated
quantity. The baseline is in `metrics_json` and is now read from there.
`accuracy_series` charted `map50_95`, which for a condition run is *mean grade
distance* where lower wins, so the grader's improvement was drawn upside down; it
charts the new `fitness` column, higher-is-better for every kind by construction.

**Identity had no version at all.** Every benchmark was `promoted=0`, so
`current_model('identity')` was always None and a change that halved top-1 would
have passed in silence. Benchmarks are now promoted against the previous one,
scoped to `source='benchmark'`, with a binomial error bar for a floor — at 58
queries the standard error of a 45% top-1 is **6.5 points**, and two benchmarks
inside that band are the same benchmark. A materially worse one is reported as a
regression rather than as the new truth.

### The window froze for four seconds

Measured, not guessed: one `refresh()` cost **4.2s**, of which `ci.index_status`
was 2.07s — a `stat` per file across **50,546 cached PNGs** — called twice, for
two integers already available from a 3ms `COUNT(*)`. The archive walk is now
opt-in (only the reclaim tile reads those fields); `corpus_summary` takes the
detector and condition summaries it used to rebuild; `_gpu()` is cached rather
than shelling out to `nvidia-smi` twice per refresh; starting a job shows its
banner without a data pass at all; and a finished job reports before it rebuilds.
**4201ms → 58ms.**

### A flaky segfault that was ours all along

Adding four tests turned the suite into an intermittent access violation, in an
unrelated file, at a site that moved between runs. The cause was a total, not a
line of code: views drop old widgets with `deleteLater()`, which needs an event
loop to take effect, and the suite never spins one — a full run finished holding
**~33,000 live QWidgets and 1,800 top-level windows**. A module-scoped autouse
fixture now drains the deferred deletes (14,433 / 258, and stable over repeated
runs). Deliberately **module**-scoped: draining after every test frees the C++
side under wrappers a live fixture still holds and crashes far sooner than the
leak does.

### Correction, same day: "Stale" is about the corpus, not the model

The first pass at this folded *"a logged run beats the model in use"* into the
stale state. Wrong, and wrong in the same shape as the bug it replaced: it made
the flag a claim about model quality, so it stayed lit after a retrain that had
nothing left to learn from.

**Stale now means one thing: a significant amount of labelled data that no
training run has seen.** Never a judgement about how anything scored. A model
that has read everything you have is not stale however it performs; one that
hasn't is stale even if it performs beautifully. The two questions have different
answers and different fixes, so they get different marks — quality problems are
`weak` ("Underperforming"), and the stale pill reads **"New data"**, which is
what it actually reports.

Applied to all three: the grader gets the same corpus-drift reading over graded
cards, and "trained but no better than guessing" became `weak`; identity has no
corpus of its own to fall behind, so it is never stale, and a benchmark below the
one in use is `weak`.

**Significance is proportional** (`stale_threshold`): `max(4, 10% of the
corpus)`. A flat four photos fires after every labelling session once the corpus
is large, and a flag that fires constantly is one you stop reading. Below the
threshold the card still says how many are new — as a plain line, not a warning.

## 2026-08-08 — What the review gate says about itself, and what it does offline

Two decisions about `review_gate.py`, both from ticket 003.

### `show` was promising a guarantee the gate stopped making

`review_gate.py show` printed *"no review on record — the gate will block a merge
or a close"* and exited 1. Then `gh pr merge` merged, unblocked. That looks like
the gate failing open, and it was first reported that way. It was not.

`hook()` has returned 0 on a missing verdict since the critic panel became opt-in
(ed4131d): a panel costs ~26k tokens per agent, so it runs when asked, and only a
recorded `block` stops anything. `show` was simply never updated alongside it.

The rule this leaves behind: **a checker states what it will actually do, and
exits 0 for its normal state.** Overstating a guarantee is the worse direction to
be wrong in — an understated gate gets checked, an overstated one gets trusted.
Exiting 1 on the ordinary case has the same shape, because a script that treats
"nothing to report" as failure gets its exit code ignored.

`show` now also names the lanes that would block a merge, rather than printing
lane verdicts and leaving the reader to work out the consequence. That the old
wording survived a redesign of the very function it describes is the argument for
stating the consequence and not just the data.

### Offline, the gate records a weak binding rather than refusing

Steps 2-5 of that ticket bind a verdict to the PR's head commit instead of local
`HEAD`, which needs `gh`. **When `gh` is unreachable, `record` falls back to local
`HEAD` and writes `"binding": "local-head"` into the artifact.** It does not
refuse.

Failing closed is the stronger guarantee and the wrong trade here. The app is
offline-first and single-user, the panel is opt-in, and the failure would land on
someone who has just spent ~100k tokens running four critics — discarding that
because GitHub is unreachable teaches people to route around the gate, and a gate
that gets disabled is worth less than a weak one that gets kept.

What makes it safe is that the weakness is **named**: `hook()` treats a
`local-head` artifact as attesting to the checkout only — it may open the gate
when `HEAD` is the PR head, and must not when it is not, which is the case that
silently passes today — and `show` prints the binding. Today's guarantee is
already exactly this weak; the difference is that it will say so.

## 2026-08-09 — Embeddings are stored at half precision, and say so

`card_embedding.vector` was 76.1 MB of a 104.3 MB `vision.db` — the largest
single thing in the file. The vectors are L2-normalised DINOv2-small outputs
compared only by cosine ranking, so float32's dynamic range buys nothing, and
measurement says it buys nothing *in fact*: over all 49,551 vectors, 482 probes
(182 real photo crops and 300 noised catalog renders) picked the **identical**
best match and the identical top-5 after conversion. **Stored vectors are now
float16.**

**int8 stays rejected.** It moves 3% of top-1 answers. Identification is at ~45%
top-1 (ADR 2026-08-05); spending three points of agreement for a further 19 MB is
not a trade worth making, and nothing should revisit it without a full benchmark.

### The dtype is written down, not inferred

Each row carries a `dtype` column defaulting to `'float32'`. Three things follow,
and they are the reason this shape was chosen over casting in place unmarked:

* an existing file keeps reading before it is converted;
* an **interrupted** conversion leaves a mixed table that is still correct, which
  is what makes the job safe to stop and safe to re-run;
* a rollback is a cast in the other direction.

Inferring the dtype from blob length was the obvious alternative and is a trap:
it works exactly until a model with a different `dim` lands, and then it is
silently wrong. `services/vectors.py` is now the only module that names a storage
dtype — `data/vision.py` keeps passing bytes, because numpy stays in the service
layer.

### Why a marker mattered more than it looks

The old readers each did `np.frombuffer(blob, dtype=np.float32)`. Point one of
them at float16 bytes and it returns a **half-length** vector — and both scoring
paths *skip* mismatched vectors rather than raising (`identity_eval.load_pool`
drops rows whose size disagrees with `dim`; `Recognizer.suggest` compares
`vec.shape == probe.shape`). A missed reader would therefore have produced an
identifier that quietly suggested nothing, with no traceback anywhere. So
`unpack` takes the row's `dim` and *raises* on disagreement, and a test asserts
no module outside `vectors.py` decodes an embedding at all. The ticket's own
impact table missed one such reader (`identity_eval.load_pool`), which is the
evidence that a five-place implicit convention was the actual defect.

### Dequantise once, at the pool, and mind the WAL

Vectors are read back as float32 wherever the pool is materialised, never per
query: numpy emulates float16 arithmetic, and a per-query cast measured 111 ms
against 74 ms — slower than doing nothing. **The consequence is that this is a
disk win, not a memory one:** a loaded index is still 76.1 MB of float32 in RAM,
and the ticket's claim of "38 MB of RAM per loaded index" contradicted its own
instruction to keep the working matrix float32. Only the disk and the page cache
shrink.

`VACUUM` alone also did nothing visible: `open_vision` sets WAL, so the rebuilt
pages sit in the `-wal` file until a checkpoint and the main file never shrinks.
The conversion ends with `PRAGMA wal_checkpoint(TRUNCATE)`. A test asserts the
file on disk actually got smaller, because that is the whole point and it is the
part that silently didn't happen.

### It is a tile, not a startup migration

Conversion is a *Compact the card index* operation in the `model_identity` group
— one registry entry and one `(conn) -> sentence` runner, per M-Ops1. Running it
inside `open_vision` would put a 76 MB read-modify-write behind the splash on
every boot forever to save one click once. The cost of the choice is that the
saving is opt-in: an un-compacted install keeps its bytes and the tile says so,
reporting the dtypes actually on disk rather than a last-run timestamp, since a
half-converted file is a legitimate state.

Measured on the real corpus: **104,292,352 → 53,006,336 bytes** in 2.1 s for
49,733 vectors, payload 76.11 → 38.06 MB, zero free pages left, and 100.00% top-1
and top-5 agreement with the pre-conversion answers.

### 2026-08-10 — Planning reads the rule registry; this file is an archive (ticket 010)

- **Decision:** `plan.py` no longer lists `docs/decisions.md` as a required read.
  What binds comes from `.claude/context/rules.json`, scoped to the files the
  task touches. Measured across all 11 windows: **~31.9k → ~2.8k tokens, −91.3%**.
- **Why the registry and not a smaller log.** A log is append-only — a reversal
  adds a second entry and both stay — so it is O(project age) and no amount of
  shrinking entries changes the slope. A registry is superseded in place (two
  contradictory rules is a bug, two contradictory ADRs is just history), so it
  grows with the app's distinct invariants and then plateaus: 35 rules today.
  An index of ADR titles was measured at 893 words and **rejected** — a 25×
  improvement that is still O(entries), and with no integrity check.
- **Keyword-scoping the log was rejected on measurement.** Its entries are prose
  that mentions everything: 51/89 match recognition terms, 52/89 ui/theme, 47/89
  money/schema. The log cannot be narrowed; the registry already is.
- **This file is now write-mostly.** Its length and its ordering stopped
  mattering, which is just as well — it claims "newest first" and is not: there
  are 8 monotonicity breaks and the tail runs oldest-first. Nothing scans it, so
  that is no longer worth a reorder.
- **It is still reachable, by anchor.** `rules.py show <ID>` prints the one
  section a rule cites, capped at 80 lines — some anchors are JSON subtrees, and
  a command that replaces a 30k-token read must not become one. Four windows whose
  design lives here rather than in a spec now cite an **exact heading**, resolved
  to a line range in the reading list; `plan.py validate` fails if one is missing
  an anchor or the heading moves. Matching by date was tried first and is far too
  coarse — `2026-07-10` alone carries ten entries.
- **Two `plan.py` defects surfaced by running it on this very ticket.** Its
  request resolved to **History**, exit 0, because "decision **log**" hit that
  window's 3-letter alias; and `MONEY_WORDS`' `p/?l` collapsed to a bare `pl`,
  so `\bp/?l\w*` matched **"planning"** and put `specs/02-data-model.md` at the
  top of the reading list. The regex is fixed here. The resolution floor is
  ticket 011 — it is a redesign of the confidence model with its own corpus, and
  bundling it would hold this up.

### 2026-08-10 — The gate admits by PR, not by whatever is checked out (ticket 003)

- **Decision:** `hook()` looks a verdict up by **`(scope, ref)`** — the PR or ticket
  the command actually names — and `record --scope pr-merge` binds the artifact to
  the **PR's head commit**, resolved with `gh pr view --json headRefOid`. The sha
  now decides only one thing: whether the verdict has been *superseded*.
- **What was broken, and it was worse than "weaker than it looks".** Admission was
  keyed on `verdict_path(repo, head(repo))` — the local checkout. A panel that
  blocked PR #8 on the branch tip was therefore invisible from anywhere else, so
  `gh pr merge 8` from `main` found no artifact and proceeded. Measured before the
  fix, same PR and same recorded block: **exit 2 from the branch tip, exit 0 from
  `main`.** The single thing this gate still enforces was bypassed by `cd`.
- **The ticket framed three holes as "fails open, the merge proceeds"; under
  opt-in only one of them bites.** A *missing* verdict already merges, so a
  mis-bound `pass` is indistinguishable from no verdict at all — legal either way.
  What genuinely broke is a recorded **`block`** becoming findable only by accident.
  That reframing is why the fix is ref-lookup first and sha-binding second, rather
  than the reverse.
- **`ref` was stored and never read.** `cmd_record` wrote it at `:159` and `hook()`
  ignored it entirely, so the field was decoration. Reading it is most of the fix.
- **Supersession is decided offline, and fails closed.** The hook has a 20s budget,
  so it must not call the network — resolving the PR head there was rejected for
  that reason. Instead the recorded head branch is resolved locally,
  `refs/remotes/origin/<branch>` first: in the ordinary workflow you push from this
  machine and then merge, so it is current. **When neither ref resolves the verdict
  is treated as still applying** — the one place this file deliberately fails
  closed, because discarding a recorded `block` on the strength of a lookup that
  did not work is the gate failing open exactly when it should hold.
- **The ticket's plan step 5 contradicted its own verification list, and the
  verification list won.** Step 5 asked that a verdict whose sha no longer matches
  "must block"; the verification list asked that pushing a commit "reopens" the
  gate. Under opt-in the second is right — blocking on staleness would make a
  *reviewed-then-amended* PR harder to merge than one nobody reviewed, which is the
  perversity ticket 008 already reversed once. Worth recording because step 5's
  wording is the pre-opt-in assumption surviving inside the plan of the very ticket
  whose fourth hole is that same assumption surviving inside `show`.
- **Classification is by argv, not by substring.** `GUARDED`'s
  `\bgh\b[^\n]*\bpr\b[^\n]*\bmerge\b` blocked a `gh pr create` whose *body prose*
  contained "merge" — and the workaround was `--body-file`, i.e. a guard teaching
  people to route around it. Commands are now tokenised and read positionally, so
  quoted prose is one opaque token and `merge` only counts in the action position.
  Capturing the ref needed argv parsing anyway, so both halves are the same change.
- **The tokeniser moved to a leaf module, `_shell.py`.** `branch_lease.py` already
  had it, and the precedent set a day earlier by ticket 010 is import-over-copy
  (`plan.py` imports `rules.path_matches` rather than duplicating its three
  hard-won bug fixes). But importing it *from `branch_lease`* would drag `_session`
  into `review_gate`'s import path — measured 26.6ms and a registry read — and
  `review_gate` deliberately imports `_session` lazily inside a `try`, because a
  module-scope `ImportError` in a `PreToolUse` hook raises on every shell call in
  the session. A leaf keeps both properties.
- **`show` had to be rewritten, not extended, and running it is what caught that.**
  The first version listed the other verdicts and then printed "nothing will be
  blocked" underneath a live block — hole four reintroduced in the same commit that
  fixed the rest of it. It now reports every artifact in the checkout with its
  binding and whether it is superseded, and states the consequence per artifact.
- **Two rules added to the registry, because nothing enumerated any of this.**
  `rules.py scope .claude/hooks/review_gate.py` returned exactly one rule
  (`CHECKER-NEEDS-NEGATIVE-TEST`) — and since ticket 010 the registry is the *only*
  thing `plan.py` reads, so an unenumerated discipline is now invisible to every
  future brief rather than merely absent from critic dispatch. `HOOK-FAILS-OPEN`
  ("fail open on confusion, closed on the detected condition") and
  `HOOK-CLASSIFIES-ARGV` both bind `.claude/hooks/**/*.py`. 35 rules → 37.
- **Verified end to end against real `gh`, not only against stubs.** Every test
  stubs `pr_head` for hermeticity, so the live path was exercised by hand: PR #27's
  head resolved to `92ef5c3` on `chore/close-ticket-002` while local `HEAD` was
  `58dbf29` — the hole, visible in one line — and a block recorded for #27 stopped
  `gh pr merge 27` from that checkout while leaving `gh pr merge 99` alone.
- **Consequences:** `record` must run *after* the branch is pushed, or it binds to a
  PR head that does not include the last commit; `/sdd-ticket` says so now.
  `--inherit <sha>` is unchanged — artifacts stay keyed on a sha filename, so the
  per-round audit trail and the documented flag both survive. `ticket-close` still
  binds to local `HEAD` on purpose: closing a ticket is a statement about work that
  has already landed, so the checkout is the right referent.

### 2026-08-10 — A window must be named, not mentioned (ticket 011)

- **Decision:** `plan.py` scores a window at full confidence only when the
  request **names** it — a distinct head that is, opens with, or closes with the
  alias. An alias merely appearing inside a sentence must be **≥6 characters**
  to resolve anything. Otherwise: no window, exit 2.
- **Why 6.** Measured on a 17-case corpus that ships as the test. Every alias of
  5 characters or fewer is an ordinary English word (`log`, `ops`, `tx`, `ml`,
  `post`, `sale`, `write`, `shows`, `audit`, `month`, `price`, `stock`, `home`,
  `grid`); 6+ holds the specific ones (`vision`, `camera`, `photos`, `corpus`,
  `bidding`, `signals`). 7 and 8 also score 17/17 but would stop `vision` and
  `camera` resolving, so 6 is the least restrictive that works.
- **The obvious fix does not work, and measurement is the only reason we know.**
  A floor on *body* matches scores 12/17 at every threshold, because
  `split_request` returns the whole sentence as the head when there is no
  separator — so on the commonest request shape every match is already a "head"
  match at maximum confidence. The axis is named-vs-mentioned, not head-vs-body.
- **Refusing is the better way to be wrong.** Exit 2 prints every window and
  costs one re-run. Exit 0 wrong is trusted by everything downstream: ticket
  010's own request resolved to **History** and produced a dossier about the
  transaction ledger, `audit_log`, and a money flag, for a task that touches no
  app code at all.
- **Split from ticket 010 deliberately** — that one is mechanical (what the
  dossier reads), this is behavioural (which window it resolves to) and carries
  a tuning constant. Bundling would have held a 91% token reduction behind a
  change that may want adjusting in use.

### 2026-08-10 — The catalog stores the number printed on the card (ticket 012)

- **Decision:** `card_catalog.number` means *the collector number printed on the
  card*, not the source's index for it. Where TCGdex disagrees, the pull
  overrides it. Today that is exactly one set: **Celebrations Classic
  Collection** (`cel25cc`), where TCGdex invents `CC001…CC025` and every one of
  the 25 cards actually carries its original set's number — Dark Gyarados is
  `8/82`, from Team Rocket.
- **What it cost to get this wrong.** The number is the join key for three
  independent things, so one bad field broke all of them and looked like three
  bugs. Autofill in the Transaction window resolved no tcgcsv product (so no
  price, and no product link — the item fell back to a junk `search_url`), the
  pokemontcg.io art fallback missed (so no preview, for all 25), and the row
  displayed `CC005/CC25`, a number on no card in existence. It also cost the
  daily catalog price sweep: `pricehistory.db` held **0 of 25** of this set
  against 24/25 for the parent Celebrations.
- **A regression, not a gap.** These cards priced correctly under the old
  pokemontcg.io catalog, which supplied the printed numerator. Two ledger items
  entered back then still carried `20/25` and `97/25` — right numerator, parent
  set's denominator — and were corrected to `20/111` and `97/146` alongside.
- **Scope was measured, not assumed.** Every English set in the catalog was
  compared against the pokemontcg.io dump, matching sets by normalized name and
  then the numbers behind shared card names. Exactly one set disagrees, 24 of 24
  shared names. That is why this is a 25-row table and not a framework.
- **A static table, not a live join.** Deriving it from tcgcsv group 2931 at
  ingest would be self-maintaining and exactly correct, but it puts a network
  round-trip and a fuzzy name join into the English catalog path to serve one
  set that has been frozen since 2021, and it fails offline. The table was
  authored *once* by that join and checked in.
- **Storing the full `8/82` rather than the bare `8`.** The numerator alone
  would restore pricing and art — that is what the old catalog effectively did —
  but the row then reads `8/25`, which is what the two stale ledger items look
  like. The full form is already the catalog's convention: all 27k Japanese rows
  store `046/081`. `CatalogCard.full_number` now short-circuits on a number that
  already carries a denominator, or the set total would append to make
  `8/82/25`.
- **Rejected: a name-only fallback in `pricing.py`** when a number matches no
  product in a resolved group. It would have masked this generically, which is
  the argument against it — `price_for_card` deliberately commits to "better no
  link than a wrong one", and a quiet guess is worse than a loud miss.
- **Still open, deliberately:** `Gardevoir ex` (CC014) remains art-less because
  pokemontcg.io names it `Gardevoir ex δ`. That is a *name*-normalization
  mismatch across the two sources, a different root cause from this one, and it
  affects a handful of cards catalog-wide. It gets its price and product link
  from this fix regardless, and `cardimages.art_for` covers its art from the
  TCGplayer product photo once a ledger item exists.

### 2026-08-11 — Catalog search is a trigram index that prunes, not a rewrite (ticket 47)

Autocomplete cost **14–35 ms of blocked UI repaint per keystroke**, at four call
sites, because `CardCatalogRepository.search()` matches every token with
`LIKE '%tok%'`. A leading wildcard cannot use an index, so 0013's indexes never
applied and each character scanned all 53,289 rows and sorted them
(`SCAN card_catalog` + `USE TEMP B-TREE FOR ORDER BY`) — the same 14 ms whether
it matched twenty-five rows or none. `display_number()` added an N+1 on top: one
`COUNT(DISTINCT number)` per match, 25–50 a popup, another 0–20 ms.

Migration 0020 adds an FTS5 **trigram** virtual table. Trigram is the specific
thing that makes a *substring* search indexable — ordinary FTS5 tokenizes on word
boundaries and could not serve `%tok%` at all. Measured after, on the real
catalog: **0.32–0.62 ms**, 30–70×.

- **The index prunes; the SQL predicate still decides.** `search()` keeps its
  original `WHERE` verbatim and merely restricts which rows it runs against, via
  `rowid IN (SELECT rowid FROM card_catalog_fts WHERE … MATCH ?)`. This was not
  caution — a direct translation is *wrong*, twice over. A collector number given
  in full prunes to nothing: `SV103/SV122` appears nowhere in the row numbered
  plain `SV103`, which the predicate accepts and the index cannot find, so only
  the head is safe to prune on. And a token under three characters matches
  **nothing at all, silently** — no error, zero rows — so `Pikachu Celebrations 5`
  would have lost every result. Both are pinned by tests; parity was verified on
  115 queries against the real 53k rows, including 100 names sampled from the
  catalog itself.
- **A rowid subquery, not a join.** `card_catalog` and its index share four
  column names, so a join would leave every unqualified column in the predicate
  ambiguous. The subquery needs no aliasing and leaves the predicate untouched.
- **External content (`content='card_catalog'`)**, so 53k rows are not stored
  twice — the index reads columns back from the table. The cost is that SQLite
  does not maintain it: the repository rebuilds inside `_insert_many`, which is
  every write. Explicit beats triggers here because a catalog refresh replaces
  all 53k rows at once and triggers would tax that row by row. The risk it takes
  on — a future writer bypassing the repo desyncs the index silently — is held by
  a `count(fts) == count(catalog)` assertion.
- **The minimum query length rises from 2 to 3.** A trigram is three characters,
  so a 2-char query would fall back to the very scan being removed — and it is
  the *first* query typed, the worst spike. Twenty-five arbitrary rows out of
  53k is not a useful list to pay 14 ms for. Considered and rejected: keeping a
  LIKE fallback for 2-char input, which preserves felt behaviour but keeps the
  worst spike to serve the least useful result.
- **Rejected: moving `search()` to a background worker.** The intuitive
  `UI-THREAD-BLOCKING` answer, and the wrong one — it buys a per-keystroke thread
  lifecycle (`WORKER-RETIRE-NOT-REPLACE`, and a worker needs its own connection)
  to hide 14 ms that can be deleted instead. Making the query fast removes the
  hazard class rather than managing it.
- **A 120 ms debounce as well**, because the query is no longer what costs: the
  model rebuild and the popup's width re-measure are. `market.py` also moved from
  `textChanged` to `textEdited` — a programmatic `setText` from `refresh()` was
  spending a full scan.
- **Three of the four call sites had forked `_on_name_edited`** (`CARD-ENTRY-
  STANDARD` says not to), which is how one slow query became four. The shared
  label-building and debounce now live in `_card_entry.py` and the forks call
  them; fully unwinding the forks onto `CardEntryMixin` remains open.
- **Tests were mutation-checked**, not just written: breaking the head-pruning,
  the trigram floor, the index rebuild, and the display-number batching each turns
  the suite red.

## 2026-08-24 — The collector number is the one printed on the card

Reported as "set 151 items don't render properly and won't autofill", plus a
Charizard promo that could not be found by its number. Two defects, compounding.
(Ticket 49.)

- **The trigram prune was narrower than the predicate it prunes for.** `search()`
  accepts both forms of a padded number — for `076` it emits
  `number = '076' OR number = '76'` — but `_fts_prune` then required the literal
  `"076"` in the index. English rows stored `74`, so the prune deleted the answer
  before the predicate ran. `_fts_core`'s own docstring states the rule this
  broke ("every row the SQL predicate could match for this token also contains
  this substring"); zero padding was simply never considered when 0020 landed.
  A padded numeric token now prunes on nothing and the other tokens carry the
  index, so `Charizard 006` is still a seek on `"Charizard"` — measured at
  0.53 ms/search over 53k rows, unchanged from ticket 47's gain.
- **Padding is a property of the set, not of the catalog.** `_tcgdex_number`
  normalised TCGdex's `localId` to a uniform `"1"`, which is right for exactly
  the eras that do not pad. Verified against the live API: Base Set, Celebrations
  and Sword & Shield really do give `1, 2, 3, 4`, while 151 and every SV-era set
  give `001, 002, 003, 004` — which is what those cards print and how tcgcsv
  names the products (`...-charizard-ex-006-165`). So the padding was destroyed
  only where it was real: 4,447 rows across 49 sets, Brilliant Stars onward. The
  strip is gone; `localId` is now kept verbatim.
- **This reverses a decision from 2026-08-03**, when the catalog moved to TCGdex
  and the strip was added "to match the historical `1` form". That preserved
  continuity with the pokemontcg.io dump rather than with the cards, and the
  cards are what the user is holding while they type.
- **Matching normalises on both sides instead.** The catalog now legitimately
  holds both conventions, so `repositories.number_forms` generates
  `{as-typed, stripped, padded-to-3}` and the predicate accepts any of them.
  Normalising the *query* alone was rejected: Japanese rows have always stored
  the printed `006/165`, so stripping the query breaks the JP half exactly as the
  prune broke the EN half. Three is the only width in use — TCGdex and tcgcsv
  both pad to three, never further.
- **Migration 0021 re-keys `item.card_number`, and nothing else.** `card_number`
  is in `_ITEM_IDENTITY_COLUMNS`, so leaving owned items behind would fork every
  affected card in two — the `6/165` bought last month and the `006/165` bought
  next month would stop being the same item. It reads the padding from `card_id`
  (whose suffix is `localId` verbatim) rather than from `card_catalog.number`,
  because at the moment it runs that column is still the stripped form it exists
  to undo — so it is correct whether or not the English catalog has been
  re-pulled yet. Measured on the real ledger: **60 of 978 items updated, zero
  identity collisions, and `lot`, `lot_consumption`, `transaction`,
  `transaction_line` and `audit_log` byte-identical before and after.**
- **Rejected: padding to three digits wherever the head is shorter.** It would
  corrupt Base Set `4/102` into `004/102` while fixing 151. 266 of 978 items have
  a one- or two-digit head and most of them are already correct. The migration
  joins the catalog so the padding comes from the set the card is actually in,
  and skips ambiguous set names rather than guessing.
- **Rejected: merging items that collide after padding.** None do, and moving
  lots between items is the money path — it belongs behind a service with an
  audit trail, not inside a `.sql` file.
- **The parity oracle in `tests/test_catalog.py` moved, deliberately and for the
  first time.** It exists to pin prune ≡ predicate, so it tracks a change in the
  predicate's *meaning* while still refusing to track its implementation. Both
  new search tests were checked to go red on revert.

## 2026-08-25 — Which printing a card is, is part of what card it is

Reported as items being grouped in Inventory that should not be: a regular
Mew ex 205/165 and a "151 Metal Card" Mew ex 205/165 shared one item, so editing
item-level Notes on one grid row changed all three copies. (Ticket 52.)

- **The app already offered the choice and discarded the answer.** tcgcsv lists
  both products for that number — the base at $32.35 and `(151 Metal Card)` at
  $25.57 — and `CardEntryMixin`'s Printing dropdown surfaces exactly that.
  `_apply_variant` applied `variant.price_cents` and `variant.tcgplayer_url` and
  dropped `variant.label`, because `Item` had no column to keep it in. With no
  such column the two printings agreed on all eleven identity columns and
  `find_by_identity` merged them.
- **The quiet damage was to price, not display.** `_match_or_create_item`
  refreshes `market_value_cents` on the item it matches, so acquiring a metal
  copy overwrote the base card's market value with the metal one's. Two assets,
  one price. That is why `variant` had to join `_ITEM_IDENTITY_COLUMNS` rather
  than being a display field.
- **`notes` stays out of identity, deliberately.** It is the obvious one-line
  fix and the wrong one: items would fragment on a typo or a trailing space, and
  `with_review_note` appends "Manual price changed detected." automatically — so
  marking a card for review would silently split it from its own other copies.
  The reporter reached for Notes because it was the only free-text field, which
  is the signal that a field was missing, not that Notes should mean more.
- **Nullable, un-backfilled, and free text.** Every existing row reads NULL, so
  the 1,071 existing items go on matching each other and every SKU already
  issued is byte-identical (`sku.make_base` only appends a segment when variant
  is set). Nothing in the data says which of the three Mew ex lots were metal —
  a migration that guessed would be worse than one that does nothing. Free text
  rather than an enum because the set of printings is vendor-defined and open;
  the picker fills it from tcgcsv's own label, and it stays editable for
  variants TCGplayer does not list.
- **Rejected: one item per acquisition.** It would destroy the point of lots —
  FIFO needs several lots under one item, and 944 open lots would become 944
  items, breaking every per-item aggregate in Dashboard, Inventory and
  Reporting. Also rejected: overloading `holo_pattern`, a closed NH/H/RH enum
  about foiling that the SKU and editor already key on.
- **`split_lot_to_new_item` repairs what is already merged**, because only the
  owner knows which copy is which. It moves one lot to its own item inside one
  transaction with an `audit_log` row, keeping basis, quantity, intent and
  acquisition date, and moves the acquiring `transaction_line` with it — the
  line references the item, so leaving it behind would make History and
  per-item realized P/L disagree with the lot. Separating a second copy reuses
  the item the first one made rather than minting a third.
- **The split refuses a partially sold lot.** Its out-lines and
  `lot_consumption` rows reference the item being left, and re-pointing those is
  the money path; it wants its own design and its own invariant proof. Same
  restriction `delete_inventory_entry` already accepts.
- **The Inventory grid shows Variant read-only.** Every other identity column
  there (name, set, number, language, condition) is inline-editable and edits
  the *shared* item, which is the pre-existing hazard this report ran into. A
  row is a lot; changing one copy's printing is what the row's "Separate this
  copy" action is for.
- **Mutation-checked:** removing `variant` from the identity tuple turns seven
  tests red, and removing the unsold-lot guard turns its negative test red.

### 2026-08-26 — Catalog artist, and why the illustrator index needs longest-wins

- **Why:** the planned **Collections** window derives template membership from
  card attributes, and "every card illustrated by X" is the template most wanted.
  An audit of the live catalog (53,289 rows) found the attribute layer thinner
  than assumed, and split by language: EN had `printed_total` for 23,444/23,444
  but **rarity for 0**; JP had rarity for 19,718/29,845 but **`printed_total` for
  0**; `supertype` was populated for **none of the 53,289** — a dead column since
  0008; and artist had no column at all. So the two obvious template axes were
  mutually exclusive by language, and the wanted one did not exist.
- **Cause of the EN gaps:** `update_catalog` reads each set's TCGdex
  `/sets/{id}` detail, whose `cards` array is a *card brief* — `id`, `localId`,
  `name`, `image`. It therefore hardcoded `rarity=None, supertype=None`, as the
  ADR of 2026-07-14 records. Nothing was wrong; the data was simply never there.
- **Decision: invert TCGdex's own indexes rather than fetch per card.**
  `/illustrators` (410 names), `/rarities` (40) and `/categories` (3) list their
  values, and `/cards?illustrator=…` lists the cards carrying one. That is **453
  requests against 23,444** — measured at **81s versus roughly 80 minutes** — and
  it fills all three columns in one pass: **artist 22,639/23,444 (96.6%), 386
  distinct artists; rarity 23,324 (99.5%); supertype 23,324 (99.5%)**.
- **The trap, and the rule that fixes it.** `?illustrator=` is
  **substring**-matched, not exact: `?illustrator=Uta` returns 493 cards, the
  first drawn by *Saya Tsuruta*. **2,935 of the 23,444 English cards (13%) are
  claimed by more than one illustrator name**, so keeping whichever arrived last
  attributes them by the order TCGdex happened to list its illustrators in —
  wrong on an eighth of the catalog, and silently. A first dry run looked clean
  only because the true names happened to sort after their false positives.

  The filter returns name `N` for a card with true value `A` exactly when `N` is
  a substring of `A`. `A` always claims itself, and every false positive is
  therefore a *proper* substring of it — so **the true value is always the
  longest claimant**. Verified against the per-card endpoint on 25 multi-claim
  cards: **25/25 correct, 0 containment violations**. This is why
  `_resolve_longest` is a named function with four tests rather than a `dict`
  assignment inside the loop; reverting it to last-wins turns four red,
  including the end-to-end pull.
- **Enrichment happens inside the pull, before the write.**
  `replace_for_language` deletes the language's rows and re-inserts, so a
  post-hoc `UPDATE card_catalog SET artist = …` would be erased by the next
  "Update card database". Populating the `CatalogCard` dataclasses before
  `replace_for_language` makes the columns correct by construction and removes
  the drift entirely. It also keeps one logical fact behind one trigger path,
  which is what `services/operations.py` exists to enforce.
- **Failure is isolated three ways**, because this is best-effort enrichment
  layered on a catalog that is already correct without it: a dead *index* drops
  only its own axis, a dead *query* drops only that one value, and a wholly dead
  sweep still writes a complete catalog with `artist IS NULL`. `summary.artists`
  reports how many resolved, so a pull that silently enriched nothing says so.
- **English only.** JP rows come from tcgcsv, whose `card_id` is a TCGplayer
  product id (`jp-694993`); TCGdex — the only source that knows the illustrator —
  keys on `base1-1`. There is no join short of fuzzy name+set+number matching,
  which needs its own accuracy budget. An artist template must say so rather than
  appear empty for half the catalog.
- **Rejected: per-card TCGdex fetch (23,444 requests).** The complete answer, and
  the *only* route to `variants` (holo / reverse / first edition, which
  `?variants=` does not filter on — it returns 0). But ~80 minutes for the last
  3.4% of artist coverage. Revisit only if a holo template is actually wanted,
  and price it then.
- **Rejected: the pokemontcg.io GitHub dump for artist.** One request per set and
  it carries rarity/supertype too, but artist coverage collapses at the leading
  edge — **sv9: 0/190**, against 102/102 for base1 and 256/258 for sv1. Reverting
  the EN source for a partly-populated column would trade the 2026-08-04 decision
  for a worse one.
- **Rejected: adding `artist` to `card_catalog_fts`.** 0020's table is
  external-content and trigram-tokenized for substring autocomplete; Collections
  filters artist by equality, which wants a b-tree. Adding it would mean dropping
  and rebuilding the virtual table for no gain. `idx_card_catalog_artist
  (artist, language)` serves the query instead.
- **Consequence:** "Update card database (EN)" is about **80 seconds slower**.
  `on_progress` now spans both phases over one total (sets + planned index
  values), so the bar does not reach 100% and then sit through the sweep. Note
  that `_CatalogUpdateWorker` does not currently pass `on_progress` at all, so the
  tile is indeterminate either way — the contract is correct for when it does.
- **Un-backfilled, deliberately.** Every existing row reads NULL until the next
  EN pull, and `distinct_artists` excludes NULL rather than offering an "unknown
  artist" bucket: a NULL means "not re-pulled yet" or "Japanese row", neither of
  which is a collecting project.
- **Four of TCGdex's 410 illustrator names are malformed upstream**, and the
  sweep reproduces them faithfully rather than guessing:
  `'"Big Mama" Tagawa'`, `'"Big Mama" Tagawa"'` (stray quote),
  `'"Big Mama" Tagawa"Big Mama" Tagawa'` (doubled) and
  `'Sumiyoshi Kizuki"Big Mama" Tagawa'` (two artists concatenated, no
  separator). Confirmed **not** an artefact of the inversion: the per-card
  endpoint returns exactly these strings — `pop2-9`'s illustrator really is the
  doubled form, `ecard1-119`'s really is the concatenated pair. Longest-wins is
  therefore behaving correctly; the source is wrong. It affects four cards, so
  normalising was rejected as out of proportion and, for the concatenated pair,
  as guesswork: splitting it needs a rule that knows `"Big Mama" Tagawa` is one
  name. **Consequence for Collections:** leading quotes sort first, so an artist
  picker over `distinct_artists` opens on these four. The picker should collapse
  or demote them; the catalog should keep saying what the source said.
- Ticket 54. Verified by a real pull into a throwaway DB: 218 sets, 23,546 cards,
  **22,827 artists (96.9%), rarity and supertype 100%, zero errors, 174s**, and
  `base1-4` / `sv09-001` / `swsh9-018` each matching independently-checked ground
  truth.

### 2026-08-27 — Auction History: a snapshot log, not a soft-delete flag (ticket 58)

The History window gained a third page beside Transaction History and
Recognition History: **Auction History**, the lifecycle log of the eBay
watchlist — an auction deleted, archived, unarchived, or **won**. A deleted
auction is restorable from it.

- **The log lives in `auctions.db`, created in-module.** `auction_event` went
  into `_SCHEMA` alongside `auction` and `auction_card`, not into
  `data/migrations/`. Only `inventory.db` is migrated by `data/migrator.py`
  (`LEDGER-VS-DERIVED`); `archived` and the thirteen `_parity_cols` both landed
  the same way, through `_SCHEMA` + `_ensure_columns`, which every
  `open_auctions()` re-runs idempotently.
- **A delete stays a real `DELETE`; the payload is snapshotted first.** The
  rejected alternative was a `deleted` flag symmetric with `archived`. It reads
  cleaner and keeps ids stable, but it makes every existing read of `auction`
  grow a `deleted = 0` filter — `list_all` already branches twice, plus the KPI
  tile counts — and it means `auctions.db`, documented as ephemeral with
  *"nothing prunes it"*, would never shrink. That is the opposite of what the
  file is for. `AuctionRepository.delete` therefore serialises the auction and
  all 24 card columns to JSON **inside the same transaction as the `DELETE`**,
  since `ON DELETE CASCADE` takes `auction_card` with the parent and the payload
  is unrecoverable one statement later.
- **The cost of that choice is id stability, and it is paid explicitly.** SQLite
  hands a freed rowid straight back out, so by the time an undo runs the deleted
  auction's id may belong to someone else — `test_restore_does_not_collide_with_a_reused_id`
  demonstrates exactly that sequence. `restore` therefore mints a **fresh** row
  and records it on the event as `restored_id`, rather than pinning the old id
  and colliding.
- **The event row is kept and stamped, never removed.** `restored_at` /
  `restored_id` mark it consumed. That matches History's standing rule that
  nothing leaves the log (voided transactions stay visible with a pill), and it
  doubles as the one-shot guard that stops a second restore minting a duplicate
  watchlist entry.
- **"Won" is export-then-post, never export alone.** Export was fire-and-forget:
  `(title, cards)` with the auction id dropped on the floor, so "this auction was
  won and its cards are in inventory" was not a fact the app held. The payload
  now carries the id, `TransactionView` parks it in `_source_auction_id` as inert
  context — it never reaches `build_request` or the posting algorithm, so
  `UNIFIED-POSTING` still infers type purely from which sides carry cards — and
  emits `posted_from_auction` **only** on a successful non-edit post. An
  abandoned export records nothing; an edit (void + re-post of an existing
  transaction) is not winning anything. `clear()` resets the tag, so it cannot
  leak into the next transaction typed by hand. Both guards were verified by
  mutation: removing either turns one event into two.
- **`won` archives the auction and logs one row, not two.** Archiving is already
  reversible in place with a visible *Unarchive* control, and a won auction is
  done. One user action, one history row.
- **`transaction_id` crosses databases and is a display pointer, not a foreign
  key.** It points into `inventory.db` from `auctions.db`. The pane renders it
  verbatim and never resolves it — the ledger may have voided that transaction
  since, or been restored from a backup where the id means something else.
  Restoring an auction re-creates a *watchlist entry only*: never a lot, never a
  transaction.
- **`HistoryView`'s `auctions_conn` is passed in, never opened.** MainWindow now
  opens `auctions.db` once, above the `builders` tuple, and hands the same
  connection to both readers — History is built before eBay Auctions, and two
  connections would be two WAL writers on one file. The kwarg defaults to `None`
  rather than lazily calling `open_auctions()`: five existing test files
  construct a bare `HistoryView(conn)`, and a lazy default would have the suite
  writing to the user's real watchlist. `None` means an empty pane and no
  database opened.
- **`auction_event` is not pruned**, inheriting the file's "nothing prunes it"
  contract; the snapshots are small. If growth ever matters, an Operations tile
  is the home for it, not an automatic sweep.
- Ticket 58. Verified offscreen: 16 new pane tests plus 11 repository tests, the
  full suite green, and the two correlation guards proven red under mutation.
### 2026-08-26 — Price the printing the card actually is, not the dearest one

- **Bug:** `TcgcsvProvider._prices_for_group` collapsed a product's foil
  treatments with `max()` — one line, commented "keep the highest market" — so
  the app recorded the dearest printing as *the* price for the card. A plain
  Normal copy was valued at its Reverse Holofoil. Worst in the catalog: a
  Shadowless Base Set Charizard in Unlimited Holo ($2,146.38) recorded at the
  1st Edition Holo's **$10,000.00**; Legendary Collection Charmander, Normal
  $2.24, recorded at **$616.33** — 275x. 786 products carry a gap >= $50.
- **The data was already being downloaded.** tcgcsv mirrors TCGplayer's own
  model: one price row per product **per `subTypeName`**. Measured across all 218
  English groups, zero fetch errors — exactly **seven** values (Normal 19,241 ·
  Reverse Holofoil 13,602 · Holofoil 10,463 · Unlimited 761 · 1st Edition 760 ·
  Unlimited Holofoil 179 · 1st Edition Holofoil 174) and **13,900 of 30,457
  products (45.6%) carry more than one**. The collapse was throwing it away.
- **Nothing new needed modelling.** `_ITEM_IDENTITY_COLUMNS` already contains
  `holo_pattern` **and** `edition` (migration 0011), so a holo copy and a
  non-holo copy have always been separate items with separate basis. All seven
  treatments decompose exactly onto those two enums. The lookup simply had to use
  what the item already said it was.
- **Decision:** `_prices_for_group` keeps every treatment
  (`{product_id: {subtype: cents}}`) and `select_treatment()` picks one, in four
  recorded steps: **exact** (the item's printing is priced), **only** (the
  product has one printing, which the item's claim cannot override), **cheapest**
  (several exist, the item does not say), **fallback** (the item names one this
  product lacks). `PriceQuote` carries `treatment` and `picked` so the choice is
  inspectable rather than buried.
- **Cheapest, not dearest, when unknown.** Overstating a portfolio inflates
  unrealized gain and the Collection Value tile — the more damaging direction to
  be wrong in — and Normal outnumbers the foils anyway.
- **Rule 4 was wrong on the first attempt, and the real ledger caught it.** It
  originally *refused* to price a card whose declared printing the product did
  not carry, reasoning that a missing price beats a confident wrong one. Run
  against a copy of the live database that **dropped 53 items whose product match
  was perfectly good**: vintage cards exist only as `1st Edition`/`Unlimited`
  with no plain `Normal`, and holo-only cards are routinely recorded `NH` because
  `NH` is the default — the claim carries almost no information when 93% of items
  make it. The card is still one of the printings offered, so the cheapest of
  them is a real answer; refusing merely deleted a price the user had. Now it
  falls back and marks the quote `fallback`, which is also the signal ticket 57
  will use to decide what to ask about.
- **Measured, old algorithm vs new, over identical fetched data** (a copy of the
  real ledger, 1,101 un-graded items, 968 resolving to a priced product):
  **968 priced before, 968 after — nothing lost** — and **108 of 968 prices
  corrected, removing $5,476.18 of overstatement.** Picks: 96 exact, 818 only,
  53 fallback, 1 cheapest. Largest single correction: Dark Slowbro 8/110 from
  $329.99 to $44.77.
- **The catalog-wide sweep is deliberately unchanged.** `market_prices()` still
  collapses to the dearest, now via an explicit `_collapse_dearest()` naming this
  ticket. That sweep prices *catalog cards*, which have no owner and therefore no
  treatment to select by, and its output is a year of `pricehistory.db` series
  behind the Inventory sparkline, `services/trends.py` and Market signals.
  Changing it would silently redefine every existing series. Whether it should
  prefer a base printing instead is a separate decision, deliberately left open.
- **Card-entry autofill follows the same rule.** `price_for_card` and
  `variants_for_card` run before an item exists, so they use the shared
  `cheapest_treatment()` rather than the dearest — otherwise the bug survived at
  the point of entry, where `market_value_cents` is first written.
- **Rejected: a treatment column on `price_snapshot`.** Records the same wrong
  number with better provenance; the bug is the selection, not the storage.
- **Rejected: duplicating cards under a foil alias** (`Geodude 111/102 Holo` as
  its own card), which was the original proposal. `holo_pattern` already makes
  them separate items, so an alias would be a second mechanism for one fact, able
  to disagree with the first — the hazard migration 0022's ADR documents. It also
  cannot be backfilled and would not survive `replace_for_language`.
- **No migration.** `price_snapshot` keeps one row per item; the item already
  encodes its treatment. Ticket 56.

### 2026-08-26 — Which foil printings a card comes in, as catalog reference data

- **Why:** the Collections window needs "every reverse holo in this set" to be an
  enumerable checklist, and nothing in the catalog said which treatments a card
  exists in. `card_catalog` carried name/set/number/rarity/supertype/artist and
  no foil.
- **Not a column — a child table.** 45.6% of TCGplayer products carry more than
  one treatment (measured: 13,900 of 30,457), so a single column could record
  only one of them and would lose the question being asked. On the real catalog,
  **68.3% of resolved cards have more than one**.
- **Source: tcgcsv, in files the app already downloads.** Its price rows carry a
  `subTypeName` per product. TCGdex knows the same thing as
  `variants: {normal, holo, reverse, firstEdition, wPromo}`, but only on its
  **per-card** endpoint — `?variants=` filters nothing — so that route is 23,546
  requests. Reading it off the price files costs group resolution and a cached
  fetch per set instead. Measured on the live catalog: **37 seconds**.
- **Measured coverage: 22,274 of 23,444 English cards (95.0%)**, 37,528
  (card, treatment) pairs, one set unresolved (`fut2020`, which has no tcgcsv
  group). Vocabulary found matches the sweep of all 218 groups exactly: Reverse
  Holofoil 14,243 · Normal 13,127 · Holofoil 8,327 · Unlimited 739 · 1st Edition
  738 · Unlimited Holofoil 182 · 1st Edition Holofoil 172.
- **Correctness signal worth recording: Base Set resolves to 0 reverse holos**,
  15 holo and 86 normal. Reverse holos did not exist until Legendary Collection
  in 2002, so a Base Set with reverse holos would have meant the join was wrong.
  Journey Together, by contrast, gives 143 reverse holo / 63 holo / 127 normal.
- **Stored as tcgcsv's raw string**, decomposed on read via
  `services/pricing._TREATMENT`. The vendor vocabulary is the source of truth and
  is open-ended in principle; a closed enum would be wrong within a release —
  the same reasoning migration 0022 applied to `variant`.
- **No foreign key, and rewritten in the same transaction as the cards.**
  `CardCatalogRepository.replace_for_language` DELETEs and re-inserts a whole
  language on every pull, so an FK would either block that or cascade the
  treatments away mid-rebuild. Writing both inside one transaction is what keeps
  them consistent; a test asserts zero orphaned rows after two consecutive pulls,
  and the real sweep confirmed **0 orphans** over 37,528 rows.
- **`language` is denormalised onto the table** so a pull can clear exactly its
  own rows without joining back to a catalog that is mid-delete.
- **Failure is isolated**, matching ticket 54's shape: a set that will not
  resolve is recorded and skipped, a set whose fetch explodes costs only itself,
  and a wholly dead sweep still writes a complete catalog with no treatments.
  `CatalogUpdateSummary.treatments` reports how many cards were resolved, so a
  pull that silently found none says so.
- **`TcgcsvProvider.treatments_by_product()`** exposes the per-treatment data
  ticket 56 started keeping — the sweep needs to know a card comes in Reverse
  Holofoil without caring what it costs.
- **Rejected: deriving treatments from the items the user owns.** It can only
  describe what is already held, so it could never populate an unowned slot,
  which is the entire point of a collection checklist.
- **Rejected: JP coverage for now.** JP `card_id` is already a tcgcsv product id
  so the join is cheaper there, but JP price files use different group ids and
  the subtype vocabulary is unverified. English first, and a foil template must
  say so rather than appearing empty for half the catalog.
- Migration 0024, applied to a copy of the live ledger: schema 23 → 24, all
  53,289 catalog rows preserved, `item`/`lot` untouched at 1,115/1,371, and the
  new table empty until the next pull. Ticket 55.

### 2026-08-26 — Card entry offers only the printings a card actually comes in

- **Why:** ticket 56 made pricing respect an item's `holo_pattern`, which is
  only worth as much as that column is accurate — and it is not. The real ledger
  carries **1,040 NH / 20 H / 10 RH / 45 unset**, about **93% on the default**,
  against a catalog where **45.6% of products have more than one printing**. That
  distribution cannot be true; it is the shape of a field nobody was asked to
  fill in, the same "the default is anti-correlated with truth" pattern already
  found for the condition heuristic.
- **There was no new widget to build.** Both card-entry screens already had
  `self.holo = _combo(HoloPattern)` listing all three foils for every card and
  defaulting to NON_HOLO. The fix is to make that combo *truthful*: offer only
  the printings tcgcsv says the product comes in, and let the choice drive the
  price.
- **Treatments belong on `CardVariant`, not on the card.** Foil is a property of
  the *product*: a stamped variant can offer a different set than the base print.
  So `variants_for_card` now returns `treatments: dict[str, int]` — the printings
  **and their individual prices** — and re-narrowing happens whenever the
  Printing dropdown changes. This closes ticket 56's loop at the point of entry:
  picking Reverse Holofoil applies the reverse holo's price, not the cheapest.
- **This does not read `card_catalog_treatment` (ticket 55), deliberately.**
  Entry resolves treatments live from the price file it is already fetching, so
  the picker works with no catalog re-pull. Ticket 55's table answers a different
  question — *which cards exist in a printing*, including ones the user does not
  own — which a live per-card lookup cannot, because a checklist must enumerate
  absences. Same vocabulary, two directions.
- **It lives in `CardEntryMixin`, not in either row.** `CARD-ENTRY-STANDARD`
  forbids forking the shared component, and Transaction and Auctions both build
  the same combo. `apply_treatment_choice` prefers the host's own
  `set_market_cents` where one exists, because Auctions tracks whether a price
  was user-edited and writing its spin box directly would register our change as
  a manual override — silently disabling future auto-pricing for that row.
- **An unresolved product leaves the combo unconstrained.** An empty treatment
  set means "could not tell", **not** "only one printing". Narrowing on a guess
  would be worse than the default it replaces, so the full list comes back.
- **`edition` is set alongside `holo_pattern`.** Both are identity columns;
  recording only the foil would make a 1st-edition and an unlimited copy the same
  item. `TREATMENT_IDENTITY` is derived from `_TREATMENT` rather than written
  twice, and a test round-trips every entry so the two cannot drift.
- **Bug caught by an existing test while building this:** filtering treatments to
  named ones *before* choosing a price dropped any tcgcsv row with no
  `subTypeName`, which cost the card its price entirely. A row without a
  treatment label still prices the card; it just cannot say which printing it is.
  Price selection now runs over all rows and only the *offer* is filtered.
- **Rejected: making the treatment mandatory before submit.** The ticket proposed
  blocking submission until an ambiguous row is resolved. Rejected on measurement:
  narrowing already removes the wrong options, so the first entry in a narrowed
  combo is a real printing rather than a default — a hard block would add friction
  to every row while the remaining error case (two genuine printings, user picks
  the first without looking) is not one a modal can fix.
- **Split out, not dropped:** correcting the **existing** 1,040 defaults. Changing
  `holo_pattern` on a saved item re-identifies it — it is in
  `_ITEM_IDENTITY_COLUMNS`, so a correction can collide with another item and
  must move lots the way `split_lot_to_new_item` does, with an audit row. That is
  a money-path operation deserving its own ticket rather than a tail on a UI one.
  Ticket 57 stops the bleeding; the backfill is filed separately.
- Suite 1,071 passing. Reverting the narrowing turns **eight** tests red across
  both hosts. Ticket 57.

### 2026-08-27 — The Inventory row editor is one dialog, not two (ticket 61)

Double-clicking an Inventory row opened `ItemDetailDialog`, and changing anything
meant clicking **Edit item…** inside it to open a second dialog,
`ItemEditorDialog`. Both are now one themed editor, and `item_detail.py` is gone.

- **It was never a missing-stylesheet problem.** `theme._stylesheet` already
  covered `QDialog` / `QPushButton` / `QLineEdit` / `QComboBox` / `QTableWidget`,
  so both dialogs *did* render dark. Rendered offscreen, they were a Windows
  property sheet in dark paint: no header band, section headers as bare
  `QLabel("<b>Lots</b>")`, the position line a `·`-joined run of text with
  hardcoded `"   ·   "` spacing, no sign tint on Unrealized P/L,
  `QHeaderView.Stretch` giving SKU the same width as Source, and a flat 20-row
  `QFormLayout` 441px wide by 778px tall. Neither file imported `theme` or
  `icons` at all. The editor's `QDialogButtonBox` was why **Save never got
  `objectName="primary"`** and so never picked up the accent button style.
- **The read-only half was redundant.** `_build_dossier` in `inventory.py`
  already shows the selected card's art, name, subline, SKU, Acquired, Cost
  basis, Market, Unrealized, Return and Sold-for plus a price chart — themed,
  and three inches to the left of the dialog. A modal whose unique content was
  the Lots/History tables did not earn a dialog; those tables became a read-only
  **Ledger** section of the editor instead.
- **`ItemEditorDialog` is the surviving name**, taking
  `(conn, item_id, lot_id=None, parent=None)`. It is the accurate name for what
  the thing is, and three of the five affected tests kept importing the same
  symbol. `lot_id=None` hides the per-copy section, which is how it stays
  constructible from a bare item.
- **The substantive gap was lot fields, not paint.** A grid row is a *lot*;
  `_open_detail` passed `row.item.id` and dropped `row.lot_id`. So `intent`,
  `acquired_market_value_cents`, `missing`, `source`, `notes`, `acquired_at` and
  `sku` were absent from the dialog — and **`Lot.source` and `Lot.notes` were
  editable nowhere in the UI**. They now live in a **This copy** section.
- **`missing` goes through `InventoryService.set_lot_missing`, not the
  repository.** It is a reversible impairment that moves money, so it writes an
  append-only `audit_log` row (Invariant 9). Proven by mutation: replacing it
  with `lot.missing = …; self._lots.update(lot)` flips the flag and the audit
  count assertion fails `1 > 1`.
- **`SKU` and `Acquired` are shown but not editable.** SKUs are minted at
  acquisition and are stable lookup keys; `acquired_at` is what FIFO orders
  disposals by. Editable identity keys are a riskier ticket than a restyle.
- **The lot/item split is now stated in the UI rather than enforced by
  omission.** When a card has more than one copy, the Identity / Pricing / Filing
  captions read *"applies to all N copies of this card"*, and Variant's hint
  points at **Separate this copy…**. This is the visible form of the rule in
  `inventory_model.py` that keeps Variant out of inline editing (ticket 52) —
  a dialog opened from a lot row that edits item fields walks straight back
  toward that surprise unless it says so.
- **The editor applies on Save, including review.** The old detail dialog wrote
  the review flag the instant its checkbox was ticked — the one field in the app
  that committed without a Save, and a second write path beside the editor's own
  `item.review` + note sync. There is now exactly one. The grid's inline review
  button is unaffected and stays immediate.
- **`without_review_note` learned about newlines** (`services/review.py`). It was
  `" ".join(notes.split())`, harmless while every notes editor was a single-line
  `QLineEdit`. The editor's notes field is a `QPlainTextEdit`, so toggling review
  on a card would have silently flattened the user's paragraphs into one line.
  Within a line whitespace still collapses, so single-line input round-trips
  exactly as before. Proven by mutation: restoring the flat join fails both the
  service test and the editor's round-trip test, printing the two lines joined.
- **`QCheckBox` got a theme rule** — a global `theme.py` change, deliberately
  beyond the dialog. Unstyled, Qt's indicator is all but invisible on this
  palette: an empty box the same colour as the panel behind it, which was a large
  part of the stock-Windows read. Checked is an accent fill rather than a glyph,
  so no image asset and no `ICONS-LUCIDE` exception is needed. It affects every
  checkbox in the app.
- **Three layout bugs found by rendering, not by tests.** The caption `QWidget`
  had a default expanding vertical policy, so in any panel stretched taller than
  its content the title floated into the middle of its own section. Side-by-side
  panels sharing a grid row's height pushed their fields apart instead of parking
  the slack at the bottom. And the Ledger tables kept Qt's Expanding policy, so
  a one-lot card got a tall empty table — the same void the old dialog had, for
  the same reason.
- Ticket 61. Verified: 1,109 tests green offscreen (11 new), ruff clean, both
  dialogs rendered before and the editor after, and the two guards that matter —
  the audit row and the notes line breaks — proven red under mutation.
### 2026-08-27 — Personal Collection: ownership is membership

- **Why:** Inventory is one row per *lot* and can only draw what is held. A
  collecting project needs the opposite — a slot for every card in the set,
  including the ones not owned — so membership comes from `card_catalog` and
  ownership is painted on top. New twelfth nav destination.
- **Named "Personal Collection", not "Collections".** The Dashboard's
  **Collection Value** tile and `collection_value_snapshot` (migration 0015)
  already use "collection" for the whole portfolio; that tile is untouched. The
  `collection`/`grid` aliases also moved off Inventory in
  `.claude/context/app-map.json` — they were what made `/task-planning` resolve
  "collection grid" to Inventory and hand out the wrong screen's gotchas.
- **Three states, and no membership table.** Settled with the user:

      filled      owned, lot intent 'personal'
      importable  owned as 'resale' — matches a slot, not brought in
      missing     not owned

  Intent **is** the membership signal, so a template collection stores no
  membership rows at all. Only `custom` uses `collection_card`. A second source
  of truth would need syncing on every import, sale and void; this one cannot
  disagree with itself.
- **Importing splits the lot.** Bringing a card in sets its lot to `personal`,
  and a lot of quantity > 1 is **split** so the rest stay stock — "keep one of
  these three for myself". `split_lot_to_new_item` could not do this: it moves a
  whole lot onto a *different item* and keeps quantity and intent. New
  `InventoryService.split_lot_for_intent`.
- **Migration 0016 already established the shape** that makes the split safe: one
  acquisition line legitimately carries several lots **sharing one SKU**, because
  a purchase whose cost does not divide evenly splits into `(qty - r)` and `r`
  lots. So the new lot keeps `unit_cost_cents`, `acquired_market_value_cents`,
  the acquisition line, SKU and date. **No money is re-divided**, so unlike a
  cost allocation there is no residual to place: `2 × unit + 1 × unit == 3 × unit`
  exactly. Restricted to a fully unsold lot, as `split_lot_to_new_item` is —
  a partially sold lot has `lot_consumption` rows pointing at the quantity it
  would be leaving.
- **Safe because intent is a filter, not a valuation input** — verified in
  `services/reporting.py`, where `realized_*`, `cost_basis_cents` and
  `collection_value_cents` all take intent as an *optional narrowing* and only
  `ripping` changes a number. Tests assert the totals are byte-identical across
  an import and that only the per-intent slices move.
- **Closed an audit gap on the way.** `ui/views/inventory.py` changed lot intent
  by calling `LotRepository.update()` straight from the view, writing **no**
  `audit_log` row — while `set_lot_missing` audits for the same reason. Both the
  grid edit and the collection import now go through
  `InventoryService.set_lot_intent`, which audits.
- **Removal does not revert intent.** Dropping a card from a custom collection is
  a membership change; reverting would push it back onto the resale side as a
  side effect of tidying a checklist. Deleting a collection is a hard delete and
  `NO-HARD-DELETE` does not reach it: a collection is a *view the user arranged*,
  not a ledger event, and no holding changes.
- **`collection_card.treatment` is `NOT NULL DEFAULT ''`, not nullable.** SQLite
  treats NULLs as **distinct** in a UNIQUE/PRIMARY KEY index, so a nullable
  column would have let `(1, 'base1-4', NULL)` be inserted twice and the primary
  key would not have stopped a duplicate slot.
- **`selector` is one free-text column** rather than one per template kind, whose
  meaning the registry owns. Five nullable columns with a
  four-of-five-must-be-NULL rule is a constraint nothing could enforce.
- **Two bugs the offscreen screenshot caught that no test did**, which is why the
  render was worth doing:
  1. **`blockSignals` does not stop `currentRowChanged`.** It is driven by the
     list's *selection model*, so moving `currentRow` off its initial `-1`
     emitted a change for row 0 even with the widget's own signals blocked — the
     navigator highlighted the collection clicked while the page rendered the
     first one. Replaced with an explicit `_syncing_nav` guard, and the nav row
     is moved *before* the render so the last render always wins.
  2. **Slots sorted lexicographically** — a 102-card set ran 1, 10, 100, 101,
     102, 11, 12 …, useless as a checklist. `ORDER BY number` cannot fix it;
     `collector_key()` sorts on (letter head, number, letter tail) so "TG01",
     "006" and "195a" all order correctly.
- **A third, caught by reading the render:** the fact strip showed
  "Slots filled 0" beside "In collection $1,113.02", because value counted
  *owned* slots including importable ones — a figure contradicting its own label.
  `Progress.value_cents` is now filled-only, with `importable_value_cents`
  reported separately.
- **Honest gaps, surfaced in the UI rather than hidden.** `item` has no
  `card_id`, so slots match holdings by printed identity and **124 owned cards
  resolve to no slot** on the real ledger — the checklist says so. Master mode
  needs `card_catalog_treatment`, which is empty until the catalog is re-pulled,
  so every slot reads *unknown* and the total is described as a floor rather than
  a count. Artist, rarity and card-type templates offer nothing until that pull
  happens, and the creation dialog says which Operations job to run.
- **Measured on a copy of the real ledger:** migration 24 → 25 with `item`,
  `lot`, `card_catalog`, `transaction_line` and `audit_log` counts identical.
  Base Set 102 slots / 9 importable, Journey Together 190 / 15, Trainer Gallery
  120 / 31, at **3–8 ms** per collection. Suite 1,167 passing. Ticket 60.

### 2026-08-27 — "N/A" and NULL are the same edition (ticket 62)

- **Bug:** master-mode Collections slots recognised **no owned card at all** —
  Base Set 9 importable by number but **0** in master mode, Journey Together 15
  vs 0, an Arita run 26 vs 0. A master-set collection was unusable.
- **Cause:** `_apply_ownership` compared
  `(r["edition"] or None) == (want[1] or None)`. `TREATMENT_IDENTITY` yields
  `None` for the plain printings, because the reverse map skips the `"N/A"`
  spellings — but `Edition.NA.value` **is** the string `"N/A"`, and that is what
  **1,055 of 1,115 items** store. `"N/A" == None` is false, so 95% of real items
  were rejected from every plain-printing slot. Two spellings of one fact
  ("this card has no edition distinction"), compared as if they differed.
- **Found by running the real catalog update, not by a test.** Ticket 60's
  master-mode tests used `edition=None` or a real edition; neither used the value
  95% of rows actually carry. The fixtures were self-consistent and the
  production data was not.
- **Decision:** `_same_edition()` normalises both sides — `None`, `""` and
  `"N/A"` are one value, anything else compares exactly.
- **Rejected: making `TREATMENT_IDENTITY` yield `"N/A"`.** It moves the mismatch:
  the same map is used by card entry (ticket 57) to *set* the edition combo,
  where `None` is the right fallback.
- **Rejected: backfilling `item.edition` to NULL.** `edition` is in
  `_ITEM_IDENTITY_COLUMNS`, so rewriting 1,055 rows to fix a comparison would
  re-identify every one of them.
- **Master mode still reports fewer than by-number, and that is correct.**
  By-number asks "do you own this card"; master asks "do you own *this
  printing*". A Base Set holo recorded `NH` cannot fill a Holofoil slot, and
  `holo_pattern` is ~93% the default. That is honest reporting of unknown foil
  data — the thing ticket 57's entry picker fixes going forward — not a matching
  failure. Base Set master now finds 3 of 9, an Arita run 6 of 26.
- Suite 1,182 passing; reverting the comparison turns the new test red.

### 2026-08-28 — The collection lag was one query, not the widgets (ticket 64)

- **Reported as** "a large amount of lag clicking into a collection for the first
  time", with a request to pre-populate on creation instead. Measured first, and
  the lag was somewhere else entirely:

  | step | Base Set (102 slots) | Sugimori master (2,072) |
  |---|---|---|
  | `slots_for()` | 28 ms | 15 ms |
  | building the tiles | 6 ms | 46 ms |
  | layout + paint | 179 ms | 1,526 ms |
  | **`unmatched_items()`** | **11,300 ms** | **11,300 ms** |

- **Cause:** `unmatched_items()` planned as `SCAN i` + a *correlated* `SCAN c` —
  1,115 items x 53,289 catalog rows is 59.4 million comparisons, and
  `LOWER(TRIM(...))` on both sides means no index can serve any of them. The
  window called it on **every** click into a collection.
- **It is also portfolio-wide**, not per-collection: the same 121 for every
  project. Rewritten as a two-pass set difference (build the catalog's key set
  once, probe it) it is **27 ms for the identical answer**, and it moved to the
  grid page's fact strip, computed once per refresh.
- **Pre-populating on creation was therefore rejected**: it would have relocated
  an 11-second stall to the moment of creation rather than removing it. The
  measurement named the query, so the query is what changed. Measured after:
  **Base Set 30,876 ms -> 214 ms**, Journey Together 15,819 -> 44, an Arita
  master run 16,191 -> 184.
- **One model, two views.** The checklist was hand-built widgets in a
  `QGridLayout`, with no shared notion of order or filtering — so sorting, a
  second view and pagination each had nowhere to hook. Now a
  `CollectionSlotModel` + one `CollectionFilterProxy` carry the sort **and** the
  filter, and the grid and the table both read it. That is what makes it
  impossible for the two to disagree about what is shown.
- **The grid paginates rather than scrolls**, at ~20 large cells a page. A
  bounded page caps decoded art by construction, so no thumbnail eviction policy
  has to be exactly right, and it is what was asked for. Sorting and filtering
  still come from the proxy, so a page is purely a window over an ordered list.
- **`ui/card_thumbs.py`**: art decoded straight to cell size behind a 512-entry
  LRU. `CardImagePane` decodes the **full-size** PNG synchronously, which is
  right for one hovered card and wrong for a wall of them — a decoded 245x342
  pixmap is ~335 KB, so a 2,072-slot collection would have held about **694 MB**.
  It reads the disk cache only and never fetches: a network call has no place on
  a paint path, and an uncached card falls back to its collector number.
- **Missing slots still show the card's own art**, dimmed. A picture of what you
  are missing is the reason to look at a checklist; only the frame says whether
  you have it.
- **`ui/pager.py`**, extracted from the Auctions watchlist and **windowed**.
  The inline version drew one button per page, which is fine for a watchlist and
  absurd at 116 pages. Auctions now uses the shared one, so there is one pager
  rather than one per screen.
- **Two bugs found by rendering it, which no test had caught:**
  1. **The pager digits were never painted.** The app-level stylesheet gives every
     `QPushButton` `padding: 6px 14px`; these are fixed at 26 px wide, so 28 px of
     horizontal padding left a negative content box and Qt elided the digit to
     nothing — a row of empty rounded rectangles. **The inline Auctions version
     had the same bug**, so it was broken there too and is now fixed for both.
  2. **Rebuilding with `deleteLater()` alone piled up widgets.** Deletion is
     deferred to the event loop, so on a control that rebuilds on every page
     change the previous page's cells stayed parented and visible. Both rebuild
     loops now `setParent(None)` first. (`ui/card_gallery.py` carries a comment
     about hitting this before.)
- **And one bug the test suite caught in the sort:** `collector_key` returns a
  Python tuple, which Qt cannot order **as a QVariant** — so `sort()` silently did
  nothing and the grid stayed in source order however the header was clicked. The
  proxy overrides `lessThan` and compares in Python.
- **A test that only demonstrated, corrected.** The first pager-padding test
  asserted `contentsRect()` was wide enough — which passes even when nothing is
  drawn, because stylesheet padding does not affect `contentsRect()`. It now
  renders the widget and samples pixels inside each button, and it was verified
  red against the bug before being accepted. (It also needed `show()` +
  `processEvents()`: before layout every button reports geometry `(0, 0, 26, 26)`,
  so every sample read the same spot.)
- Suite 1,226 passing. Ticket 64.

### 2026-08-28 — Warming a collection's card art, and a spinner that earns its place (ticket 65)

- **Why:** deferred out of ticket 64 on the grounds that ~98% of English art was
  already cached; asked for explicitly, with a loading indicator if the work is
  slow. Measured first, and both halves are true at different times.
- **Most "missing art" is not work.** The catalog has no `image_small` for ~880
  English cards, and those dominate every gap: a 5,805-slot rarity run had **224
  cards without art, 220 of them with no URL**, leaving four to fetch. `art_gap`
  therefore reports `fetchable` and `no_source` separately, and only the first is
  ever queued — queueing the rest would retry a permanent hole on every open and
  never converge.
- **On a cold cache it is real work**, so the indicator has to exist: an empty
  cache on Base Set is 102 images, measured end-to-end at **0.14 s each at six in
  flight**, so ~14 s.
- **One QThread with a pool inside, not the shared hover queue.**
  `card_preview._fetch_queue()` deliberately **drops the oldest** request past 48
  pending — correct for hovers, where a stale one no longer matters, and fatal
  for a bulk warm of 200 cards. So the warm owns a single thread
  (`QTHREAD-LIFETIME`) and runs six fetches inside it, matching that queue's
  process-wide ceiling. Interruptible between items, and retired rather than
  replaced (`WORKER-RETIRE-NOT-REPLACE`).
- **The spinner only appears if the work is slow.** `BusyChip.begin(text,
  delay_ms)` reveals on a single-shot timer, so a warm finishing in 300 ms never
  flashes anything; under three images it is not announced at all. The arc stops
  its timer when hidden — a `QTimer` on a hidden widget repaints forever, which on
  a screen nobody is looking at is pure battery.
- **Warms on create *and* on open.** Creation was the ask, but collections made
  before this existed have cold art and a catalog re-pull can add new images.
  `art_gap` only stats the cache, so repeating the check is cheap.
- **Rejected: an always-visible progress bar.** On a warm cache the work is about
  four images; a bar that flashes on every open is noise. **Rejected: the
  existing `opBar` rail alone** — the house style for a tile with one job, but it
  cannot carry "12 of 40".
- **Two bugs found by rendering it.** The chip's pill was invisible: a plain
  `QWidget` does not paint its stylesheet background or border without
  `WA_StyledBackground` (a `QFrame` gets it for free). And a visibility test
  asserted the wrong predicate — `isVisible()` is False for a widget inside a
  `QStackedWidget` page on a view the test never shows, however the chip was set,
  so the tests use `isHidden()`, which asks whether the widget hid *itself*.
- **And one test helper that lied:** the first `qt_wait` built a `QEventLoop` and
  never `exec()`d it, so it waited ~20 ms instead of 140 and the reveal test
  failed for the wrong reason.
- Verified end-to-end over the real network: 12 of 12 fetched in 1.6 s, 12
  progress events, and `art_gap` dropping 102 -> 90 — exactly the twelve.
  Suite 1,253 passing; mutation-checked both rules.

### 2026-08-28 — A checklist cell says which card it is, not just a number (ticket 66)

- **Why:** the grid cell's caption was the collector number — the least useful
  thing to say about a card that is already showing its own art, and a duplicate
  of the list's No. column. And the list had no card preview, unlike the
  Inventory grid's Card cell.
- **The sub-line mirrors `inventory_model.subline_for`**, omissions included: a
  plain non-holo says *nothing* about foil, because `NH` on nine cards in ten is
  noise rather than information.
- **Where "holo" comes from depends on what the slot is**, in precedence order:
  the slot's own `treatment` (master mode, where the printing *is* the identity);
  else the held copy's `holo` (by number the slot is a collector number, so the
  copy you own is the only thing that can say "you have the reverse holo"); else
  the card's **sole known printing**, which is how Base Set 1-16 read as holos
  without anyone owning them. `"Normal"` is dropped for the same reason `NH` is.
- **Treatments are now loaded in both slot modes**, not only master. That is what
  makes the third case possible, and it is the same single batched query master
  mode already ran — click-in timing is unchanged at ~215 ms for Base Set.
- **Rejected: inferring the printing when a card has several.** `only_printing`
  is set only when the catalog knows of exactly one; picking the first of several
  would be a guess presented as fact.
- **Rejected: a sub-line in the list too.** Unlike Inventory, that table already
  has Set, No. and Printing as sortable columns, so repeating them under the name
  prints the row twice. The list gets the thumbnail and the name only, with
  unowned rows drawn dimmed rather than hidden.
- **Rejected: reusing `subline_for`.** It takes an `InventoryRow` — an owned lot —
  and a checklist exists to show the slots you do *not* own.
- Suite 1,270 passing.

### 2026-09-03 — `parentItem()` deleted the scene photo (ticket 68)

- **Symptom:** in Recognition, drawing a box made the photo go blank. Reported
  as "accept the YOLO proposals, then draw a new box", but it reproduced with no
  proposals at all: any draw-mode press landing on bare image rather than on an
  existing box destroyed the background. A press that lands *on* a box returns
  early and was safe, which is what made it look proposal-specific — accepting
  proposals is simply what leaves you drawing in the gaps.
- **Cause: in PySide6, `QGraphicsItem.parentItem()` returning `None` transfers
  that item's ownership from C++ to Python.** `Shiboken.ownedByPython(item)` goes
  `False -> True` across the call. `_Scene._box_at` walked `node.parentItem()` to
  find the `_BoxItem` behind a badge or grip; the background was added as
  `addPixmap(...)` with the return value dropped, so nothing held a Python
  reference, and the transient wrapper the walk created took the item with it
  when it was refcounted away. Deleting the item removes it from the scene.
  Measured: `items=16 pixmaps=1` before the drag, `items=22 pixmaps=0` after,
  and the loss happens on the **press**, not the release.
- Introduced 2026-08-07 in `b745e0c`, the commit that added `_box_at` — the
  parent walk is what bought "a click selects a box while draw mode is on".
- **The fix is to tag the decorations and climb only through them.** Badge,
  badge number and the four grips carry `_DECOR_ROLE`; the walk runs
  `while node is not None and node.data(_DECOR_ROLE)`. A decoration always has a
  parent, so `parentItem()` can no longer return `None`, and `_BoxItem` carries
  no tag so the loop terminates on it. Role `0` stays the box number, which the
  delete paths read.
- **Rejected: an `isinstance` tuple of the decoration types.** Works today, but
  it is a list of every decoration that will ever exist and breaks silently the
  next time one is added. A tag travels with the item that needs it.
- **Rejected: only holding a reference to the background.** A referenced item
  survives the flip, so it fixes the symptom — and leaves the trap armed for
  every other top-level item the walk touches. `_DividerLine` is flipped today
  too, and survives only because `self._divider` happens to hold it.
- The held reference is kept anyway, as defence in depth: `self._pixmap_item`,
  dropped before each `scene.clear()`. Belt-and-braces, not the fix.
- **The guard had to be the ownership, not the pixel.** Holding the reference
  hides the walk regressing, so the test asserts
  `not Shiboken.ownedByPython(background)` after a hit-test. Checked both ways:
  reverting the walk reddens that test, reverting the reference reddens the
  defence test. `_box_at` is the only `parentItem()` call in `ui/`.
- Suite 1,275 passing.

### 2026-09-10 — An iOS client reads a published snapshot, not the ledger (ticket 73)

- **Decision:** the phone gets a **derived, versioned, immutable projection** of
  the inventory, published by the desktop. Never a copy of `inventory.db`, never
  a peer ledger. Transport sits behind that artifact and is interchangeable; the
  first client is **native SwiftUI**, not PySide6.
- **Why a projection rather than the database: it is four hundred times smaller.**
  Measured on the live ledger — `InventoryService.inventory_rows()` returns 1,479
  rows in 26 ms, which serialise to 1,266 KB of JSON and **106 KB gzipped** (969
  in-stock rows alone: 70 KB). `inventory.db` is 38 MB, and almost all of it is
  reference data the phone has no use for: 53,391 `card_catalog` rows, 37,699
  treatments, 57,315 `price_snapshot` rows. The ledger proper is small — 1,219
  items, 478 transactions, 2,001 lines, 1,479 lots, 507 audit rows. `InventoryRow`
  is already flat and self-contained (an `Item` plus about a dozen ints, no
  catalog join), so the projection is something the app already computes rather
  than something this work invents.
- **The scope narrowed twice before this was the question.** "Port the app" became
  "Transaction and Inventory" became "strictly Inventory". Each cut removed a
  constraint, and the last one removed the decisive one — see below.
- **Why the Qt-free boundary is what made any of this cheap.** Exactly one file
  outside `ui/` imports Qt (`app.py:174`). More usefully, the C-extension
  dependencies turn out to be confined **entirely** to the vision stack: `numpy`
  in 12 files, `Pillow` in 12, `onnxruntime` in 4, all of them `condition_*`,
  `detector*`, `identity_*`, `recognizer`, `vectors`, `exemplars`, `modeljobs`,
  `corpus`, `inference`, `regioncrops`, `training`. Not one is imported by
  `transactions.py`, `inventory.py`, `reporting.py`, `allocation.py`,
  `repositories.py`, `pricing.py` or `catalog.py`, and `ui/` has none at all —
  its two PIL imports are lazy, inside `recognition.py`. The whole image path is
  `QPixmap`.
- **That mattered because Qt 6.12 shipped PySide6-on-iOS**, which would otherwise
  be the obvious route: cross-compile for `ios_arm64`, generate an Xcode project,
  keep one codebase. Its documented limitation is that packages with C extensions
  cannot be used, no iOS wheels existing on PyPI — which rules out the vision
  stack and nothing else. So the route was live for a Transaction-and-Inventory
  client.
- **Rejected: PySide6-on-iOS, once the scope became read-only.** The entire
  argument for it was keeping **one** implementation of the ledger algorithms. A
  read-only inventory client has none — no FIFO, no basis allocation, no posting,
  no audit; the projection is computed on the desktop before it leaves. What
  remains is a searchable table over a 106 KB file, and paying 1–2 weeks of
  cross-compile toolchain, no simulator, a hand-rolled Xcode project and a
  desktop-shaped UI to reuse a `QTableView` is a bad trade. The hover-based
  preview convention has no touch analogue either. Reconsider **only** if the
  phone ever needs to post.
- **Rejected: a writable phone ledger, or bidirectional sync — it is unmergeable
  by construction, not merely hard.** Every PK is `INTEGER PRIMARY KEY`, so two
  writers collide on rowids. Worse, item identity is match-or-create via
  `ORDER BY id LIMIT 1` (`repositories.py:228,273`) and FIFO draw-down is
  `ORDER BY acquired_at, id` (`repositories.py:419,433`) — so **even with UUIDs
  the tiebreak is insertion order**, and merging two divergent ledgers silently
  changes which lots were consumed, which changes cost basis, which changes
  realized P/L. There is no after-the-fact reconciliation for that. This is the
  strongest argument for the read-only framing and should be re-read before
  anyone proposes sync again.
- **Recorded, not adopted: the staged `TransactionRequest` outbox.** When the
  phone does need to write, the shape is a phone-side outbox of
  `TransactionRequest` values replayed on return through the real
  `TransactionService.post()` — that dataclass is already `post()`'s contract, and
  replaying keeps the posting algorithm in one implementation running once, which
  is what the unified-algorithm rule protects. The alternative, leasing the ledger
  the way `branch_lease.py` leases the checkout, is also correct and less good:
  it solves the same problem by forbidding concurrency rather than by keeping one
  writer.
- **Rejected: iCloud/CloudKit.** The clean answer on a Mac. Windows iCloud Drive
  cannot write into an arbitrary iOS app container, and an iOS app can only
  auto-read its own — so the "just use a synced folder" shape does not exist while
  the desktop is Windows.
- **Art is the only real payload and it is optional.** The 969 in-stock rows
  resolve to 770 distinct `card_id`s totalling **77 MB** at 103 KB average, with
  25 resolving to no art at all. `cardimages.py` is already a lazy `card_id`-keyed
  cache, so this re-encodes to roughly 10 MB of thumbnails or drops entirely —
  text rows still answer "do I own this, what did I pay, what is it worth, where
  is it". Assume **no connectivity at a show**, so whatever ships must ship before
  leaving, not stream on arrival.
- Incidental, ticketed separately: `cardimages.art_for()` is called per row from
  `ui/views/inventory.py:267` — an N+1 costing 711 ms across 969 rows.
- Nothing is built yet. Ticket 73 is deliberately mutable and carries the phased
  plan; the provider, credential and encryption decisions are open.

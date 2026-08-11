# 45# — Worker threads are parked when replaced, but not when the app shuts down

```
status:   open
kind:     fix
source:   tcgi-risk-critic on PR #6, 2026-08-07
mr:       45
axis:     correctness
found:    2026-08-07
saving:   -  (crash avoidance)
risk:     medium — the failure is a hard abort with no traceback, so it is easy to misread as something else
```

## The finding

PR #6 fixed one trigger of `QThread: Destroyed while thread is still running`
(exit `0xC0000409`): a view that interrupts a worker and immediately overwrites
its own reference to it. `retire()` parks the outgoing worker until `finished`.

**The other trigger is untouched.** `retire()` is only ever called from three
explicit stop-and-replace sites:

- `src/tcginventory/ui/views/inventory.py:712` — `_prefetch_previews`
- `src/tcginventory/ui/views/history.py:1150` — `_stop_cropper`
- `src/tcginventory/ui/views/history.py:1607` — `_stop_resolver`

Nothing parks a worker at **teardown**. There is no `closeEvent`,
`aboutToQuit` or `lastWindowClosed` handler anywhere in `src/` — verified:

```bash
grep -rn "closeEvent\|aboutToQuit\|lastWindowClosed" src/tcginventory/ --include=*.py
# (no matches)
```

So if the app is closed while a `_ThumbResolver`, `_CropWorker` or
`_PreviewPrefetchWorker` is still mid-flight, interpreter shutdown drops the
last reference to a running `QThread` — the same destructor, the same abort, a
different route in.

**Status of the evidence, stated plainly:** the replace-path abort was
*observed* (5/5 runs, fixed to 3/3, instrumented trace of the four workers). The
shutdown path is *inferred from the same mechanism* and has not been reproduced.
The window is narrow — these workers finish in well under a second at the
current ~1,100-row scale — which is consistent with nobody having hit it.
Reproducing it is step 1 below, precisely so this is not fixed on a resemblance.

## Area of impact

| File | Line | What changes |
|---|---|---|
| `src/tcginventory/ui/main_window.py` | — | gains a `closeEvent` that retires every view's live workers |
| `src/tcginventory/ui/views/_worker.py` | 17-41 | `retire()` may need a companion that *waits* with a bounded timeout — at shutdown there is no later event loop to deliver `finished` |
| `src/tcginventory/ui/views/inventory.py` | 712 | expose its worker/parked list to the shutdown path |
| `src/tcginventory/ui/views/history.py` | 1150, 1607 | same, for both workers |
| `src/tcginventory/ui/views/dashboard.py`, `calendar.py`, `shows.py` | — | these never *replace* a running worker, but they are equally exposed at teardown |
| `tests/` | new | a test that starts a worker and tears the view down under it |

## Approach

**Chosen: a shutdown pass that interrupts every live worker, then waits with a
bounded timeout.**

`retire()`'s park-and-release depends on the event loop delivering `finished`.
At shutdown there is no more event loop, so parking alone does not help — the
process is leaving and the object dies either way. Shutdown needs
`requestInterruption()` followed by a bounded `wait(ms)`, which is exactly the
blocking call `retire()` avoids during normal operation, and is appropriate here
because the UI is already going away.

### Rejected alternatives

- **Call `retire()` from `closeEvent` and rely on `finished`.** Symmetrical and
  wrong: no event loop runs after `closeEvent` returns, so the release never
  fires and the wait never happens.
- **Make every worker a Qt child of its view.** Qt would delete them in the
  right order, but `QThread` deletion still requires the thread to have stopped,
  so it converts the crash into the same crash with a tidier ownership graph.
- **`wait()` with no timeout.** A worker blocked on a network fetch would hang
  the close. The bounded wait accepts a rare abort over a frozen window.
- **Daemonise and ignore it.** `QThread` has no daemon flag; this is not
  `threading.Thread`.
- **Leave it.** Defensible while the window is sub-second — but it is the same
  abort that already cost a debugging session, and it presents as "crashes on
  exit, no error", which is expensive to diagnose a second time.

## Plan

1. **Reproduce it first.** Force the window: a worker with an artificial delay,
   close the main window under it, confirm the abort. Without this, the fix is
   being applied to a resemblance rather than a defect — the trap this repo has
   already recorded once.
2. Add the shutdown pass to `main_window.py`, walking views that expose live
   workers.
3. Give `_worker.py` the bounded-wait companion, documented against `retire()`
   so the difference between "replaced" and "torn down" is legible.
4. Cover every worker-owning view, not just the three that had the replace bug.
5. Negative test per `CHECKER-NEEDS-NEGATIVE-TEST`: the test must fail with the
   shutdown pass removed.

## Verification

- The repro from step 1 aborts before and exits cleanly after.
- Full suite green, offscreen.
- The frozen exe opens and closes with History and Inventory visible, repeatedly.

## Notes

- Raised by `tcgi-risk-critic` reviewing PR #6, which it passed as `revise` —
  explicitly not a regression from that PR, since the gap predates it and is not
  made worse by it.
- Related: [[003]] — the review gate. Both were found by using the machinery
  rather than reading it.

## Filed before the numbering change

This was issue #16 under the old scheme, where a ticket carried three unrelated numbers (ours, the issue's, the PR's). It now carries one — the merge request's — so `X#` and `X!` are the same work. The old issue is closed and points here; nothing about the finding below changed.

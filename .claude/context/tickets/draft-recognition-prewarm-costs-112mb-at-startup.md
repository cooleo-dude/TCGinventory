# Recognition prewarm holds 112 MB at startup for everyone

```
status:   deferred
kind:     optimization
source:   /optimization-sweep grounded pass, 2026-08-07
mr:       none
axis:     memory
found:    2026-08-07
saving:   ~103 MB resident for anyone who never opens Recognition
risk:     low — the change is a flag; the cost is the first-suggestion freeze it exists to prevent
```

## The finding

`RecognitionView(conn, prewarm=True)` loads the DINOv2 ONNX session while the
app starts, for every user, whether or not they ever open Recognition.

Isolated in a fresh process, so the async allocation is attributed correctly:

| | build | resident |
|---|---:|---|
| `prewarm=True` | 198 ms | 96 → **198 MB** (lands ~1 s later) |
| `prewarm=False` | 182 ms | 96 → **95 MB** |

Total resident with all views built is **257 MB**, so this is roughly **44% of
the app's memory** — and the largest single item by a wide margin.

**Build time barely moves (198 ms vs 182 ms), so latency work cannot see this.**
It was invisible until memory was measured as its own axis, which is the reason
the sweep has three.

## Why it is deferred, not open

The user said to leave it. Prewarm exists so the first suggestion is not a freeze,
and that tradeoff stands. This ticket exists so the 112 MB stays visible and the
decision is re-openable, not to argue with it.

**Reopen if:** the user asks, or resident memory becomes a reported problem.

## If it is ever picked up

The obvious shape is making prewarm lazy or opt-in — but note the coupling
recorded in `declined.md`: **deferring the vision imports off startup was
measured and rejected**, and the reason was prewarm. With the eager import gone,
`RecognitionView(prewarm=True)` loads onnxruntime itself on a background thread
against the import lock, and total build went 1,228 ms → 1,324 ms. The two
changes only pay off together.

So the real options are: prewarm on first navigation to Recognition; prewarm
behind a preference; or prewarm on an idle timer after the window is
interactive. Each needs the first-suggestion latency measured, not assumed —
that is the thing prewarm is buying.

## Verification, if implemented

- `perf.py memory` — resident with all views built should fall from ~257 MB to
  ~145 MB.
- Time the first autofill suggestion with and without, on a cold process. If that
  regresses past a second, the tradeoff has simply moved rather than improved.
- `perf.py latency --views` — Recognition build must not regress, and neither
  must whatever is built after it.

## Notes

- Measured, not estimated: `references/baseline.md` § Memory carries the isolated
  before/after.
- Do not pair this with the vision-import deferral without re-measuring both;
  that combination is the one that already failed.

## Filed before the numbering change

This was issue #12 under the old scheme, where a ticket carried three unrelated numbers (ours, the issue's, the PR's). It now carries one — the merge request's — so `X#` and `X!` are the same work. The old issue is closed and points here; nothing about the finding below changed.

"""Shell-command tokenising, shared by the two `PreToolUse` hooks.

`branch_lease.py` and `review_gate.py` both have to answer "what is this command
actually doing?" on **every** shell call in the session. Both learned the same
lesson the hard way, and it is recorded in ticket 003: a substring search over the
raw command line is *simultaneously too broad and too narrow*. `review_gate`'s
`\\bgh\\b[^\\n]*\\bpr\\b[^\\n]*\\bmerge\\b` blocked a `gh pr create` whose **body
prose** contained the word "merge", while missing a real merge invoked through a
shell variable. Tokenising fixes both directions at once: quoted prose becomes one
opaque token, and the subcommand is read from its argv position rather than from
anywhere in the string.

**This module is a leaf on purpose.** It imports nothing but `shlex`, so either
hook can import it at module scope without dragging a dependency chain into a
code path that runs before every shell call. That is why the tokeniser lives here
rather than being imported from `branch_lease`, which pulls in `_session` (and
with it the lease registry) the moment it is imported.

Sharing it rather than copying it follows the precedent set for `rules.py`'s
`path_matches`: the function's docstring records the bugs it exists to prevent,
and a second copy re-opens every one of them somewhere nobody is looking.
"""

from __future__ import annotations

import shlex

SEPARATORS = {";", "&&", "||", "|", "&", "\n"}


def segments(command: str) -> list[list[str]]:
    """Tokenise, then split on shell separators.

    `punctuation_chars=True` is what makes `;` and `&&` their own tokens while
    leaving a quoted `"fix; the thing"` intact. Backslashes are consumed in posix
    mode, so a Windows path arrives mangled — harmless for both callers, because
    neither reads a path *value*, only subcommands and flags.

    Returns `[]` when the command cannot be tokenised (an unbalanced quote), which
    every caller treats as "nothing to judge" rather than as a reason to raise.
    """
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return []

    out: list[list[str]] = [[]]
    for token in tokens:
        if token in SEPARATORS:
            out.append([])
        else:
            out[-1].append(token)
    return [segment for segment in out if segment]


def program(argv0: str) -> str:
    """The invoked program's bare name — `gh.exe` and `C:/bin/gh` both give `gh`.

    Classifying on this rather than on a `\\bgh\\b` regex is what stops the word
    appearing anywhere else in the command from counting as an invocation.
    """
    name = argv0.replace("\\", "/").rsplit("/", 1)[-1].lower()
    return name.removesuffix(".exe")

"""Quoting Python strings as Tcl words.

Script arguments reach a session as ``set ::argv [list ...]`` in front of the
script, so each one has to survive Tcl's parser unchanged whatever it holds.
"""

from __future__ import annotations

import re

_PLAIN = re.compile(r"[A-Za-z0-9_./:=+,@%^-]+\Z")
_NAMED = {"\n": "\\n", "\r": "\\r", "\t": "\\t"}


def tcl_word(text: str) -> str:
    """One Tcl word that evaluates to exactly ``text``.

    Anything outside a plain set of characters is backslash-escaped rather
    than braced: braces fail on an unbalanced ``{`` or a trailing backslash,
    and a backslash before an ASCII punctuation character always yields that
    character.
    """
    if text == "":
        return "{}"
    if _PLAIN.match(text):
        return text
    out = []
    for ch in text:
        if ch in _NAMED:
            out.append(_NAMED[ch])
        elif ch.isascii() and not ch.isalnum() and ch != "_":
            out.append("\\" + ch)
        else:
            out.append(ch)
    return "".join(out)


def tcl_list(items: list[str]) -> str:
    """A ``[list ...]`` command whose result is ``items``."""
    return "[list" + "".join(" " + tcl_word(i) for i in items) + "]"


def with_argv(script: str, args: list[str]) -> str:
    """``script`` with ``::argv`` and ``::argc`` set to ``args`` first.

    The assignment shares the script's first line, so line numbers in a
    proc body's traceback still match the file.
    """
    return f"set ::argv {tcl_list(args)}; set ::argc {len(args)}; {script}"

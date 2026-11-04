"""Memory figures from /proc: what each session holds, and what is left.

Each session is a whole Vivado and holds 4 to 6 GB, and the hosts this runs
on have no swap, so "how much do the sessions take" and "is there room for
another" are the questions asked most often around `vrc list` and `vrc
start`. Linux only, like the rest of the pid handling.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

#: argv[0] of the real Vivado process. The `bin/vivado` and `bin/loader`
#: wrappers above it are shell scripts, so their argv[0] is the shell.
VIVADO_EXE_SUFFIX = "unwrapped/lnx64.o/vivado"

_PAGE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096


def _pids() -> list[int]:
    try:
        return [int(d) for d in os.listdir("/proc") if d.isdigit()]
    except OSError:
        return []


def _ppid(pid: int) -> int | None:
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            data = fh.read()
    except OSError:
        return None
    fields = data[data.rfind(b")") + 1 :].split()
    try:
        return int(fields[1])
    except (IndexError, ValueError):
        return None


def rss(pid: int) -> int | None:
    """Resident bytes of one process, or None if it cannot be read."""
    try:
        with open(f"/proc/{pid}/statm", "rb") as fh:
            return int(fh.read().split()[1]) * _PAGE
    except (OSError, IndexError, ValueError):
        return None


def children_map() -> dict[int, list[int]]:
    tree: dict[int, list[int]] = {}
    for pid in _pids():
        parent = _ppid(pid)
        if parent is not None:
            tree.setdefault(parent, []).append(pid)
    return tree


def descendants(pid: int, tree: dict[int, list[int]]) -> list[int]:
    """``pid`` and every process below it."""
    out, todo = [], [pid]
    while todo:
        p = todo.pop()
        out.append(p)
        todo.extend(tree.get(p, ()))
    return out


def tree_rss(pid: int, tree: dict[int, list[int]] | None = None) -> int | None:
    """Resident bytes of a process and its children, or None if not visible.

    Synthesis runs helper processes under the Vivado that asked for it, and
    they count against the same memory.
    """
    if pid <= 0 or rss(pid) is None:
        return None
    tree = children_map() if tree is None else tree
    return sum(rss(p) or 0 for p in descendants(pid, tree))


def mem_available() -> int | None:
    """MemAvailable from /proc/meminfo, in bytes."""
    try:
        with open("/proc/meminfo", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


@dataclass
class Stray:
    """A Vivado running outside any session: a batch run, or someone's GUI."""

    pid: int
    rss: int
    args: str


def _cmdline(pid: int) -> list[str]:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            raw = fh.read()
    except OSError:
        return []
    return [a.decode("utf-8", errors="replace") for a in raw.split(b"\0") if a]


def stray_vivados(
    session_pids: list[int], tree: dict[int, list[int]] | None = None
) -> list[Stray]:
    """Vivado processes that belong to no registered session."""
    tree = children_map() if tree is None else tree
    owned: set[int] = set()
    for pid in session_pids:
        if pid > 0:
            owned.update(descendants(pid, tree))
    out = []
    for pid in sorted(_pids()):  # parents first, nearly always
        if pid in owned:
            continue
        argv = _cmdline(pid)
        if not argv or not argv[0].endswith(VIVADO_EXE_SUFFIX):
            continue
        # A Vivado's own helper processes count towards it, not as strays.
        owned.update(descendants(pid, tree))
        out.append(Stray(pid, tree_rss(pid, tree) or 0, " ".join(argv[1:])))
    return out


def fmt_bytes(n: int | None) -> str:
    if n is None:
        return "-"
    gib = n / 2**30
    if gib >= 1:
        return f"{gib:.1f}G"
    return f"{n / 2**20:.0f}M"

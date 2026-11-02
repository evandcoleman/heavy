"""heavyq: the shared queue behind `heavy` (imported by heavy-run and heavy-top).

State lives in $HEAVY_HOME (default ~/.heavy); the slot count in ~/.config/heavy/slots.
Stdlib only, Python 3.10+, no terminal I/O and no process spawning.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

DEFAULT_SLOTS = 2
POLL_SEC = 0.5


# --- paths -----------------------------------------------------------------

@dataclass(frozen=True)
class Paths:
    home: Path        # $HEAVY_HOME or $HOME/.heavy
    slots_file: Path  # $HOME/.config/heavy/slots

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Paths:
        user_home = Path(env.get("HOME") or os.path.expanduser("~"))
        home = env.get("HEAVY_HOME") or str(user_home / ".heavy")
        return cls(home=Path(home), slots_file=user_home / ".config" / "heavy" / "slots")


# --- models ----------------------------------------------------------------

@dataclass(frozen=True)
class Waiter:
    pid: int
    cmd: str
    cwd: str
    enqueued: float  # epoch; also this incarnation's token
    order: float     # queue position key; bump lowers it
    force: bool = False
    cancel: bool = False


@dataclass(frozen=True)
class Holder:
    pid: int
    cmd: str
    cwd: str
    started: float   # epoch; also this incarnation's token
    slot: int | None  # None when forced
    forced: bool = False
    kill: bool = False
    last_progress: float | None = None


def _is_num(v: object) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _get(d: dict, key: str, check, default=...) -> object:
    if key not in d:
        if default is ...:
            raise ValueError(f"missing {key}")
        return default
    v = d[key]
    if not check(v):
        raise ValueError(f"bad {key}: {v!r}")
    return v


def _common(d: object, pid: int) -> dict:
    if not isinstance(d, dict):
        raise ValueError("entry is not an object")
    got = _get(d, "pid", lambda v: isinstance(v, int) and not isinstance(v, bool))
    if got != pid:
        raise ValueError(f"pid {got} does not match file {pid}")
    return {
        "pid": pid,
        "cmd": _get(d, "cmd", lambda v: isinstance(v, str)),
        "cwd": _get(d, "cwd", lambda v: isinstance(v, str), ""),
    }


def _is_bool(v: object) -> bool:
    return isinstance(v, bool)


def waiter_to_json(w: Waiter) -> dict:
    return {"pid": w.pid, "cmd": w.cmd, "cwd": w.cwd, "enqueued": w.enqueued,
            "order": w.order, "force": w.force, "cancel": w.cancel}


def waiter_from_json(d: object, pid: int) -> Waiter:
    base = _common(d, pid)
    enqueued = float(_get(d, "enqueued", _is_num))
    return Waiter(**base, enqueued=enqueued,
                  order=float(_get(d, "order", _is_num, enqueued)),
                  force=_get(d, "force", _is_bool, False),
                  cancel=_get(d, "cancel", _is_bool, False))


def holder_to_json(h: Holder) -> dict:
    return {"pid": h.pid, "cmd": h.cmd, "cwd": h.cwd, "started": h.started, "slot": h.slot,
            "forced": h.forced, "kill": h.kill, "last_progress": h.last_progress}


def holder_from_json(d: object, pid: int) -> Holder:
    base = _common(d, pid)
    slot = _get(d, "slot", lambda v: v is None or (isinstance(v, int) and not isinstance(v, bool)
                                                   and v >= 0), None)
    lp = _get(d, "last_progress", lambda v: v is None or _is_num(v), None)
    return Holder(**base, started=float(_get(d, "started", _is_num)), slot=slot,
                  forced=_get(d, "forced", _is_bool, False),
                  kill=_get(d, "kill", _is_bool, False),
                  last_progress=None if lp is None else float(lp))


def parse_slots(text: str) -> int:
    digits = "".join(c for c in text if c.isdigit())
    return int(digits) if digits and int(digits) > 0 else DEFAULT_SLOTS


# --- atomic writes ---------------------------------------------------------

def atomic_write_text(path: Path, text: str) -> None:
    tmp = path.parent / f".{path.name}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_json(path: Path, obj: dict) -> None:
    atomic_write_text(path, json.dumps(obj, sort_keys=True) + "\n")

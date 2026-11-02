"""heavyq: the shared queue behind `heavy` (imported by heavy-run and heavy-top).

State lives in $HEAVY_HOME (default ~/.heavy); the slot count in ~/.config/heavy/slots.
Stdlib only, Python 3.10+, no terminal I/O and no process spawning.

Layout of $HEAVY_HOME:
  lock               flock held briefly around every queue read-modify-write
  queue/<pid>.json   a waiter      running/<pid>.json   a holder
  slots/<i>          a holder keeps flock(LOCK_EX) on one for its life; the kernel frees it on death
  live/<pid>         every heavy-run keeps flock(LOCK_EX) on its own file for its whole life.
                     An entry is alive only while that lock is held, so a dead or zombie
                     process (whose fds the kernel has closed) is pruned, and pid reuse can't
                     keep a stale entry alive.
  paused             exists -> no new non-forced admissions

Invariants:
  * Slots are acquired only inside Store.locked(). Holders release them only by closing
    the fd or dying. So within one lock section, held_slots() then take_slot() can't be beaten.
  * Entry files and *.tmp files are written only inside Store.locked(), so prune may
    delete any *.tmp it sees.
  * Every fd this module opens is close-on-exec, so a job's children never inherit a slot.
  * Nothing here ever signals a pid read from a state file.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Iterable, Iterator, Literal, Mapping, Sequence

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


# --- pure ranking and admission --------------------------------------------

def rank(waiters: Iterable[Waiter]) -> list[Waiter]:
    """Waiters competing for a slot, in queue order. Forced/cancelled ones don't take a rank."""
    eligible = [w for w in waiters if not w.force and not w.cancel]
    return sorted(eligible, key=lambda w: (w.order, w.enqueued, w.pid))


def free_slots(n: int, held: frozenset[int]) -> list[int]:
    """Admittable slot indices. A held index >= n (slots shrank) still uses up capacity."""
    free = [i for i in range(n) if i not in held]
    overflow = sum(1 for i in held if i >= n)
    return free[:max(0, len(free) - overflow)]


@dataclass(frozen=True)
class Verdict:
    kind: Literal["cancel", "force", "admit", "wait"]
    slot: int | None = None
    rank: int | None = None


def decide(me: Waiter, waiters: Sequence[Waiter], n: int,
           held: frozenset[int], paused: bool) -> Verdict:
    if me.cancel:
        return Verdict("cancel")
    if me.force:
        return Verdict("force")
    if paused:
        return Verdict("wait")
    ranked = rank([w for w in waiters if w.pid != me.pid] + [me])
    my_rank = next(i for i, w in enumerate(ranked) if w.pid == me.pid)
    free = free_slots(n, held)
    if my_rank < len(free):
        return Verdict("admit", slot=free[0], rank=my_rank)
    return Verdict("wait", rank=my_rank)


def admitted(me: Waiter, v: Verdict, now: float) -> Holder:
    forced = v.kind == "force"
    return Holder(pid=me.pid, cmd=me.cmd, cwd=me.cwd, started=now,
                  slot=None if forced else v.slot, forced=forced)


def bumped(target: Waiter, waiters: Sequence[Waiter]) -> Waiter:
    lowest = min([w.order for w in waiters] + [target.order])
    return replace(target, order=lowest - 1)


# --- store I/O -------------------------------------------------------------

LOCK_RETRY_SEC = 0.01
LIVE_RETRIES = 100
_ENTRY_RE = re.compile(r"^([0-9]+)\.json$")
_OPEN_FLAGS = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC


class Busy(Exception):
    """The queue lock is held by someone else past the timeout."""


def _try_flock(fd: int) -> bool:
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


def _is_locked_elsewhere(path: Path) -> bool | None:
    """True if another open file holds path's flock, False if free, None if the file is missing."""
    try:
        fd = os.open(path, os.O_RDWR | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    try:
        return not _try_flock(fd)
    finally:
        os.close(fd)


def _unlink_quiet(path: Path) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


class Store:
    def __init__(self, paths: Paths, *, alive: Callable[[int], bool] | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep,
                 live_probe_hook: Callable[[Path], None] = lambda path: None):
        self.paths = paths
        self._alive = alive or self._live_lock_held
        self._clock = clock
        self._sleep = sleep
        self._live_probe_hook = live_probe_hook  # test seam: runs between probe and unlink

    # paths
    def _dir(self, sub: str) -> Path:
        return self.paths.home / sub

    def _entry(self, sub: str, pid: int) -> Path:
        return self._dir(sub) / f"{pid}.json"

    def ensure(self) -> None:
        for sub in ("", "queue", "running", "slots", "live"):
            (self.paths.home / sub).mkdir(mode=0o700, parents=True, exist_ok=True)

    @contextmanager
    def locked(self, timeout: float = 0.0) -> Iterator[None]:
        fd = os.open(self.paths.home / "lock", _OPEN_FLAGS, 0o600)
        try:
            deadline = self._clock() + timeout
            while not _try_flock(fd):
                if self._clock() >= deadline:
                    raise Busy(str(self.paths.home / "lock"))
                self._sleep(LOCK_RETRY_SEC)
            yield
        finally:
            os.close(fd)  # closing the last fd releases the flock

    # liveness
    def hold_live(self, pid: int) -> int:
        """Lock live/<pid> for this process's life; returns the fd (keep it open)."""
        path = self._dir("live") / str(pid)
        for _ in range(LIVE_RETRIES):
            fd = self.try_hold_live(path)
            if fd is not None:
                return fd
            self._sleep(LOCK_RETRY_SEC)
        raise OSError(f"cannot lock {path}")

    @staticmethod
    def try_hold_live(path: Path) -> int | None:
        """One attempt of hold_live: the locked fd, or None if busy or the file was replaced."""
        fd = os.open(path, _OPEN_FLAGS, 0o600)
        # A pruner may have unlinked the file between our open and flock: make sure
        # the locked inode is still the one at path.
        if _try_flock(fd):
            try:
                if os.stat(path).st_ino == os.fstat(fd).st_ino:
                    return fd
            except FileNotFoundError:
                pass
        os.close(fd)
        return None

    def _live_lock_held(self, pid: int) -> bool:
        try:
            return bool(_is_locked_elsewhere(self._dir("live") / str(pid)))
        except OSError:
            return True  # can't tell: keep the entry rather than drop a live job

    # entries
    def _entries(self, sub: str, parse) -> Iterator[tuple[Path, object | None]]:
        """(path, entry) per entry file; entry is None when the file is corrupt."""
        try:
            paths = list(self._dir(sub).iterdir())
        except FileNotFoundError:
            return
        for path in paths:
            m = _ENTRY_RE.match(path.name)
            if not m:
                continue
            try:
                yield path, parse(json.loads(path.read_text()), int(m.group(1)))
            except FileNotFoundError:
                continue
            except (ValueError, OSError):
                yield path, None

    def prune(self) -> list[str]:
        """Drop dead-pid and corrupt entries and stale temp files. Call under locked()."""
        corrupt = []
        for sub, parse in (("queue", waiter_from_json), ("running", holder_from_json)):
            for path in list(self._dir(sub).glob(".*.tmp")):
                _unlink_quiet(path)
            for path, entry in self._entries(sub, parse):
                if entry is None:
                    corrupt.append(f"{sub}/{path.name}")
                    _unlink_quiet(path)
                elif not self._alive(entry.pid):
                    _unlink_quiet(path)
        self._prune_live()
        return corrupt

    def _prune_live(self) -> None:
        try:
            names = os.listdir(self._dir("live"))
        except FileNotFoundError:
            return
        for name in names:
            path = self._dir("live") / name
            try:
                fd = os.open(path, os.O_RDWR | os.O_CLOEXEC)
            except OSError:
                continue
            try:
                # Unlink while still holding the probe lock: a new owner can't lock this
                # inode until we close, and by then it's gone from path, so hold_live's
                # inode check makes the owner retry on a fresh file.
                if _try_flock(fd):
                    self._live_probe_hook(path)
                    _unlink_quiet(path)
            except OSError:
                pass
            finally:
                os.close(fd)

    def waiters(self) -> list[Waiter]:
        return [e for _, e in self._entries("queue", waiter_from_json) if e is not None]

    def holders(self) -> list[Holder]:
        return [e for _, e in self._entries("running", holder_from_json) if e is not None]

    def _get(self, sub: str, pid: int, parse):
        try:
            return parse(json.loads(self._entry(sub, pid).read_text()), pid)
        except (OSError, ValueError):
            return None

    def get_waiter(self, pid: int) -> Waiter | None:
        return self._get("queue", pid, waiter_from_json)

    def get_holder(self, pid: int) -> Holder | None:
        """Safe without the lock: entries are replaced by atomic rename."""
        return self._get("running", pid, holder_from_json)

    def put_waiter(self, w: Waiter) -> None:
        atomic_write_json(self._entry("queue", w.pid), waiter_to_json(w))

    def put_holder(self, h: Holder) -> None:
        atomic_write_json(self._entry("running", h.pid), holder_to_json(h))

    def drop_waiter(self, pid: int) -> None:
        _unlink_quiet(self._entry("queue", pid))

    def drop_holder(self, pid: int) -> None:
        _unlink_quiet(self._entry("running", pid))

    # queue settings
    def paused(self) -> bool:
        return (self.paths.home / "paused").exists()

    def set_paused(self, on: bool) -> None:
        flag = self.paths.home / "paused"
        if on:
            flag.touch()
        else:
            _unlink_quiet(flag)

    def slots(self) -> int:
        try:
            return parse_slots(self.paths.slots_file.read_text())
        except OSError:
            return DEFAULT_SLOTS

    def set_slots(self, n: int) -> None:
        self.paths.slots_file.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(self.paths.slots_file, f"{max(1, n)}\n")

    # slots
    def _slot_path(self, i: int) -> Path:
        return self._dir("slots") / str(i)

    def held_slots(self, n: int) -> frozenset[int]:
        """Indices whose flock someone holds: 0..n-1 plus any existing index >= n."""
        extra = []
        for name in os.listdir(self._dir("slots")):
            if name.isdigit() and int(name) >= n:
                extra.append(int(name))
        held = set()
        for i in [*range(n), *extra]:
            path = self._slot_path(i)
            if i < n:
                os.close(os.open(path, _OPEN_FLAGS, 0o600))
            if _is_locked_elsewhere(path):
                held.add(i)
        return frozenset(held)

    def take_slot(self, i: int) -> int | None:
        fd = os.open(self._slot_path(i), _OPEN_FLAGS, 0o600)
        if _try_flock(fd):
            return fd
        os.close(fd)
        return None


# --- transactions (each takes the lock itself) -----------------------------

CONTROL_LOCK_SEC = 1.0


@dataclass(frozen=True)
class Admitted:
    holder: Holder
    slot_fd: int | None  # None when forced


@dataclass(frozen=True)
class Waiting:
    rank: int | None  # None when paused or the lock was busy


@dataclass(frozen=True)
class Cancelled:
    pass


Step = Admitted | Waiting | Cancelled
Outcome = Literal["ok", "gone", "busy"]


def enqueue(store: Store, me: Waiter) -> None:
    """Add me to the queue. Raises Busy; admit_step re-creates the entry anyway."""
    with store.locked(CONTROL_LOCK_SEC):
        store.prune()
        store.put_waiter(me)


def admit_step(store: Store, me: Waiter, now: float) -> Step:
    """One admission poll. Flags (force/cancel/bump) are read from the entry file."""
    try:
        with store.locked():
            store.prune()
            cur = store.get_waiter(me.pid)
            if cur is None or cur.enqueued != me.enqueued:
                store.put_waiter(me)  # self-heal: re-create from memory, same order
                cur = me
            waiters = store.waiters()
            if cur.cancel:
                store.drop_waiter(me.pid)
                return Cancelled()
            n = store.slots()
            held = frozenset() if cur.force else store.held_slots(n)
            v = decide(cur, waiters, n, held, store.paused())
            if v.kind == "wait":
                return Waiting(v.rank)
            fd = None if v.kind == "force" else store.take_slot(v.slot)
            if v.kind == "admit" and fd is None:
                return Waiting(v.rank)
            holder = admitted(cur, v, now)
            try:
                store.put_holder(holder)
                store.drop_waiter(me.pid)
            except BaseException:
                if fd is not None:
                    os.close(fd)
                raise
            return Admitted(holder, fd)
    except Busy:
        return Waiting(None)


def _drop_best_effort(store: Store, drop: Callable[[int], None], pid: int) -> None:
    """Remove our own entry; without the lock if it's busy (the file is ours, removal is atomic)."""
    try:
        with store.locked(CONTROL_LOCK_SEC):
            drop(pid)
    except Busy:
        drop(pid)


def leave_queue(store: Store, pid: int) -> None:
    _drop_best_effort(store, store.drop_waiter, pid)


def finish(store: Store, pid: int) -> None:
    """Drop our running entry. The caller closes the slot fd afterwards."""
    _drop_best_effort(store, store.drop_holder, pid)


def record_progress(store: Store, base: Holder, last_progress: float) -> bool:
    """Merge last_progress into the current entry (keeps a kill flag). False when the lock is busy."""
    try:
        with store.locked():
            cur = store.get_holder(base.pid)
            if cur is None or cur.started != base.started:
                cur = base
            store.put_holder(replace(cur, last_progress=last_progress))
            return True
    except Busy:
        return False


def kill_requested(store: Store, pid: int) -> bool:
    h = store.get_holder(pid)
    return bool(h and h.kill)


# --- controls (heavy-top actions) ------------------------------------------

def _control_waiter(store: Store, pid: int, token: float,
                    change: Callable[[Waiter, list[Waiter]], Waiter]) -> Outcome:
    try:
        with store.locked(CONTROL_LOCK_SEC):
            store.prune()
            w = store.get_waiter(pid)
            if w is None or w.enqueued != token:
                return "gone"
            store.put_waiter(change(w, store.waiters()))
            return "ok"
    except Busy:
        return "busy"


def force(store: Store, pid: int, token: float) -> Outcome:
    return _control_waiter(store, pid, token, lambda w, _ws: replace(w, force=True))


def cancel(store: Store, pid: int, token: float) -> Outcome:
    return _control_waiter(store, pid, token, lambda w, _ws: replace(w, cancel=True))


def bump(store: Store, pid: int, token: float) -> Outcome:
    return _control_waiter(store, pid, token, bumped)


def kill(store: Store, pid: int, token: float) -> Outcome:
    """Flag a holder; its own heavy-run kills the job. Never signals anything itself."""
    try:
        with store.locked(CONTROL_LOCK_SEC):
            store.prune()
            h = store.get_holder(pid)
            if h is None or h.started != token:
                return "gone"
            store.put_holder(replace(h, kill=True))
            return "ok"
    except Busy:
        return "busy"


def toggle_pause(store: Store) -> bool | Literal["busy"]:
    try:
        with store.locked(CONTROL_LOCK_SEC):
            on = not store.paused()
            store.set_paused(on)
            return on
    except Busy:
        return "busy"


def change_slots(store: Store, delta: int) -> int | Literal["busy"]:
    try:
        with store.locked(CONTROL_LOCK_SEC):
            n = max(1, store.slots() + delta)
            store.set_slots(n)
            return n
    except Busy:
        return "busy"


@dataclass(frozen=True)
class QueueState:
    waiters: tuple[Waiter, ...]  # ranked; pending forced/cancelled after them
    holders: tuple[Holder, ...]  # by start time
    n: int
    held: frozenset[int]
    paused: bool
    corrupt: tuple[str, ...]


def read_state(store: Store, timeout: float = CONTROL_LOCK_SEC) -> QueueState:
    """Prune, read and probe in one lock section. Raises Busy."""
    with store.locked(timeout):
        corrupt = store.prune()
        waiters = store.waiters()
        ranked = rank(waiters)
        pending = sorted((w for w in waiters if w.force or w.cancel), key=lambda w: (w.order, w.enqueued))
        n = store.slots()
        return QueueState(
            waiters=tuple(ranked + pending),
            holders=tuple(sorted(store.holders(), key=lambda h: (h.started, h.pid))),
            n=n, held=store.held_slots(n), paused=store.paused(), corrupt=tuple(corrupt))

# Implementation plan: own queue and heavy-top job control

Spec: `docs/specs/2026-11-01-own-queue-and-tui-controls.md`. Save this plan as `docs/plans/2026-11-01-own-queue-and-tui-controls.md`.
Branch: `feat/own-queue`. Commit after each step with a conventional message.
TDD applies to every logic step. The TUI's colours and layout are exempt; the plan notes where.

**Decision needed before Step 1: Risk R1, zombie liveness.** It is the one place where I recommend something beyond the spec's state table. Everything else below follows the spec.

---

## 0. Conventions used throughout

- **Python and imports.** Python 3.10+, stdlib only, `from __future__ import annotations`. Tests import `heavyq` with `sys.path.insert(0, ROOT/"bin")`. They load `heavy-run` and `heavy-top` with `SourceFileLoader`, the same pattern the tests use today.
- **Unit test isolation.** Every unit test gets a `tempfile.TemporaryDirectory()` as `HEAVY_HOME`, its own slots file, a fake clock (`lambda: t[0]`) and a fake `sleep` that advances `t[0]`. Pid liveness is injected through `alive`. There are no real sleeps and no real child processes.
- **Real flock in unit tests.** Unit tests use real `flock` on temp files, which is fast. To simulate "another process holds slot i", the test opens the slot file on a second fd and calls `flock(LOCK_EX)`. Two different open-file descriptions conflict even inside one process, on both macOS and Linux.
- **Command shorthand.** `T=python3 tests/test_heavyq.py` and so on, for the commands in each step.

---

## 1. Ordered steps

### Step 1: `heavyq.py` state model, serialization, paths and atomic writes
**Files:** `bin/heavyq.py` (new), `tests/test_heavyq.py` (new)

```python
DEFAULT_SLOTS = 2
POLL_SEC = 0.5

@dataclass(frozen=True)
class Paths:
    home: Path            # $HEAVY_HOME or $HOME/.heavy
    slots_file: Path      # $HOME/.config/heavy/slots
    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Paths

@dataclass(frozen=True)
class Waiter:
    pid: int; cmd: str; cwd: str; enqueued: float; order: float
    force: bool = False; cancel: bool = False
    # token = enqueued (identifies this incarnation of pid)

@dataclass(frozen=True)
class Holder:
    pid: int; cmd: str; cwd: str; started: float; slot: int | None
    forced: bool = False; kill: bool = False; last_progress: float | None = None
    # token = started

def waiter_to_json(w: Waiter) -> dict
def waiter_from_json(d: object, pid: int) -> Waiter    # ValueError on bad shape / pid mismatch
def holder_to_json(h: Holder) -> dict
def holder_from_json(d: object, pid: int) -> Holder
def parse_slots(text: str) -> int                       # digits only, >0 else DEFAULT_SLOTS (same rule as today)
def atomic_write_json(path: Path, obj: dict) -> None    # .<name>.<pid>.tmp + os.replace; unlink tmp on error
```

**Failing tests:**
- `test_paths_default_and_override`: `HEAVY_HOME` wins; otherwise `$HOME/.heavy`. The slots file is always `$HOME/.config/heavy/slots`.
- `test_waiter_roundtrip` and `test_holder_roundtrip`: `from_json(to_json(x)) == x`. Optional fields default when they are missing.
- `test_from_json_rejects_bad`: wrong types, missing `pid`/`cmd`/`enqueued`, and a pid that doesn't match the filename all raise `ValueError`.
- `test_parse_slots`: `"3\n"` gives 3; `"0"`, `""` and `"x"` give 2.
- `test_atomic_write_leaves_no_tmp`: the target is valid JSON afterwards and no dotfiles remain in the directory.

**Verify:** `python3 tests/test_heavyq.py`

### Step 2: Pure ranking and admission decision
**Files:** `bin/heavyq.py`, `tests/test_heavyq.py`

```python
def rank(waiters: Iterable[Waiter]) -> list[Waiter]
    # eligible = not force and not cancel; sorted by (order, enqueued, pid)
def free_slots(n: int, held: frozenset[int]) -> list[int]
    # free = [i in 0..n-1 not held]; overflow = |{i in held: i >= n}|; return free[:max(0, len(free)-overflow)]

@dataclass(frozen=True)
class Verdict:
    kind: Literal["cancel", "force", "admit", "wait"]
    slot: int | None = None
    rank: int | None = None

def decide(me: Waiter, waiters: Sequence[Waiter], n: int,
           held: frozenset[int], paused: bool) -> Verdict
    # cancel > force > (paused -> wait) > rank < len(free) -> admit(free[0]) > wait(rank)
def admitted(me: Waiter, v: Verdict, now: float) -> Holder   # slot/forced from verdict, started=now
def bumped(target: Waiter, waiters: Sequence[Waiter]) -> Waiter   # order = min(order of waiters) - 1
```

**Failing tests (one per behaviour):**
- `test_fifo_head_admitted_when_one_free`: 3 waiters and 1 free slot. Only the earliest gets `admit`; the others get `wait` with ranks 1 and 2.
- `test_two_free_admits_ranks_0_and_1`: rank 1 is admitted even while rank 0 hasn't polled yet.
- `test_bump_moves_to_front`: after `bumped`, the bumped waiter ranks 0. `bumped` on the current head still sets its order to `min - 1`.
- `test_force_admits_without_slot_even_when_full_and_paused`: returns `Verdict("force")`, and `admitted(...)` gives `slot=None, forced=True`.
- `test_forced_and_cancelled_waiters_do_not_take_rank`: see R7.
- `test_cancel_wins_over_force`
- `test_paused_blocks_non_forced`
- `test_shrink_overflow_counts_as_held`: `n=2, held={1,2}` gives no free slots; `n=1, held={0,1}` gives no free slots; `n=3, held={3}` gives `[0,1]`.
- `test_admit_takes_lowest_free_index`

**Verify:** `python3 tests/test_heavyq.py`

### Step 3: Store I/O: locking, reading, pruning and slot probing
**Files:** `bin/heavyq.py`, `tests/test_heavyq.py`

```python
class Busy(Exception): ...

class Store:
    def __init__(self, paths: Paths, *, alive: Callable[[int], bool] = pid_alive,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep)
    def ensure(self) -> None                    # mkdir home, queue/, running/, slots/ (mode 0o700); OSError propagates
    @contextmanager
    def locked(self, timeout: float = 0.0) -> Iterator[None]
        # LOCK_EX|LOCK_NB on home/lock, retried every 10 ms until timeout; raises Busy. fd O_CLOEXEC.
    def prune(self) -> list[str]                # under lock: drop dead-pid and corrupt entries and all *.tmp; return corrupt names
    def waiters(self) -> list[Waiter]           # skips dotfiles; corrupt/vanished files skipped (FileNotFoundError tolerated)
    def holders(self) -> list[Holder]
    def get_waiter(self, pid) -> Waiter | None
    def get_holder(self, pid) -> Holder | None  # lock-free read is safe (atomic rename)
    def put_waiter(self, w) / put_holder(self, h) / drop_waiter(self, pid) / drop_holder(self, pid)
    def paused(self) -> bool / set_paused(self, on: bool)
    def slots(self) -> int / set_slots(self, n: int)    # set: max(1, n), atomic, mkdir parents
    def held_slots(self, n: int) -> frozenset[int]
        # probe 0..n-1 plus existing numeric slot files >= n: LOCK_EX|LOCK_NB, success -> unlock+close (free)
    def take_slot(self, i: int) -> int | None   # os.open(O_RDWR|O_CREAT|O_CLOEXEC), LOCK_EX|LOCK_NB; fd or None

def pid_alive(pid: int) -> bool                 # see R1 for zombie handling
```

**Invariant (document it in the module docstring):** slots are acquired only inside `Store.locked()`. Holders release only by closing the fd or by dying. Therefore, within one lock section, `held_slots` followed by `take_slot` cannot be beaten.

**Failing tests:**
- `test_prune_removes_dead_and_corrupt_keeps_live`: `alive` is injected. A corrupt file comes back in the returned list.
- `test_prune_removes_stale_tmp_files`
- `test_reader_tolerates_entry_vanishing_mid_listdir`: delete a file between the patched `iterdir` and `open`.
- `test_held_slots_sees_other_holder_and_does_not_disturb_it`: the test holds slot 0 on its own fd. The probe returns `{0}`, and slot 0 is still held afterwards.
- `test_held_slots_counts_held_index_beyond_n`
- `test_take_slot_fd_not_inheritable`: `os.get_inheritable(fd) is False`.
- `test_locked_raises_busy_when_held_elsewhere`: the test holds `lock` on a second fd. The fake clock and sleep advance past the timeout, and `Busy` is raised.
- `test_ensure_fails_on_uncreatable_home`: `HEAVY_HOME = <file>/sub` raises `OSError`.
- `test_set_slots_min_one_and_atomic`

**Verify:** `python3 tests/test_heavyq.py`

### Step 4: Queue transactions and control mutations
**Files:** `bin/heavyq.py`, `tests/test_heavyq.py`

Each transaction takes the lock internally.

```python
@dataclass(frozen=True)
class Admitted:
    holder: Holder
    slot_fd: int | None

@dataclass(frozen=True)
class Waiting:
    rank: int | None              # None when paused, or when the lock was busy

class Cancelled: ...
Step = Admitted | Waiting | Cancelled

def enqueue(store, me: Waiter) -> None                         # blocking-with-timeout lock
def admit_step(store, me: Waiter, now: float) -> Step
    # lock(timeout=0) -> Busy => Waiting(None)
    # prune; if own entry missing -> re-put `me` (self-heal); re-read own entry (flags come from file)
    # n = store.slots(); held = store.held_slots(n) (skip probe when verdict is cancel/force)
    # decide -> on admit: fd = take_slot(v.slot) (None => Waiting); drop_waiter; put_holder(admitted)
    # on cancel: drop_waiter
def leave_queue(store, pid) -> None                            # signal while waiting
def finish(store, pid) -> None                                 # drop holder (slot fd closed by caller after)
def record_progress(store, base: Holder, last_progress: float) -> bool
    # lock(timeout=0); merge onto CURRENT file (preserves kill flag); missing -> put base+progress; Busy -> False
def kill_requested(store, pid) -> bool                         # lock-free read

Outcome = Literal["ok", "gone", "busy"]
def force(store, pid, token) -> Outcome      # waiter must exist with enqueued == token
def cancel(store, pid, token) -> Outcome
def bump(store, pid, token) -> Outcome
def kill(store, pid, token) -> Outcome       # holder must exist with started == token
def toggle_pause(store) -> bool | Literal["busy"]
def change_slots(store, delta: int) -> int | Literal["busy"]

@dataclass(frozen=True)
class QueueState:
    waiters: tuple[Waiter, ...]   # ranked; forced/cancelled pending appended after ranked ones
    holders: tuple[Holder, ...]
    n: int
    held: frozenset[int]
    paused: bool
    corrupt: tuple[str, ...]

def read_state(store, timeout: float = 1.0) -> QueueState      # prune + read + probe, one lock section
```

**Failing tests:**
- `test_admit_step_admits_and_moves_entry`: `queue/<pid>.json` is gone, `running/<pid>.json` exists with `slot=0`, and the fd holds the slot.
- `test_admit_step_waits_behind_earlier_waiter`
- `test_admit_step_self_heals_missing_entry`: delete the own entry. The next step re-creates it with the same order and enqueued values, then waits or admits normally.
- `test_admit_step_cancel_removes_entry`
- `test_admit_step_busy_lock_is_waiting`
- `test_slot_freed_when_holder_fd_closed`: the test holds slot 0, so the waiter waits. Close the fd and the waiter is admitted.
- `test_record_progress_preserves_kill_flag`: set `kill` via `kill()`, call `record_progress`, and the file still has `kill: true`. This is the lost-update race.
- `test_controls_on_vanished_entry_are_gone`: `force`, `cancel` and `bump` on a missing waiter, and `kill` on a missing holder, all return `"gone"` and create no files.
- `test_controls_token_mismatch_is_gone`: the same pid with a different `enqueued`/`started` stands for pid reuse.
- `test_cancel_on_admitted_job_does_not_kill`: the waiter was admitted between render and confirm. `cancel` returns `"gone"` and the holder keeps `kill: false`.
- `test_bump_reorders`, `test_toggle_pause`, `test_change_slots_floor_one`
- `test_read_state_reports_corrupt_once_per_call`

**Verify:** `python3 tests/test_heavyq.py`

### Step 5: heavy-run supervise tick, kill flag and last_progress
**Files:** `bin/heavy-run`, `tests/test_heavy_run.py`

```python
TICK_SEC = 0.5
REMOTE_KILL_EXIT = 143
REMOTE_KILL_MSG = "heavy: killed from heavy-top"

class Watchdog:
    last_progress: float     # starts at start; set to now on a join, or when the per-sample gain
                             # >= idle_cpu * dt / idle_sec (gain > 0 when idle checks are disabled). See R8.

def supervise(cfg, *, clock, wait_exit, sampler, kill,
              kill_requested: Callable[[], bool] = lambda: False,
              on_progress: Callable[[float], None] = lambda t: None,
              tick: float = TICK_SEC)
    # next_sample = start + sample_sec; each loop wait_exit(max(0, min(tick, next_sample - clock())))
    # exit -> ("exit", code); kill_requested() -> kill(); ("remote", REMOTE_KILL_MSG)
    # clock() >= next_sample -> next_sample += sample_sec; observe; on_progress(dog.last_progress); watchdog kill as today
```

**Failing tests:**
- `test_kill_flag_seen_within_one_tick`: the flag flips at t=10.2 with `sample_sec=30`. Expect `("remote", msg)` and kill called at t ≤ 10.5.
- `test_sampling_cadence_unchanged_by_ticks`: the sampler is called at 30, 60, 90 and so on, not every tick.
- `test_on_progress_reports_watchdog_last_progress`
- `test_last_progress_*`: progress at a join; progress at gain at or above the kill rate; no progress below the rate; the disabled case uses gain > 0.
- Existing `Supervise` and `Idle` tests pass unchanged. `test_idle_kills_and_reports` must still kill at t=600.0, because 0.5 is exact in binary floating point.

**Verify:** `python3 tests/test_heavy_run.py`

### Step 6: heavy-run wait loop, signals and main wiring
**Files:** `bin/heavy-run`, `tests/test_heavy_run.py`

- **Header:** `sys.dont_write_bytecode = True` and `sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))`, both before `import heavyq`.

```python
CANCEL_EXIT = 125
CANCEL_MSG = "heavy: cancelled from heavy-top"
HOME_ERROR_EXIT = 2
WAIT_SIGNALS = (SIGINT, SIGTERM, SIGHUP)

class SignalLatch:                     # handler only records the signal; never raises
    def install(self) -> None
    def pending(self) -> int | None

def wait_for_slot(store, me: heavyq.Waiter, *, clock, sleep,
                  pending: Callable[[], int | None], tick: float = TICK_SEC
                  ) -> heavyq.Admitted | heavyq.Cancelled | int   # int = signal number
    # loop: sig = pending() -> leave_queue; return sig
    #       step = admit_step(store, me, clock()) -> Admitted/Cancelled return; Waiting -> sleep(tick)

def main(argv, env=os.environ, *, spawn=subprocess.Popen, clock=time.time,
         mono=time.monotonic, sleep=time.sleep) -> int
```

`main` runs these steps in order:
1. Usage check.
2. `load_config`.
3. `store = Store(Paths.from_env(env))` and `store.ensure()`. On `OSError`: print `heavy: cannot use HEAVY_HOME <path>: <strerror>` and return 2.
4. Install the latch, then `enqueue`.
5. Call `wait_for_slot`.
   - Signal: return 128 + sig.
   - Cancelled: print `CANCEL_MSG` and return 125.
6. Re-check `pending()` after admission. If a signal is pending: `finish`, close the fd, return 128 + sig.
7. `spawn(argv, preexec_fn=os.setpgrp)` with `close_fds` left at its default of True. Spawn happens **after** the lock is released.
   - Spawn `OSError`: `finish`, close the fd, return 127/126 as today.
8. Install the forwarding handlers. If the latch caught a signal between spawn and install, forward it.
9. Run `supervise`:
   - `kill_requested = lambda: heavyq.kill_requested(store, pid)`
   - `on_progress` converts the monotonic time to epoch and calls `record_progress` (best effort).
10. In a `finally` block: `finish(store, pid)`, then `os.close(slot_fd)`.
11. Exit code mapping:
    - `"remote"`: print the message and return 143.
    - `"killed"`: return 124.
    - otherwise: `exit_code(code)`.

**Failing tests:**
- `test_wait_admits_after_slot_frees`: the fake `sleep` closes the test-held slot fd on its second call. Admission happens on the third poll.
- `test_wait_cancel_returns_cancelled_and_removes_entry`
- `test_wait_signal_removes_entry_and_returns_signum`
- `test_wait_paused_keeps_waiting_until_force`
- `test_main_uncreatable_home_exits_2_without_spawning`: `spawn` is a spy that must not be called.
- `test_main_spawn_failure_releases_slot_and_entry`: `spawn` raises `FileNotFoundError`. Expect 127, and afterwards `running/` is empty and the slot can be locked.
- `test_main_remote_kill_exit_143`: a fake `spawn` returns an object whose `wait` never completes. The kill flag is pre-set in the holder file after admission, by hooking the fake `sleep` or `spawn`. Expect 143 and the message on stderr (`contextlib.redirect_stderr`).

**Verify:** `python3 tests/test_heavy_run.py && python3 tests/test_heavyq.py`

### Step 7: `bin/heavy` (sh) drops sem
**Files:** `bin/heavy`

- Remove `PARALLEL_HOME` and `sem`. Add `HEAVY_HOME="${HEAVY_HOME:-$HOME/.heavy}"`.
- `--status`: print `slots: N` as today, then for each `"$HEAVY_HOME"/running/[0-9]*.json` take `pid=${f##*/}; pid=${pid%.json}` and run `ps -o pid=,etime=,command= -p "$pid" | cut -c1-160`.
- Otherwise `exec "$(dirname "$(readlink -f "$0")")/heavy-run" "$@"`. Keep the no-args usage check in sh.
- Update the header comment: queue semantics, exit codes 125 and 143, `HEAVY_HOME`.
- No unit test (sh glue). It is covered by integration scenario Q9 and the CI smoke test.

**Verify:** `sh -n bin/heavy`, then `HOME=$(mktemp -d) bin/heavy sh -c 'echo hi; exit 3'; echo $?` should print `hi` and `3`.

### Step 8: heavy-top data source, snapshot and render
**Files:** `bin/heavy-top`, `tests/test_heavy_top.py` (rewritten as `unittest`)

- Same header as heavy-run: `dont_write_bytecode` plus the `sys.path` insert.
- Delete `SEM_*`, `holder_pids`, `user_command`, `cwd_of`/`lsof` and the stale-slot rendering. The cwd now comes from the entries.
- Keep `parse_etime`, `parse_ps`, `tree_cpu` and the rendering helpers.

```python
@dataclass(frozen=True)
class Row:
    kind: Literal["run", "wait"]; pid: int; token: float; cmd: str; cwd: str
    elapsed: int; cpu: float; tags: tuple[str, ...]; rank: int | None

@dataclass(frozen=True)
class Snapshot:
    slots: int; held: int; forced: int; paused: bool
    running: tuple[Row, ...]; waiting: tuple[Row, ...]
    load: tuple[float, float, float]; cores: int
    def rows(self) -> tuple[Row, ...]          # running then waiting: selection order

IDLE_HINT_SEC = 60

def build_snapshot(state: heavyq.QueueState, procs: dict[int, Proc],
                   load, cores, now: float) -> Snapshot
    # run: elapsed=now-started, cpu=tree_cpu(pid), tags: "forced", f"idle {fmt}" if now-last_progress>=IDLE_HINT_SEC
    # wait: rank 1-based in queue order, elapsed=now-enqueued, tags: "bumped" if order<enqueued
    #       (pending force/cancel rows tagged "forcing"/"cancelling", listed after ranked rows)

def probe(store, now) -> tuple[Snapshot, tuple[str, ...]]
    # read_state (lock) then ps outside the lock; Busy -> previous snapshot + note

def render(snap, history, width, interval, color=True, max_rows=None,
           selected: tuple[str, int] | None = None, footer: str | None = None) -> str
```

- Header: a `PAUSED` banner when paused. The slots line shows `held/N` plus ` +K forced` when K > 0.
- Free rows: `max(0, N - held)`.
- The waiting header drops "(sem has no strict FIFO)".
- The selected row's first line is rendered in reverse video. There is no highlight when `color=False`.
- The footer lists the keys, or shows the confirm prompt or note.

**Failing tests:**
- `test_snapshot_ranks_waiters_in_queue_order_with_rank_numbers`
- `test_snapshot_elapsed_from_injected_now`
- `test_snapshot_tags_forced_bumped_idle`
- `test_tree_cpu_attributed_to_holder_pid`
- `test_render_paused_banner`
- `test_render_slots_held_and_forced`: renders `2/2` and `+1 forced`.
- `test_render_free_rows_count`
- `test_render_once_plain_has_new_fields`: no ANSI escapes; contains rank, `forced`, `bumped` and `idle`.
- `test_render_caps_to_max_rows`: kept from the current test.
- `test_render_selected_row_reverse_video`

Styling details (colours, column widths) are UI styling and are exempt from TDD.

**Verify:** `python3 tests/test_heavy_top.py`

### Step 9: heavy-top selection, keys, confirm and actions
**Files:** `bin/heavy-top`, `tests/test_heavy_top.py`

```python
def decode_keys(data: bytes) -> list[str]
    # "\x1b[A"/"\x1bOA" -> "up", "\x1b[B"/"\x1bOB" -> "down"; lone ESC -> "esc"; other escapes dropped

@dataclass(frozen=True)
class Action:
    verb: Literal["force", "kill", "cancel", "bump", "pause", "slots"]
    kind: str | None = None
    pid: int | None = None
    token: float | None = None
    delta: int = 0
    label: str = ""

@dataclass(frozen=True)
class UiState:
    selected: tuple[str, int] | None
    interval: float
    confirm: Action | None = None
    note: str = ""

@dataclass(frozen=True)
class KeyResult:
    ui: UiState
    action: Action | None
    quit: bool
    refresh: bool

def handle_key(ui: UiState, key: str, snap: Snapshot) -> KeyResult
def reconcile(ui: UiState, snap: Snapshot) -> UiState
    # keep selection by (kind,pid); if gone clamp to same index; drop confirm whose target vanished (note)
def apply_action(store, a: Action) -> str      # calls heavyq control, maps Outcome -> footer note
```

Key map:

| Key | Behaviour |
|---|---|
| `up` / `k`, `down` / `j` | move the selection |
| `f` | waiter rows only |
| `b` | waiter rows only |
| `x` | running rows only; sets `confirm` with label `kill <cmd>? y/n` |
| `c` | waiter rows only; sets `confirm` |
| `y` while confirming | emits the action |
| any other key while confirming | clears the prompt with the note "kept"; it does not quit, even `q` |
| `p` | pause/resume |
| `+` / `=`, `-` / `_` | slot delta ±1 |
| `[` / `]` | interval ∓1, clamped to 1..30 |
| `r` | refresh |
| `q` / Ctrl-C | quit |
| wrong-kind key (for example `x` on a waiter) | note only, no action |

`run_tui` changes:
- The inner loop feeds each decoded key through `handle_key`.
- On an action it runs `apply_action`, sets the note and refreshes immediately.
- After each probe it runs `reconcile`.
- Corrupt-entry warnings are shown once per name (a session set) through the note.
- `SIGTSTP`/`SIGINT` are masked with `signal.pthread_sigmask` around `read_state` and `apply_action`, so Ctrl-Z can't stop the TUI while it holds `lock` (R4).

**Failing tests:**
- `test_decode_arrows_both_modes`
- `test_selection_moves_across_running_and_waiting`
- `test_key_to_action_mapping`: a table of key × row kind giving the expected verb or note.
- `test_kill_requires_confirm_y`
- `test_confirm_cancelled_by_other_key_including_q`
- `test_reconcile_keeps_selection_by_pid_and_clamps_when_gone`
- `test_reconcile_drops_confirm_when_target_vanished`
- `test_apply_action_gone_sets_note`: real `Store` on a temp home; `force` on a pid that isn't present.
- `test_brackets_change_interval_plus_minus_change_slots`

**Verify:** `python3 tests/test_heavy_top.py`

### Step 10: Integration tests
**Files:** `tests/integration_heavy_run.sh` (isolate it), `tests/integration_queue.sh` (new), `tests/qctl.py` (new test helper). See Section 3.

**Verify:** `sh tests/integration_heavy_run.sh && sh tests/integration_queue.sh`

### Step 11: install.sh, CI and README
See Section 4.

**Verify:**
- `sh -n install.sh`
- `PREFIX=$(mktemp -d) ./install.sh && $PREFIX/bin/heavy true`
- `PREFIX=$(mktemp -d) ./install.sh --link && $PREFIX/bin/heavy true && $PREFIX/bin/heavy --top --once`
- Push the branch and confirm CI is green on both OSes.

### Step 12: Integration gate
1. Check the load average, then run all unit tests and both integration scripts directly. They run in seconds, so they don't need `heavy`.
2. Get a fresh-context review of the full diff with the severity bar stated: fix P0/P1 and anything a realistic workload hits; record R-class edge cases as accepted risks.
3. On Evan's host: `./install.sh --link`, then watch a real `heavy --top` while forcing, bumping and killing jobs. This is the "done means watching it move" check.

---

## 2. Module boundaries

| `bin/heavyq.py` (shared, no terminal I/O, no process spawning) | `bin/heavy-run` | `bin/heavy-top` |
|---|---|---|
| `Paths.from_env`, `HEAVY_HOME` and slots-file locations | Config/env parsing, the watchdog (unchanged rules, plus `last_progress`) | `ps` parsing and `tree_cpu`; load history |
| `Waiter`/`Holder` models, JSON validation | `supervise`: 0.5 s tick, kill-flag poll, sample cadence, `on_progress` hook | `build_snapshot` (pure: QueueState + procs + now → Snapshot) |
| `atomic_write_json`; `Store` (lock, read, prune, probe/take slot with CLOEXEC, pause, slots) | `wait_for_slot` loop (tick, `sleep`, signal latch) | `render` (header, PAUSED, `held/N +K forced`, tags, highlight, footer) |
| **Pure:** `rank`, `free_slots`, `decide`, `admitted`, `bumped` | `SignalLatch` (waiting) vs forwarding handlers (running), and the handoff between them | `decode_keys`, `handle_key`, `reconcile` (pure); confirm-prompt state |
| **Transactions:** `enqueue`, `admit_step`, `leave_queue`, `finish`, `record_progress`, `kill_requested`, `read_state` | Spawn/kill-tree plumbing; mapping outcomes to exit codes (0..255, 124, 125, 143, 128+sig, 2) | `apply_action` → heavyq control → footer note |
| **Controls:** `force`, `cancel`, `bump`, `kill`, `toggle_pause`, `change_slots`, all returning `ok`/`gone`/`busy` | Prints `CANCEL_MSG`/`REMOTE_KILL_MSG`/`HEAVY_HOME` errors | Terminal mode, SIGWINCH/TSTP/CONT, signal masking around lock sections |

Rules:
- The decision logic is pure, and every I/O seam is injected (`alive`, `clock`, `sleep`, `spawn`, `mono`).
- Unit tests use a temp `HEAVY_HOME` and fake time.
- **No component ever sends a signal to a pid read from a state file.** heavy-run only signals its own child tree, and the TUI only writes flags. A corrupt or forged entry therefore can't redirect a kill.

---

## 3. Integration tests

**`tests/integration_heavy_run.sh` (existing).** At the top, add `tmp=$(mktemp -d)`, `export HOME="$tmp" HEAVY_HOME="$tmp/q"` and a trap that runs `rm -rf`. Without this, the watchdog cases would join the developer's real `~/.heavy` queue and could block behind live jobs on the dev host. The cases themselves are unchanged.

**`tests/qctl.py`.** About 25 lines. Usage: `qctl.py force|cancel|bump|kill <pid>` and `qctl.py state`. It builds a `Store` from the environment, reads the entry to get its token, calls the heavyq control and exits 0 on `ok`, 1 otherwise. `state` prints `queue/running` pids, so the shell can poll.

**`tests/integration_queue.sh`.** POSIX sh with `set -u`.

Shared helpers:
- `until_ <cond> [tries=40]` polls `eval "$cond"` every 0.1 s and fails on timeout, so the cap is about 4 s.
- `scen` starts a scenario with a fresh `HEAVY_HOME` under `$tmp/<name>` and writes slots=1 to `$HOME/.config/heavy/slots`.
- Long-lived holders are **gate-controlled**, never timed: `sh -c 'echo $$ > $T/a.pid; while [ ! -e $T/a.go ]; do sleep 0.1; done'`. Releasing a holder is `touch $T/a.go`. Every admission therefore costs at most one 0.5 s poll, and no assertion depends on a sleep duration.
- A trap kills leftover background jobs and orphans (pids recorded in files) and removes `$tmp`.

| # | Scenario | Assertions | Cost |
|---|---|---|---|
| Q1 | Waits, then force | A holds (wait for `running/$A.json`). B (`touch $T/b.ran; gate b`) is started and `queue/$B.json` appears. After 0.6 s, `b.ran` is absent. `qctl force $B`, then `until_ b.ran`. `running/$B.json` has `"slot": null` and `"forced": true`. Release both; B exits 0 and A exits 0. | ~1.5 s |
| Q2 | Kill flag → 143, tree gone | A runs `sh -c 'echo $$ > $T/c.pid; exec sleep 30'`. `qctl kill $A`; `wait $A` gives rc 143; stderr is exactly `heavy: killed from heavy-top`; `kill -0 $(cat c.pid)` fails. | ~1 s |
| Q3 | Cancel → 125 | A holds and B waits. `qctl cancel $B`; rc 125; stderr `heavy: cancelled from heavy-top`; B's marker was never created; `queue/$B.json` is gone. | ~1 s |
| Q4 | Holder SIGKILLed frees its slot | A holds (child pid recorded) and B waits. `kill -9 $A`; B is admitted within ≤1.5 s. Clean up A's orphan child by pid. A is not reaped until `wait $A` (zombie), which also exercises R1 for holders: the slot frees even though A is a zombie. | ~1 s |
| Q5 | No slot fd leak into a daemon (CLOEXEC) | A runs `sh -c 'sleep 30 & echo $! > $T/orphan'` and exits 0 straight away, leaving the background `sleep` alive. B must be admitted within ≤1.5 s. Kill the orphan. | ~1 s |
| Q6 | FIFO of three waiters | A holds. Start W1, wait for its queue entry, then W2, then W3. Each runs `sh -c 'echo Wn >> $T/order'`. `touch a.go`, wait for all three; `order` is exactly `W1 W2 W3`. | ~2 s |
| Q7 | TERM while waiting | A holds and B waits. `kill -TERM $B`; rc 143; `queue/$B.json` is gone; B's marker is absent. TERM is used rather than INT because sh starts background jobs with SIGINT ignored; the INT path is unit-tested. | ~0.6 s |
| Q8 | Fail closed | `HEAVY_HOME=/dev/null/x bin/heavy-run touch $T/nope` gives rc 2, and `nope` is absent. | <0.2 s |
| Q9 | Passthrough via `bin/heavy` | `echo hi \| bin/heavy cat` prints `hi`; `bin/heavy sh -c 'exit 3'` gives rc 3; `bin/heavy sh -c 'kill -9 $$'` gives rc 137; `bin/heavy --status` lists a gated holder's pid. | ~0.5 s |

The script totals about 9 s. The existing watchdog script runs as well, for about 12 s; most of that is its 6 s churn case.

---

## 4. install.sh, CI and README

**install.sh**
- Remove the `sem` check. python3 becomes a requirement, with an `exit 1` message `heavy needs python3 (3.10+)` (see R12).
- `FILES="heavy heavy-top heavy-run heavyq.py"`. Use it in the checkout detection (`-f "$here/bin/heavyq.py"`), the curl download loop and the install loop.
- Copy mode: `chmod 755` the three scripts and `chmod 644` `heavyq.py`.
- `--link` mode symlinks all four. `rm -f` first also clears a stale copied `heavyq.py`. Resolution doesn't depend on the `heavyq.py` link: the scripts insert `dirname(realpath(__file__))`, which is the checkout's `bin/`. CPython also resolves a symlinked script's directory for `sys.path[0]`; the explicit insert keeps it deterministic when the scripts are loaded through `SourceFileLoader`.
- Success message: `installed heavy, heavy-top, heavy-run, heavyq.py -> $bin`.
- Optional: remove a stale `$bin/__pycache__/heavyq.*.pyc`. With `dont_write_bytecode` there shouldn't be one.

**CI (`.github/workflows/ci.yml`)**
- Delete the "Install GNU parallel" step.
- Unit tests: add `python3 tests/test_heavyq.py`.
- Integration: run `sh tests/integration_heavy_run.sh` and `sh tests/integration_queue.sh`, renaming the step to "Integration tests".
- Shell syntax: add `sh -n tests/integration_queue.sh`.
- Smoke test: keep it, and add `cmp "$HOME/.local/bin/heavyq.py" bin/heavyq.py`.
- New step "Link install": `PREFIX="$RUNNER_TEMP/link" ./install.sh --link`, then `test -L "$RUNNER_TEMP/link/bin/heavy-run"`, `"$RUNNER_TEMP/link/bin/heavy" sh -c 'exit 4'; test $? -eq 4` and `"$RUNNER_TEMP/link/bin/heavy" --top --once`.
- One-line install: add `cmp "$RUNNER_TEMP/pipe/bin/heavyq.py" bin/heavyq.py`.

**README**
- Intro and Install: drop GNU parallel and `brew install parallel`. Requirements become Python 3.10+ only. The install text lists four files.
- `heavy --top` section: a new sample (PAUSED banner, `held/N +K forced`, ranked waiters, `forced`/`bumped`/`idle` tags, footer keys) and a key table matching the spec. Interval keys are now `[`/`]`. `--once` shows the same fields.
- Usage: document exit codes 125 (cancelled) and 143 (killed from heavy-top), and that Ctrl-C while waiting leaves the queue and exits 130.
- Slots: changes take effect live (`+`/`-` in the TUI, or edit the file). Shrinking never stops running jobs.
- How it works: rewrite around `$HEAVY_HOME` (state table, flock slots freed by the kernel on death, strict FIFO plus bump, 0.5 s polling). Remove the sem and `PARALLEL_HOME` text and the "no strict FIFO" caveat. Keep the per-user caveat. Add "HEAVY_HOME must be on a local filesystem" (R2).
- Migration: jobs already waiting in the old sem queue still run, but don't count against the new slots, so the host may be briefly oversubscribed. `~/.parallel` can be deleted afterwards.
- Watchdog section: wording unchanged. Note that the watchdog clock starts at admission.
- Tests section: add `test_heavyq.py` and `integration_queue.sh`.

---

## 5. Risks and open questions

**R1. Zombie waiters can block the head of the queue (P1; needs a decision before Step 1).**
- `os.kill(pid, 0)` succeeds on a zombie. A waiter that is SIGKILLed while its parent never reaps it keeps a live-looking `queue/<pid>.json` at rank 0, and the queue stalls until the parent exits.
- The TUI can't clear it either, because `cancel` only works when the waiter acts on it.
- Holders aren't affected: the kernel closes a zombie's fds, so its slot frees. Only the display lingers.
- The options:
  - **(a) Recommended: flock-based liveness.** Each heavy-run holds `flock(LOCK_EX)` on `$HEAVY_HOME/live/<pid>` (O_CLOEXEC) for its whole life, waiting and running. Prune calls an entry dead when that file can be `LOCK_NB`-locked or is missing. This costs no subprocess, is identical on macOS and Linux, catches zombies, and also removes the pid-reuse risk the spec accepted. It adds one directory to the spec's state table.
  - **(b) Within the spec: zombie-aware `pid_alive`.** On Linux, read `/proc/<pid>/stat` and check for state `Z`. On macOS, run one `ps -o pid=,stat= -p <pids>` per prune. That is a subprocess at about 2 Hz per waiter.

  The plan keeps liveness behind `Store(alive=...)` so either option slots in. I need Evan's pick.

**R2. flock semantics on macOS and Linux.**
- Both platforms use BSD-style `flock(2)` (Python's `fcntl.flock`). Locks belong to the open file description and are released when its last fd closes, including at process death.
- Two `open()`s in one process conflict. The unit tests rely on this, and the TUI's probes count correctly because of it.
- They differ on network filesystems. Linux emulates `flock` over NFS with POSIX byte-range locks, which are per-process: a process never conflicts with itself, and fork semantics differ. macOS has its own behaviour on SMB and NFS.
- Decision: a local filesystem is required, documented in the README and not detected at runtime.
- EINTR is not a concern: all locking is `LOCK_NB` polling, so a signal never leaves a blocked `flock` uninterruptible (see R4).

**R3. fd inheritance (decided: close-on-exec everywhere).**
- Slot, lock and live fds are opened with `os.open(..., O_CLOEXEC)`. That is already Python's default since PEP 446; it is passed explicitly and asserted with `os.get_inheritable(fd) is False`.
- `Popen` keeps `close_fds=True`. Never use `pass_fds`, and never call `os.fork()` without exec.
- Between fork and exec, the child briefly holds the fd, and exec drops it. A forked daemon in the job's tree therefore can't hold the slot once heavy-run exits. Integration case Q5 guards this.

**R4. A process stopped while holding `lock`.**
- If a lock holder is SIGSTOPped, for example by Ctrl-Z in the TUI mid-section, every blocking locker would hang. That includes heavy-run's supervise loop, which would stall kill flags and the watchdog.
- Mitigations: no code path blocks on `lock`.
  - Waiters try `LOCK_NB` once per tick and skip the tick if it's busy. FIFO is unaffected because ranking doesn't depend on who polls first.
  - `record_progress` is best-effort and non-blocking.
  - The TUI polls for up to 1 s, then shows "queue busy", and masks SIGTSTP/SIGINT inside its lock sections.
  - `kill_requested` reads lock-free.

**R5. Races between prune and admission.**
- Every write, prune and slot acquisition happens under `lock`, so prune and admission are serialized. Pid-liveness pruning can only remove entries whose owner is dead (modulo R1 and pid reuse).
- Slot truth is the flocks, never the entries. A stale running entry therefore can't block admission, and a kernel-released slot can only make the queue freer between probe and take.
- If a live waiter finds its own entry missing (it was corrupt and got pruned, or someone ran a manual `rm`), it re-creates the entry from memory with the original `order` and `enqueued`. A flag set in the lost file is lost too (accepted).
- Temp files are only created under `lock`, so prune can delete every `*.tmp` it sees.
- The kill flag versus `last_progress` lost-update race is closed by doing both as read-merge-writes under `lock` (test in Step 4).

**R6. TUI entries that vanish between render and keypress.**
- Actions carry `(kind, pid, token)`, where the token is `enqueued` for waiters and `started` for holders.
- The heavyq control revalidates all three under `lock` and returns `gone` on any mismatch. The footer then says "job N no longer waiting/running".
- `cancel` on a waiter that was admitted in the meantime never escalates to a kill.
- `reconcile` keeps the selection by pid, clamps to the same index when the pid disappears, and drops a pending confirm whose target is gone.
- Confirm prompts revalidate when `y` is pressed, not when they open.

**R7. Ambiguity: do forced or cancelled waiters hold a rank?**
- The plan excludes them from ranking. Otherwise a forced waiter at the head would delay others by a poll, and one stuck while stopped would block them.
- They are shown after the ranked rows with `forcing`/`cancelling` tags.

**R8. Ambiguity: the definition of `last_progress`.**
- The plan defines it as the last sample where a new pid joined, or where the per-sample CPU gain was at least the rate the watchdog requires (`idle_cpu * dt / idle_sec`); when idle checks are disabled, any gain counts.
- So "idle 6m" means "below the kill rate for 6 minutes", which tracks the watchdog.
- The TUI shows the hint once it reaches 60 s or more. Flag this if Evan wants a different threshold.

**R9. A stopped (Ctrl-Z) waiter at rank 0 with one free slot blocks the queue.** This is accepted. The owner can bump or force others. It differs from R1: a stopped process is alive, and cancel takes effect once it resumes.

**R10. Migration double-queue.**
- A job that is still waiting in the old sem queue will, once sem admits it, exec the **new** `heavy-run` (same path) and wait again in the new queue.
- That is harmless but surprising. It goes in the README migration note and needs no code.

**R11. Clock.**
- `enqueued`, `order` and `started` are epoch times, because they must be shared across processes. An NTP step backwards could rank a new waiter ahead of older ones. Accepted.
- Durations shown in the TUI use the same epoch, so a step backwards can briefly show negative durations. Clamp them at 0.

**R12. Python is now on the core path.**
- `heavy` itself now needs python3 3.10+. On macOS, `/usr/bin/python3` from the Command Line Tools is 3.9.
- The plan makes `install.sh` fail hard without python3. The plan does **not** add a runtime version guard; that would be a small addition if wanted.
- `sys.dont_write_bytecode = True` stops `__pycache__/` from appearing in `~/.local/bin`.

**R13. `heavy --status` etime.** It still prints `ps` etime, which includes wait time, as today. The command column now shows `python3 …/heavy-run <cmd>` instead of sem's line. "As today" in the spirit of the spec; flag it if Evan wants it reformatted.

**R14. Unspecified: HEAVY_HOME failure in heavy-top.** The spec only defines HEAVY_HOME failure for heavy-run. The plan makes heavy-top print the same error and exit 2.
---

## Orchestrator decisions (2026-11-01)

- **R1: option (a), flock-based liveness.** Each heavy-run holds `flock(LOCK_EX)` on `$HEAVY_HOME/live/<pid>` (O_CLOEXEC) for its whole life. Prune treats an entry as dead when that file is missing or can be locked with `LOCK_NB`. `Store(alive=...)` stays injectable for the unit tests. This adds `live/` to the spec's state table; it's an implementation detail that also removes the pid-reuse risk.
- **R7, R8, R11, R13, R14:** accepted as the plan proposes.
- **R12: add a runtime guard.** heavy-run and heavy-top check `sys.version_info >= (3, 10)` before any 3.10-only syntax is parsed. That means keeping the entry scripts 3.9-parseable up to the check, or doing the check in a tiny top section before the imports. On failure they print `heavy: needs python3 >= 3.10 (found X.Y at <sys.executable>)` and exit 2. Reason: agent shells whose PATH lacks Homebrew resolve `/usr/bin/python3`, which is 3.9.6 on this host.
- **Step 11 "push the branch":** don't push. The orchestrator pushes after the review.

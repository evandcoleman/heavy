# Own queue + heavy-top job control

Status: draft for approval · 2026-11-01

## Goal

From `heavy --top`, the owner can force-run a waiting job, kill a running job, cancel a waiter, bump a waiter to the front, pause/resume the queue, and change the slot count live. GNU `sem` can't support force/bump/cancel (a waiter is stuck inside sem's polling loop), so heavy gets its own queue and drops the GNU parallel dependency.

## Non-goals

- Multi-user or multi-host queues. One user, one host.
- Strict protection against pid reuse in stale-entry pruning (accepted risk).
- Changing the CLI: `heavy <cmd> [args...]`, `heavy --status`, `heavy --top` keep their behavior. Stdio passthrough, exit codes, and the watchdog (commits dc441c5, 87d857c) stay unchanged.

## Architecture

`bin/heavy` (sh) keeps `--status`/`--top` and otherwise `exec`s `heavy-run "$@"`. `heavy-run` becomes queue + supervisor: it waits for admission, then runs the existing watchdog/supervise path. The watchdog clock starts at admission.

The shared queue logic lives in one stdlib module, `bin/heavyq.py`, imported by both `heavy-run` and `heavy-top`. Each script adds its own real directory to `sys.path`, and `install.sh` installs/links the module next to the scripts.

### State: `$HEAVY_HOME` (default `~/.heavy`)

| Path | Meaning |
|---|---|
| `lock` | `flock` held briefly for every queue read-modify-write |
| `slots/<i>` | slot lock files. A holder keeps `flock(LOCK_EX)` on one for the job's life, so the kernel frees it when the process dies (no stale slots) |
| `queue/<pid>.json` | waiter: `pid, cmd, cwd, enqueued (epoch), order (float), force, cancel` |
| `running/<pid>.json` | holder: `pid, cmd, cwd, started, slot (int or null if forced), forced, kill` |
| `paused` | exists → no new non-forced admissions |
| `~/.config/heavy/slots` | slot count (unchanged location), re-read on every admission check |

Writes are atomic (temp file + `rename`). An entry whose pid is dead is pruned by whichever process next takes `lock`.

### Admission (heavy-run, poll every 0.5 s while waiting)

Under `lock`:
1. Prune dead entries.
2. If the own entry has `cancel` → remove it, print `heavy: cancelled from heavy-top` to stderr, exit **125**.
3. If `force` → admit without a slot (`slot: null, forced: true`).
4. Else, if not paused: rank the live waiters by `(order, enqueued)`. `free` = number of slot indices in `0..N-1` this process can `flock(LOCK_NB)`. If its rank < `free`, it takes the lowest free slot and is admitted.
5. On admission: delete `queue/<pid>.json`, write `running/<pid>.json`.

New waiters get `order = enqueued`. FIFO is strict except for bumps. When slots shrink live, running jobs continue and admission waits until fewer than N are held. Held slots with index ≥ N still count as held.

Ctrl-C/TERM/HUP while waiting → remove the entry and exit 130/143/129. While running, signals forward as today.

### Control (heavy-top actions; all under `lock`, all just mutate state)

| Key | Action | Effect |
|---|---|---|
| `↑/↓` or `j/k` | select row (across running + waiting) | — |
| `f` | force (waiter) | `force: true` → starts within ~0.5 s, shown as `forced` |
| `x` | kill (running) | asks `kill <cmd>? y/n`, then sets `kill: true`. heavy-run's supervise loop checks the flag each wait tick (≤0.5 s), runs the existing TERM→10 s→KILL sequence, prints `heavy: killed from heavy-top`, exits **143** |
| `c` | cancel (waiter) | asks for confirmation, then sets `cancel: true` |
| `b` | bump (waiter) | `order = min(order of waiters) − 1` |
| `p` | pause/resume | toggles `paused` |
| `+` / `-` | slots ±1 | writes `~/.config/heavy/slots` (min 1) |
| `[` / `]` | refresh interval ∓1 s | moved from `+/-` |
| `q` | quit | — |

The supervise loop's wait granularity drops to 0.5 s ticks so kill flags are seen promptly. CPU sampling stays on `HEAVY_SAMPLE_SEC`.

### TUI display changes

- The data source becomes the state dir plus `ps` (for per-tree CPU), no longer sem files or `ps` command-line parsing.
- Header: `PAUSED` banner when paused; slots line shows `held/N` plus `+K forced` when oversubscribed.
- Running rows: elapsed, tree CPU%, a `forced` tag, and an idle hint (`idle 6m` when the watchdog's trailing window shows no progress). heavy-run writes `last_progress` (epoch) into its running entry on each sample.
- Waiting rows: strict queue order (rank #), waited time, `bumped` tag if order < enqueued.
- Selected row highlighted; footer lists the keys. Confirm prompts render in the footer.
- `--once` output gains the same fields (plain text), so agents can read it.

`heavy --status` reads `running/` and prints `pid etime command`, as today.

## Migration

The live host has jobs inside the old sem queue. After install, new jobs use the new queue, and old sem holders don't count against slots, so the host is briefly oversubscribed (accepted). README drops the `parallel` dependency. CI drops the `parallel` install.

## Error handling

- Unreadable/corrupt entry JSON → treat as dead and prune it, warn once in the TUI footer.
- `HEAVY_HOME` not creatable → heavy-run prints the error and exits 2 without running the command (fail closed; never silently bypass the queue).
- TUI actions on an entry that vanished between render and keypress → no-op, footer note.

## Testing

- Unit (fake clock and temp `HEAVY_HOME`, no real sleeps): ranking/admission (FIFO, bump, force, pause, shrink slots, dead-entry pruning, cancel), state mutations from each TUI action, kill-flag handling in supervise, the render of new fields, and key dispatch → action mapping.
- Integration (real processes, temp `HEAVY_HOME`, a few seconds): with 1 slot, a second job waits, then `force` admits it; cancel → 125; kill flag → 143 and the tree is gone; slot freed when a holder is SIGKILLed; FIFO order of three waiters; stdin/exit passthrough still OK; the existing watchdog integration cases still pass.
- Existing `test_heavy_top.py` updated for the new data source.

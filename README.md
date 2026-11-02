# heavy

A machine-wide queue for CPU-heavy commands, plus a little TUI to watch it.

```sh
heavy npm run test:e2e      # waits for a free slot, then runs
heavy --top                 # live view of the queue
```

If you run several AI coding agents (or terminals, or CI runners) on one box, they
all decide to run the full test suite, a headless-browser walkthrough and a release
build at the same moment. Load goes to 40 on 14 cores, everything slows down,
timeouts fire, and agents retry, which makes it worse.

`heavy` puts a queue in front of those commands. Every caller on the host shares it,
so at most N heavy jobs run at once and the rest wait their turn, first in, first out.
Output, stdin and exit codes pass through unchanged, so you can wrap any command
without changing how you use it. From `heavy --top` you can force a job past the
queue, kill one, cancel or bump a waiter, pause the queue and change the slot count.

## heavy --top

```
heavy queue                                                             14:32:07 EDT
PAUSED — new jobs wait; forced jobs still start · p resumes
●● 2/2 slots +1 forced   waiting 2   load 18.4/15.2/11.9 on 14 cores
load ▂▂▃▃▄▄▄▅▅

╭─ running ────────────────────────────────────────────────────────────────────────╮
│ #     pid  elapsed     cpu  command                                              │
│ 1   41210   38m12s    360%  npm run test:e2e                                     │
│                             ~/src/web-app                                        │
│ 2   41388   19m45s     12%  xcodebuild -scheme App build [idle 6m]               │
│                             ~/src/ios-app                                        │
│ 3   41702    1m04s    540%  cargo test -p parser [forced]                        │
│                             ~/src/engine                                         │
╰──────────────────────────────────────────────────────────────────────────────────╯
╭─ waiting ────────────────────────────────────────────────────────────────────────╮
│  #    pid   waited  command                                                      │
│  1  41923    0m12s  pnpm test [bumped]                                           │
│                     ~/src/web-app                                                │
│  2  41850    2m40s  cargo build --release                                        │
│                     ~/src/engine                                                 │
╰──────────────────────────────────────────────────────────────────────────────────╯
↑↓ f force · x kill · c cancel · b bump · p pause · +/- slots · [/] 2s · q quit
```

- **running**: each job, how long it has run, CPU for its whole process tree (so a
  test run counts the browsers it spawned), and where it runs. `[forced]` jobs were
  started past the queue and don't hold a slot; `[idle 6m]` means the tree has stayed
  below the watchdog's idle rate for that long.
- **waiting**: the queue in order (`#` is the position). `[bumped]` jobs were moved to
  the front.
- The header shows `held/N slots`, plus `+K forced` when forced jobs oversubscribe the
  host, and a `PAUSED` banner while the queue is paused.
- **load**: 1/5/15-minute load against core count, with a history sparkline.

| Key | Action |
|---|---|
| `↑`/`↓` or `j`/`k` | select a job (running and waiting) |
| `f` | force the selected waiter to start now, without a slot |
| `x` | kill the selected running job (asks `y/n`); it exits `143` |
| `c` | cancel the selected waiter (asks `y/n`); it exits `125` without running |
| `b` | bump the selected waiter to the front of the queue |
| `p` | pause or resume the queue (forced jobs still start) |
| `+` / `-` | one more or one fewer slot (writes `~/.config/heavy/slots`, minimum 1) |
| `[` / `]` | shorter or longer refresh interval |
| `r` / `q` | refresh now / quit |

`heavy --top --once` prints a single plain-text snapshot with the same fields, which is
handy for scripts and agents.

## Install

Needs Python 3.10+ (stdlib only). No other dependencies.

```sh
curl -fsSL https://raw.githubusercontent.com/evandcoleman/heavy/main/install.sh | sh
```

That puts `heavy`, `heavy-top`, `heavy-run` and `heavyq.py` in `~/.local/bin`. Set `PREFIX` to install
somewhere else (`… | PREFIX=~/tools sh` installs to `~/tools/bin`), or `HEAVY_REF` to pin a tag or commit.

From a clone:

```sh
git clone https://github.com/evandcoleman/heavy.git
cd heavy && ./install.sh         # or --link to symlink, so `git pull` updates it
```

Works on macOS and Linux. On macOS, `/usr/bin/python3` is 3.9: `heavy` then exits `2`
with `heavy: needs python3 >= 3.10`, so make sure a newer `python3` comes first on `PATH`.

### Upgrading from the GNU parallel version

Earlier versions queued through GNU parallel's `sem`. Jobs already waiting in the old
`sem` queue still run, but they don't count against the new slots, so the host may be
oversubscribed for a while after you upgrade. Such a job then joins the new queue too,
once `sem` lets it through. Afterwards you can delete `~/.parallel` and uninstall GNU
parallel if nothing else uses it.

## Usage

```sh
heavy <cmd> [args...]     # run cmd once a slot is free
heavy --status            # list running jobs
heavy --top [-n SECONDS]  # TUI (alias for heavy-top)
```

The exit code is the command's own (`128+signal` if it died by signal), or:

| Code | Meaning |
|---|---|
| `124` | killed by the watchdog |
| `125` | cancelled from `heavy --top` while waiting; the command never ran |
| `143` | killed from `heavy --top` |
| `130` / `143` / `129` | Ctrl-C / TERM / HUP while waiting: it leaves the queue, the command never ran |
| `2` | `$HEAVY_HOME` can't be created or used; the command never runs |

Wrap the expensive things, not everything:

- full test suites, e2e and Playwright runs, screenshot sweeps
- Xcode, Swift, Rust and other big builds
- video or image renders

Quick targeted tests and linters don't need it.

### Slots

The slot count lives in `~/.config/heavy/slots` (a single integer, default `2`). It is
re-read on every admission check, so a change takes effect live: press `+`/`-` in
`heavy --top` or edit the file. Shrinking never stops running jobs; new ones wait until
fewer than N run. A good starting point is cores / 6. If cores sit idle while
jobs wait, raise it; if load stays high, more heavy work is running outside `heavy`.

### Watchdog

A hung job would hold its slot forever, so `heavy-run` watches every job and kills it
when either limit trips. Its clock starts when the job is admitted, not while it waits.

- **Idle:** the job's process tree gained less than `HEAVY_IDLE_CPU` CPU-seconds
  (default `5`) over the last `HEAVY_IDLE_MIN` minutes (default `10`), and no new
  process joined the tree in that window.
- **Wall cap:** the job has run longer than `HEAVY_MAX_MIN` minutes
  (default `90`).

Set a variable to `0` to disable that check; minutes may be fractional. Invalid values
warn on stderr and fall back to the default. Per call: `HEAVY_IDLE_MIN=0 heavy <cmd>`.
`HEAVY_SAMPLE_SEC` (default `30`) is the sampling interval, for tests and tuning only.

A kill sends SIGTERM to the job's process group and tracked descendants, SIGKILL after
10s, prints one line to stderr naming the limit that tripped, and exits `124`. Otherwise
the exit code is the job's own (`128+signal` if it died by signal).

Caveat: `ps` doesn't count the CPU of exited children, so a loop of short-lived
processes (bats, CLI test loops) looks CPU-idle. New processes appearing therefore count
as activity, which means a hung job that keeps spawning processes is never idle-killed;
only the wall cap catches it.

Caveat: the job tree is the launcher's descendants plus its process group. A process
reparented to init/launchd outside that group (for example a macOS XCTest app host) is
neither counted nor killed directly; the watchdog judges by the launcher's tree.

### Watching from another machine

```sh
alias htop-heavy='ssh -t buildbox ~/.local/bin/heavy --top'
```

Use the full path: a remote command over `ssh` doesn't load your interactive shell
config, so `~/.local/bin` is usually not on its `PATH`. Keep `-t`; the TUI needs a
terminal.

### Telling your agents

Add something like this to your agent instructions (`CLAUDE.md`, `AGENTS.md`, …):

> Run every CPU-heavy command through `heavy <cmd> [args...]`: full test suites,
> headless-browser runs, screenshot sweeps, Xcode/Swift builds and renders. It's a
> machine-wide queue that runs at most 2 at once; output and exit code pass through
> unchanged. Waiting for a slot counts against your command timeout, so run `heavy`
> jobs in the background. Don't use it for quick targeted tests or lint.
> `heavy --status` shows who holds the slots.

## How it works

`heavy` is a few lines of POSIX sh that exec `heavy-run`, which queues, then runs and
supervises the command. The queue state lives in `$HEAVY_HOME` (default `~/.heavy`);
both `heavy-run` and `heavy-top` use the same module, `heavyq.py`:

| Path | Meaning |
|---|---|
| `lock` | `flock` held briefly around every queue read-modify-write |
| `slots/<i>` | a running job keeps an `flock` on one for its whole life, so the kernel frees the slot when the process dies |
| `live/<pid>` | every `heavy-run` keeps an `flock` on its own file; an entry whose lock is free belongs to a dead process and is pruned |
| `queue/<pid>.json` | a waiting job: command, directory, enqueue time, queue order, force/cancel flags |
| `running/<pid>.json` | a running job: command, directory, start time, slot, forced/kill flags, last progress |
| `paused` | exists while the queue is paused |

A waiter polls every 0.5 s. Waiters are ranked by queue order (enqueue time, lowered by
a bump), and one is admitted when its rank is below the number of free slots, so the
queue is strict FIFO apart from bumps. `heavy-top` never signals anything: its actions
only set flags in these files, and each job's own `heavy-run` acts on them within about
half a second.

Caveats:

- The queue is per user, because its state lives in your home directory.
- `$HEAVY_HOME` must be on a local filesystem; `flock` over NFS or SMB doesn't behave.

## Tests

```sh
python3 tests/test_heavyq.py
python3 tests/test_heavy_top.py
python3 tests/test_heavy_run.py
sh tests/integration_heavy_run.sh   # watchdog, real processes, ~12 s
sh tests/integration_queue.sh       # queue and controls, real processes, ~10 s
```

## License

MIT

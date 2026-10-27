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

`heavy` puts a counting semaphore in front of those commands. Every caller on the
host shares it, so at most N heavy jobs run at once and the rest wait their turn.
Output, stdin and exit codes pass through unchanged, so you can wrap any command
without changing how you use it.

## heavy --top

```
heavy queue                                                           14:32:07 EDT

●● 2/2 slots   waiting 2   load 18.4/15.2/11.9 on 14 cores
load ▂▂▃▄▄▅▅▅▅

╭─ running ──────────────────────────────────────────────────────────────────────╮
│ #     pid  elapsed     cpu  command                                            │
│ 1      10   38m12s    360%  npm run test:e2e                                   │
│                             /home/you/src/web-app                              │
│ 2      20    3m05s    640%  xcodebuild -scheme App build                       │
│                             /home/you/src/ios-app                              │
╰────────────────────────────────────────────────────────────────────────────────╯
╭─ waiting ──────────────────────────────────────────────────────────────────────╮
│  #    pid   waited  command   (sem has no strict FIFO)                         │
│  1     30    2m40s  cargo build --release                                      │
│                     /home/you/src/engine                                       │
│  2     31    0m12s  pnpm test                                                  │
│                     /home/you/src/web-app                                      │
╰────────────────────────────────────────────────────────────────────────────────╯
q quit · r refresh · +/- interval (2s)
```

- **running**: each slot holder, how long it has had the slot, CPU for its whole
  process tree (so a test run counts the browsers it spawned), and where it runs.
- **waiting**: callers blocked on a slot, longest wait first.
- **load**: 1/5/15-minute load against core count, with a history sparkline.

Keys: `q` quit, `r` refresh, `+`/`-` change the refresh interval. `heavy --top
--once` prints a single plain-text snapshot, which is handy for scripts and agents.

## Install

Needs [GNU parallel](https://www.gnu.org/software/parallel/) (for `sem`) and, for
the TUI, Python 3.10+. No other dependencies.

```sh
brew install parallel            # or: apt install parallel
git clone https://github.com/evandcoleman/heavy.git
cd heavy && ./install.sh         # copies into ~/.local/bin
```

`./install.sh --link` symlinks instead of copying, so `git pull` updates it.
Set `PREFIX` to install somewhere else (`PREFIX=/usr/local ./install.sh`).

Works on macOS and Linux.

## Usage

```sh
heavy <cmd> [args...]     # run cmd once a slot is free
heavy --status            # list current slot holders
heavy --top [-n SECONDS]  # TUI (alias for heavy-top)
```

Wrap the expensive things, not everything:

- full test suites, e2e and Playwright runs, screenshot sweeps
- Xcode, Swift, Rust and other big builds
- video or image renders

Quick targeted tests and linters don't need it.

### Slots

The slot count lives in `~/.config/heavy/slots` (a single integer, default `2`).
Everyone sharing the queue should use the same value, since `sem` enforces whatever
limit each caller passes. A good starting point is cores / 6. If cores sit idle while
jobs wait, raise it; if load stays high, more heavy work is running outside `heavy`.

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

`heavy` is a few lines of POSIX sh around
`sem --id heavy -j <slots> --fg -q -u --will-cite <cmd>`. `-q` keeps your arguments'
quoting intact, `-u` streams output live (so it survives if the job is killed), and
`--will-cite` skips GNU parallel's citation prompt. `sem` keeps the semaphore as lock
files in `~/.parallel/semaphores/id-heavy/`; `heavy` pins `PARALLEL_HOME` to
`~/.parallel` so every process run by the same user shares one queue, whatever its
environment. A crashed holder's slot is reclaimed the next time someone acquires.

`heavy-top` reads those lock files to find holders, and the process table to find
waiters (live `sem --id heavy` processes that don't hold a slot).

Caveats:

- The queue is per user, because the lock files live in your home directory.
- `sem` doesn't promise first-in, first-out. Waiters poll, and whoever polls next
  after a slot frees gets it.

## Tests

```sh
python3 tests/test_heavy_top.py
```

## License

MIT

#!/bin/sh
# Queue integration tests: real heavy-run processes on a temp HEAVY_HOME with 1 slot.
# Long-lived jobs are gate-controlled (they wait for a file), never timed. Takes ~10 s.
set -u
root="$(cd "$(dirname "$0")/.." && pwd)"
run="$root/bin/heavy-run"
heavy="$root/bin/heavy"
qctl="python3 $root/tests/qctl.py"

tmp=$(mktemp -d)
export HOME="$tmp/home" HEAVY_HOME="$tmp/q"
mkdir -p "$HOME/.config/heavy"

cleanup() {
  # Leftover pids recorded by a failed scenario: our own job children and orphans.
  for f in "$tmp"/*.t/*.pid "$tmp"/*.t/orphan; do
    [ -e "$f" ] && kill -9 "$(cat "$f")" 2>/dev/null
  done
  for p in $(jobs -p); do kill -9 "$p" 2>/dev/null; done
  rm -rf "$tmp"
}
trap cleanup EXIT
trap 'exit 1' INT TERM

fail() { echo "FAIL [$scen]: $*" >&2; exit 1; }

# until_ <shell condition> [tries]: poll every 0.1 s (default cap ~4 s)
until_() {
  n=${2:-40}
  while [ "$n" -gt 0 ]; do
    eval "$1" && return 0
    sleep 0.1
    n=$((n - 1))
  done
  fail "timed out waiting for: $1"
}

# scen <name>: fresh queue dir, 1 slot, scratch dir $T
scen() {
  scen=$1
  export HEAVY_HOME="$tmp/$1"
  T="$tmp/$1.t"
  mkdir -p "$T"
  echo 1 > "$HOME/.config/heavy/slots"
}

# gate <name>: a job body that records its pid and waits for $T/<name>.go
gate() { echo "echo \$\$ > $T/$1.pid; while [ ! -e $T/$1.go ]; do sleep 0.1; done; rm -f $T/$1.pid"; }

queued() { [ -e "$HEAVY_HOME/queue/$1.json" ]; }
running() { [ -e "$HEAVY_HOME/running/$1.json" ]; }
expect_rc() { wait "$1"; rc=$?; [ "$rc" -eq "$2" ] || fail "$3: rc=$rc, want $2"; }

# Q1: a waiter is forced past a full queue
scen q1
"$run" sh -c "$(gate a)" & A=$!
until_ "running $A"
"$run" sh -c "touch $T/b.ran; $(gate b)" & B=$!
until_ "queued $B"
sleep 0.6
[ ! -e "$T/b.ran" ] || fail "B ran while the only slot was held"
$qctl force "$B" >/dev/null || fail "force"
until_ "[ -e $T/b.ran ]"
grep -q '"slot": null' "$HEAVY_HOME/running/$B.json" || fail "forced holder has a slot"
grep -q '"forced": true' "$HEAVY_HOME/running/$B.json" || fail "holder not marked forced"
touch "$T/a.go" "$T/b.go"
expect_rc "$A" 0 "A"
expect_rc "$B" 0 "B"

# Q2: kill flag -> 143, message, tree gone
scen q2
"$run" sh -c "echo \$\$ > $T/c.pid; exec sleep 30" 2>"$T/a.err" & A=$!
until_ "running $A && [ -s $T/c.pid ]"
$qctl kill "$A" >/dev/null || fail "kill"
expect_rc "$A" 143 "killed job"
[ "$(cat "$T/a.err")" = "heavy: killed from heavy-top" ] || fail "stderr: $(cat "$T/a.err")"
! kill -0 "$(cat "$T/c.pid")" 2>/dev/null || fail "job tree survived"
rm -f "$T/c.pid"

# Q3: cancel a waiter -> 125, never runs
scen q3
"$run" sh -c "$(gate a)" & A=$!
until_ "running $A"
"$run" touch "$T/b.ran" 2>"$T/b.err" & B=$!
until_ "queued $B"
$qctl cancel "$B" >/dev/null || fail "cancel"
expect_rc "$B" 125 "cancelled waiter"
[ "$(cat "$T/b.err")" = "heavy: cancelled from heavy-top" ] || fail "stderr: $(cat "$T/b.err")"
[ ! -e "$T/b.ran" ] || fail "cancelled job ran"
! queued "$B" || fail "queue entry left behind"
touch "$T/a.go"
expect_rc "$A" 0 "A"

# Q4: a SIGKILLed holder frees its slot, even while it is an unreaped zombie
scen q4
"$run" sh -c "$(gate a)" & A=$!
until_ "running $A && [ -s $T/a.pid ]"
"$run" touch "$T/b.ran" & B=$!
until_ "queued $B"
kill -9 "$A"
until_ "[ -e $T/b.ran ]" 15
expect_rc "$B" 0 "B"
kill "$(cat "$T/a.pid")" 2>/dev/null; rm -f "$T/a.pid"  # A's orphaned job
wait "$A" 2>/dev/null

# Q5: a daemon left behind by a finished job doesn't hold the slot (close-on-exec)
scen q5
"$run" sh -c "sleep 30 & echo \$! > $T/orphan" & A=$!
expect_rc "$A" 0 "A"
"$run" touch "$T/b.ran" & B=$!
until_ "[ -e $T/b.ran ]" 15
expect_rc "$B" 0 "B"
kill "$(cat "$T/orphan")" 2>/dev/null; rm -f "$T/orphan"

# Q6: strict FIFO for three waiters
scen q6
"$run" sh -c "$(gate a)" & A=$!
until_ "running $A"
"$run" sh -c "echo W1 >> $T/order" & W1=$!
until_ "queued $W1"
"$run" sh -c "echo W2 >> $T/order" & W2=$!
until_ "queued $W2"
"$run" sh -c "echo W3 >> $T/order" & W3=$!
until_ "queued $W3"
touch "$T/a.go"
expect_rc "$A" 0 "A"
for w in "$W1" "$W2" "$W3"; do expect_rc "$w" 0 "waiter"; done
[ "$(cat "$T/order")" = "$(printf 'W1\nW2\nW3')" ] || fail "order: $(cat "$T/order")"

# Q7: TERM while waiting leaves the queue, exit 143, never runs
scen q7
"$run" sh -c "$(gate a)" & A=$!
until_ "running $A"
"$run" touch "$T/b.ran" & B=$!
until_ "queued $B"
kill -TERM "$B"
expect_rc "$B" 143 "TERMed waiter"
! queued "$B" || fail "queue entry left behind"
[ ! -e "$T/b.ran" ] || fail "TERMed waiter ran"
touch "$T/a.go"
expect_rc "$A" 0 "A"

# Q8: unusable HEAVY_HOME fails closed
scen q8
HEAVY_HOME=/dev/null/x "$run" touch "$T/nope" 2>/dev/null; rc=$?
[ "$rc" -eq 2 ] || fail "rc=$rc, want 2"
[ ! -e "$T/nope" ] || fail "command ran without a queue"

# Q9: passthrough and --status through bin/heavy
scen q9
out=$(echo hi | "$heavy" cat); [ "$out" = hi ] || fail "stdin passthrough: $out"
"$heavy" sh -c 'exit 3'; rc=$?; [ "$rc" -eq 3 ] || fail "exit code: $rc"
"$heavy" sh -c 'kill -9 $$'; rc=$?; [ "$rc" -eq 137 ] || fail "signal exit: $rc"
"$heavy" sh -c "$(gate a)" & A=$!
until_ "running $A"
"$heavy" --status | grep -q "^ *$A .*while \[" || fail "--status misses $A or its command: $("$heavy" --status)"
touch "$T/a.go"
expect_rc "$A" 0 "A"

echo "ok"

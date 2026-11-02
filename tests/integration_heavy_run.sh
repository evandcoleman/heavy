#!/bin/sh
# Runs the real heavy-run binary with tiny limits. Takes a few seconds.
set -u
run="$(cd "$(dirname "$0")/.." && pwd)/bin/heavy-run"
# heavy-run queues before it runs: use a private queue, never the developer's ~/.heavy.
tmp=$(mktemp -d)
export HOME="$tmp" HEAVY_HOME="$tmp/q"
trap 'rm -rf "$tmp"' EXIT
fail() { echo "FAIL: $*" >&2; exit 1; }

# idle kill: sleeps burn no CPU, 3s window sampled every 0.5s
start=$(date +%s)
err=$(HEAVY_IDLE_MIN=0.05 HEAVY_SAMPLE_SEC=0.5 "$run" sleep 60 2>&1 >/dev/null); rc=$?
elapsed=$(( $(date +%s) - start ))
[ "$rc" -eq 124 ] || fail "idle kill: rc=$rc, want 124"
[ "$elapsed" -le 8 ] || fail "idle kill took ${elapsed}s"
case "$err" in "heavy: killed idle job ("*"override per call: HEAVY_IDLE_MIN=0 heavy <cmd>") ;; *) fail "message: $err" ;; esac
[ "$(printf '%s\n' "$err" | wc -l | tr -d ' ')" -eq 1 ] || fail "message not one line: $err"

# churn: a loop of short-lived busy children shows ~0 visible CPU but must not be idle-killed
start=$(date +%s)
HEAVY_IDLE_MIN=0.05 HEAVY_SAMPLE_SEC=0.5 "$run" sh -c 'end=$(( $(date +%s) + 6 )); while [ "$(date +%s)" -lt "$end" ]; do python3 -c "sum(range(2000000))"; done'; rc=$?
elapsed=$(( $(date +%s) - start ))
[ "$rc" -eq 0 ] || fail "churn loop was killed: rc=$rc"
[ "$elapsed" -ge 5 ] || fail "churn loop ended early (${elapsed}s)"

# wall cap
err=$(HEAVY_IDLE_MIN=0 HEAVY_MAX_MIN=0.03 HEAVY_SAMPLE_SEC=0.5 "$run" sleep 60 2>&1 >/dev/null); rc=$?
[ "$rc" -eq 124 ] || fail "wall cap: rc=$rc"
case "$err" in "heavy: killed job at wall cap (HEAVY_MAX_MIN=0.03m)"*) ;; *) fail "wall msg: $err" ;; esac

# exit code passthrough
"$run" sh -c 'exit 3'; [ $? -eq 3 ] || fail "exit code passthrough"
"$run" sh -c 'kill -9 $$'; [ $? -eq 137 ] || fail "signal exit code"

# stdin/stdout passthrough
out=$(echo hi | "$run" cat); [ "$out" = hi ] || fail "stdin passthrough: $out"

# invalid env warns and still runs
err=$(HEAVY_IDLE_MIN=bogus "$run" true 2>&1); rc=$?
[ "$rc" -eq 0 ] && case "$err" in *"invalid HEAVY_IDLE_MIN"*) ;; *) false ;; esac || fail "bad env: rc=$rc $err"

echo "ok"

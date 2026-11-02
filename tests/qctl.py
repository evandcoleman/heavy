#!/usr/bin/env python3
"""Test helper: drive heavyq controls from shell, against $HEAVY_HOME.

Usage: qctl.py force|cancel|bump|kill <pid>   exit 0 on "ok", else 1 (prints the outcome)
       qctl.py state                          prints "queue <pids>" and "running <pids>"
"""
import os
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "bin"))
import heavyq as q  # noqa: E402


def main(argv: list[str]) -> int:
    store = q.Store(q.Paths.from_env(os.environ))
    if argv == ["state"]:
        st = q.read_state(store)
        print("queue", *[w.pid for w in st.waiters])
        print("running", *[h.pid for h in st.holders])
        return 0
    if len(argv) != 2 or argv[0] not in ("force", "cancel", "bump", "kill"):
        print(__doc__, file=sys.stderr)
        return 2
    verb, pid = argv[0], int(argv[1])
    if verb == "kill":
        h = store.get_holder(pid)
        token = h.started if h else -1.0
    else:
        w = store.get_waiter(pid)
        token = w.enqueued if w else -1.0
    outcome = getattr(q, verb)(store, pid, token)
    print(outcome)
    return 0 if outcome == "ok" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

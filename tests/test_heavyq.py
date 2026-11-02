"""Unit tests for bin/heavyq.py: temp HEAVY_HOME, fake clock, no real sleeps or children."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))
sys.dont_write_bytecode = True
import heavyq as q  # noqa: E402


def W(pid, enqueued, order=None, **kw):
    return q.Waiter(pid=pid, cmd=f"job{pid}", cwd="/tmp", enqueued=enqueued,
                    order=enqueued if order is None else order, **kw)


def H(pid, started, slot=0, **kw):
    return q.Holder(pid=pid, cmd=f"job{pid}", cwd="/tmp", started=started, slot=slot, **kw)


class TempHome(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)


class PathsTest(TempHome):
    def test_paths_default_and_override(self):
        p = q.Paths.from_env({"HOME": "/h"})
        self.assertEqual(p.home, Path("/h/.heavy"))
        self.assertEqual(p.slots_file, Path("/h/.config/heavy/slots"))
        p = q.Paths.from_env({"HOME": "/h", "HEAVY_HOME": "/x/q"})
        self.assertEqual(p.home, Path("/x/q"))
        self.assertEqual(p.slots_file, Path("/h/.config/heavy/slots"))


class Serialization(unittest.TestCase):
    def test_waiter_roundtrip(self):
        w = W(10, 100.5, order=99.0, force=True)
        self.assertEqual(q.waiter_from_json(q.waiter_to_json(w), 10), w)
        d = {"pid": 11, "cmd": "x", "enqueued": 5}
        self.assertEqual(q.waiter_from_json(d, 11),
                         q.Waiter(pid=11, cmd="x", cwd="", enqueued=5.0, order=5.0))

    def test_holder_roundtrip(self):
        h = H(10, 100.0, slot=None, forced=True, kill=True, last_progress=120.0)
        self.assertEqual(q.holder_from_json(q.holder_to_json(h), 10), h)
        d = {"pid": 11, "cmd": "x", "started": 5}
        self.assertEqual(q.holder_from_json(d, 11),
                         q.Holder(pid=11, cmd="x", cwd="", started=5.0, slot=None))

    def test_from_json_rejects_bad(self):
        good_w = {"pid": 1, "cmd": "x", "cwd": "/", "enqueued": 1.0, "order": 1.0}
        bads = [
            [], "x", None,
            {k: v for k, v in good_w.items() if k != "pid"},
            {k: v for k, v in good_w.items() if k != "cmd"},
            {k: v for k, v in good_w.items() if k != "enqueued"},
            {**good_w, "pid": "1"}, {**good_w, "pid": True}, {**good_w, "cmd": 3},
            {**good_w, "enqueued": "1"}, {**good_w, "order": None}, {**good_w, "force": "yes"},
        ]
        for b in bads:
            with self.assertRaises(ValueError, msg=repr(b)):
                q.waiter_from_json(b, 1)
        with self.assertRaises(ValueError):
            q.waiter_from_json(good_w, 2)  # pid doesn't match filename
        good_h = {"pid": 1, "cmd": "x", "started": 1.0, "slot": 0}
        for b in ({**good_h, "slot": -1}, {**good_h, "slot": "0"}, {**good_h, "kill": 1},
                  {**good_h, "last_progress": "x"}, {k: v for k, v in good_h.items() if k != "started"}):
            with self.assertRaises(ValueError, msg=repr(b)):
                q.holder_from_json(b, 1)
        with self.assertRaises(ValueError):
            q.holder_from_json(good_h, 3)

    def test_parse_slots(self):
        self.assertEqual(q.parse_slots("3\n"), 3)
        for bad in ("0", "", "x"):
            self.assertEqual(q.parse_slots(bad), q.DEFAULT_SLOTS, bad)


class AtomicWrite(TempHome):
    def test_atomic_write_leaves_no_tmp(self):
        target = self.tmp / "a.json"
        q.atomic_write_json(target, {"a": 1})
        q.atomic_write_json(target, {"a": 2})
        self.assertEqual(json.loads(target.read_text()), {"a": 2})
        self.assertEqual(sorted(os.listdir(self.tmp)), ["a.json"])


class Decide(unittest.TestCase):
    def test_fifo_head_admitted_when_one_free(self):
        ws = [W(3, 30.0), W(1, 10.0), W(2, 20.0)]
        self.assertEqual(q.decide(ws[1], ws, 1, frozenset(), False), q.Verdict("admit", slot=0, rank=0))
        self.assertEqual(q.decide(ws[2], ws, 1, frozenset(), False), q.Verdict("wait", rank=1))
        self.assertEqual(q.decide(ws[0], ws, 1, frozenset(), False), q.Verdict("wait", rank=2))

    def test_two_free_admits_ranks_0_and_1(self):
        ws = [W(1, 10.0), W(2, 20.0), W(3, 30.0)]
        self.assertEqual(q.decide(ws[1], ws, 2, frozenset(), False).kind, "admit")
        self.assertEqual(q.decide(ws[2], ws, 2, frozenset(), False).kind, "wait")

    def test_bump_moves_to_front(self):
        ws = [W(1, 10.0), W(2, 20.0), W(3, 30.0)]
        b = q.bumped(ws[2], ws)
        self.assertEqual(b.order, 9.0)
        self.assertEqual(b.enqueued, 30.0)
        ranked = q.rank([ws[0], ws[1], b])
        self.assertEqual([w.pid for w in ranked], [3, 1, 2])
        self.assertEqual(q.bumped(ws[0], ws).order, 9.0)  # head bump still lowers order

    def test_force_admits_without_slot_even_when_full_and_paused(self):
        me = W(2, 20.0, force=True)
        ws = [W(1, 10.0), me]
        v = q.decide(me, ws, 1, frozenset({0}), True)
        self.assertEqual(v, q.Verdict("force"))
        h = q.admitted(me, v, 50.0)
        self.assertEqual((h.slot, h.forced, h.started, h.pid, h.cmd), (None, True, 50.0, 2, "job2"))

    def test_forced_and_cancelled_waiters_do_not_take_rank(self):
        ws = [W(1, 10.0, force=True), W(2, 20.0, cancel=True), W(3, 30.0)]
        self.assertEqual([w.pid for w in q.rank(ws)], [3])
        self.assertEqual(q.decide(ws[2], ws, 1, frozenset(), False).kind, "admit")

    def test_cancel_wins_over_force(self):
        me = W(1, 10.0, force=True, cancel=True)
        self.assertEqual(q.decide(me, [me], 1, frozenset(), False).kind, "cancel")

    def test_paused_blocks_non_forced(self):
        me = W(1, 10.0)
        self.assertEqual(q.decide(me, [me], 2, frozenset(), True), q.Verdict("wait", rank=None))

    def test_shrink_overflow_counts_as_held(self):
        self.assertEqual(q.free_slots(2, frozenset({1, 2})), [])
        self.assertEqual(q.free_slots(1, frozenset({0, 1})), [])
        self.assertEqual(q.free_slots(3, frozenset({3})), [0, 1])
        self.assertEqual(q.free_slots(2, frozenset({5, 6})), [])

    def test_admit_takes_lowest_free_index(self):
        me = W(1, 10.0)
        v = q.decide(me, [me], 3, frozenset({0}), False)
        self.assertEqual(v, q.Verdict("admit", slot=1, rank=0))
        h = q.admitted(me, v, 11.0)
        self.assertEqual((h.slot, h.forced), (1, False))

    def test_decide_counts_me_even_if_missing_from_list(self):
        me = W(2, 20.0)
        self.assertEqual(q.decide(me, [W(1, 10.0)], 1, frozenset(), False), q.Verdict("wait", rank=1))


if __name__ == "__main__":
    unittest.main()

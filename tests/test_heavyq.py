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


if __name__ == "__main__":
    unittest.main()

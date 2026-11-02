"""Unit tests for bin/heavyq.py: temp HEAVY_HOME, fake clock, no real sleeps or children."""
from __future__ import annotations

import fcntl
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


class StoreCase(TempHome):
    """A Store on a temp HEAVY_HOME with injected liveness and a fake clock."""

    def setUp(self):
        super().setUp()
        self.t = [1000.0]
        self.live: set[int] = set()
        self.paths = q.Paths(home=self.tmp / "q", slots_file=self.tmp / "cfg" / "slots")
        self.store = self.make_store(alive=lambda pid: pid in self.live)
        self.store.ensure()

    def make_store(self, **kw):
        def sleep(sec):
            self.t[0] += sec
        return q.Store(self.paths, clock=lambda: self.t[0], sleep=sleep, **kw)

    def hold(self, path: Path) -> int:
        """Hold an flock on path through a separate open file description, as another process would."""
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.addCleanup(lambda: _close_quiet(fd))
        return fd

    def hold_slot(self, i: int) -> int:
        return self.hold(self.paths.home / "slots" / str(i))


def _close_quiet(fd: int) -> None:
    try:
        os.close(fd)
    except OSError:
        pass


class StoreIO(StoreCase):
    def test_prune_removes_dead_and_corrupt_keeps_live(self):
        self.live |= {1, 3}
        for pid in (1, 2):
            self.store.put_waiter(W(pid, float(pid)))
        self.store.put_holder(H(3, 3.0))
        self.store.put_holder(H(4, 4.0))
        (self.paths.home / "queue" / "5.json").write_text("{not json")
        (self.paths.home / "running" / "6.json").write_text('{"pid": 7, "cmd": "x", "started": 1}')
        self.live |= {5, 6}
        corrupt = self.store.prune()
        self.assertEqual(sorted(corrupt), ["queue/5.json", "running/6.json"])
        self.assertEqual([w.pid for w in self.store.waiters()], [1])
        self.assertEqual([h.pid for h in self.store.holders()], [3])

    def test_prune_removes_stale_tmp_files(self):
        for d in ("queue", "running"):
            (self.paths.home / d / ".1.json.99.tmp").write_text("{}")
        self.store.prune()
        self.assertEqual(os.listdir(self.paths.home / "queue"), [])
        self.assertEqual(os.listdir(self.paths.home / "running"), [])

    def test_reader_tolerates_entry_vanishing_mid_listdir(self):
        self.live |= {1, 2}
        self.store.put_waiter(W(1, 1.0))
        self.store.put_waiter(W(2, 2.0))
        qdir = self.paths.home / "queue"
        real = Path.iterdir

        def iterdir(path):
            items = list(real(path))
            if path == qdir:
                os.unlink(qdir / "1.json")
            return iter(items)

        Path.iterdir = iterdir
        try:
            got = self.store.waiters()
        finally:
            Path.iterdir = real
        self.assertEqual([w.pid for w in got], [2])

    def test_held_slots_sees_other_holder_and_does_not_disturb_it(self):
        fd = self.hold_slot(0)
        self.assertEqual(self.store.held_slots(2), frozenset({0}))
        with self.assertRaises(BlockingIOError):  # still held by the test's fd
            fcntl.flock(os.open(self.paths.home / "slots" / "0", os.O_RDWR | os.O_CLOEXEC),
                        fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.close(fd)
        self.assertEqual(self.store.held_slots(2), frozenset())

    def test_held_slots_counts_held_index_beyond_n(self):
        self.hold_slot(3)
        (self.paths.home / "slots" / "4").write_text("")  # free, beyond n
        self.assertEqual(self.store.held_slots(2), frozenset({3}))

    def test_take_slot_fd_not_inheritable(self):
        fd = self.store.take_slot(0)
        self.addCleanup(os.close, fd)
        self.assertIsNotNone(fd)
        self.assertFalse(os.get_inheritable(fd))
        self.assertIsNone(self.store.take_slot(0))  # second open file description conflicts
        self.assertEqual(self.store.held_slots(1), frozenset({0}))

    def test_locked_raises_busy_when_held_elsewhere(self):
        self.hold(self.paths.home / "lock")
        start = self.t[0]
        with self.assertRaises(q.Busy):
            with self.store.locked(timeout=1.0):
                self.fail("entered a held lock")
        self.assertGreaterEqual(self.t[0] - start, 1.0)
        with self.assertRaises(q.Busy):
            with self.store.locked():
                pass

    def test_locked_is_reacquirable(self):
        with self.store.locked():
            pass
        with self.store.locked():
            pass

    def test_ensure_fails_on_uncreatable_home(self):
        f = self.tmp / "file"
        f.write_text("")
        s = q.Store(q.Paths(home=f / "sub", slots_file=self.tmp / "s"))
        with self.assertRaises(OSError):
            s.ensure()

    def test_set_slots_min_one_and_atomic(self):
        self.assertEqual(self.store.slots(), q.DEFAULT_SLOTS)
        self.store.set_slots(0)
        self.assertEqual(self.store.slots(), 1)
        self.store.set_slots(5)
        self.assertEqual(self.paths.slots_file.read_text().strip(), "5")
        self.assertEqual(sorted(os.listdir(self.paths.slots_file.parent)), ["slots"])

    def test_paused_toggle(self):
        self.assertFalse(self.store.paused())
        self.store.set_paused(True)
        self.assertTrue(self.store.paused())
        self.store.set_paused(False)
        self.assertFalse(self.store.paused())


class LiveLocks(StoreCase):
    """Default liveness (R1): an entry is alive while live/<pid> is flocked."""

    def setUp(self):
        super().setUp()
        self.store = self.make_store()

    def test_entry_alive_only_while_live_file_locked(self):
        self.store.put_waiter(W(1, 1.0))
        self.store.put_waiter(W(2, 2.0))
        self.store.put_holder(H(3, 3.0))
        fd1 = self.hold(self.paths.home / "live" / "1")
        (self.paths.home / "live" / "2").write_text("")  # exists but nobody holds it (zombie/dead)
        self.store.prune()
        self.assertEqual([w.pid for w in self.store.waiters()], [1])
        self.assertEqual(self.store.holders(), [])  # live/3 missing
        self.assertEqual(os.listdir(self.paths.home / "live"), ["1"])  # stale live/2 removed
        os.close(fd1)
        self.store.prune()
        self.assertEqual(self.store.waiters(), [])

    def test_prune_cannot_orphan_a_new_live_owner(self):
        """A new owner that locks live/<pid> between prune's probe and its unlink must not
        end up holding an unlinked inode (its entries would then be pruned as dead)."""
        live = self.paths.home / "live" / "7"
        live.write_text("")  # stale: nobody holds it
        owner: list[int | None] = []

        def new_owner_arrives(path):
            if path == live and not owner:
                owner.append(q.Store.try_hold_live(path))

        store = self.make_store(live_probe_hook=new_owner_arrives)
        store.prune()
        self.assertEqual(len(owner), 1)
        fd = owner[0] if owner[0] is not None else store.hold_live(7)
        self.addCleanup(os.close, fd)
        store.put_waiter(W(7, 1.0))
        store.prune()
        self.assertEqual([w.pid for w in store.waiters()], [7])
        self.assertEqual(os.stat(live).st_ino, os.fstat(fd).st_ino)

    def test_hold_live_marks_alive_and_is_cloexec(self):
        fd = self.store.hold_live(42)
        self.addCleanup(os.close, fd)
        self.assertFalse(os.get_inheritable(fd))
        self.store.put_waiter(W(42, 1.0))
        self.store.prune()
        self.assertEqual([w.pid for w in self.store.waiters()], [42])


class Transactions(StoreCase):
    def waiter(self, pid, enqueued, **kw):
        w = W(pid, enqueued, **kw)
        self.live.add(pid)
        q.enqueue(self.store, w)
        return w

    def test_admit_step_admits_and_moves_entry(self):
        me = self.waiter(1, 10.0)
        step = q.admit_step(self.store, me, 20.0)
        self.assertIsInstance(step, q.Admitted)
        self.addCleanup(os.close, step.slot_fd)
        self.assertFalse((self.paths.home / "queue" / "1.json").exists())
        h = self.store.get_holder(1)
        self.assertEqual((h.slot, h.started, h.forced), (0, 20.0, False))
        self.assertEqual(step.holder, h)
        self.assertEqual(self.store.held_slots(2), frozenset({0}))

    def test_admit_step_waits_behind_earlier_waiter(self):
        self.store.set_slots(1)
        self.waiter(1, 10.0)
        me = self.waiter(2, 11.0)
        self.assertEqual(q.admit_step(self.store, me, 20.0), q.Waiting(1))
        self.assertIsNotNone(self.store.get_waiter(2))

    def test_admit_step_self_heals_missing_entry(self):
        self.store.set_slots(1)
        self.hold_slot(0)
        me = self.waiter(1, 10.0, order=5.0)
        self.store.drop_waiter(1)
        self.assertEqual(q.admit_step(self.store, me, 20.0), q.Waiting(0))
        self.assertEqual(self.store.get_waiter(1), me)

    def test_admit_step_cancel_removes_entry(self):
        me = self.waiter(1, 10.0)
        self.assertEqual(q.cancel(self.store, 1, 10.0), "ok")
        self.assertIsInstance(q.admit_step(self.store, me, 20.0), q.Cancelled)
        self.assertIsNone(self.store.get_waiter(1))
        self.assertEqual(self.store.holders(), [])

    def test_admit_step_force_admits_without_slot(self):
        self.store.set_slots(1)
        self.hold_slot(0)
        me = self.waiter(1, 10.0)
        self.assertEqual(q.force(self.store, 1, 10.0), "ok")
        step = q.admit_step(self.store, me, 20.0)
        self.assertEqual((step.slot_fd, step.holder.slot, step.holder.forced), (None, None, True))

    def test_admit_step_busy_lock_is_waiting(self):
        me = self.waiter(1, 10.0)
        self.hold(self.paths.home / "lock")
        self.assertEqual(q.admit_step(self.store, me, 20.0), q.Waiting(None))

    def test_admit_step_paused_waits_with_no_rank(self):
        me = self.waiter(1, 10.0)
        self.store.set_paused(True)
        self.assertEqual(q.admit_step(self.store, me, 20.0), q.Waiting(None))

    def test_slot_freed_when_holder_fd_closed(self):
        self.store.set_slots(1)
        fd = self.hold_slot(0)
        me = self.waiter(1, 10.0)
        self.assertEqual(q.admit_step(self.store, me, 20.0), q.Waiting(0))
        os.close(fd)
        step = q.admit_step(self.store, me, 21.0)
        self.assertIsInstance(step, q.Admitted)
        os.close(step.slot_fd)

    def test_leave_queue_and_finish(self):
        me = self.waiter(1, 10.0)
        q.leave_queue(self.store, 1)
        self.assertIsNone(self.store.get_waiter(1))
        self.store.put_holder(H(2, 1.0))
        q.finish(self.store, 2)
        self.assertIsNone(self.store.get_holder(2))

    def test_record_progress_preserves_kill_flag(self):
        self.live.add(1)
        base = H(1, 50.0)
        self.store.put_holder(base)
        self.assertEqual(q.kill(self.store, 1, 50.0), "ok")
        self.assertTrue(q.record_progress(self.store, base, 70.0))
        h = self.store.get_holder(1)
        self.assertEqual((h.kill, h.last_progress), (True, 70.0))
        self.assertTrue(q.kill_requested(self.store, 1))

    def test_record_progress_recreates_missing_and_reports_busy(self):
        base = H(1, 50.0)
        self.assertTrue(q.record_progress(self.store, base, 60.0))
        self.assertEqual(self.store.get_holder(1).last_progress, 60.0)
        self.hold(self.paths.home / "lock")
        self.assertFalse(q.record_progress(self.store, base, 61.0))

    def test_kill_requested_false_without_flag_or_entry(self):
        self.assertFalse(q.kill_requested(self.store, 1))
        self.store.put_holder(H(1, 50.0))
        self.assertFalse(q.kill_requested(self.store, 1))

    def test_controls_on_vanished_entry_are_gone(self):
        for ctl in (q.force, q.cancel, q.bump, q.kill):
            self.assertEqual(ctl(self.store, 9, 1.0), "gone", ctl.__name__)
        self.assertEqual(os.listdir(self.paths.home / "queue"), [])
        self.assertEqual(os.listdir(self.paths.home / "running"), [])

    def test_controls_token_mismatch_is_gone(self):
        self.waiter(1, 10.0)
        self.live.add(2)
        self.store.put_holder(H(2, 20.0))
        for ctl in (q.force, q.cancel, q.bump):
            self.assertEqual(ctl(self.store, 1, 11.0), "gone", ctl.__name__)
        self.assertEqual(q.kill(self.store, 2, 21.0), "gone")
        self.assertEqual(self.store.get_waiter(1), W(1, 10.0))
        self.assertFalse(self.store.get_holder(2).kill)

    def test_controls_on_dead_entry_are_gone(self):
        self.store.put_waiter(W(1, 10.0))  # pid 1 not live
        self.assertEqual(q.force(self.store, 1, 10.0), "gone")

    def test_cancel_on_admitted_job_does_not_kill(self):
        me = self.waiter(1, 10.0)
        step = q.admit_step(self.store, me, 20.0)
        self.addCleanup(os.close, step.slot_fd)
        self.assertEqual(q.cancel(self.store, 1, 10.0), "gone")
        self.assertFalse(self.store.get_holder(1).kill)

    def test_controls_report_busy(self):
        self.waiter(1, 10.0)
        self.hold(self.paths.home / "lock")
        self.assertEqual(q.force(self.store, 1, 10.0), "busy")
        self.assertEqual(q.toggle_pause(self.store), "busy")
        self.assertEqual(q.change_slots(self.store, 1), "busy")

    def test_bump_reorders(self):
        self.waiter(1, 10.0)
        self.waiter(2, 11.0)
        self.waiter(3, 12.0)
        self.assertEqual(q.bump(self.store, 3, 12.0), "ok")
        state = q.read_state(self.store)
        self.assertEqual([w.pid for w in state.waiters], [3, 1, 2])
        self.assertEqual(self.store.get_waiter(3).order, 9.0)

    def test_toggle_pause(self):
        self.assertIs(q.toggle_pause(self.store), True)
        self.assertTrue(self.store.paused())
        self.assertIs(q.toggle_pause(self.store), False)
        self.assertFalse(self.store.paused())

    def test_change_slots_floor_one(self):
        self.assertEqual(q.change_slots(self.store, 1), 3)
        self.assertEqual(q.change_slots(self.store, -5), 1)
        self.assertEqual(q.change_slots(self.store, -1), 1)
        self.assertEqual(self.store.slots(), 1)

    def test_read_state_reports_corrupt_once_per_call(self):
        self.waiter(1, 10.0)
        self.waiter(2, 11.0, force=True)
        self.waiter(3, 12.0)
        self.live.add(4)
        self.store.put_holder(H(4, 5.0, slot=None, forced=True))
        self.hold_slot(1)
        (self.paths.home / "queue" / "7.json").write_text("garbage")
        self.store.set_paused(True)
        st = q.read_state(self.store)
        self.assertEqual(st.corrupt, ("queue/7.json",))
        self.assertEqual([w.pid for w in st.waiters], [1, 3, 2])  # forced pending after ranked
        self.assertEqual([h.pid for h in st.holders], [4])
        self.assertEqual((st.n, st.held, st.paused), (2, frozenset({1}), True))
        self.assertEqual(q.read_state(self.store).corrupt, ())

    def test_read_state_busy(self):
        self.hold(self.paths.home / "lock")
        with self.assertRaises(q.Busy):
            q.read_state(self.store, timeout=0.05)


if __name__ == "__main__":
    unittest.main()

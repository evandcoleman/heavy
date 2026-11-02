"""Unit tests for bin/heavy-top: snapshot building, rendering, keys and actions."""
import importlib.machinery
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.dont_write_bytecode = True
loader = importlib.machinery.SourceFileLoader("ht", str(ROOT / "bin" / "heavy-top"))
spec = importlib.util.spec_from_loader("ht", loader)
ht = importlib.util.module_from_spec(spec)
sys.modules["ht"] = ht
loader.exec_module(ht)
q = ht.heavyq

NOW = 10_000.0


def W(pid, enqueued, order=None, **kw):
    return q.Waiter(pid=pid, cmd=f"job{pid}", cwd="/tmp/w", enqueued=enqueued,
                    order=enqueued if order is None else order, **kw)


def H(pid, started, slot=0, **kw):
    return q.Holder(pid=pid, cmd=f"run{pid}", cwd="/tmp/r", started=started, slot=slot, **kw)


def state(waiters=(), holders=(), n=2, held=frozenset(), paused=False):
    return q.QueueState(waiters=tuple(waiters), holders=tuple(holders), n=n, held=frozenset(held),
                        paused=paused, corrupt=())


def snap_of(st, procs=None):
    return ht.build_snapshot(st, procs or {}, (1.0, 2.0, 3.0), 8, NOW)


def P(pid, ppid, cpu):
    return ht.Proc(pid, ppid, cpu, 0, "x")


class Parsing(unittest.TestCase):
    def test_parse_etime(self):
        self.assertEqual(ht.parse_etime("05:07"), 307)
        self.assertEqual(ht.parse_etime("01:34:33"), 5673)
        self.assertEqual(ht.parse_etime("2-01:00:00"), 2 * 86400 + 3600)

    def test_lclip(self):
        self.assertEqual(ht.lclip("abcdef", 4), "…def")


class BuildSnapshot(unittest.TestCase):
    def test_snapshot_ranks_waiters_in_queue_order_with_rank_numbers(self):
        st = state(waiters=[W(3, 30.0, order=1.0), W(1, 10.0), W(2, 20.0, force=True),
                            W(4, 40.0, cancel=True)])
        s = snap_of(st)
        self.assertEqual([(r.pid, r.rank) for r in s.waiting], [(3, 1), (1, 2), (2, None), (4, None)])
        self.assertIn("forcing", s.waiting[2].tags)
        self.assertIn("cancelling", s.waiting[3].tags)
        self.assertEqual([r.kind for r in s.waiting], ["wait"] * 4)
        self.assertEqual(s.waiting[0].token, 30.0)

    def test_snapshot_elapsed_from_injected_now(self):
        s = snap_of(state(waiters=[W(1, NOW - 75), W(2, NOW + 5)], holders=[H(9, NOW - 3600)]))
        self.assertEqual([r.elapsed for r in s.waiting], [75, 0])  # clock skew clamps at 0
        self.assertEqual(s.running[0].elapsed, 3600)
        self.assertEqual(s.running[0].token, NOW - 3600)

    def test_snapshot_tags_forced_bumped_idle(self):
        st = state(waiters=[W(1, 50.0, order=10.0), W(2, 60.0)],
                   holders=[H(7, 0.0, slot=None, forced=True, last_progress=NOW - 360),
                            H(8, 0.0, last_progress=NOW - 59), H(9, 0.0, kill=True)])
        s = snap_of(st)
        self.assertEqual(s.running[0].tags, ("forced", "idle 6m"))
        self.assertEqual(s.running[1].tags, ())
        self.assertEqual(s.running[2].tags, ("killing",))
        self.assertEqual(s.waiting[0].tags, ("bumped",))
        self.assertEqual(s.waiting[1].tags, ())
        self.assertEqual(s.forced, 1)

    def test_tree_cpu_attributed_to_holder_pid(self):
        procs = {p.pid: p for p in [P(7, 1, 0.0), P(70, 7, 50.0), P(71, 70, 120.5), P(80, 1, 99.0)]}
        s = snap_of(state(holders=[H(7, 0.0)]), procs)
        self.assertAlmostEqual(s.running[0].cpu, 170.5)

    def test_counts(self):
        s = snap_of(state(holders=[H(1, 0.0), H(2, 0.0, slot=None, forced=True)], n=3,
                          held={0}, paused=True))
        self.assertEqual((s.slots, s.held, s.forced, s.paused), (3, 1, 1, True))
        self.assertEqual([(r.kind, r.pid) for r in s.rows()], [("run", 1), ("run", 2)])


class Render(unittest.TestCase):
    def render(self, s, **kw):
        return ht.render(s, [1, 5, 20], 100, 2, color=kw.pop("color", False), **kw)

    def test_render_paused_banner(self):
        self.assertIn("PAUSED", self.render(snap_of(state(paused=True))))
        self.assertNotIn("PAUSED", self.render(snap_of(state())))

    def test_render_slots_held_and_forced(self):
        s = snap_of(state(holders=[H(1, 0.0), H(2, 0.0, slot=1), H(3, 0.0, slot=None, forced=True)],
                          held={0, 1}))
        out = self.render(s)
        self.assertIn("2/2 slots", out)
        self.assertIn("+1 forced", out)

    def test_render_free_rows_count(self):
        self.assertEqual(self.render(snap_of(state(holders=[H(1, 0.0)], n=3, held={0}))).count("free"), 2)
        self.assertEqual(self.render(snap_of(state(n=2, held={0, 1}))).count("free"), 0)

    def test_render_once_plain_has_new_fields(self):
        st = state(waiters=[W(1, NOW - 10, order=0.0), W(2, NOW - 5)],
                   holders=[H(7, NOW - 400, slot=None, forced=True, last_progress=NOW - 360)],
                   held=set())
        out = self.render(snap_of(st))
        self.assertNotIn("\x1b", out)
        for want in ("forced", "bumped", "idle 6m", "job1", "run7", "/tmp/r", "/tmp/w"):
            self.assertIn(want, out)
        line = next(l for l in out.splitlines() if "job2" in l)
        self.assertRegex(line, r"\b2\s+2\s")  # rank 2, pid 2

    def test_render_caps_to_max_rows(self):
        st = state(waiters=[W(500 + i, float(i)) for i in range(40)])
        out = self.render(snap_of(st), max_rows=30)
        self.assertLessEqual(len(out.splitlines()), 30)
        self.assertIn("more", out)

    def test_render_selected_row_reverse_video(self):
        s = snap_of(state(waiters=[W(1, 1.0), W(2, 2.0)]))
        out = self.render(s, color=True, selected=("wait", 2))
        line = next(l for l in out.splitlines() if "job2" in l)
        self.assertIn(ht.REVERSE, line)
        self.assertNotIn(ht.REVERSE, next(l for l in out.splitlines() if "job1" in l))
        plain = self.render(s, selected=("wait", 2))
        self.assertNotIn("\x1b", plain)

    def test_render_footer_override(self):
        self.assertIn("kill run7? y/n", self.render(snap_of(state()), footer="kill run7? y/n"))
        self.assertIn("q quit", self.render(snap_of(state())))


class Probe(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.paths = q.Paths(home=Path(tmp.name) / "q", slots_file=Path(tmp.name) / "slots")
        self.t = [0.0]
        self.store = q.Store(self.paths, alive=lambda pid: True, clock=lambda: self.t[0],
                             sleep=lambda s: self.t.__setitem__(0, self.t[0] + s))
        self.store.ensure()

    def test_probe_reads_queue_and_reports_corrupt(self):
        self.store.put_waiter(W(1, NOW - 5))
        (self.paths.home / "queue" / "9.json").write_text("junk")
        snap, notes = ht.probe(self.store, NOW, ps=lambda: {})
        self.assertEqual([r.pid for r in snap.waiting], [1])
        self.assertEqual(notes, ("dropped corrupt entry queue/9.json",))

    def test_probe_busy_keeps_previous_snapshot(self):
        prev = snap_of(state(waiters=[W(1, 1.0)]))
        import fcntl
        fd = os.open(self.paths.home / "lock", os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        snap, notes = ht.probe(self.store, NOW, prev=prev, ps=lambda: {})
        self.assertIs(snap, prev)
        self.assertEqual(notes, ("queue busy",))
        snap, _ = ht.probe(self.store, NOW, ps=lambda: {})
        self.assertEqual(snap.rows(), ())


if __name__ == "__main__":
    unittest.main()

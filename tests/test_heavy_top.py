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


def two_by_two():
    return snap_of(state(waiters=[W(3, 30.0), W(4, 40.0)], holders=[H(1, 10.0), H(2, 20.0, slot=1)],
                         held={0, 1}))


def ui(selected=None, **kw):
    return ht.UiState(selected=selected, interval=kw.pop("interval", 2.0), **kw)


class Keys(unittest.TestCase):
    def test_decode_arrows_both_modes(self):
        self.assertEqual(ht.decode_keys(b"\x1b[A\x1bOB\x1b[Bk"), ["up", "down", "down", "k"])
        self.assertEqual(ht.decode_keys(b"\x1b"), ["esc"])
        self.assertEqual(ht.decode_keys(b"\x1b[5~x"), ["x"])  # other escapes dropped
        self.assertEqual(ht.decode_keys(b"\x1b[<64;10;5M"), [])  # mouse wheel
        self.assertEqual(ht.decode_keys(b"+\x03"), ["+", "\x03"])

    def test_selection_moves_across_running_and_waiting(self):
        s = two_by_two()
        u = ui(("run", 1))
        seen = []
        for _ in range(5):
            u = ht.handle_key(u, "down", s).ui
            seen.append(u.selected)
        self.assertEqual(seen, [("run", 2), ("wait", 3), ("wait", 4), ("wait", 4), ("wait", 4)])
        u = ht.handle_key(u, "k", s).ui
        self.assertEqual(u.selected, ("wait", 3))
        u = ht.handle_key(ui(None), "j", s).ui
        self.assertEqual(u.selected, ("run", 1))

    def test_key_to_action_mapping(self):
        s = two_by_two()
        cases = [  # (selected, key, verb or None, confirm?)
            (("wait", 3), "f", "force", False),
            (("wait", 3), "b", "bump", False),
            (("wait", 3), "c", "cancel", True),
            (("run", 1), "x", "kill", True),
            (("run", 1), "f", None, False),
            (("run", 1), "b", None, False),
            (("run", 1), "c", None, False),
            (("wait", 3), "x", None, False),
            (None, "f", None, False),
            (("wait", 3), "p", "pause", False),
            (("wait", 3), "+", "slots", False),
            (("wait", 3), "=", "slots", False),
            (("wait", 3), "-", "slots", False),
            (("wait", 3), "_", "slots", False),
        ]
        for sel, key, verb, confirm in cases:
            r = ht.handle_key(ui(sel), key, s)
            got = (r.ui.confirm.verb if r.ui.confirm else None) if confirm else (r.action.verb if r.action else None)
            self.assertEqual(got, verb, (sel, key))
            if verb is None:
                self.assertTrue(r.ui.note, (sel, key))
                self.assertIsNone(r.ui.confirm)
            if confirm:
                self.assertIsNone(r.action)
        a = ht.handle_key(ui(("wait", 4)), "f", s).action
        self.assertEqual((a.kind, a.pid, a.token), ("wait", 4, 40.0))
        self.assertEqual(ht.handle_key(ui(), "+", s).action.delta, 1)
        self.assertEqual(ht.handle_key(ui(), "-", s).action.delta, -1)

    def test_kill_requires_confirm_y(self):
        s = two_by_two()
        r = ht.handle_key(ui(("run", 2)), "x", s)
        self.assertEqual(r.ui.confirm.label, "kill run2? y/n")
        r2 = ht.handle_key(r.ui, "y", s)
        self.assertEqual((r2.action.verb, r2.action.pid, r2.action.token), ("kill", 2, 20.0))
        self.assertIsNone(r2.ui.confirm)
        self.assertFalse(r2.quit)
        self.assertIn("cancel job3? y/n", ht.handle_key(ui(("wait", 3)), "c", s).ui.confirm.label)

    def test_confirm_cancelled_by_other_key_including_q(self):
        s = two_by_two()
        asking = ht.handle_key(ui(("run", 1)), "x", s).ui
        for key in ("n", "q", "\x03", "esc", "x"):
            r = ht.handle_key(asking, key, s)
            self.assertEqual((r.action, r.quit, r.ui.confirm, r.ui.note), (None, False, None, "kept"), key)

    def test_quit_keys(self):
        for key in ("q", "Q", "\x03"):
            self.assertTrue(ht.handle_key(ui(), key, two_by_two()).quit)

    def test_brackets_change_interval_plus_minus_change_slots(self):
        s = two_by_two()
        self.assertEqual(ht.handle_key(ui(interval=2.0), "]", s).ui.interval, 3.0)
        self.assertEqual(ht.handle_key(ui(interval=2.0), "[", s).ui.interval, 1.0)
        self.assertEqual(ht.handle_key(ui(interval=1.0), "[", s).ui.interval, 1.0)
        self.assertEqual(ht.handle_key(ui(interval=30.0), "]", s).ui.interval, 30.0)
        r = ht.handle_key(ui(interval=2.0), "+", s)
        self.assertEqual((r.ui.interval, r.action.verb), (2.0, "slots"))
        self.assertTrue(ht.handle_key(ui(), "r", s).refresh)


class Reconcile(unittest.TestCase):
    def test_reconcile_keeps_selection_by_pid_and_clamps_when_gone(self):
        s = two_by_two()
        self.assertEqual(ht.reconcile(ui(("wait", 4)), s).selected, ("wait", 4))
        self.assertEqual(ht.reconcile(ui(None), s).selected, ("run", 1))
        admitted = snap_of(state(waiters=[W(4, 40.0)], holders=[H(1, 10.0), H(2, 20.0), H(3, 50.0)]))
        self.assertEqual(ht.reconcile(ui(("wait", 3)), admitted).selected, ("run", 3))  # follows the job
        u = ht.reconcile(ui(("wait", 3)), s)  # remembers index 2
        gone = snap_of(state(waiters=[W(4, 40.0)], holders=[H(1, 10.0), H(2, 20.0)]))
        self.assertEqual(ht.reconcile(u, gone).selected, ("wait", 4))  # same index
        u = ht.reconcile(ui(("wait", 4)), s)
        short = snap_of(state(holders=[H(1, 10.0)]))
        self.assertEqual(ht.reconcile(u, short).selected, ("run", 1))  # clamped to the last row
        self.assertIsNone(ht.reconcile(u, snap_of(state())).selected)

    def test_reconcile_drops_confirm_when_target_vanished(self):
        s = two_by_two()
        asking = ht.handle_key(ui(("run", 2)), "x", s).ui
        self.assertIsNotNone(ht.reconcile(asking, s).confirm)
        restarted = snap_of(state(holders=[H(1, 10.0), H(2, 99.0)]))  # pid reused: new token
        u = ht.reconcile(asking, restarted)
        self.assertIsNone(u.confirm)
        self.assertEqual(u.note, "job 2 no longer running")


class ApplyAction(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.paths = q.Paths(home=Path(tmp.name) / "q", slots_file=Path(tmp.name) / "slots")
        self.store = q.Store(self.paths, alive=lambda pid: True)
        self.store.ensure()

    def test_apply_action_gone_sets_note(self):
        note = ht.apply_action(self.store, ht.Action("force", "wait", 77, 1.0))
        self.assertEqual(note, "job 77 no longer waiting")
        note = ht.apply_action(self.store, ht.Action("kill", "run", 78, 1.0))
        self.assertEqual(note, "job 78 no longer running")
        self.assertEqual(os.listdir(self.paths.home / "queue"), [])

    def test_apply_action_ok_mutates_store(self):
        self.store.put_waiter(W(5, 50.0))
        self.store.put_waiter(W(6, 60.0))
        self.assertEqual(ht.apply_action(self.store, ht.Action("bump", "wait", 6, 60.0)), "bumped job 6 to the front")
        self.assertEqual(self.store.get_waiter(6).order, 49.0)
        self.assertEqual(ht.apply_action(self.store, ht.Action("force", "wait", 5, 50.0)), "forcing job 5")
        self.assertTrue(self.store.get_waiter(5).force)
        self.assertEqual(ht.apply_action(self.store, ht.Action("pause")), "queue paused")
        self.assertEqual(ht.apply_action(self.store, ht.Action("pause")), "queue resumed")
        self.assertEqual(ht.apply_action(self.store, ht.Action("slots", delta=1)), "slots: 3")


if __name__ == "__main__":
    unittest.main()

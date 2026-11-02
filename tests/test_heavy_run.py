import contextlib, fcntl, io, os, shutil, signal, subprocess, sys, tempfile, unittest
import importlib.util, importlib.machinery
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.dont_write_bytecode = True
loader = importlib.machinery.SourceFileLoader("hr", str(ROOT / "bin" / "heavy-run"))
spec = importlib.util.spec_from_loader("hr", loader)
hr = importlib.util.module_from_spec(spec)
sys.modules["hr"] = hr
loader.exec_module(hr)
q = hr.heavyq

MIN = 60.0


def dog(idle_cpu=5.0, idle_min=10.0, max_min=90.0, start=0.0):
    return hr.Watchdog(idle_cpu=idle_cpu, idle_sec=idle_min * MIN, max_sec=max_min * MIN, start=start)


class ParseCpuTime(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(hr.parse_cpu_time("0:05"), 5)
        self.assertEqual(hr.parse_cpu_time("05:07"), 307)
        self.assertEqual(hr.parse_cpu_time("01:34:33"), 5673)
        self.assertEqual(hr.parse_cpu_time("2-01:00:00"), 2 * 86400 + 3600)

    def test_fractions(self):
        self.assertAlmostEqual(hr.parse_cpu_time("0:01.50"), 1.5)
        self.assertAlmostEqual(hr.parse_cpu_time("1:02:03.25"), 3723.25)

    def test_garbage_is_none(self):
        self.assertIsNone(hr.parse_cpu_time("-"))
        self.assertIsNone(hr.parse_cpu_time(""))


class ParsePs(unittest.TestCase):
    def test_parses_rows_and_skips_malformed(self):
        text = "  10     1    10   0:01.50\n 11 10 10 01:00\nbad line\n 12 x 3 0:01\n"
        procs = hr.parse_ps(text)
        self.assertEqual([(p.pid, p.ppid, p.pgid, p.cpu) for p in procs],
                         [(10, 1, 10, 1.5), (11, 10, 10, 60.0)])


class SelectTree(unittest.TestCase):
    def test_descendants_and_group_members_only(self):
        P = hr.Proc
        procs = [
            P(100, 1, 100, 1.0),    # child (root)
            P(101, 100, 100, 2.0),  # child in group
            P(102, 101, 102, 3.0),  # grandchild, own group (descendant)
            P(103, 1, 100, 4.0),    # reparented, still in group
            P(200, 1, 200, 9.0),    # unrelated
            P(201, 200, 200, 9.0),  # unrelated child
        ]
        self.assertEqual(hr.select_tree(procs, 100, 100), {100: 1.0, 101: 2.0, 102: 3.0, 103: 4.0})


class Idle(unittest.TestCase):
    def test_busy_job_continues(self):
        d = dog()
        for t in range(30, 3600, 30):
            self.assertEqual(d.observe(t, {1: t * 1.0}).kind, "continue")

    def test_idle_job_killed_after_window(self):
        d = dog()
        self.assertEqual(d.observe(0, {1: 1.0}).kind, "continue")
        for t in range(30, 600, 30):
            self.assertEqual(d.observe(t, {1: 1.0}).kind, "continue")  # window not yet eligible
        r = d.observe(600, {1: 1.0})
        self.assertEqual(r.kind, "idle")
        self.assertAlmostEqual(r.cpu, 0.0)

    def test_not_eligible_before_window_even_if_idle(self):
        d = dog()
        self.assertEqual(d.observe(599, {1: 0.0}).kind, "continue")

    def test_busy_then_idle(self):
        d = dog()
        for t in range(30, 600, 30):
            self.assertEqual(d.observe(t, {1: t * 1.0}).kind, "continue")
        # busy until t=600 (cpu 600), then flat
        kinds = {}
        for t in range(600, 1500, 30):
            r = d.observe(t, {1: 600.0})
            kinds[t] = r.kind
            if r.kind == "idle":
                break
        self.assertEqual(max(t for t in kinds), 1200)  # trailing 600s window has zero gain
        self.assertEqual(kinds[1200], "idle")
        self.assertTrue(all(k == "continue" for t, k in kinds.items() if t < 1200))

    def test_vanished_pid_high_water_counts_as_progress(self):
        d = dog()
        d.observe(30, {1: 0.0, 2: 0.0})
        d.observe(60, {1: 0.0, 2: 100.0})  # pid 2 burns 100s then exits
        for t in range(90, 660, 30):
            r = d.observe(t, {1: 0.0})  # pid 2 vanished: must not look like negative progress
            self.assertEqual(r.kind, "continue", t)
        # window at 660 starts at t=60 (total 100); no gain since -> idle at 660
        self.assertEqual(d.observe(660, {1: 0.0}).kind, "idle")

    def test_threshold_is_strict_less_than(self):
        d = dog()
        r = d.observe(600, {1: 5.0})
        self.assertEqual(r.kind, "continue")

    def test_idle_cpu_zero_never_idle(self):
        d = dog(idle_cpu=0, max_min=0)
        self.assertEqual(d.observe(6000, {1: 0.0}).kind, "continue")

    def test_idle_min_zero_disables(self):
        d = dog(idle_min=0)
        self.assertEqual(d.observe(80 * MIN, {1: 0.0}).kind, "continue")

    def test_churn_with_no_visible_cpu_is_not_idle(self):
        d = dog()
        for i, t in enumerate(range(30, 3600, 30)):
            # a new short-lived pid every sample; exited children's CPU never shows up
            self.assertEqual(d.observe(t, {1: 0.0, 1000 + i: 0.0}).kind, "continue", t)

    def test_initial_sample_pids_are_not_churn(self):
        d = dog()
        for t in range(30, 600, 30):
            d.observe(t, {1: 0.0, 2: 0.0, 3: 0.0})
        self.assertEqual(d.observe(600, {1: 0.0, 2: 0.0, 3: 0.0}).kind, "idle")

    def test_stable_pid_set_at_low_cpu_is_idle(self):
        d = dog()
        for t in range(30, 600, 30):
            d.observe(t, {1: 0.1 * t / 30, 2: 0.0})
        self.assertEqual(d.observe(600, {1: 2.0, 2: 0.0}).kind, "idle")

    def test_idle_after_churn_stops_and_a_full_quiet_window_passes(self):
        d = dog()
        d.observe(30, {1: 0.0})
        for i, t in enumerate(range(60, 331, 30)):
            d.observe(t, {1: 0.0, 1000 + i: 0.0})  # last new pid joins at t=330
        for t in range(360, 930, 30):
            self.assertEqual(d.observe(t, {1: 0.0, 1009: 0.0}).kind, "continue", t)
        self.assertEqual(d.observe(930, {1: 0.0, 1009: 0.0}).kind, "idle")

    def test_failed_sample_is_skipped(self):
        d = dog()
        self.assertEqual(d.observe(600, None).kind, "continue")


class WallCap(unittest.TestCase):
    def test_wall_cap(self):
        d = dog(max_min=90, idle_min=0)
        self.assertEqual(d.observe(90 * MIN, {1: 1.0}).kind, "continue")
        self.assertEqual(d.observe(90 * MIN + 1, {1: 1.0}).kind, "wallcap")

    def test_wall_cap_applies_when_sample_failed(self):
        d = dog(max_min=1)
        self.assertEqual(d.observe(61, None).kind, "wallcap")

    def test_zero_disables(self):
        d = dog(max_min=0, idle_min=0)
        self.assertEqual(d.observe(10 ** 7, {1: 0.0}).kind, "continue")

    def test_cap_wins_over_idle(self):
        d = dog(max_min=5, idle_min=10)
        self.assertEqual(d.observe(6 * MIN, {1: 0.0}).kind, "wallcap")


class EnvLimits(unittest.TestCase):
    def run_cfg(self, env):
        warns = []
        cfg = hr.load_config(env, warns.append)
        return cfg, warns

    def test_defaults(self):
        cfg, warns = self.run_cfg({})
        self.assertEqual((cfg.idle_cpu, cfg.idle_min, cfg.max_min, cfg.sample_sec), (5.0, 10.0, 90.0, 30.0))
        self.assertEqual(warns, [])

    def test_fractional_and_zero(self):
        cfg, warns = self.run_cfg({"HEAVY_IDLE_MIN": "0.05", "HEAVY_MAX_MIN": "0", "HEAVY_SAMPLE_SEC": "0.5"})
        self.assertEqual((cfg.idle_min, cfg.max_min, cfg.sample_sec), (0.05, 0.0, 0.5))
        self.assertEqual(warns, [])

    def test_invalid_values_warn_and_default(self):
        for bad in ("abc", "-1", "nan", "inf", ""):
            cfg, warns = self.run_cfg({"HEAVY_IDLE_MIN": bad, "HEAVY_IDLE_CPU": bad, "HEAVY_MAX_MIN": bad})
            self.assertEqual((cfg.idle_cpu, cfg.idle_min, cfg.max_min), (5.0, 10.0, 90.0), bad)
            self.assertEqual(len(warns), 3, bad)
            self.assertTrue(all(w.startswith("heavy: ") and "HEAVY_" in w for w in warns))

    def test_sample_sec_must_be_positive(self):
        cfg, warns = self.run_cfg({"HEAVY_SAMPLE_SEC": "0"})
        self.assertEqual(cfg.sample_sec, 30.0)
        self.assertEqual(len(warns), 1)


class Messages(unittest.TestCase):
    def test_idle_message(self):
        cfg = hr.Config(idle_cpu=5.0, idle_min=10.0, max_min=90.0, sample_sec=30.0)
        m = hr.kill_message(hr.Decision("idle", cpu=1.23), cfg)
        self.assertEqual(m, "heavy: killed idle job (1.2s CPU in last 10m; limit HEAVY_IDLE_CPU=5s/HEAVY_IDLE_MIN=10m)"
                            " — override per call: HEAVY_IDLE_MIN=0 heavy <cmd>")

    def test_wallcap_message(self):
        cfg = hr.Config(idle_cpu=5.0, idle_min=10.0, max_min=90.0, sample_sec=30.0)
        m = hr.kill_message(hr.Decision("wallcap"), cfg)
        self.assertEqual(m, "heavy: killed job at wall cap (HEAVY_MAX_MIN=90m)"
                            " — override per call: HEAVY_MAX_MIN=0 heavy <cmd>")


class Supervise(unittest.TestCase):
    """The I/O loop with injected clock, wait and sampler."""

    def make(self, cfg, samples, exit_at=None):
        t = [0.0]
        killed = []

        def wait_exit(sec):
            t[0] += sec
            return 0 if exit_at is not None and t[0] >= exit_at else None

        it = iter(samples)
        sampler = lambda: next(it, {1: 0.0})
        return t, killed, dict(clock=lambda: t[0], wait_exit=wait_exit, sampler=sampler,
                               kill=lambda: killed.append(t[0]))

    def test_exit_returns_child_code(self):
        cfg = hr.Config(5.0, 10.0, 90.0, 30.0)
        t, killed, seams = self.make(cfg, [], exit_at=60)
        self.assertEqual(hr.supervise(cfg, **seams), ("exit", 0))
        self.assertEqual(killed, [])

    def test_idle_kills_and_reports(self):
        cfg = hr.Config(5.0, 10.0, 90.0, 30.0)
        t, killed, seams = self.make(cfg, [])
        kind, msg = hr.supervise(cfg, **seams)
        self.assertEqual(kind, "killed")
        self.assertIn("killed idle job", msg)
        self.assertEqual(killed, [600.0])


class Tick(unittest.TestCase):
    """0.5 s supervise ticks: kill flag polling, sample cadence and progress reporting."""

    def seams(self, flag_at=None, sampler=None):
        t = [10.0]
        log = {"kill": [], "samples": [], "progress": []}

        def wait_exit(sec):
            t[0] += sec
            return None

        def sample():
            log["samples"].append(t[0])
            return sampler(t[0]) if sampler else {1: 0.0}

        return t, log, dict(
            clock=lambda: t[0], wait_exit=wait_exit, sampler=sample,
            kill=lambda: log["kill"].append(t[0]),
            kill_requested=lambda: flag_at is not None and t[0] >= flag_at,
            on_progress=log["progress"].append)

    def test_kill_flag_seen_within_one_tick(self):
        cfg = hr.Config(5.0, 10.0, 90.0, 30.0)
        t, log, seams = self.seams(flag_at=20.2)
        self.assertEqual(hr.supervise(cfg, **seams), ("remote", hr.REMOTE_KILL_MSG))
        self.assertEqual(len(log["kill"]), 1)
        self.assertLessEqual(log["kill"][0], 20.5)
        self.assertGreaterEqual(log["kill"][0], 20.2)
        self.assertEqual(hr.REMOTE_KILL_MSG, "heavy: killed from heavy-top")

    def test_sampling_cadence_unchanged_by_ticks(self):
        cfg = hr.Config(5.0, 0.0, 0.0, 30.0)
        t, log, seams = self.seams(flag_at=100.2)
        hr.supervise(cfg, **seams)
        self.assertEqual(log["samples"], [40.0, 70.0, 100.0])

    def test_on_progress_reports_watchdog_last_progress(self):
        cfg = hr.Config(5.0, 10.0, 0.0, 30.0)
        # busy until t=70 (well above 5s/600s), flat afterwards
        t, log, seams = self.seams(flag_at=160.0, sampler=lambda now: {1: min(now, 70.0)})
        hr.supervise(cfg, **seams)
        self.assertEqual(log["progress"], [40.0, 70.0, 70.0, 70.0])


class LastProgress(unittest.TestCase):
    def test_starts_at_start(self):
        self.assertEqual(dog(start=5.0).last_progress, 5.0)

    def test_last_progress_at_join(self):
        d = dog()
        d.observe(30, {1: 0.0})
        d.observe(60, {1: 0.0, 2: 0.0})
        self.assertEqual(d.last_progress, 60)

    def test_last_progress_at_or_above_kill_rate(self):
        d = dog()  # rate 5s / 600s; 30 s sample needs 0.25 s gain
        d.observe(30, {1: 0.25})
        self.assertEqual(d.last_progress, 30)
        d.observe(60, {1: 0.5})
        self.assertEqual(d.last_progress, 60)

    def test_no_progress_below_rate(self):
        d = dog()
        d.observe(30, {1: 0.1})
        d.observe(60, {1: 0.2})
        self.assertEqual(d.last_progress, 0.0)

    def test_disabled_idle_uses_any_gain(self):
        d = dog(idle_min=0)
        d.observe(30, {1: 0.0})
        self.assertEqual(d.last_progress, 0.0)
        d.observe(60, {1: 0.01})
        self.assertEqual(d.last_progress, 60)

    def test_failed_sample_is_not_progress(self):
        d = dog()
        d.observe(30, None)
        self.assertEqual(d.last_progress, 0.0)


class QueueCase(unittest.TestCase):
    """Temp HEAVY_HOME/HOME, fake time; saves and restores signal handlers main() installs."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.env = {"HOME": str(self.tmp), "HEAVY_HOME": str(self.tmp / "q")}
        self.paths = q.Paths.from_env(self.env)
        self.t = [1000.0]
        self.store = q.Store(self.paths, alive=lambda pid: True,
                             clock=lambda: self.t[0], sleep=self.advance)
        self.store.ensure()
        saved = {s: signal.getsignal(s) for s in hr.WAIT_SIGNALS}
        self.addCleanup(lambda: [signal.signal(s, h) for s, h in saved.items()])

    def advance(self, sec):
        self.t[0] += sec

    def hold_slot(self, i):
        fd = os.open(self.paths.home / "slots" / str(i), os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd

    def me(self, pid=os.getpid()):
        w = q.Waiter(pid=pid, cmd="job", cwd="/", enqueued=self.t[0], order=self.t[0])
        q.enqueue(self.store, w)
        return w


class WaitForSlot(QueueCase):
    def wait(self, me, sleep=None, pending=lambda: None):
        return hr.wait_for_slot(self.store, me, clock=lambda: self.t[0],
                                sleep=sleep or self.advance, pending=pending)

    def test_wait_admits_after_slot_frees(self):
        self.store.set_slots(1)
        fd = self.hold_slot(0)
        polls = []

        def sleep(sec):
            polls.append(sec)
            self.advance(sec)
            if len(polls) == 2:
                os.close(fd)

        got = self.wait(self.me(), sleep=sleep)
        self.assertIsInstance(got, q.Admitted)
        os.close(got.slot_fd)
        self.assertEqual(polls, [hr.TICK_SEC, hr.TICK_SEC])  # admitted on the third poll

    def test_wait_cancel_returns_cancelled_and_removes_entry(self):
        self.store.set_slots(1)
        self.addCleanup(os.close, self.hold_slot(0))
        me = self.me()

        def sleep(sec):
            q.cancel(self.store, me.pid, me.enqueued)

        self.assertIsInstance(self.wait(me, sleep=sleep), q.Cancelled)
        self.assertIsNone(self.store.get_waiter(me.pid))

    def test_wait_signal_removes_entry_and_returns_signum(self):
        self.store.set_slots(1)
        self.addCleanup(os.close, self.hold_slot(0))
        me = self.me()
        sig = []
        got = self.wait(me, sleep=lambda s: sig.append(signal.SIGINT), pending=lambda: sig[0] if sig else None)
        self.assertEqual(got, signal.SIGINT)
        self.assertIsNone(self.store.get_waiter(me.pid))

    def test_wait_paused_keeps_waiting_until_force(self):
        self.store.set_paused(True)
        me = self.me()
        polls = []

        def sleep(sec):
            polls.append(sec)
            if len(polls) == 3:
                q.force(self.store, me.pid, me.enqueued)

        got = self.wait(me, sleep=sleep)
        self.assertEqual((got.slot_fd, got.holder.forced, len(polls)), (None, True, 3))


class FakeProc:
    pid = 2 ** 22 + 4242  # never signalled: the test injects the killer

    def wait(self, timeout=None):
        raise subprocess.TimeoutExpired("fake", timeout)


class Main(QueueCase):
    def run_main(self, argv, **kw):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = hr.main(argv, self.env, clock=lambda: self.t[0], mono=lambda: self.t[0],
                         sleep=self.advance, **kw)
        return rc, err.getvalue()

    def test_main_uncreatable_home_exits_2_without_spawning(self):
        (self.tmp / "file").write_text("")
        self.env["HEAVY_HOME"] = str(self.tmp / "file" / "sub")
        spawned = []
        rc, err = self.run_main(["true"], spawn=lambda *a, **k: spawned.append(a))
        self.assertEqual((rc, spawned), (2, []))
        self.assertIn("heavy: cannot use HEAVY_HOME", err)

    def test_main_spawn_failure_releases_slot_and_entry(self):
        def spawn(*a, **k):
            raise FileNotFoundError(2, "No such file or directory")

        rc, err = self.run_main(["nope-cmd"], spawn=spawn)
        self.assertEqual(rc, 127)
        self.assertEqual(os.listdir(self.paths.home / "running"), [])
        self.assertEqual(os.listdir(self.paths.home / "queue"), [])
        os.close(self.hold_slot(0))  # lockable again

    def test_main_remote_kill_exit_143(self):
        killed = []

        def spawn(argv, **kw):
            self.assertEqual(kw.get("preexec_fn"), os.setpgrp)
            h = self.store.get_holder(os.getpid())
            self.assertEqual(q.kill(self.store, h.pid, h.started), "ok")
            return FakeProc()

        rc, err = self.run_main(["sleep", "30"], spawn=spawn,
                                killer=lambda proc, pgid, pids: killed.append(pgid))
        self.assertEqual(rc, 143)
        self.assertEqual(err.strip(), "heavy: killed from heavy-top")
        self.assertEqual(killed, [FakeProc.pid])
        self.assertEqual(os.listdir(self.paths.home / "running"), [])

    def test_main_cancelled_while_waiting_exits_125(self):
        self.store.set_slots(1)
        self.addCleanup(os.close, self.hold_slot(0))

        def sleep(sec):
            w = self.store.get_waiter(os.getpid())
            q.cancel(self.store, w.pid, w.enqueued)

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = hr.main(["true"], self.env, clock=lambda: self.t[0], mono=lambda: self.t[0],
                         sleep=sleep, spawn=lambda *a, **k: self.fail("spawned"))
        self.assertEqual((rc, err.getvalue().strip()), (125, "heavy: cancelled from heavy-top"))


class VersionGuard(unittest.TestCase):
    def test_old_python_gets_clear_error(self):
        old = shutil.which("python3.9") or "/usr/bin/python3"
        try:
            ver = subprocess.run([old, "-c", "import sys; print(sys.version_info >= (3, 10))"],
                                 capture_output=True, text=True, timeout=10).stdout.strip()
        except OSError:
            self.skipTest("no old python3 on this host")
        if ver != "False":
            self.skipTest("no python3 < 3.10 on this host")
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        for script in ("heavy-run", "heavy-top"):
            r = subprocess.run([old, str(ROOT / "bin" / script), "true"], capture_output=True, text=True,
                               env={"HOME": tmp, "HEAVY_HOME": tmp + "/q", "PATH": "/usr/bin:/bin"},
                               timeout=10)
            self.assertEqual(r.returncode, 2, (script, r.stderr))
            self.assertRegex(r.stderr, r"^heavy: needs python3 >= 3\.10 \(found 3\.\d+ at .+\)\n$")


if __name__ == "__main__":
    unittest.main()

import sys, importlib.util, importlib.machinery
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
loader = importlib.machinery.SourceFileLoader("ht", str(ROOT / "bin" / "heavy-top"))
spec = importlib.util.spec_from_loader("ht", loader)
ht = importlib.util.module_from_spec(spec); sys.modules["ht"] = ht; loader.exec_module(ht)
assert ht.parse_etime("05:07") == 307
assert ht.parse_etime("01:34:33") == 5673
assert ht.parse_etime("2-01:00:00") == 2*86400+3600
ps = """  10 1 0.0 10:00 perl /opt/homebrew/bin/sem --id heavy -j 2 --fg -q -u --will-cite npm run motion
  11 10 50.0 09:59 npm run motion
  12 11 120.5 09:58 node chrome
  20 1 0.0 01:00 perl /opt/homebrew/bin/sem --id heavy -j 2 --fg -q -u --will-cite pnpm check
  21 1 0.0 01:00 sem --id heavy -j 2 --fg ls
  30 1 0.0 00:30 perl /opt/homebrew/bin/sem --id heavyx -j 2 --fg foo
  40 1 0.0 05:00 grep sem --id heavy
  41 1 0.0 05:00 python3 - perl /opt/homebrew/bin/sem --id heavy -j 2"""
p = ht.parse_ps(ps)
s = ht.build_snapshot(p, [10, 99], 2, (1, 2, 3), 14)
assert [j.pid for j in s.running] == [10, 99], s.running
assert s.running[0].command == "npm run motion" and abs(s.running[0].cpu - 170.5) < 1e-6
assert not s.running[1].alive
assert sorted(j.pid for j in s.waiting) == [20, 21], s.waiting
assert ht.lclip("abcdef", 4) == "…def"
print(ht.render(s, [1, 5, 20, 30], 100, 2, color=False))
# elapsed for a holder = time since sem started its job (child), not since it began waiting
assert s.running[0].elapsed == 599, s.running[0].elapsed
# stale holder doesn't eat a free row
r = ht.render(s, [], 100, 2, color=False)
assert "99  (stale" in r and r.count("free") == 1  # header says 1/2 used, so one free row
s3 = ht.build_snapshot(p, [10, 99], 3, (1,2,3), 14)
assert ht.render(s3, [], 100, 2, color=False).count("free") == 2
# many waiters are capped to the terminal height
many = "\n".join(f"  {500+i} 1 0.0 01:00 perl /opt/homebrew/bin/sem --id heavy -j 2 --fg -q -u --will-cite job{i}" for i in range(40))
s2 = ht.build_snapshot(ht.parse_ps(many), [], 2, (1,2,3), 14, cwd_of=lambda pid: "/tmp/x")
out = ht.render(s2, [1], 100, 2, color=False, max_rows=30)
assert len(out.splitlines()) <= 30, len(out.splitlines())
assert "more" in out
print("ok")

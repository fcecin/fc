"""The site's stats: the numbers and names of what it has and uses.

The stats process (`manage.py fc_stats`) writes the report every REPORT_SECS
to FC_STATS_FILE, in memory, and the pages read only that file. The report
reads a few database counts, the cached network graph, the hub, and the
cgroups of the site's units and the CES servers.
"""

import fnmatch
import glob
import json
import os
import time
from datetime import timedelta

from django.conf import settings
from django.db.models import Sum
from django.utils import timezone

from . import credits, net, push
from .models import ChatLine, Profile, Widget

REPORT_SECS = 30
STALE_SECS = 5 * REPORT_SECS     # an older report shows its age
CGROUP = "/sys/fs/cgroup"
PROC = "/proc"
UNITS = ["system.slice/ces.service", "system.slice/fc-*.service",
         "system.slice/system-fcnet.slice/fcnet@*.service", "fc.slice/fc-shells.slice"]
# The CES server units, each with the server's process and one cesluajitd
# per running program. /proc can hide other users' process names (hidepid), so
# the program count is the unit's process count, less the server.
SERVERS = ("ces", "fc-ces", "fcnet@*")


def _read(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return None


def _int(text):
    """The first number of `text`, whole: "90061.5 1000.0" is 90061."""
    try:
        return int(float(text.split()[0]))
    except (AttributeError, IndexError, ValueError):
        return None


def _usage(d):
    """The CPU time of a cgroup, in µs."""
    for line in (_read(os.path.join(d, "cpu.stat")) or "").splitlines():
        if line.startswith("usage_usec "):
            return _int(line[11:])
    return None


def _processes(d):
    return len((_read(os.path.join(d, "cgroup.procs")) or "").split())


def _units(cgroup, before, dt):
    out, usage = [], {}
    for pattern in UNITS:
        for d in sorted(glob.glob(os.path.join(cgroup, pattern))):
            name = os.path.basename(d).removesuffix(".service")
            u = _usage(d)
            usage[name] = u
            last = before.get(name)
            cpu = round((u - last) / (dt * 1e6) * 100, 1) if u is not None and last is not None and dt > 0 else None
            server = any(fnmatch.fnmatchcase(name, s) for s in SERVERS)
            out.append({"name": name, "memory": _int(_read(os.path.join(d, "memory.current"))), "cpu": cpu,
                        "tasks": _int(_read(os.path.join(d, "pids.current"))),
                        "programs": max(0, _processes(d) - 1) if server else None})
    return out, usage


def _host(proc, before, dt):
    load = (_read(os.path.join(proc, "loadavg")) or "").split()[:3]
    mem = {}
    for line in (_read(os.path.join(proc, "meminfo")) or "").splitlines():
        k, _, v = line.partition(":")
        mem[k] = _int(v)
    fields = [int(x) for x in ((_read(os.path.join(proc, "stat")) or "").splitlines() or [""])[0].split()[1:]]
    cpu = (sum(fields), fields[3] + fields[4]) if len(fields) > 4 else None     # all, and idle with iowait
    busy = None
    if cpu and before and before[0] < cpu[0]:
        busy = round(100 * (1 - (cpu[1] - before[1]) / (cpu[0] - before[0])), 1)
    try:
        st = os.statvfs("/")
        disk = {"total": st.f_blocks * st.f_frsize, "free": st.f_bavail * st.f_frsize}
    except OSError:
        disk = None
    return {"load": [float(x) for x in load], "cpus": os.cpu_count(), "busy": busy,
            "memory": {"total": (mem.get("MemTotal") or 0) * 1024, "available": (mem.get("MemAvailable") or 0) * 1024},
            "disk": disk, "up": _int(_read(os.path.join(proc, "uptime")))}, cpu


def build(before=None, cgroup=CGROUP, proc=PROC):
    """The report now. `before` is the CPU times that the last build returned,
    for each unit's CPU since then. Returns (the report, the CPU times now)."""
    before = before or {}
    now, mono = timezone.now(), time.monotonic()
    dt = mono - before["at"] if "at" in before else 0
    day = now - timedelta(days=1)
    units, usage = _units(cgroup, before.get("units", {}), dt)
    host, cpu = _host(proc, before.get("cpu"), dt)
    g = net.graph() or {}
    links = {tuple(sorted((a, b))) for a, ps in g.items() for b in ps if a != b}
    lines = ChatLine.objects.filter(created__gte=day)
    hub = push.ask({"stats": {}}, 2.0).get("ok")
    db = settings.DATABASES["default"]["NAME"]
    report = {
        "at": int(time.time()),
        "fc": (_read(os.path.join(settings.BASE_DIR, "VERSION")) or "").strip(),
        "name": settings.FC_NAME,
        "people": {"accounts": Profile.objects.count(), "seen": Profile.objects.filter(seen__gte=day).count(),
                   "new": Profile.objects.filter(created__gte=day).count()},
        "hub": hub,
        "chat": {"answers": lines.filter(kind="model").count(), "said": lines.filter(kind="user").count(),
                 "runs": lines.filter(kind="run").count(),
                 "charged": lines.filter(kind="model").aggregate(n=Sum("charged"))["n"] or 0},
        "desk": {"windows": Widget.objects.count(), "programs": Widget.objects.filter(kind="program").count()},
        "network": {"servers": len(set(g) | {p for ps in g.values() for p in ps}), "links": len(links),
                    "home": settings.FC_LEDGER},
        "host": host,
        "units": units,
        "database": os.path.getsize(db) if isinstance(db, (str, os.PathLike)) and os.path.exists(db) else None,
    }
    return report, {"at": mono, "units": usage, "cpu": cpu}


def write(report, path):
    """Replace the report in `path` at once, so a reader sees the old one or the new one."""
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(report, f)
    os.replace(tmp, path)


def loop(path, every=REPORT_SECS):
    before = None
    while True:
        t0 = time.monotonic()
        report, before = build(before)
        write(report, path)
        time.sleep(max(1.0, every - (time.monotonic() - t0)))


_kept = {"stamp": None, "report": None}


def read(path=None):
    """The last report, or None, kept until the file changes."""
    path = path or settings.FC_STATS_FILE
    try:
        st = os.stat(path)
    except OSError:
        return None
    stamp = (path, st.st_mtime_ns, st.st_size)
    if _kept["stamp"] != stamp:
        try:
            with open(path) as f:
                _kept["report"] = json.load(f)
        except (OSError, ValueError):
            return None
        _kept["stamp"] = stamp
    return _kept["report"]


# --- As text -------------------------------------------------------------------

def size(n):
    if n is None:
        return "-"
    for unit in ("", "K", "M", "G", "T"):
        if n < 1024 or unit == "T":
            return f"{n:.0f}{unit}" if unit == "" or n >= 100 else f"{n:.1f}{unit}"
        n /= 1024


def _n(n, one, many=None):
    return f"{n} {one if n == 1 else many or one + 's'}"


def _credit(raw):
    """Credit to two decimals, less trailing zeros: 1.5, 345.56."""
    t = f"{raw / credits.UNIT:.2f}"
    return t.rstrip("0").rstrip(".")


def _span(secs):
    d, h, m = secs // 86400, secs % 86400 // 3600, secs % 3600 // 60
    return _n(d, "day") if d else _n(h, "hour") if h else _n(m, "minute")


def text(r, now=None):
    """The report as lines of text, for the front page and /stats."""
    now = now or time.time()
    p, c, h, n = r["people"], r["chat"], r["host"], r["network"]
    stamp = time.strftime("%H:%M:%S UTC", time.gmtime(r["at"]))
    head = f"{r['name']} {r['fc'] or ''}".rstrip() + f", {stamp}"
    if now - r["at"] > STALE_SECS:
        head += f" ({_span(int(now - r['at']))} ago)"
    rows = [
        ("people", f"{_n(p['accounts'], 'account')}, {p['seen']} seen in 24 h, {p['new']} new in 24 h"),
    ]
    if r.get("hub"):
        u = r["hub"]
        rows.append(("online", f"{_n(u['people'], 'person', 'people')}, {_n(u['visitors'], 'visitor')}, "
                               f"{_n(u['pages'], 'page')}"))
        rows.append(("shells", f"{u['shells']} running"))
    rows += [
        ("chat", f"{_n(c['answers'], 'answer')} in 24 h, {_n(c['said'], 'line')} from people, "
                 f"{_n(c['runs'], 'shell line')}, {_credit(c['charged'])} credits of thought"),
        ("desk", f"{_n(r['desk']['windows'], 'window')}, {r['desk']['programs']} on programs"),
        ("network", f"{_n(n['servers'], 'server')}, {_n(n['links'], 'link')}, home {n['home']}"),
        ("", ""),
        ("load", " ".join(f"{x:.2f}" for x in h["load"]) + f", {_n(h['cpus'], 'cpu')}"
                 + (f", {h['busy']}% busy" if h["busy"] is not None else "")),
        ("memory", f"{size(h['memory']['total'] - h['memory']['available'])} used of {size(h['memory']['total'])}"),
    ]
    if h["disk"]:
        rows.append(("disk", f"{size(h['disk']['total'] - h['disk']['free'])} used of {size(h['disk']['total'])}"))
    if h["up"] is not None:
        rows.append(("up", _span(h["up"])))
    rows.append(("database", size(r["database"])))
    out = [head, ""] + [f"{k:<10} {v}".rstrip() for k, v in rows]
    if r["units"]:
        w = max(len(u["name"]) for u in r["units"])
        out += ["", f"{'unit':<{w}}  {'memory':>7}  {'cpu':>6}  {'tasks':>5}  programs"]
        for u in r["units"]:
            cpu = "-" if u["cpu"] is None else f"{u['cpu']}%"
            out.append(f"{u['name']:<{w}}  {size(u['memory']):>7}  {cpu:>6}  {u['tasks'] if u['tasks'] is not None else '-':>5}"
                       f"  {'' if u['programs'] is None else u['programs']}".rstrip())
    return "\n".join(out) + "\n"

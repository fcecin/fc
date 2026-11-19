"""A shell's jobs: background commands, apart from the pty, with their output
in a file. bin/job asks for them on ~/.fc/jobs/sock, and ptyhost.py runs
them in its loop. A job can wake the assistant once, at its end (--wake) or
at the first matching line (--until), and the jobs end with the shell.
"""

import json
import os
import re
import selectors
import signal
import socket
import subprocess
import time
from collections import deque

MAX_LIVE = 8              # the most jobs running at once
MAX_LOG = 1 << 20         # a log file's size, past which it moves to <id>.log.1 and a new one starts
TAIL_LINES = 20           # the last lines that a wake gives the assistant
TAIL_BYTES = 4096
LINE_MAX = 4096           # a longer line is cut at this length
KEEP = 50                 # the ended jobs that `job ls` shows
KILL_SECS = 2.0           # the wait from SIGTERM to SIGKILL
MAX_REQUEST = 16 * 1024


def span(secs):
    secs = int(secs)
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m{secs % 60:02d}s"
    return f"{secs // 3600}h{secs % 3600 // 60:02d}m"


class Job:
    def __init__(self, n, cmd, cwd, wake, until, log):
        self.n, self.cmd, self.wake, self.until, self.log = n, cmd, wake, until, log
        self.pattern = re.compile(until) if until else None
        self.started = time.monotonic()
        self.ended = None
        self.code = None
        self.eof = False
        self.woke = False
        self.kill_at = None
        self.tail = deque(maxlen=TAIL_LINES)
        self.part = b""
        self.size = 0
        self.file = open(log, "wb")
        self.proc = subprocess.Popen(["bash", "-c", cmd], cwd=cwd, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
        self.fd = self.proc.stdout.fileno()
        os.set_blocking(self.fd, False)

    @property
    def pid(self):
        return self.proc.pid

    def write(self, data):
        """Write output to the log, MAX_LOG bytes per file."""
        while data:
            if self.size >= MAX_LOG:
                self.file.close()
                os.replace(self.log, self.log + ".1")
                self.file = open(self.log, "wb")
                self.size = 0
            part, data = data[:MAX_LOG - self.size], data[MAX_LOG - self.size:]
            self.file.write(part)
            self.size += len(part)
        self.file.flush()

    def feed(self, data):
        """Take the job's output, and return the first line that matches the
        pattern, or None."""
        self.write(data)
        hit = None
        lines = (self.part + data).split(b"\n")
        self.part = lines.pop()
        if len(self.part) > LINE_MAX:
            lines.append(self.part)
            self.part = b""
        for raw in lines:
            line = raw.decode(errors="replace").rstrip("\r")
            self.tail.append(line[:LINE_MAX])
            if hit is None and self.pattern is not None and self.pattern.search(line):
                hit = line
        return hit

    def last(self):
        lines = list(self.tail) + ([self.part.decode(errors="replace")] if self.part else [])
        text = "\n".join(lines).strip("\n")
        if len(text.encode()) > TAIL_BYTES:
            text = "[...]\n" + text.encode()[-TAIL_BYTES:].decode(errors="ignore")
        return text

    def state(self):
        if self.code is None:
            return "running"
        return f"exit {self.code}" if self.code >= 0 else f"signal {-self.code}"

    def info(self, now):
        return {"id": self.n, "pid": self.pid, "cmd": self.cmd, "state": self.state(),
                "secs": int((self.ended or now) - self.started), "wake": self.wake, "until": self.until}


class Jobs:
    """One shell's jobs. `sel` is the selector of ptyhost's loop, and
    `wake(text)` sends a wake to the hub."""

    def __init__(self, home, sel, wake):
        self.sel = sel
        self.wake = wake
        self.dir = os.path.join(home, ".fc", "jobs")
        os.makedirs(self.dir, exist_ok=True)
        # An older shell's jobs ended with it, so their files go.
        for name in os.listdir(self.dir):
            try:
                os.unlink(os.path.join(self.dir, name))
            except OSError:
                pass
        self.path = os.path.join(self.dir, "sock")
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.sock.bind(self.path)
        os.chmod(self.path, 0o600)
        self.sock.listen(8)
        self.sock.setblocking(False)
        sel.register(self.sock, selectors.EVENT_READ, self)
        self.jobs = {}
        self.next = 1

    def live(self):
        return [j for j in self.jobs.values() if j.code is None]

    # --- bin/job's requests ------------------------------------------------

    def accept(self):
        try:
            conn, _ = self.sock.accept()
        except OSError:
            return
        with conn:
            conn.settimeout(1.0)
            try:
                req = json.loads(conn.recv(MAX_REQUEST))
                reply = self.request(req)
            except (OSError, ValueError):
                return
            try:
                conn.send(json.dumps(reply).encode())
            except OSError:
                pass

    def request(self, req):
        if not isinstance(req, dict) or len(req) != 1:
            return {"error": "a bad request"}
        (op, a), = req.items()
        if op == "run":
            return self.run(a)
        if op == "ls":
            now = time.monotonic()
            return {"ok": [j.info(now) for j in self.jobs.values()]}
        if op == "kill":
            j = self.jobs.get(a.get("id") if isinstance(a, dict) else None)
            if j is None:
                return {"error": "no such job"}
            if j.code is None:
                self.signal(j, signal.SIGTERM)
                j.kill_at = time.monotonic() + KILL_SECS
            return {"ok": j.info(time.monotonic())}
        return {"error": f"no op {op}"}

    def run(self, a):
        if not isinstance(a, dict) or not isinstance(a.get("cmd"), str) or not a["cmd"].strip():
            return {"error": "give a command"}
        if len(self.live()) >= MAX_LIVE:
            return {"error": f"{MAX_LIVE} jobs run already: end one first"}
        until = a.get("until") or None
        if until is not None:
            try:
                re.compile(until)
            except (re.error, TypeError) as e:
                return {"error": f"a bad pattern: {e}"}
        cwd = a.get("cwd") if isinstance(a.get("cwd"), str) and os.path.isdir(a["cwd"]) else None
        n = self.next
        try:
            j = Job(n, a["cmd"], cwd, bool(a.get("wake")) or until is not None, until,
                    os.path.join(self.dir, f"{n}.log"))
        except OSError as e:
            return {"error": f"the job did not start: {e.strerror or e}"}
        self.next += 1
        self.jobs[n] = j
        self.sel.register(j.fd, selectors.EVENT_READ, j)
        ended = [k for k, x in self.jobs.items() if x.code is not None]
        for k in ended[:max(0, len(ended) - KEEP)]:
            del self.jobs[k]
        return {"ok": j.info(time.monotonic())}

    # --- the loop -------------------------------------------------------------

    def readable(self, j):
        """Read a job's output once. True when more can come now."""
        try:
            data = os.read(j.fd, 65536)
        except BlockingIOError:
            return False
        except OSError:
            data = b""
        if not data:
            j.eof = True
            self.sel.unregister(j.fd)
            j.proc.stdout.close()
            j.file.close()
            return False
        hit = j.feed(data)
        if hit is not None and j.code is None and j.wake and not j.woke:
            j.woke = True
            self.wake(f"job {j.n} printed a line that matches /{j.until}/ after {span(time.monotonic() - j.started)}"
                      f", and it still runs: `{j.cmd}`.\nIts last lines:\n{j.last()}")
        return True

    def check(self):
        """On every loop turn, handle the jobs that ended and the kills due."""
        now = time.monotonic()
        for j in list(self.jobs.values()):
            if j.code is not None:
                continue
            if j.kill_at is not None and now >= j.kill_at:
                self.signal(j, signal.SIGKILL)
                j.kill_at = None
            code = j.proc.poll()
            if code is None:
                continue
            # Read its last output from the pipe before the wake. The job has
            # ended, so a line that matches here goes in the end's wake.
            j.code, j.ended = code, now
            while not j.eof and self.readable(j):
                pass
            if j.wake and not j.woke:
                j.woke = True
                last = j.last()
                self.wake(f"job {j.n} ended with {j.state()} after {span(now - j.started)}: `{j.cmd}`.\n"
                          + (f"Its last lines:\n{last}" if last else "It printed nothing."))

    def signal(self, j, sig):
        try:
            os.killpg(j.pid, sig)
        except OSError:
            pass

    def close(self):
        for j in self.live():
            self.signal(j, signal.SIGKILL)
        try:
            self.sock.close()
            os.unlink(self.path)
        except OSError:
            pass

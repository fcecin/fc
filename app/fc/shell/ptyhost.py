#!/usr/bin/python3
"""The main process of a shell unit: bash on a pty, relayed to the hub
(wire.py). bash survives a hub restart, and this process reconnects every
second and replays the last REPLAY bytes. It types the assistant's commands
(RUN) at the prompt, reads bashrc's OSC 133 marks to see each command start
and end, strips the marks, and sets aside the person's typed line meanwhile.
It ends with bash, at SIGTERM, or after FCSH_AWAY seconds with no hub.
"""

import errno
import fcntl
import hashlib
import json
import os
import pty
import re
import selectors
import signal
import socket
import struct
import sys
import termios
import time
from collections import deque

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import jobs  # noqa: E402
import wire  # noqa: E402

REPLAY = 64 * 1024        # the output that a new connection gets first
RUN_OUT = 1024 * 1024     # the most output of an assistant command that goes to the hub
MINE_MAX = 8 * 1024       # the most of a person's command kept for the assistant
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
HERE = os.path.dirname(os.path.abspath(__file__))
RCFILE = os.path.join(HERE, "bashrc")
MARK = re.compile(rb"\x1b\]133;([ABCDL])(?:;(-?\d+))?\x07")
TAKE, GIVE = b"\x1b[9998~", b"\x1b[9997~"     # bound in bashrc
PASTE = (b"\x1b[200~", b"\x1b[201~")
MAX_QUEUE = 256           # frames queued for the hub, past which bash waits
RETRY = 1.0               # seconds between connects


class Host:
    def __init__(self, path, away, argv, home=None):
        self.path = path
        self.away = away
        self.sel = selectors.DefaultSelector()
        self.wakes = []               # WAKE frames made while the hub is away
        self.jobs = jobs.Jobs(home, self.sel, self.wake) if home else None
        self.sock = None
        self.queue = deque()
        self.replay = bytearray()
        self.keys = bytearray()      # input waiting to go to bash
        self.lost = time.monotonic()  # when the hub went away
        self.next_try = 0.0
        self.code = None
        self.carry = b""              # the start of a mark that the next read finishes
        self.out_buf = bytearray()    # bash output waiting to go to the hub
        self.at_prompt = False        # bash waits for a line at its prompt
        self.waiting = deque()        # (id, line): assistant commands to type
        self.cur = None               # the running assistant command
        self.taken = False            # the person's line waits aside
        self.mine = None              # the person's command at this prompt: [typed, printed, runs]
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            try:
                os.execvp(argv[0], argv)
            finally:
                os._exit(127)
        os.set_blocking(self.fd, False)
        self.sel.register(self.fd, selectors.EVENT_READ, "pty")

    # --- the hub ----------------------------------------------------------

    def connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        try:
            s.connect(self.path)
            s.send(wire.hello(kind="term", pid=os.getpid(), v=wire.VERSION))
        except OSError:
            s.close()
            return
        s.setblocking(False)
        self.sock = s
        self.queue.clear()
        self.queue.extend(wire.frame(wire.OUT, c) for c in wire.chunks(bytes(self.replay)))
        self.queue.extend(self.wakes)
        self.wakes.clear()
        self.sel.register(s, selectors.EVENT_READ, "hub")
        self.watch()

    def wake(self, text):
        f = wire.frame(wire.WAKE, json.dumps({"text": text}).encode()[:wire.MAX_MSG - 1])
        if self.sock is None:
            self.wakes.append(f)
        else:
            self.queue.append(f)
            self.watch()

    def drop(self):
        self.waiting.clear()        # the hub that sent them is gone
        if self.sock is not None:
            self.sel.unregister(self.sock)
            self.sock.close()
            self.sock = None
            self.queue.clear()
            self.lost = time.monotonic()
            self.next_try = self.lost + RETRY
            self.watch()

    def watch(self):
        pty_events = 0 if len(self.queue) > MAX_QUEUE else selectors.EVENT_READ
        if self.keys:
            pty_events |= selectors.EVENT_WRITE
        self.modify(self.fd, pty_events, "pty")
        if self.sock is not None:
            events = selectors.EVENT_READ | (selectors.EVENT_WRITE if self.queue else 0)
            self.modify(self.sock, events, "hub")

    def modify(self, f, events, tag):
        try:
            key = self.sel.get_key(f)
        except KeyError:
            key = None
        if not events:
            if key is not None:
                self.sel.unregister(f)
        elif key is None:
            self.sel.register(f, events, tag)
        elif key.events != events:
            self.sel.modify(f, events, tag)

    def from_hub(self):
        try:
            msg = self.sock.recv(wire.MAX_MSG)
        except BlockingIOError:
            return
        except OSError:
            msg = b""
        if not msg:
            self.drop()
            return
        kind, payload = msg[0], msg[1:]
        if kind == wire.RUN:
            self.start_run(payload)
        elif kind == wire.KILL:
            self.kill_run(payload)
        elif kind == wire.IN:
            self.keys += payload
        elif kind == wire.SIZE:
            size = wire.unsize(payload)
            if size:
                cols, rows = size
                fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    def to_hub(self):
        while self.queue:
            try:
                self.sock.send(self.queue[0])
            except BlockingIOError:
                return
            except OSError:
                self.drop()
                return
            self.queue.popleft()

    # --- Assistant commands --------------------------------------

    def start_run(self, payload):
        try:
            req = json.loads(payload)
            rid, line = int(req["id"]), str(req["line"])
        except (ValueError, KeyError, TypeError):
            return
        self.waiting.append((rid, line))
        self.next_run()

    def kill_run(self, payload):
        """A command's time ran out. A waiting command is dropped, and a
        running one gets Ctrl+C, as from the person."""
        try:
            rid = int(json.loads(payload)["id"])
        except (ValueError, KeyError, TypeError):
            return
        for item in list(self.waiting):
            if item[0] == rid:
                self.waiting.remove(item)
                self.run_end(rid, None, busy=True)
                return
        if self.cur is not None and self.cur["id"] == rid:
            if self.cur["phase"] == "take":
                self.cur = None
                self.run_end(rid, None, busy=True)
            else:
                self.keys += b"\x03"

    def next_run(self):
        """At the prompt, type the next command, or give the person's line
        back."""
        if self.cur is not None or not self.at_prompt:
            return
        if self.waiting:
            rid, line = self.waiting.popleft()
            self.cur = {"id": rid, "line": line, "phase": "take", "echo": bytearray(), "out": 0}
            self.at_prompt = False
            self.mine = None
            if self.taken:
                self.type_cur()
            else:
                self.keys += TAKE
        elif self.taken:
            self.keys += GIVE
            self.taken = False

    def type_cur(self):
        """Paste the command at the prompt, and press Enter."""
        self.cur["phase"] = "typed"
        self.keys += PASTE[0] + self.cur["line"].encode() + PASTE[1] + b"\r"

    def mark(self, kind, arg):
        if kind == b"B":
            if self.cur is None:
                self.at_prompt = True
                self.mine = [bytearray(), bytearray(), False]
                self.next_run()
        elif kind == b"L":
            if self.cur is not None and self.cur["phase"] == "take":
                self.taken = int(arg or 0) > 0
                self.type_cur()
        elif kind == b"C":
            self.at_prompt = False
            if self.cur is not None and self.cur["phase"] == "typed":
                self.cur["phase"] = "running"
            elif self.cur is None and self.mine is not None:
                self.mine[2] = True
        elif kind == b"D":
            if self.cur is None and self.mine is not None and self.mine[2]:
                typed, printed = plain(self.mine[0]).strip(), plain(self.mine[1])
                self.mine = None
                if self.sock is not None and typed:
                    self.flush()
                    self.queue.append(wire.frame(wire.MINE, json.dumps({"line": typed, "out": printed}).encode()))
            if self.cur is not None and self.cur["phase"] in ("typed", "running"):
                cur, self.cur = self.cur, None
                if cur["phase"] == "typed":        # no command started: send what bash printed
                    self.run_out(cur, bytes(cur["echo"]))
                self.run_end(cur["id"], int(arg) if arg else -1)

    def output(self, seg):
        """What bash printed between two marks."""
        if not seg:
            return
        self.out_buf += seg
        cur = self.cur
        if cur is None:
            if self.mine is not None:
                part = self.mine[1] if self.mine[2] else self.mine[0]
                if len(part) < MINE_MAX:
                    part += seg[:MINE_MAX - len(part)]
            return
        if cur["phase"] == "running":
            self.run_out(cur, seg)
        elif cur["phase"] == "typed" and len(cur["echo"]) < RUN_OUT:
            cur["echo"] += seg

    def run_out(self, cur, data):
        if cur["out"] < RUN_OUT and self.sock is not None and data:
            self.flush()
            self.queue.append(wire.runout(cur["id"], data[:RUN_OUT - cur["out"]]))
        cur["out"] += len(data)

    def run_end(self, rid, code, busy=False):
        if self.sock is not None:
            self.flush()
            self.queue.append(wire.frame(wire.RUNEND, json.dumps({"id": rid, "exit": code, "busy": busy}).encode()))

    def flush(self):
        """Send bash's output to the hub before a frame that follows it."""
        if self.out_buf:
            data, self.out_buf = bytes(self.out_buf), bytearray()
            self.replay += data
            del self.replay[:-REPLAY]
            if self.sock is not None:
                self.queue.extend(wire.frame(wire.OUT, c) for c in wire.chunks(data))

    # --- bash -------------------------------------------------------------

    def from_bash(self):
        try:
            data = os.read(self.fd, wire.MAX_MSG - 1)
        except BlockingIOError:
            return
        except OSError as e:
            if e.errno != errno.EIO:
                raise
            data = b""
        if not data:
            self.flush()
            self.end()
            return
        data, self.carry = self.carry + data, b""
        cut = data.rfind(b"\x1b]")
        if cut != -1 and len(data) - cut < 64 and b"\x07" not in data[cut:]:
            data, self.carry = data[:cut], data[cut:]     # a mark that the next read finishes
        pos = 0
        for m in MARK.finditer(data):
            self.output(data[pos:m.start()])
            self.mark(m.group(1), m.group(2))
            pos = m.end()
        self.output(data[pos:])
        self.flush()

    def to_bash(self):
        try:
            n = os.write(self.fd, self.keys)
        except BlockingIOError:
            return
        except OSError:
            self.keys.clear()
            return
        del self.keys[:n]

    def reaped(self, block=False):
        """True when bash has ended. Sets the exit code."""
        if self.code is not None:
            return True
        pid, status = os.waitpid(self.pid, 0 if block else os.WNOHANG)
        if pid == 0:
            return False
        self.code = os.waitstatus_to_exitcode(status)
        return True

    def end(self):
        """bash is gone: tell the hub, and stop. The unit ends, and its jobs
        with it, even a job that holds the pty."""
        self.reaped(block=True)
        if self.jobs is not None:
            self.jobs.close()
        if self.sock is not None:
            self.sock.setblocking(True)
            self.sock.settimeout(2)
            try:
                for f in self.queue:
                    self.sock.send(f)
                self.sock.send(wire.frame(wire.END, json.dumps({"exit": self.code}).encode()))
            except OSError:
                pass

    def stop(self, *_):
        if self.jobs is not None:
            self.jobs.close()
        try:
            os.killpg(self.pid, signal.SIGHUP)
        except OSError:
            pass
        sys.exit(0)

    def run(self):
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGHUP, self.stop)
        while self.code is None:
            now = time.monotonic()
            if self.sock is None:
                if now - self.lost > self.away:
                    self.stop()
                if now >= self.next_try:
                    self.next_try = now + RETRY
                    self.connect()
            self.watch()
            wait = 1.0 if self.sock is not None else max(0.0, min(1.0, self.next_try - time.monotonic()))
            for key, events in self.sel.select(wait):
                if isinstance(key.data, jobs.Job):
                    self.jobs.readable(key.data)
                elif isinstance(key.data, jobs.Jobs):
                    key.data.accept()
                elif key.data == "pty":
                    if events & selectors.EVENT_READ:
                        self.from_bash()
                        if self.code is not None:
                            break
                    if events & selectors.EVENT_WRITE:
                        self.to_bash()
                elif self.sock is not None:
                    if events & selectors.EVENT_READ:
                        self.from_hub()
                    if self.sock is not None and events & selectors.EVENT_WRITE:
                        self.to_hub()
            if self.jobs is not None:
                self.jobs.check()
            if self.code is None and self.reaped():
                self.end()
        return self.code


BASHRC = """\
# The shell of fc. cesh and desk are commands here: cesh man, desk help.
PS1='\\[\\e[38;2;255;179;71m\\]\\w\\[\\e[0m\\] \\$ '
"""


def plain(data):
    """Terminal output as plain text, with the escape codes stripped and the
    backspaces of an edited line applied."""
    text = ANSI.sub("", data.decode(errors="replace")).replace("\r\n", "\n")
    out = []
    for ch in text:
        if ch == "\b":
            if out and out[-1] != "\n":
                out.pop()
        elif ch == "\r":
            while out and out[-1] != "\n":
                out.pop()
        else:
            out.append(ch)
    return "".join(out)


AGENTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "AGENTS.md")


def plant(home, src=AGENTS):
    """Copy the site's AGENTS.md to ~/AGENTS.md when that file is missing,
    or is still the copy that the site put there last, whose sha256 is in
    ~/.fc/AGENTS.md.sha256."""
    if not home or not os.path.isdir(home):
        return
    try:
        with open(src, "rb") as f:
            new = f.read()
    except OSError:
        return
    dst = os.path.join(home, "AGENTS.md")
    mark = os.path.join(home, ".fc", "AGENTS.md.sha256")
    try:
        with open(dst, "rb") as f:
            cur = f.read()
    except OSError:
        cur = None
    try:
        with open(mark) as f:
            last = f.read().strip()
    except OSError:
        last = None
    if cur is not None and cur != new and hashlib.sha256(cur).hexdigest() != last:
        return      # the person's own copy
    try:
        os.makedirs(os.path.dirname(mark), exist_ok=True)
        if cur != new:
            with open(dst, "wb") as f:
                f.write(new)
        with open(mark, "w") as f:
            f.write(hashlib.sha256(new).hexdigest())
    except OSError:
        pass


def seed(home):
    """A new home's ~/.bashrc, and ~/.tmp for TMPDIR."""
    if not home or not os.path.isdir(home):
        return
    rc = os.path.join(home, ".bashrc")
    try:
        if not os.path.exists(rc):
            with open(rc, "w") as f:
                f.write(BASHRC)
        os.makedirs(os.path.join(home, ".tmp"), exist_ok=True)
    except OSError:
        pass


def main():
    path = os.environ.get("FCSH_SOCKET", wire.SOCKET)
    away = float(os.environ.get("FCSH_AWAY", "600"))
    home = os.environ.get("HOME", "")
    seed(home)
    plant(home)
    # bash and its commands find the jobs here (bin/job).
    os.environ["FCSH_JOBS"] = os.path.join(home, ".fc", "jobs", "sock")
    argv = sys.argv[1:] or ["bash", "--rcfile", RCFILE, "-i"]
    code = Host(path, away, argv, home or None).run()
    return code if isinstance(code, int) and code >= 0 else 1


if __name__ == "__main__":
    sys.exit(main())

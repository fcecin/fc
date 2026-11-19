"""The people's shells, held by the hub ("The shells" in USING.md).

A person has at most one shell, bash in its own systemd unit
(fc/shell/shelld.py), or a plain process in development (Local). The shell
connects to <run>/u<uid>/hub.sock (fc/shell/wire.py) with bash's pty and one
connection per cesh or desk command. The hub keeps the last REPLAY bytes for
a page that reattaches, stops a shell after IDLE seconds with no page, and
past MAX shells stops the one longest without a page.
"""

import asyncio
import codecs
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import time

from .shell import wire

IDLE = 30 * 60
MAX = 200
REPLAY = 64 * 1024
RUN_KEEP = 256 * 1024   # the output of a model line kept for the model
MINE_KEEP = 8 * 1024    # the newest part of the person's commands kept for the model
UP = 15.0         # seconds for a new shell to connect
LOST = 5.0        # seconds for a shell to reconnect its broken pty connection
GC_EVERY = 30.0
STARTING, LIVE, ENDED = "starting", "live", "ended"
# Terminal escape codes, stripped before the model reads the output.
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
BUSY = "the shell is busy: a program runs in it"

log = logging.getLogger("fc.shellhub")


class ShellError(Exception):
    pass


class Systemd:
    """Shells as systemd units, through fc-shelld."""

    def __init__(self, path):
        self.path = path

    def _ask(self, req):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        s.settimeout(30)
        try:
            s.connect(self.path)
            s.send(json.dumps(req).encode())
            r = json.loads(s.recv(4096))
        except (OSError, ValueError) as e:
            raise ShellError(f"the shells do not start now ({e})")
        finally:
            s.close()
        if "error" in r:
            raise ShellError(str(r["error"]))
        return r

    def start(self, uid):
        self._ask({"start": uid})

    def stop(self, uid):
        self._ask({"stop": uid})

    def wipe(self, uid):
        self._ask({"wipe": uid})

    def running(self):
        return list(self._ask({"list": True}).get("shells", []))


class Local:
    """Shells as plain processes, for development and the tests. `argv`
    replaces bash."""

    def __init__(self, run, homes, argv=None):
        self.run = run
        self.homes = homes
        self.argv = argv or []
        self.procs = {}

    def start(self, uid):
        p = self.procs.get(uid)
        if p is not None and p.poll() is None:
            return
        shell = os.path.join(os.path.dirname(os.path.abspath(__file__)), "shell")
        home = os.path.join(self.homes, f"u{uid}")
        os.makedirs(home, exist_ok=True)
        os.makedirs(os.path.join(self.run, f"u{uid}"), exist_ok=True)
        env = {"HOME": home, "PATH": f"{shell}/bin:/usr/bin:/bin", "TERM": "xterm-256color",
               "LANG": "C.UTF-8", "FCSH_SOCKET": os.path.join(self.run, f"u{uid}", "hub.sock")}
        self.procs[uid] = subprocess.Popen([sys.executable, os.path.join(shell, "ptyhost.py"), *self.argv],
                                           env=env, cwd=home, start_new_session=True)

    def stop(self, uid):
        p = self.procs.pop(uid, None)
        if p is not None and p.poll() is None:
            p.terminate()
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                p.kill()

    def wipe(self, uid):
        self.stop(uid)
        shutil.rmtree(os.path.join(self.homes, f"u{uid}"), ignore_errors=True)

    def running(self):
        return [u for u, p in self.procs.items() if p.poll() is None]


class Shell:
    def __init__(self, uid):
        self.uid = uid
        self.state = STARTING
        self.why = ""
        self.server = None
        self.accepting = None
        self.term = None        # ptyhost's pty connection
        self.replay = bytearray()
        self.decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self.size = (80, 24)
        self.version = 0        # its ptyhost.py's VERSION (fc/shell/wire.py)
        self.mine = ""          # the person's commands since the model last read them
        self.runs = {}          # id -> {"out", "done", "show", "decoder"}: the lines running apart from the pty
        self.next_run = 1
        self.page = False
        self.since = time.monotonic()   # pageless since
        self.tasks = set()


async def recv_fds(sock, maxfds, timeout=10):
    """One message and up to `maxfds` descriptors from a non-blocking
    SOCK_SEQPACKET socket."""
    loop = asyncio.get_running_loop()
    got = loop.create_future()

    def ready():
        try:
            r = socket.recv_fds(sock, wire.MAX_MSG, maxfds)
        except BlockingIOError:
            return
        except OSError as e:
            r = e
        if not got.done():
            got.set_exception(r) if isinstance(r, Exception) else got.set_result(r)

    loop.add_reader(sock.fileno(), ready)
    try:
        msg, fds, _, _ = await asyncio.wait_for(got, timeout)
        return msg, fds
    finally:
        loop.remove_reader(sock.fileno())


class Shells:
    """The people's shells, with the hub's callbacks: `send(uid, msg)`,
    `printed(uid, text)`, `cesh(uid, args, fds)`, `desk(uid, op)` and
    `wake(uid, text)`."""

    def __init__(self, launcher, run, send, printed, cesh, desk, idle=IDLE, cap=MAX, wake=None):
        self.launcher = launcher
        self.rundir = run
        self.send = send
        self.printed = printed
        self.cesh = cesh
        self.desk = desk
        self.wake = wake
        self.idle = idle
        self.cap = cap
        self.shells = {}

    def alive(self, uid):
        sh = self.shells.get(uid)
        return sh is not None and sh.state != ENDED

    # --- The page ---------------------------------------------------------

    async def attach(self, uid, cols, rows):
        """The page of `uid` shows the shell, so start it if it isn't running."""
        sh = self.shells.get(uid)
        if sh is None or sh.state == ENDED:
            sh = await self.start(uid)
        sh.page = True
        await self.state(sh)
        if sh.replay:
            await self.send(uid, {"term": {"out": bytes(sh.replay).decode(errors="replace"), "replay": True}})
        self.resize(uid, cols, rows)

    def detach(self, uid):
        sh = self.shells.get(uid)
        if sh is not None:
            sh.page = False
            sh.since = time.monotonic()

    async def restart(self, uid, cols, rows):
        sh = self.shells.get(uid)
        if sh is not None and sh.state != ENDED:
            await self.stop(uid, "a new shell")
        await self.attach(uid, cols, rows)

    def keys(self, uid, data):
        """Keys for the shell of `uid`. False when no shell is live."""
        sh = self.shells.get(uid)
        if sh is None or sh.term is None:
            return False
        for c in wire.chunks(data):
            self._put(sh, wire.frame(wire.IN, c))
        return True

    def resize(self, uid, cols, rows):
        sh = self.shells.get(uid)
        if sh is None or not isinstance(cols, int) or not isinstance(rows, int):
            return
        sh.size = (max(1, min(cols, 500)), max(1, min(rows, 200)))
        if sh.term is not None:
            self._put(sh, wire.size(*sh.size))

    def _put(self, sh, frame):
        try:
            sh.term.send(frame)
        except OSError:
            pass

    async def state(self, sh):
        if sh.page:
            t = {"state": sh.state}
            if sh.why:
                t["why"] = sh.why
            await self.send(sh.uid, {"term": t})

    # --- The assistant's lines, apart from the pty ------------------------

    async def ready(self, uid):
        """The live shell of `uid`, started if none runs."""
        sh = self.shells.get(uid)
        if sh is None or sh.state == ENDED:
            sh = await self.start(uid)
        end = time.monotonic() + UP
        while sh.term is None:
            if sh.state == ENDED or time.monotonic() > end:
                raise ShellError(sh.why or "the shell did not start")
            await asyncio.sleep(0.05)
        return sh

    async def run(self, uid, line, timeout):
        """Type `line` at the prompt of `uid`'s shell, as the person would, and
        wait for it to end. Returns its output as plain text, and the result,
        "exit N", "timeout" or "error WHY"."""
        sh = await self.ready(uid)
        if sh.version < wire.RUN_MIN:
            raise ShellError("the shell is older than the site: exit it, and press Enter in its window "
                             "for a new one")
        rid = sh.next_run
        sh.next_run += 1
        done = asyncio.get_running_loop().create_future()
        sh.runs[rid] = {"out": bytearray(), "done": done}
        try:
            self._put(sh, wire.frame(wire.RUN, json.dumps({"id": rid, "line": line}).encode()))
            try:
                end = await asyncio.wait_for(asyncio.shield(done), timeout)
                timed_out = False
            except asyncio.TimeoutError:
                timed_out = True
                self._put(sh, wire.frame(wire.KILL, json.dumps({"id": rid}).encode()))
                try:
                    end = await asyncio.wait_for(done, 5)
                except asyncio.TimeoutError:
                    end = None
            if end is not None and end.get("busy"):
                result = f"error {BUSY}"
            elif timed_out:
                result = "timeout"
            elif end is None:
                result = "error the shell went away"
            else:
                result = f"exit {end.get('exit')}"
            text = bytes(sh.runs[rid]["out"]).decode(errors="replace")
            return ANSI.sub("", text).replace("\r\n", "\n").replace("\r", ""), result
        finally:
            sh.runs.pop(rid, None)

    def _run_out(self, sh, payload):
        rid, data = wire.unrunout(payload)
        run = sh.runs.get(rid)
        if run is not None and len(run["out"]) < RUN_KEEP:
            run["out"] += data[:RUN_KEEP - len(run["out"])]

    def _run_end(self, sh, payload):
        try:
            m = json.loads(payload)
            run = sh.runs.get(int(m["id"]))
        except (ValueError, KeyError, TypeError):
            return
        if run is not None and not run["done"].done():
            run["done"].set_result(m if isinstance(m, dict) else None)

    def _mine(self, sh, payload):
        try:
            m = json.loads(payload)
            entry = f"$ {m['line']}\n{m['out']}"
        except (ValueError, KeyError, TypeError):
            return
        if not entry.endswith("\n"):
            entry += "\n"
        sh.mine = (sh.mine + entry)[-MINE_KEEP:]

    def _wake(self, sh, payload):
        try:
            text = json.loads(payload)["text"]
        except (ValueError, KeyError, TypeError):
            return
        if isinstance(text, str) and text and self.wake is not None:
            self.wake(sh.uid, text)

    def take_mine(self, uid):
        """The commands that the person ran in their shell since the last
        call, with their output, for the model."""
        sh = self.shells.get(uid)
        if sh is None:
            return ""
        text, sh.mine = sh.mine, ""
        return text

    def _runs_gone(self, sh):
        for run in sh.runs.values():
            if not run["done"].done():
                run["done"].set_result(None)

    # --- Start, stop, recover ---------------------------------------------

    async def start(self, uid):
        live = [s for s in self.shells.values() if s.state != ENDED]
        if len(live) >= self.cap:
            idle = sorted((s for s in live if not s.page), key=lambda s: s.since)
            if not idle:
                raise ShellError("the site runs too many shells now")
            await self.stop(idle[0].uid, "the site needed the room")
        old = self.shells.get(uid)
        sh = Shell(uid)
        if old is not None:
            sh.page = old.page
        self.shells[uid] = sh
        try:
            await asyncio.to_thread(self.launcher.start, uid)
            self.listen(sh)
        except (ShellError, OSError) as e:
            await self.ended(sh, str(e))
            return sh
        self.watch(sh, UP, "the shell did not start")
        return sh

    async def stop(self, uid, why):
        sh = self.shells.get(uid)
        if sh is None:
            return
        try:
            await asyncio.to_thread(self.launcher.stop, uid)
        except ShellError as e:
            log.warning("stop the shell of %s: %s", uid, e)
        await self.ended(sh, why)

    async def wipe(self, uid):
        """The person logged out: end their shell, and delete their home."""
        await self.stop(uid, "you logged out")
        await asyncio.to_thread(self.launcher.wipe, uid)

    async def recover(self):
        """When the hub starts, take in the shells that already run, which
        reconnect."""
        try:
            uids = await asyncio.to_thread(self.launcher.running)
        except ShellError as e:
            log.warning("the shells that run: %s", e)
            return
        for uid in uids:
            if uid in self.shells:
                continue
            sh = Shell(uid)
            self.shells[uid] = sh
            try:
                self.listen(sh)
            except OSError as e:
                log.warning("listen for the shell of %s: %s", uid, e)
                continue
            self.watch(sh, UP, "the shell did not come back")

    def close(self):
        """The hub stops: drop its connections. The shells keep running, and
        reconnect to the next hub."""
        for sh in self.shells.values():
            for t in list(sh.tasks):
                t.cancel()
            if sh.accepting is not None:
                sh.accepting.cancel()
            for s in (sh.term, sh.server):
                if s is not None:
                    s.close()
            sh.term = sh.server = None

    async def ended(self, sh, why):
        if sh.state == ENDED:
            return
        sh.state, sh.why = ENDED, why
        for t in list(sh.tasks):
            if t is not asyncio.current_task():
                t.cancel()
        if sh.accepting is not None and sh.accepting is not asyncio.current_task():
            sh.accepting.cancel()
        for s in (sh.term, sh.server):
            if s is not None:
                s.close()
        sh.term = sh.server = None
        await self.state(sh)

    def watch(self, sh, secs, why):
        """End the shell when it still has no pty connection after `secs` and
        its unit has stopped."""
        async def check():
            await asyncio.sleep(secs)
            if sh.term is not None or sh.state == ENDED or self.shells.get(sh.uid) is not sh:
                return
            try:
                running = sh.uid in await asyncio.to_thread(self.launcher.running)
            except ShellError:
                running = True
            if not running:
                await self.ended(sh, why)
            else:
                self.watch(sh, secs, why)
        self.task(sh, check())

    async def gc(self):
        """Stop the shells that have had no page for `idle` seconds."""
        while True:
            await asyncio.sleep(GC_EVERY)
            now = time.monotonic()
            for sh in list(self.shells.values()):
                if sh.state != ENDED and not sh.page and now - sh.since > self.idle:
                    await self.stop(sh.uid, "it had no page for a long time")
                elif sh.state == ENDED and not sh.page:
                    self.shells.pop(sh.uid, None)

    def task(self, sh, coro):
        t = asyncio.create_task(coro)
        sh.tasks.add(t)
        t.add_done_callback(sh.tasks.discard)
        return t

    # --- A shell's socket --------------------------------------------

    def listen(self, sh):
        d = os.path.join(self.rundir, f"u{sh.uid}")
        path = os.path.join(d, "hub.sock")
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        s = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        s.setblocking(False)
        s.bind(path)
        os.chmod(path, 0o666)       # the unit has its own uid, and only it sees the directory
        s.listen(16)
        sh.server = s
        sh.accepting = asyncio.create_task(self.accept(sh))

    async def accept(self, sh):
        loop = asyncio.get_running_loop()
        while sh.server is not None:
            try:
                conn, _ = await loop.sock_accept(sh.server)
            except OSError:
                return
            conn.setblocking(False)
            self.task(sh, self.serve(sh, conn))

    async def serve(self, sh, conn):
        fds = []
        try:
            msg, fds = await recv_fds(conn, 3)
            hello = json.loads(msg) if msg else {}
            kind = hello.get("kind") if isinstance(hello, dict) else None
            if kind == "term" and not fds:
                v = hello.get("v")
                sh.version = v if isinstance(v, int) else 1
                await self.pty(sh, conn)
                return
            if kind == "cesh" and len(fds) == 3 and isinstance(hello.get("args"), list) \
                    and all(isinstance(a, str) for a in hello["args"]):
                given, fds = fds, []
                await self.run_cesh(sh, conn, hello["args"], given)
            elif kind == "desk" and isinstance(hello.get("op"), str) and not fds:
                r = await self.desk(sh.uid, hello["op"])
                await asyncio.get_running_loop().sock_sendall(conn, json.dumps(r).encode())
        except (OSError, ValueError, asyncio.TimeoutError):
            pass
        finally:
            for fd in fds:
                os.close(fd)
            if conn is not sh.term:
                conn.close()

    async def pty(self, sh, conn):
        loop = asyncio.get_running_loop()
        if sh.term is not None:
            sh.term.close()
        sh.term = conn
        if sh.state != LIVE:
            sh.state, sh.why = LIVE, ""
            await self.state(sh)
        self._put(sh, wire.size(*sh.size))
        end = None
        try:
            while True:
                msg = await loop.sock_recv(conn, wire.MAX_MSG)
                if not msg:
                    break
                kind, payload = msg[0], msg[1:]
                if kind == wire.OUT:
                    await self.out(sh, payload)
                elif kind == wire.RUNOUT:
                    self._run_out(sh, payload)
                elif kind == wire.RUNEND:
                    self._run_end(sh, payload)
                elif kind == wire.MINE:
                    self._mine(sh, payload)
                elif kind == wire.WAKE:
                    self._wake(sh, payload)
                elif kind == wire.END:
                    try:
                        end = json.loads(payload).get("exit")
                    except (ValueError, AttributeError):
                        end = None
                    break
        except OSError:
            pass
        if sh.term is not conn:         # a newer connection took its place
            return
        sh.term = None
        conn.close()
        self._runs_gone(sh)
        if end is not None:
            await self.ended(sh, f"exit {end}")
        else:
            self.watch(sh, LOST, "the shell stopped")

    async def out(self, sh, data):
        sh.replay += data
        del sh.replay[:-REPLAY]
        text = sh.decoder.decode(data)
        if text:
            self.printed(sh.uid, text)
            if sh.page:
                await self.send(sh.uid, {"term": {"out": text}})

    async def run_cesh(self, sh, conn, args, fds):
        """A cesh command of the shell. Closing the connection stops it."""
        loop = asyncio.get_running_loop()
        work = asyncio.create_task(self.cesh(sh.uid, args, fds))
        gone = asyncio.create_task(loop.sock_recv(conn, 1))
        try:
            done, _ = await asyncio.wait({work, gone}, return_when=asyncio.FIRST_COMPLETED)
            if work in done:
                result = work.result()
                await loop.sock_sendall(conn, json.dumps({"result": result}).encode())
        finally:
            gone.cancel()
            if not work.done():
                work.cancel()

"""The hub pushes changes to the open pages over WebSocket.

The site sends one JSON line per change on a Unix socket, {"to": <user id> or
"all", "msg": {...}}. A person has one open page, and a new one closes the old
one (ELSEWHERE). A page connects with a signed ticket from GET /api/hub.

The hub also holds the shells (fc/shellhub.py) and the dials of the program
windows. A page sends {"term": ...} for its shell and windows, and a window's
bytes go both ways as binary messages, [u8 n][window name, n bytes][bytes].
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlparse

from . import ceshargs, launch, shellhub, vs1screen

TICKET_SECS = 60
# Limits.
MAX_ALL = 4000          # open pages in all
MAX_TERMS = 24          # terminals of one page: its shell and its program windows
MAX_WINDOW = 40         # the characters of the name of a window
VISITOR = 0             # a visitor's user id
# A shell job wakes the assistant (fc/shell/jobs.py). The hub waits while the
# site runs the turn, and retries while another turn runs.
WAKE_TIMEOUT = 1800     # the longest turn, MAX_CALLS model calls
WAKE_AGAIN = 5          # seconds between retries
WAKE_TRIES = 120        # 10 minutes of retries
# The close codes for a page.
BAD_TICKET = 4001
FULL = 4002
ELSEWHERE = 4003        # the person opened another page
MAX_LINE = 1 << 20      # the largest message from the site
# The largest message from a page: a line, keys, or a command with its input,
# which JSON can make 6 times longer.
MAX_IN = 8 * launch.MAX_STDIN
LOG_MAX = 64 * 1024     # the shell output kept for `tail shell`, in characters
MAX_HOLD_MS = 10000     # the total hold time of one press
SETTLE = 0.1            # the wait for the program's answer to a press or a type
WINDOW_WAIT = 3.0       # the wait for a new window's dial and first frame
MAX_RUN_SECS = 600      # the longest model command in the shell
# Terminal escape codes, which the shell log strips.
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")

log = logging.getLogger("fc.hub")


def key(secret):
    """The tickets' key, from the site's secret."""
    return hashlib.sha256(("fc-hub:" + secret).encode()).digest()


def sign(k, body):
    return hmac.new(k, body.encode(), hashlib.sha256).hexdigest()


def ticket(k, uid, now=None, shell=False):
    """A ticket for a page of `uid`. With `shell`, the page may run cesh in its
    shell, which the site allows on a person's page only."""
    exp = int((now if now is not None else time.time()) + TICKET_SECS)
    body = f"{uid}.{exp}.{1 if shell else 0}"
    return f"{body}.{sign(k, body)}"


def parse(k, t, now=None):
    """(user id, shell) of a good ticket, else None."""
    try:
        uid, exp, shell, sig = t.split(".")
        good = sign(k, f"{uid}.{exp}.{shell}")
        if hmac.compare_digest(sig, good) and int(exp) >= (now if now is not None else time.time()):
            return int(uid), shell == "1"
    except (ValueError, AttributeError):
        pass
    return None


def check(k, t, now=None):
    """The user id of a good ticket, else None."""
    r = parse(k, t, now)
    return r[0] if r else None


SHELL = "shell"          # the shell window, whose messages have no "w"


class Term:
    """A program window's stream to its program (`sock`, with its descriptor
    `fd`). Closing it ends the dial."""

    def __init__(self, fd, sock, uid=None):
        self.fd = fd
        self.sock = sock
        self.uid = uid
        self.screen = vs1screen.Screen()


class ShellLog:
    """The last LOG_MAX characters that a person's shell printed, as plain
    text."""

    def __init__(self):
        self.text = ""

    def add(self, s):
        s = ANSI.sub("", s).replace("\r\n", "\n").replace("\r", "")
        self.text = (self.text + s)[-LOG_MAX:]

    def tail(self, n):
        return self.text[-n:] if n > 0 else ""


class Hub:
    def __init__(self, k, cesh=None, launcher=None, run=None, app="http://127.0.0.1:8000", host=None):
        self.k = k
        self.cesh = cesh    # the bridge's cesh socket
        self.app = app      # the site, for the shells' desk ops
        self.host = host    # the site's host name, checked on every request
        self.pages = {}     # user id -> that person's open pages
        self.count = 0
        self.people = {}    # user id -> {"sealed", "server"}, for their shell
        self.terms = {}     # (page, window) -> its Term
        self.logs = {}      # user id -> ShellLog
        self.shells = None
        if launcher is not None:
            self.shells = shellhub.Shells(launcher, run, self.to_page, self.log,
                                          self.shell_cesh, self.shell_desk, wake=self.shell_wake)
        self.waking = set()   # the wakes on their way to the site

    async def page(self, ws):
        """Serve one page until it goes."""
        q = parse_qs(urlparse(ws.request.path).query)
        t = parse(self.k, (q.get("t") or [""])[0])
        if t is None:
            await ws.close(BAD_TICKET, "bad ticket")
            return
        uid, shell = t
        if self.count >= MAX_ALL:
            await ws.close(FULL, "too many pages")
            return
        mine = self.pages.setdefault(uid, set())
        old = [] if uid == VISITOR else list(mine)
        # Add the new page before closing the old ones, so the set stays
        # when they leave.
        mine.add(ws)
        self.count += 1
        for o in old:
            mine.discard(o)
            self.count -= 1
            await o.close(ELSEWHERE, "open in another place")
        from websockets.exceptions import ConnectionClosed
        try:
            async for raw in ws:
                if shell and uid != VISITOR:
                    await self.said(ws, uid, raw)
        except ConnectionClosed:    # the page left, or a newer page closed it
            pass
        finally:
            self.stop(ws)
            if ws in mine:      # still the person's page
                mine.discard(ws)
                self.count -= 1
            if not mine and self.pages.get(uid) is mine:
                self.pages.pop(uid, None)
                if self.shells is not None:
                    self.shells.detach(uid)
                if self.shells is None or not self.shells.alive(uid):
                    self.people.pop(uid, None)
                    self.logs.pop(uid, None)

    # --- A page's shell and windows ---------------------------------------------

    async def said(self, ws, uid, raw):
        """A page's message: {"term": ...}, or bytes for a program window."""
        if isinstance(raw, (bytes, bytearray)):
            # [u8 n][window name, n bytes][bytes for its program]
            if not raw or len(raw) < 1 + raw[0]:
                return
            term = self.terms.get((ws, raw[1:1 + raw[0]].decode(errors="replace")))
            if term is not None and term.sock is not None:
                self.put(term, bytes(raw[1 + raw[0]:]))
            return
        try:
            m = json.loads(raw)
        except ValueError:
            return
        t = m.get("term") if isinstance(m, dict) else None
        if not isinstance(t, dict):
            return
        w = t.get("w", SHELL)
        if not isinstance(w, str) or not 0 < len(w) <= MAX_WINDOW:
            return
        if w == SHELL:
            await self.shell_said(uid, t)
            return
        term = self.terms.get((ws, w))
        if "dial" in t:
            await self.dial(ws, uid, w, t["dial"])
        elif term is None:
            return
        elif isinstance(t.get("in"), str):
            self.type(term, t["in"])
        elif t.get("stop"):
            self.halt(term)

    async def shell_said(self, uid, t):
        if self.shells is None:
            if t.get("open") or t.get("restart"):
                await self.to_page(uid, {"term": {"state": shellhub.ENDED,
                                                  "why": "this site runs no shells"}})
            return
        cols, rows = t.get("cols"), t.get("rows")
        try:
            if t.get("open"):
                await self.shells.attach(uid, cols, rows)
            elif t.get("restart"):
                await self.shells.restart(uid, cols, rows)
            elif isinstance(t.get("in"), str):
                self.shells.keys(uid, t["in"].encode())
            elif isinstance(t.get("size"), list) and len(t["size"]) == 2:
                self.shells.resize(uid, *t["size"])
        except shellhub.ShellError as e:
            await self.to_page(uid, {"term": {"state": shellhub.ENDED, "why": str(e)}})

    async def to_page(self, uid, msg):
        """Send a message to the open page of `uid`. A slow page makes this
        wait, and so a shell that prints a lot waits too."""
        data = json.dumps(msg)
        for ws in list(self.pages.get(uid, ())):
            try:
                await ws.send(data)
            except Exception:   # the page went
                pass

    async def shell_cesh(self, uid, args, fds):
        """Run a cesh command of `uid`'s shell on `fds`, and return ceshd's
        result. A cancel stops the command."""
        try:
            person = self.people.get(uid)
            if person is None or not self.cesh:
                return "error cesh runs while you have the site open"
            why = ceshargs.disabled(args)
            if why:
                return f"error {why}"
            try:
                conn = await asyncio.to_thread(launch.launch, self.cesh, person["sealed"],
                                               person["server"], args, fds)
            except launch.LaunchError as e:
                return f"error {e}"
        finally:
            for fd in fds:
                os.close(fd)        # the command has them now
        conn.setblocking(False)
        try:
            reply = await asyncio.get_running_loop().sock_recv(conn, launch.REPLY_MAX)
            return reply.decode(errors="replace") or "error no reply"
        finally:
            conn.close()            # on a cancel too, which stops the command

    async def to_site(self, path, msg, timeout):
        """A signed POST to the site (hub_desk and hub_wake in fc/api.py).
        Returns its JSON reply, or {"error": why}."""
        body = json.dumps(dict(msg, exp=int(time.time()) + 60))

        def call():
            headers = {"Content-Type": "application/json", "X-Fc-Hub": sign(self.k, body)}
            if self.host:
                headers["Host"] = self.host
            req = urllib.request.Request(self.app + path, data=body.encode(), headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    return json.loads(r.read())
            except urllib.error.HTTPError as e:
                return json.loads(e.read())
        try:
            r = await asyncio.to_thread(call)
        except (OSError, ValueError) as e:
            return {"error": f"the site does not answer ({e})"}
        return r if isinstance(r, dict) else {"error": "the site gave no answer"}

    async def shell_desk(self, uid, op):
        return await self.to_site("/api/hub/desk", {"uid": uid, "op": op}, 60)

    def shell_wake(self, uid, text):
        """A job in `uid`'s shell wakes the assistant, so the site runs a turn.
        While another turn runs, the wake retries."""
        async def go():
            for _ in range(WAKE_TRIES):
                r = await self.to_site("/api/hub/wake", {"uid": uid, "text": text}, WAKE_TIMEOUT)
                if r.get("error") != "BUSY":
                    if "error" in r:
                        log.warning("a wake of user %s: %s", uid, r["error"])
                    return
                await asyncio.sleep(WAKE_AGAIN)
            log.warning("a wake of user %s: the turn stayed busy", uid)
        task = asyncio.get_running_loop().create_task(go())
        self.waking.add(task)
        task.add_done_callback(self.waking.discard)

    async def dial(self, ws, uid, w, d):
        """Dial the program of window `w` as the person: {"pid", "server"}."""
        async def end(result):
            await ws.send(json.dumps({"term": {"end": result, "w": w}}))
        if (ws, w) in self.terms:
            return await end("error the window is on its program already")
        person = self.people.get(uid)
        if person is None or not self.cesh:
            return await end("error the window cannot dial now")
        pid = d.get("pid") if isinstance(d, dict) else None
        server = d.get("server") if isinstance(d, dict) else None
        if (not isinstance(pid, int) or isinstance(pid, bool) or pid < 0
                or not isinstance(server, str) or not 0 < len(server) <= 260):
            return await end("error give the pid of a program and its server")
        if sum(1 for (page, _) in self.terms if page is ws) >= MAX_TERMS:
            return await end("error a page has too many windows on programs")
        try:
            sock = await asyncio.to_thread(launch.dial, self.cesh, person["sealed"], server, pid)
        except launch.LaunchError as e:
            return await end(f"error {e}")
        if (ws, w) in self.terms or ws.state.name != "OPEN":
            sock.close()        # the page left, or dialed again, meanwhile
            return
        sock.setblocking(False)
        term = Term(sock.fileno(), sock, uid=uid)
        self.terms[(ws, w)] = term
        try:
            await ws.send(json.dumps({"term": {"live": True, "w": w}}))
        except Exception:   # the page left, so the pump ends the dial
            pass
        asyncio.create_task(self.pump(ws, w, term))

    async def pump(self, ws, w, term):
        """Relay what the program of window `w` sends to the page, until the
        stream ends. A slow page makes the program wait, so nothing piles up
        here."""
        from websockets.exceptions import ConnectionClosed
        loop = asyncio.get_running_loop()
        ready = asyncio.Event()
        loop.add_reader(term.fd, ready.set)
        head = bytes([len(w.encode())]) + w.encode()
        try:
            while True:
                await ready.wait()
                ready.clear()
                try:
                    data = os.read(term.fd, 65536)
                except BlockingIOError:
                    continue
                except OSError:
                    data = b""
                if not data:
                    break
                term.screen.feed(data)
                await ws.send(head + data)
            await ws.send(json.dumps({"term": {"end": "closed", "w": w}}))
        except ConnectionClosed:
            pass
        finally:
            loop.remove_reader(term.fd)
            self.end(ws, w, term)

    def type(self, term, text):
        self.put(term, text.encode())

    def put(self, term, data):
        try:
            while data:
                n = os.write(term.fd, data)
                data = data[n:]
        except OSError:     # its input is full, or it ended, so the rest is dropped
            pass

    def halt(self, term):
        try:
            term.sock.shutdown(2)   # the pump sees the end
        except OSError:
            pass

    def end(self, ws, w, term):
        if self.terms.get((ws, w)) is term:
            del self.terms[(ws, w)]
        term.sock.close()           # the bridge ends the dial

    def stop(self, ws):
        """The page left: end its dials, and keep the shell."""
        for (page, _), term in list(self.terms.items()):
            if page is ws:
                self.halt(term)

    def person(self, p):
        """What a person's shell needs, from the site. It stays while the
        person has a page open, or is about to (the site sends it with the
        ticket). Past MAX_ALL people, the ones with no page go."""
        uid = int(p["uid"])
        self.people[uid] = {"sealed": str(p["sealed"]), "server": str(p.get("server") or "")}
        if len(self.people) > MAX_ALL:
            for u in [u for u in self.people if u not in self.pages and u != uid]:
                del self.people[u]

    def give(self, to, data):
        from websockets.asyncio.server import broadcast
        if to == "all":
            broadcast([ws for s in self.pages.values() for ws in s], data)
        else:
            broadcast(list(self.pages.get(to, ())), data)

    # --- Desk ops that read and type on a page (fc/desk.py) -----------

    def log(self, uid, text):
        if uid is not None and uid != VISITOR and text:
            self.logs.setdefault(uid, ShellLog()).add(text)

    def stats(self):
        """The counts for the site's stats (fc/stats.py): people with an open
        page, visitors' pages, all pages, and running shells."""
        live = 0 if self.shells is None else sum(1 for s in self.shells.shells.values() if s.state != shellhub.ENDED)
        return {"people": sum(1 for u in self.pages if u != VISITOR), "visitors": len(self.pages.get(VISITOR, ())),
                "pages": sum(len(s) for s in self.pages.values()), "shells": live}

    async def ask(self, req):
        """A request on the person's page or shell: {"look"}, {"tail"},
        {"press"}, {"type"}, {"start"} or {"wipe"}, each with the "uid".
        Returns {"ok": ...} or {"error": why}."""
        if not isinstance(req, dict) or len(req) != 1:
            return {"error": "a bad request"}
        (what, a), = req.items()
        if what == "stats":
            return {"ok": self.stats()}
        if not isinstance(a, dict) or not isinstance(a.get("uid"), int):
            return {"error": "a bad request"}
        uid = a["uid"]
        if what == "tail":
            n = a.get("n")
            log = self.logs.get(uid)
            return {"ok": {"text": log.tail(n) if log and isinstance(n, int) else ""}}
        if what == "run":
            return await self._shell_run(uid, a)
        if what == "mine":
            return {"ok": {"text": self.shells.take_mine(uid) if self.shells is not None else ""}}
        if what == "wipe":
            if self.shells is not None:
                await self.shells.wipe(uid)
            self.people.pop(uid, None)
            self.logs.pop(uid, None)
            return {"ok": True}
        w = a.get("w")
        if what == "start" or w == SHELL:
            return await self._shell_ask(uid, what, a)
        ws = next(iter(self.pages.get(uid, ())), None)
        if ws is None:
            return {"error": "the person has no page open"}
        if not isinstance(w, str):
            return {"error": "a bad request"}
        term = await self._window(ws, w)
        if term is None:
            return {"error": f"the window {w} is not on a program now"}
        if what == "look":
            return self._look(term, w, a.get("rect"))
        if what == "press":
            return await self._press(term, a.get("keys"))
        if what == "click":
            return await self._click(term, a)
        if what == "type":
            if not isinstance(a.get("text"), str):
                return {"error": "a bad request"}
            self.put(term, vs1screen.text(a["text"]))
            await asyncio.sleep(SETTLE)
            return {"ok": {}}
        return {"error": f"no request {what}"}

    async def _shell_run(self, uid, a):
        """Type a model command at the person's shell prompt, as the person
        would (fc/shell/ptyhost.py), so the page shows it as the shell prints
        it."""
        line, timeout = a.get("line"), a.get("timeout", 60)
        if not isinstance(line, str) or not line.strip() or not isinstance(timeout, int) \
                or not 0 < timeout <= MAX_RUN_SECS:
            return {"error": "a bad request"}
        if self.shells is None:
            return {"error": "this site runs no shells"}
        try:
            out, result = await self.shells.run(uid, line, timeout)
        except shellhub.ShellError as e:
            result, out = f"error {e}", ""
        return {"ok": {"out": out, "result": result}}

    async def _shell_ask(self, uid, what, a):
        """The model types in the person's shell: a line ("start"), text, or
        keys."""
        if self.shells is None or not self.shells.alive(uid):
            return {"error": "the person's shell does not run now"}
        if what == "start":
            if not isinstance(a.get("line"), str):
                return {"error": "a bad request"}
            data = (a["line"] + "\r").encode()
        elif what == "type":
            if not isinstance(a.get("text"), str):
                return {"error": "a bad request"}
            data = a["text"].replace("\n", "\r").encode()
        elif what == "press":
            keys = a.get("keys")
            if (not isinstance(keys, list) or not keys
                    or not all(isinstance(k, list) and len(k) == 2 and isinstance(k[0], str) for k in keys)):
                return {"error": "a bad request"}
            parts = [vs1screen.terminal_key(name) for name, _ in keys]
            if None in parts:
                return {"error": f"no key {keys[parts.index(None)][0]} in a terminal"}
            data = b"".join(parts)
        elif what == "look":
            return {"error": "the shell has no screen, and tail reads it"}
        elif what == "click":
            return {"error": "the shell takes keys, not clicks"}
        else:
            return {"error": f"no request {what}"}
        if not self.shells.keys(uid, data):
            return {"error": "the person's shell does not run now"}
        await asyncio.sleep(SETTLE)
        return {"ok": {}}

    async def _window(self, ws, w):
        """The Term of program window `w`. A window that the page just opened
        dials first, so wait a moment for the dial and the first frame."""
        end = time.monotonic() + WINDOW_WAIT
        while True:
            term = self.terms.get((ws, w))
            if (term is not None and term.screen.cols) or time.monotonic() >= end:
                return term
            await asyncio.sleep(0.05)

    def _look(self, term, w, rect):
        sc = term.screen
        if sc.broken:
            return {"error": f"the window {w} broke: {sc.broken}"}
        if not sc.cols:
            return {"error": f"the program of {w} drew nothing yet"}
        if rect is not None and (not isinstance(rect, list) or len(rect) != 4
                                 or not all(isinstance(v, int) and v >= 1 for v in rect)):
            return {"error": "a bad rectangle"}
        x1, y1, x2, y2 = rect or (1, 1, sc.cols, sc.rows)
        return {"ok": {"title": sc.title, "cols": sc.cols, "rows": sc.rows,
                       "text": sc.text(x1, y1, x2, y2)}}

    async def _click(self, term, a):
        x, y, button = a.get("x"), a.get("y"), a.get("button", 0)
        if not all(isinstance(v, int) and not isinstance(v, bool) for v in (x, y, button)) \
                or not (1 <= x <= 65536 and 1 <= y <= 65536 and 0 <= button <= 4):
            return {"error": "a bad request"}
        t0 = vs1screen.now_ms()
        self.put(term, vs1screen.pointer(vs1screen.DOWN, button, x, y, t0))
        await asyncio.sleep(0.03)
        self.put(term, vs1screen.pointer(vs1screen.UP, button, x, y, t0 + 30))
        await asyncio.sleep(SETTLE)
        return {"ok": {}}

    async def _press(self, term, keys):
        if (not isinstance(keys, list) or not keys
                or not all(isinstance(k, list) and len(k) == 2 and isinstance(k[0], str)
                           and isinstance(k[1], int) and k[1] >= 0 for k in keys)):
            return {"error": "a bad request"}
        if sum(ms for _, ms in keys) > MAX_HOLD_MS:
            return {"error": f"the keys of one press hold {MAX_HOLD_MS} ms at most"}
        for name, ms in keys:
            name, t0 = vs1screen.vs1_key(name), vs1screen.now_ms()
            self.put(term, vs1screen.key(vs1screen.DOWN, name, t0))
            await asyncio.sleep(ms / 1000)
            self.put(term, vs1screen.key(vs1screen.UP, name, t0 + ms))
        await asyncio.sleep(SETTLE)
        return {"ok": {}}

    async def site(self, reader, writer):
        """One connection from the site, with one JSON message per line."""
        try:
            while True:
                try:
                    line = await reader.readline()
                except ValueError:      # a line past MAX_LINE
                    log.warning("a message from the site is too long")
                    break
                if not line:
                    break
                try:
                    m = json.loads(line)
                    if "person" in m:
                        self.person(m["person"])
                        continue
                    if "ask" in m:
                        reply = await self.ask(m["ask"])
                        writer.write((json.dumps(reply) + "\n").encode())
                        await writer.drain()
                        continue
                    to = m["to"] if m["to"] == "all" else int(m["to"])
                    data = json.dumps(m["msg"])
                except (ValueError, KeyError, TypeError):
                    log.warning("a bad message from the site")
                    continue
                self.give(to, data)
        finally:
            writer.close()


async def serve(k, sock, host, port, origins=None, ready=None, cesh=None, launcher=None, run=None,
                app="http://127.0.0.1:8000", hub_ready=None, app_host=None):
    """Run a hub until cancelled. `ready` gets the WebSocket port (for port
    0), `cesh` is the bridge's cesh socket, `launcher` starts the shells
    (fc/shellhub.py) with their sockets in `run`, and `hub_ready` gets the
    Hub."""
    from websockets.asyncio.server import serve as ws_serve
    hub = Hub(k, cesh, launcher, run, app, app_host)
    if hub_ready is not None:
        hub_ready.set_result(hub)
    gc = None
    if hub.shells is not None:
        await hub.shells.recover()
        gc = asyncio.create_task(hub.shells.gc())
    if os.path.exists(sock):
        os.unlink(sock)
    unix = await asyncio.start_unix_server(hub.site, sock, limit=MAX_LINE)
    os.chmod(sock, 0o600)
    try:
        async with unix, ws_serve(hub.page, host, port, max_size=MAX_IN, origins=origins) as server:
            if ready is not None:
                ready.set_result(server.sockets[0].getsockname()[1])
            await asyncio.Future()
    finally:
        if gc is not None:
            gc.cancel()
            hub.shells.close()


def main():
    logging.basicConfig(level=logging.INFO)
    k = key(os.environ["FC_SECRET_KEY"])
    sock = os.environ.get("FC_HUB_SOCKET", "/run/fc-hub/hub.sock")
    host, port = os.environ.get("FC_HUB_BIND", "127.0.0.1:8001").rsplit(":", 1)
    # Only this site's pages connect: the Origin must match.
    origins = None
    if os.environ.get("FC_DEBUG") != "1":
        origins = [f"https://{h}" for h in os.environ.get("FC_HOSTS", "").split(",") if h]
    cesh = os.environ.get("FC_CESH_SOCKET", "/run/fc/cesh.sock")
    launcher = run = None
    if os.environ.get("FC_SHELLD"):
        launcher, run = shellhub.Systemd(os.environ["FC_SHELLD"]), "/run/fc-shells"
    elif os.environ.get("FC_SHELL_LOCAL"):
        base = os.path.abspath(os.environ["FC_SHELL_LOCAL"])
        run = os.path.join(base, "run")
        launcher = shellhub.Local(run, os.path.join(base, "homes"))
    app = os.environ.get("FC_APP_URL", "http://127.0.0.1:8000")
    app_host = (os.environ.get("FC_HOSTS", "").split(",") or [""])[0] or None
    log.info("hub on %s:%s, site socket %s, cesh %s, shells in %s", host, port, sock, cesh, run)
    asyncio.run(serve(k, sock, host, int(port), origins, cesh=cesh, launcher=launcher, run=run, app=app,
                      app_host=app_host))


if __name__ == "__main__":
    main()

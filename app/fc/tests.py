import hashlib
import json
import os
import re
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core import mail
from django.core.exceptions import ValidationError
from django.test import Client, TestCase, override_settings

from . import account, aishell, bridge, ceshargs, hub, kernel, launch, push, shellhub, vs1screen
from . import desk
from .models import ChatLine, Profile, Widget, validate_handle

LEDGER = "127.0.0.1:55100"
NET = ["127.0.0.1:55110", "127.0.0.1:55120"]


def sh(*commands, text=""):
    """A model answer that runs `commands` in the shell, after `text`."""
    return text, list(commands)


def shown(req):
    """A chat request as text, for the tests to read."""
    out = []
    for m in req["messages"]:
        t = f"{m['role'].upper()}: {m.get('content') or ''}"
        for c in m.get("tool_calls") or []:
            t += "\nCALL " + c["function"]["arguments"]
        out.append(t)
    return "\n\n".join(out)


class FakeBridge:
    """Answers bridge calls like fc-bridge does, and records them."""

    def __init__(self):
        self.calls = []
        self.balances = {}            # (server, pubkey) -> raw
        self.requests = []            # the think chat requests, in order
        self.prompts = []             # the same, as text (shown)
        self.answer = "Hello."
        # The answers of the next think calls, in order. Each is a text, or (text, commands) from sh.
        self.answers = []
        self.n_calls = 0              # tool calls made
        self.window = 131072          # think's context window
        self.fail = {}                # op -> error code
        self.down = set()             # servers that don't answer
        self.n = 0

    def __call__(self, op, **args):
        self.calls.append((op, args))
        if op in self.fail:
            raise bridge.BridgeError(self.fail[op])
        if args.get("server") in self.down:
            raise bridge.BridgeError("CES_ERROR_TIMEOUT")
        if op == "ping":
            return {"ok": True, "ledger": LEDGER}
        if op == "info":
            # The ledger has rpc_port 0, and the network servers have compute.
            return {"ok": True, "pubkey": "ab" * 32, "rpc_port": 0 if args["server"] == LEDGER else 1,
                    "tps": 1, "min_difficulty": 1, "pow_engine": False}
        if op == "peers":
            p = NET if args["server"] == LEDGER else [LEDGER]
            return {"ok": True, "count": len(p), "peers": [{"pubkey": "cd" * 32, "address": a} for a in p]}
        if op == "newkey":
            self.n += 1
            return {"ok": True, "pubkey": f"{self.n:064x}", "sealed": f"sealed{self.n}"}
        if op == "grant":
            self.balances[(LEDGER, args["pubkey"])] = 100_000_000
            return {"ok": True, "granted": 100_000_000}
        if op == "balance":
            b = self.balances.get((args["server"], args["pubkey"]))
            return {"ok": True, "exists": b is not None, "balance": b or 0}
        if op == "transfer":
            src = (args["server"], self.key_of(args["sealed"]))
            if self.balances.get(src, 0) < args["amount"]:
                raise bridge.BridgeError("CES_ERROR_INSUFFICIENT_BALANCE")
            self.balances[src] -= args["amount"]
            dst = (args["server"], args["dest"])
            self.balances[dst] = self.balances.get(dst, 0) + args["amount"]
            return {"ok": True, "balance": self.balances[src]}
        if op == "cross":
            pk = args["dest"]
            self.balances[(LEDGER, pk)] -= args["amount"]
            self.balances[(args["dest_server"], pk)] = (
                self.balances.get((args["dest_server"], pk), 0) + args["amount"])
            return {"ok": True, "balance": self.balances[(LEDGER, pk)]}
        if op == "instances":
            return {"ok": True, "instances": []}
        if op == "think":
            req = args["chat"]
            p = shown(req)
            self.requests.append(req)
            self.prompts.append(p)
            payer = (args["server"], self.key_of(args["sealed"]))
            self.balances[payer] = self.balances.get(payer, 0) - 1000
            fold = req["messages"][0]["content"].startswith("You fold the older part")
            answer = "SUMMARY" if fold else (self.answers.pop(0) if self.answers else self.answer)
            text, commands = (answer, []) if isinstance(answer, str) else answer
            calls = []
            for c in commands:   # a command, or a whole tool call
                self.n_calls += 1
                calls.append(c if isinstance(c, dict) else {
                    "id": f"call_{self.n_calls}", "type": "function",
                    "function": {"name": "shell", "arguments": json.dumps({"command": c})}})
            return {"ok": True, "reply": text, "tool_calls": calls,
                    "finish_reason": "tool_calls" if calls else "stop", "charged": 1000,
                    "prompt_tokens": len(p) // 4, "cached_tokens": len(p) // 8, "output_tokens": 5,
                    "context_tokens": self.window, "max_input_bytes": 1048576, "max_out_tokens": 4096}
        if op == "export":
            return {"ok": True, "private": "ab" * 32}
        raise AssertionError(f"unexpected op {op}")

    def stream(self, op, timeout=20, **args):
        """As bridge.stream: the answer in three pieces, then the reply."""
        r = self(op, **args)
        text = r.get("reply", "")
        third = max(1, len(text) // 3)
        for i in range(0, len(text), third):
            yield {"piece": text[i:i + third]}
        return r

    def key_of(self, sealed):
        return f"{int(sealed[len('sealed'):]):064x}"

    def ops(self):
        return [c[0] for c in self.calls]


@override_settings(FC_LEDGER=LEDGER, CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class Base(TestCase):
    def setUp(self):
        cache.clear()
        self.fake = FakeBridge()
        p = mock.patch.object(bridge, "call", self.fake)
        p.start()
        self.addCleanup(p.stop)
        q = mock.patch.object(bridge, "stream", self.fake.stream)
        q.start()
        self.addCleanup(q.stop)
        # What the site sends the hub, as (user id, message), with no socket.
        self.pushed = []
        self.push_patch = mock.patch.object(push, "send", lambda uid, msg: self.pushed.append((uid, msg)))
        self.push_patch.start()
        self.addCleanup(self.push_patch.stop)
        pushed = self.pushed

        class Stream:
            def __init__(self, uid):
                self.uid = uid

            def send(self, msg):
                pushed.append((self.uid, msg))

            def close(self):
                pass
        st = mock.patch.object(push, "Stream", Stream)
        st.start()
        self.addCleanup(st.stop)
        # The model's requests to the hub (look, tail, press, type, start), and
        # the hub's replies by kind.
        self.asked = []
        self.hub_reply = {}
        # The model's lines in the person's shell, with their (result, output).
        self.run_out = {}

        def ask(req, timeout=5.0):
            self.asked.append(req)
            if "run" in req and "run" not in self.hub_reply:
                result, out = self.run_out.get(req["run"]["line"], ("exit 0", ""))
                return {"ok": {"out": out, "result": result}}
            return self.hub_reply.get(next(iter(req)), {"ok": {}})
        a = mock.patch.object(push, "ask", ask)
        a.start()
        self.addCleanup(a.stop)
        # What the site tells the hub about a person's shell: (user id, sealed, server).
        self.people = []
        self.person_patch = mock.patch.object(
            push, "person", lambda p, server: self.people.append((p.user_id, p.sealed, server)))
        self.person_patch.start()
        self.addCleanup(self.person_patch.stop)

    def person(self, handle, email=None, server=NET[0]):
        """A person with a key and the grant, at `server`, or at the ledger
        when it is empty, as for a new account."""
        u = get_user_model().objects.create_user(username=handle, email=email or f"{handle}@x.org")
        r = self.fake("newkey")
        self.fake("grant", pubkey=r["pubkey"])
        Profile.objects.create(user=u, handle=handle, pubkey=r["pubkey"], sealed=r["sealed"], granted=True,
                               server=server)
        return u

    def login(self, user):
        """A client with the person's session."""
        c = Client()
        c.force_login(user)
        return c

    def api(self, method, path, body=None, client=None):
        c = client or Client()
        data = json.dumps(body) if body is not None else None
        fn = getattr(c, method.lower())
        r = fn(path, data=data, content_type="application/json") if data is not None else fn(path)
        return r.status_code, r.json()


class Names(TestCase):
    def test_reserved_and_bad_handles(self):
        for bad in ["api", "man", "a", "-ab", "ab-", "a--b", "Ab", "x" * 33]:
            with self.assertRaises(ValidationError, msg=bad):
                validate_handle(bad)
        validate_handle("ana")
        validate_handle("a1")


class Auth(Base):
    def test_no_login_is_refused(self):
        for method, path in (("POST", "/api/send"), ("GET", "/api/me"), ("GET", "/api/chat")):
            s, j = self.api(method, path, {} if method == "POST" else None)
            self.assertEqual((s, j["error"]), (401, "NOT_LOGGED_IN"), path)
        r = Client().get("/api/me", HTTP_AUTHORIZATION="Bearer fc_old")
        self.assertEqual((r.status_code, r.json()["error"]), (401, "NOT_LOGGED_IN"))
        for path in ("/man", "/man/desk", "/api/watch"):
            self.assertEqual(Client().get(path).status_code, 404, path)

    def test_session_post_needs_csrf(self):
        u = self.person("ana")
        c = Client(enforce_csrf_checks=True)
        c.force_login(u)
        s, j = self.api("POST", "/api/send", {"to": "ana", "amount": "0.1"}, client=c)
        self.assertEqual((s, j["error"]), (403, "CSRF"))



class Pages(Base):
    def test_the_three_pages(self):
        u = self.person("ana", server="")
        c = Client()
        out = c.get("/")
        self.assertContains(out, 'name="email"')
        c.force_login(u)
        home = c.get("/")
        self.assertContains(home, "data-me-server")
        self.assertNotContains(home, 'name="email"')
        self.assertContains(c.get("/net"), "data-sky")
        self.assertContains(c.get("/account/"), u.profile.pubkey)

    def test_the_about_page_shows_fc_md_with_the_source_links(self):
        r = Client().get("/about")
        self.assertEqual(r.status_code, 200)
        for url in ("https://github.com/fcecin/fc", "https://github.com/fcecin/ces"):
            self.assertContains(r, url)
        self.assertContains(r, "What a person has")      # FC.md, which the model reads too
        self.assertContains(r, "fc/about.js")
        self.assertRegex(r.content.decode(), r'<body [^>]*data-tab="about"')
        self.assertContains(Client().get("/"), 'data-tab="about"')   # the tab, for visitors too

    def test_the_bar_marks_the_page(self):
        u = self.person("ana", server="")
        c = Client()
        self.assertNotContains(c.get("/"), 'data-tab="net"')
        c.force_login(u)
        for path, here in (("/", "home"), ("/net", "net"), ("/account/", "account"), ("/account/key", "account")):
            r = c.get(path)
            self.assertRegex(r.content.decode(), f'<body [^>]*data-tab="{here}"', msg=path)
            for tab in ("home", "net", "account", "about"):
                self.assertContains(r, f'data-tab="{tab}"', msg_prefix=path)
        home = c.get("/")
        self.assertContains(home, f"<bdi data-me-server>{LEDGER}</bdi>")
        self.assertContains(home, '<span class="tag" data-me-ledger>home</span>')
        u.profile.server = NET[0]
        u.profile.save()
        self.assertContains(c.get("/"), '<span class="tag" data-me-ledger hidden>home</span>')
        self.assertContains(c.get("/account/"), "log out")
        self.assertNotContains(c.get("/"), "data-me-balance")
        self.assertNotContains(c.get("/"), "log out")

    def test_the_things_are_gone(self):
        u = self.person("ana")
        c = Client()
        c.force_login(u)
        for path in ("/ana/lamp", "/ana/lamp.lua", "/new", "/api/things", "/api/say"):
            self.assertEqual(c.get(path).status_code, 404, path)

    def test_a_person_with_no_handle_chooses_one(self):
        u = get_user_model().objects.create_user(username="x", email="x@x.org")
        c = Client()
        c.force_login(u)
        self.assertContains(c.get("/"), 'name="handle"')
        c.post("/account/handle", {"handle": "api"})
        self.assertIsNone(Profile.objects.get(user=u).handle)
        c.post("/account/handle", {"handle": "zed"})
        self.assertEqual(Profile.objects.get(user=u).handle, "zed")


class KeyOf(Base):
    """A handle's key, for anyone, as one line of text and as a page."""

    def test_the_plain_route_gives_one_line_of_hex(self):
        u = self.person("ana")
        c = Client()
        r = c.get("/key/ana")
        self.assertEqual((r.status_code, r.content.decode()), (200, u.profile.pubkey + "\n"))
        self.assertEqual(r["Content-Type"], "text/plain; charset=utf-8")
        self.assertEqual(r["Access-Control-Allow-Origin"], "*")
        self.assertEqual(c.get("/key/ANA").content.decode(), u.profile.pubkey + "\n")
        r = c.get("/key/bob")
        self.assertEqual((r.status_code, r.content.decode()), (404, "no such handle: bob\n"))
        self.assertEqual(r["Access-Control-Allow-Origin"], "*")
        self.assertEqual(c.post("/key/ana").status_code, 405)

    def test_the_ledger_is_one_line_for_anyone(self):
        r = Client().get("/ledger")
        self.assertEqual((r.status_code, r.content.decode(), r["Content-Type"]),
                         (200, LEDGER + "\n", "text/plain; charset=utf-8"))
        self.assertEqual(r["Access-Control-Allow-Origin"], "*")

    def test_the_page_asks_and_shows_the_key(self):
        u = self.person("ana")
        c = Client()
        r = c.get("/key")
        self.assertContains(r, 'data-tab="key"')
        self.assertContains(r, 'name="handle"')
        self.assertNotContains(r, "no such handle")
        r = c.get("/key", {"handle": " Ana "})
        self.assertContains(r, f"<code>{u.profile.pubkey}</code>")
        self.assertContains(r, '<a href="/key/ana">/key/ana</a>')
        self.assertContains(c.get("/key", {"handle": "bob"}), "no such handle: bob")
        r = c.get("/key", {"handle": "a/b<x>"})
        self.assertContains(r, "no such handle: a/b&lt;x&gt;")
        self.assertContains(r, 'href="/key/a%2Fb%3Cx%3E"')

    def test_the_tab_is_on_each_page(self):
        u = self.person("ana")
        self.assertContains(Client().get("/"), 'data-tab="key"')
        c = Client()
        c.force_login(u)
        for path in ("/", "/net", "/account/"):
            self.assertContains(c.get(path), 'data-tab="key"', msg_prefix=path)


class Wake(Base):
    """A shell job wakes the model through the hub (hub_wake in fc/api.py),
    and the turn goes to the page through the hub."""

    def wake(self, uid, text, sign=True, exp=None):
        import time
        from django.conf import settings
        body = json.dumps({"uid": uid, "text": text, "exp": exp or int(time.time()) + 60})
        sig = hub.sign(hub.key(settings.SECRET_KEY), body) if sign else "nope"
        return Client().post("/api/hub/wake", data=body, content_type="application/json", HTTP_X_FC_HUB=sig)

    def test_a_job_wakes_the_model_and_the_page_sees_the_turn(self):
        u = self.person("ana")
        self.fake.answers = ["It printed hi, so the build works."]
        job = "job 1 ended with exit 0 after 3s: `make`.\nIts last lines:\nhi"
        r = self.wake(u.pk, job)
        self.assertEqual(r.json(), {"ok": True})
        self.assertEqual(list(ChatLine.objects.filter(user=u).exclude(kind="desk").values_list("kind", "text")),
                         [("job", job), ("model", "It printed hi, so the build works.")])
        self.assertIn(f"[site] {job}", self.fake.prompts[-1])
        chat = [m["chat"] for uid, m in self.pushed if uid == u.pk and "chat" in m]
        kinds = [next(iter(e)) for e in chat]
        self.assertEqual((kinds[0], kinds[-1]), ("line", "done"))
        self.assertIn("piece", kinds)
        self.assertEqual([e["line"]["kind"] for e in chat if "line" in e], ["job", "desk", "model"])
        self.assertEqual(chat[-1]["done"]["lines"][0]["text"], job)

    def test_a_wake_waits_for_a_turn_and_is_bounded(self):
        from . import api
        u = self.person("ana")
        cache.set(f"chat:busy:{u.pk}", 1, 60)                  # a turn runs
        self.assertEqual(self.wake(u.pk, "job 1 ended").json(), {"error": "BUSY"})
        cache.delete(f"chat:busy:{u.pk}")
        self.assertEqual(self.wake(u.pk, "job 1 ended").json(), {"ok": True})
        with mock.patch.object(api, "MAX_WAKES", 2):
            self.assertEqual(self.wake(u.pk, "job 2 ended").json(), {"error": "TOO_MANY"})
        self.assertEqual(ChatLine.objects.filter(user=u, kind="note").last().text,
                         api.TOO_MANY_WAKES.format(2))
        self.assertEqual([m["chat"]["note"]["kind"] for _, m in self.pushed if "note" in m.get("chat", {})], ["note"])
        self.assertEqual(self.wake(u.pk, "job 3 ended", sign=False).status_code, 403)
        self.assertEqual(self.wake(u.pk, "job 3 ended", exp=1).json()["error"], "EXPIRED")
        self.assertEqual(self.wake(999, "job 3 ended").json(), {"error": "no such person"})
        self.assertEqual(Client().get("/api/hub/wake").status_code, 405)


class Stats(Base):
    """The site's stats, a report that the stats process writes and the
    front page and /stats only read."""

    def tree(self):
        """A box's cgroup tree and /proc, in a scratch directory."""
        import tempfile
        from pathlib import Path
        from django.conf import settings
        root = Path(tempfile.mkdtemp(dir=settings.BASE_DIR.parent / "tmp"))
        self.addCleanup(__import__("shutil").rmtree, root)
        cg, proc = root / "cgroup", root / "proc"

        def unit(path, mem, usage, pids, procs=()):
            d = cg / path
            d.mkdir(parents=True)
            (d / "memory.current").write_text(f"{mem}\n")
            (d / "cpu.stat").write_text(f"usage_usec {usage}\nuser_usec 1\n")
            (d / "pids.current").write_text(f"{pids}\n")
            (d / "cgroup.procs").write_text("".join(f"{p}\n" for p in procs))
        unit("system.slice/ces.service", 3 << 30, 1_000_000, 40, [11, 12, 13])
        unit("system.slice/fc-app.service", 100 << 20, 500_000, 25, [21])
        unit("system.slice/system-fcnet.slice/fcnet@net1.service", 1 << 30, 0, 9, [30, 31])
        unit("system.slice/system-fcnet.slice/fcnet@net2.service", 1 << 30, 0, 3, [40])
        unit("system.slice/other.service", 5, 5, 5)              # another service
        proc.mkdir()
        (proc / "loadavg").write_text("0.50 0.25 0.10 1/400 999\n")
        (proc / "meminfo").write_text("MemTotal:       65536000 kB\nMemFree: 1 kB\nMemAvailable:   49152000 kB\n")
        (proc / "stat").write_text("cpu  100 0 100 700 100 0 0 0 0 0\n")
        (proc / "uptime").write_text("90061.5 1000.0\n")
        return cg, proc

    def test_the_report_counts_what_the_site_has_and_uses(self):
        from datetime import timedelta
        from django.utils import timezone
        from . import net, stats
        net.walk()
        a, b = self.person("ana"), self.person("bob")
        Profile.objects.filter(user=a).update(seen=timezone.now())
        Profile.objects.filter(user=b).update(seen=timezone.now() - timedelta(days=2),
                                              created=timezone.now() - timedelta(days=3))
        ChatLine.objects.create(user=a, kind="user", text="hi")
        ChatLine.objects.create(user=a, kind="model", text="hello", charged=150_000_000)
        ChatLine.objects.create(user=a, kind="run", text="ran `ls`")
        Widget.objects.create(user=a, name="game", kind="program", props={})
        self.hub_reply["stats"] = {"ok": {"people": 1, "visitors": 2, "pages": 4, "shells": 1}}
        cg, proc = self.tree()
        r, before = stats.build(cgroup=str(cg), proc=str(proc))
        self.assertEqual(r["people"], {"accounts": 2, "seen": 1, "new": 1})
        self.assertEqual(r["chat"], {"answers": 1, "said": 1, "runs": 1, "charged": 150_000_000})
        self.assertEqual(r["desk"]["programs"], 1)
        self.assertEqual(r["network"], {"servers": 1 + len(NET), "links": len(NET), "home": LEDGER})
        self.assertEqual(r["hub"]["pages"], 4)
        self.assertEqual([u["name"] for u in r["units"]], ["ces", "fc-app", "fcnet@net1", "fcnet@net2"])
        # A CES server: its process, and one per program.
        self.assertEqual([u["programs"] for u in r["units"]], [2, None, 1, 0])
        ces = r["units"][0]
        self.assertEqual((ces["memory"], ces["tasks"], ces["programs"], ces["cpu"]), (3 << 30, 40, 2, None))
        self.assertEqual(r["host"]["load"], [0.5, 0.25, 0.1])
        self.assertEqual(r["host"]["memory"], {"total": 65536000 * 1024, "available": 49152000 * 1024})
        self.assertEqual(r["host"]["up"], 90061)
        (cg / "system.slice/ces.service/cpu.stat").write_text("usage_usec 1500000\n")
        (proc / "stat").write_text("cpu  200 0 200 1400 200 0 0 0 0 0\n")
        before["at"] -= 10                                          # ten seconds ago
        r2, _ = stats.build(before, cgroup=str(cg), proc=str(proc))
        self.assertAlmostEqual(r2["units"][0]["cpu"], 5.0, delta=0.2)   # 0.5 s of CPU in 10 s
        self.assertEqual(r2["host"]["busy"], 20.0)                  # 800 of 1000 idle
        t = stats.text(r2)
        for line in ["people     2 accounts, 1 seen in 24 h, 1 new in 24 h",
                     "online     1 person, 2 visitors, 4 pages", "shells     1 running",
                     "chat       1 answer in 24 h, 1 line from people, 1 shell line, 1.5 credits of thought",
                     f"network    {1 + len(NET)} servers, {len(NET)} links, home {LEDGER}",
                     "memory     15.6G used of 62.5G", "up         1 day"]:
            self.assertIn(line, t)
        self.assertRegex(t, r"\nces +3\.0G +5\.\d% +40  2\n")
        self.assertRegex(t, r"\nfc-app +100M +0\.0% +25\n")             # not a server: no count
        self.assertRegex(t, r"\nfcnet@net2 +1\.0G +0\.0% +3  0\n")
        self.assertNotIn("ago", t)
        self.assertIn("ago", stats.text(r2, now=r2["at"] + 3600))
        self.hub_reply["stats"] = {"error": "no hub"}
        self.assertNotIn("online", stats.text(stats.build(cgroup=str(cg), proc=str(proc))[0]))

    def test_pages_only_read_the_report(self):
        from . import stats
        cg, proc = self.tree()
        path = cg.parent / "report.json"
        with override_settings(FC_STATS_FILE=str(path)):
            c = Client()
            self.assertNotContains(c.get("/"), 'class="stats"')
            r = c.get("/stats.txt")
            self.assertEqual((r.status_code, r.content), (503, b"no stats now\n"))
            stats.write(stats.build(cgroup=str(cg), proc=str(proc))[0], str(path))
            n = len(self.fake.calls)
            with self.assertNumQueries(0):
                r = c.get("/stats.txt")
            self.assertEqual(self.fake.calls[n:], [])
            self.assertEqual((r.status_code, r["Content-Type"]), (200, "text/plain; charset=utf-8"))
            self.assertIn("people     0 accounts", r.content.decode())
            self.assertContains(c.get("/"), '<pre class="stats">')
            report = json.loads(path.read_text())
            report["people"]["accounts"] = 7
            stats.write(report, str(path))
            self.assertIn("people     7 accounts", c.get("/stats.txt").content.decode())
            u = self.person("ana")
            c.force_login(u)
            self.assertNotContains(c.get("/"), 'class="stats"')
            self.assertContains(c.get("/"), 'data-tab="stats"')
            r = c.get("/stats")
            self.assertContains(r, 'data-tab="stats"')
            self.assertContains(r, f'<pre class="stats">{stats.text(stats.read())}</pre>', html=False)
            self.assertNotContains(Client().get("/"), 'data-tab="stats"')

    def test_a_call_marks_the_person_seen_once_in_a_while(self):
        from datetime import timedelta
        from django.utils import timezone
        from . import api
        u = self.person("ana")
        tok = self.login(u)
        self.assertIsNone(Profile.objects.get(user=u).seen)
        self.api("GET", "/api/me", client=tok)
        seen = Profile.objects.get(user=u).seen
        self.assertLess(timezone.now() - seen, timedelta(seconds=5))
        old = timezone.now() - timedelta(hours=1)
        Profile.objects.filter(user=u).update(seen=old)
        self.api("GET", "/api/me", client=tok)                     # within SEEN_SECS, so no write
        self.assertEqual(Profile.objects.get(user=u).seen, old)
        cache.delete(f"seen:{u.pk}")                              # SEEN_SECS later
        self.api("GET", "/api/me", client=tok)
        self.assertGreater(Profile.objects.get(user=u).seen, old)
        self.assertEqual(api.SEEN_SECS, 600)

    def test_the_hub_counts_its_pages_and_shells(self):
        from types import SimpleNamespace
        h = hub.Hub(b"k" * 32)
        self.assertEqual(h.stats(), {"people": 0, "visitors": 0, "pages": 0, "shells": 0})
        h.pages = {5: {"a", "b"}, 6: {"c"}, hub.VISITOR: {"v", "w"}}
        h.shells = SimpleNamespace(shells={5: SimpleNamespace(state=shellhub.LIVE),
                                           6: SimpleNamespace(state=shellhub.ENDED)})
        self.assertEqual(h.stats(), {"people": 2, "visitors": 2, "pages": 5, "shells": 1})
        import asyncio
        self.assertEqual(asyncio.run(h.ask({"stats": {}})), {"ok": h.stats()})


class Enter(Base):
    def code_of(self, n=-1):
        import re
        return re.search(r"[A-Z0-9]{4}-[A-Z0-9]{4}", mail.outbox[n].body).group(0)

    def test_a_new_email_enters_with_its_code(self):
        c = Client()
        r = c.post("/enter", {"email": "Zoe@X.org"})
        self.assertRedirects(r, "/enter/code", fetch_redirect_response=False)
        self.assertEqual(mail.outbox[-1].to, ["zoe@x.org"])
        r = c.post("/enter/code", {"code": self.code_of().lower().replace("-", "")})
        self.assertRedirects(r, "/", fetch_redirect_response=False)
        u = get_user_model().objects.get(email="zoe@x.org")
        p = Profile.objects.get(user=u)
        self.assertTrue(p.granted)
        self.assertContains(c.get("/"), 'name="handle"')   # the next page asks for the handle
        c.post("/account/handle", {"handle": "zoe"})
        self.assertEqual(Profile.objects.get(user=u).handle, "zoe")

    def test_a_known_email_enters_the_same_account(self):
        u = self.person("ana")
        c = Client()
        c.post("/enter", {"email": "ana@x.org"})
        c.post("/enter/code", {"code": self.code_of()})
        self.assertEqual(int(c.session["_auth_user_id"]), u.pk)
        self.assertEqual(get_user_model().objects.count(), 1)

    def test_the_login_outlives_a_reload_and_a_browser_restart(self):
        u = self.person("ana")
        c = Client()
        c.post("/enter", {"email": "ana@x.org"})
        r = c.post("/enter/code", {"code": self.code_of()})
        cookie = r.cookies[settings.SESSION_COOKIE_NAME]
        self.assertEqual(cookie["max-age"], 365 * 24 * 3600)
        self.assertTrue(cookie["httponly"])
        self.assertEqual(c.get("/api/me").json()["handle"], "ana")

    def test_the_code_page_does_not_tell_if_an_email_has_an_account(self):
        self.person("ana")
        pages = []
        for email in ("ana@x.org", "new@x.org"):
            c = Client()
            c.post("/enter", {"email": email})
            pages.append(c.get("/enter/code").content.decode().replace(email, "EMAIL"))
            pages[-1] = re.sub(r'name="csrfmiddlewaretoken" value="[^"]+"', "", pages[-1])
        self.assertEqual(pages[0], pages[1])
        self.assertNotIn('name="handle"', pages[0])
        c.post("/enter/code", {"code": self.code_of()})
        self.assertContains(c.get("/"), 'name="handle"')
        c.post("/account/handle", {"handle": "ana"})
        self.assertIsNone(Profile.objects.get(user__email="new@x.org").handle)   # taken
        c.post("/account/handle", {"handle": "neo"})
        self.assertEqual(Profile.objects.get(user__email="new@x.org").handle, "neo")

    def test_a_wrong_code_fails_and_the_code_dies_after_five(self):
        c = Client()
        c.post("/enter", {"email": "zoe@x.org"})
        good = self.code_of()
        for _ in range(4):
            r = c.post("/enter/code", {"code": "AAAA-AAAA"})
            self.assertContains(r, "That code is not right.")
        r = c.post("/enter/code", {"code": "AAAA-AAAA"})
        self.assertContains(r, "no longer works")
        r = c.post("/enter/code", {"code": good})
        self.assertRedirects(r, "/", fetch_redirect_response=False)
        self.assertNotIn("_auth_user_id", c.session)
        self.assertFalse(get_user_model().objects.exists())

    def test_the_tries_count_in_the_database(self):
        # Each worker reads the database, so tries from parallel requests all
        # count: here the other four tries came from elsewhere.
        from .models import EntryCode
        c = Client()
        c.post("/enter", {"email": "zoe@x.org"})
        good = self.code_of()
        EntryCode.objects.filter(email="zoe@x.org").update(tries=4)
        self.assertContains(c.post("/enter/code", {"code": "AAAA-AAAA"}), "no longer works")
        c.post("/enter", {"email": "zoe@x.org"})
        self.assertNotIn("_auth_user_id", c.session)
        r = c.post("/enter/code", {"code": good})   # the old code
        self.assertContains(r, "That code is not right.")
        r = c.post("/enter/code", {"code": self.code_of()})
        self.assertRedirects(r, "/", fetch_redirect_response=False)
        self.assertIn("_auth_user_id", c.session)
        self.assertFalse(EntryCode.objects.exists())

    def test_an_old_code_ends(self):
        from .models import EntryCode
        c = Client()
        c.post("/enter", {"email": "zoe@x.org"})
        EntryCode.objects.update(until=0)
        self.assertContains(c.post("/enter/code", {"code": self.code_of()}), "no longer works")

    def test_sends_are_limited(self):
        c = Client()
        for i in range(5):
            c.post("/enter", {"email": f"z{i}@x.org"})
        r = c.post("/enter", {"email": "z9@x.org"})
        self.assertEqual(r.status_code, 429)

    def test_leave(self):
        u = self.person("ana")
        c = Client()
        c.force_login(u)
        c.post("/leave")
        self.assertNotIn("_auth_user_id", c.session)
        self.assertIn({"wipe": {"uid": u.pk}}, self.asked)


class Account(Base):
    def test_a_new_account_starts_at_the_ledger(self):
        u = self.person("ana", server="")
        s, j = self.api("GET", "/api/me", client=self.login(u))
        self.assertEqual((j["server"], j["ledger"], j["account"], j["balance"]), (LEDGER, True, True, "1.00000000"))

    def test_travel_and_the_balance_there(self):
        u = self.person("ana", server="")
        tok = self.login(u)
        s, j = self.api("POST", "/api/me", {"server": NET[0]}, client=tok)
        self.assertEqual((s, j["server"], j["account"], j["balance"]), (200, NET[0], False, "0.00000000"))
        self.fake.fail["info"] = "CANNOT_CONNECT"
        cache.clear()
        s, j = self.api("POST", "/api/me", {"server": "127.0.0.1:1"}, client=tok)
        self.assertEqual((s, j["error"]), (400, "NO_ANSWER"))

    def test_send_to_a_handle_and_to_a_key(self):
        ana, bea = self.person("ana", server=""), self.person("bea", server="")
        tok = self.login(ana)
        s, j = self.api("POST", "/api/send", {"to": "bea", "amount": "0.25"}, client=tok)
        self.assertEqual((s, j["balance"]), (200, "0.75000000"))
        self.assertEqual(self.fake.balances[(LEDGER, bea.profile.pubkey)], 125_000_000)
        s, j = self.api("POST", "/api/send", {"to": "ab" * 32, "amount": "0.25"}, client=tok)
        self.assertEqual(j["balance"], "0.50000000")
        s, j = self.api("POST", "/api/send", {"to": "nobody", "amount": "0.25"}, client=tok)
        self.assertEqual(j["error"], "BAD_RECIPIENT")
        s, j = self.api("POST", "/api/send", {"to": "bea", "amount": "5"}, client=tok)
        self.assertEqual((s, j["message"]), (400, "Not enough credit on this server."))

    def test_move_to_another_server(self):
        u = self.person("ana", server="")
        tok = self.login(u)
        s, j = self.api("POST", "/api/move", {"to_server": NET[0], "amount": "0.4"}, client=tok)
        self.assertEqual((s, j["balance"]), (200, "0.60000000"))
        self.assertEqual(self.fake.balances[(NET[0], u.profile.pubkey)], 40_000_000)
        cross = [c for c in self.fake.calls if c[0] == "cross"][-1][1]
        self.assertEqual((cross["server"], cross["dest"], cross["dest_server"]), (LEDGER, u.profile.pubkey, NET[0]))
        s, j = self.api("POST", "/api/move", {"to_server": LEDGER, "amount": "0.1"}, client=tok)
        self.assertEqual(j["error"], "SAME_SERVER")

    def test_the_net_is_here_and_its_neighbors(self):
        u = self.person("ana", server="")
        s, j = self.api("GET", "/api/net", client=self.login(u))
        self.assertEqual(j["here"], LEDGER)
        self.assertEqual([n["address"] for n in j["nodes"]], [LEDGER, *NET])
        self.assertFalse(j["nodes"][0]["compute"])
        self.assertTrue(all(n["compute"] for n in j["nodes"][1:]))
        self.assertEqual(j["nodes"][0]["balance"], "1.00000000")
        self.assertEqual(j["edges"], [[LEDGER, a] for a in NET])
        asked = {c[1]["server"] for c in self.fake.calls if c[0] == "peers"}
        self.assertEqual(asked, {LEDGER})

    def test_bring_credit_here_from_a_neighbor(self):
        u = self.person("ana", server="")
        tok = self.login(u)
        self.api("POST", "/api/move", {"to_server": NET[0], "amount": "0.4"}, client=tok)
        self.api("POST", "/api/me", {"server": NET[0]}, client=tok)
        s, j = self.api("POST", "/api/move", {"from_server": LEDGER, "amount": "0.1"}, client=tok)
        self.assertEqual(s, 200, j)
        cross = [c for c in self.fake.calls if c[0] == "cross"][-1][1]
        self.assertEqual((cross["server"], cross["dest_server"]), (LEDGER, NET[0]))
        self.assertEqual(self.fake.balances[(NET[0], u.profile.pubkey)], 50_000_000)

    def test_the_navigator_page(self):
        u = self.person("ana")
        c = Client()
        c.force_login(u)
        self.assertContains(c.get("/net"), "data-sky")


class ChatPage(Base):
    def test_the_chat_names_the_ledger_that_pays_it(self):
        self.assertContains(self.login(self.person("ana")).get("/"), f'data-ledger="{LEDGER}"')


class Miner(Base):
    @override_settings(FC_MINER_DOWNLOADS="https://example.org/dl")
    def test_the_tab_puts_the_visitors_build_first(self):
        import re
        c = Client(HTTP_USER_AGENT="Mozilla/5.0 (Windows NT 10.0; Win64; x64)")
        self.assertContains(c.get("/"), 'data-tab="miner"')
        html = c.get("/miner").content.decode()
        self.assertEqual(re.findall(r'href="https://example.org/dl/([^"]+)"', html),
                         ["fc-miner-windows-x86_64.exe", "fc-miner-macos-arm64.zip",
                          "fc-miner-linux-x86_64.tar.gz", "SHA256SUMS"])
        self.assertIn('class="button primary" href="https://example.org/dl/fc-miner-windows-x86_64.exe"', html)
        html = Client(HTTP_USER_AGENT="Mozilla/5.0 (X11; Linux x86_64)").get("/miner").content.decode()
        self.assertEqual(re.findall(r'href="https://example.org/dl/([^"]+)"', html)[0], "fc-miner-linux-x86_64.tar.gz")

    @override_settings(FC_MINER_DOWNLOADS="")
    def test_an_installation_with_no_builds_has_no_tab(self):
        c = Client()
        self.assertNotContains(c.get("/"), 'data-tab="miner"')
        self.assertEqual(c.get("/miner").status_code, 404)


class Chat(Base):
    def test_a_line_and_its_answer_stay(self):
        u = self.person("ana", server="")
        tok = self.login(u)
        s, j = self.api("POST", "/api/chat", {"text": "hi there"}, client=tok)
        self.assertEqual(s, 200, j)
        self.assertEqual([x["kind"] for x in j["lines"]], ["user", "desk", "model"])   # the desk now, for the model
        self.assertEqual(j["lines"][2]["text"], "Hello.")
        self.assertEqual(j["balance"], "0.99999000")
        self.assertEqual(j["context"]["prompt_tokens"], len(self.fake.prompts[0]) // 4)
        n = len(self.fake.prompts[0])
        self.assertEqual(j["context"]["cached_percent"], int((n // 8) * 100 / (n // 4)))
        call = [c for c in self.fake.calls if c[0] == "think"][-1][1]
        self.assertEqual((call["server"], call["sealed"]), (LEDGER, u.profile.sealed))
        req = self.fake.requests[0]
        self.assertEqual([m["role"] for m in req["messages"]], ["system", "user"])
        self.assertTrue(req["messages"][1]["content"].startswith("hi there\n\n[site] the desk now"))
        self.assertEqual(req["tools"], [kernel.SHELL_TOOL])
        s, j = self.api("GET", "/api/chat", client=tok)
        self.assertEqual([x["kind"] for x in j["lines"]], ["user", "desk", "model"])

    def test_the_answer_streams(self):
        u = self.person("ana", server="")
        tok = self.login(u)
        self.fake.answer = "Hello there, friend."
        r = tok.post("/api/chat?stream=1", data=json.dumps({"text": "hi"}), content_type="application/json")
        self.assertEqual(r["Content-Type"], "text/event-stream")
        events = [json.loads(e[len("data: "):]) for e in b"".join(r.streaming_content).decode().split("\n\n") if e]
        kinds = [next(iter(e)) for e in events]
        self.assertEqual(kinds[0], "line")
        self.assertEqual("".join(e["piece"] for e in events if "piece" in e), "Hello there, friend.")
        self.assertGreater(kinds.count("piece"), 1)
        self.assertLess(kinds.index("piece"), kinds.index("done"))
        done = events[-1]["done"]
        self.assertEqual([x["kind"] for x in done["lines"]], ["user", "desk", "model"])
        self.assertEqual(done["balance"], "0.99999000")
        s, j = self.api("POST", "/api/chat", {"text": "again"}, client=tok)
        self.assertEqual(s, 200, j)

    def test_a_stream_that_fails_says_so(self):
        u = self.person("ana", server="")
        self.fake.fail["think"] = "CES_ERROR_INSUFFICIENT_BALANCE"
        r = self.login(u).post("/api/chat?stream=1", data=json.dumps({"text": "hi"}), content_type="application/json")
        events = [json.loads(e[len("data: "):]) for e in b"".join(r.streaming_content).decode().split("\n\n") if e]
        self.assertEqual(events[-1], {"error": "CES_ERROR_INSUFFICIENT_BALANCE",
                                      "message": "Not enough credit on the ledger for an answer."})

    def test_the_prompt_only_grows_at_its_end(self):
        u = self.person("ana", server="")
        tok = self.login(u)
        for t in ("one", "two", "three"):
            self.api("POST", "/api/chat", {"text": t}, client=tok)
        p = self.fake.requests
        for a, b in zip(p, p[1:]):
            self.assertEqual(b["messages"][:len(a["messages"])], a["messages"], "a request changed before its end")

    def test_the_older_lines_fold_into_a_summary(self):
        u = self.person("ana", server="")
        tok = self.login(u)
        # A small window, with room for the rules and about 1000 tokens of
        # conversation, since the fake counts 4 bytes per token. So 12 turns of
        # about 1 KB fold.
        room = (kernel._size(kernel.request(u)) // 4 + 1000) * 100 // kernel.COMPACT_AT
        self.fake.window = 4096 + room
        self.fake.answer = "x" * 900
        for i in range(12):
            s, j = self.api("POST", "/api/chat", {"text": f"turn {i}"}, client=tok)
            self.assertEqual(s, 200, j)
        fold = [r["messages"][0]["content"] == kernel.FOLD_RULES for r in self.fake.requests]
        self.assertGreaterEqual(fold.count(True), 1)
        self.assertLessEqual(fold.count(True), 4)
        last = [p for p, f in zip(self.fake.prompts, fold) if not f][-1]
        self.assertIn("## Earlier in this conversation (a summary)\n\nSUMMARY", last)
        self.assertIn("USER: turn 11", last)
        self.assertNotIn("USER: turn 0\n", last)
        for p in self.fake.prompts:
            self.assertLessEqual(len(p) // 4, room)
        s, j = self.api("GET", "/api/chat", client=tok)
        self.assertIn("turn 0", [x["text"] for x in j["lines"]])

    def test_no_credit_is_said(self):
        u = self.person("ana", server="")
        self.fake.fail["think"] = "CES_ERROR_INSUFFICIENT_BALANCE"
        s, j = self.api("POST", "/api/chat", {"text": "hi"}, client=self.login(u))
        self.assertEqual((s, j["error"]), (402, "CES_ERROR_INSUFFICIENT_BALANCE"))
        self.assertEqual(j["message"], "Not enough credit on the ledger for an answer.")

    def test_one_line_at_a_time(self):
        from django.core.cache import cache as c
        u = self.person("ana", server="")
        c.add(f"chat:busy:{u.pk}", 1, 60)
        s, j = self.api("POST", "/api/chat", {"text": "hi"}, client=self.login(u))
        self.assertEqual((s, j["error"]), (409, "BUSY"))

    def test_a_new_conversation(self):
        u = self.person("ana", server="")
        tok = self.login(u)
        self.api("POST", "/api/chat", {"text": "hi"}, client=tok)
        s, j = self.api("POST", "/api/chat/new", {}, client=tok)
        self.assertEqual(j["lines"], [])
        self.assertEqual(self.api("GET", "/api/chat")[0], 401)
        s, j = self.api("POST", "/api/chat", {"text": "  "}, client=tok)
        self.assertEqual(j["error"], "BAD_TEXT")

    def test_the_home_page_has_the_chat(self):
        u = self.person("ana", server="")
        c = Client()
        self.assertNotContains(c.get("/"), "data-chat")
        c.force_login(u)
        home = c.get("/")
        self.assertContains(home, "data-chat-form")
        self.assertContains(home, "fc/vendor/winbox/winbox.min.js")
        self.assertContains(home, "fc/desk.js")

    def test_the_desk_keeps_the_windows(self):
        u = self.person("ana", server="")
        tok = self.login(u)
        s, j = self.api("GET", "/api/desk", client=tok)
        self.assertEqual([w["name"] for w in j["widgets"]], ["chat", "shell"])
        rev = j["rev"]
        s, j = self.api("POST", "/api/desk", {"op": "move chat 10 135"}, client=tok)
        self.assertEqual((s, j["out"]), (200, "move chat 10 135"), j)
        self.assertEqual((j["widgets"][0]["x"], j["widgets"][0]["y"]), (10, 135))
        s, j = self.api("GET", f"/api/desk?since={rev}", client=tok)
        self.assertEqual((j["rev"], [(w["name"], w["x"]) for w in j["widgets"]]), (rev + 1, [("chat", 10), ("shell", 0)]))
        self.assertEqual(self.api("GET", f"/api/desk?since={rev + 1}", client=tok)[1], {"rev": rev + 1})
        self.assertEqual(Widget.objects.get(user=u, name="chat").x, 10)
        s, j = self.api("POST", "/api/desk", {"op": "del chat"}, client=tok)
        self.assertEqual((s, j["error"]), (400, "DESK"))


class Desk(Base):
    def ops(self, u, *lines):
        return [desk.run(u, l)[0] for l in lines]

    def test_the_hub_runs_a_desk_op_of_a_shell(self):
        import time
        from django.conf import settings
        u = self.person("ana")
        k = hub.key(settings.SECRET_KEY)

        def post(body, sig=None):
            raw = json.dumps(body)
            return self.client.post("/api/hub/desk", raw, content_type="application/json",
                                    HTTP_X_FC_HUB=hub.sign(k, raw) if sig is None else sig)
        exp = int(time.time()) + 30
        r = post({"uid": u.pk, "op": "new text milk 'buy milk'", "exp": exp})
        self.assertEqual(r.json(), {"out": "new text milk"})
        self.assertEqual(Widget.objects.get(user=u, name="milk").props["text"], "buy milk")
        self.assertEqual(post({"uid": u.pk, "op": "move nothing 1 2", "exp": exp}).json(),
                         {"error": "no widget named nothing (ls lists them)"})
        self.assertEqual(post({"uid": u.pk, "op": "ls", "exp": exp}, sig="0" * 64).status_code, 403)
        self.assertEqual(post({"uid": u.pk, "op": "ls", "exp": exp - 100}).json()["error"], "EXPIRED")
        self.assertEqual(self.client.get("/api/hub/desk").status_code, 405)

    def test_a_program_window_keeps_its_program(self):
        u = self.person("ana")
        self.assertEqual(self.ops(u, "new program game 12"), ["new program game"])
        w = Widget.objects.get(user=u, name="game")
        self.assertEqual((w.kind, w.w, w.h, w.props), ("program", 560, 420,
                                                       {"title": "game", "pid": 12, "server": NET[0]}))
        self.ops(u, 'new program dice 3 on net.example:55000 title "dice" at 10 20 size 400 300')
        w = Widget.objects.get(user=u, name="dice")
        self.assertEqual((w.x, w.y, w.w, w.h, w.props["server"], w.props["title"]),
                         (10, 20, 400, 300, "net.example:55000", "dice"))
        j = desk.widget_json(w)
        self.assertEqual((j["pid"], j["server"]), (3, "net.example:55000"))
        self.assertIn("pid: 3", self.ops(u, "show dice")[0])
        for line, why in (("new program x seven", "a pid is a number"),
                          ("new program x 7 on nowhere", "on <host:port>"),
                          ("new program x", "new program <name> <pid>")):
            with self.assertRaises(desk.DeskError, msg=line) as c:
                desk.run(u, line)
            self.assertIn(why, str(c.exception), line)

    def test_vs1_is_a_read(self):
        u = self.person("ana")
        rev = Profile.objects.get(user=u).desk_rev
        out = self.ops(u, "vs1")[0]
        self.assertTrue(out.startswith("VS1 (Visual System 1)."), out[:80])
        self.assertIn('  local vs1 = ces.load("vs1/vs1.lua")', out)
        self.assertIn("131 focus  [u8 on][u32 time]", out)
        head = desk.VS1.read_text().split("local vs1 = {}")[0].rstrip().splitlines()
        self.assertEqual(len(out.splitlines()), len(head))
        self.assertIn("vs1", desk.READS)
        self.assertEqual(Profile.objects.get(user=u).desk_rev, rev)
        import os
        agents = open(os.path.join(os.path.dirname(__file__), "shell", "AGENTS.md")).read()
        self.assertIn('local vs1 = ces.load("vs1/vs1.lua")', agents)
        self.assertIn("for e in vs1.events(conn, data) do", agents)

    def test_look_tail_press_and_type_go_to_the_hub(self):
        u = self.person("ana")
        self.ops(u, "new program game 5", 'new text note "x"')
        rev = Profile.objects.get(user=u).desk_rev
        self.hub_reply["look"] = {"ok": {"title": "g", "cols": 3, "rows": 2, "text": "abc\n d"}}
        self.assertEqual(self.ops(u, "look game")[0], 'game 3x2 "g"\nabc\n d')
        self.assertEqual(self.ops(u, "grid game")[0], 'game 3x2 "g"\n |123\n1|abc\n2| d')
        self.assertEqual(self.asked[-1], {"look": {"uid": u.pk, "w": "game", "rect": None}})
        self.hub_reply["look"] = {"ok": {"title": "g", "cols": 3, "rows": 2, "text": "bc"}}
        self.assertEqual(self.ops(u, "look game 2 2 3 2", "grid game 2 2 3 2"),
                         ['game 3x2 "g" from 2,2\nbc', 'game 3x2 "g" from 2,2\n |23\n2|bc'])
        self.assertEqual(self.asked[-1]["look"]["rect"], [2, 2, 3, 2])
        self.hub_reply["look"] = {"ok": {"title": "", "cols": 12, "rows": 10, "text": "\n".join("x" * 10)}}
        out = self.ops(u, "grid game 9 1 12 10")[0].split("\n")
        self.assertEqual(out[1:4], ["  | 1", "  |9012", " 1|x"])
        self.assertEqual(out[-1], "10|x")
        self.assertEqual(self.ops(u, "press game ArrowRight ArrowRight:600 Space")[0],
                         "pressed ArrowRight ArrowRight:600 Space in game")
        self.assertEqual(self.asked[-1], {"press": {"uid": u.pk, "w": "game",
                                                    "keys": [["ArrowRight", 30], ["ArrowRight", 600], ["Space", 30]]}})
        self.assertEqual(self.ops(u, "click game 3 4", "click game 1 2 1"), ["clicked 3,4 in game", "clicked 1,2 in game"])
        self.assertEqual(self.asked[-2:], [{"click": {"uid": u.pk, "w": "game", "x": 3, "y": 4, "button": 0}},
                                           {"click": {"uid": u.pk, "w": "game", "x": 1, "y": 2, "button": 1}}])
        self.assertEqual(self.ops(u, 'type shell "ls -l\\n\\\\x"')[0], "typed 8 characters in shell")
        self.assertEqual(self.asked[-1], {"type": {"uid": u.pk, "w": "shell", "text": "ls -l\n\\x"}})
        self.hub_reply["tail"] = {"ok": {"text": "$ cesh query\n"}}
        self.assertEqual(self.ops(u, "tail shell 500", "tail shell"), ["$ cesh query\n"] * 2)
        self.assertEqual([a["tail"]["n"] for a in self.asked[-2:]], [500, desk.TAIL_CHARS])
        self.assertEqual(Profile.objects.get(user=u).desk_rev, rev)
        self.hub_reply["look"] = {"error": "the window game is not on a program now"}
        for line, why in [("look game", "the window game is not on a program now"),
                          ("look shell", "look takes a program window"), ("tail game", "tail takes the shell"),
                          ("grid shell", "grid takes a program window"),
                          ("look note", "note is a text window"), ("press game", "press <name>"),
                          ("press game a:x", "a key is <name> or <name>:<ms>"),
                          ("look game 0 1 2 2", "a cell is a number from 1"),
                          ("press game a:6000 b:6000", "hold 10000 ms at most"),
                          ('type game "a" "b"', "type <name>"), ("tail shell 0", "a count is a number"),
                          ("click shell 1 1", "click takes a program window"), ("click game 1", "click <name>"),
                          ("click game 0 1", "a cell is a number from 1"), ("click game 1 1 9", "a button")]:
            with self.assertRaises(desk.DeskError, msg=line) as c:
                desk.run(u, line)
            self.assertIn(why, str(c.exception), line)

    def test_the_ops_make_change_and_delete_a_window(self):
        u = self.person("ana", server="")
        self.ops(u, 'new text milk "buy milk" title "today" at 400 120 size 300 150')
        w = Widget.objects.get(user=u, name="milk")
        self.assertEqual((w.kind, w.x, w.y, w.w, w.h, w.props), ("text", 400, 120, 300, 150, {"title": "today", "text": "buy milk"}))
        self.ops(u, 'set milk text "buy milk and eggs"', "fold milk", "size milk 320 160")
        w.refresh_from_db()
        self.assertEqual((w.props["text"], w.folded, w.w), ("buy milk and eggs", True, 320))
        out = self.ops(u, "ls")[0]
        self.assertIn("chat  chat", out)
        self.assertIn('milk  text  at 400 120  size 320 160  folded  "today"', out)
        self.assertIn("text: buy milk and eggs", self.ops(u, "show milk")[0])
        self.ops(u, "del milk")
        self.assertFalse(Widget.objects.filter(user=u, name="milk").exists())
        self.assertEqual(Profile.objects.get(user=u).desk_rev, 5)

    def test_writers_wait_in_turn(self):
        # Two pages send ops at the same moment, and each op reads, then writes.
        # On SQLite, a transaction must take the write lock when it begins, or
        # two of them fail with "database is locked". The tests' in-memory
        # database can't show that, so this checks the setting.
        from django.conf import settings as s
        o = s.DATABASES["default"]["OPTIONS"]
        self.assertEqual(o["transaction_mode"], "IMMEDIATE")
        self.assertIn("journal_mode=WAL", o["init_command"])

    def test_bad_ops_are_refused(self):
        u = self.person("ana", server="")
        for line, why in [("del chat", "cannot be deleted"), ("set chat text \"x\"", "no text"),
                          ("move nobody 1 1", "no widget named"), ("move chat -1 5", "from 0"),
                          ('new text Milk "x"', "a name is"), ('new text a "x" colour red', "not 'colour'"),
                          ('new text a "unclosed', "quote"), ("fly chat", "no op"),
                          ("size chat 251 35", "at least 200 wide and 100 high"),
                          ('new text a "x" size 100 300', "at least 200"),
                          ("new text a " + '"' + "x" * 4001 + '"', "at most 4000")]:
            with self.assertRaises(desk.DeskError, msg=line) as c:
                desk.run(u, line)
            self.assertIn(why, str(c.exception), line)
        self.assertEqual(Profile.objects.get(user=u).desk_rev, 0)

    def test_the_chat_runs_no_command(self):
        # A chat line is words for the model, even with "$" in front, since the
        # person runs commands in the shell.
        u = self.person("ana", server="")
        s, j = self.api("POST", "/api/chat", {"text": '$ new text hi "hello there"'}, client=self.login(u))
        self.assertEqual(s, 200, j)
        self.assertEqual([x["kind"] for x in j["lines"]], ["user", "desk", "model"])
        self.assertEqual(len(self.fake.prompts), 1)
        self.assertFalse(Widget.objects.filter(user=u, name="hi").exists())

    def test_the_model_types_desk_lines_in_the_shell(self):
        u = self.person("ana", server="")
        tok = self.login(u)
        self.fake.answer = sh('desk new text milk "buy milk"', text="Done, I made the note.")
        self.run_out['desk new text milk "buy milk"'] = ("exit 0", "new text milk\n")
        s, j = self.api("POST", "/api/chat", {"text": "make me a note: buy milk"}, client=tok)
        self.assertEqual(s, 200, j)
        self.assertEqual([x["kind"] for x in j["lines"]], ["user", "desk", "model", "run"])
        self.assertEqual(j["lines"][2]["text"], "Done, I made the note.")
        self.assertEqual(j["lines"][3]["text"], 'ran `desk new text milk "buy milk"`: exit 0\nnew text milk')
        self.assertEqual(len(self.fake.prompts), 1)   # nothing to read back, so one call
        self.assertEqual([a["run"]["line"] for a in self.asked if "run" in a], ['desk new text milk "buy milk"'])
        self.assertIn("cat ~/AGENTS.md", self.fake.prompts[0])
        self.assertNotIn("new text <name>", self.fake.prompts[0])

    def test_a_read_goes_back_to_the_model(self):
        u = self.person("ana", server="")
        tok = self.login(u)
        self.fake.answers = [sh("desk ls"), "You have the chat only."]
        self.run_out["desk ls"] = ("exit 0", "chat  chat\n")
        s, j = self.api("POST", "/api/chat", {"text": "what is on my desk?"}, client=tok)
        self.assertEqual([x["kind"] for x in j["lines"]], ["user", "desk", "model", "run", "model"])
        self.assertEqual(self.fake.requests[1]["messages"][-1],
                         {"role": "tool", "tool_call_id": "call_1", "content": "ran `desk ls`: exit 0\nchat  chat"})
        self.assertEqual(j["lines"][-1]["text"], "You have the chat only.")

    def test_the_shell_runs_ops_and_keeps_nothing(self):
        u = self.person("ana", server="")
        self.assertIn("shell  shell", self.ops(u, "ls")[0])
        with self.assertRaises(desk.DeskError):
            desk.run(u, "del shell")
        rev = Profile.objects.get(user=u).desk_rev
        with self.captureOnCommitCallbacks(execute=True):
            self.ops(u, 'run shell "ls"', 'run shell "new text a \\"hi there\\""',
                     'run shell "del nobody"', 'run shell "help"')
        said = [m["shell"] for _, m in self.pushed if "shell" in m]
        self.assertEqual([x["name"] for x in said], ["shell"] * 4)
        entries = [e for x in said for e in x["entries"]]
        self.assertEqual([e["k"] for e in entries], ["in", "out", "in", "out", "in", "err", "in", "out"])
        self.assertEqual(entries[0]["t"], "desk ls")
        self.assertIn("shell  shell", entries[1]["t"])
        self.assertIn("no widget named nobody", entries[5]["t"])
        self.assertIn('run shell "<op>"', entries[7]["t"])
        self.assertIn("shell  shell", self.ops(u, 'run shell "desk ls"')[0])
        self.assertEqual(Widget.objects.get(user=u, name="a").props["text"], "hi there")
        self.assertNotIn("lines", Widget.objects.get(user=u, name="shell").props)
        self.assertNotIn("lines", [w for w in desk.state(u)["widgets"] if w["name"] == "shell"][0])
        self.assertEqual(Profile.objects.get(user=u).desk_rev, rev + 1)
        self.assertEqual(len([m for _, m in self.pushed if "desk" in m]), 1)
        self.assertIn("DESK(7)", self.ops(u, 'run shell "man"')[0])
        self.assertIn("not run", self.ops(u, 'run shell "run shell ls"')[0])
        with self.assertRaises(desk.DeskError):
            desk.run(u, 'run a "ls"')
        self.pushed.clear()
        with self.captureOnCommitCallbacks(execute=True):
            self.ops(u, 'run shell "clear"')
        self.assertEqual(self.pushed, [(u.pk, {"shell": {"name": "shell", "clear": True}})])

    def test_every_window_has_hard_bounds(self):
        u = self.person("ana", server="")
        with self.assertRaises(desk.DeskError) as c:
            desk.run(u, 'run shell "' + "x" * (desk.ENTRY_CHARS + 1) + '"')
        self.assertIn("at most 4000", str(c.exception))
        for i in range(desk.MAX_WIDGETS - 2):
            self.ops(u, f'new text t{i} "x"')
        with self.assertRaises(desk.DeskError) as c:
            desk.run(u, 'new text one-more "x"')
        self.assertIn("at most 20", str(c.exception))
        self.assertEqual(Widget.objects.filter(user=u).count(), 20)

    def test_the_chat_shows_the_balance_that_pays(self):
        from django.conf import settings as st
        u = self.person("ana", server="")
        tok = self.login(u)
        Profile.objects.filter(user=u).update(server="127.0.0.1:55200")
        seen = []
        real = account.balance
        with mock.patch.object(account, "balance", side_effect=lambda p, s: (seen.append(s), real(p, s))[1]):
            self.api("GET", "/api/chat", client=tok)
        self.assertEqual(seen, [st.FC_LEDGER])

    def test_the_chat_keeps_its_newest_lines(self):
        from . import kernel
        u = self.person("ana", server="")
        ChatLine.objects.bulk_create([ChatLine(user=u, kind="note", text=str(i)) for i in range(kernel.MAX_LINES + 5)])
        kernel._trim(u)
        kept = list(ChatLine.objects.filter(user=u).order_by("id").values_list("text", flat=True))
        self.assertEqual((len(kept), kept[0]), (kernel.MAX_LINES, "5"))

    def test_the_read_ops_show_everything(self):
        u = self.person("ana", server="")
        self.ops(u, 'new text milk "buy milk" title "today"', "move milk 400 120")
        self.assertEqual(self.ops(u, "show milk")[0].splitlines()[:6],
                         ["name: milk", "kind: text", "x: 400", "y: 120", "w: 360", "h: 200"])
        self.assertIn("cannot be deleted", self.ops(u, "show chat")[0])
        self.assertEqual(self.ops(u, "screen")[0], "(the page has not told its size yet)")
        self.assertEqual(self.ops(u, "screen 1280 751", "screen")[1], "1280 751")
        self.assertEqual(Profile.objects.get(user=u).desk_rev, 2)
        self.ops(u, 'set milk text "a \\"quoted\\" word and a \\\\ back"')
        self.assertEqual(Widget.objects.get(user=u, name="milk").props["text"], 'a "quoted" word and a \\ back')

    def test_the_model_sees_the_desk_as_it_is_now(self):
        u = self.person("ana", server="")
        tok = self.login(u)
        self.api("POST", "/api/desk", {"op": "screen 1280 751"}, client=tok)
        self.api("POST", "/api/chat", {"text": "hi"}, client=tok)
        first = self.fake.prompts[-1]
        self.assertIn("[site] the desk now (the screen: 1280 wide, 751 high):\nchat  chat  at 0 0", first)
        for x in (10, 20, 30):
            desk.run(u, f"move chat {x} 40")
        self.api("POST", "/api/chat", {"text": "what moved?"}, client=tok)
        second = self.fake.prompts[-1]
        self.assertTrue(second.startswith(first), "the request changed before its end")
        tail = second[len(first):]
        self.assertEqual(tail.count("[site] the desk now"), 1)
        self.assertIn("chat  chat  at 30 40", tail)
        self.assertNotIn("at 10 40", tail)
        self.api("POST", "/api/chat", {"text": '$ new text n1 "one"'}, client=tok)
        self.api("POST", "/api/chat", {"text": "and now?"}, client=tok)
        self.assertEqual(self.fake.prompts[-1][len(second):].count("[site] the desk now"), 0)


class Hub(Base):
    """The site sends every change to the hub when it commits, and the hub
    pushes it to the person's open pages."""

    def test_a_desk_change_goes_to_the_pages_when_it_commits(self):
        u = self.person("ana", server="")
        with self.captureOnCommitCallbacks(execute=True):
            desk.run(u, "move chat 5 6")
        self.assertEqual(len(self.pushed), 1)
        uid, msg = self.pushed[0]
        self.assertEqual(uid, u.pk)
        self.assertEqual(msg, {"desk": desk.state(u)})
        self.assertEqual([w["x"] for w in msg["desk"]["widgets"] if w["name"] == "chat"], [5])
        self.pushed.clear()
        with self.captureOnCommitCallbacks(execute=True):
            desk.run(u, "ls")
            with self.assertRaises(desk.DeskError):
                desk.run(u, "del chat")
        self.assertEqual(self.pushed, [])
        with self.captureOnCommitCallbacks(execute=True):
            desk.run(u, 'run shell "new text a hi"')
        self.assertEqual(sorted(next(iter(m)) for _, m in self.pushed), ["desk", "shell"])
        d = [m["desk"] for _, m in self.pushed if "desk" in m][0]
        self.assertIn("a", [w["name"] for w in d["widgets"]])


    def test_a_page_gets_a_ticket_for_its_person(self):
        u = self.person("ana", server="")
        s, j = self.api("GET", "/api/hub", client=self.login(u))
        self.assertEqual(s, 200, j)
        from django.conf import settings as st
        k = hub.key(st.SECRET_KEY)
        self.assertEqual(hub.check(k, j["ticket"]), u.pk)
        self.assertIsNone(hub.check(k, j["ticket"], now=10 ** 12))        # expired
        self.assertIsNone(hub.check(hub.key("another"), j["ticket"]))     # another key
        self.assertIsNone(hub.check(k, j["ticket"].replace(f"{u.pk}.", f"{u.pk + 1}.", 1)))
        s, j = self.api("GET", "/api/hub")                                # a visitor
        self.assertEqual((s, hub.check(k, j["ticket"])), (200, hub.VISITOR))

    def test_the_hub_gives_a_change_to_the_pages_of_its_person_only(self):
        import asyncio
        import os
        import tempfile
        from websockets.asyncio.client import connect
        from websockets.exceptions import ConnectionClosed
        self.push_patch.stop()   # the real push.send
        base = os.path.join(os.path.dirname(__file__), "..", "..", "tmp")
        os.makedirs(base, exist_ok=True)
        k = hub.key("test-secret")

        async def go():
            with tempfile.TemporaryDirectory(dir=base) as d:
                sock = os.path.join(d, "hub.sock")
                ready = asyncio.get_running_loop().create_future()
                task = asyncio.create_task(hub.serve(k, sock, "127.0.0.1", 0, ready=ready))
                port = await ready
                url = f"ws://127.0.0.1:{port}/hub?t="
                ana1 = await connect(url + hub.ticket(k, 1))
                bob = await connect(url + hub.ticket(k, 2))
                ana2 = await connect(url + hub.ticket(k, 1))
                with self.assertRaises(ConnectionClosed):
                    await asyncio.wait_for(ana1.recv(), 2)
                self.assertEqual(ana1.close_code, hub.ELSEWHERE)
                v1 = await connect(url + hub.ticket(k, hub.VISITOR))
                v2 = await connect(url + hub.ticket(k, hub.VISITOR))
                bad = await connect(url + "1.99999999999.nope")
                with self.assertRaises(ConnectionClosed):
                    await asyncio.wait_for(bad.recv(), 2)
                self.assertEqual(bad.close_code, 4001)
                with override_settings(FC_HUB_SOCKET=sock):
                    await asyncio.to_thread(push.send, 1, {"desk": {"rev": 7}})
                self.assertEqual(json.loads(await asyncio.wait_for(ana2.recv(), 2)), {"desk": {"rev": 7}})
                for p in (bob, v1):
                    with self.assertRaises(asyncio.TimeoutError):
                        await asyncio.wait_for(p.recv(), 0.3)
                with override_settings(FC_HUB_SOCKET=sock):
                    await asyncio.to_thread(push.send, "all", {"net": True})
                got = [json.loads(await asyncio.wait_for(p.recv(), 2)) for p in (ana2, bob, v1, v2)]
                self.assertEqual(got, [{"net": True}] * 4)
                await v1.close()
                await asyncio.sleep(0.05)
                with override_settings(FC_HUB_SOCKET=sock):
                    await asyncio.to_thread(push.send, "all", {"net": True})
                self.assertEqual(json.loads(await asyncio.wait_for(v2.recv(), 2)), {"net": True})
                for p in (ana2, bob, v2):
                    await p.close()
                task.cancel()

        asyncio.run(go())
        with override_settings(FC_HUB_SOCKET=os.path.join(base, "no-such-hub.sock")):
            with self.assertLogs("fc.push", "WARNING"):
                push.send(1, {"desk": {}})


class CeshArgs(TestCase):
    """The cesh commands that the site's shell refuses, and a line's heredoc."""

    def test_the_shell_of_the_site_disables_some_commands(self):
        def why(line):
            return ceshargs.disabled(ceshargs.split(line))
        self.assertEqual(why("cesh chat"), "chat is disabled here: use the chat window")
        self.assertEqual(why("--server h:1 -lx chat --listen s"), why("chat"))
        self.assertEqual(why("keys export"), "keys is disabled here: "
                         "the site keeps your key, and the model reads this shell")
        self.assertEqual(why("man --serve 8080"), "man --serve is disabled here: it starts a web server")
        self.assertIn("is disabled here", why("bootstrap --solutions 0"))
        self.assertIn("is disabled here", why("mine -t 2"))
        for line in ("query @0", "man", "man chat", "-l chat query", "think hello", "dial 5"):
            self.assertIsNone(why(line), line)

    def test_a_heredoc_gives_its_lines_to_the_command(self):
        self.assertEqual(ceshargs.here("cesh file put - a.lua <<END"), ("cesh file put - a.lua", "END"))
        self.assertEqual(ceshargs.here("cesh file put - a.lua << 'EOF' "), ("cesh file put - a.lua", "EOF"))
        self.assertEqual(ceshargs.here('cesh think "a <<END"'), ('cesh think "a <<END"', None))
        self.assertEqual(ceshargs.here("cesh query <<END x"), ("cesh query <<END x", None))

    def test_a_heredoc_needs_its_end_line_somewhere_after_it(self):
        self.assertIsNone(ceshargs.unclosed("cat > a <<'END'\nx\nEND\nwc -l a"))
        self.assertIsNone(ceshargs.unclosed("cat > a <<END\nx\nEND\ncat > b <<EOF\ny\nEOF"))
        self.assertIsNone(ceshargs.unclosed("ls\ncat > a <<END\nEND"))
        self.assertIsNone(ceshargs.unclosed("echo hi"))
        self.assertEqual(ceshargs.unclosed("cat > a <<END\nx\n  END"), "END")
        self.assertEqual(ceshargs.unclosed("cat > a <<END\nx\nEND\ncat > b <<EOF\ny"), "EOF")
        self.assertEqual(ceshargs.unclosed("ls\ncat > a <<END"), "END")

    def test_a_long_output_is_cut_for_the_model(self):
        self.assertIn("[... 9900 more bytes]", aishell.cap("x" * 10000, 100))
        self.assertEqual(aishell.cap("short", 100), "short")


class ChatCesh(Base):
    """The model works in the person's shell, where each of its lines runs at
    once in bash, as the person (fc/aishell.py, through the hub)."""

    def turn(self, u, text, shell=None):
        return kernel.say(Profile.objects.get(user=u), text, shell)

    def runs(self):
        """The lines that the model ran in the person's shell."""
        return [r["run"]["line"] for r in self.asked if "run" in r]

    def test_a_line_runs_in_the_shell_and_the_model_reads_it(self):
        u = self.person("ana")
        self.run_out["cesh query"] = ("exit 0", '{"balance":5}\n')
        self.fake.answers = [sh("cesh query", text="I look."), "You have 5."]
        lines = self.turn(u, "what do I have?")
        self.assertEqual([x.kind for x in lines], ["user", "desk", "model", "run", "model"])
        self.assertEqual(self.asked, [{"mine": {"uid": u.pk}},
                                      {"run": {"uid": u.pk, "line": "cesh query", "timeout": aishell.RUN_SECS}}])
        answer, result = self.fake.requests[-1]["messages"][-2:]
        self.assertEqual(answer, {"role": "assistant", "content": "I look.", "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "shell", "arguments": '{"command": "cesh query"}'}}]})
        self.assertEqual(result, {"role": "tool", "tool_call_id": "call_1",
                                  "content": 'ran `cesh query`: exit 0\n{"balance":5}'})
        rules = self.fake.requests[0]["messages"][0]["content"]
        self.assertTrue(rules.endswith("notes."))   # no installation text here
        # The shells see app/fc/shell at /opt/fcsh.
        self.assertIn("/opt/fcsh/FC.md", rules)
        self.assertTrue(os.path.isfile(os.path.join(os.path.dirname(aishell.__file__), "shell", "FC.md")))
        self.assertIn("cat ~/AGENTS.md", rules)
        self.assertIn(Profile.objects.get(user=u).pubkey, rules)
        self.assertIn(NET[0], rules)
        self.assertNotIn("desk manual", rules)
        self.assertLess(len(rules), 2500)
        m = [x for x in lines if x.kind == "model"][0]
        self.assertEqual(kernel.line_json(m)["text"], "I look.")

    def test_the_installation_adds_its_text_to_the_rules(self):
        u = self.person("ana")
        with self.settings(FC_CHAT_CONTEXT="Read /library/x.md first."):
            self.turn(u, "hi")
        rules = self.fake.requests[-1]["messages"][0]["content"]
        self.assertTrue(rules.endswith("notes.\nRead /library/x.md first."), rules[-80:])

    def test_any_command_runs(self):
        u = self.person("ana")
        self.run_out["ls -l"] = ("exit 0", "total 0\n")
        self.fake.answers = [sh("ls -l"), "Empty."]
        lines = self.turn(u, "list the files")
        self.assertEqual([x.text for x in lines if x.kind == "run"], ["ran `ls -l`: exit 0\ntotal 0"])

    def test_a_desk_change_needs_no_second_call(self):
        u = self.person("ana")
        self.fake.answers = [sh("desk new text a b", text="Done."), "never asked"]
        self.turn(u, "make a note")
        self.assertEqual((self.runs(), len(self.fake.prompts)), (["desk new text a b"], 1))
        self.fake.answers = [sh("desk ls"), "Two windows."]
        self.turn(u, "what is on the desk?")
        self.assertEqual(len(self.fake.prompts), 3)

    def test_a_think_call_that_fails_fast_runs_once_more(self):
        u = self.person("ana")
        real, calls = self.fake.stream, []

        def flaky(op, timeout=20, **args):
            calls.append(op)
            if len(calls) == 1 or self.always:
                raise bridge.BridgeError("CES_ERROR_INTERNAL")
                yield {}        # a generator, as bridge.stream
            return (yield from real(op, timeout, **args))
        self.always = False
        self.fake.answers = ["Hi."]
        with mock.patch.object(bridge, "stream", flaky), self.assertLogs("fc.kernel", "WARNING") as logs:
            lines = kernel.say(Profile.objects.get(user=u), "hello")
        self.assertEqual((lines[-1].kind, lines[-1].text, len(calls)), ("model", "Hi.", 2))
        self.assertIn("CES_ERROR_INTERNAL after", logs.output[0])
        self.always, calls[:] = True, []
        with mock.patch.object(bridge, "stream", flaky), self.assertLogs("fc.kernel", "WARNING"):
            with self.assertRaises(kernel.ChatError):
                kernel.say(Profile.objects.get(user=u), "hello again")
        self.assertEqual(len(calls), 2)

    def test_a_turn_that_reaches_the_cap_says_so(self):
        u = self.person("ana")
        self.fake.answers = [sh("cesh query"), sh("cesh query"), "never asked"]
        with mock.patch.object(kernel, "MAX_CALLS", 2):
            lines = self.turn(u, "look twice")
        self.assertEqual([x.kind for x in lines], ["user", "desk", "model", "run", "model", "run", "note"])
        self.assertEqual(lines[-1].text, "The assistant stopped after 2 steps for this line. Write to it to go on.")

    def test_a_line_that_fails_stops_the_lines_after_it(self):
        u = self.person("ana")
        put = "cesh file put - a.lua <<END\nces.run()\nEND"
        self.run_out[put] = ("exit 1", "Error: create failed: CES_ERROR_INSUFFICIENT_BALANCE\n")
        self.fake.answers = [sh(put, "cesh compute launch a.lua", "cesh compute stat %PID%"), "No credit."]
        lines = self.turn(u, "make a program and launch it")
        self.assertEqual(self.runs(), [put])
        self.assertEqual([x.kind for x in lines], ["user", "desk", "model", "run", "run", "run", "model"])
        self.assertIn("CES_ERROR_INSUFFICIENT_BALANCE", lines[3].text)
        self.assertEqual([x.text for x in lines[4:6]], ["did not run, because `cesh file put - a.lua <<END` failed"] * 2)
        self.assertEqual([(m["role"], m.get("tool_call_id")) for m in self.fake.requests[-1]["messages"][-3:]],
                         [("tool", "call_1"), ("tool", "call_2"), ("tool", "call_3")])

    def test_a_last_line_that_fails_stops_nothing_more(self):
        u = self.person("ana")
        self.run_out["cesh compute stat 9"] = ("exit 1", "not found\n")
        self.fake.answers = [sh("cesh query", "cesh compute stat 9"), "Pid 9 is gone."]
        lines = self.turn(u, "is 9 there?")
        self.assertEqual(self.runs(), ["cesh query", "cesh compute stat 9"])
        self.assertEqual([x.kind for x in lines], ["user", "desk", "model", "run", "run", "model"])

    def test_a_line_that_does_not_run_says_why(self):
        u = self.person("ana")
        self.hub_reply["run"] = {"error": "the shell did not start"}
        self.fake.answers = [sh("cesh query", "ls"), "The shell is down."]
        lines = self.turn(u, "look")
        self.assertEqual([x.text for x in lines if x.kind == "run"],
                         ["`cesh query` did not run: the shell did not start",
                          "did not run, because `cesh query` failed"])

    def test_a_heredoc_is_part_of_its_line(self):
        u = self.person("ana")
        self.fake.answers = [sh("cesh file put - a.lua <<END\nlocal x = 1\n  ces.run()\nEND", text="I write it."),
                             "Done."]
        lines = self.turn(u, "make a program")
        self.assertEqual(self.runs(), ["cesh file put - a.lua <<END\nlocal x = 1\n  ces.run()\nEND"])
        m = [x for x in lines if x.kind == "model"][0]
        self.assertEqual(kernel.line_json(m)["text"], "I write it.")

    def test_a_command_goes_on_after_its_heredoc(self):
        u = self.person("ana")
        cmd = "cat > a.lua <<'END'\nlocal x = 1\nEND\nwc -l a.lua"
        self.fake.answers = [sh(cmd), "Done."]
        self.turn(u, "make a program")
        self.assertEqual(self.runs(), [cmd])

    def test_input_with_no_end_line_does_not_run(self):
        u = self.person("ana")
        self.fake.answers = [sh("cesh file put - a.lua <<END\nx = 1"), "I forgot the end."]
        lines = self.turn(u, "make a program")
        self.assertEqual([x.text for x in lines if x.kind == "run"],
                         ["`cesh file put - a.lua <<END` did not run: its input lines have no end line END"])
        self.assertEqual(self.runs(), [])

    def test_the_model_goes_on_from_a_line_that_the_person_ran(self):
        u = self.person("ana")
        self.fake.answers = [sh("desk new program game 7")]
        lines = self.turn(u, "", "$ cesh compute launch game.lua\nInstance:       7\n")
        self.assertEqual([x.kind for x in lines if x.kind != "desk"], ["shell", "model", "run"])
        said = [m["content"] for m in self.fake.requests[-1]["messages"] if m["role"] == "user"]
        self.assertEqual(len(said), 1)
        self.assertTrue(said[0].startswith("[site] In their shell, the person typed this"))   # only the site
        self.assertIn("Instance:       7", said[0])
        self.assertIn("ran `desk new program game 7`", lines[-1].text)
        with self.assertRaises(kernel.ChatError):
            self.turn(u, "", "  ")

    def test_the_api_takes_the_shell_with_no_line(self):
        u = self.person("ana")
        tok = self.login(u)
        self.fake.answers = ["It ran."]
        s, j = self.api("POST", "/api/chat", {"shell": "$ cesh query\nbalance 5\n"}, client=tok)
        self.assertEqual(s, 200, j)
        s, j = self.api("POST", "/api/chat", {}, client=tok)
        self.assertEqual((s, j["error"]), (400, "BAD_TEXT"))

    def test_a_bad_tool_call_gets_its_reason(self):
        u = self.person("ana")
        def call(i, name, arguments):
            return {"id": i, "type": "function", "function": {"name": name, "arguments": arguments}}
        self.fake.answers = [sh(call("x1", "python", "{}"), call("x2", "shell", "not json"),
                                call("x3", "shell", '{"cmd": "ls"}')), "Sorry."]
        lines = self.turn(u, "look")
        self.assertEqual([(x.call_id, x.text) for x in lines if x.kind == "run"],
                         [("x1", "did not run: there is no tool 'python': the one tool is shell"),
                          ("x2", "did not run: the arguments are not JSON"),
                          ("x3", "did not run: the arguments have no command")])
        self.assertEqual(self.runs(), [])
        self.assertEqual(lines[-1].text, "Sorry.")   # the model read the reasons

    def test_every_tool_call_gets_its_tool_message(self):
        u = self.person("ana")
        def call(i):
            return {"id": i, "type": "function", "function": {"name": "shell", "arguments": '{"command": "ls"}'}}
        lines = [ChatLine.objects.create(user=u, **kw) for kw in [
            dict(kind="user", text="hi"), dict(kind="note", text="a note"),
            dict(kind="model", text="I looked."),                  # an answer from before the tools
            dict(kind="run", text="ran `ls`: exit 0"),             # and its result
            dict(kind="model", text="", calls=[call("a"), call("b")], reasoning="r"),
            dict(kind="run", call_id="a", text="ran `ls`: exit 0"),
            dict(kind="desk", text="the desk now: x")]]
        self.assertEqual(kernel.messages(lines), [
            {"role": "user", "content": "hi\n\n[site] a note"},
            {"role": "assistant", "content": "I looked."},
            {"role": "user", "content": "[site] ran `ls`: exit 0"},
            {"role": "assistant", "content": "", "reasoning_content": "r", "tool_calls": [call("a"), call("b")]},
            {"role": "tool", "tool_call_id": "a", "content": "ran `ls`: exit 0"},
            {"role": "tool", "tool_call_id": "b", "content": "did not run"},
            {"role": "user", "content": "[site] the desk now: x"}])

    def test_the_commands_of_the_person_come_from_the_hub(self):
        u = self.person("ana")
        self.hub_reply["mine"] = {"ok": {"text": "$ ls\nnote.txt\n"}}
        self.fake.answers = ["You have a note."]
        lines = self.turn(u, "what is in my home?")
        self.assertEqual([x.kind for x in lines[:2]], ["shell", "user"])
        self.assertEqual(lines[0].text, "$ ls\nnote.txt")
        self.assertIn("note.txt", self.fake.prompts[-1])

    def test_the_shell_lines_come_before_the_line(self):
        u = self.person("ana")
        lines = self.turn(u, "what does it mean?", shell="$ cesh query\n{\"balance\":5}\n")
        self.assertEqual([x.kind for x in lines[:2]], ["shell", "user"])
        self.assertIn("In their shell, the person typed this", self.fake.prompts[-1])
        self.assertIn('{"balance":5}', self.fake.prompts[-1])
        s, j = self.api("GET", "/api/chat", client=self.login(u))
        self.assertNotIn("shell", [x["kind"] for x in j["lines"]])
        s, j = self.api("POST", "/api/chat", {"text": "and now?", "shell": "y" * 9000}, client=self.login(u))
        self.assertEqual(s, 200, j)
        kept = ChatLine.objects.filter(user=u, kind="shell").last().text
        self.assertTrue(kept.startswith("[... older lines]"))
        self.assertLessEqual(len(kept), kernel.SHELL_TYPED_MAX + 40)

    def test_the_hub_learns_the_key_and_server_of_a_shell(self):
        from django.conf import settings as st
        u = self.person("ana")
        p = Profile.objects.get(user=u)
        k = hub.key(st.SECRET_KEY)
        s, j = self.api("GET", "/api/hub", client=self.login(u))
        self.assertEqual(hub.parse(k, j["ticket"]), (u.pk, True))
        self.assertEqual(self.people, [(u.pk, p.sealed, NET[0])])
        s, j = self.api("GET", "/api/hub")
        self.assertEqual(hub.parse(k, j["ticket"]), (hub.VISITOR, False))
        with self.captureOnCommitCallbacks(execute=True):
            account.travel(p, NET[1])
        self.assertEqual(self.people[-1], (u.pk, p.sealed, NET[1]))


class Launch(TestCase):
    """The bridge's cesh socket client, against a fake bridge."""

    def bridge(self, answer):
        """A fake cesh socket that takes one request, writes "hi" on its
        stdout, and answers with `answer(conn)`. Returns (path, the request)."""
        import os
        import socket
        import tempfile
        import threading
        base = os.path.join(os.path.dirname(__file__), "..", "..", "tmp")
        os.makedirs(base, exist_ok=True)
        d = tempfile.mkdtemp(dir=base)
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        path = os.path.join(d, "cesh.sock")
        lsn = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        lsn.bind(path)
        lsn.listen(1)
        got = {}

        def serve():
            conn, _ = lsn.accept()
            data, fds, _, _ = socket.recv_fds(conn, 65536, 3)
            got["req"], got["fds"] = json.loads(data), len(fds)
            os.write(fds[1], b"hi")
            for fd in fds:
                os.close(fd)
            answer(conn)
            conn.close()
            lsn.close()
        t = threading.Thread(target=serve)
        t.start()
        self.addCleanup(t.join)
        return path, got

    def test_the_command_gets_the_descriptors_and_the_caller_gets_its_result(self):
        import os
        import socket
        keep = []

        def ok(conn):
            a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
            socket.send_fds(conn, [b"ok"], [b.fileno()])
            b.close()
            a.send(b"exit 0")
            keep.append(a)
        path, got = self.bridge(ok)
        r, w = os.pipe()
        nul = os.open(os.devnull, os.O_RDONLY)
        conn = launch.launch(path, "sealed1", "h:1", ["query"], [nul, w, w])
        os.close(w)
        os.close(nul)
        self.assertEqual(os.read(r, 10), b"hi")
        os.close(r)
        self.assertEqual(launch.result(conn, timeout=5), "exit 0")
        self.assertEqual(got, {"req": {"sealed": "sealed1", "server": "h:1", "args": ["query"]}, "fds": 3})
        for a in keep:
            a.close()

    def test_a_refusal_is_an_error(self):
        import os
        path, got = self.bridge(lambda conn: conn.send(b"error the key does not open"))
        r, w = os.pipe()
        with self.assertRaisesMessage(launch.LaunchError, "the key does not open"):
            launch.launch(path, "bad", "", ["query"], [w, w, w])
        os.close(r)
        os.close(w)

    def test_no_bridge(self):
        with self.assertRaises(launch.LaunchError):
            launch.launch("/nonexistent/cesh.sock", "s", "", ["query"], [0, 1, 2])

    def test_the_input_of_a_command_is_a_pipe_that_holds_it(self):
        import os
        r = launch.stdin_pipe(b"line 1\nline 2\n")
        self.assertEqual(os.read(r, 100), b"line 1\nline 2\n")
        self.assertEqual(os.read(r, 100), b"")
        os.close(r)
        r = launch.stdin_pipe(b"x" * launch.MAX_STDIN)    # the most fits, with no reader yet
        self.assertEqual(len(b"".join(iter(lambda: os.read(r, 65536), b""))), launch.MAX_STDIN)
        os.close(r)
        with self.assertRaisesMessage(launch.LaunchError, "the input is too big"):
            launch.stdin_pipe(b"x" * (launch.MAX_STDIN + 1))


class Vs1Screen(TestCase):
    """The hub's copy of a program window's screen, and the messages that the
    hub sends a program as the person."""

    @staticmethod
    def frame(runs):
        import struct
        body = b"".join(struct.pack("<HHH", x, y, len(cells)) + b"".join(
            struct.pack("<I", c) + fg.to_bytes(3, "little") + bg.to_bytes(3, "little") for c, fg, bg in cells)
            for x, y, cells in runs)
        return bytes([2]) + struct.pack("<I", len(body)) + body

    def test_it_reads_the_frames_of_a_program(self):
        import struct
        sc = vs1screen.Screen()
        mode = bytes([1]) + struct.pack("<I", 5) + bytes([1]) + struct.pack("<HH", 4, 2)
        title = bytes([3]) + struct.pack("<I", 2) + b"hi"
        data = mode + self.frame([(1, 0, [(ord("a"), 1, 2), (0x2588, 3, 4)]), (0, 1, [(ord("z"), 5, 6)])]) + title
        for i in range(len(data)):          # a message counts only once it is whole
            sc.feed(data[i:i + 1])
        self.assertEqual((sc.cols, sc.rows, sc.title), (4, 2, "hi"))
        self.assertEqual(sc.text(), " a\u2588\nz")
        self.assertEqual(sc.text(2, 1, 3, 1), "a\u2588")
        self.assertEqual(sc.cells[1], (ord("a"), 1, 2))
        sc.feed(self.frame([(3, 0, [(ord("x"), 0, 0), (ord("y"), 0, 0)]), (0, 1, [(0x110000, 0, 0)])]))
        self.assertEqual(sc.text(), " a\u2588x\n\ufffd")
        sc.feed(bytes([2]) + struct.pack("<I", vs1screen.MAX_MESSAGE + 1))
        sc.feed(mode)
        self.assertIn("a message of", sc.broken)
        self.assertEqual(sc.text(), " a\u2588x\n\ufffd")

    def test_the_messages_of_a_viewer(self):
        import struct
        self.assertEqual(vs1screen.key(vs1screen.DOWN, "ArrowLeft", 7),
                         bytes([129]) + struct.pack("<I", 15) + bytes([1, 0]) + struct.pack("<I", 7) + b"ArrowLeft")
        big = "\u00e9" * 3000           # 6000 bytes: two messages, cut between characters
        data, parts, at = vs1screen.text(big), [], 0
        while at < len(data):
            n = struct.unpack_from("<I", data, at + 1)[0]
            self.assertEqual(data[at], 132)
            self.assertLessEqual(n, vs1screen.MAX_TEXT)
            parts.append(data[at + 5:at + 5 + n].decode())
            at += 5 + n
        self.assertEqual(("".join(parts), len(parts)), (big, 2))
        for name, want in [("Enter", b"\r"), ("Ctrl+c", b"\x03"), ("ArrowUp", b"\x1b[A"), ("q", b"q"),
                           ("Space", b" "), ("NoSuchKey", None)]:
            self.assertEqual(vs1screen.terminal_key(name), want, name)
        self.assertEqual(vs1screen.vs1_key("Space"), " ")

    def test_the_shell_log_keeps_plain_text(self):
        log = hub.ShellLog()
        log.add("$ cesh query\n\x1b[31mred\x1b[0m\r\n\x1b]0;title\x07ok\r\n")
        self.assertEqual(log.tail(100), "$ cesh query\nred\nok\n")
        self.assertEqual(log.tail(3), "ok\n")
        log.add("x" * hub.LOG_MAX)
        self.assertEqual(len(log.text), hub.LOG_MAX)


class HubShell(TestCase):
    """A person's shell (fc/shellhub.py): bash on a pty, as a plain process
    (Local), with a fake cesh socket."""

    BASH = None         # ptyhost.py's bash, with its rcfile (fc/shell/bashrc)

    def setUp(self):
        import os
        import tempfile
        base = os.path.join(os.path.dirname(__file__), "..", "..", "tmp")
        os.makedirs(base, exist_ok=True)
        self.dir = tempfile.mkdtemp(dir=base)
        self.addCleanup(lambda: __import__("shutil").rmtree(self.dir, ignore_errors=True))
        self.k = hub.key("test-secret")

    def local(self):
        import os
        launcher = shellhub.Local(os.path.join(self.dir, "run"), os.path.join(self.dir, "homes"), self.BASH)
        self.addCleanup(lambda: [launcher.stop(u) for u in list(launcher.procs)])
        return launcher

    async def start_hub(self, cesh=None, launcher=None):
        """A hub with real shells. Returns its task, its port, the Hub and
        the launcher."""
        import asyncio
        import os
        launcher = launcher or self.local()
        loop = asyncio.get_running_loop()
        ready, got = loop.create_future(), loop.create_future()
        task = asyncio.create_task(hub.serve(self.k, os.path.join(self.dir, "hub.sock"), "127.0.0.1", 0,
                                             ready=ready, cesh=cesh, launcher=launcher, run=launcher.run,
                                             hub_ready=got))
        return task, await ready, await got, launcher

    async def site(self, msg):
        """A message from the site to the hub, and the reply to an "ask"."""
        import asyncio
        import os
        r, w = await asyncio.open_unix_connection(os.path.join(self.dir, "hub.sock"))
        w.write((json.dumps(msg) + "\n").encode())
        await w.drain()
        reply = await asyncio.wait_for(r.readline(), 15) if "ask" in msg else b""
        w.close()
        await asyncio.sleep(0.05)
        return json.loads(reply) if reply else None

    async def shell_page(self, port, cols=90, rows=20):
        from websockets.asyncio.client import connect
        page = await connect(f"ws://127.0.0.1:{port}/hub?t=" + hub.ticket(self.k, 5, shell=True))
        await page.send(json.dumps({"term": {"open": True, "cols": cols, "rows": rows}}))
        return page

    async def until(self, page, want, timeout=10):
        """Read the shell's messages until `want`, a state or a text in its
        output. Returns (the output, the last message)."""
        import asyncio
        out = ""
        while True:
            m = json.loads(await asyncio.wait_for(page.recv(), timeout))["term"]
            out += m.get("out", "")
            if (m.get("state") == want) if want in (shellhub.STARTING, shellhub.LIVE, shellhub.ENDED) \
                    else want in out:
                return out, m

    async def type(self, page, text):
        await page.send(json.dumps({"term": {"in": text}}))

    def test_the_shell_is_a_real_bash_that_ends_and_starts_again(self):
        import asyncio

        async def go():
            task, port, h, launcher = await self.start_hub()
            page = await self.shell_page(port)
            self.assertEqual((await self.until(page, "starting"))[1], {"state": "starting"})
            await self.until(page, "live")
            await self.type(page, "stty size; echo mark$((1+2))\r")
            out, _ = await self.until(page, "mark3")
            self.assertIn("20 90", out)
            await self.type(page, "exit 7\r")
            self.assertEqual((await self.until(page, "ended"))[1], {"state": "ended", "why": "exit 7"})
            for _ in range(100):                    # its process ends a moment later
                if not launcher.running():
                    break
                await asyncio.sleep(0.02)
            self.assertEqual(launcher.running(), [])
            await page.send(json.dumps({"term": {"restart": True, "cols": 80, "rows": 24}}))
            await self.until(page, "live")
            await self.type(page, "echo again$((2*3))\r")
            await self.until(page, "again6")
            await page.close()
            task.cancel()
        asyncio.run(go())

    def test_cesh_and_desk_in_the_shell(self):
        import asyncio
        import os
        seen = {}

        def script(req, fds, conn):
            seen.setdefault("reqs", []).append(req)
            if req["args"] == ["dial", "5"]:        # it runs until the hub stops it
                conn.settimeout(10)
                try:
                    seen["stopped"] = conn.recv(10) == b""
                except OSError:
                    seen["stopped"] = False
            else:
                os.write(fds[1], b"hello\n")
                conn.send(b"exit 3")
            for fd in fds:
                os.close(fd)
            conn.close()
        cesh_sock = os.path.join(self.dir, "cesh.sock")
        self.fake_bridge(cesh_sock, script)

        async def go():
            task, port, h, _ = await self.start_hub(cesh=cesh_sock)

            async def desk(uid, op):
                return {"error": "no op bad"} if op == "bad" else {"out": f"did {op} for {uid}"}
            h.shells.desk = desk
            page = await self.shell_page(port)
            await self.until(page, "live")
            await self.type(page, "cesh query @0; echo code=$?\r")
            out, _ = await self.until(page, "code=1")
            self.assertIn("cesh: cesh runs while you have the site open", out)
            await self.site({"person": {"uid": 5, "sealed": "sealed7", "server": "h:9"}})
            await self.type(page, "cesh query @0; echo code=$?\r")
            out, _ = await self.until(page, "code=3")
            self.assertIn("hello", out)
            await self.type(page, "cesh chat; echo code=$?\r")
            out, _ = await self.until(page, "code=1")
            self.assertIn("chat is disabled here", out)
            await self.type(page, "cesh dial 5\r")
            for _ in range(100):
                if len(seen.get("reqs", [])) == 2:
                    break
                await asyncio.sleep(0.05)
            await self.type(page, "\x03")
            await self.type(page, "echo code=$?\r")
            await self.until(page, "code=130")
            await self.type(page, "desk new text a 'b c'; echo code=$?\r")
            out, _ = await self.until(page, "code=0")
            self.assertIn("did new text a 'b c' for 5", out)
            await self.type(page, "desk bad; echo code=$?\r")
            out, _ = await self.until(page, "code=1")
            self.assertIn("desk: no op bad", out)
            await page.close()
            task.cancel()
        asyncio.run(go())
        self.assertEqual(seen["reqs"], [{"sealed": "sealed7", "server": "h:9", "args": ["query", "@0"]},
                                        {"sealed": "sealed7", "server": "h:9", "args": ["dial", "5"]}])
        self.assertTrue(seen["stopped"])

    def test_a_job_of_the_shell_wakes_the_model_through_the_hub(self):
        import asyncio

        async def go():
            task, port, h, launcher = await self.start_hub()
            asked = []

            async def to_site(path, msg, timeout):
                asked.append((path, msg))
                return {"error": "BUSY"} if len(asked) == 1 else {"ok": True}
            h.to_site = to_site

            async def run(line, timeout=20):
                return (await self.site({"ask": {"run": {"uid": 5, "line": line, "timeout": timeout}}}))["ok"]
            with mock.patch.object(hub, "WAKE_AGAIN", 0.05):
                r = await run("job run --wake 'echo hi; exit 2'")
                self.assertEqual(r["result"], "exit 0")
                self.assertRegex(r["out"], r"^\[1\] \d+\n$")
                for _ in range(100):
                    if len(asked) == 2:
                        break
                    await asyncio.sleep(0.05)
            self.assertEqual(asked[0], asked[1])
            path, msg = asked[1]
            self.assertEqual((path, msg["uid"]), ("/api/hub/wake", 5))
            self.assertEqual(msg["text"], "job 1 ended with exit 2 after 0s: `echo hi; exit 2`.\nIts last lines:\nhi")
            ls = (await run("job ls"))["out"]
            self.assertRegex(ls, r"^\[1\] exit 2 +0s  echo hi; exit 2 \(wakes at the end\)\n$")
            self.assertEqual((await run("job tail 1"))["out"], "hi\n")
            self.assertEqual((await run("job tail 9"))["result"], "exit 1")
            self.assertEqual((await run("job run --nope x"))["result"], "exit 2")
            task.cancel()
        asyncio.run(go())

    def test_a_wake_waits_while_the_hub_is_away(self):
        import asyncio

        async def go():
            task, port, h, launcher = await self.start_hub()
            r = await self.site({"ask": {"run": {"uid": 5, "line": "job run --wake 'sleep 1; echo late'", "timeout": 20}}})
            self.assertEqual(r["ok"]["result"], "exit 0")
            task.cancel()                           # the hub goes while the job runs
            await asyncio.sleep(1.5)                # the job ends while the hub is away
            task, port, h, _ = await self.start_hub(launcher=launcher)
            asked = []

            async def to_site(path, msg, timeout):
                asked.append(msg["text"])
                return {"ok": True}
            h.to_site = to_site
            for _ in range(100):                    # ptyhost reconnects and sends it
                if asked:
                    break
                await asyncio.sleep(0.05)
            self.assertEqual(asked, ["job 1 ended with exit 0 after 1s: `sleep 1; echo late`.\nIts last lines:\nlate"])
            task.cancel()
        asyncio.run(go())

    def test_the_model_types_its_commands_at_the_prompt(self):
        import asyncio
        import os

        def script(req, fds, conn):
            os.write(fds[1], b"hello\n")
            conn.send(b"exit 3")
            for fd in fds:
                os.close(fd)
            conn.close()
        cesh_sock = os.path.join(self.dir, "cesh.sock")
        self.fake_bridge(cesh_sock, script)

        async def go():
            task, port, h, launcher = await self.start_hub(cesh=cesh_sock)

            async def desk(uid, op):
                return {"out": f"did {op}"}
            h.shells.desk = desk

            async def run(line, timeout=20):
                return (await self.site({"ask": {"run": {"uid": 5, "line": line, "timeout": timeout}}}))
            self.assertEqual(await run("echo a$((1+1)); (exit 3)"), {"ok": {"out": "a2\n", "result": "exit 3"}})
            self.assertEqual(launcher.running(), [5])
            self.assertEqual((await run("cat <<END\nx  y\nEND"))["ok"], {"out": "x  y\n", "result": "exit 0"})
            self.assertEqual((await run("sleep 5; echo no", timeout=1))["ok"]["result"], "timeout")
            self.assertEqual((await run("cd /tmp"))["ok"]["result"], "exit 0")
            self.assertEqual((await run("pwd; cd"))["ok"]["out"], "/tmp\n")
            await self.site({"person": {"uid": 5, "sealed": "s", "server": ""}})
            r = (await run("cesh query; echo code=$?; desk ls"))["ok"]
            self.assertEqual(r, {"out": "hello\ncode=3\ndid ls\n", "result": "exit 0"})
            self.assertEqual((await run("echo ("))["ok"]["result"], "exit 2")
            for bad in (7, "  "):
                self.assertEqual(await self.site({"ask": {"run": {"uid": 5, "line": bad}}}), {"error": "a bad request"})
            page = await self.shell_page(port)
            await self.until(page, "live")
            self.assertEqual((await run("echo b$((2+2))"))["ok"]["out"], "b4\n")
            out, _ = await self.until(page, "b4\r\n")
            self.assertIn("echo b$((2+2))", out)
            self.assertNotIn("\x1b]133;", out)
            await self.type(page, "echo part")
            await self.until(page, "echo part")
            self.assertEqual((await run("echo c$((3+3))"))["ok"]["out"], "c6\n")
            await self.type(page, "ial\r")
            await self.until(page, "partial\r\n")
            await self.type(page, "echo own$((4+4))\r")
            await self.until(page, "own8\r\n")
            await asyncio.sleep(0.2)
            mine = (await self.site({"ask": {"mine": {"uid": 5}}}))["ok"]["text"]
            self.assertEqual(mine, "$ echo partial\npartial\n$ echo own$((4+4))\nown8\n")
            self.assertEqual((await self.site({"ask": {"mine": {"uid": 5}}}))["ok"]["text"], "")   # read once
            await self.type(page, "sleep 3\r")
            await asyncio.sleep(0.3)
            self.assertEqual((await run("echo d", timeout=1))["ok"]["result"],
                             "error the shell is busy: a program runs in it")
            await page.close()
            task.cancel()
        asyncio.run(go())

    def test_cesh_in_the_shell_reads_and_writes_the_files_of_the_shell(self):
        import asyncio
        import os
        seen = {}

        def script(req, fds, conn):
            if req["args"][:2] == ["file", "put"]:
                seen["put"] = (req["args"], os.read(fds[0], 100))
            elif req["args"][:2] == ["file", "get"]:
                seen["get"] = req["args"]
                os.write(fds[1], b"from ces\n")
            else:
                seen["call"] = req["args"]
            conn.send(b"exit 0")
            for fd in fds:
                os.close(fd)
            conn.close()
        cesh_sock = os.path.join(self.dir, "cesh.sock")
        self.fake_bridge(cesh_sock, script)

        async def go():
            task, port, h, _ = await self.start_hub(cesh=cesh_sock)
            await self.site({"person": {"uid": 5, "sealed": "s", "server": ""}})
            line = ("echo mine > a.txt; printf 'mem' > m; cesh file put a.txt r.txt --deposit 9; "
                    "cesh file get r.txt b.txt && cat b.txt; cesh compute call 7 0 --in file:m; "
                    "cesh file put nope r.txt; echo code=$?")
            r = (await self.site({"ask": {"run": {"uid": 5, "line": line, "timeout": 20}}}))["ok"]
            self.assertEqual(r["out"], "from ces\ncesh: nope: No such file or directory\ncode=1\n")
            task.cancel()
        asyncio.run(go())
        self.assertEqual(seen["put"], (["file", "put", "-", "r.txt", "--deposit", "9"], b"mine\n"))
        self.assertEqual(seen["get"], ["file", "get", "r.txt", "-"])
        self.assertEqual(seen["call"], ["compute", "call", "7", "0", "--in", "hex:6d656d"])

    def test_a_shell_outlives_its_page_and_the_hub(self):
        import asyncio

        async def go():
            task, port, h, launcher = await self.start_hub()
            page = await self.shell_page(port)
            await self.until(page, "live")
            await self.type(page, "x=$((5*5)); echo first$x\r")
            await self.until(page, "first25")
            await page.close()
            await asyncio.sleep(0.1)
            self.assertTrue(h.shells.alive(5))
            task.cancel()                           # the hub goes; bash stays
            await asyncio.sleep(0.1)
            self.assertEqual(launcher.running(), [5])
            task, port, h, _ = await self.start_hub(launcher=launcher)
            page = await self.shell_page(port)
            await self.until(page, "first25")
            await self.type(page, "echo still$x\r")
            await self.until(page, "still25")
            await page.close()
            task.cancel()
        asyncio.run(go())

    def test_idle_shells_stop_and_a_full_site_makes_room(self):
        import asyncio
        sent = []

        async def go():
            launcher = self.local()

            async def send(uid, msg):
                sent.append((uid, msg))
            shells = shellhub.Shells(launcher, launcher.run, send, lambda uid, text: None, None, None,
                                     idle=0.3, cap=1)
            await shells.attach(1, 80, 24)
            for _ in range(100):
                if (1, {"term": {"state": "live"}}) in sent:
                    break
                await asyncio.sleep(0.05)
            with self.assertRaises(shellhub.ShellError):
                await shells.start(2)               # the one shell has a page
            shells.detach(1)
            await shells.attach(2, 80, 24)          # it stops the shell with no page
            self.assertFalse(shells.alive(1))
            self.assertEqual(shells.shells[1].why, "the site needed the room")
            self.assertEqual(launcher.running(), [2])
            shells.detach(2)
            with mock.patch.object(shellhub, "GC_EVERY", 0.05):
                gc = asyncio.create_task(shells.gc())
                for _ in range(100):
                    if not shells.alive(2):
                        break
                    await asyncio.sleep(0.05)
                gc.cancel()
            self.assertEqual(launcher.running(), [])
            shells.close()
        asyncio.run(go())

    def fake_bridge(self, path, script):
        """A cesh socket that runs `script(req, fds, conn_end)` in a thread for
        every request, with fds the command's 3 descriptors (the pty)."""
        import socket
        import threading
        lsn = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        lsn.bind(path)
        lsn.listen(4)
        lsn.settimeout(10)

        def serve():
            while True:
                try:
                    conn, _ = lsn.accept()
                except OSError:
                    return
                data, fds, _, _ = socket.recv_fds(conn, 65536, 3)
                a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
                socket.send_fds(conn, [b"ok"], [b.fileno()])
                b.close()
                conn.close()
                threading.Thread(target=script, args=(json.loads(data), fds, a), daemon=True).start()
        threading.Thread(target=serve, daemon=True).start()
        self.addCleanup(lsn.close)

    def test_a_program_window_dials_its_program(self):
        import asyncio
        import os
        import socket
        import threading
        from websockets.asyncio.client import connect
        seen = {}
        path = os.path.join(self.dir, "cesh.sock")
        lsn = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        lsn.bind(path)
        lsn.listen(4)
        lsn.settimeout(10)

        def serve():
            while True:
                try:
                    conn, _ = lsn.accept()
                except OSError:
                    return
                seen["req"] = json.loads(conn.recv(65536))
                mine, program = socket.socketpair()
                socket.send_fds(conn, [b"ok"], [mine.fileno()])
                mine.close()
                conn.close()
                program.sendall(b"\x01\xff\x00HI")     # raw bytes, as they are
                seen["typed"] = program.recv(100)
                program.sendall(b"!" + seen["typed"])
                program.close()
        threading.Thread(target=serve, daemon=True).start()
        self.addCleanup(lsn.close)
        k = hub.key("test-secret")

        async def go():
            sock = os.path.join(self.dir, "hub.sock")
            ready = asyncio.get_running_loop().create_future()
            task = asyncio.create_task(hub.serve(k, sock, "127.0.0.1", 0, ready=ready, cesh=path))
            port = await ready
            r, w = await asyncio.open_unix_connection(sock)
            w.write((json.dumps({"person": {"uid": 5, "sealed": "sealed7", "server": "h:9"}}) + "\n").encode())
            await w.drain()
            w.close()
            await asyncio.sleep(0.05)
            page = await connect(f"ws://127.0.0.1:{port}/hub?t=" + hub.ticket(k, 5, shell=True))

            async def recv():
                m = await asyncio.wait_for(page.recv(), 5)
                return m if isinstance(m, bytes) else json.loads(m)["term"]
            head = bytes([4]) + b"game"
            await page.send(json.dumps({"term": {"dial": {"pid": "x", "server": "h:1"}, "w": "game"}}))
            self.assertEqual(await recv(), {"end": "error give the pid of a program and its server", "w": "game"})
            await page.send(json.dumps({"term": {"dial": {"pid": 7, "server": "ces.example:55000"}, "w": "game"}}))
            self.assertEqual(await recv(), {"live": True, "w": "game"})
            self.assertEqual(await recv(), head + b"\x01\xff\x00HI")
            await page.send(bytes([5]) + b"other" + b"x")
            await page.send(bytes([5]) + b"shell" + b"x")
            await page.send(head + b"\x81k\xff")
            self.assertEqual(await recv(), head + b"!\x81k\xff")
            self.assertEqual(await recv(), {"end": "closed", "w": "game"})
            await page.close()
            task.cancel()

        asyncio.run(go())
        self.assertEqual(seen["req"], {"sealed": "sealed7", "server": "ces.example:55000", "dial": 7})
        self.assertEqual(seen["typed"], b"\x81k\xff")

    def test_the_model_sees_and_types_through_the_hub(self):
        import asyncio
        import os
        import socket
        import struct
        import threading
        from websockets.asyncio.client import connect
        seen = {}
        path = os.path.join(self.dir, "cesh.sock")
        lsn = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        lsn.bind(path)
        lsn.listen(4)
        lsn.settimeout(10)
        screen = (bytes([1]) + struct.pack("<I", 5) + bytes([1]) + struct.pack("<HH", 5, 1)
                  + Vs1Screen.frame([(0, 0, [(ord(c), 0xffffff, 0) for c in "hey"])])
                  + bytes([3]) + struct.pack("<I", 4) + b"game")

        def messages(data):
            out, at = [], 0
            while len(data) - at >= 5:
                n = struct.unpack_from("<I", data, at + 1)[0]
                if len(data) - at - 5 < n:
                    break
                out.append((data[at], data[at + 5:at + 5 + n]))
                at += 5 + n
            return out

        def program(sock):
            sock.sendall(screen)
            sock.settimeout(5)
            got = b""
            while len(messages(got)) < 5:       # a key down, its up, a text, a press and a release
                got += sock.recv(4096)
            seen["program"] = messages(got)
            sock.close()

        def serve():
            while True:
                try:
                    conn, _ = lsn.accept()
                except OSError:
                    return
                data, fds, _, _ = socket.recv_fds(conn, 65536, 3)
                mine, other = socket.socketpair()
                socket.send_fds(conn, [b"ok"], [mine.fileno()])
                mine.close()
                threading.Thread(target=program, args=(other,), daemon=True).start()
                conn.close()
        threading.Thread(target=serve, daemon=True).start()
        self.addCleanup(lsn.close)
        k = hub.key("test-secret")

        async def go():
            task, port, h, _ = await self.start_hub(cesh=path)

            async def ask(what, **a):
                return await self.site({"ask": {what: {"uid": 5, **a}}})
            await self.site({"person": {"uid": 5, "sealed": "sealed7", "server": "h:9"}})
            self.assertEqual(await ask("look", w="game"), {"error": "the person has no page open"})
            page = await connect(f"ws://127.0.0.1:{port}/hub?t=" + hub.ticket(k, 5, shell=True))

            async def recv():
                m = await asyncio.wait_for(page.recv(), 5)
                return m if isinstance(m, bytes) else json.loads(m)["term"]
            self.assertEqual(await ask("look", w="game"), {"error": "the window game is not on a program now"})
            await page.send(json.dumps({"term": {"dial": {"pid": 7, "server": "h:1"}, "w": "game"}}))
            self.assertEqual(await recv(), {"live": True, "w": "game"})
            self.assertEqual(await recv(), bytes([4]) + b"game" + screen)
            self.assertEqual(await ask("look", w="game", rect=None),
                             {"ok": {"title": "game", "cols": 5, "rows": 1, "text": "hey"}})
            self.assertEqual((await ask("look", w="game", rect=[2, 1, 3, 1]))["ok"]["text"], "ey")
            self.assertEqual(await ask("press", w="game", keys=[["Space", 20]]), {"ok": {}})
            self.assertEqual(await ask("type", w="game", text="hi"), {"ok": {}})
            self.assertEqual(await ask("click", w="game", x=0, y=2), {"error": "a bad request"})
            self.assertEqual(await ask("click", w="game", x=3, y=2, button=0), {"ok": {}})
            self.assertEqual(await recv(), {"end": "closed", "w": "game"})
            self.assertEqual(await ask("start", line="echo x"), {"error": "the person's shell does not run now"})
            await page.send(json.dumps({"term": {"open": True, "cols": 90, "rows": 20}}))
            await self.until(page, "live")
            self.assertEqual(await ask("start", line="echo ai$((2+2))"), {"ok": {}})
            await self.until(page, "ai4")
            self.assertEqual(await ask("press", w="shell", keys=[["NoSuchKey", 0]]),
                             {"error": "no key NoSuchKey in a terminal"})
            self.assertEqual(await ask("type", w="shell", text="echo typed$((3+3))"), {"ok": {}})
            self.assertEqual(await ask("press", w="shell", keys=[["Enter", 0]]), {"ok": {}})
            await self.until(page, "typed6")
            tail = (await ask("tail", n=1000))["ok"]["text"]
            self.assertIn("ai4", tail)
            self.assertIn("typed6", tail)
            self.assertEqual(await ask("look", w="shell"), {"error": "the shell has no screen, and tail reads it"})
            await page.close()
            task.cancel()

        asyncio.run(go())
        (k1, down), (k2, up), (k3, text), (k4, press), (k5, release) = seen["program"]
        # [u8 action][u8 button][u16 x][u16 y][u32 time], with x and y from 0.
        self.assertEqual((k4, press[:6]), (130, bytes([1, 0]) + struct.pack("<HH", 2, 1)))
        self.assertEqual((k5, release[:6]), (130, bytes([2, 0]) + struct.pack("<HH", 2, 1)))
        self.assertEqual((k1, down[0], down[6:]), (129, 1, b" "))
        self.assertEqual((k2, up[0], up[6:]), (129, 2, b" "))
        self.assertEqual(struct.unpack_from("<I", up, 2)[0] - struct.unpack_from("<I", down, 2)[0], 20)
        self.assertEqual((k3, text), (132, b"hi"))

class Jobs(TestCase):
    """A shell's jobs (fc/shell/jobs.py), in a loop as ptyhost runs them."""

    def setUp(self):
        import os
        import selectors
        import tempfile
        from .shell import jobs
        base = os.path.join(os.path.dirname(__file__), "..", "..", "tmp")
        os.makedirs(base, exist_ok=True)
        self.home = tempfile.mkdtemp(dir=base)
        self.addCleanup(lambda: __import__("shutil").rmtree(self.home, ignore_errors=True))
        self.sel = selectors.DefaultSelector()
        self.wakes = []
        self.jobs = jobs.Jobs(self.home, self.sel, self.wakes.append)
        self.addCleanup(self.jobs.close)

    def loop(self, until, secs=10):
        """ptyhost's loop, until `until()` is true."""
        import time
        from .shell import jobs
        end = time.monotonic() + secs
        while not until():
            self.assertLess(time.monotonic(), end, "the jobs took too long")
            for key, _ in self.sel.select(0.05):
                if isinstance(key.data, jobs.Job):
                    self.jobs.readable(key.data)
                elif isinstance(key.data, jobs.Jobs):
                    key.data.accept()
            self.jobs.check()

    def run_job(self, cmd, **kw):
        r = self.jobs.request({"run": dict(cmd=cmd, **kw)})
        self.assertIn("ok", r, r)
        return self.jobs.jobs[r["ok"]["id"]]

    def test_a_job_wakes_at_its_end_or_at_a_line(self):
        j = self.run_job("echo one; sleep 0.2; echo ready now; sleep 0.3; echo ready again; echo two", until="^ready")
        self.loop(lambda: self.wakes)
        self.assertRegex(self.wakes[0], r"^job 1 printed a line that matches /\^ready/ after 0s, and it still runs: "
                                        r"`echo one; .*`\.\nIts last lines:\none\nready now$")
        self.loop(lambda: j.code is not None)
        self.assertEqual(len(self.wakes), 1)
        self.assertEqual(j.state(), "exit 0")
        with open(j.log) as f:
            self.assertEqual(f.read(), "one\nready now\nready again\ntwo\n")
        self.wakes.clear()
        self.run_job("echo x; echo y >&2; exit 3", wake=True)
        self.run_job("echo z", until="never")
        self.run_job("true")
        self.loop(lambda: len(self.wakes) == 2 and all(x.code is not None for x in self.jobs.jobs.values()))
        self.assertEqual(sorted(self.wakes), [
            "job 2 ended with exit 3 after 0s: `echo x; echo y >&2; exit 3`.\nIts last lines:\nx\ny",
            "job 3 ended with exit 0 after 0s: `echo z`.\nIts last lines:\nz"])
        ls = self.jobs.request({"ls": {}})["ok"]
        self.assertEqual([(x["id"], x["state"], x["wake"]) for x in ls],
                         [(1, "exit 0", True), (2, "exit 3", True), (3, "exit 0", True), (4, "exit 0", False)])

    def test_a_job_ends_when_its_process_ends(self):
        import time
        j = self.run_job("sleep 30 & echo started", wake=True)
        t0 = time.monotonic()
        self.loop(lambda: self.wakes)
        self.assertLess(time.monotonic() - t0, 3)
        self.assertTrue(self.wakes[0].endswith("Its last lines:\nstarted"), self.wakes[0])
        self.assertEqual(j.state(), "exit 0")
        k = self.run_job("sleep 30", wake=True)
        self.assertEqual(self.jobs.request({"kill": {"id": k.n}})["ok"]["state"], "running")
        self.loop(lambda: k.code is not None)
        self.assertEqual(k.state(), "signal 15")
        self.assertIn("ended with signal 15", self.wakes[-1])
        self.assertIn("It printed nothing.", self.wakes[-1])
        self.assertEqual(self.jobs.request({"kill": {"id": 99}}), {"error": "no such job"})

    def test_the_limits_of_the_jobs(self):
        import os
        from .shell import jobs
        for _ in range(jobs.MAX_LIVE):
            self.run_job("sleep 30")
        self.assertIn("jobs run already", self.jobs.request({"run": {"cmd": "true"}})["error"])
        for j in self.jobs.live():
            self.jobs.request({"kill": {"id": j.n}})
        self.loop(lambda: not self.jobs.live())
        self.assertIn("a bad pattern", self.jobs.request({"run": {"cmd": "true", "until": "("}})["error"])
        self.assertEqual(self.jobs.request({"run": {"cmd": " "}}), {"error": "give a command"})
        self.assertEqual(self.jobs.request({"nope": {}}), {"error": "no op nope"})
        with mock.patch.object(jobs, "MAX_LOG", 64):
            j = self.run_job("for i in $(seq 1 40); do echo line$i; done")
            self.loop(lambda: j.code is not None and j.eof)
        self.assertTrue(os.path.exists(j.log + ".1"))
        self.assertLessEqual(os.path.getsize(j.log), 64)
        with open(j.log) as f:
            self.assertTrue(f.read().endswith("line40\n"))


class ShellUnit(TestCase):
    """fc-shelld: a shell unit's settings, and the root that it sees."""

    def test_a_unit_has_its_own_root_and_no_network(self):
        from .shell import shelld
        args = shelld.command(42)
        props = [args[i + 1] for i, a in enumerate(args) if a == "-p"]
        for p in ("RootDirectory=/var/lib/fc-shells-root", "PrivateNetwork=yes", "DynamicUser=yes",
                  "RestrictAddressFamilies=AF_UNIX", "BindPaths=/var/lib/private/fc-shells/u42:/h/u42",
                  "BindPaths=/run/fc-shells/u42:/run/fcsh", "WorkingDirectory=/h/u42", "User=fcsh-u42",
                  "TemporaryFileSystem=/h:size=1M /var:size=16M", "ReadOnlyPaths=+/tmp",
                  "BindReadOnlyPaths=-/opt/fc/library:/library",
                  "SystemCallFilter=@system-service", "CapabilityBoundingSet="):
            self.assertIn(p, props)
        self.assertEqual(args[:2], ["systemd-run", "--unit=fcsh-u42.service"])
        self.assertIn("HOME=/h/u42", args)
        self.assertIn("TMPDIR=/h/u42/.tmp", args)
        self.assertEqual(args[-2:], ["/usr/bin/python3", "/opt/fcsh/ptyhost.py"])
        self.assertEqual(shelld.handle({"start": "42; rm -rf /"}, None), {"error": "give a user id"})
        self.assertEqual(shelld.handle({"stop": True}, None), {"error": "give a user id"})
        self.assertEqual(shelld.handle({}, None), {"error": "give start, stop, wipe or list"})

    def test_a_home_is_a_capped_image_until_logout(self):
        import os
        import shutil
        import subprocess
        import tempfile
        from .shell import shelld
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        state, homes = os.path.join(d, "state"), os.path.join(d, "homes")
        os.makedirs(os.path.join(state, "u5"))
        open(os.path.join(state, "u5", "old"), "w").close()
        ran, mounted = [], set()

        def run(cmd, **kw):
            ran.append(cmd)
            if cmd[0] == "mount":
                mounted.add(cmd[-1])
            if cmd[0] == "umount":
                mounted.discard(cmd[-1])
            return subprocess.CompletedProcess(cmd, 0)
        kw = dict(state=state, homes=homes, run=run, mounted=lambda p: p in mounted)
        image, home = os.path.join(homes, "u5.img"), os.path.join(state, "u5")
        shelld.mount_home(5, **kw)
        self.assertEqual(os.path.getsize(image), 22 << 20)
        self.assertEqual(ran, [["mkfs.ext4", "-q", "-F", "-m", "0", image],
                               ["mount", "-o", "loop,nosuid,nodev", image, home]])
        self.assertEqual(os.listdir(home), [])                  # the plain home went
        shelld.mount_home(5, **kw)
        self.assertEqual(len(ran), 2)
        mounted.clear()                                         # a reboot: the same image again
        shelld.mount_home(5, **kw)
        self.assertEqual([c[0] for c in ran], ["mkfs.ext4", "mount", "mount"])
        shelld.wipe(5, **kw)
        self.assertEqual(ran[-2:], [["systemctl", "stop", "fcsh-u5.service"], ["umount", home]])
        self.assertFalse(os.path.exists(image) or os.path.exists(home))
        with mock.patch.object(shelld, "wipe") as w:
            self.assertEqual(shelld.handle({"wipe": 5}, None), {"ok": True})
        w.assert_called_once_with(5)

    def test_logout_ends_the_shell_and_deletes_its_home(self):
        import asyncio
        import os
        import shutil
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        launcher = shellhub.Local(os.path.join(d, "run"), os.path.join(d, "homes"), ["sleep", "60"])
        h = hub.Hub(b"k" * 32, launcher=launcher, run=os.path.join(d, "run"))
        h.people[5] = {"sealed": "s", "server": "x"}

        async def go():
            await h.shells.start(5)
            self.assertTrue(os.path.isdir(os.path.join(d, "homes", "u5")))
            self.assertEqual(await h.ask({"wipe": {"uid": 5}}), {"ok": True})
        asyncio.run(go())
        self.assertEqual(launcher.running(), [])
        self.assertFalse(os.path.exists(os.path.join(d, "homes", "u5")))
        self.assertNotIn(5, h.people)

    def test_a_shell_from_an_older_site_runs_no_model_lines(self):
        import asyncio

        async def go():
            shells = shellhub.Shells(None, "/nonexistent", None, None, None, None)
            sh = shellhub.Shell(5)
            sh.state, sh.term, sh.version = shellhub.LIVE, object(), 1
            shells.shells[5] = sh
            with self.assertRaisesRegex(shellhub.ShellError, "older than the site: exit it"):
                await shells.run(5, "ls", 5)
        asyncio.run(go())

    def test_the_commands_of_the_person_are_plain_text(self):
        import importlib.util
        import os
        spec = importlib.util.spec_from_file_location(
            "ptyhost", os.path.join(os.path.dirname(__file__), "shell", "ptyhost.py"))
        ptyhost = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ptyhost)
        self.assertEqual(ptyhost.plain(b"lsx\b \b -l\r\n\x1b[1mtotal 0\x1b[0m\r\n"), "ls -l\ntotal 0\n")
        self.assertEqual(ptyhost.plain(b"abc\rxy"), "xy")

    def test_agents_md_is_planted_and_the_persons_changes_stay(self):
        import importlib.util
        import os
        import tempfile
        here = os.path.join(os.path.dirname(__file__), "shell")
        spec = importlib.util.spec_from_file_location("ptyhost", os.path.join(here, "ptyhost.py"))
        ptyhost = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ptyhost)
        base = os.path.join(os.path.dirname(__file__), "..", "..", "tmp")
        os.makedirs(base, exist_ok=True)
        d = tempfile.mkdtemp(dir=base)
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        home, src = os.path.join(d, "home"), os.path.join(d, "AGENTS.md")
        os.makedirs(home)

        def site(text):
            with open(src, "w") as f:
                f.write(text)

        def mine():
            with open(os.path.join(home, "AGENTS.md")) as f:
                return f.read()
        site("v1")
        ptyhost.plant(home, src)
        self.assertEqual(mine(), "v1")
        site("v2")                                  # an unchanged copy follows the site
        ptyhost.plant(home, src)
        self.assertEqual(mine(), "v2")
        with open(os.path.join(home, "AGENTS.md"), "w") as f:
            f.write("my rules")                     # the person's own
        site("v3")
        ptyhost.plant(home, src)
        self.assertEqual(mine(), "my rules")
        os.unlink(os.path.join(home, "AGENTS.md"))  # gone: it comes back
        ptyhost.plant(home, src)
        self.assertEqual(mine(), "v3")
        self.assertIn("cat ~/AGENTS.md", kernel.RULES)
        self.assertTrue(os.path.exists(ptyhost.AGENTS))

    def test_the_root_of_a_shell_has_only_what_it_needs(self):
        import os
        import tempfile
        from .shell import shelld
        base = os.path.join(os.path.dirname(__file__), "..", "..", "tmp")
        os.makedirs(base, exist_ok=True)
        d = tempfile.mkdtemp(dir=base)
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        etc = os.path.join(d, "host-etc")
        os.makedirs(os.path.join(etc, "ld.so.conf.d"))
        for name in ("inputrc", "ld.so.cache", "shadow", "fc.env"):
            with open(os.path.join(etc, name), "w") as f:
                f.write("x")
        root = os.path.join(d, "root")
        shelld.skeleton(root, etc)
        shelld.skeleton(root, etc)              # again, at the next start
        self.assertEqual(os.readlink(os.path.join(root, "bin")), "usr/bin")
        self.assertEqual(sorted(os.listdir(os.path.join(root, "etc"))),
                         sorted(["alternatives", "bash.bashrc", "group", "hosts", "inputrc", "ld.so.cache",
                                 "ld.so.conf.d", "localtime", "nsswitch.conf", "passwd", "profile"]))
        self.assertTrue(os.path.isdir(os.path.join(root, "h")))
        self.assertEqual(oct(os.stat(os.path.join(root, "tmp")).st_mode & 0o7777), "0o1777")


class Network(Base):
    """The network is a graph of addresses and peer tables, mapped by an
    hourly walk, and a server's state is read when a person looks."""

    def test_the_walk_maps_the_graph_and_reads_only_peer_tables(self):
        from . import net
        g = net.walk()
        self.assertEqual(sorted(g), sorted([LEDGER] + NET))
        self.assertEqual(g[LEDGER], NET)
        self.assertEqual({c[0] for c in self.fake.calls}, {"peers"})
        self.assertEqual(self.pushed, [])
        net.walk()
        self.assertEqual(self.pushed, [])
        self.fake.down.add(NET[1])                         # its peer table goes away
        g = net.walk()
        self.assertEqual(g[NET[1]], [])
        self.assertEqual(self.pushed, [("all", {"net": True})])

    def test_the_walk_starts_at_the_server_of_each_person_too(self):
        from . import net
        self.person("ana", server="127.0.0.1:55190")       # a server in no peer table
        self.assertIn("127.0.0.1:55190", net.walk())

    def test_the_main_server_is_marked_for_the_sky(self):
        with override_settings(FC_MAIN_SERVER=NET[1]):
            s, j = self.api("GET", "/api/net")
        self.assertEqual([x["address"] for x in j["nodes"] if x["main"]], [NET[1]])
        with override_settings(FC_MAIN_SERVER=""):
            s, j = self.api("GET", "/api/net")
        self.assertEqual([x for x in j["nodes"] if x["main"]], [])

    def test_a_view_reads_its_servers_once_in_a_while(self):
        from . import net
        net.walk()
        n = len(self.fake.calls)
        s, j = self.api("GET", "/api/net")
        self.assertEqual(s, 200, j)
        self.assertEqual([x["address"] for x in j["nodes"]], [LEDGER] + NET)
        self.assertEqual([c[0] for c in self.fake.calls[n:] if c[0] == "peers"], [])
        self.assertEqual(sorted(c[1]["server"] for c in self.fake.calls[n:] if c[0] == "info"),
                         sorted([LEDGER] + NET))
        n = len(self.fake.calls)
        self.api("GET", "/api/net")                        # read a moment ago, so no call
        self.assertEqual([c for c in self.fake.calls[n:] if c[0] in ("info", "instances")], [])

    def test_a_person_asks_one_server_again(self):
        from . import net
        net.walk()
        u = self.person("ana", server="")
        tok = self.login(u)
        self.api("GET", "/api/net", client=tok)
        self.fake.down.add(NET[0])
        s, j = self.api("GET", f"/api/net?fresh={NET[0]}", client=tok)
        self.assertEqual(s, 200, j)
        self.assertEqual([x["up"] for x in j["nodes"] if x["address"] == NET[0]], [False])
        self.assertEqual([x["up"] for x in j["nodes"] if x["address"] == NET[1]], [True])
        s, j = self.api("GET", "/api/net?fresh=10.0.0.9:1", client=tok)   # not in the view
        self.assertEqual((s, j["error"]), (400, "NOT_IN_VIEW"))
        s, j = self.api("GET", f"/api/net?fresh={NET[0]}")                # a visitor
        self.assertEqual(s, 401)

    def test_the_layout_comes_from_the_graph_and_the_addresses(self):
        import itertools
        import math
        from . import net
        names = [f"s{i}.example:{4000 + i}" for i in range(12)]
        g = {a: [names[(i + 1) % 12], names[(i + 5) % 12]] for i, a in enumerate(names)}
        places = net.layout(g)
        self.assertEqual(sorted(places), sorted(names))
        self.assertEqual(net.layout(dict(reversed(list(g.items())))), places)
        self.assertEqual(net.layout({a: list(reversed(p)) for a, p in g.items()}), places)
        lo, hi = net.HEIGHT
        for x, y, z in places.values():
            self.assertTrue(all(isinstance(v, int) for v in (x, y, z)))
            self.assertTrue(lo <= y <= hi)
        near = min(math.hypot(places[a][0] - places[b][0], places[a][2] - places[b][2])
                   for a, b in itertools.combinations(names, 2))
        self.assertGreaterEqual(near, net.GAP - 1)
        for a, ps in g.items():
            for b in ps:
                d = math.hypot(places[a][0] - places[b][0], places[a][2] - places[b][2])
                self.assertLess(d, 3 * net.EDGE, (a, b))
        other = net.layout({a.replace("example", "other"): [b.replace("example", "other") for b in p] for a, p in g.items()})
        self.assertNotEqual(sorted(other.values()), sorted(places.values()))
        g2 = dict(g)
        g2[names[0]] = g[names[0]] + [names[6]]
        self.assertNotEqual(net.layout(g2), places)
        two = net.layout({"a:1": [], "b:1": []})
        self.assertGreaterEqual(math.hypot(two["a:1"][0] - two["b:1"][0], two["a:1"][2] - two["b:1"][2]), net.GAP - 1)
        self.assertEqual(net.layout({}), {})

    def test_the_view_gives_each_server_its_place_in_the_layout(self):
        from . import net
        s, j = self.api("GET", "/api/net")
        star = net.layout({LEDGER: NET})
        self.assertEqual({n["address"]: n["pos"] for n in j["nodes"]}, star)
        g = net.walk()
        whole = net.layout(g)
        key = "net:layout:" + hashlib.sha256(json.dumps(g, sort_keys=True).encode()).hexdigest()
        self.assertEqual(cache.get(key), whole)
        s, j = self.api("GET", "/api/net")
        self.assertEqual([n["pos"] for n in j["nodes"]], [whole[n["address"]] for n in j["nodes"]])
        self.assertEqual(self.api("GET", "/api/net")[1]["nodes"], j["nodes"])

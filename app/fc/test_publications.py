"""Local website visits, their authority and their HTTP framing."""
import socket
import threading
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.http import HttpResponse
from django.test import Client, RequestFactory, TestCase, override_settings

from . import desk, kernel, publications, webdial
from .models import Profile, Publication

HOST = "fc.localhost:8775"
SERVER = "127.0.0.1:57110"
MIDDLEWARE = ["fc.publications.ContentHost"] + [
    m for m in settings.MIDDLEWARE if m != "fc.publications.ContentHost"
]


@override_settings(FC_PUBLICATIONS_PORT=8775, ALLOWED_HOSTS=[".fc.localhost"], MIDDLEWARE=MIDDLEWARE)
class WebsiteTests(TestCase):
    def setUp(self):
        self.author = self.person("ana", "a")
        self.viewer = self.person("ben", "b")
        self.page = Publication.objects.create(owner=self.author, name="board", server=SERVER, server_key="1" * 64,
                                               source="/p/board.lua", program_key="2" * 64)
        self.host = publications.content_host(self.page.pk)
        self.control = Client(HTTP_HOST=HOST)
        self.app = Client(HTTP_HOST=self.host)
        self.control.force_login(self.viewer.user)

    def person(self, name, key):
        user = get_user_model().objects.create_user(username=name)
        return Profile.objects.create(user=user, handle=name, pubkey=key * 64, sealed=name, granted=True)

    def open(self):
        with patch("fc.account.balance", return_value=(True, 123)):
            page = self.control.get("/link/ana/board/deep/?a=1&a=2")
        self.assertEqual(page.status_code, 200)
        ticket = page.context["ticket"]
        fields = {"HTTP_ORIGIN": "http://" + HOST}
        entered = self.app.post(publications.BOOT, {"ticket": ticket}, **fields)
        self.assertEqual((entered.status_code, entered["Location"]), (303, "/deep/?a=1&a=2"))
        self.assertEqual(self.app.post(publications.BOOT, {"ticket": ticket}, **fields).status_code, 403)

    def test_handoff_and_revocation(self):
        self.open()
        with patch("fc.webdial.exchange", return_value=HttpResponse("hello")) as exchange:
            self.assertEqual(self.app.get("/").content, b"hello")
            self.assertEqual(exchange.call_args.args[1:], (self.viewer, self.page))
            self.assertEqual(self.app.post("/", HTTP_ORIGIN="http://" + HOST).status_code, 403)
            self.control.logout()
            self.assertEqual(self.app.get("/").status_code, 403)
            self.assertEqual(exchange.call_count, 1)

    def test_the_frame_is_isolated(self):
        """The site runs in a sandbox with no scripts, on its own origin, and its
        forms and redirects stay there. Each check is part of the isolation."""
        with patch("fc.account.balance", return_value=(True, 123)):
            page = self.control.get("/link/ana/board/")
        origin = "http://" + self.host
        self.assertIn(b'sandbox="allow-forms allow-same-origin"', page.content)
        csp = page["Content-Security-Policy"]
        for part in ("default-src 'none'", "script-src 'self'", f"frame-src {origin}",
                     f"form-action {origin}", "frame-ancestors 'none'"):
            self.assertIn(part, csp)
        self.open()
        with patch("fc.webdial.exchange", return_value=HttpResponse(b"hi")):
            inner = self.app.get("/")
        csp = inner["Content-Security-Policy"]
        for part in ("default-src 'none'", "form-action 'self'", "base-uri 'none'",
                     "frame-ancestors http://" + HOST):
            self.assertIn(part, csp)
        self.assertNotIn("script-src", csp)
        self.assertEqual(inner["X-Content-Type-Options"], "nosniff")

    def test_a_redirect_out_of_the_site_is_refused(self):
        self.open()
        for location in ("http://evil.example/", "//evil.example/x", "http://" + HOST + "/account/key",
                         "/a\\b"):
            with patch("fc.webdial.roundtrip", return_value=(302, "text/html", location, b"")), \
                 patch("fc.webdial.live_pid", return_value=8):
                self.assertEqual(self.app.get("/").status_code, 502, location)
        with patch("fc.webdial.roundtrip", return_value=(303, "text/html", "/next", b"")), \
             patch("fc.webdial.live_pid", return_value=8):
            moved = self.app.get("/")
        self.assertEqual((moved.status_code, moved["Location"]), (303, "http://" + self.host + "/next"))

    def test_content_host_never_routes_to_account(self):
        self.app.force_login(self.author.user)
        self.assertEqual(self.app.get("/account/key").status_code, 403)
        with self.settings(FC_PUBLICATIONS_PORT=0):
            self.assertEqual(self.control.get("/link/ana/board/").status_code, 404)

    def test_withdrawal(self):
        self.open()
        with self.assertRaises(desk.DeskError):
            desk.run(self.viewer.user, "unpublish board")
        desk.run(self.author.user, "unpublish board")
        self.assertEqual(self.app.get("/").status_code, 403)
        self.assertEqual(self.control.get("/link/ana/board/").status_code, 404)

    def test_the_website_guide(self):
        guide = desk.run(self.author.user, "website")[0]
        self.assertIn("conn.pubkey", guide)
        self.assertIn("application/vnd.fc.payment+json", guide)
        self.assertIn("function on_l2call(memo, info)", guide)   # the example shop
        self.assertIn("website", desk.run(self.author.user, "help")[0])
        with self.settings(FC_PUBLICATIONS_PORT=0):
            with self.assertRaisesMessage(desk.DeskError, "no op 'website'"):
                desk.run(self.author.user, "website")
            self.assertNotIn("website", desk.run(self.author.user, "help")[0])

    def test_the_model_reads_what_a_publication_op_prints(self):
        from . import aishell
        for op in ("publish", "unpublish", "visit", "website"):
            self.assertIn(op, desk.READS)
        sh = aishell.Shell(self.author)
        with patch("fc.push.ask", return_value={"ok": {"out": "http://fc.localhost:8775/link/ana/board/\n",
                                                      "result": "exit 0"}}):
            kind, out, more = sh.run("desk publish board board.lua on " + SERVER)
        self.assertTrue(more, "the model must read the link that publish prints")
        self.assertIn("/link/ana/board/", out)

    def test_the_assistant_reads_of_websites_and_of_a_test_server(self):
        with patch("fc.net.peers", return_value=[]):
            head = kernel._head(self.viewer.user)
            with self.settings(DEBUG=True):
                local = kernel._head(self.viewer.user)
            with self.settings(FC_PUBLICATIONS_PORT=0):
                plain = kernel._head(self.viewer.user)
        self.assertIn(kernel.WEBSITES, head)
        self.assertNotIn(kernel.LOCAL_TEST, head)
        self.assertIn(kernel.LOCAL_TEST, local)
        self.assertNotIn(kernel.WEBSITES, plain)

    def test_publication_name_conflict(self):
        with patch("fc.bridge.call", side_effect=[{"pubkey": self.page.server_key},
                                                   {"instances": [{"program_key": "3" * 64}]}]):
            with self.assertRaisesMessage(desk.DeskError, "already in use"):
                desk.run(self.author.user, "publish board other.lua on " + SERVER)
        self.assertEqual(Publication.objects.get().program_key, self.page.program_key)

    def test_each_request_resolves_the_live_instance(self):
        with patch("fc.bridge.call", side_effect=[
            {"instances": []}, {"instances": [{"pid": 8, "program_key": self.page.program_key}]}
        ]), patch("fc.webdial._exchange") as exchange:
            req = RequestFactory().get("/")
            self.assertEqual(webdial.exchange(req, self.viewer, self.page).status_code, 503)
            webdial.exchange(req, self.viewer, self.page)
            exchange.assert_called_once_with(req, self.viewer, self.page, 8)

    def test_a_visit_reaches_the_newest_instance(self):
        live = [{"pid": 3, "program_key": self.page.program_key, "started_us": 200},
                {"pid": 5, "program_key": self.page.program_key, "started_us": 900},
                {"pid": 7, "program_key": "3" * 64, "started_us": 999},
                {"pid": 4, "program_key": self.page.program_key, "started_us": 500}]
        with patch("fc.bridge.call", return_value={"instances": live}):
            self.assertEqual(webdial.live_pid(SERVER, self.page.source, self.page.program_key), 5)
            self.assertIsNone(webdial.live_pid(SERVER, self.page.source, "4" * 64))

    def test_visit_shows_a_page_as_the_visitor_gets_it(self):
        local, remote = socket.socketpair()
        sent = []

        def program():
            with remote, remote.makefile("rb") as stream:
                for line in stream:
                    sent.append(line)
                    if line == b"\r\n":
                        break
                remote.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: 11\r\n\r\n<p>hi</p>\r\n")

        worker = threading.Thread(target=program)
        worker.start()
        live = {"instances": [{"pid": 8, "program_key": self.page.program_key}]}
        with patch("fc.bridge.call", return_value=live), patch("fc.launch.dial", return_value=local) as dial:
            text = desk.run(self.author.user, "visit board /posts")[0]
        worker.join(2)
        self.assertEqual(sent[0], b"GET /posts HTTP/1.1\r\n")
        self.assertIn(b"Host: " + self.host.encode() + b"\r\n", sent)
        self.assertEqual(dial.call_args.args[1:4], (self.author.sealed, SERVER, 8))
        self.assertTrue(text.startswith("HTTP 200, text/html"), text)
        self.assertIn("<p>hi</p>", text)
        with patch("fc.bridge.call", return_value={"instances": []}):
            self.assertIn("offline", desk.run(self.author.user, "visit board")[0])
        with self.assertRaisesMessage(desk.DeskError, "no publication"):
            desk.run(self.author.user, "visit nothing")
        # Only your own: another person's page would go into your assistant.
        with self.assertRaises(desk.DeskError):
            desk.run(self.viewer.user, "visit ana/board")
        with self.assertRaisesMessage(desk.DeskError, "no publication"):
            desk.run(self.viewer.user, "visit board")

    def test_binary_http_and_truncated_reply(self):
        for wire, status in [(b"abc\x00", 200), (b"ab", 502)]:
            local, remote = socket.socketpair()
            sent = []
            def program():
                with remote, remote.makefile("rb") as stream:
                    sent.append(stream.readline())
                    for line in stream:
                        if line == b"\r\n":
                            break
                        sent.append(line)
                    sent.append(stream.read(3))
                    remote.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 4\r\nSet-Cookie: app=123\r\n\r\n" + wire)

            worker = threading.Thread(target=program)
            worker.start()
            req = RequestFactory().post("/deep?q=1&q=2", b"x\x00z", content_type="application/octet-stream",
                                        HTTP_HOST=self.host, HTTP_COOKIE="sessionid=private")
            with patch("fc.bridge.call", return_value={"instances": [{"pid": 8, "program_key": self.page.program_key}]}), \
                 patch("fc.launch.dial", return_value=local):
                reply = webdial.exchange(req, self.viewer, self.page)
            worker.join(2)
            self.assertEqual(reply.status_code, status)
            self.assertEqual(sent[0], b"POST /deep?q=1&q=2 HTTP/1.1\r\n")
            self.assertEqual(sent[-1], b"x\x00z")
            self.assertNotIn(b"sessionid", b"".join(sent))
            self.assertFalse(reply.cookies)

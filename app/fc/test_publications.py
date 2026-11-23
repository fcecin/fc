"""Websites at /link/<handle>/<name>/: the visit as the visitor, the policy of
the pages, the checks of a form post, the paths and the HTTP framing."""
import socket
import threading
import time
from unittest.mock import patch

import os
import subprocess
import sys

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client, TestCase

from . import account, bridge, desk, kernel, launch, webdial
from .models import Profile, Publication

LIVE_PID = webdial.live_pid

SERVER = "net.example:55000"
ORIGIN = "http://testserver"
HERE = ORIGIN + "/link/ana/board/"
POLICY = ("default-src 'none'; sandbox allow-forms allow-same-origin; script-src 'none'; "
          f"style-src 'unsafe-inline' {HERE}; img-src {HERE} data:; connect-src 'none'; "
          "object-src 'none'; frame-src 'none'; worker-src 'none'; base-uri 'none'; "
          f"form-action {HERE} {ORIGIN}/payments/review; frame-ancestors 'none'")


def reply(status=200, content_type="text/html", body=b"<p>hi</p>", locations=()):
    return webdial.Reply(status, content_type, list(locations), body)


class Program:
    """A fake program on a socket pair: it keeps the request bytes and sends
    `wire` as its reply."""

    def __init__(self, wire):
        self.local, remote = socket.socketpair()
        self.sent = b""

        def run():
            with remote:
                remote.settimeout(5)
                while b"\r\n\r\n" not in self.sent:
                    chunk = remote.recv(65536)
                    if not chunk:
                        return
                    self.sent += chunk
                head, _, body = self.sent.partition(b"\r\n\r\n")
                need = 0
                for line in head.split(b"\r\n"):
                    if line.lower().startswith(b"content-length:"):
                        need = int(line.split(b":")[1])
                while len(body) < need:
                    body += remote.recv(65536)
                self.sent = head + b"\r\n\r\n" + body
                remote.sendall(wire)

        self.worker = threading.Thread(target=run)
        self.worker.start()


class WebsiteTests(TestCase):
    def setUp(self):
        cache.clear()   # the balances, and the holds of moves
        self.author = self.person("ana", "a")
        self.viewer = self.person("ben", "b")
        self.page = Publication.objects.create(owner=self.author, name="board", server=SERVER, server_key="1" * 64,
                                               source="/p/board.lua", program_key="2" * 64)
        self.home = "/h/" + self.author.pubkey + "/"
        self.client = Client()
        self.client.force_login(self.viewer.user)
        live = patch("fc.webdial.live_pid", return_value=8)
        self.live = live.start()
        self.addCleanup(live.stop)

    def person(self, name, key):
        user = get_user_model().objects.create_user(username=name, email=f"{name}@example.test")
        return Profile.objects.create(user=user, handle=name, pubkey=key * 64, sealed=name, granted=True)

    def post(self, path, data=None, referer=HERE, origin=ORIGIN, client=None, **extra):
        headers = dict(extra)
        if origin is not None:
            headers["HTTP_ORIGIN"] = origin
        if referer is not None:
            headers["HTTP_REFERER"] = referer
        return (client or self.client).post(path, data or {"name": "Ann"}, **headers)

    def test_a_visit_dials_the_program_as_the_visitor(self):
        with patch("fc.webdial.roundtrip", return_value=reply()) as trip:
            page = self.client.get("/link/ana/board/deep/?a=1&a=2&v=a%2Bb&e=")
        self.assertEqual((page.status_code, page.content), (200, b"<p>hi</p>"))
        self.assertEqual(page["Content-Type"], "text/html; charset=utf-8")
        profile, publication, pid, method, target, headers, body = trip.call_args.args
        self.assertEqual((profile, publication, pid, method, body), (self.viewer, self.page, 8, "GET", b""))
        self.assertEqual(target, "/deep/?a=1&a=2&v=a%2Bb&e=")
        self.assertEqual(set(headers), {"Accept"})
        with patch("fc.webdial.roundtrip", return_value=reply()) as trip:
            self.client.get("/link/ana/board/")
            self.client.get("/link/ana/board/caf%C3%A9/a%20b")
        self.assertEqual([c.args[4] for c in trip.call_args_list], ["/", "/caf%C3%A9/a%20b"])

    def test_the_policy_of_each_reply(self):
        """A page of the website, a redirect, an error of fc and a refused path
        all carry the policy of the website."""
        with patch("fc.webdial.roundtrip", side_effect=[reply(), reply(303, locations=["/next"])]):
            replies = [self.client.get("/link/ana/board/"), self.client.get("/link/ana/board/")]
        self.live.return_value = None
        replies += [self.client.get("/link/ana/board/"), self.client.get("/link/ana/board/a/../b")]
        self.assertEqual([r.status_code for r in replies], [200, 303, 503, 400])
        for r in replies:
            self.assertEqual(r["Content-Security-Policy"], POLICY)
            self.assertEqual((r["X-Content-Type-Options"], r["Referrer-Policy"], r["Cache-Control"],
                              r["X-Frame-Options"], r["X-DNS-Prefetch-Control"]),
                             ("nosniff", "same-origin", "private, no-store", "DENY", "off"))
        missing = self.client.get("/link/ana/nothing/")
        self.assertEqual(missing.status_code, 404)
        self.assertIn("sandbox", missing["Content-Security-Policy"])
        self.assertIn("form-action 'none'", missing["Content-Security-Policy"])

    def test_the_wire_request_and_reply(self):
        """fc makes a new request with a few fields, and a new response: no
        cookie or header of the browser reaches the program, and no header of
        the program reaches the browser."""
        wire = (b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: 2\r\n"
                b"Set-Cookie: sessionid=evil; Path=/\r\nSet-Cookie: csrftoken=evil\r\nRefresh: 0;url=/leave\r\n"
                b"Link: </x>; rel=preload\r\nContent-Security-Policy: script-src *\r\nX-Frame-Options: ALLOWALL\r\n"
                b"Content-Disposition: attachment\r\nClear-Site-Data: \"cookies\"\r\nAccess-Control-Allow-Origin: *\r\n"
                b"\r\nhi")
        program = Program(wire)
        with patch("fc.launch.dial", return_value=program.local) as dial:
            self.client.cookies["tracker"] = "private"
            page = self.post("/link/ana/board/sign?q=1&q=2", "name=Ann", HTTP_X_CSRFTOKEN="token",
                             content_type="application/x-www-form-urlencoded",
                             HTTP_AUTHORIZATION="Basic x", HTTP_X_FORWARDED_FOR="1.2.3.4")
        program.worker.join(5)
        self.assertEqual(dial.call_args.args[1:4], (self.viewer.sealed, SERVER, 8))
        self.assertEqual(dial.call_args.kwargs, {"server_key": self.page.server_key})
        head, _, body = program.sent.partition(b"\r\n\r\n")
        lines = head.split(b"\r\n")
        self.assertEqual(lines[0], b"POST /sign?q=1&q=2 HTTP/1.1")
        self.assertEqual(sorted(line.split(b":")[0].lower() for line in lines[1:]),
                         [b"accept", b"accept-encoding", b"connection", b"content-length", b"content-type", b"host"])
        self.assertIn(b"Host: site", lines)
        self.assertEqual(body, b"name=Ann")
        self.assertNotIn(b"private", program.sent)
        self.assertNotIn(b"sessionid", program.sent)
        self.assertEqual((page.status_code, page.content), (200, b"hi"))
        for name in ("Refresh", "Link", "Content-Disposition", "Clear-Site-Data", "Access-Control-Allow-Origin"):
            self.assertFalse(page.has_header(name), name)
        self.assertEqual(page["Content-Security-Policy"], POLICY)
        self.assertEqual(page["X-Frame-Options"], "DENY")
        # The session middleware can send fc's own session cookie again. No
        # cookie value of the program reaches the browser.
        self.assertNotIn("evil", str(page.cookies))
        self.assertNotIn("csrftoken", page.cookies)

    def test_a_visitor_who_is_not_signed_in_signs_in_and_comes_back(self):
        guest = Client()
        with patch("fc.webdial.roundtrip") as trip:
            first = guest.get("/link/ana/board/posts/?page=2")
            posted = guest.post("/link/ana/board/sign", {"name": "Ann"}, HTTP_ORIGIN=ORIGIN, HTTP_REFERER=HERE)
        trip.assert_not_called()
        self.assertEqual((first.status_code, first["Location"]), (303, "/"))
        self.assertEqual((posted.status_code, posted["Location"]), (303, "/"))
        # A post does not come back as a post: the sign-in returns to the website's page.
        self.assertEqual(guest.session["publication_return"], "/link/ana/board/")
        guest.get("/link/ana/board/posts/?page=2")
        with self.settings(FC_LOGIN_WITHOUT_CODE=True), patch("fc.enter.ensure_profile"):
            entered = guest.post("/enter", {"email": "ben@example.test"})
        self.assertEqual(entered["Location"], "/link/ana/board/posts/?page=2")
        # A saved return that is not a website page goes home.
        for saved in ("/account/key", "//evil.example/", "/link/ana/board/../../account/key", "https://evil.example/"):
            other = Client()
            session = other.session
            session["publication_return"] = saved
            session.save()
            with self.settings(FC_LOGIN_WITHOUT_CODE=True), patch("fc.enter.ensure_profile"):
                self.assertEqual(other.post("/enter", {"email": "ben@example.test"})["Location"], "/", saved)

    def test_a_link_with_no_slash_gets_one(self):
        moved = self.client.get("/link/ana/board?a=1&b=%2B")
        self.assertEqual((moved.status_code, moved["Location"]), (302, "/link/ana/board/?a=1&b=%2B"))
        self.assertEqual(self.client.head("/link/ana/board")["Location"], "/link/ana/board/")
        with patch("fc.webdial.roundtrip") as trip:
            self.assertEqual(self.post("/link/ana/board").status_code, 405)
        trip.assert_not_called()

    def test_paths_that_fc_does_not_pass_on(self):
        with patch("fc.webdial.roundtrip", return_value=reply()) as trip:
            for path in ("a/../b", "a/./b", "a//b", "%2E%2E/x", "a/%2e", "a%5Cb", "a%00b", "a%7Fb"):
                self.assertEqual(self.client.get("/link/ana/board/" + path).status_code, 400, path)
            # Django's route takes no newline, so this one is a 404 of fc.
            self.assertEqual(self.client.get("/link/ana/board/a%0Ab").status_code, 404)
        trip.assert_not_called()

    def test_methods(self):
        self.assertEqual(self.client.put("/link/ana/board/").status_code, 405)
        self.assertIn("POST", self.client.delete("/link/ana/board/")["Allow"])
        # A HEAD goes to the program as a GET, and gets the length of the page.
        with patch("fc.webdial.roundtrip", return_value=reply(body=b"123456789")) as trip:
            head = self.client.head("/link/ana/board/x")
        self.assertEqual((trip.call_args.args[3], head.status_code, head.content), ("GET", 200, b""))
        self.assertEqual(head["Content-Length"], "9")
        self.assertEqual(head["Content-Security-Policy"], POLICY)
        with patch("fc.webdial.roundtrip", return_value=reply(body=b"")):
            self.assertEqual(self.client.head("/link/ana/board/x")["Content-Length"], "0")
        with patch("fc.webdial.roundtrip") as trip:
            self.assertEqual(self.client.generic("GET", "/link/ana/board/", b"body").status_code, 400)
        trip.assert_not_called()

    def test_every_reply_under_link_has_a_policy(self):
        """A 405 of Django, a route of no website and a post with no slash
        get the policy that allows nothing."""
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.viewer.user)
        replies = [self.client.put("/link/ana/board/"), self.client.get("/link/ana"),
                   self.client.get("/link/ana/b%20c/"), strict.post("/link/ana/board", {})]
        self.assertEqual([r.status_code for r in replies], [405, 404, 404, 405])
        for r in replies:
            self.assertIn("sandbox", r["Content-Security-Policy"])
            self.assertIn("form-action 'none'", r["Content-Security-Policy"])
            self.assertNotIn("script-src 'self'", r["Content-Security-Policy"])

    def test_an_error_of_a_middleware_under_link_has_the_policy(self):
        """The header middleware is outside the others, so a host that Django
        refuses also gets the policy under /link/."""
        refused = Client(HTTP_HOST="evil.example").get("/link/ana/board/")
        self.assertEqual(refused.status_code, 400)
        self.assertIn("sandbox", refused["Content-Security-Policy"])

    def test_each_module_imports_first(self):
        """No import cycle of the website modules breaks at import time."""
        env = dict(os.environ, DJANGO_SETTINGS_MODULE="fcsite.settings")
        for module in ("fc.webdial", "fc.payments", "fc.publications", "fc.account", "fc.desk"):
            run = subprocess.run([sys.executable, "-c", f"import django; django.setup(); import {module}"],
                                 cwd=settings.BASE_DIR, env=env, capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, f"{module}: {run.stderr[-400:]}")

    def test_a_form_post_must_come_from_a_page_of_the_same_website(self):
        """The pages of all websites share fc's origin. fc sends a post only
        when the page that sent it is in the same website."""
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.viewer.user)
        with patch("fc.webdial.roundtrip", return_value=reply()) as trip:
            self.assertEqual(self.post("/link/ana/board/sign", client=strict).status_code, 200)
            self.assertEqual(self.post("/link/ana/board/sign", referer=HERE + "posts/7?x").status_code, 200)
            self.assertEqual(trip.call_count, 2)
            refused = [
                self.post("/link/ana/board/sign", origin=None),
                self.post("/link/ana/board/sign", origin="null"),
                self.post("/link/ana/board/sign", origin="http://evil.example"),
                self.post("/link/ana/board/sign", origin="https://testserver"),
                self.post("/link/ana/board/sign", referer=None),
                self.post("/link/ana/board/sign", referer=ORIGIN),
                self.post("/link/ana/board/sign", referer=ORIGIN + "/"),
                self.post("/link/ana/board/sign", referer=ORIGIN + "/link/ana/boardx/"),
                self.post("/link/ana/board/sign", referer=ORIGIN + "/link/ana/board"),
                self.post("/link/ana/board/sign", referer=ORIGIN + "/link/ben/other/"),
                self.post("/link/ana/board/sign", referer="http://evil.example/link/ana/board/"),
                self.post("/link/ana/board/sign", HTTP_SEC_FETCH_SITE="cross-site"),
                self.post("/link/ana/board/sign", HTTP_SEC_FETCH_SITE="same-site"),
                self.post("/link/ana/board/sign", HTTP_SEC_FETCH_MODE="no-cors"),
                self.post("/link/ana/board/sign", HTTP_SEC_FETCH_MODE="cors"),
                self.post("/link/ana/board/sign", HTTP_SEC_FETCH_DEST="iframe"),
                self.post("/link/ana/board/sign", referer="http://[::1/link/ana/board/"),
                self.post("/link/ana/board/sign", referer=ORIGIN + "/link/ana/boards/"),
                # In this website, but with segments that fc never serves.
                self.post("/link/ana/board/sign", referer=HERE + "a/..%2F..%2F..%2Flink/ben/other/"),
                self.post("/link/ana/board/sign", referer=HERE + "a//b"),
                self.post("/link/ana/board/sign", referer=HERE + "a%5Cb"),
                self.post("/link/ana/board/sign", referer=HERE + "a%2Fb"),
            ]
            self.assertEqual([r.status_code for r in refused], [403] * len(refused))
            ping = self.client.post("/link/ana/board/sign", "PING", content_type="text/ping",
                                    HTTP_ORIGIN=ORIGIN, HTTP_REFERER=HERE)
            self.assertEqual(ping.status_code, 415)
            self.assertEqual(trip.call_count, 2)

    def test_a_page_of_one_website_cannot_post_to_another_as_its_visitor(self):
        other = Publication.objects.create(owner=self.viewer, name="other", server=SERVER, server_key="1" * 64,
                                           source="/p/other.lua", program_key="3" * 64)
        with patch("fc.webdial.roundtrip", return_value=reply()) as trip:
            self.assertEqual(self.post("/link/ben/other/delete", referer=HERE).status_code, 403)
            self.assertEqual(self.post("/link/ben/other/delete", referer=HERE + "../../ben/other/").status_code, 403)
        trip.assert_not_called()
        self.assertEqual(other.name, "other")

    def test_only_a_page_of_the_website_loads_its_parts(self):
        """A navigation that the visitor starts can come from anywhere, as a
        link can. An image, a stylesheet, a prefetch or a prerender comes
        from the page that has it, so it must be a page of the same website."""
        other = ORIGIN + "/link/ben/other/"
        with patch("fc.webdial.roundtrip", return_value=reply()) as trip:
            allowed = [
                self.client.get("/link/ana/board/x"),
                self.client.get("/link/ana/board/x", HTTP_SEC_FETCH_MODE="navigate", HTTP_REFERER=other),
                self.client.get("/link/ana/board/x", HTTP_SEC_FETCH_MODE="navigate", HTTP_SEC_FETCH_SITE="cross-site"),
                self.client.get("/link/ana/board/a.png", HTTP_SEC_FETCH_MODE="no-cors", HTTP_REFERER=HERE + "p"),
                self.client.get("/link/ana/board/s.css", HTTP_SEC_FETCH_MODE="no-cors",
                                HTTP_REFERER=HERE + "style.css"),
                self.client.get("/link/ana/board/n", HTTP_SEC_PURPOSE="prefetch", HTTP_REFERER=HERE),
            ]
            self.assertEqual([r.status_code for r in allowed], [200] * len(allowed))
            self.assertEqual(trip.call_count, len(allowed))
            refused = [
                self.client.get("/link/ana/board/a.png", HTTP_SEC_FETCH_MODE="no-cors", HTTP_REFERER=other),
                self.client.get("/link/ana/board/a.png", HTTP_SEC_FETCH_MODE="no-cors"),
                self.client.get("/link/ana/board/x", HTTP_SEC_FETCH_MODE="navigate", HTTP_REFERER=other,
                                HTTP_SEC_PURPOSE="prefetch;prerender"),
                self.client.get("/link/ana/board/x", HTTP_SEC_FETCH_MODE="no-cors", HTTP_PURPOSE="prefetch",
                                HTTP_REFERER=other),
                self.client.get("/link/ana/board/x", HTTP_SEC_FETCH_MODE="navigate", HTTP_SEC_PURPOSE="prefetch"),
                self.client.head("/link/ana/board/x", HTTP_SEC_FETCH_MODE="cors", HTTP_REFERER=other),
            ]
            self.assertEqual([r.status_code for r in refused], [403] * len(refused))
            self.assertEqual(trip.call_count, len(allowed))

    def test_the_endpoints_of_fc_keep_their_csrf_check(self):
        """A form of a website can reach fc's own endpoints only without fc's
        CSRF token, so each one refuses it."""
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.viewer.user)
        for path in ("/leave", "/account/key", "/account/handle", "/api/send", "/api/move", "/payments/1/approve",
                     "/payments/1/decline", "/api/desk"):
            r = strict.post(path, {"amount": "1", "to": "ana"}, HTTP_ORIGIN=ORIGIN, HTTP_REFERER=HERE)
            self.assertEqual(r.status_code, 403, path)

    def test_redirects_stay_in_the_website(self):
        def follow(location, target="/orders/view"):
            return webdial.redirect_target(location, target, "/link/ana/board")
        self.assertEqual(follow("/done"), "/link/ana/board/done")
        self.assertEqual(follow("next?q=1"), "/link/ana/board/orders/next?q=1")
        self.assertEqual(follow("?x=1"), "/link/ana/board/orders/view?x=1")
        self.assertEqual(follow("#top"), "/link/ana/board/orders/view#top")
        self.assertEqual(follow("#top", "/orders/view?page=2"), "/link/ana/board/orders/view?page=2#top")
        self.assertEqual(follow("next", "/orders/view?page=2"), "/link/ana/board/orders/next")
        self.assertEqual(follow("?", "/page?old=1"), "/link/ana/board/page")
        self.assertEqual(follow("?#x", "/page?old=1"), "/link/ana/board/page#x")
        self.assertEqual(follow("../../../x"), "/link/ana/board/x")
        self.assertEqual(follow("/"), "/link/ana/board/")
        for bad in ("http://evil.example/", "//evil.example/x", "http://testserver/account/key", "https://site/x",
                    "http://site/x", "/a\\b", "/%2e%2e/%2e%2e/account", "/a/%2E%2E/b", "/a%2Fb", "/a%5Cb",
                    "javascript:alert(1)", "/a b", "/a\x00", "/café", "", "/a//b", "x:y"):
            self.assertIsNone(follow(bad), bad)
        with patch("fc.webdial.roundtrip", return_value=reply(303, locations=["/next", "/other"])):
            self.assertEqual(self.client.get("/link/ana/board/").status_code, 502)
        with patch("fc.webdial.roundtrip", return_value=reply(302, locations=["//evil.example/"])):
            self.assertEqual(self.client.get("/link/ana/board/").status_code, 502)
        with patch("fc.webdial.roundtrip", return_value=reply(307, locations=["sign"])):
            moved = self.post("/link/ana/board/form/")
        self.assertEqual((moved.status_code, moved["Location"], moved.content), (307, "/link/ana/board/form/sign", b""))
        with patch("fc.webdial.roundtrip", return_value=reply(304)):
            self.assertEqual(self.client.get("/link/ana/board/").status_code, 502)

    def test_the_types_that_reach_the_browser(self):
        cases = [("text/html; charset=iso-8859-1", b"x", 200, "text/html; charset=utf-8"),
                 ("text/css", b"x", 200, "text/css; charset=utf-8"),
                 ("TEXT/PLAIN", b"x", 200, "text/plain; charset=utf-8"),
                 ("image/png", b"x", 200, "image/png"),
                 ("image/webp; q=1", b"x", 200, "image/webp"),
                 ("image/svg+xml", b"<svg/>", 502, None), ("application/xhtml+xml", b"x", 502, None),
                 ("text/xml", b"x", 502, None), ("application/javascript", b"x", 502, None),
                 ("application/pdf", b"x", 502, None), ("application/octet-stream", b"x", 502, None),
                 ("application/octet-stream", b"", 200, "text/plain; charset=utf-8")]
        for content_type, body, status, given in cases:
            with patch("fc.webdial.roundtrip", return_value=reply(content_type=content_type, body=body)):
                r = self.client.get("/link/ana/board/")
            self.assertEqual(r.status_code, status, content_type)
            if given:
                self.assertEqual(r["Content-Type"], given, content_type)

    def test_an_encoded_reply_or_two_types_is_refused(self):
        for head in (b"Content-Type: text/html\r\nContent-Encoding: gzip\r\n",
                     b"Content-Type: text/plain\r\nContent-Type: text/html\r\n"):
            program = Program(b"HTTP/1.1 200 OK\r\n" + head + b"Content-Length: 2\r\n\r\nhi")
            with patch("fc.launch.dial", return_value=program.local):
                self.assertEqual(self.client.get("/link/ana/board/").status_code, 502, head)
            program.worker.join(5)

    def test_a_visitor_with_no_credit_on_the_server_gets_some_first(self):
        """CES refuses a dial with no credit on the program's server. fc reads
        the balance, moves VISIT_CREDIT, waits for it there, and dials again."""
        refused = launch.LaunchError("CES_ERROR_INSUFFICIENT_BALANCE")
        there = {"raw": 0}

        def balance(profile, server, timeout=20):
            return (there["raw"] > 0, there["raw"])

        def move(profile, src, dst, amount):
            there["raw"] += amount
        with patch("fc.webdial.roundtrip", side_effect=[refused, reply()]) as trip, \
             patch("fc.account.balance", side_effect=balance), patch("fc.account.move", side_effect=move) as moved:
            page = self.client.get("/link/ana/board/")
        self.assertEqual((page.status_code, trip.call_count), (200, 2))
        moved.assert_called_once_with(self.viewer, account.here(self.viewer), SERVER, webdial.VISIT_CREDIT)
        # Credit that another request moved meanwhile: no move.
        with patch("fc.webdial.roundtrip", side_effect=[refused, reply()]), \
             patch("fc.account.balance", return_value=(True, 5)), patch("fc.account.move") as moved:
            self.assertEqual(self.client.get("/link/ana/board/").status_code, 200)
        moved.assert_not_called()
        # fc cannot move credit there.
        with patch("fc.webdial.roundtrip", side_effect=[refused]) as trip, \
             patch("fc.account.balance", return_value=(False, 0)), \
             patch("fc.account.move", side_effect=account.AccountError("CES_ERROR_INSUFFICIENT_BALANCE", "no")):
            page = self.client.get("/link/ana/board/")
        self.assertEqual((page.status_code, trip.call_count), (402, 1))
        self.assertIn(b"Not enough credit", page.content)
        # The credit does not arrive in time: the visitor loads the page again.
        with patch("fc.webdial.roundtrip", side_effect=[refused]) as trip, patch.object(account, "ARRIVAL", 0.3), \
             patch("fc.account.balance", return_value=(False, 0)), patch("fc.account.move") as moved:
            page = self.client.get("/link/ana/board/")
        self.assertEqual((page.status_code, trip.call_count, moved.call_count), (402, 1, 1))
        self.assertIn(b"Load the page again in a moment", page.content)
        # A reload while that credit is on its way does not move it again,
        # also in another worker: the move holds back the next one.
        with patch("fc.webdial.roundtrip", side_effect=[refused]), \
             patch("fc.account.balance", return_value=(False, 0)), patch("fc.account.move") as moved:
            page = self.client.get("/link/ana/board/")
        self.assertEqual((page.status_code, moved.call_count), (402, 0))
        cache.clear()
        # A move with no answer can be on its way too: no second move.
        with patch("fc.webdial.roundtrip", side_effect=[refused, refused]), \
             patch("fc.account.balance", return_value=(False, 0)), \
             patch("fc.account.move", side_effect=account.AccountError("BRIDGE_LOST", "lost")) as moved:
            first = self.client.get("/link/ana/board/")
            again = self.client.get("/link/ana/board/")
        self.assertEqual((first.status_code, again.status_code, moved.call_count), (402, 402, 1))
        self.assertIn(b"not there yet", first.content)
        cache.clear()
        # A balance that fc cannot read, or one below 0, moves nothing.
        for unreadable in ((False, None), (True, -5)):
            with patch("fc.webdial.roundtrip", side_effect=[refused]), \
                 patch("fc.account.balance", return_value=unreadable), patch("fc.account.move") as moved:
                self.assertEqual(self.client.get("/link/ana/board/").status_code, 402, unreadable)
            moved.assert_not_called()
        # The visitor's own server is the website's server.
        self.viewer.server = SERVER
        self.viewer.save()
        with patch("fc.webdial.roundtrip", side_effect=[refused]), patch("fc.account.move") as moved:
            self.assertEqual(self.client.get("/link/ana/board/").status_code, 402)
        moved.assert_not_called()
        # Another refusal of the dial moves nothing.
        with patch("fc.webdial.roundtrip", side_effect=launch.LaunchError("CES_ERROR_TIMEOUT")), \
             patch("fc.account.move") as moved:
            self.assertEqual(self.client.get("/link/ana/board/").status_code, 502)
        moved.assert_not_called()

    def test_busy_slots_and_limits(self):
        with patch.object(webdial, "SLOTS", threading.BoundedSemaphore(1)) as slots:
            slots.acquire()
            with patch("fc.webdial.roundtrip") as trip:
                self.assertEqual(self.client.get("/link/ana/board/").status_code, 503)
            trip.assert_not_called()
            slots.release()
        with patch("fc.webdial.roundtrip") as trip:
            big = self.client.post("/link/ana/board/sign", b"x" * (webdial.MAX_UPLOAD + 1),
                                   content_type="text/plain", HTTP_ORIGIN=ORIGIN, HTTP_REFERER=HERE)
        self.assertEqual(big.status_code, 413)
        trip.assert_not_called()

    def test_publish_on_a_network_server(self):
        live = [{"pid": 3, "program_key": "5" * 64, "started_us": 100},
                {"pid": 5, "program_key": "5" * 64, "started_us": 900},
                {"pid": 6, "program_key": "0" * 64, "started_us": 999},
                {"pid": 7, "started_us": 999}]
        calls = []

        def fake(op, **args):
            calls.append((op, args))
            return {"pubkey": "9" * 64} if op == "info" else {"instances": live}
        with patch("fc.bridge.call", side_effect=fake), self.settings(FC_URL="https://ces.example"):
            out = desk.run(self.author.user, "publish shop shop.lua on " + SERVER)[0]
        self.assertEqual(out, "https://ces.example/link/ana/shop/")
        self.assertEqual(calls[1], ("instances", {"server": SERVER, "server_key": "9" * 64,
                                                  "path": "/h/" + "a" * 64 + "/shop.lua"}))
        shop = Publication.objects.get(name="shop")
        self.assertEqual((shop.server, shop.server_key, shop.program_key), (SERVER, "9" * 64, "5" * 64))
        with patch("fc.bridge.call", side_effect=bridge.BridgeError("ADDRESS_NOT_PUBLIC")):
            with self.assertRaises(desk.DeskError):
                desk.run(self.author.user, "publish other other.lua on 10.0.0.1:55000")
        with patch("fc.bridge.call", side_effect=[{"pubkey": "9" * 64}, {"instances": []}]):
            with self.assertRaisesMessage(desk.DeskError, "Start the website"):
                desk.run(self.author.user, "publish other other.lua on " + SERVER)
        # A file that was made again runs with a new key next to its old instances.
        two = [{"pid": 3, "program_key": "4" * 64}, {"pid": 5, "program_key": "6" * 64}]
        with patch("fc.bridge.call", side_effect=[{"pubkey": "9" * 64}, {"instances": two}]):
            with self.assertRaisesMessage(desk.DeskError, "different program keys"):
                desk.run(self.author.user, "publish other other.lua on " + SERVER)
        self.assertFalse(Publication.objects.filter(name="other").exists())

    def test_publication_name_conflict(self):
        with patch("fc.bridge.call", side_effect=[{"pubkey": self.page.server_key},
                                                   {"instances": [{"program_key": "3" * 64}]}]):
            with self.assertRaisesMessage(desk.DeskError, "already in use"):
                desk.run(self.author.user, "publish board other.lua on " + SERVER)
        self.assertEqual(Publication.objects.get(name="board").program_key, self.page.program_key)

    def test_withdrawal(self):
        with self.assertRaises(desk.DeskError):
            desk.run(self.viewer.user, "unpublish board")
        desk.run(self.author.user, "unpublish board")
        with patch("fc.webdial.roundtrip") as trip:
            self.assertEqual(self.client.get("/link/ana/board/").status_code, 404)
        trip.assert_not_called()

    def test_the_website_guide(self):
        index = desk.run(self.author.user, "website")[0]
        names = [p.name.removesuffix(".md") for p in desk.WEBSITE.iterdir()]
        self.assertIn("program", names)
        for name in names:
            if name != "index":
                self.assertIn(f"`desk website {name}`", index.split("## Pages")[1])
        program = desk.run(self.author.user, "website program")[0]
        self.assertIn("conn.pubkey", program)
        self.assertIn('href="style.css"', program)
        shop = desk.run(self.author.user, "website shop")[0]
        self.assertIn("application/vnd.fc.payment+json", shop)
        self.assertIn("function on_l2call(memo, info)", desk.run(self.author.user, "website booklet.lua")[0])
        for bad in ("bakery", "../AGENTS", "index.md x"):
            with self.assertRaisesMessage(desk.DeskError, "the pages of the guide are booklet.lua, guestbook.lua, index"):
                desk.run(self.author.user, f"website {bad}")
        self.assertIn("website", desk.run(self.author.user, "help")[0])
        self.assertIn("WEBSITES", desk.run(self.author.user, "man")[0])

    def test_the_assistant_reads_each_page_of_the_guide_whole(self):
        """The model reads only the start of a long output (fc/aishell.py)."""
        from . import aishell
        for p in desk.WEBSITE.iterdir():
            op = "website " + p.name.removesuffix(".md")
            page = desk.run(self.author.user, op)[0]
            with patch("fc.push.ask", return_value={"ok": {"out": page + "\n", "result": "exit 0"}}):
                read = aishell.Shell(self.author).run("desk " + op)[1]
            self.assertTrue(read.endswith("\n" + page), f"{op}: the model reads to {read[-60:]!r}")

    def test_the_model_reads_what_a_publication_op_prints(self):
        from . import aishell
        for op in ("publish", "unpublish", "visit", "website"):
            self.assertIn(op, desk.READS)
        sh = aishell.Shell(self.author)
        with patch("fc.push.ask", return_value={"ok": {"out": "https://ces.example/link/ana/board/\n",
                                                      "result": "exit 0"}}):
            kind, out, more = sh.run("desk publish board board.lua on " + SERVER)
        self.assertTrue(more, "the model must read the link that publish prints")
        self.assertIn("/link/ana/board/", out)

    def test_the_assistant_reads_of_websites_and_of_a_test_server(self):
        with patch("fc.net.peers", return_value=[]):
            head = kernel._head(self.viewer.user)
            with self.settings(DEBUG=True):
                local = kernel._head(self.viewer.user)
        self.assertIn(kernel.WEBSITES, head)
        self.assertNotIn(kernel.LOCAL_TEST, head)
        self.assertIn(kernel.LOCAL_TEST, local)

    def test_a_visit_reaches_the_newest_instance(self):
        live = [{"pid": 3, "program_key": self.page.program_key, "started_us": 200},
                {"pid": 5, "program_key": self.page.program_key, "started_us": 900},
                {"pid": 7, "program_key": "3" * 64, "started_us": 999},
                {"pid": 4, "program_key": self.page.program_key, "started_us": 500}]
        with patch("fc.bridge.call", return_value={"instances": live}) as call:
            self.assertEqual(LIVE_PID(SERVER, "1" * 64, self.page.source, self.page.program_key), 5)
            self.assertIsNone(LIVE_PID(SERVER, "1" * 64, self.page.source, "4" * 64))
        self.assertEqual(call.call_args.kwargs, {"server": SERVER, "server_key": "1" * 64, "path": self.page.source})

    def test_a_visit_starts_a_website_that_does_not_run(self):
        """CES starts no program again after a restart of its server. So a
        visit starts the program of a published website, as its owner."""
        self.page.source = self.home + "board.lua"
        self.page.save()
        self.live.side_effect = [None, 9]
        starting = launch.LaunchError("CES_ERROR_NOT_LISTENING")
        with patch("fc.bridge.call", return_value={"ok": True, "pid": 9}) as call, \
                patch("fc.webdial.roundtrip", side_effect=[starting, starting, reply()]) as trip, \
                patch("fc.webdial.time.sleep"):
            page = self.client.get("/link/ana/board/")
        self.assertEqual((page.status_code, page.content), (200, b"<p>hi</p>"))
        call.assert_called_once_with("launch", sealed="ana", server=SERVER, path=self.page.source)
        self.assertEqual({c.args[2] for c in trip.call_args_list}, {9})
        self.assertEqual({c.args[0] for c in trip.call_args_list}, {self.viewer})

    def test_a_website_that_does_not_stay_up_starts_once_in_a_while(self):
        """A program that ends at its start, or that CES refuses to launch,
        does not start for each request."""
        self.page.source = self.home + "board.lua"
        self.page.save()
        self.live.return_value = None
        for answer in ({"return_value": {"ok": True, "pid": 9}},
                       {"side_effect": bridge.BridgeError("CES_ERROR_COMPUTE_FUND_TOO_LOW")}):
            cache.clear()
            with patch("fc.bridge.call", **answer) as call, patch("fc.webdial.START", 0.01):
                pages = [self.client.get("/link/ana/board/") for _ in range(3)]
            self.assertEqual([p.status_code for p in pages], [503] * 3)
            self.assertContains(pages[0], "The website is offline", status_code=503)
            self.assertEqual([c.args[0] for c in call.call_args_list], ["launch"])
        cache.clear()
        with patch("fc.bridge.call", return_value={"ok": True, "pid": 9}) as call, patch("fc.webdial.START", 0.01), \
                patch("fc.webdial.REVIVE_HOLD", 0.05):
            self.client.get("/link/ana/board/")
            time.sleep(0.1)
            self.client.get("/link/ana/board/")
        self.assertEqual(len(call.call_args_list), 2)

    def test_a_visit_starts_only_a_file_of_the_owner(self):
        self.live.return_value = None
        for source in ("/p/board.lua", "/h/" + self.viewer.pubkey + "/board.lua", "/s/board.lua"):
            self.page.source = source
            self.page.save()
            with patch("fc.bridge.call") as call:
                self.assertEqual(self.client.get("/link/ana/board/").status_code, 503)
            call.assert_not_called()

    def test_visit_shows_a_page_as_the_visitor_gets_it(self):
        program = Program(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: 11\r\n\r\n<p>hi</p>\r\n")
        with patch("fc.launch.dial", return_value=program.local) as dial:
            text = desk.run(self.author.user, "visit board /posts")[0]
        program.worker.join(5)
        lines = program.sent.split(b"\r\n")
        self.assertEqual(lines[0], b"GET /posts HTTP/1.1")
        self.assertIn(b"Host: site", lines)
        self.assertEqual(dial.call_args.args[1:4], (self.author.sealed, SERVER, 8))
        self.assertTrue(text.startswith("HTTP 200, text/html"), text)
        self.assertIn("<p>hi</p>", text)
        self.assertNotIn("Note:", text)
        rooted = b'<link rel="stylesheet" href="/style.css"><form action=\'/note\'><p style="b: url(/x.png)">'
        program = Program(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: %d\r\n\r\n" % len(rooted)
                          + rooted)
        with patch("fc.launch.dial", return_value=program.local):
            text = desk.run(self.author.user, "visit board")[0]
        program.worker.join(5)
        self.assertIn("Note: this page has addresses with a / at the start", text)
        self.assertIn('href="/', text.split("Note:")[1])
        self.live.return_value = None
        self.assertIn("offline", desk.run(self.author.user, "visit board")[0])
        with self.assertRaisesMessage(desk.DeskError, "no publication"):
            desk.run(self.author.user, "visit nothing")
        # Only your own: another person's page would go into your assistant.
        with self.assertRaises(desk.DeskError):
            desk.run(self.viewer.user, "visit ana/board")
        with self.assertRaisesMessage(desk.DeskError, "no publication"):
            desk.run(self.viewer.user, "visit board")
        with self.assertRaises(desk.DeskError):
            desk.run(self.author.user, "visit board /a/../b")

    def test_binary_http_and_truncated_reply(self):
        for wire, status in [(b"abc\x00", 200), (b"ab", 502)]:
            program = Program(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 4\r\n\r\n" + wire)
            with patch("fc.launch.dial", return_value=program.local):
                page = self.client.post("/link/ana/board/deep?q=1&q=2", b"x\x00z", content_type="text/plain",
                                        HTTP_ORIGIN=ORIGIN, HTTP_REFERER=HERE)
            program.worker.join(5)
            self.assertEqual(page.status_code, status)
            self.assertTrue(program.sent.startswith(b"POST /deep?q=1&q=2 HTTP/1.1\r\n"))
            self.assertTrue(program.sent.endswith(b"\r\n\r\nx\x00z"))
            if status == 200:
                self.assertEqual(page.content, b"abc\x00")

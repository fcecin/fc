"""Payments that websites ask for: the request, fc's review page, the
visitor's decision, and a paid call that fc sends at most once."""
import base64
import json
import os
import socket
import tempfile
import threading
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.utils import timezone

from . import account, bridge, payments, webdial
from .models import Payment, Profile, Publication

SERVER = "net.example:55000"
ORIGIN = "http://testserver"
HERE = ORIGIN + "/link/ana/shop/"


def ask_body(**fields):
    terms = {"order": "booklet-1", "description": "The club booklet", "amount": "200000000",
             "return": "/booklet"}
    terms.update(fields)
    return json.dumps(terms)


class PaymentTests(TestCase):
    def setUp(self):
        self.author = self.person("ana", "a")
        self.viewer = self.person("ben", "b")
        self.page = Publication.objects.create(owner=self.author, name="shop", server=SERVER, server_key="1" * 64,
                                               source="/p/shop.lua", program_key="2" * 64)
        self.client = Client()
        self.client.force_login(self.viewer.user)
        balance = patch("fc.account.balance", return_value=(True, 900000000))
        balance.start()
        self.addCleanup(balance.stop)
        self.calls = []

    def person(self, name, key):
        user = get_user_model().objects.create_user(username=name)
        return Profile.objects.create(user=user, handle=name, pubkey=key * 64, sealed=name, granted=True)

    def site_replies(self, body, method="post", content_type=payments.MEDIA, status=200):
        """The shop's reply to a form post (or a GET) of the visitor."""
        reply = webdial.Reply(status, content_type, [], body.encode() if isinstance(body, str) else body)
        with patch("fc.webdial.live_pid", return_value=8), patch("fc.webdial.roundtrip", return_value=reply):
            if method == "post":
                return self.client.post("/link/ana/shop/buy", {}, HTTP_ORIGIN=ORIGIN, HTTP_REFERER=HERE)
            return self.client.get("/link/ana/shop/buy")

    def bridge(self, call_reply=None, live=None):
        """A fake bridge: the shop runs as pid 8, and a paid call gives
        `call_reply`, or raises it when it is a BridgeError."""
        live = [{"pid": 8, "program_key": self.page.program_key}] if live is None else live

        def fake(op, timeout=20, **args):
            self.calls.append((op, args))
            if op == "instances":
                return {"ok": True, "instances": live}
            if op == "call":
                if isinstance(call_reply, bridge.BridgeError):
                    raise call_reply
                return {"ok": True, "reply_b64": base64.b64encode(call_reply or b"thanks").decode()}
            raise AssertionError(op)
        return patch("fc.bridge.call", side_effect=fake)

    def paid_calls(self):
        return [args for op, args in self.calls if op == "call"]

    def ask(self):
        asked = self.site_replies(ask_body())
        payment = Payment.objects.get()
        self.assertEqual((asked.status_code, asked["Location"]), (303, f"/payments/review?id={payment.pk}"))
        self.assertIn("/payments/review", asked["Content-Security-Policy"])
        return payment

    def decide(self, payment, decision, client=None, **extra):
        return (client or self.client).post(f"/payments/{payment.pk}/{decision}", **extra)

    def state(self, payment):
        return Payment.objects.get(pk=payment.pk).state

    def test_terms(self):
        terms = payments.Terms(ask_body())
        self.assertEqual((terms.order, terms.amount, terms.back), ("booklet-1", 200000000, "/booklet"))
        self.assertEqual(payments.Terms(ask_body(**{"return": "/"})).back, "/")
        self.assertEqual(payments.Terms(ask_body(**{"return": "/a/b?x=1#y"})).back, "/a/b?x=1#y")
        for bad in [{"order": ""}, {"order": "a b"}, {"order": "x" * 65}, {"amount": "0"}, {"amount": "01"},
                    {"amount": 5}, {"amount": "-1"}, {"amount": str(2**47)}, {"description": ""},
                    {"description": "a\nb"}, {"description": "x" * 201}, {"return": "//evil.example/"},
                    {"return": "/\\evil.example/"}, {"return": "https://evil.example/"}, {"return": "/a b"},
                    {"return": "/../account/key"}, {"return": "/%2e%2e/%2e%2e/account"}, {"return": "/a%2Fb"},
                    {"return": "booklet"}, {"return": "/a//b"}]:
            with self.assertRaises(ValueError, msg=bad):
                payments.Terms(ask_body(**bad))
        for body in ["[]", "not json", b"\xff"]:
            with self.assertRaises(ValueError):
                payments.Terms(body)

    def test_only_a_form_post_can_ask(self):
        self.assertEqual(self.site_replies(ask_body(), method="get").status_code, 502)
        self.assertEqual(self.site_replies(ask_body(), status=201).status_code, 502)
        self.assertEqual(self.site_replies("{}", content_type=payments.MEDIA + "; charset=utf-8").status_code, 502)
        self.assertFalse(Payment.objects.exists())
        self.assertEqual(self.site_replies("<p>ok</p>", content_type="text/html").content, b"<p>ok</p>")

    def test_the_same_order_gives_the_same_payment(self):
        payment = self.ask()
        self.assertEqual((payment.payer, payment.payee, payment.amount, payment.site, payment.state),
                         (self.viewer, self.page.program_key, 200000000, "ana/shop", Payment.PENDING))
        self.assertEqual(self.site_replies(ask_body()).status_code, 303)
        self.assertEqual(self.site_replies(ask_body(**{"return": "/other"})).status_code, 303)
        self.assertEqual(self.site_replies(ask_body(amount="300000000")).status_code, 409)
        self.assertEqual(self.site_replies(ask_body(description="Another booklet")).status_code, 409)
        self.assertEqual(Payment.objects.get().back, "/booklet")
        self.site_replies(ask_body(order="booklet-2"))
        self.assertEqual(Payment.objects.count(), 2)

    def test_an_order_of_another_publication_is_not_taken(self):
        """The same program under a second name gives the same payee. Its
        request for a known order does not get the other site's payment."""
        payment = self.ask()
        Publication.objects.create(owner=self.author, name="shop2", server=SERVER, server_key="1" * 64,
                                   source="/p/shop.lua", program_key="2" * 64)
        reply = webdial.Reply(200, payments.MEDIA, [], ask_body().encode())
        with patch("fc.webdial.live_pid", return_value=8), patch("fc.webdial.roundtrip", return_value=reply):
            other = self.client.post("/link/ana/shop2/buy", {}, HTTP_ORIGIN=ORIGIN, HTTP_REFERER=ORIGIN + "/link/ana/shop2/")
        self.assertEqual(other.status_code, 409)
        self.assertEqual(Payment.objects.get().pk, payment.pk)

    def test_the_review_page_belongs_to_the_visitor(self):
        payment = self.ask()
        with self.bridge():
            page = self.client.get(f"/payments/review?id={payment.pk}")
            self.client.head(f"/payments/review?id={payment.pk}")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "The club booklet")
        self.assertContains(page, f'action="/payments/{payment.pk}/approve"')
        self.assertContains(page, 'href="/link/ana/shop/booklet"')
        self.assertIn("form-action 'self'", page["Content-Security-Policy"])
        self.assertEqual(page["X-Frame-Options"], "DENY")
        self.assertEqual(self.paid_calls(), [])
        other = Client()
        other.force_login(self.author.user)
        self.assertEqual(other.get(f"/payments/review?id={payment.pk}").status_code, 404)
        self.assertEqual(self.decide(payment, "approve", other).status_code, 404)
        for bad in ("", "x", "-1", "1" * 30, str(payment.pk + 1)):
            self.assertEqual(self.client.get(f"/payments/review?id={bad}").status_code, 404, bad)
        self.assertEqual(Client().get(f"/payments/review?id={payment.pk}")["Location"], "/")
        self.assertEqual(self.state(payment), Payment.PENDING)

    def test_the_review_shows_the_text_of_the_website_as_text(self):
        self.site_replies(ask_body(description='<b>Free</b> "x" <a href="/leave">'))
        payment = Payment.objects.get()
        page = self.client.get(f"/payments/review?id={payment.pk}")
        self.assertNotContains(page, "<b>Free</b>")
        self.assertContains(page, "&lt;b&gt;Free&lt;/b&gt;")

    def test_decline_sends_nothing_and_goes_back_to_the_website(self):
        payment = self.ask()
        with self.bridge():
            declined = self.decide(payment, "decline")
            again = self.decide(payment, "approve")
        self.assertEqual((declined.status_code, declined["Location"]), (303, "/link/ana/shop/booklet"))
        self.assertEqual(again["Location"], f"/payments/review?id={payment.pk}")
        self.assertEqual(self.state(payment), Payment.DECLINED)
        self.assertEqual(self.paid_calls(), [])
        self.assertContains(self.client.get(f"/payments/review?id={payment.pk}"), "Declined")

    def test_approval_sends_one_paid_call_and_goes_back(self):
        payment = self.ask()
        with self.bridge(call_reply=b"booklet is yours"):
            done = self.decide(payment, "approve")
            again = self.decide(payment, "approve")
        self.assertEqual((done.status_code, done["Location"]), (303, "/link/ana/shop/booklet"))
        self.assertEqual(again["Location"], "/link/ana/shop/booklet")
        stored = Payment.objects.get(pk=payment.pk)
        self.assertEqual((stored.state, stored.reply), ("paid", "booklet is yours"))
        self.assertEqual(self.paid_calls(), [{
            "sealed": "ben", "server": SERVER, "server_key": self.page.server_key, "pid": 8,
            "value": 200000000, "memo_b64": base64.b64encode(b"booklet-1").decode()}])
        self.assertEqual([a for op, a in self.calls if op == "instances"][0],
                         {"server": SERVER, "server_key": self.page.server_key, "path": self.page.source})
        self.assertContains(self.client.get(f"/payments/review?id={payment.pk}"), "Paid.")
        self.assertEqual(self.site_replies(ask_body())["Location"], f"/payments/review?id={payment.pk}")

    def credit(self, raw, arrives=True):
        """The payer's balance on the website's server, `raw`, and a move that
        adds to it (when `arrives`). Patches account.balance and account.move."""
        there = {"raw": raw}

        def balance(profile, server):
            return (True, there["raw"]) if server == SERVER else (True, 10**12)

        def move(profile, src, dst, amount):
            if arrives:
                there["raw"] += amount
        self.addCleanup(patch.stopall)
        patch("fc.account.balance", side_effect=balance).start()
        return patch("fc.account.move", side_effect=move).start()

    def test_enough_credit_on_the_server_moves_nothing(self):
        payment = self.ask()
        move = self.credit(200000000 + payments.FEE_ALLOWANCE)
        with self.bridge():
            self.decide(payment, "approve")
        move.assert_not_called()
        self.assertEqual((self.state(payment), len(self.paid_calls())), (Payment.PAID, 1))

    def test_too_little_credit_moves_what_is_missing_before_the_one_call(self):
        payment = self.ask()
        move = self.credit(190000000)
        page = self.client.get(f"/payments/review?id={payment.pk}")
        self.assertContains(page, "fc first moves 0.11000000 credits of yours from")
        self.assertContains(page, "the server's fee for the call (at most 0.01000000 credits)")
        self.assertContains(page, self.page.program_key)
        with self.bridge():
            done = self.decide(payment, "approve")
        move.assert_called_once_with(self.viewer, account.here(self.viewer), SERVER,
                                     200000000 + payments.FEE_ALLOWANCE - 190000000)
        self.assertEqual(done["Location"], "/link/ana/shop/booklet")
        self.assertEqual((self.state(payment), len(self.paid_calls())), (Payment.PAID, 1))

    def test_a_move_that_fails_or_does_not_arrive_sends_no_call(self):
        payment = self.ask()
        self.credit(0)
        with self.bridge(), patch("fc.account.move", side_effect=account.AccountError("CES_ERROR_UNKNOWN_PEER", "no")):
            self.decide(payment, "approve")
        stored = Payment.objects.get(pk=payment.pk)
        self.assertEqual((stored.state, stored.error), ("failed", "MOVE_CES_ERROR_UNKNOWN_PEER"))
        self.assertContains(self.client.get(f"/payments/review?id={payment.pk}"), "fc could not move credit")
        self.assertEqual(self.paid_calls(), [])
        Payment.objects.all().delete()
        payment = self.ask()
        move = self.credit(0, arrives=False)
        with self.bridge(), patch.object(webdial, "ARRIVAL", 0.3):
            self.decide(payment, "approve")
        stored = Payment.objects.get(pk=payment.pk)
        self.assertEqual((stored.state, stored.error, move.call_count), ("failed", "MOVE_PENDING", 1))
        self.assertContains(self.client.get(f"/payments/review?id={payment.pk}"), "it was not there yet")
        self.assertEqual(self.paid_calls(), [])

    def test_a_refused_call_is_never_sent_again(self):
        """CES can refuse the call for too little credit also after fc moved
        some. fc keeps the failure and sends no second call."""
        payment = self.ask()
        move = self.credit(500000000)
        with self.bridge(call_reply=bridge.BridgeError("CES_ERROR_INSUFFICIENT_BALANCE")):
            self.decide(payment, "approve")
            self.decide(payment, "approve")
        move.assert_not_called()
        stored = Payment.objects.get(pk=payment.pk)
        self.assertEqual((stored.state, stored.error), ("failed", "CES_ERROR_INSUFFICIENT_BALANCE"))
        self.assertEqual(len(self.paid_calls()), 1)

    def test_no_move_from_the_same_server_or_with_an_unknown_balance(self):
        payment = self.ask()
        self.viewer.server = SERVER
        self.viewer.save()
        move = self.credit(0)
        self.assertNotContains(self.client.get(f"/payments/review?id={payment.pk}"), "fc first moves")
        with self.bridge():
            self.decide(payment, "approve")
        move.assert_not_called()
        self.assertEqual(len(self.paid_calls()), 1)
        Payment.objects.all().delete()
        self.calls = []
        self.viewer.server = ""
        self.viewer.save()
        payment = self.ask()
        patch.stopall()
        patch("fc.account.balance", return_value=(False, None)).start()
        move = patch("fc.account.move").start()
        self.assertContains(self.client.get(f"/payments/review?id={payment.pk}"), "fc cannot read your balance")
        with self.bridge():
            self.decide(payment, "approve")
        move.assert_not_called()
        self.assertEqual(len(self.paid_calls()), 1)

    def test_a_lost_result_is_never_sent_again(self):
        for code in ["CES_ERROR_TIMEOUT", "CES_ERROR_CONNECTION_LOST", "BRIDGE_LOST", "BRIDGE_BAD_REPLY"]:
            Payment.objects.all().delete()
            self.calls = []
            payment = self.ask()
            with self.bridge(call_reply=bridge.BridgeError(code)), patch("fc.account.move") as move:
                first = self.decide(payment, "approve")
                self.decide(payment, "approve")
            move.assert_not_called()
            self.assertEqual(first["Location"], f"/payments/review?id={payment.pk}")
            self.assertEqual(self.state(payment), Payment.UNKNOWN)
            self.assertEqual(len(self.paid_calls()), 1)
            self.assertContains(self.client.get(f"/payments/review?id={payment.pk}"), "will not send it again")

    def test_an_approval_during_the_send_sends_nothing(self):
        payment = self.ask()
        seen = []
        with self.bridge() as fake:
            original = fake.side_effect

            def during(op, timeout=20, **args):
                if op == "call":
                    self.decide(payment, "approve")
                    seen.append(self.state(payment))
                return original(op, timeout, **args)
            fake.side_effect = during
            self.decide(payment, "approve")
        self.assertEqual(seen, ["sending"])
        self.assertEqual(len(self.paid_calls()), 1)

    def test_failures_that_move_nothing(self):
        for code in ["CES_ERROR_COMPUTE_INSTANCE_NOT_FOUND", "SERVER_KEY_CHANGED", "BRIDGE_DOWN"]:
            Payment.objects.all().delete()
            payment = self.ask()
            with self.bridge(call_reply=bridge.BridgeError(code)):
                failed = self.decide(payment, "approve")
            stored = Payment.objects.get(pk=payment.pk)
            self.assertEqual((stored.state, stored.error), ("failed", code))
            self.assertEqual(failed["Location"], f"/payments/review?id={payment.pk}")

    def test_an_offline_website_gets_no_call(self):
        payment = self.ask()
        with self.bridge(live=[{"pid": 9, "program_key": "3" * 64}]):
            self.decide(payment, "approve")
        stored = Payment.objects.get(pk=payment.pk)
        self.assertEqual((stored.state, stored.error), ("failed", "WEBSITE_OFFLINE"))
        self.assertEqual(self.paid_calls(), [])

    def test_a_send_that_stopped_is_unknown(self):
        payment = self.ask()
        # fc stopped after it took the approval, before it kept the result.
        Payment.objects.filter(pk=payment.pk).update(state=Payment.SENDING, deadline=timezone.now() - timedelta(seconds=1))
        with self.bridge():
            self.decide(payment, "approve")
        self.assertEqual(self.state(payment), Payment.UNKNOWN)
        self.assertEqual(self.paid_calls(), [])
        self.assertContains(self.client.get("/payments"), "no result came back")
        # A late result replaces the unknown outcome.
        payments.finish(payment, Payment.PAID, reply="late")
        self.assertEqual(self.state(payment), Payment.PAID)

    def test_an_old_request_is_declined(self):
        payment = self.ask()
        Payment.objects.filter(pk=payment.pk).update(
            created=timezone.now() - payments.PENDING_LIFE - timedelta(seconds=1))
        with self.bridge():
            self.decide(payment, "approve")
        self.assertEqual(self.state(payment), Payment.DECLINED)
        self.assertEqual(self.paid_calls(), [])

    def test_a_decision_needs_the_csrf_token(self):
        payment = self.ask()
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.viewer.user)
        with self.bridge():
            self.assertEqual(self.decide(payment, "approve", strict, HTTP_ORIGIN=ORIGIN,
                                         HTTP_REFERER=HERE).status_code, 403)
            self.assertEqual(self.decide(payment, "decline", strict).status_code, 403)
            review = strict.get(f"/payments/review?id={payment.pk}")
            token = review.cookies["csrftoken"].value
            done = self.decide(payment, "decline", strict, HTTP_X_CSRFTOKEN=token)
        self.assertEqual(done.status_code, 303)
        self.assertEqual(self.state(payment), Payment.DECLINED)
        self.assertEqual(self.paid_calls(), [])

    def test_a_withdrawn_website_keeps_its_payments(self):
        payment = self.ask()
        self.page.delete()
        self.assertEqual(Payment.objects.get(pk=payment.pk).site, "ana/shop")
        self.assertContains(self.client.get("/payments"), "The club booklet")
        page = self.client.get(f"/payments/review?id={payment.pk}")
        self.assertNotContains(page, "/link/ana/shop/")
        # A new website with the same name is another website: the decision
        # stays on fc's page.
        Publication.objects.create(owner=self.author, name="shop", server=SERVER, server_key="1" * 64,
                                   source="/p/shop.lua", program_key="2" * 64)
        with self.bridge():
            self.assertEqual(self.decide(payment, "decline")["Location"], f"/payments/review?id={payment.pk}")

    def test_a_stored_return_that_leaves_the_website_is_not_followed(self):
        payment = self.ask()
        Payment.objects.filter(pk=payment.pk).update(back="/../../../account/key")
        with self.bridge():
            self.assertEqual(self.decide(payment, "decline")["Location"], f"/payments/review?id={payment.pk}")


class BridgeFailureTests(TestCase):
    def test_no_bridge_is_down_and_a_lost_exchange_is_lost(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "bridge.sock")
            with self.settings(FC_BRIDGE_SOCKET=path):
                with self.assertRaises(bridge.BridgeError) as down:
                    bridge.call("info", server=SERVER)
                self.assertEqual(down.exception.code, "BRIDGE_DOWN")
                # A bridge that takes the request and never answers.
                listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                listener.bind(path)
                listener.listen(1)
                listener.settimeout(5)
                held = []
                accept = threading.Thread(target=lambda: held.append(listener.accept()[0]))
                accept.start()
                try:
                    with self.assertRaises(bridge.BridgeError) as lost:
                        bridge.call("info", timeout=0.2, server=SERVER)
                    self.assertEqual(lost.exception.code, "BRIDGE_LOST")
                finally:
                    accept.join(5)
                    for s in held:
                        s.close()
                    listener.close()

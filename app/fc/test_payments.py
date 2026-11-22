"""Payments that websites ask for: the request, the visitor's decision, and a
paid call that fc sends at most once."""
import base64
import json
import os
import socket
import tempfile
import threading
from datetime import timedelta
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.http import HttpResponse
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from . import bridge, payments, publications
from .models import Payment, Profile, Publication

HOST = "fc.localhost:8775"
SERVER = "127.0.0.1:57110"
MIDDLEWARE = ["fc.publications.ContentHost"] + [
    m for m in settings.MIDDLEWARE if m != "fc.publications.ContentHost"
]


def ask_body(**fields):
    terms = {"order": "booklet-1", "description": "The club booklet", "amount": "200000000",
             "return": "/booklet"}
    terms.update(fields)
    return json.dumps(terms)


@override_settings(FC_PUBLICATIONS_PORT=8775, ALLOWED_HOSTS=[".fc.localhost"], MIDDLEWARE=MIDDLEWARE)
class PaymentTests(TestCase):
    def setUp(self):
        self.author = self.person("ana", "a")
        self.viewer = self.person("ben", "b")
        self.page = Publication.objects.create(owner=self.author, name="shop", server=SERVER, server_key="1" * 64,
                                               source="/p/shop.lua", program_key="2" * 64)
        self.host = publications.content_host(self.page.pk)
        self.control = Client(HTTP_HOST=HOST)
        self.app = Client(HTTP_HOST=self.host)
        self.control.force_login(self.viewer.user)
        balance = patch("fc.account.balance", return_value=(True, 900000000))
        balance.start()
        self.addCleanup(balance.stop)
        self.calls = []

    def person(self, name, key):
        user = get_user_model().objects.create_user(username=name)
        return Profile.objects.create(user=user, handle=name, pubkey=key * 64, sealed=name, granted=True)

    def visit(self):
        page = self.control.get("/link/ana/shop/")
        self.assertEqual(page.status_code, 200)
        entered = self.app.post(publications.BOOT, {"ticket": page.context["ticket"]},
                                HTTP_ORIGIN="http://" + HOST)
        self.assertEqual(entered.status_code, 303)

    def site_replies(self, body, method="post", content_type=payments.MEDIA):
        reply = HttpResponse(body, content_type=content_type)
        with patch("fc.webdial.exchange", return_value=reply):
            if method == "post":
                return self.app.post("/buy", HTTP_ORIGIN="http://" + self.host)
            return self.app.get("/buy")

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
        self.visit()
        frame = self.site_replies(ask_body())
        self.assertEqual(frame.status_code, 200)
        self.assertIn(b"Approve or decline", frame.content)
        return Payment.objects.get()

    def test_terms(self):
        terms = payments.Terms(ask_body())
        self.assertEqual((terms.order, terms.amount, terms.back), ("booklet-1", 200000000, "/booklet"))
        self.assertEqual(payments.Terms(ask_body(**{"return": "/"})).back, "/")
        for bad in [{"order": ""}, {"order": "a b"}, {"order": "x" * 65}, {"amount": "0"}, {"amount": "01"},
                    {"amount": 5}, {"amount": "-1"}, {"amount": str(2**47)}, {"description": ""},
                    {"description": "a\nb"}, {"description": "x" * 201}, {"return": "//evil.example/"},
                    {"return": "/\\evil.example/"}, {"return": "https://evil.example/"}, {"return": "/a b"}]:
            with self.assertRaises(ValueError, msg=bad):
                payments.Terms(ask_body(**bad))
        for body in ["[]", "not json", b"\xff"]:
            with self.assertRaises(ValueError):
                payments.Terms(body)

    def test_only_a_form_post_can_ask(self):
        self.visit()
        self.assertEqual(self.site_replies(ask_body(), method="get").status_code, 502)
        self.assertEqual(self.site_replies("{}", content_type=payments.MEDIA + "; charset=utf-8").status_code, 502)
        self.assertFalse(Payment.objects.exists())
        self.assertEqual(self.site_replies("<p>ok</p>", content_type="text/html").content, b"<p>ok</p>")

    def test_the_same_order_gives_the_same_payment(self):
        payment = self.ask()
        self.assertEqual((payment.payer, payment.payee, payment.amount, payment.site, payment.state),
                         (self.viewer, self.page.program_key, 200000000, "ana/shop", Payment.PENDING))
        self.assertEqual(self.site_replies(ask_body()).status_code, 200)
        self.assertEqual(self.site_replies(ask_body(**{"return": "/other"})).status_code, 200)
        self.assertEqual(self.site_replies(ask_body(amount="300000000")).status_code, 409)
        self.assertEqual(self.site_replies(ask_body(description="Another booklet")).status_code, 409)
        self.assertEqual(Payment.objects.get().back, "/booklet")
        self.site_replies(ask_body(order="booklet-2"))
        self.assertEqual(Payment.objects.count(), 2)

    def test_open_payments_belong_to_the_visitor(self):
        payment = self.ask()
        listed = self.control.get(f"/payments/site/{self.page.pk}").json()["payments"]
        self.assertEqual([(p["id"], p["amount"], p["state"]) for p in listed],
                         [(payment.pk, "2.00000000", "pending")])
        self.assertEqual(listed[0]["frame"], "http://" + self.host + "/booklet")
        other = Client(HTTP_HOST=HOST)
        other.force_login(self.author.user)
        self.assertEqual(other.get(f"/payments/site/{self.page.pk}").json()["payments"], [])
        self.assertEqual(other.post(f"/payments/{payment.pk}/approve").status_code, 404)
        self.assertEqual(other.get(f"/payments/{payment.pk}").status_code, 404)
        # A content host carries only the website, so fc's payment routes are the website's paths there.
        with patch("fc.webdial.exchange", return_value=HttpResponse("site page")):
            self.assertEqual(self.app.get(f"/payments/site/{self.page.pk}").content, b"site page")
        self.assertEqual(Client(HTTP_HOST=HOST).get(f"/payments/site/{self.page.pk}").status_code, 404)

    def test_decline_sends_nothing(self):
        payment = self.ask()
        with self.bridge():
            self.assertEqual(self.control.post(f"/payments/{payment.pk}/decline").json()["state"], "declined")
            self.assertEqual(self.control.post(f"/payments/{payment.pk}/approve").json()["state"], "declined")
        self.assertEqual(self.paid_calls(), [])
        self.assertIn(b"declined this order", self.site_replies(ask_body()).content)

    def test_approval_sends_one_paid_call(self):
        payment = self.ask()
        with self.bridge(call_reply=b"booklet is yours"):
            done = self.control.post(f"/payments/{payment.pk}/approve").json()
            again = self.control.post(f"/payments/{payment.pk}/approve").json()
        self.assertEqual((done["state"], done["reply"]), ("paid", "booklet is yours"))
        self.assertEqual(again["state"], "paid")
        self.assertEqual(self.paid_calls(), [{
            "sealed": "ben", "server": SERVER, "server_key": self.page.server_key, "pid": 8,
            "value": 200000000, "memo_b64": base64.b64encode(b"booklet-1").decode()}])
        self.assertEqual(self.control.get(f"/payments/site/{self.page.pk}").json()["payments"], [])
        self.assertEqual(self.control.get(f"/payments/{payment.pk}").json()["state"], "paid")
        self.assertIn(b"paid for this order", self.site_replies(ask_body()).content)

    def test_an_approval_during_the_send_sends_nothing(self):
        payment = self.ask()
        seen = []
        with self.bridge() as fake:
            original = fake.side_effect

            def during(op, timeout=20, **args):
                if op == "call":
                    seen.append(self.control.post(f"/payments/{payment.pk}/approve").json()["state"])
                return original(op, timeout, **args)
            fake.side_effect = during
            self.control.post(f"/payments/{payment.pk}/approve")
        self.assertEqual(seen, ["sending"])
        self.assertEqual(len(self.paid_calls()), 1)

    def test_failures_that_move_nothing(self):
        for code in ["CES_ERROR_INSUFFICIENT_BALANCE", "CES_ERROR_COMPUTE_INSTANCE_NOT_FOUND",
                     "SERVER_KEY_CHANGED", "BRIDGE_DOWN"]:
            Payment.objects.all().delete()
            payment = self.ask()
            with self.bridge(call_reply=bridge.BridgeError(code)):
                result = self.control.post(f"/payments/{payment.pk}/approve").json()
            self.assertEqual((result["state"], result["error"]), ("failed", code))

    def test_an_offline_website_gets_no_call(self):
        payment = self.ask()
        with self.bridge(live=[{"pid": 9, "program_key": "3" * 64}]):
            result = self.control.post(f"/payments/{payment.pk}/approve").json()
        self.assertEqual((result["state"], result["error"]), ("failed", "WEBSITE_OFFLINE"))
        self.assertEqual(self.paid_calls(), [])

    def test_a_lost_result_is_unknown_and_never_sent_again(self):
        for code in ["CES_ERROR_TIMEOUT", "CES_ERROR_CONNECTION_LOST", "BRIDGE_LOST", "BRIDGE_BAD_REPLY"]:
            Payment.objects.all().delete()
            self.calls = []
            payment = self.ask()
            with self.bridge(call_reply=bridge.BridgeError(code)):
                self.assertEqual(self.control.post(f"/payments/{payment.pk}/approve").json()["state"], "unknown")
                self.assertEqual(self.control.post(f"/payments/{payment.pk}/approve").json()["state"], "unknown")
            self.assertEqual(len(self.paid_calls()), 1)
            self.assertIn(b"will not send it again", self.site_replies(ask_body()).content)

    def test_a_send_that_stopped_is_unknown(self):
        payment = self.ask()
        # fc stopped after it took the approval, before it kept the result.
        Payment.objects.filter(pk=payment.pk).update(state=Payment.SENDING, deadline=timezone.now() - timedelta(seconds=1))
        with self.bridge():
            self.assertEqual(self.control.post(f"/payments/{payment.pk}/approve").json()["state"], "unknown")
        self.assertEqual(self.paid_calls(), [])
        self.assertEqual(self.control.get(f"/payments/site/{self.page.pk}").json()["payments"], [])
        self.assertContains(self.control.get("/payments"), "no result came back")
        # A late result replaces the unknown outcome.
        payments.finish(payment, Payment.PAID, reply="late")
        self.assertEqual(Payment.objects.get().state, Payment.PAID)

    def test_an_old_request_is_declined(self):
        payment = self.ask()
        Payment.objects.filter(pk=payment.pk).update(
            created=timezone.now() - payments.PENDING_LIFE - timedelta(seconds=1))
        with self.bridge():
            self.assertEqual(self.control.post(f"/payments/{payment.pk}/approve").json()["state"], "declined")
            self.assertEqual(self.control.get(f"/payments/site/{self.page.pk}").json()["payments"], [])
        self.assertEqual(self.paid_calls(), [])

    def test_a_decision_needs_the_csrf_token(self):
        payment = self.ask()
        strict = Client(HTTP_HOST=HOST, enforce_csrf_checks=True)
        strict.force_login(self.viewer.user)
        with self.bridge():
            self.assertEqual(strict.post(f"/payments/{payment.pk}/approve").status_code, 403)
            token = strict.get("/link/ana/shop/").cookies["csrftoken"].value
            done = strict.post(f"/payments/{payment.pk}/decline", HTTP_X_CSRFTOKEN=token)
        self.assertEqual(done.json()["state"], "declined")
        self.assertEqual(self.paid_calls(), [])

    def test_a_withdrawn_website_keeps_its_payments(self):
        payment = self.ask()
        self.page.delete()
        self.assertEqual(Payment.objects.get(pk=payment.pk).site, "ana/shop")
        self.assertContains(self.control.get("/payments"), "The club booklet")


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

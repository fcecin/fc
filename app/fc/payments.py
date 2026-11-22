"""Payments that a website asks for, and the visitor's decision in fc.

A website asks with a reply of type MEDIA to a form post of a visit. fc keeps
the request as a Payment and shows it in its own bar, outside the website's
frame. Only the visitor's approval there sends the paid call, and fc sends it
at most once: the approval changes the payment from pending to sending before
the call goes out, and only one request can make that change."""

import base64
from datetime import timedelta
import json
import re

from django.conf import settings
from django.db import IntegrityError, transaction
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.utils.html import escape
from django.views.decorators.http import require_GET, require_POST

from . import account, bridge, credits, publications, webdial
from .models import Payment

MEDIA = "application/vnd.fc.payment+json"
ORDER = re.compile(r"[A-Za-z0-9._:-]{1,64}")
AMOUNT = re.compile(r"[1-9][0-9]{0,15}")
# A path on the website, with no "//" at its start (that names another host).
BACK = re.compile(r"/(?![/\\])[!-~]{0,511}")
# The paid call waits for the program. The bridge exchange stops after
# CALL_TIMEOUT. A payment that is still sending after SEND_LIMIT has an unknown
# outcome: the process that sent it stopped before it kept the result.
CALL_TIMEOUT = 60
SEND_LIMIT = timedelta(seconds=CALL_TIMEOUT + 60)
# A request that waits this long for a decision is declined, so an old request
# never opens on a later page load.
PENDING_LIFE = timedelta(minutes=10)
# The failures after which the amount did not move: the bridge refused the
# request, or CES refused the call before it took the amount, or gave it back.
NOT_MOVED = {
    "BRIDGE_DOWN", "BAD_SEALED_KEY", "BAD_PID", "BAD_AMOUNT", "BAD_MEMO", "BAD_ADDRESS",
    "ADDRESS_NOT_PUBLIC", "CANNOT_RESOLVE", "CANNOT_CONNECT", "NO_FILE_STORE", "BAD_SERVER_KEY", "SERVER_KEY_CHANGED",
    "CES_ERROR_ORIGIN_NOT_FOUND", "CES_ERROR_INSUFFICIENT_BALANCE", "CES_ERROR_BAD_INPUT",
    "CES_ERROR_COMPUTE_INSTANCE_NOT_FOUND", "CES_ERROR_UNSUPPORTED", "CES_ERROR_UNREACHABLE",
}
# The payments that the visitor can still decide, or that fc sends now.
OPEN = (Payment.PENDING, Payment.SENDING)


class Terms:
    """The payment that a website asks for: a reply body of type MEDIA."""

    def __init__(self, body):
        try:
            data = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            raise ValueError("not JSON")
        if not isinstance(data, dict):
            raise ValueError("not an object")
        order, text, amount = data.get("order"), data.get("description"), data.get("amount")
        back = data.get("return", "/")
        if not isinstance(order, str) or not ORDER.fullmatch(order):
            raise ValueError("bad order")
        if (not isinstance(text, str) or not 0 < len(text) <= 200
                or any(not c.isprintable() for c in text)):
            raise ValueError("bad description")
        if not isinstance(amount, str) or not AMOUNT.fullmatch(amount) or int(amount) > credits.MAX_RAW:
            raise ValueError("bad amount")
        if not isinstance(back, str) or not BACK.fullmatch(back):
            raise ValueError("bad return path")
        self.order, self.description, self.amount, self.back = order, text, int(amount), back


def ask(publication, payer, body):
    """Keep the payment that a website's form post asks for, and give the
    page that the website's frame shows meanwhile. A second request for the
    same order gives the payment that fc keeps already."""
    try:
        terms = Terms(body)
    except ValueError:
        return frame_page(502, "The website asked for a payment that fc cannot read.")
    key = dict(server=publication.server, payee=publication.program_key, payer=payer, order=terms.order)
    fields = dict(publication=publication, site=f"{publication.owner.handle}/{publication.name}",
                  server_key=publication.server_key, source=publication.source,
                  description=terms.description, amount=terms.amount, back=terms.back)
    try:
        with transaction.atomic():
            payment, _ = Payment.objects.get_or_create(**key, defaults=fields)
    except IntegrityError:
        payment = Payment.objects.get(**key)
    if (payment.description, payment.amount) != (terms.description, terms.amount):
        return frame_page(409, "This order is open already with other terms. Ask the website for a new order.")
    state = current(payment).state
    return frame_page(200, {
        Payment.PENDING: "Approve or decline this payment in the fc bar above the website.",
        Payment.SENDING: "fc sends this payment now.",
        Payment.PAID: "You paid for this order already.",
        Payment.DECLINED: "You declined this order. Ask the website for a new order.",
        Payment.FAILED: "This payment did not go through. Ask the website for a new order.",
        Payment.UNKNOWN: "fc sent this payment, and no result came back. fc will not send it again.",
    }[state])


def frame_page(status, text):
    page = ('<!doctype html><meta charset="utf-8"><title>Payment</title>'
            '<p style="font: 1rem system-ui, sans-serif; margin: 2rem">' + escape(text) + "</p>")
    return HttpResponse(page, status=status, content_type="text/html; charset=utf-8")


def current(payment):
    """The payment, declined when it waited too long for a decision, and with
    an unknown outcome when its send passed the deadline."""
    now = timezone.now()
    if payment.state == Payment.PENDING and payment.created < now - PENDING_LIFE:
        Payment.objects.filter(pk=payment.pk, state=Payment.PENDING).update(
            state=Payment.DECLINED, decided=now)
        payment.refresh_from_db()
    if payment.state == Payment.SENDING and payment.deadline and payment.deadline < now:
        Payment.objects.filter(pk=payment.pk, state=Payment.SENDING).update(state=Payment.UNKNOWN)
        payment.refresh_from_db()
    return payment


def send(payment):
    """Send the approved payment: one paid call, as the payer, to the program
    that has the published program key. fc never sends it again."""
    try:
        pid = webdial.live_pid(payment.server, payment.source, payment.payee)
    except bridge.BridgeError as e:
        return finish(payment, Payment.FAILED, error=e.code)
    if pid is None:
        return finish(payment, Payment.FAILED, error="WEBSITE_OFFLINE")
    try:
        r = bridge.call("call", timeout=CALL_TIMEOUT, sealed=payment.payer.sealed, server=payment.server,
                        server_key=payment.server_key, pid=pid, value=payment.amount,
                        memo_b64=base64.b64encode(payment.order.encode()).decode())
    except bridge.BridgeError as e:
        state = Payment.FAILED if e.code in NOT_MOVED else Payment.UNKNOWN
        return finish(payment, state, error=e.code)
    reply = base64.b64decode(r.get("reply_b64", "")).decode("utf-8", "replace")[:1000]
    return finish(payment, Payment.PAID, reply=reply)


def finish(payment, state, reply="", error=""):
    # A known result replaces the unknown outcome that a reader can set when
    # the send is late.
    Payment.objects.filter(pk=payment.pk, state__in=(Payment.SENDING, Payment.UNKNOWN)).update(
        state=state, reply=reply, error=error)
    account.forget(payment.payer, payment.server)


def control(request):
    """The payer of a request on fc's own host, or 404."""
    if not settings.FC_PUBLICATIONS_PORT or request.get_host() != publications.control_host():
        raise Http404
    if not request.user.is_authenticated:
        raise Http404
    return request.user.profile


def mine(request, pk):
    try:
        return Payment.objects.select_related("payer", "publication").get(pk=pk, payer=control(request))
    except Payment.DoesNotExist:
        raise Http404


def as_json(payment):
    payment = current(payment)
    _, balance = account.balance(payment.payer, payment.server)
    frame = None
    if payment.publication_id:
        frame = "http://" + publications.content_host(payment.publication_id) + payment.back
    return {
        "id": payment.pk, "site": payment.site, "server": payment.server, "payee": payment.payee,
        "order": payment.order, "description": payment.description,
        "amount": credits.to_text(payment.amount), "state": payment.state,
        "reply": payment.reply, "error": payment.error, "frame": frame,
        "balance": credits.to_text(balance) if balance is not None else None,
    }


def reply(data):
    response = JsonResponse(data)
    response["Cache-Control"] = "no-store"
    return response


@require_GET
def open_for_site(request, pk):
    """The payments of a website that the visitor can still decide, or that fc
    sends now. The fc page asks each time the website's frame loads a page."""
    payer = control(request)
    payments = Payment.objects.filter(payer=payer, publication_id=pk, state__in=OPEN).order_by("pk")
    data = [as_json(p) for p in payments.select_related("payer")]
    return reply({"payments": [d for d in data if d["state"] in OPEN]})


@require_GET
def status(request, pk):
    """One payment of the visitor, as it is now."""
    return reply(as_json(mine(request, pk)))


@require_POST
def approve(request, pk):
    payment = mine(request, pk)
    now = timezone.now()
    # Only one request can change a pending payment to sending: a second click,
    # tab or retry reads the payment as it is.
    taken = Payment.objects.filter(pk=payment.pk, state=Payment.PENDING,
                                   created__gte=now - PENDING_LIFE).update(
        state=Payment.SENDING, decided=now, deadline=now + SEND_LIMIT)
    if taken:
        payment.refresh_from_db()
        send(payment)
    payment.refresh_from_db()
    return reply(as_json(payment))


@require_POST
def decline(request, pk):
    payment = mine(request, pk)
    Payment.objects.filter(pk=payment.pk, state=Payment.PENDING).update(
        state=Payment.DECLINED, decided=timezone.now())
    payment.refresh_from_db()
    return reply(as_json(payment))


@require_GET
def receipts(request):
    """The visitor's payments, the newest first."""
    payer = control(request)
    payments = [current(p) for p in Payment.objects.filter(payer=payer).order_by("-pk")[:200]]
    response = render(request, "fc/payments.html", {"payments": payments})
    response["Cache-Control"] = "no-store"
    return response

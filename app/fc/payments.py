"""Payments that a website asks for, and the visitor's decision in fc.

A website asks with a reply of type MEDIA to a form post of a visit. fc keeps
the request as a Payment and sends the browser to fc's own review page.
Only the visitor's approval there, a form of fc with fc's CSRF token, sends
the paid call. fc sends it at most once: the approval changes the payment
from pending to sending before the call goes out, and only one request can
make that change."""

import base64
from datetime import timedelta
import json
import re

from django.db import IntegrityError, transaction
from django.http import Http404, HttpResponse, HttpResponseRedirect
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from . import account, bridge, credits, publications, webdial
from .models import Payment

MEDIA = "application/vnd.fc.payment+json"
ORDER = re.compile(r"[A-Za-z0-9._:-]{1,64}")
AMOUNT = re.compile(r"[1-9][0-9]{0,15}")
# A path on the website, with no "//" at its start (that names another host).
BACK = re.compile(r"/(?![/\\])[!-~]{0,511}")
# The paid call waits for the program. The bridge exchange stops after
# CALL_TIMEOUT. Before the call, fc can move credit to the website's server
# (a bridge op of up to MOVE_TIMEOUT) and wait for it there (up to
# account.ARRIVAL). A payment that is still sending after SEND_LIMIT has an
# unknown outcome: the process that sent it stopped before it kept the
# result.
CALL_TIMEOUT = 60
MOVE_TIMEOUT = 20
SEND_LIMIT = timedelta(seconds=MOVE_TIMEOUT + account.ARRIVAL + CALL_TIMEOUT + 60)
# CES takes its fee for a call from the payer before it takes the amount, and
# keeps the fee also when the amount is not there. When fc moves credit for a
# payment, it moves this much more for that fee: the query fee of fc's network
# servers is 0.0008 credit (tools/netconf.py). Raw units: 0.001 credit.
FEE_ALLOWANCE = 100_000
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
    "CES_ERROR_BALANCE_OVERFLOW",
}


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
        if not isinstance(back, str) or not BACK.fullmatch(back) or not publications.website_path(back):
            raise ValueError("bad return path")
        self.order, self.description, self.amount, self.back = order, text, int(amount), back


def review_path(payment):
    return f"{publications.REVIEW}?id={payment.pk}"


def ask(publication, payer, body):
    """Keep the payment that a website's form post asks for, and send the
    browser to fc's review of it. A second request for the same order gives
    the payment that fc keeps already."""
    try:
        terms = Terms(body)
    except ValueError:
        return _text(502, "The website asked for a payment that fc cannot read.")
    key = dict(server=publication.server, payee=publication.program_key, payer=payer, order=terms.order)
    fields = dict(publication=publication, site=f"{publication.owner.handle}/{publication.name}",
                  server_key=publication.server_key, source=publication.source,
                  description=terms.description, amount=terms.amount, back=terms.back)
    try:
        with transaction.atomic():
            payment, _ = Payment.objects.get_or_create(**key, defaults=fields)
    except IntegrityError:
        payment = Payment.objects.get(**key)
    if ((payment.description, payment.amount, payment.publication_id, payment.server_key)
            != (terms.description, terms.amount, publication.pk, publication.server_key)):
        return _text(409, "This order is open already with other terms. Ask the website for a new order.")
    return HttpResponseRedirect(review_path(payment), status=303)


def _text(status, words):
    return HttpResponse(words, status=status, content_type="text/plain; charset=utf-8")


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


def _call(payment, pid):
    return bridge.call("call", timeout=CALL_TIMEOUT, sealed=payment.payer.sealed, server=payment.server,
                       server_key=payment.server_key, pid=pid, value=payment.amount,
                       memo_b64=base64.b64encode(payment.order.encode()).decode())


def shortfall(payment, balance):
    """The credit that fc moves to the website's server before the paid call,
    for a payer who has `balance` raw there: what they lack for the amount
    and the fee. 0 when they have enough there, when that server is their
    own server, or when their balance there is unknown or below 0."""
    if balance is None or balance < 0 or account.here(payment.payer) == payment.server:
        return 0
    return max(0, payment.amount + FEE_ALLOWANCE - balance)


def plan(payment):
    """(the move, the server that it comes from) for the payment now, as the
    review page shows them."""
    account.forget(payment.payer, payment.server)
    _, balance = account.balance(payment.payer, payment.server)
    return shortfall(payment, balance), account.here(payment.payer), balance


def fund(payment, limit, source):
    """Move what the payer lacks on the website's server, and wait for it
    there. `limit` and `source` are the move and its server that the payer
    approved. Returns None when the call can go out, else the error of the
    payment. fc reads the balance before the call and never sends a call
    again, so a refusal of a call is never a reason to move credit."""
    need, home, balance = plan(payment)
    if home != source or need > limit:
        return "MOVE_CHANGED"
    if not need:
        return None
    error = account.fund(payment.payer, payment.server, need, balance + need)
    return f"MOVE_{error}"[:64] if error else None


def send(payment, limit=0, source=None):
    """Send the approved payment: one paid call, as the payer, to the program
    that has the published program key. When the payer lacks credit on the
    website's server, fc moves it there first, at most `limit` from
    `source`, and the call goes out only when it is there. fc never sends
    the call again."""
    try:
        pid = webdial.live_pid(payment.server, payment.server_key, payment.source, payment.payee)
    except bridge.BridgeError as e:
        return finish(payment, Payment.FAILED, error=e.code)
    if pid is None:
        return finish(payment, Payment.FAILED, error="WEBSITE_OFFLINE")
    error = fund(payment, limit, source or account.here(payment.payer))
    if error:
        return finish(payment, Payment.FAILED, error=error)
    try:
        r = _call(payment, pid)
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
    account.forget(payment.payer, payment.server, account.here(payment.payer))


def back_url(payment):
    """The website page after a decision, or None when the publication of the
    payment is gone. A new publication with the same name is another website."""
    page = payment.publication
    if page is None or not publications.website_path(payment.back):
        return None
    return publications.mount(page.owner.handle, page.name) + payment.back


def mine(request, pk):
    """The payment `pk` of the signed-in person, or 404."""
    if not request.user.is_authenticated:
        raise Http404
    try:
        return Payment.objects.select_related("payer", "publication__owner").get(
            pk=pk, payer=request.user.profile)
    except Payment.DoesNotExist:
        raise Http404


@require_GET
def review(request):
    """fc's page for one payment: what the website asks for, the approval and
    the decline while it waits, and the outcome after."""
    if not request.user.is_authenticated:
        return redirect("home")
    pk = request.GET.get("id", "")
    if not (pk.isascii() and pk.isdigit()) or len(pk) > 18:
        raise Http404
    payment = current(mine(request, int(pk)))
    moves, home, balance = plan(payment)
    response = render(request, "fc/pay.html", {
        "p": payment, "back": back_url(payment), "home": home, "balance": balance, "moves": moves,
        "fee": FEE_ALLOWANCE, "changed": request.GET.get("changed") == "1",
    })
    response["Cache-Control"] = "private, no-store"
    return response


def _after(payment, done):
    """Back to the website after the decision `done`, else to the review."""
    back = back_url(payment)
    if payment.state == done and back:
        return HttpResponseRedirect(back, status=303)
    return HttpResponseRedirect(review_path(payment), status=303)


@require_POST
def approve(request, pk):
    payment = current(mine(request, pk))
    # The approval is for the move that the review page showed, from the
    # server that it showed. When the move is now larger or comes from
    # another server, the page opens again with the new figures.
    shown = request.POST.get("move", "0")
    limit = int(shown) if shown.isascii() and shown.isdigit() and len(shown) < 20 else 0
    source = request.POST.get("source", "")
    if payment.state == Payment.PENDING:
        need, home, _ = plan(payment)
        if home != source or need > limit:
            return HttpResponseRedirect(review_path(payment) + "&changed=1", status=303)
    now = timezone.now()
    # Only one request can change a pending payment to sending: a second click,
    # tab or retry reads the payment as it is.
    taken = Payment.objects.filter(pk=payment.pk, state=Payment.PENDING,
                                   created__gte=now - PENDING_LIFE).update(
        state=Payment.SENDING, decided=now, deadline=now + SEND_LIMIT)
    if taken:
        payment.refresh_from_db()
        send(payment, limit, source)
    payment.refresh_from_db()
    return _after(payment, Payment.PAID)


@require_POST
def decline(request, pk):
    payment = mine(request, pk)
    Payment.objects.filter(pk=payment.pk, state=Payment.PENDING).update(
        state=Payment.DECLINED, decided=timezone.now())
    payment.refresh_from_db()
    return _after(payment, Payment.DECLINED)


@require_GET
def receipts(request):
    """The visitor's payments, the newest first."""
    if not request.user.is_authenticated:
        return redirect("home")
    payments = [current(p) for p in Payment.objects.filter(payer=request.user.profile).order_by("-pk")[:200]]
    response = render(request, "fc/payments.html", {"payments": payments})
    response["Cache-Control"] = "private, no-store"
    return response

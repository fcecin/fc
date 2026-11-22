"""Local publication names, browser visits and their dedicated content hosts."""

from datetime import timedelta
import hashlib
import re
import secrets

from django.conf import settings
from django.db import transaction
from django.http import Http404, HttpResponse, HttpResponseRedirect
from django.middleware.csrf import get_token
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.encoding import escape_uri_path
from django.views.decorators.http import require_GET

from . import account, bridge, credits, payments, webdial
from .models import PageVisit, Publication
from .signals import ensure_profile

COOKIE = "fc_page_visit"
BOOT = "/.fc/enter"
SOURCE = re.compile(r"/(?:h|f|s|p)/[!-~]{1,196}")
LOCAL_SERVER = re.compile(r"127\.0\.0\.1:([1-9][0-9]{0,4})")


def control_host():
    return f"fc.localhost:{settings.FC_PUBLICATIONS_PORT}"


def content_host(pk):
    return f"p-{pk}.{control_host()}"


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def publish(profile, name, server, source):
    port = LOCAL_SERVER.fullmatch(server)
    if not port or int(port[1]) > 65535:
        raise ValueError("Use the local CES server's 127.0.0.1:port.")
    if not SOURCE.fullmatch(source) or ".." in source or "//" in source:
        raise ValueError("Give the full CES source path of the running website.")
    server_key = bridge.call("info", server=server)["pubkey"]
    instances = bridge.call("instances", server=server, path=source)["instances"]
    if not instances:
        raise ValueError("Start the website on CES before publishing it.")
    key = instances[0].get("program_key")
    if not key:
        raise ValueError("Rebuild fc-bridge to publish a program key.")
    page, created = Publication.objects.get_or_create(owner=profile, name=name, defaults={
        "source": source, "server": server, "program_key": key, "server_key": server_key,
    })
    if not created and (page.source, page.server, page.program_key, page.server_key) != (source, server, key, server_key):
        raise ValueError("This publication name is already in use.")
    return f"http://{control_host()}/link/{profile.handle}/{name}/"


def publication(request, handle, name):
    if not settings.FC_PUBLICATIONS_PORT or request.get_host() != control_host():
        raise Http404
    return get_object_or_404(Publication, owner__handle=handle, name=name)


# The part of a page that `desk visit` prints, in characters.
VISIT_TEXT = 3500


def visit(profile, page, path):
    """A page of the website as `profile` gets it through the link: the status,
    the content type, and the body as text."""
    pid = webdial.live_pid(page.server, page.source, page.program_key)
    if pid is None:
        return "The website is offline: no instance of its program runs."
    status, content_type, location, data = webdial.roundtrip(
        profile, page, pid, "GET", path, content_host(page.pk),
        {"Connection": "close", "Accept": "text/html,*/*"}, b"")
    head = f"HTTP {status}, {content_type}" + (f", Location: {location}" if location else "")
    text = data.decode("utf-8", "replace")
    if len(text) > VISIT_TEXT:
        text = text[:VISIT_TEXT] + f"\n(the page has {len(text)} characters; this shows the first {VISIT_TEXT})"
    return head + "\n\n" + text


@require_GET
def open_page(request, handle, name, path=""):
    page = publication(request, handle, name)
    if not request.user.is_authenticated:
        request.session["publication_return"] = request.get_full_path()
        return redirect("home")
    if path.startswith("/") or "\\" in path:
        raise Http404
    try:
        viewer = ensure_profile(request.user)
    except bridge.BridgeError:
        return HttpResponse("Your CES key is unavailable.", status=503)
    target = "/" + escape_uri_path(path)
    query = request.META.get("QUERY_STRING")
    if query:
        target += "?" + query
    ticket = secrets.token_urlsafe(32)
    now = timezone.now()
    PageVisit.objects.filter(expires__lt=now).delete()
    PageVisit.objects.create(publication=page, viewer=viewer, session_id=request.session.session_key,
                             ticket=digest(ticket), expires=now + timedelta(minutes=1), target=target)
    origin = "http://" + content_host(page.pk)
    _, balance = account.balance(viewer, page.server)
    # The fc bar asks for the website's open payments and sends the decisions.
    get_token(request)
    response = render(request, "fc/website.html", {
        "name": f"{handle}/{name}", "server": page.server, "site": page.pk,
        "balance": credits.to_text(balance) if balance is not None else "—",
        "bootstrap": origin + BOOT, "ticket": ticket,
    })
    response["Content-Security-Policy"] = (f"default-src 'none'; style-src 'self'; script-src 'self'; "
                                          f"connect-src 'self'; frame-src {origin}; form-action {origin}; "
                                          f"frame-ancestors 'none'")
    response["Referrer-Policy"] = "strict-origin"
    response["Cache-Control"] = "no-store"
    return response


class ContentHost:
    """Dispatch content hosts before Django handles sessions, static app routes or CSRF."""
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        host = request.get_host()
        if not host.split(":")[0].lower().rstrip(".").endswith(".fc.localhost"):
            return self.get_response(request)
        match = re.fullmatch(r"p-([1-9][0-9]{0,17})\." + re.escape(control_host()), host)
        if not match:
            return HttpResponse(status=404)
        response = content(request, int(match[1]))
        response["Content-Security-Policy"] = (
            "default-src 'none'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "form-action 'self'; base-uri 'none'; frame-ancestors http://" + control_host())
        response["Cache-Control"] = "private, no-store"
        response["Referrer-Policy"] = "same-origin"
        response["X-Content-Type-Options"] = "nosniff"
        return response


def content(request, pk):
    now = timezone.now()
    visits = PageVisit.objects.filter(publication_id=pk, expires__gt=now, session__expire_date__gt=now)
    if request.path == BOOT:
        if request.method != "POST" or request.headers.get("Origin") != "http://" + control_host():
            return HttpResponse(status=403)
        token = secrets.token_urlsafe(32)
        with transaction.atomic():
            visit = visits.filter(ticket=digest(request.POST.get("ticket", ""))).first()
            if not visit:
                return HttpResponse("Open the publication link again.", status=403)
            visit.ticket = None
            visit.cookie = digest(token)
            visit.expires = now + timedelta(hours=4)
            visit.save(update_fields=["ticket", "cookie", "expires"])
        response = HttpResponseRedirect(visit.target)
        response.status_code = 303
        response.set_cookie(COOKIE, token, httponly=True, samesite="Lax", max_age=4 * 3600)
        return response
    if request.method not in ("GET", "HEAD", "POST"):
        return HttpResponse(status=405)
    if request.method == "POST" and request.headers.get("Origin") != "http://" + content_host(pk):
        return HttpResponse(status=403)
    visit = visits.select_related("viewer", "publication").filter(
        cookie=digest(request.COOKIES.get(COOKIE, ""))).first()
    if visit is None:
        return HttpResponse("Open the website through its fc link.", status=403)
    response = webdial.exchange(request, visit.viewer, visit.publication)
    if response.get("Content-Type", "").split(";")[0].strip().lower() != payments.MEDIA:
        return response
    # Only a form post can ask for a payment, so that a page load or an image
    # cannot start a purchase.
    if request.method != "POST" or response.status_code != 200:
        return HttpResponse("The website asked for a payment outside a form post.", status=502)
    return payments.ask(visit.publication, visit.viewer, response.content)

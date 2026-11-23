"""One bounded HTTP/1.1 exchange with a website's program, over a dial as the
visitor.

fc makes a new request for the program from a few fields of the browser's
request, and a new response for the browser from a few fields of the
program's reply. No other header goes either way, so the program never
gets fc's cookies, and the browser never gets a cookie of the program."""

import hashlib
from http.client import HTTPConnection, HTTPException
import logging
import socket
import threading
import time
from urllib.parse import urljoin, urlsplit

from django.conf import settings
from django.core.cache import cache
from django.http import HttpResponse

from . import account, bridge, launch, payments, publications
from .models import Publication

log = logging.getLogger("fc.webdial")

MAX_UPLOAD = 1 << 20
MAX_REPLY = 8 << 20
SLOTS = threading.BoundedSemaphore(8)
# The Host of each request to a program.
HOST = "site"
# The credit that fc moves to the website's server for a visitor with none
# there: CES lets a key dial a program only when it has credit on that
# server. Raw units: 0.01 credit.
VISIT_CREDIT = 1_000_000
# fc starts the program of a website again at most once in this time, in
# seconds, so that a program that ends at once does not start for each request.
REVIVE_HOLD = 600
# The seconds that a program has from its start to its first dial.
START = 5
REDIRECTS = (301, 302, 303, 307, 308)
# The types that fc gives to the browser. A text type gets UTF-8.
TYPES = {"text/html", "text/plain", "text/css",
         "image/png", "image/jpeg", "image/gif", "image/webp", "image/x-icon"}


# One lock for each visitor and server, so that the requests of one page in
# this process do not each read the balance and move credit. account.fund
# holds back a second move in the other processes.
FUNDING = [threading.Lock() for _ in range(64)]


class Reply:
    def __init__(self, status, content_type, locations, body):
        self.status, self.content_type, self.locations, self.body = status, content_type, locations, body


def instances(server, server_key, source):
    """The running instances of a file, the newest last."""
    found = bridge.call("instances", server=server, server_key=server_key, path=source)["instances"]
    return sorted(found, key=lambda i: i.get("started_us", 0))


def live_pid(server, server_key, source, program_key):
    """The pid of the program that serves a website: the newest running
    instance of `source` with the published program key, on the server with
    the published server key, or None. An author who launches a new version
    reaches visitors at once."""
    mine = [i["pid"] for i in instances(server, server_key, source) if i.get("program_key") == program_key]
    return mine[-1] if mine else None


def revive(p):
    """Start the program of the publication `p` that does not run, as its
    owner, and return its pid, or None when it does not start. CES starts no
    program again after a restart of its server."""
    if not p.source.startswith(f"/h/{p.owner.pubkey}/"):
        return None
    # 1 while it starts, 0 after it did not. Two names of one file share it.
    hold = "starting:" + hashlib.sha256(f"{p.server} {p.source}".encode()).hexdigest()
    first = cache.add(hold, 1, REVIVE_HOLD)
    pid = launched(p) if first else None
    if first and pid is None:
        cache.set(hold, 0, REVIVE_HOLD)
        return None
    end = time.monotonic() + START
    while cache.get(hold) and time.monotonic() < end:
        keys = {i["pid"]: i.get("program_key") for i in instances(p.server, p.server_key, p.source)}
        mine = [i for i, key in keys.items() if key == p.program_key]
        # A file that was made again has another key, and another worker can
        # start the program at the same moment. One instance stays.
        extra = mine[:-1] + ([pid] if keys.get(pid, p.program_key) != p.program_key else [])
        for i in extra if first else []:
            bridge.call("kill", sealed=p.owner.sealed, server=p.server, server_key=p.server_key, pid=i)
        if mine:
            return mine[-1]
        if pid in extra:
            break
        time.sleep(0.2)
    if first:
        cache.set(hold, 0, REVIVE_HOLD)
    return None


def launched(p):
    """Launch the file of the publication `p` as its owner, and return the
    pid. None when the owner withdrew the link, the published file is not
    there any more, or CES refuses."""
    try:
        # The account of the program key ends with its file.
        account = bridge.call("balance", server=p.server, pubkey=p.program_key)
        if not account.get("exists") or account["balance"] <= 0 or not Publication.objects.filter(pk=p.pk):
            return None
        return bridge.call("launch", sealed=p.owner.sealed, server=p.server, server_key=p.server_key,
                           path=p.source)["pid"]
    except bridge.BridgeError as e:
        log.warning("website %s: its program did not start: %s", p.pk, e.code)
        return None


class NoCredit(Exception):
    """The visitor has no credit on the website's server, and fc could not
    move some there, or the credit is not there yet."""


def fund_visit(profile, server):
    """Give the visitor credit on `server` for a dial: move VISIT_CREDIT of
    theirs from their own server, and wait for it there. fc reads the balance
    before it moves, so a refusal of a dial is never the only reason for a
    move, and it does not move on a balance that it cannot read."""
    home = account.here(profile)
    if home == server:
        raise NoCredit(f"You have no credit on {server}, the website's server.")
    with FUNDING[hash((profile.pubkey, server)) % len(FUNDING)]:
        account.forget(profile, server)
        _, raw = account.balance(profile, server)
        if raw is None:
            raise NoCredit(f"fc cannot read your balance on {server}, the website's server, now. "
                           f"Load the page again in a moment.")
        if raw > 0:
            return   # another request of this visitor moved it
        if raw < 0:
            raise NoCredit(f"Your account on {server}, the website's server, cannot take credit now.")
        error = account.fund(profile, server, VISIT_CREDIT, 1)
    if error == "PENDING":
        raise NoCredit(f"fc moved 0.01 credit of yours from {home} to {server}, the website's server, "
                       f"and it is not there yet. It stays yours. Load the page again in a moment.")
    if error:
        raise NoCredit(f"You have no credit on {server}, the website's server, and fc could not "
                       f"move some there from {home}: {account._why(error)}")


def fetch(profile, publication, method, target, headers, body):
    """The program's reply to one request, or None when the program does not
    run and does not start. The program did not get the request when CES
    refuses the dial. CES does that when the visitor has no credit on the
    program's server, and then fc gives them some, once. It also does that
    while a program starts, and then fc waits."""
    pid = live_pid(publication.server, publication.server_key, publication.source, publication.program_key)
    starts_until = 0
    if pid is None:
        pid = revive(publication)
        if pid is None:
            return None
        starts_until = time.monotonic() + START
    funded = False
    while True:
        try:
            return roundtrip(profile, publication, pid, method, target, headers, body)
        except launch.LaunchError as e:
            if str(e) == "CES_ERROR_INSUFFICIENT_BALANCE" and not funded:
                fund_visit(profile, publication.server)
                funded = True
            elif str(e) == "CES_ERROR_NOT_LISTENING" and time.monotonic() < starts_until:
                time.sleep(0.1)
            else:
                raise


def exchange(request, profile, publication, target):
    """The response for the browser to its request at `target` of the website."""
    if not SLOTS.acquire(blocking=False):
        return _text(503, "All website connections are busy. Try again.")
    try:
        if int(request.META.get("CONTENT_LENGTH") or 0) > MAX_UPLOAD:
            return _text(413, "The form is too large for a website.")
        body = request.read(MAX_UPLOAD + 1)
        if len(body) > MAX_UPLOAD:
            return _text(413, "The form is too large for a website.")
        headers = {"Accept": request.headers.get("Accept", "*/*")[:1000]}
        if request.method == "POST":
            headers["Content-Type"] = request.headers.get("Content-Type", "")[:1000]
        elif body:
            return _text(400, "A GET or HEAD request has no body.")
        # A HEAD goes to the program as a GET, so that the length of the
        # reply is the length of the page. The browser gets no body.
        reply = fetch(profile, publication, "GET" if request.method == "HEAD" else request.method,
                      target, headers, body)
        if reply is None:
            return _text(503, "The website is offline: its program does not run.")
        return _response(reply, request.method, publication, target)
    except NoCredit as e:
        return _text(402, str(e))
    except bridge.BridgeError:
        return _text(503, "The CES website is unavailable now. Try again.")
    except (HTTPException, OSError, ValueError, launch.LaunchError):
        return _text(502, "The CES website did not return a complete HTTP response.")
    finally:
        SLOTS.release()


def _text(status, words):
    return HttpResponse(words, status=status, content_type="text/plain; charset=utf-8")


def _response(reply, method, publication, target):
    """The browser's response from the program's reply to a request at the
    program target `target` (the path and the query)."""
    if reply.status in REDIRECTS:
        location = len(reply.locations) == 1 and redirect_target(
            reply.locations[0], target, publications.mount(publication.owner.handle, publication.name))
        if not location:
            raise ValueError("the program sent a redirect that leaves its website")
        response = HttpResponse(status=reply.status)
        response["Location"] = location
        return response
    if 300 <= reply.status < 400:
        raise ValueError("the program sent a redirect status that fc does not pass")
    media = reply.content_type.split(";")[0].strip().lower()
    if media == payments.MEDIA:
        # fc reads the payment request. It never goes to the browser.
        return HttpResponse(reply.body, status=reply.status, content_type=reply.content_type)
    if not reply.body:
        return HttpResponse(status=reply.status, content_type="text/plain; charset=utf-8")
    if media not in TYPES:
        return _text(502, f"The website sent a type that fc does not give to browsers: {media[:80]}.")
    response = HttpResponse(b"" if method == "HEAD" else reply.body, status=reply.status,
                            content_type=media + ("; charset=utf-8" if media.startswith("text/") else ""))
    if method == "HEAD":
        response["Content-Length"] = str(len(reply.body))
    return response


def redirect_target(location, target, mount):
    """The fc path for a redirect of the program, or None. The program names
    a path of its own website, and fc puts it under the website's mount.
    `target` is the program target of the request (the path and the query),
    for a relative location."""
    if not location or not location.isascii() or any(c <= " " or c == "\x7f" or c == "\\" for c in location):
        return None
    parts = urlsplit(location)
    if parts.scheme or parts.netloc or location.startswith("//"):
        return None
    joined = urlsplit(urljoin(f"http://{HOST}" + target, location))
    if joined.netloc != HOST or not publications.website_path(joined.path):
        return None
    out = mount + joined.path
    # urljoin keeps the query of the request for "?", which clears it.
    if joined.query and not location.split("#")[0].endswith("?"):
        out += "?" + joined.query
    if joined.fragment:
        out += "#" + joined.fragment
    return out


def roundtrip(profile, publication, pid, method, target, headers, body):
    """One HTTP/1.1 request to the program `pid`, over a dial as `profile`.
    Raises ValueError or OSError for a reply with bad framing, too large,
    or too late."""
    with launch.dial(settings.FC_CESH_SOCKET, profile.sealed, publication.server, pid,
                     server_key=publication.server_key) as stream:
        def expire():
            try:
                stream.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        end = time.monotonic() + 15
        deadline = threading.Timer(15, expire)
        conn = HTTPConnection(HOST)
        conn.sock = stream
        deadline.start()
        try:
            conn.request(method, target, body=body or None, headers=dict(headers, Connection="close"))
            with conn.getresponse() as upstream:
                lengths = upstream.headers.get_all("Content-Length", [])
                types = upstream.headers.get_all("Content-Type", [])
                encoding = upstream.getheader("Transfer-Encoding")
                coding = (upstream.getheader("Content-Encoding") or "identity").strip().lower()
                if (len(lengths) > 1 or (lengths and not lengths[0].isascii())
                        or (lengths and not lengths[0].isdigit())
                        or (encoding and (lengths or encoding.lower() != "chunked"))
                        or coding != "identity" or upstream.status < 200 or upstream.status > 599
                        or len(types) > 1):
                    raise ValueError("unsupported HTTP response framing")
                length = upstream.length
                if length is not None and length > MAX_REPLY:
                    raise ValueError("response exceeds limit")
                data = upstream.read(MAX_REPLY + 1)
                if time.monotonic() >= end:
                    raise TimeoutError("website reply timed out")
                if len(data) > MAX_REPLY or (length is not None and len(data) != length):
                    raise ValueError("invalid response length")
                return Reply(upstream.status, types[0] if types else "application/octet-stream",
                             upstream.headers.get_all("Location", []), data)
        finally:
            deadline.cancel()
            conn.close()

"""One bounded HTTP/1.1 exchange with a website's program, over a dial as the
visitor.

fc makes a new request for the program from a few fields of the browser's
request, and a new response for the browser from a few fields of the
program's reply. No other header goes either way, so the program never
gets fc's cookies, and the browser never gets a cookie of the program."""

from http.client import HTTPConnection, HTTPException
import socket
import threading
import time
from urllib.parse import urljoin, urlsplit

from django.conf import settings
from django.http import HttpResponse

from . import account, bridge, launch, payments, publications

MAX_UPLOAD = 1 << 20
MAX_REPLY = 8 << 20
SLOTS = threading.BoundedSemaphore(8)
# The Host of each request to a program.
HOST = "site"
# The credit that fc moves to the website's server for a visitor with none
# there: CES lets a key dial a program only when it has credit on that
# server. Raw units: 0.01 credit.
VISIT_CREDIT = 1_000_000
REDIRECTS = (301, 302, 303, 307, 308)
# The types that fc gives to the browser. A text type gets UTF-8.
TYPES = {"text/html", "text/plain", "text/css",
         "image/png", "image/jpeg", "image/gif", "image/webp", "image/x-icon"}


# How long fc waits for moved credit to arrive at the website's server.
ARRIVAL = 5
# One lock for each visitor and server, so that the requests of one page do
# not each move credit. The locks are in one process: another worker can still
# move once more, which costs the visitor one more VISIT_CREDIT at most.
FUNDING = [threading.Lock() for _ in range(64)]


class Reply:
    def __init__(self, status, content_type, locations, body):
        self.status, self.content_type, self.locations, self.body = status, content_type, locations, body


def live_pid(server, server_key, source, program_key):
    """The pid of the program that serves a website: the newest running
    instance of `source` with the published program key, on the server with
    the published server key, or None. An author who launches a new version
    reaches visitors at once."""
    instances = bridge.call("instances", server=server, server_key=server_key, path=source)["instances"]
    mine = [i for i in instances if i.get("program_key") == program_key]
    return max(mine, key=lambda i: i.get("started_us", 0))["pid"] if mine else None


class NoCredit(Exception):
    """The visitor has no credit on the website's server, and fc could not
    move some there, or the credit is not there yet."""


def fund_visit(profile, server):
    """Give the visitor credit on `server` for a dial: move VISIT_CREDIT of
    theirs from their own server, and wait for it there. fc reads the balance
    before it moves, so a refusal of a dial is never the only reason for a
    move."""
    home = account.here(profile)
    if home == server:
        raise NoCredit(f"You have no credit on {server}, the website's server.")
    with FUNDING[hash((profile.pubkey, server)) % len(FUNDING)]:
        account.forget(profile, server)
        _, raw = account.balance(profile, server)
        if raw is not None and raw > 0:
            return   # another request of this visitor moved it
        try:
            account.move(profile, home, server, VISIT_CREDIT)
        except account.AccountError as e:
            raise NoCredit(f"You have no credit on {server}, the website's server, and fc could not "
                           f"move some there from {home}: {e.message}")
        if not account.arrives(profile, server, 1, ARRIVAL):
            raise NoCredit(f"fc moved 0.01 credit of yours from {home} to {server}, the website's server, "
                           f"and it is not there yet. It stays yours. Load the page again in a moment.")


def fetch(profile, publication, method, target, headers, body):
    """The program's reply to one request, or None when the program does not
    run. CES lets a key dial only with credit on the program's server. When
    CES refuses the dial for that reason, the program did not get the
    request, so fc gives the visitor credit there and dials once more."""
    pid = live_pid(publication.server, publication.server_key, publication.source, publication.program_key)
    if pid is None:
        return None
    try:
        return roundtrip(profile, publication, pid, method, target, headers, body)
    except launch.LaunchError as e:
        if str(e) != "CES_ERROR_INSUFFICIENT_BALANCE":
            raise
    fund_visit(profile, publication.server)
    return roundtrip(profile, publication, pid, method, target, headers, body)


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
    if joined.query:
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

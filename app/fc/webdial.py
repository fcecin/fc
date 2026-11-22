"""One bounded HTTP/1.1 exchange over a visitor's CesPlex dial."""

from http.client import HTTPConnection, HTTPException
import socket
import threading
import time
from urllib.parse import urljoin, urlsplit

from django.conf import settings
from django.http import HttpResponse

from . import bridge, launch

MAX_UPLOAD = 1 << 20
MAX_REPLY = 8 << 20
SLOTS = threading.BoundedSemaphore(8)


def live_pid(server, source, program_key):
    """The pid of the program that serves a website: the newest running
    instance of `source` with the published program key, or None. An author
    who launches a new version reaches visitors at once."""
    instances = bridge.call("instances", server=server, path=source)["instances"]
    mine = [i for i in instances if i.get("program_key") == program_key]
    return max(mine, key=lambda i: i.get("started_us", 0))["pid"] if mine else None


def exchange(request, profile, publication):
    if not SLOTS.acquire(blocking=False):
        return HttpResponse("All website connections are busy.", status=503)
    try:
        pid = live_pid(publication.server, publication.source, publication.program_key)
        if pid is None:
            return HttpResponse("The published website is offline.", status=503)
        return _exchange(request, profile, publication, pid)
    except bridge.BridgeError:
        return HttpResponse("The CES website is unavailable.", status=503)
    except (HTTPException, OSError, ValueError, launch.LaunchError):
        return HttpResponse("The CES website did not return a complete HTTP response.", status=502)
    finally:
        SLOTS.release()


def _exchange(request, profile, publication, pid):
    if int(request.META.get("CONTENT_LENGTH") or 0) > MAX_UPLOAD:
        return HttpResponse(status=413)
    body = request.read(MAX_UPLOAD + 1)
    if len(body) > MAX_UPLOAD:
        return HttpResponse(status=413)
    headers = {"Connection": "close", "Accept": request.headers.get("Accept", "*/*")}
    if request.method == "POST":
        headers["Content-Type"] = request.headers.get("Content-Type", "application/octet-stream")
    elif body:
        return HttpResponse(status=400)

    host = request.get_host()
    status, content_type, location, data = roundtrip(
        profile, publication, pid, request.method, request.get_full_path(), host, headers, body)
    response = HttpResponse(data, status=status, content_type=content_type)
    if 300 <= status < 400 and location:
        location = urljoin(request.build_absolute_uri(), location)
        dest = urlsplit(location)
        if dest.scheme != request.scheme or dest.netloc != host or "\\" in location:
            raise ValueError("redirect leaves app")
        response["Location"] = location
    return response


def roundtrip(profile, publication, pid, method, target, host, headers, body):
    """One HTTP/1.1 request to the program `pid`, over a dial as `profile`.
    Returns (status, content type, Location or None, body). Raises ValueError
    or OSError for a reply with bad framing, too large, or too late."""
    with launch.dial(settings.FC_CESH_SOCKET, profile.sealed, publication.server, pid,
                     server_key=publication.server_key) as stream:
        def expire():
            try:
                stream.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        end = time.monotonic() + 15
        deadline = threading.Timer(15, expire)
        conn = HTTPConnection(host)
        conn.sock = stream
        deadline.start()
        try:
            conn.request(method, target, body=body or None, headers=headers)
            with conn.getresponse() as upstream:
                lengths = upstream.headers.get_all("Content-Length", [])
                encoding = upstream.getheader("Transfer-Encoding")
                if (len(lengths) > 1 or (lengths and not lengths[0].isascii())
                        or (lengths and not lengths[0].isdigit())
                        or (encoding and (lengths or encoding.lower() != "chunked"))
                        or upstream.status < 200):
                    raise ValueError("unsupported HTTP response framing")
                length = upstream.length
                if length is not None and length > MAX_REPLY:
                    raise ValueError("response exceeds limit")
                data = upstream.read(MAX_REPLY + 1)
                if time.monotonic() >= end:
                    raise TimeoutError("website reply timed out")
                if len(data) > MAX_REPLY or (length is not None and len(data) != length):
                    raise ValueError("invalid response length")
                return (upstream.status, upstream.getheader("Content-Type", "application/octet-stream"),
                        upstream.getheader("Location"), data)
        finally:
            deadline.cancel()
            conn.close()

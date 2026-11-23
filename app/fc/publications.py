"""Websites: CES programs that fc serves at /link/<handle>/<name>/.

For each request, fc dials the program as the signed-in visitor, and gives
the program's reply to the browser as the page. The pages share fc's origin
with fc's own pages. So each reply carries a policy: the page runs no
scripts, loads styles and images only from its own website, and sends forms
only to its own website and to fc's payment review. A form post must also
come from a page of the same website. fc checks that before it dials."""

import re
from urllib.parse import unquote, urlsplit

from django.conf import settings
from django.http import HttpResponse, HttpResponseRedirect
from django.utils.encoding import escape_uri_path
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from . import bridge, middleware, payments, webdial
from .models import Publication
from .signals import ensure_profile

SOURCE = re.compile(r"/(?:h|f|s|p)/[!-~]{1,196}")
PROGRAM_KEY = re.compile(r"[0-9a-f]{64}")
QUERY = re.compile(r"[!-~]*")
# A saved return path after the sign-in: a page of a website.
RETURN = re.compile(r"/link/[-a-zA-Z0-9_]+/[-a-zA-Z0-9_]+/[!-~]*")
# The types of a form post.
FORM_TYPES = ("application/x-www-form-urlencoded", "multipart/form-data", "text/plain")
# The path of fc's payment review. A form post of a website can go there by
# fc's redirect, so the website's policy allows it.
REVIEW = "/payments/review"


def mount(handle, name):
    """The path of a website on fc, with no slash at the end."""
    return f"/link/{handle}/{name}"


def link(handle, name):
    """The link of a website, as `desk publish` prints it."""
    return settings.FC_URL + mount(handle, name) + "/"


def segments_ok(segments):
    """True when the decoded segments of a path are safe to pass on. A path
    has no empty segment before its last, no "." or ".." segment, and no
    slash, backslash or control character in a segment."""
    for i, s in enumerate(segments):
        if s in (".", "..") or (s == "" and i < len(segments) - 1):
            return False
        if "/" in s or "\\" in s or any(ord(c) < 32 or ord(c) == 127 for c in s):
            return False
    return True


def website_path(path):
    """True for a path of a website in its encoded form, such as "/a/b?x=1":
    one slash at the start, and segments that `segments_ok` accepts."""
    if not path.startswith("/") or not path.isascii() or any(c <= " " or c == "\x7f" for c in path):
        return False
    parts = urlsplit(path)
    if parts.scheme or parts.netloc:
        return False
    return segments_ok([unquote(s) for s in parts.path[1:].split("/")])


def return_path(value):
    """The page to open after the sign-in, or None."""
    if isinstance(value, str) and RETURN.fullmatch(value):
        handle, name, rest = value.split("/", 4)[2:]
        if website_path("/" + rest):
            return value
    return None


def publish(profile, name, server, source):
    """Publish the newest running instance of `source` on `server` as the
    website `name`. The publication keeps the server key and the program
    key, so each visit goes only to that server and that program."""
    if not SOURCE.fullmatch(source) or ".." in source or "//" in source:
        raise ValueError("Give the full CES source path of the running website.")
    server_key = bridge.call("info", server=server)["pubkey"]
    running = bridge.call("instances", server=server, server_key=server_key, path=source)["instances"]
    running = [i for i in running if PROGRAM_KEY.fullmatch(str(i.get("program_key", "")))
               and int(i["program_key"], 16)]
    if not running:
        raise ValueError("Start the website on CES before publishing it.")
    keys = {i["program_key"] for i in running}
    if len(keys) > 1:
        # A file that was made again has a new program key, and its old
        # instances keep the old one.
        raise ValueError("Instances of this file run with different program keys. Stop the old ones first.")
    key = keys.pop()
    page, created = Publication.objects.get_or_create(owner=profile, name=name, defaults={
        "source": source, "server": server, "program_key": key, "server_key": server_key,
    })
    if not created and (page.source, page.server, page.program_key, page.server_key) != (source, server, key, server_key):
        raise ValueError("This publication name is already in use.")
    return link(profile.handle, name)


# The part of a page that `desk visit` prints, in characters.
VISIT_TEXT = 3500
# An address of a page or a stylesheet with a "/" at its start. In a website
# it goes to the site itself, so the browser refuses it.
ROOTED = re.compile(r"""\b(?:href|src|action|formaction|poster)\s*=\s*["']?/|url\(\s*["']?/""", re.I)


def visit(profile, page, path):
    """A page of the website as `profile` gets it through the link: the status,
    the content type, and the body as text. A note follows a page with
    addresses that leave the website, because the author must change them."""
    if not website_path(path):
        raise ValueError("fc does not pass this path to a website.")
    if not webdial.SLOTS.acquire(blocking=False):
        raise ValueError("All website connections are busy. Try again.")
    try:
        reply = webdial.fetch(profile, page, "GET", path, {"Accept": "text/html,*/*"}, b"")
    finally:
        webdial.SLOTS.release()
    if reply is None:
        return "The website is offline: no instance of its program runs."
    head = f"HTTP {reply.status}, {reply.content_type}" + (
        f", Location: {reply.locations[0]}" if reply.locations else "")
    text = reply.body.decode("utf-8", "replace")
    rooted = [m.group(0) for m in ROOTED.finditer(text)][:3]
    if len(text) > VISIT_TEXT:
        text = text[:VISIT_TEXT] + f"\n(the page has {len(text)} characters; this shows the first {VISIT_TEXT})"
    if rooted:
        text += ("\n\nNote: this page has addresses with a / at the start, such as " + ", ".join(rooted)
                 + ". They go to the site itself, out of the website. A link there leaves the website, and "
                   "the browser does not load a stylesheet or an image there or send a form there. "
                   "Write them relative, such as href=\"style.css\" and action=\"buy\".")
    return head + "\n\n" + text


def origin(request):
    return f"{request.scheme}://{request.get_host()}"


def protect(request, response, page=None):
    """Put the policy of a website page on a response to a request under
    /link/. Without a publication, the policy allows nothing."""
    if page is not None:
        here = origin(request) + mount(page.owner.handle, page.name) + "/"
        policy = (f"default-src 'none'; sandbox allow-forms allow-same-origin; script-src 'none'; "
                  f"style-src 'unsafe-inline' {here}; img-src {here} data:; connect-src 'none'; "
                  f"object-src 'none'; frame-src 'none'; worker-src 'none'; base-uri 'none'; "
                  f"form-action {here} {origin(request)}{REVIEW}; frame-ancestors 'none'")
    else:
        policy = middleware.LINK_POLICY
    response["Content-Security-Policy"] = policy
    response["X-Content-Type-Options"] = "nosniff"
    response["Referrer-Policy"] = "same-origin"
    response["Cache-Control"] = "private, no-store"
    response["X-Frame-Options"] = "DENY"
    response["X-DNS-Prefetch-Control"] = "off"
    return response


def text(status, words):
    return HttpResponse(words, status=status, content_type="text/plain; charset=utf-8")


def referred_by(request, page):
    """True when the page that made the request is a page of the website
    `page`. The browser sends the address of that page. Another website on
    fc has the same origin, so the address must be in this website."""
    try:
        referer = urlsplit(request.headers.get("Referer", ""))
    except ValueError:
        return False
    if f"{referer.scheme}://{referer.netloc}" != origin(request):
        return False
    # Split before decoding, so that an encoded "/" stays in its segment,
    # where segments_ok refuses it.
    parts = [unquote(x) for x in referer.path.split("/")]
    return (len(parts) >= 5 and parts[:4] == ["", "link", page.owner.handle, page.name]
            and segments_ok(parts[4:]))


def from_the_website(request, page):
    """True when a form post comes from a page of the website `page`. A form
    post is a navigation of the page. A ping or a fetch is not."""
    return (request.headers.get("Origin") == origin(request)
            and request.headers.get("Sec-Fetch-Site", "same-origin") == "same-origin"
            and request.headers.get("Sec-Fetch-Mode", "navigate") == "navigate"
            and request.headers.get("Sec-Fetch-Dest", "document") == "document"
            and referred_by(request, page))


def started_by_the_visitor(request):
    """True for a navigation that the visitor starts, such as a link, a typed
    address or a bookmark. An image, a stylesheet, a prefetch and a
    prerender are not: the page that has them makes them. A browser that
    does not tell sends no Sec-Fetch-Mode."""
    if request.headers.get("Sec-Purpose") or request.headers.get("Purpose"):
        return False
    return request.headers.get("Sec-Fetch-Mode", "navigate") == "navigate"


@csrf_exempt
@require_http_methods(["GET", "HEAD", "POST"])
def slashless(request, handle, name):
    """A link with no slash at the end. The relative links of a page need the
    slash, so fc adds it. A form post goes to a path with the slash."""
    if request.method == "POST":
        return protect(request, text(405, "Send the form to a path of the website."))
    target = mount(handle, name) + "/"
    query = request.META.get("QUERY_STRING", "")
    if query:
        target += "?" + query
    return protect(request, HttpResponseRedirect(target))


@csrf_exempt
@require_http_methods(["GET", "HEAD", "POST"])
def open_page(request, handle, name, path=""):
    """One request of a visitor to a website. A form post has no CSRF token of
    fc: `from_the_website` checks it in place of the token."""
    page = Publication.objects.select_related("owner").filter(owner__handle=handle, name=name).first()
    if page is None:
        return protect(request, text(404, "There is no website at this link."))
    if not request.user.is_authenticated:
        back = request.get_full_path() if request.method != "POST" else mount(handle, name) + "/"
        request.session["publication_return"] = back if return_path(back) else None
        return protect(request, HttpResponseRedirect("/", status=303), page)
    query = request.META.get("QUERY_STRING", "")
    if not segments_ok(path.split("/")) or not QUERY.fullmatch(query):
        return protect(request, text(400, "fc does not pass this path to a website."), page)
    if request.method == "POST":
        if not from_the_website(request, page):
            return protect(request, text(403, "A form of another page sent this. Open the website and send it again."), page)
        if request.content_type not in FORM_TYPES:
            return protect(request, text(415, "fc passes only form posts to a website."), page)
    elif not started_by_the_visitor(request) and not referred_by(request, page):
        # A page of another website cannot load this website as its visitor.
        return protect(request, text(403, "Only a page of this website can load this."), page)
    try:
        viewer = ensure_profile(request.user)
    except bridge.BridgeError:
        return protect(request, text(503, "Your CES key is unavailable now. Try again."), page)
    target = "/" + escape_uri_path(path) + ("?" + query if query else "")
    response = webdial.exchange(request, viewer, page, target)
    if response.get("Content-Type", "").split(";")[0].strip().lower() == payments.MEDIA:
        # Only a form post can ask for a payment, so that a page load or an
        # image cannot start a purchase.
        if request.method != "POST" or response.status_code != 200:
            response = text(502, "The website asked for a payment outside a form post.")
        else:
            response = payments.ask(page, viewer, response.content)
    return protect(request, response, page)

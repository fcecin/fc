"""The fc API, which the pages call with the session cookie and the CSRF
token. Bodies and replies are JSON, and amounts are credit text, as "0.01"."""

import hmac
import json
import time
from functools import wraps

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.http import Http404, JsonResponse, StreamingHttpResponse
from django.middleware.csrf import CsrfViewMiddleware
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from . import account, bridge, credits, kernel, net, push
from . import hub as hubmod
from . import desk as deskmod
from .forms import clean_handle_value
from .models import ChatLine, Profile

UNSAFE = {"POST", "PUT", "DELETE", "PATCH"}


class ApiError(Exception):
    def __init__(self, code, message, status=400):
        super().__init__(code)
        self.code, self.message, self.status = code, message, status


def _err(code, message, status=400):
    return JsonResponse({"error": code, "message": message}, status=status)


def _csrf_ok(request):
    return CsrfViewMiddleware(lambda r: None).process_view(request, None, (), {}) is None


SEEN_SECS = 600
# Model wakes from shell jobs (hub_wake).
MAX_WAKES = 30          # in an hour
WAKE_MAX = 8192         # the bytes of a job's report
TOO_MANY_WAKES = "The jobs of your shell woke the assistant {} times in this hour. The site let this one go."


def endpoint(methods, auth=True):
    """Checks the session, limits the rate, parses JSON, and turns errors into
    replies."""
    def deco(view):
        @csrf_exempt
        @wraps(view)
        def wrapped(request, *a, **kw):
            if request.method not in methods:
                return _err("METHOD", f"Use {' or '.join(methods)}.", 405)
            if request.user.is_authenticated:
                if request.method in UNSAFE and not _csrf_ok(request):
                    return _err("CSRF", "The CSRF token is missing or wrong.", 403)
            elif auth:
                return _err("NOT_LOGGED_IN", "Log in.", 401)
            if request.user.is_authenticated:
                # When the person was last seen, written at most once per SEEN_SECS.
                if cache.add(f"seen:{request.user.pk}", 1, SEEN_SECS):
                    Profile.objects.filter(user=request.user).update(seen=timezone.now())
                bucket = f"api:{request.user.pk}:{int(time.time() // 60)}"
                n = cache.get_or_set(bucket, 0, 120)
                if n >= settings.FC_API_RATE:
                    return _err("RATE_LIMITED", "Too many calls. Wait one minute.", 429)
                cache.incr(bucket)
            body = {}
            if request.method in UNSAFE and request.body:
                try:
                    body = json.loads(request.body)
                except ValueError:
                    return _err("BAD_JSON", "The body is not valid JSON.")
                if not isinstance(body, dict):
                    return _err("BAD_JSON", "The body is not a JSON object.")
            try:
                return view(request, body, *a, **kw)
            except ApiError as e:
                return _err(e.code, e.message, e.status)
            except Http404:
                return _err("NOT_FOUND", "Nothing is at this address.", 404)
            except account.AccountError as e:
                return _err(e.code, e.message)
            except kernel.ChatError as e:
                return _err(e.code, e.message, e.status)
            except bridge.BridgeError as e:
                if e.code.startswith("BRIDGE"):
                    return _err(e.code, "The bridge to CES is not running.", 502)
                return _err(e.code, f"CES refused the operation ({e.code}).")
        return wrapped
    return deco


def profile_of(request):
    p = Profile.objects.filter(user=request.user).first()
    if p is None:
        raise ApiError("NO_KEY", "The key of the account is not ready. Open the site once.", 409)
    return p


def need_handle(request):
    p = profile_of(request)
    if not p.handle:
        raise ApiError("NO_HANDLE", "Choose a handle first (POST /api/me {\"handle\": ...}).", 409)
    return p


def amount(body, key, default=None):
    v = body.get(key)
    if v in (None, ""):
        return default
    raw = credits.parse(str(v))
    if raw is None:
        raise ApiError("BAD_AMOUNT", f"Give {key} as credit text, as \"0.01\".")
    return raw


# --- The account -----------------------------------------------------------

@endpoint({"GET", "POST"})
def me(request, body):
    p = profile_of(request)
    if request.method == "POST" and "handle" in body:
        if p.handle:
            raise ApiError("HAS_HANDLE", "The handle cannot change.")
        try:
            p.handle = clean_handle_value(body.get("handle"))
        except Exception as e:
            raise ApiError("BAD_HANDLE", " ".join(getattr(e, "messages", [str(e)])))
        p.save(update_fields=["handle"])
    if request.method == "POST" and "server" in body:
        account.travel(p, str(body["server"]))
    return JsonResponse(me_json(p))


def me_json(p):
    server = account.here(p)
    exists, bal = account.balance(p, server)
    return {"handle": p.handle, "pubkey": p.pubkey, "server": server,
            "ledger": server == settings.FC_LEDGER, "account": exists,
            "balance": credits.to_text(bal) if bal is not None else None}


# --- The network and credit moves -------------------------------------

@endpoint({"GET"}, auth=False)
def network(request, body):
    """The server where you are and its neighbors, each with your balance
    there when you are a person. ?fresh=ADDRESS reads one server of the view
    again now, with the balance."""
    p = Profile.objects.filter(user=request.user).first() if request.user.is_authenticated else None
    here = account.here(p) if p else settings.FC_LEDGER
    fresh = request.GET.get("fresh")
    if fresh:
        if p is None:
            raise ApiError("NOT_LOGGED_IN", "Log in to ask a server again.", 401)
        if fresh != here and fresh not in net.peers(here):
            raise ApiError("NOT_IN_VIEW", "Ask only a server in your view.")
        net.refresh(fresh)
        account.forget(p, fresh)
    g = net.around(here)
    for n in g["nodes"]:
        if p is not None and n["up"]:
            exists, bal = account.balance(p, n["address"])
            n["account"] = exists
            n["balance"] = credits.to_text(bal) if bal is not None else None
    g["here"] = g["nodes"][0]["address"]
    return JsonResponse(g)


@endpoint({"POST"})
def send(request, body):
    """Sends credit to a person (handle) or a key, on your server."""
    p = need_handle(request)
    to = account.recipient(body.get("to"))
    if to is None:
        raise ApiError("BAD_RECIPIENT", "Give a handle on this site or a 64-hex key.")
    raw = amount(body, "amount")
    if not raw:
        raise ApiError("BAD_AMOUNT", "Give amount as credit text, as \"0.5\".")
    account.send(p, to, raw)
    return JsonResponse(me_json(p))


@endpoint({"POST"})
def move(request, body):
    """Moves your credit between two neighbor servers. With no from_server,
    from your server. With no to_server, to your server."""
    p = need_handle(request)
    raw = amount(body, "amount")
    if not raw:
        raise ApiError("BAD_AMOUNT", "Give amount as credit text, as \"0.5\".")
    here = account.here(p)
    account.move(p, str(body.get("from_server") or here), str(body.get("to_server") or here), raw)
    return JsonResponse(me_json(p))


# --- The desktop --------------------------------------------------------------

@endpoint({"GET", "POST"})
def desk(request, body):
    """GET gives the desktop (its revision and widgets), and GET ?since=REV
    gives it only if it changed since REV, else only the revision.
    POST {"op": OP} runs one desk op (fc/desk.py), and replies with its
    output and the desktop after it."""
    need_handle(request)
    u = request.user
    if request.method == "GET":
        raw = request.GET.get("since")
        if raw in (None, ""):
            return JsonResponse(deskmod.state(u))
        try:
            rev = int(raw)
        except ValueError:
            raise ApiError("BAD_SINCE", "Give since as a number.")
        return JsonResponse(deskmod.since(u, rev))
    op = body.get("op")
    if not isinstance(op, str):
        raise ApiError("BAD_OP", 'Give op as text, as "move chat 10 135".')
    try:
        out, _ = deskmod.run(u, op)
    except deskmod.DeskError as e:
        raise ApiError("DESK", str(e))
    return JsonResponse({"out": out, **deskmod.state(u)})


@csrf_exempt
def hub_desk(request):
    """A desk op from a person's shell, sent by the hub (shell_desk in
    fc/hub.py) as POST {"uid", "op", "exp"}, signed with the tickets' key in
    the X-Fc-Hub header. Replies {"out": T} or {"error": T}."""
    if request.method != "POST":
        return _err("METHOD", "Use POST.", 405)
    body = request.body.decode(errors="replace")
    good = hubmod.sign(hubmod.key(settings.SECRET_KEY), body)
    if not hmac.compare_digest(request.headers.get("X-Fc-Hub", ""), good):
        return _err("BAD_SIGNATURE", "Only the hub sends this.", 403)
    try:
        m = json.loads(body)
        uid, op, exp = int(m["uid"]), m["op"], int(m["exp"])
    except (ValueError, KeyError, TypeError):
        return _err("BAD_JSON", "Give uid, op and exp.")
    if exp < time.time() or not isinstance(op, str):
        return _err("EXPIRED", "The request is too old.")
    user = get_user_model().objects.filter(pk=uid).first()
    if user is None:
        return JsonResponse({"error": "no such person"})
    try:
        out, _ = deskmod.run(user, op)
    except deskmod.DeskError as e:
        return JsonResponse({"error": str(e)})
    return JsonResponse({"out": out})


@csrf_exempt
def hub_wake(request):
    """A shell job wakes the model: the hub POSTs {"uid", "text", "exp"},
    signed as for hub_desk, and the site runs the turn, sending its events to
    the person's pages. Replies {"ok": true}, or {"error": "BUSY"} while
    another turn runs."""
    if request.method != "POST":
        return _err("METHOD", "Use POST.", 405)
    body = request.body.decode(errors="replace")
    good = hubmod.sign(hubmod.key(settings.SECRET_KEY), body)
    if not hmac.compare_digest(request.headers.get("X-Fc-Hub", ""), good):
        return _err("BAD_SIGNATURE", "Only the hub sends this.", 403)
    try:
        m = json.loads(body)
        uid, text, exp = int(m["uid"]), m["text"], int(m["exp"])
    except (ValueError, KeyError, TypeError):
        return _err("BAD_JSON", "Give uid, text and exp.")
    if exp < time.time() or not isinstance(text, str) or not text.strip():
        return _err("EXPIRED", "The request is too old.")
    p = Profile.objects.filter(user_id=uid).first()
    if p is None:
        return JsonResponse({"error": "no such person"})
    stream = push.Stream(uid)
    try:
        # A job that wakes the model again and again spends the person's
        # credit, so an hour allows few wakes. One that waits for a turn to
        # end spends none.
        hour = f"wake:{uid}:{int(time.time() // 3600)}"
        cache.add(hour, 0, 3700)
        if cache.get(hour, 0) >= MAX_WAKES:
            line = ChatLine.objects.create(user=p.user, kind="note", text=TOO_MANY_WAKES.format(MAX_WAKES))
            stream.send({"chat": {"note": kernel.line_json(line)}})
            return JsonResponse({"error": "TOO_MANY"})
        try:
            turn = kernel.start(p, "", None, job=text[:WAKE_MAX])
        except kernel.ChatError as e:
            return JsonResponse({"error": e.code})
        cache.incr(hour)
        for e in _turn_events(p, turn):
            stream.send({"chat": e})
        return JsonResponse({"ok": True})
    finally:
        stream.close()


# --- The chat ---------------------------------------------------------------

CHAT_LINES = 400


def chat_json(p, new=None):
    u = p.user
    lines = list(ChatLine.objects.filter(user=u).exclude(kind__in=["summary", "shell"])
                 .order_by("-id")[:CHAT_LINES])
    lines.reverse()
    out = {"lines": [kernel.line_json(x) for x in (new if new is not None else lines)],
           "context": kernel.context(u)}
    # The ledger's model answers and the ledger charges, so the chat shows the
    # balance there, wherever the person is.
    exists, bal = account.balance(p, settings.FC_LEDGER)
    out["balance"] = credits.to_text(bal) if bal is not None else None
    return out


@endpoint({"GET", "POST"})
def chat(request, body):
    """GET gives the last lines, and POST {"text": T, "shell": S} says a line
    and replies with the new lines. S is what the person ran in the shell,
    which the model reads first, and S with no T resumes. With ?stream=1 the
    reply is server-sent events: {"line"}, {"piece"}, then {"done"} or
    {"error", "message"}."""
    p = need_handle(request)
    if request.method == "GET":
        return JsonResponse(chat_json(p))
    turn = kernel.start(p, body.get("text"), body.get("shell"))   # a bad line or a busy turn raises a JSON error
    if request.GET.get("stream") != "1":
        new = [x for kind, x in turn if kind == "line"]
        return JsonResponse(chat_json(p, new))
    r = StreamingHttpResponse(_chat_events(p, turn), content_type="text/event-stream")
    r["Cache-Control"] = "no-cache"
    r["X-Accel-Buffering"] = "no"
    return r


def _sse(obj):
    return f"data: {json.dumps(obj)}\n\n"


def _turn_events(p, turn):
    """A turn's events for the page: {"piece"}, {"line"}, and at its end
    {"done"} or {"error"}."""
    new = []
    try:
        for kind, x in turn:
            if kind == "piece":
                yield {"piece": x}
            else:
                new.append(x)
                yield {"line": kernel.line_json(x)}
        yield {"done": chat_json(p, new)}
    except kernel.ChatError as e:
        yield {"error": e.code, "message": e.message}


def _chat_events(p, turn):
    for e in _turn_events(p, turn):
        yield _sse(e)


@endpoint({"POST"})
def chat_new(request, body):
    p = need_handle(request)
    kernel.forget(request.user)
    return JsonResponse(chat_json(p))


@endpoint({"GET"}, auth=False)
def hub(request, body):
    """A ticket for a page to connect to the hub (fc/hub.py), which pushes
    the person's changes to their page, and network changes to every page."""
    shell = False
    if request.user.is_authenticated:
        p = Profile.objects.filter(user=request.user).first()
        if p is not None:
            push.person(p, account.here(p))
            shell = True
    return JsonResponse({"url": settings.FC_HUB_URL, "ticket": push.ticket(request.user, shell=shell)})

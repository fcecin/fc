"""The pages of fc, whose code (static/fc/fc.js) talks to the API."""

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from . import bridge, stats
from .forms import clean_handle_value
from .models import Profile
from .signals import ensure_profile


def _profile(request):
    """The viewer's profile, with its key and grant. None when the viewer is
    logged out, or CES doesn't answer."""
    if not request.user.is_authenticated:
        return None
    try:
        return ensure_profile(request.user)
    except bridge.BridgeError:
        return Profile.objects.filter(user=request.user).first()


@ensure_csrf_cookie   # the chat posts with the cookie's token
def home(request):
    """The home page, or for a visitor the email entry."""
    profile = _profile(request)
    if profile is not None and not profile.handle:
        return render(request, "fc/handle.html", {"error": request.session.pop("handle_error", None)})
    if profile is not None:
        return render(request, "fc/home.html", {"profile": profile})
    report = stats.read()
    return render(request, "fc/home.html", {"profile": None, "stats": stats.text(report) if report else ""})


@require_GET
def stats_page(request):
    """The site's stats as the front page shows them, in a person's tab."""
    report = stats.read()
    return render(request, "fc/stats.html", {"stats": stats.text(report) if report else ""})


@require_GET
def stats_text(request):
    """The site's stats as plain text, for anyone."""
    report = stats.read()
    r = HttpResponse(stats.text(report) if report else "no stats now\n", status=200 if report else 503,
                     content_type="text/plain; charset=utf-8")
    r["Access-Control-Allow-Origin"] = "*"
    return r


@ensure_csrf_cookie   # the navigator posts with the cookie's token
def network(request):
    """The navigator: the CES network as a live graph, where a person picks a
    server to go to or to move credit to."""
    return render(request, "fc/net.html", {"profile": _profile(request)})


@login_required
@require_POST
def choose_handle(request):
    profile = _profile(request)
    if profile is None or profile.handle:
        return redirect("home")
    try:
        profile.handle = clean_handle_value(request.POST.get("handle"))
    except Exception as e:
        request.session["handle_error"] = " ".join(getattr(e, "messages", [str(e)]))
        return redirect("home")
    profile.save(update_fields=["handle"])
    return redirect("home")


@login_required
def account(request):
    profile = _profile(request)
    if profile is None:
        return render(request, "fc/error.html", {"code": "NO_KEY", "message": "CES does not answer. Try again soon."}, status=502)
    return render(request, "fc/account.html", {"profile": profile})


@login_required
def export_key(request):
    profile = get_object_or_404(Profile, user=request.user)
    private, error = None, None
    if request.method == "POST" and request.POST.get("confirm") == "yes":
        try:
            private = bridge.call("export", sealed=profile.sealed)["private"]
        except bridge.BridgeError as e:
            error = e.code
    return render(request, "fc/key.html", {"profile": profile, "private": private, "error": error})


@require_GET
def handle_key(request, handle):
    """A handle's public key as one line of hex, for anyone, other sites'
    pages included."""
    p = Profile.objects.filter(handle=handle.lower()).first()
    if p is None:
        r = HttpResponse(f"no such handle: {handle[:40]}\n", status=404, content_type="text/plain; charset=utf-8")
    else:
        r = HttpResponse(p.pubkey + "\n", content_type="text/plain; charset=utf-8")
    r["Access-Control-Allow-Origin"] = "*"
    return r


@require_GET
def ledger(request):
    """The site's ledger address (host:port) as one line, for anyone, such as
    the miner (miner/), which moves its credit there."""
    r = HttpResponse(settings.FC_LEDGER + "\n", content_type="text/plain; charset=utf-8")
    r["Access-Control-Allow-Origin"] = "*"
    return r


@require_GET
def key_of(request):
    """The handle key page, for anyone: a form, and the answer to ?handle=."""
    handle = (request.GET.get("handle") or "").strip().lower()[:40]
    p = Profile.objects.filter(handle=handle).first() if handle else None
    return render(request, "fc/key_of.html", {"handle": handle, "key": p.pubkey if p else None})


# The miner builds of a release (.github/workflows/miner.yml).
MINER_BUILDS = (
    ("windows", "Windows", "x86-64", "fc-miner-windows-x86_64.exe"),
    ("macos", "macOS", "Apple silicon", "fc-miner-macos-arm64.zip"),
    ("linux", "Linux", "x86-64", "fc-miner-linux-x86_64.tar.gz"),
)


def _system(request):
    """The visitor's system, from the User-Agent, or ""."""
    ua = request.META.get("HTTP_USER_AGENT", "")
    if "Windows" in ua:
        return "windows"
    if "Macintosh" in ua:
        return "macos"
    if "Linux" in ua and "Android" not in ua:
        return "linux"
    return ""


@require_GET
def miner(request):
    """The miner tab, with the visitor's build first."""
    base = settings.FC_MINER_DOWNLOADS
    if not base:
        raise Http404
    mine = _system(request)
    builds = sorted(({"os": o, "name": n, "arch": a, "url": f"{base}/{f}"} for o, n, a, f in MINER_BUILDS),
                    key=lambda b: b["os"] != mine)
    return render(request, "fc/miner.html", {"builds": builds, "mine": mine, "sums": f"{base}/SHA256SUMS"})

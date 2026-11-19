"""Entry by email: give an email, get a code, type the code. The code makes
the account of a new email. The pages look the same for a new email and a
known one."""

import hashlib
import hmac
import logging
import re
import secrets
import time

from django.conf import settings
from django.contrib.auth import get_user_model, login, logout
from django.core.cache import cache
from django.core.mail import send_mail
from django.db import transaction
from django.db.models import F
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from . import push
from .models import EntryCode
from .signals import ensure_profile

log = logging.getLogger(__name__)

CODE_TTL = 600          # how long a code works, in seconds
TRIES = 5               # the tries of one code
SENDS_PER_IP = (5, 60)          # codes per minute from one address
SENDS_PER_EMAIL = (10, 3600)    # codes per hour to one email
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,253}\.[^@\s]{2,63}$")


def _limited(key, limit):
    n, window = limit
    bucket = f"fc:rl:{key}:{int(time.time() // window)}"
    count = cache.get_or_set(bucket, 0, window * 2)
    if count >= n:
        return True
    cache.incr(bucket)
    return False


def _digest(code):
    return hmac.new(settings.SECRET_KEY.encode(), code.encode(), hashlib.sha256).hexdigest()


def _ip(request):
    """The client's address. X-Forwarded-For counts only for a request from
    the proxy on this host (Caddy), which sets it."""
    remote = request.META.get("REMOTE_ADDR", "")
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded and remote in ("127.0.0.1", "::1"):
        return forwarded.split(",")[0].strip()
    return remote


@require_POST
def enter(request):
    email = (request.POST.get("email") or "").strip().lower()
    if not EMAIL_RE.match(email):
        return render(request, "fc/enter.html", {"error": "That is not an email address."}, status=400)
    if _limited(f"ip:{_ip(request)}", SENDS_PER_IP) or _limited(f"email:{email}", SENDS_PER_EMAIL):
        return render(request, "fc/enter.html", {"error": "Too many codes. Wait a few minutes."}, status=429)
    code = "".join(secrets.choice(ALPHABET) for _ in range(8))
    code = f"{code[:4]}-{code[4:]}"
    now = time.time()
    EntryCode.objects.filter(until__lt=now).delete()
    EntryCode.objects.update_or_create(email=email, defaults={"digest": _digest(code), "until": now + CODE_TTL,
                                                              "tries": 0})
    request.session["fc_enter"] = email
    send_mail(
        subject=f"{code} is your code for {settings.FC_NAME}",
        message=f"{code}\n\nType this code on {settings.FC_NAME} to enter. It works for 10 minutes.\n"
                f"If you did not ask for it, do nothing.\n",
        from_email=None, recipient_list=[email])
    return redirect("enter_code")


def enter_code(request):
    email = request.session.get("fc_enter")
    if not isinstance(email, str):
        return redirect("home")
    page = {"email": email}
    if request.method != "POST":
        return render(request, "fc/enter.html", page)
    code = (request.POST.get("code") or "").strip().upper().replace(" ", "")
    if len(code) == 8:
        code = f"{code[:4]}-{code[4:]}"
    # Count the try and check the code in one transaction, which SQLite runs
    # alone, so tries in parallel cannot pass TRIES.
    with transaction.atomic():
        counted = EntryCode.objects.filter(email=email, tries__lt=TRIES, until__gt=time.time()).update(
            tries=F("tries") + 1)
        row = EntryCode.objects.filter(email=email).first()
        right = bool(counted) and row is not None and hmac.compare_digest(_digest(code), row.digest)
        if right or not counted or (row and row.tries >= TRIES):
            EntryCode.objects.filter(email=email).delete()
    if not right:
        if counted and row and row.tries < TRIES:
            return render(request, "fc/enter.html", dict(page, error="That code is not right."))
        request.session.pop("fc_enter", None)
        return render(request, "fc/enter.html", {"error": "This code no longer works. Ask for a new one."})
    request.session.pop("fc_enter", None)
    User = get_user_model()
    user = User.objects.filter(email__iexact=email).first()
    if user is None:
        user = User.objects.create_user(username=f"u{secrets.token_hex(8)}", email=email)
    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    try:
        ensure_profile(user)
    except Exception:
        # CES didn't answer, so the next page visit tries again.
        pass
    return redirect("home")


@require_POST
def leave(request):
    if request.user.is_authenticated:
        # The person's shell ends, and its home goes.
        r = push.ask({"wipe": {"uid": request.user.pk}}, timeout=30)
        if "error" in r:
            log.warning("the hub did not wipe the shell of %s: %s", request.user.pk, r["error"])
    logout(request)
    return redirect("home")

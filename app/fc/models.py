"""The web data of fc: the accounts with their sealed keys, the chat lines,
and the desktop windows."""

import re

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,30})[a-z0-9]$")
# Handles that would collide with the site's paths.
RESERVED = {
    "accounts", "account", "api", "man", "new", "static", "admin", "www", "help",
    "about", "login", "logout", "signup", "root", "system", "ces", "fc",
    "support", "abuse", "security", "mail", "postmaster", "world", "watch", "at",
}


def validate_name(value):
    if not NAME_RE.match(value or "") or "--" in value:
        raise ValidationError(
            "Use 2 to 32 characters: lowercase letters, digits, and single "
            "hyphens. Start and end with a letter or digit."
        )


def validate_handle(value):
    validate_name(value)
    if value in RESERVED:
        raise ValidationError("This name is reserved. Choose another name.")


class Profile(models.Model):
    """The CES identity of an account. The bridge seals the private key."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    handle = models.CharField(max_length=32, unique=True, null=True, blank=True,
                              validators=[validate_handle])
    pubkey = models.CharField(max_length=64, unique=True)
    sealed = models.CharField(max_length=256)
    granted = models.BooleanField(default=False)
    created = models.DateTimeField(auto_now_add=True)
    # The person's last API call, to within SEEN_SECS (fc/api.py), so the stats
    # can count the people seen in a day.
    seen = models.DateTimeField(null=True, blank=True)
    # The CES server where the person is now (host:port), whose balance the page
    # shows. Empty until chosen.
    server = models.CharField(max_length=260, blank=True, default="")
    # The desktop's revision, one more at every change, so a page that saw an
    # older one fetches the desktop again.
    desk_rev = models.PositiveBigIntegerField(default=0)
    # The desktop's size, as the person's page reports it (0 until known).
    desk_w = models.PositiveIntegerField(default=0)
    desk_h = models.PositiveIntegerField(default=0)
    # The desk revision that the model last saw. When the desk is newer, the chat
    # shows the model the desk as it is now.
    model_rev = models.PositiveBigIntegerField(default=0)


class EntryCode(models.Model):
    """The last login code sent to an email: its HMAC, when it ends, and the
    tries so far."""

    email = models.CharField(max_length=320, unique=True)
    digest = models.CharField(max_length=64)
    until = models.FloatField()
    tries = models.PositiveSmallIntegerField(default=0)


class ChatLine(models.Model):
    """One line of a person's conversation with the model. The chat shows the
    "user", "model", "note" and "summary" lines, and the first line of a
    "job". The "run", "desk" and "shell" lines are for the model,
    since the shell shows them. Folded lines stay for the person to read. A
    "model" line keeps the tool calls of the answer, and a "run" line names
    the call that it answers."""

    KINDS = ["user", "model", "note", "run", "desk", "shell", "summary", "job"]
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="chat")
    kind = models.CharField(max_length=10)
    text = models.TextField()
    folded = models.BooleanField(default=False)
    # A model answer's tool calls, as the model API gives them, and its
    # reasoning, which the API needs back.
    calls = models.JSONField(null=True, blank=True)
    reasoning = models.TextField(blank=True, default="")
    call_id = models.CharField(max_length=200, blank=True, default="")   # a run line's tool call
    # For a model answer or a fold: what the call cost and used, and the
    # server's limits. Null when unknown.
    charged = models.BigIntegerField(null=True, blank=True)
    prompt_bytes = models.BigIntegerField(null=True, blank=True)
    prompt_tokens = models.BigIntegerField(null=True, blank=True)
    cached_tokens = models.BigIntegerField(null=True, blank=True)
    context_tokens = models.BigIntegerField(null=True, blank=True)
    max_input_bytes = models.BigIntegerField(null=True, blank=True)
    max_out_tokens = models.BigIntegerField(null=True, blank=True)
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]


class Widget(models.Model):
    """A window on a person's desktop: "chat" and "shell" (the kernel's),
    "text", or "program" (with its pid and server in props)."""

    KINDS = ["chat", "shell", "text", "program"]
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="widgets")
    name = models.CharField(max_length=40)
    kind = models.CharField(max_length=10)
    x = models.PositiveIntegerField(default=0)
    y = models.PositiveIntegerField(default=0)
    w = models.PositiveIntegerField(default=0)   # 0 = the kind's own size
    h = models.PositiveIntegerField(default=0)
    folded = models.BooleanField(default=False)
    props = models.JSONField(default=dict, blank=True)   # title, text

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "name"], name="unique_widget_name")]
        ordering = ["id"]


class Publication(models.Model):
    """A local name pointing to a CES program file across instance restarts."""
    owner = models.ForeignKey(Profile, on_delete=models.CASCADE)
    name = models.SlugField(max_length=40)
    server = models.CharField(max_length=260)
    server_key = models.CharField(max_length=64)
    source = models.CharField(max_length=200)
    program_key = models.CharField(max_length=64)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["owner", "name"], name="unique_publication")]


class Payment(models.Model):
    """A payment that a website asked a visitor for, and what became of it.
    The payee is the website's program on its server. The order is the
    website's name for this purchase, so a second request for the same order
    gives this record again."""

    PENDING = "pending"     # the visitor did not decide yet
    DECLINED = "declined"
    SENDING = "sending"     # fc sends the paid call now
    PAID = "paid"
    FAILED = "failed"       # the call did not go through, and the amount did not move
    UNKNOWN = "unknown"     # the call went out, and no result came back

    payer = models.ForeignKey(Profile, on_delete=models.CASCADE)
    # The website that asked. A withdrawn publication keeps its payments.
    publication = models.ForeignKey(Publication, null=True, on_delete=models.SET_NULL)
    site = models.CharField(max_length=80)
    server = models.CharField(max_length=260)
    server_key = models.CharField(max_length=64)
    source = models.CharField(max_length=200)
    payee = models.CharField(max_length=64)
    order = models.CharField(max_length=64)
    description = models.CharField(max_length=200)
    amount = models.PositiveBigIntegerField()
    # The website path that the frame opens after the decision.
    back = models.CharField(max_length=512, default="/")
    state = models.CharField(max_length=8, default=PENDING)
    created = models.DateTimeField(auto_now_add=True)
    decided = models.DateTimeField(null=True)
    # fc sets this when it accepts the approval, before it sends the call. A
    # payment that is still sending after this time has an unknown outcome.
    deadline = models.DateTimeField(null=True)
    reply = models.TextField(blank=True, default="")
    error = models.CharField(max_length=64, blank=True, default="")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["server", "payee", "payer", "order"],
                                               name="unique_payment")]


class PageVisit(models.Model):
    """One browser handoff. Removing the login session revokes its visits."""
    publication = models.ForeignKey(Publication, on_delete=models.CASCADE)
    session = models.ForeignKey("sessions.Session", on_delete=models.CASCADE)
    viewer = models.ForeignKey(Profile, on_delete=models.CASCADE)
    ticket = models.CharField(max_length=64, unique=True, null=True)
    cookie = models.CharField(max_length=64, unique=True, null=True)
    expires = models.DateTimeField(db_index=True)
    target = models.TextField()

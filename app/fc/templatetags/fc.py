"""Template helpers: the installation's name, the viewer's server, the ledger,
the miner downloads and credit text."""

from django import template
from django.conf import settings

from ..credits import to_text

register = template.Library()


@register.simple_tag
def instance():
    return settings.FC_NAME


@register.simple_tag(takes_context=True)
def here(context):
    """The server where the viewer is."""
    from ..models import Profile
    user = context.get("user")
    p = Profile.objects.filter(user=user).first() if user and user.is_authenticated else None
    return (p.server if p else "") or settings.FC_LEDGER


@register.simple_tag
def ledger():
    return settings.FC_LEDGER


@register.simple_tag
def miner_downloads():
    return settings.FC_MINER_DOWNLOADS


@register.filter
def credits(raw):
    """Raw units as credit text, such as 1.50000000."""
    return to_text(raw)

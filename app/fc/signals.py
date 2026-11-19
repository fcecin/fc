"""An account gets a CES key and its grant when it first logs in with an
email code."""

import logging

from . import bridge
from .models import Profile

log = logging.getLogger(__name__)


def make_profile(user):
    """The account's profile, with a new key if it has none."""
    profile = Profile.objects.filter(user=user).first()
    if profile is None:
        r = bridge.call("newkey")
        profile = Profile.objects.create(user=user, pubkey=r["pubkey"], sealed=r["sealed"])
    return profile


def ensure_profile(user):
    """The profile, after asking for its grant once."""
    profile = make_profile(user)
    if not profile.granted:
        try:
            bridge.call("grant", pubkey=profile.pubkey)
            profile.granted = True
        except bridge.BridgeError as e:
            if e.code == "ALREADY_GRANTED":
                profile.granted = True
            else:
                log.warning("grant failed for %s: %s", profile.pubkey, e.code)
        profile.save(update_fields=["granted"])
    return profile

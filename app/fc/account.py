"""A person's CES account: their server, their balances and credit moves."""

import re
import time

from django.conf import settings
from django.core.cache import cache
from django.db import transaction

from . import bridge, net, push
from .models import Profile

HEX64 = re.compile(r"^[0-9a-f]{64}$")


class AccountError(Exception):
    def __init__(self, code, message):
        super().__init__(code)
        self.code, self.message = code, message


def here(profile):
    """The person's server. A new account starts at the ledger."""
    return profile.server or settings.FC_LEDGER


def balance(profile, server, timeout=20):
    """(exists, raw balance) of the person's key on `server`. The balance is
    None when fc cannot read it."""
    key = f"acct:{profile.pubkey}:{server}"
    r = cache.get(key)
    if r is None:
        try:
            b = bridge.call("balance", timeout=timeout, server=server, pubkey=profile.pubkey)
            r = (bool(b.get("exists")), b.get("balance", 0) if b.get("exists") else 0)
        except bridge.BridgeError:
            r = (False, None)
        cache.set(key, r, 5)
    return r


def forget(profile, *servers):
    for s in servers:
        cache.delete(f"acct:{profile.pubkey}:{s}")


def travel(profile, server):
    """Make `server` the person's server, if it answers."""
    if server != settings.FC_LEDGER and net.info(server, fresh=True) is None:
        raise AccountError("NO_ANSWER", "That server does not answer.")
    profile.server = server
    profile.save(update_fields=["server"])
    transaction.on_commit(lambda: push.person(profile, server))


def recipient(text):
    """The key of a handle on this site, or a 64-hex key, else None."""
    text = (text or "").strip().lower()
    if HEX64.match(text):
        return text
    p = Profile.objects.filter(handle=text).first()
    return p.pubkey if p else None


def send(profile, to, amount):
    """Send `amount` raw credit to the key `to` on the person's server, which
    opens an account for the recipient if needed."""
    server = here(profile)
    try:
        r = bridge.call("transfer", sealed=profile.sealed, server=server, dest=to, amount=amount)
    except bridge.BridgeError as e:
        raise AccountError(e.code, _why(e.code))
    forget(profile, server)
    return r["balance"]


def move(profile, from_server, to_server, amount):
    """Move `amount` raw credit of the person from `from_server` to the same
    key on `to_server`. The two servers settle by their reserves, so they
    must be neighbors."""
    if to_server == from_server:
        raise AccountError("SAME_SERVER", "The credit is already there.")
    try:
        r = bridge.call("cross", sealed=profile.sealed, server=from_server, dest=profile.pubkey,
                        amount=amount, dest_server=to_server)
    except bridge.BridgeError as e:
        raise AccountError(e.code, _why(e.code))
    forget(profile, from_server, to_server)
    return r["balance"]


# How long fc waits for moved credit to arrive at the second server, in
# seconds. CES takes a move from the first server at once, and the credit
# arrives at the second one when the two servers settle, usually at once too.
ARRIVAL = 5
# A move that fc did not see arrive holds back the next move of the same
# person to the same server for this long, in seconds, also in other workers.
# So a reload or a second request does not move the credit again.
MOVE_HOLD = 60
# The errors of a move after which no credit left the first server.
MOVE_REFUSED = {
    "BRIDGE_DOWN", "BAD_SEALED_KEY", "BAD_AMOUNT", "BAD_ADDRESS", "BAD_DEST_SERVER", "ADDRESS_NOT_PUBLIC",
    "CANNOT_RESOLVE", "CANNOT_CONNECT", "SAME_SERVER", "CES_ERROR_ORIGIN_NOT_FOUND",
    "CES_ERROR_INSUFFICIENT_BALANCE", "CES_ERROR_BAD_INPUT", "CES_ERROR_UNKNOWN_PEER",
}


def arrives(profile, server, at_least, seconds=None):
    """Wait up to `seconds` (ARRIVAL when None) until the person's balance on
    `server` is at least `at_least` raw. True when it is there."""
    end = time.monotonic() + (ARRIVAL if seconds is None else seconds)
    while True:
        left = end - time.monotonic()
        if left <= 0:
            return False
        forget(profile, server)
        _, raw = balance(profile, server, timeout=max(0.5, left))
        if raw is not None and raw >= at_least and time.monotonic() <= end:
            return True
        time.sleep(max(0, min(0.2, end - time.monotonic())))


def fund(profile, server, amount, at_least):
    """Move `amount` raw of the person from their own server to `server`, and
    wait until their balance there is at least `at_least`. Returns None when
    it is there. Else returns "PENDING", when credit can be on its way (a
    move with no answer, or a move that did not arrive yet), or the code of
    the refusal, when no credit left."""
    hold = f"moving:{profile.pubkey}:{server}"
    if not cache.add(hold, 1, MOVE_HOLD):
        return "PENDING"
    try:
        move(profile, here(profile), server, amount)
    except AccountError as e:
        if e.code in MOVE_REFUSED:
            cache.delete(hold)
            return e.code
        return "PENDING"
    if arrives(profile, server, at_least):
        cache.delete(hold)
        return None
    return "PENDING"


def _why(code):
    if "INSUFFICIENT" in code or code == "CES_ERROR_ORIGIN_NOT_FOUND":
        return "Not enough credit on this server."
    if code == "CES_ERROR_UNKNOWN_PEER":
        return "This server does not peer with that server, so the credit cannot go there directly."
    if code == "BAD_DEST_SERVER":
        return "That is not a server that the credit can go to."
    if code.startswith("BRIDGE"):
        return "The bridge to CES is not running."
    if code == "CES_ERROR_UNREACHABLE":
        return "The server did not answer. Try again."
    if code == "CES_ERROR_CONNECTION_LOST":
        return "The connection to the server closed. Try again."
    return f"CES refused it ({code})."

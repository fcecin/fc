"""Changes to the hub (fc/hub.py), which pushes them to the person's open
pages. Call `later` in the change's transaction, so the message goes when the
change commits, and a rollback sends nothing."""

import json
import logging
import socket

from django.conf import settings
from django.db import transaction

from . import hub

log = logging.getLogger("fc.push")


def ticket(user, shell=False):
    """A ticket to connect a page of `user`, or of a visitor, to the hub. With
    `shell`, the person's own page may run cesh in its shell."""
    uid = user.pk if user.is_authenticated else hub.VISITOR
    return hub.ticket(hub.key(settings.SECRET_KEY), uid, shell=shell and uid != hub.VISITOR)


def send(uid, msg):
    """Send msg to the open page of user `uid`, or to every page ("all")."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            s.connect(settings.FC_HUB_SOCKET)
            s.sendall((json.dumps({"to": uid, "msg": msg}) + "\n").encode())
    except OSError as e:
        # The pages miss this change, and read the state again when their
        # socket reconnects after the hub comes back.
        log.warning("hub: %s", e)


class Stream:
    """Messages for the open page of `uid`, in order on one connection, since
    separate sends can arrive out of order. As with send, a message that
    can't go is lost."""

    def __init__(self, uid):
        self.uid = uid
        self.s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.s.settimeout(0.5)
            self.s.connect(settings.FC_HUB_SOCKET)
        except OSError as e:
            log.warning("hub: %s", e)
            self.s.close()
            self.s = None

    def send(self, msg):
        if self.s is None:
            return
        try:
            self.s.sendall((json.dumps({"to": self.uid, "msg": msg}) + "\n").encode())
        except OSError as e:
            log.warning("hub: %s", e)
            self.close()

    def close(self):
        if self.s is not None:
            self.s.close()
            self.s = None


def ask(req, timeout=5.0):
    """A model request to the hub (Hub.ask in fc/hub.py), and its reply,
    {"ok": ...} or {"error": why}."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(settings.FC_HUB_SOCKET)
            s.sendall((json.dumps({"ask": req}) + "\n").encode())
            data = b""
            while not data.endswith(b"\n"):
                chunk = s.recv(65536)
                if not chunk:
                    break
                data += chunk
        reply = json.loads(data)
        return reply if isinstance(reply, dict) else {"error": "a bad reply from the hub"}
    except (OSError, ValueError) as e:
        log.warning("hub: %s", e)
        return {"error": "the site cannot reach the shell and the windows now"}


def person(profile, server):
    """Tell the hub what the person's shell needs to run cesh: the sealed key,
    which only the bridge can open, and the server for commands that name
    none."""
    msg = {"person": {"uid": profile.user_id, "sealed": profile.sealed, "server": server}}
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            s.connect(settings.FC_HUB_SOCKET)
            s.sendall((json.dumps(msg) + "\n").encode())
    except OSError as e:
        log.warning("hub: %s", e)


def later(uid, make):
    """Send make() to the pages of `uid` when the transaction commits."""
    transaction.on_commit(lambda: send(uid, make()))

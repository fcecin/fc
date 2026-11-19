"""Client for fc-bridge: one JSON request per line on a Unix socket."""

import json
import socket

from django.conf import settings


class BridgeError(Exception):
    def __init__(self, code, message="", detail=None):
        super().__init__(code)
        self.code = code
        self.message = message
        self.detail = detail or {}


def call(op, timeout=20, **args):
    """One bridge op. `timeout` (seconds) bounds the whole exchange, and a
    think call, which waits for the model, passes a longer one."""
    req = dict(op=op, **args)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(settings.FC_BRIDGE_SOCKET)
        s.sendall((json.dumps(req) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    except OSError:
        raise BridgeError("BRIDGE_DOWN")
    finally:
        s.close()
    try:
        reply = json.loads(buf)
    except ValueError:
        raise BridgeError("BRIDGE_BAD_REPLY")
    if not reply.get("ok"):
        raise BridgeError(reply.get("error", "BRIDGE_ERROR"), reply.get("message", ""), reply)
    return reply


def stream(op, timeout=20, **args):
    """One bridge op whose reply comes in lines. It yields each line before the
    last as a dict, and returns the last one, the reply, as the generator's
    value. `timeout` bounds each wait for a line."""
    req = dict(op=op, **args)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(settings.FC_BRIDGE_SOCKET)
        s.sendall((json.dumps(req) + "\n").encode())
        buf = b""
        while True:
            while b"\n" not in buf:
                chunk = s.recv(65536)
                if not chunk:
                    raise BridgeError("BRIDGE_BAD_REPLY")
                buf += chunk
            line, buf = buf.split(b"\n", 1)
            try:
                msg = json.loads(line)
            except ValueError:
                raise BridgeError("BRIDGE_BAD_REPLY")
            if "ok" in msg:
                if not msg.get("ok"):
                    raise BridgeError(msg.get("error", "BRIDGE_ERROR"), msg.get("message", ""), msg)
                return msg
            yield msg
    except OSError:
        raise BridgeError("BRIDGE_DOWN")
    finally:
        s.close()

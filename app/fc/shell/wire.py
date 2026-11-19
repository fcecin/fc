"""The wire between the hub and a shell unit: a SOCK_SEQPACKET Unix socket
with one message per frame, whose first message is JSON, {"kind": ...}.

- {"kind": "term"} is the shell's pty (ptyhost.py), followed by [u8 type]
  [payload] frames: OUT (output), IN (keys), SIZE (u16 cols, u16 rows), END
  ({"exit"}), RUN ({"id", "line"}, an assistant command), RUNOUT ([u32 id]
  [bytes]), RUNEND ({"id", "exit", "busy"}), KILL ({"id"}, Ctrl+C), MINE
  ({"line", "out"}, a person's command) and WAKE ({"text"}, a job's wake).
- {"kind": "cesh", "args": [...]} comes with the command's three descriptors
  (bin/cesh), and the reply is {"result": R} as ceshd gives it. Closing the
  connection stops the command.
- {"kind": "desk", "op": "..."} (bin/desk) gets {"out": T} or {"error": T}.
"""

import json
import struct

VERSION = 5     # ptyhost.py's version, in its hello
RUN_MIN = 3     # the oldest ptyhost.py that runs the assistant's commands
OUT, IN, SIZE, END = 1, 2, 3, 4
RUN, RUNOUT, RUNEND, KILL = 5, 6, 7, 8
MINE = 9
WAKE = 10
MAX_MSG = 64 * 1024          # the largest message
SOCKET = "/run/fcsh/hub.sock"   # the hub, as the unit sees it


def frame(kind, payload=b""):
    return bytes([kind]) + payload


def size(cols, rows):
    return frame(SIZE, struct.pack("!HH", cols, rows))


def unsize(payload):
    """(cols, rows) of a SIZE payload, or None."""
    if len(payload) != 4:
        return None
    return struct.unpack("!HH", payload)


def runout(rid, data):
    return frame(RUNOUT, struct.pack("!I", rid) + data)


def unrunout(payload):
    """(id, bytes) of a RUNOUT payload."""
    return struct.unpack("!I", payload[:4])[0], payload[4:]


def hello(**fields):
    return json.dumps(fields).encode()


def chunks(data, n=MAX_MSG - 1):
    """`data` in pieces that each fit one frame with its type byte."""
    for i in range(0, len(data), n):
        yield data[i:i + n]

"""VS1 in the hub: a program window's screen, read from its program's bytes,
and a viewer's messages, for the model's look, press and type (desk.py).
The top of fc/vs1.lua lists the messages, and the page has its own monitor
(fc/monitor.js)."""
import struct
import time

MODE, FRAME, TITLE = 1, 2, 3
KEY, POINTER, TEXT = 129, 130, 132
MAX_MESSAGE = 1 << 20       # a bigger message breaks the cable, as on the page
MAX_TEXT = 4000             # VS1 drops a viewer message over 4096 bytes
BLANK = (32, 0xaaaaaa, 0)
DOWN, UP = 1, 2


class Screen:
    """A program window's grid, as its program's frames leave it."""

    def __init__(self):
        self.cols = self.rows = 0
        self.cells = []
        self.title = ""
        self.pending = b""
        self.broken = ""

    def feed(self, data):
        if self.broken:
            return
        buf, at = self.pending + data, 0
        while len(buf) - at >= 5:
            size = struct.unpack_from("<I", buf, at + 1)[0]
            if size > MAX_MESSAGE:
                self.broken = f"a message of {size} bytes"
                buf, at = b"", 0
                break
            if len(buf) - at - 5 < size:
                break
            self._message(buf[at], buf[at + 5:at + 5 + size])
            at += 5 + size
        self.pending = buf[at:]

    def _message(self, kind, b):
        if kind == MODE and len(b) == 5 and b[0] == 1:
            self.cols, self.rows = struct.unpack_from("<HH", b, 1)
            self.cells = [BLANK] * (self.cols * self.rows)
        elif kind == FRAME:
            at = 0
            while at + 6 <= len(b):
                x, y, n = struct.unpack_from("<HHH", b, at)
                at += 6
                for i in range(n):
                    if at + 10 > len(b):
                        return
                    if y < self.rows and x + i < self.cols:
                        code = struct.unpack_from("<I", b, at)[0]
                        fg = int.from_bytes(b[at + 4:at + 7], "little")
                        bg = int.from_bytes(b[at + 7:at + 10], "little")
                        self.cells[y * self.cols + x + i] = (code, fg, bg)
                    at += 10
        elif kind == TITLE:
            self.title = b.decode(errors="replace")

    def text(self, x1=1, y1=1, x2=None, y2=None):
        """Rows y1 to y2 and columns x1 to x2, from 1 and inclusive, as text
        less trailing spaces."""
        x2 = self.cols if x2 is None else min(x2, self.cols)
        y2 = self.rows if y2 is None else min(y2, self.rows)
        rows = []
        for y in range(max(y1, 1), y2 + 1):
            row = self.cells[(y - 1) * self.cols:y * self.cols]
            rows.append("".join(_char(row[x - 1][0]) for x in range(max(x1, 1), x2 + 1)).rstrip())
        return "\n".join(rows)


def _char(code):
    if code < 32:
        return " "
    if code > 0x10FFFF or 0xD800 <= code <= 0xDFFF:
        return "�"
    return chr(code)


def _message(kind, body):
    return bytes([kind]) + struct.pack("<I", len(body)) + body


def now_ms():
    """The viewer's clock for a key, in ms."""
    return int(time.monotonic() * 1000) & 0xFFFFFFFF


def key(action, name, ms):
    """A key down (DOWN) or up (UP), with the browser's name for it."""
    return _message(KEY, bytes([action, 0]) + struct.pack("<I", ms & 0xFFFFFFFF) + name.encode())


def pointer(action, button, x, y, ms):
    """A pointer press (DOWN) or release (UP) on cell x, y, counted from 1,
    while the wire counts from 0."""
    return _message(POINTER, struct.pack("<BBHHI", action, button, x - 1, y - 1, ms & 0xFFFFFFFF))


def text(s):
    """`s` as text messages, each small enough for VS1."""
    out, part = [], b""
    for ch in s:
        c = ch.encode()
        if len(part) + len(c) > MAX_TEXT:
            out.append(_message(TEXT, part))
            part = b""
        part += c
    if part:
        out.append(_message(TEXT, part))
    return b"".join(out)


# A key's terminal bytes, for the shell.
TERMINAL_KEYS = {
    "Enter": "\r", "Tab": "\t", "Backspace": "\x7f", "Escape": "\x1b", "Space": " ",
    "ArrowUp": "\x1b[A", "ArrowDown": "\x1b[B", "ArrowRight": "\x1b[C", "ArrowLeft": "\x1b[D",
    "Home": "\x1b[H", "End": "\x1b[F", "Delete": "\x1b[3~", "PageUp": "\x1b[5~", "PageDown": "\x1b[6~",
}


def terminal_key(name):
    """The bytes that a terminal sends for key `name` ("Enter", "Ctrl+c",
    "a"), or None."""
    if name in TERMINAL_KEYS:
        return TERMINAL_KEYS[name].encode()
    if name.lower().startswith("ctrl+") and len(name) == 6 and name[5].isalpha():
        return bytes([ord(name[5].lower()) & 0x1F])
    if len(name) == 1:
        return name.encode()
    return None


def vs1_key(name):
    """The browser's name for key `name` ("Space" is " ")."""
    return " " if name == "Space" else name

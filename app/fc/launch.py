"""cesh as a person, through fc-bridge's cesh socket (bridge/src/ceshops.h).

`launch` returns the ceshd connection, which brings the command's result and
stops the command when closed, and `dial` returns the stream to a program for
a window. The private key stays in the bridge and ceshd. The hub uses this
module too, so it runs without Django.
"""

import json
import os
import socket

REPLY_MAX = 1024


class LaunchError(Exception):
    pass


# The most input of one command, which one pipe holds whole, so nothing waits
# to write it.
MAX_STDIN = 64 * 1024


def stdin_pipe(data):
    """A command's stdin: the read end of a pipe that holds `data` (bytes) and
    then ends. The caller closes it after the launch."""
    if len(data) > MAX_STDIN:
        raise LaunchError(f"the input is too big ({len(data)} bytes, at most {MAX_STDIN})")
    r, w = os.pipe()
    try:
        os.set_blocking(w, False)
        n = os.write(w, data) if data else 0
    except OSError:
        n = -1
    finally:
        os.close(w)
    if n != len(data):
        os.close(r)
        raise LaunchError("the input does not fit in a pipe")
    return r


def launch(path, sealed, server, args, fds, timeout=5):
    """Start `cesh <args>` as the person of `sealed`, with `fds` (stdin,
    stdout and stderr), and `server` for a command that names none. Returns
    the ceshd connection (a socket). The caller must then close its own copies
    of the descriptors to see the end of the output."""
    req = json.dumps({"sealed": sealed, "server": server or "", "args": list(args)}).encode()
    s = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    s.settimeout(timeout)
    try:
        s.connect(path)
        socket.send_fds(s, [req], list(fds))
        data, got, _, _ = socket.recv_fds(s, REPLY_MAX, 1)
    except OSError as e:
        raise LaunchError(f"cesh does not run now ({e.strerror or e})")
    finally:
        s.close()
    text = data.decode(errors="replace")
    if text == "ok" and len(got) == 1:
        return socket.socket(fileno=got[0])
    for fd in got:
        os.close(fd)
    raise LaunchError(text[6:] if text.startswith("error ") else (text or "no reply"))


def dial(path, sealed, server, pid, timeout=25, server_key=None):
    """Dial the program `pid` on `server` as the person of `sealed`. Returns
    the stream to the program (a socket), which gives the program's bytes,
    the hello first, and takes the bytes for it. Closing it ends the dial,
    and the bridge waits up to 20 s for the program."""
    fields = {"sealed": sealed, "server": server, "dial": pid}
    if server_key is not None:
        fields["server_key"] = server_key
    req = json.dumps(fields).encode()
    s = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    s.settimeout(timeout)
    try:
        s.connect(path)
        s.sendall(req)
        data, got, _, _ = socket.recv_fds(s, REPLY_MAX, 1)
    except OSError as e:
        raise LaunchError(f"the window cannot dial now ({e.strerror or e})")
    finally:
        s.close()
    text = data.decode(errors="replace")
    if text == "ok" and len(got) == 1:
        return socket.socket(fileno=got[0])
    for fd in got:
        os.close(fd)
    raise LaunchError(text[6:] if text.startswith("error ") else (text or "no reply"))


def result(conn, timeout=None):
    """The command's result from its ceshd connection, which then closes, or
    "error no reply" when the connection ends without one."""
    try:
        conn.settimeout(timeout)
        data = conn.recv(REPLY_MAX)
    except OSError:
        data = b""
    finally:
        conn.close()
    return data.decode(errors="replace") if data else "error no reply"

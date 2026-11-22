"""The desk: a person's desktop, which the site keeps.

Every change is a desk op (desk.txt is the manual), applied only by `run`,
which returns what a terminal would print. A drag on the page, a "desk" line
in the shell and the model all send the same ops, and every change bumps
Profile.desk_rev and goes whole to the open pages through the hub.
"""

from http.client import HTTPException
import re
import shlex
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.template.loader import render_to_string

from . import account, bridge, hub, launch, publications, push
from .models import Profile, Publication, Widget

NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
SERVER = re.compile(r"^[A-Za-z0-9.:\[\]-]{1,253}:[0-9]{1,5}$")
MAX_WIDGETS = 20        # every window, the kernel's included
MAX_TEXT = 4000
MAX_TITLE = 80
MAX_PX = 20000
MIN_W, MIN_H = 200, 100  # the smallest window (the page's WinBox has the same)
# The ops whose output the model reads before it goes on. The other ops
# change the desk, and the model knows the desk that it made. A publication
# op prints what the model must tell, such as a link, so it is here too.
READS = {"ls", "show", "screen", "help", "man", "vs1", "website", "look", "grid", "tail",
         "publish", "unpublish", "visit"}
OPS = "new, set, move, size, fold, unfold, del, run, ls, show, screen, help, man, vs1, look, grid, tail, press, click, type"
# The ops that read or type on the person's open page, through the hub (fc/hub.py).
HUB_OPS = {"look", "grid", "tail", "press", "click", "type"}
TAIL_CHARS, MAX_TAIL = 2000, 20000
HOLD_MS, MAX_HOLD_MS = 30, 10000
TYPE_CHARS = 4000
ENTRY_CHARS = 4000       # characters in one shell entry (a command or its output)
OPS_HELP = """the desk ops (man prints the whole manual):
  new text <name> "<text>" [title "<t>"] [at <x> <y>] [size <w> <h>]
  new program <name> <pid> [on <server>] [title "<t>"] [at <x> <y>] [size <w> <h>]
  set <name> title|text "<t>"      move <name> <x> <y>      size <name> <w> <h>
  fold <name>      unfold <name>      del <name>      run shell "<op>"
  ls      show <name>      screen      help      man      vs1
  look <name> [<x1> <y1> <x2> <y2>]      grid <name> [<x1> <y1> <x2> <y2>]
  tail shell [<n>]      press <name> <key>[:<ms>] ...      click <name> <x> <y> [<button>]
  type <name> "<text>"  (\\n is Enter)"""
# A copy of ces's loadables/vs1/vs1.lua, for `desk vs1` and the tests.
VS1 = Path(__file__).resolve().parent / "static" / "fc" / "vs1.lua"
# The guide to websites and its example, for `desk website`.
WEBSITE = Path(__file__).resolve().parent / "shell" / "WEBSITE.md"
WEBSITE_EXAMPLE = Path(__file__).resolve().parent / "shell" / "booklet.lua"

# A new window's place and size, when the op leaves them out.
DEFAULTS = {"text": dict(x=60, y=60, w=360, h=200), "program": dict(x=80, y=80, w=560, h=420)}
# The kernel's windows, on every desk. Size 0 lets the page give the first
# place.
KERNEL = {"chat": "chat", "shell": "shell"}   # name -> kind


class DeskError(Exception):
    pass


def vs1_text():
    """The docs at the top of VS1, as `desk vs1` prints them."""
    text = VS1.read_text()
    head = text[:text.index("local vs1 = {}")].rstrip().splitlines()
    return "\n".join(line[3:] if line.startswith("-- ") else line[2:] for line in head)


def website_text():
    """The guide to websites, with the example shop at its end."""
    return (WEBSITE.read_text().rstrip() + "\n\n```lua\n" + WEBSITE_EXAMPLE.read_text().rstrip()
            + "\n```")


def widget_json(w):
    j = {"name": w.name, "kind": w.kind, "x": w.x, "y": w.y, "w": w.w, "h": w.h,
         "folded": w.folded, "title": w.props.get("title", w.name)}
    if w.kind == "text":
        j["text"] = w.props.get("text", "")
    if w.kind == "program":
        j["pid"] = w.props.get("pid", 0)
        j["server"] = w.props.get("server", "")
    return j


def ensure_kernel(user):
    for name, kind in KERNEL.items():
        if not Widget.objects.filter(user=user, name=name).exists():
            Widget.objects.create(user=user, name=name, kind=kind, props={"title": name})


def state(user):
    ensure_kernel(user)
    p = Profile.objects.get(user=user)
    return {"rev": p.desk_rev, "widgets": [widget_json(w) for w in Widget.objects.filter(user=user)]}


def since(user, rev):
    """For a page that saw revision `rev`: the whole desktop if it changed
    since, else only the revision."""
    p = Profile.objects.get(user=user)
    return state(user) if p.desk_rev != rev else {"rev": p.desk_rev}


def ls(user):
    return "\n".join(f"{w.name}  {w.kind}  at {w.x} {w.y}  size {w.w} {w.h}{'  folded' if w.folded else ''}"
                     f"  \"{w.props.get('title', w.name)}\"" for w in Widget.objects.filter(user=user))


def _px(word, what):
    try:
        v = int(word)
    except ValueError:
        raise DeskError(f"{what} is a number of pixels, not {word!r}")
    if not 0 <= v <= MAX_PX:
        raise DeskError(f"{what} is from 0 to {MAX_PX}")
    return v


def _name(word):
    if not NAME.match(word or ""):
        raise DeskError("a name is 1 to 40 lowercase letters, digits, - or _, and starts with a letter or digit")
    return word


def _min_size(w, h):
    if w < MIN_W or h < MIN_H:
        raise DeskError(f"a window is at least {MIN_W} wide and {MIN_H} high")
    return w, h


def _get(user, name):
    w = Widget.objects.filter(user=user, name=_name(name)).first()
    if w is None:
        raise DeskError(f"no widget named {name} (ls lists them)")
    return w


def _bump(user):
    p = Profile.objects.select_for_update().get(user=user)
    p.desk_rev += 1
    p.save(update_fields=["desk_rev"])
    return p.desk_rev


def _save(user, w):
    w.save()
    _bump(user)


def parse(line):
    try:
        words = shlex.split(line or "")
    except ValueError:
        raise DeskError("a quote is not closed")
    if not words:
        raise DeskError(f"say an op: {OPS}")
    return words


def run(user, line):
    """Apply one op. Returns (text, changed): what a terminal would print, and
    whether the desktop changed. Raises DeskError with the reason. A change
    goes to the open pages when it commits."""
    words = parse(line)
    if settings.FC_PUBLICATIONS_PORT and words[0] in ("publish", "unpublish", "visit"):
        return _publication(user, words[0], words[1:]), False
    if words[0] in HUB_OPS:
        return _hub(user, words[0], words[1:]), False
    with transaction.atomic():
        text, changed = _apply(user, words)
        if changed:
            push.later(user.pk, lambda: {"desk": state(user)})
    return text, changed


def _publication(user, op, args):
    profile = Profile.objects.get(user=user)
    if op == "visit":
        return _visit(profile, args)
    if not profile.handle:
        raise DeskError("Choose a handle before publishing.")
    if op == "unpublish":
        if len(args) != 1:
            raise DeskError("unpublish <name>")
        name = _name(args[0])
        deleted, _ = Publication.objects.filter(owner=profile, name=name).delete()
        if not deleted:
            raise DeskError("There is no publication with that name.")
        return f"Unpublished {name}."
    try:
        if len(args) == 2:
            window = _get(user, args[0])
            if window.kind != "program":
                raise DeskError("Select a program window.")
            name = _name(args[1])
            server = window.props["server"]
            running = bridge.call("ps", sealed=profile.sealed, server=server)["instances"]
            source = next((i["path"] for i in running if i["pid"] == window.props["pid"]), None)
            if not source:
                raise DeskError("For another author's program, publish its full source path.")
        elif len(args) == 4 and args[2] == "on":
            name, source, _, server = args
            name = _name(name)
            if not source.startswith("/"):
                source = f"/h/{profile.pubkey}/{source}"
        else:
            raise DeskError("publish <window> <name>, or publish <name> <source.lua> on <server>")
        return publications.publish(profile, name, server, source)
    except (ValueError, bridge.BridgeError) as e:
        raise DeskError(str(e)) from e


def _visit(profile, args):
    """A page of one of the person's own websites as it comes through its
    link: visit <name> [<path>]. Only their own: the page goes into the
    assistant's context, and the assistant holds the person's shell."""
    if len(args) not in (1, 2) or "/" in args[0]:
        raise DeskError("visit <name> [<path>], for a website that you published")
    page = Publication.objects.filter(owner=profile, name=args[0]).first()
    if page is None:
        raise DeskError("You have no publication with that name.")
    path = args[1] if len(args) == 2 else "/"
    if not path.startswith("/") or path.startswith("//") or any(c in path for c in " \r\n"):
        raise DeskError("A path starts with one / and has no spaces.")
    try:
        return publications.visit(profile, page, path)
    except (ValueError, OSError, HTTPException, bridge.BridgeError, launch.LaunchError) as e:
        raise DeskError(f"The website did not answer: {e}") from e


def _hub(user, op, args):
    """look, grid, tail, press, click and type, on the shell and the program
    windows of the person's open page, which the hub holds while the page is
    open."""
    usage = {"look": "look <name> [<x1> <y1> <x2> <y2>]", "grid": "grid <name> [<x1> <y1> <x2> <y2>]",
             "tail": "tail shell [<n>]", "press": "press <name> <key>[:<ms>] ...",
             "click": "click <name> <x> <y> [<button>]", "type": 'type <name> "<text>"'}[op]
    if not args:
        raise DeskError(usage)
    ensure_kernel(user)
    w = _get(user, args[0])
    if w.kind not in ("program", "shell"):
        raise DeskError(f"{w.name} is a {w.kind} window, with no program or shell")
    if op in ("look", "grid", "click") and w.kind != "program":
        raise DeskError(f"{op} takes a program window, and tail takes the shell")
    if op == "tail" and w.kind != "shell":
        raise DeskError("tail takes the shell, and look and grid take a program window")
    target = hub.SHELL if w.kind == "shell" else w.name
    if op in ("look", "grid"):
        if len(args) not in (1, 5):
            raise DeskError(usage)
        rect = [_count(v, "a cell", 1000) for v in args[1:]] or None
        ok = _ask({"look": {"uid": user.pk, "w": target, "rect": rect}})
        x1, y1 = (rect[0], rect[1]) if rect else (1, 1)
        head = f'{w.name} {ok["cols"]}x{ok["rows"]} "{ok["title"]}"' + (f" from {x1},{y1}" if rect else "")
        if op == "look":
            return head + "\n" + ok["text"]
        x2 = min(rect[2], ok["cols"]) if rect else ok["cols"]
        rows = ok["text"].split("\n")
        n = len(str(y1 + len(rows) - 1))
        tens = "".join(str(c // 10 % 10) if c % 10 == 0 else " " for c in range(x1, x2 + 1)).rstrip()
        ruler = ([" " * n + "|" + tens] if tens else []) + [" " * n + "|" + "".join(str(c % 10) for c in range(x1, x2 + 1))]
        return "\n".join([head] + ruler + [f"{y1 + i:>{n}}|{row}" for i, row in enumerate(rows)])
    if op == "tail":
        if len(args) > 2:
            raise DeskError(usage)
        n = _count(args[1], "a count", MAX_TAIL) if len(args) == 2 else TAIL_CHARS
        return _ask({"tail": {"uid": user.pk, "n": n}})["text"]
    if op == "click":
        if len(args) not in (3, 4):
            raise DeskError(usage)
        x, y = _count(args[1], "a cell", 1000), _count(args[2], "a cell", 1000)
        button = _count(args[3], "a button", 4) if len(args) == 4 else 0
        _ask({"click": {"uid": user.pk, "w": target, "x": x, "y": y, "button": button}})
        return f"clicked {x},{y} in {w.name}"
    if op == "press":
        keys = [_key(k) for k in args[1:]]
        if not keys:
            raise DeskError(usage)
        if sum(ms for _, ms in keys) > MAX_HOLD_MS:
            raise DeskError(f"the keys of one press hold {MAX_HOLD_MS} ms at most")
        _ask({"press": {"uid": user.pk, "w": target, "keys": keys}}, MAX_HOLD_MS / 1000 + 5)
        return f"pressed {' '.join(args[1:])} in {w.name}"
    if len(args) != 2:
        raise DeskError(usage)
    text = re.sub(r"\\(.)", lambda m: {"n": "\n", "t": "\t", "\\": "\\"}.get(m[1], m[0]), args[1])
    if len(text) > TYPE_CHARS:
        raise DeskError(f"a type is at most {TYPE_CHARS} characters")
    _ask({"type": {"uid": user.pk, "w": target, "text": text}})
    return f"typed {len(text)} characters in {w.name}"


def _ask(req, timeout=5.0):
    r = push.ask(req, timeout)
    if "error" in r:
        raise DeskError(str(r["error"]))
    return r.get("ok") or {}


def _count(v, what, most):
    if not v.isdigit() or not 1 <= int(v) <= most:
        raise DeskError(f"{what} is a number from 1 to {most}")
    return int(v)


def _key(word):
    name, _, ms = word.partition(":")
    if not name or (ms and not ms.isdigit()):
        raise DeskError(f"a key is <name> or <name>:<ms>, not {word!r}")
    return [name, int(ms) if ms else HOLD_MS]


def _apply(user, words):
    op, args = words[0], words[1:]
    with transaction.atomic():
        ensure_kernel(user)
        if op == "ls":
            return ls(user), False
        if op == "show":
            if len(args) != 1:
                raise DeskError("show <name>")
            w = _get(user, args[0])
            j = widget_json(w)
            out = [f"{k}: {j[k]}" for k in ("name", "kind", "x", "y", "w", "h", "folded", "title", "text",
                                             "pid", "server") if k in j]
            if w.kind in KERNEL.values():
                out.append("cannot be deleted: it is a window of the kernel")
            return "\n".join(out), False
        if op == "help":
            help_text = OPS_HELP
            if settings.FC_PUBLICATIONS_PORT:
                help_text += ("\n  publish <window> <name>    publish <name> <source.lua> on <server>    "
                              "unpublish <name>    visit <name> [<path>]    website")
            return help_text, False
        if op == "man":
            return render_to_string("fc/desk.txt", {"publications": settings.FC_PUBLICATIONS_PORT}).rstrip(), False
        if op == "vs1":
            return vs1_text(), False
        if op == "website" and settings.FC_PUBLICATIONS_PORT:
            return website_text(), False
        if op == "screen":
            p = Profile.objects.get(user=user)
            if not args:
                return (f"{p.desk_w} {p.desk_h}" if p.desk_w else "(the page has not told its size yet)"), False
            if len(args) != 2:
                raise DeskError("screen [<w> <h>]")
            p.desk_w, p.desk_h = _px(args[0], "w"), _px(args[1], "h")
            p.save(update_fields=["desk_w", "desk_h"])
            return f"{p.desk_w} {p.desk_h}", False
        if op == "new":
            return _new(user, args), True
        if op in ("move", "size"):
            if len(args) != 3:
                raise DeskError(f"{op} <name> <{'x' if op == 'move' else 'w'}> <{'y' if op == 'move' else 'h'}>")
            w = _get(user, args[0])
            a, b = _px(args[1], "x" if op == "move" else "w"), _px(args[2], "y" if op == "move" else "h")
            if op == "size":
                _min_size(a, b)
            if op == "move":
                w.x, w.y = a, b
            else:
                w.w, w.h = a, b
            _save(user, w)
            return f"{op} {w.name} {a} {b}", True
        if op in ("fold", "unfold"):
            if len(args) != 1:
                raise DeskError(f"{op} <name>")
            w = _get(user, args[0])
            w.folded = op == "fold"
            _save(user, w)
            return f"{op} {w.name}", True
        if op == "set":
            if len(args) != 3 or args[1] not in ("title", "text"):
                raise DeskError('set <name> title|text "<value>"')
            w = _get(user, args[0])
            if w.kind != "text" and args[1] == "text":
                raise DeskError(f"a {w.kind} has no text to set")
            w.props = {**w.props, args[1]: _bounded(args[1], args[2])}
            _save(user, w)
            return f"set {w.name} {args[1]}", True
        if op == "run":
            return _run(user, args)
        if op == "del":
            if len(args) != 1:
                raise DeskError("del <name>")
            w = _get(user, args[0])
            if w.kind in KERNEL.values():
                raise DeskError(f"the {w.name} cannot be deleted: it is a window of the kernel")
            name = w.name
            w.delete()
            _bump(user)
            return f"del {name}", True
    raise DeskError(f"no op {op!r}: {OPS}")


def _bounded(what, value):
    limit = MAX_TITLE if what == "title" else MAX_TEXT
    if len(value) > limit:
        raise DeskError(f"a {what} is at most {limit} characters")
    return value


def _run(user, args):
    """A desk op in a shell, as if typed there, with or without "desk" in
    front, or clear. Returns (output, whether the desk changed). The command
    and its output go to the person's page through the hub, and the site
    keeps none of it."""
    if len(args) != 2:
        raise DeskError('run <shell> "<op>"')
    w = _get(user, args[0])
    if w.kind != "shell":
        raise DeskError(f"{w.name} is a {w.kind}, not a shell")
    command = args[1].strip()
    if len(command) > ENTRY_CHARS:
        raise DeskError(f"a command is at most {ENTRY_CHARS} characters")
    words = command.split()
    if words and words[0] == "desk":
        command = command[len("desk"):].strip()
        words = words[1:]
    changed, out = False, None
    if not words:
        pass
    elif words[0] == "clear":
        push.later(user.pk, lambda: {"shell": {"name": w.name, "clear": True}})
        return "", False
    elif words[0] == "run":
        out = ("err", "a shell runs desk ops, not run")
    else:
        try:
            text, changed = _apply(user, parse(command))
            out = ("out", text)
        except DeskError as e:
            out = ("err", str(e))
    entries = [{"k": "in", "t": f"desk {command}".strip()}]
    if out and out[1]:
        entries.append({"k": out[0], "t": out[1][:ENTRY_CHARS]})
    push.later(user.pk, lambda: {"shell": {"name": w.name, "entries": entries}})
    return (out[1] if out else ""), changed


NEW_USAGE = ('new text <name> "<text>" [title "<t>"] [at <x> <y>] [size <w> <h>], or '
             'new program <name> <pid> [on <server>] [title "<t>"] [at <x> <y>] [size <w> <h>]')


def _new(user, args):
    kind = args[0] if args else ""
    if kind not in ("text", "program") or len(args) < 3:
        raise DeskError(NEW_USAGE)
    name = _name(args[1])
    if Widget.objects.filter(user=user, name=name).exists():
        raise DeskError(f"there is a widget named {name} already")
    if Widget.objects.filter(user=user).count() >= MAX_WIDGETS:
        raise DeskError(f"a desktop has at most {MAX_WIDGETS} widgets")
    g = dict(DEFAULTS[kind])
    rest = args[3:]
    if kind == "text":
        props = {"title": name, "text": _bounded("text", args[2])}
    else:
        props = {"title": name, "pid": _pid(args[2]),
                 "server": account.here(Profile.objects.get(user=user))}
        if rest[:1] == ["on"]:
            if len(rest) < 2 or not SERVER.match(rest[1]):
                raise DeskError("on <host:port>")
            props["server"] = rest[1]
            rest = rest[2:]
    while rest:
        k = rest[0]
        if k == "title" and len(rest) >= 2:
            props["title"] = _bounded("title", rest[1])
            rest = rest[2:]
        elif k == "at" and len(rest) >= 3:
            g["x"], g["y"] = _px(rest[1], "x"), _px(rest[2], "y")
            rest = rest[3:]
        elif k == "size" and len(rest) >= 3:
            g["w"], g["h"] = _min_size(_px(rest[1], "w"), _px(rest[2], "h"))
            rest = rest[3:]
        else:
            raise DeskError(f"then: title \"<t>\", at <x> <y>, size <w> <h>; not {k!r}")
    w = Widget(user=user, name=name, kind=kind, props=props, **g)
    _save(user, w)
    return f"new {kind} {name}"


def _pid(v):
    """The pid of a program, as `cesh compute ps` shows it."""
    if not v.isdigit() or len(v) > 18:
        raise DeskError(f"a pid is a number, not {v!r}")
    return int(v)

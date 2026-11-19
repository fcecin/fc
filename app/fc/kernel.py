"""The chat kernel: a person's conversation with the ledger's model, paid
with their credit.

The system message holds the rules and the last summary, and the open lines
follow as messages. The request grows only at its end, so the prompt cache
serves its start. Past COMPACT_AT percent of the room, one model call folds
the older lines into a summary.
"""

import json
import logging
import time

from django.conf import settings
from django.core.cache import cache

from . import account, aishell, bridge, push
from . import desk
from .models import ChatLine, Profile

log = logging.getLogger("fc.kernel")

COMPACT_AT = 70
# A think call that fails this fast with CES_ERROR_INTERNAL, before any of the
# answer, never reached the model, so it runs once more.
FAST_FAIL_SECS = 3.0
KEEP = 25
# The model's limits, until a server gives them.
DEFAULT_WINDOW = 65536
DEFAULT_MAX_OUT = 8192
DEFAULT_MAX_INPUT = 1024 * 1024
BUSY_SECS = 400
# The chat lines that a person keeps. Past it, the oldest lines go.
MAX_LINES = 1000
# The start of a user message that the site writes.
SITE = "[site]"
# Model calls for one line of the person: the answer, plus one for each result
# that the model reads.
MAX_CALLS = 24
STOPPED = "The assistant stopped after {} steps for this line. Write to it to go on."
MAX_OPS = 16           # desk commands run from one answer
MAX_COMMANDS = 8       # other commands run from one answer
# The newest part of what the person typed in the shell since their last chat
# line, as the page gives it.
SHELL_TYPED_MAX = 4000

RULES = """You are the assistant of {name}, a website on a CES network. You talk with one person, who pays for each of your answers with their credits. Answer in plain, short words. Say when you do not know something. Do not make up facts.
You work in the person's shell: bash in their home on the site, as the person. Run a command there with the shell tool. The site types each command at the person's prompt, as the person does, so the person sees it and what it prints, and you get the result. The shell keeps its state between commands (the directory, variables). Never exit the shell. The chat shows only your words.
A command runs for 60 seconds at most: then the site presses Ctrl+C. For longer work, run a job (job run --wake CMD, see ~/AGENTS.md): when it ends, the site gives you what it printed, and you go on. A command that fails stops the commands after it in the same answer.
Before you work in the shell in a conversation, read ~/AGENTS.md (cat ~/AGENTS.md). It says how this site works, and the person can change it.
The person's key is {pubkey}, and their server is {server}. The ledger of the site is {ledger}.
The site writes the messages that start with [site]. They tell you what the person typed in their shell, the desk, the jobs that ended, and notes."""

SHELL_TOOL = {"type": "function", "function": {
    "name": "shell",
    "description": "Run one command in the person's shell, and get what it printed and its exit status.",
    "parameters": {"type": "object", "properties": {"command": {
        "type": "string",
        "description": "The command line. It can end with a heredoc (<<END), then its lines, then a line END."}},
        "required": ["command"]}}}

FOLD_RULES = """You fold the older part of a conversation between a person and an assistant into a summary. The summary replaces that part: the assistant will see only the summary and the newer lines.
Keep what the rest of the conversation needs: what the person wants and prefers, the facts that came up, what was decided, and what is still open. Keep exact values. Drop greetings and chatter.
Write plain, short sentences, at most 400 words, with no title and no preamble."""


class ChatError(Exception):
    def __init__(self, code, message, status=400):
        super().__init__(code)
        self.code, self.message, self.status = code, message, status


def _site(line):
    """The text of a site line, as the model reads it."""
    if line.kind == "shell":
        return f"{SITE} In their shell, the person typed this (with what it printed):\n{line.text}"
    return f"{SITE} {line.text}"


def _open(user):
    """The lines after the last fold, less the summaries."""
    return list(ChatLine.objects.filter(user=user, folded=False).exclude(kind="summary"))


def _summary(user):
    return ChatLine.objects.filter(user=user, kind="summary", folded=False).last()


def _head(user):
    s = _summary(user)
    p = Profile.objects.get(user=user)
    head = RULES.format(name=settings.FC_NAME, pubkey=p.pubkey, server=account.here(p),
                        ledger=settings.FC_LEDGER)
    if s is not None:
        head += "\n\n## Earlier in this conversation (a summary)\n\n" + s.text
    return head


def messages(lines):
    """The chat messages of `lines`. Each tool call gets a tool message with
    the result of its command, or "did not run" when no line has the result."""
    out, waiting = [], []

    def add(role, content):
        # Two user messages in a row become one.
        if role == "user" and out and out[-1]["role"] == "user":
            out[-1]["content"] += "\n\n" + content
        else:
            out.append({"role": role, "content": content})

    def close():
        for cid in waiting:
            out.append({"role": "tool", "tool_call_id": cid, "content": "did not run"})
        waiting.clear()

    for x in lines:
        if x.kind == "run" and x.call_id in waiting:
            out.append({"role": "tool", "tool_call_id": x.call_id, "content": x.text})
            waiting.remove(x.call_id)
            continue
        close()
        if x.kind == "user":
            add("user", x.text)
        elif x.kind == "model":
            m = {"role": "assistant", "content": x.text}
            if x.reasoning:
                m["reasoning_content"] = x.reasoning
            if x.calls:
                m["tool_calls"] = x.calls
                waiting.extend(c["id"] for c in x.calls)
            out.append(m)
        else:
            add("user", _site(x))
    close()
    return out


def request(user):
    return {"messages": [{"role": "system", "content": _head(user)}] + messages(_open(user)),
            "tools": [SHELL_TOOL]}


def _size(req):
    return len(json.dumps(req, ensure_ascii=False).encode())


def _last_report(user):
    return (ChatLine.objects.filter(user=user, kind__in=["model", "summary"], prompt_tokens__isnull=False)
            .order_by("-id").first())


def _bytes_per_token(user):
    r = _last_report(user)
    if r and r.prompt_tokens and r.prompt_bytes:
        return r.prompt_bytes / r.prompt_tokens
    return 3.0   # more tokens than most text has, to be safe


def room(user):
    """Tokens for a prompt: the window less the longest reply, up to the
    largest prompt."""
    r = _last_report(user)
    window = (r and r.context_tokens) or DEFAULT_WINDOW
    out = (r and r.max_out_tokens) or DEFAULT_MAX_OUT
    max_in = (r and r.max_input_bytes) or DEFAULT_MAX_INPUT
    tokens = window - out if window > out else window // 2
    return min(tokens, int(max_in / _bytes_per_token(user)))


def context(user):
    """How full the window is, for the status line."""
    r = ChatLine.objects.filter(user=user, kind="model", prompt_tokens__isnull=False).order_by("-id").first()
    used = r.prompt_tokens if r else int(_size(request(user)) / _bytes_per_token(user))
    cached = int(r.cached_tokens * 100 / r.prompt_tokens) if r and r.prompt_tokens and r.cached_tokens is not None else None
    return {"prompt_tokens": used, "room": room(user), "cached_percent": cached, "estimated": r is None}


def _think(profile, req):
    r = bridge.call("think", timeout=settings.FC_THINK_TIMEOUT, sealed=profile.sealed,
                    server=settings.FC_LEDGER, chat=req)
    account.forget(profile, settings.FC_LEDGER)
    return r


def _stats(r, req):
    return dict(charged=r.get("charged"), prompt_bytes=_size(req),
                prompt_tokens=r.get("prompt_tokens"), cached_tokens=r.get("cached_tokens"),
                context_tokens=r.get("context_tokens") or None,
                max_input_bytes=r.get("max_input_bytes") or None,
                max_out_tokens=r.get("max_out_tokens") or None)


def _text(line):
    """A line as the fold reads it."""
    if line.kind == "user":
        return "The person: " + line.text
    if line.kind == "model":
        text = "The assistant: " + line.text
        for c in line.calls or []:
            text += "\nThe assistant ran: " + _command(c)[0]
        return text
    if line.kind == "run":
        return "The shell: " + line.text
    return "The site: " + _site(line)[len(SITE) + 1:]


def _fold(profile):
    """Fold the older lines into the summary. The current line stays."""
    user = profile.user
    lines = _open(user)
    if len(lines) < 2:
        return None
    head = len(_head(user).encode())
    keep = max(0, int((room(user) * _bytes_per_token(user) - head) * KEEP / 100))
    cut = len(lines) - 1
    size = len(_text(lines[-1]).encode())
    while cut > 0 and size + len(_text(lines[cut - 1]).encode()) <= keep:
        cut -= 1
        size += len(_text(lines[cut]).encode())
    folded = lines[:cut]
    if not folded:
        return None
    old = _summary(user)
    ask = ""
    if old is not None:
        ask += "## The summary so far\n\n" + old.text + "\n\n"
    ask += "## The part to fold\n\n" + "\n\n".join(_text(x) for x in folded)
    req = {"messages": [{"role": "system", "content": FOLD_RULES}, {"role": "user", "content": ask}]}
    why = "an empty summary"
    try:
        r = _think(profile, req)
        text = (r.get("reply") or "").strip()
    except bridge.BridgeError as e:
        r, text, why = {}, "", e.code
    if not text:
        text = ((old.text + "\n") if old else "") + "(Older lines were dropped here; they are lost.)"
        note = f"The older conversation could not be folded ({why}); those lines were dropped."
    else:
        note = "The older conversation was folded into a summary."
    ChatLine.objects.filter(id__in=[x.id for x in folded]).update(folded=True)
    if old is not None:
        ChatLine.objects.filter(id=old.id).update(folded=True)
    s = ChatLine.objects.create(user=user, kind="summary", text=text, **(_stats(r, req) if r else {}))
    return note, s


def _command(call):
    """The command of a tool call, and why it cannot run ("" when it can)."""
    fn = call.get("function") or {}
    if fn.get("name") != "shell":
        return "", f"there is no tool {fn.get('name')!r}: the one tool is shell"
    try:
        args = json.loads(fn.get("arguments") or "{}")
    except ValueError:
        return "", "the arguments are not JSON"
    command = args.get("command") if isinstance(args, dict) else None
    if not isinstance(command, str) or not command.strip():
        return "", "the arguments have no command"
    return command, ""


SNAPSHOT = "the desk now"


def _seen(profile):
    """The model knows the desk as it is now (it ran the ops, or read them)."""
    Profile.objects.filter(pk=profile.pk).update(model_rev=Profile.objects.get(pk=profile.pk).desk_rev)


def _desk_now(profile):
    """The desk as it is now, as a "desk" line at the end of the
    conversation, when the model needs it: in a new conversation, after a
    fold, or when the desk changed since the model last saw it."""
    user = profile.user
    p = Profile.objects.get(pk=profile.pk)
    pictured = any(x.kind == "desk" for x in _open(user))
    if pictured and p.model_rev == p.desk_rev:
        return None
    screen = f"{p.desk_w} wide, {p.desk_h} high" if p.desk_w else "size not known"
    line = ChatLine.objects.create(user=user, kind="desk",
                                   text=f"{SNAPSHOT} (the screen: {screen}):\n{desk.ls(user)}")
    _seen(profile)
    return line


def start(profile, text, shell=None, job=None):
    """Check a line of the person and start the turn, one at a time per
    person, with `shell` (what the person ran) and `job` (a waking job's
    report). Returns a generator of ("line", ChatLine) and ("piece", text),
    which frees the turn when it ends or its reader leaves."""
    user = profile.user
    text = (text or "").strip() if isinstance(text, str) else ""
    shell = shell.strip() if isinstance(shell, str) else ""
    job = job.strip() if isinstance(job, str) else ""
    if text:
        mine = push.ask({"mine": {"uid": user.pk}}).get("ok", {}).get("text", "")
        shell = "\n".join(t for t in (mine.strip() if isinstance(mine, str) else "", shell) if t)
    if not text and not shell and not job:
        raise ChatError("BAD_TEXT", "Say something.")
    if len(text) > 20000:
        raise ChatError("BAD_TEXT", "That line is too long.")
    if len(shell) > SHELL_TYPED_MAX:
        shell = "[... older lines]\n" + shell[-SHELL_TYPED_MAX:]
    lock = f"chat:busy:{user.pk}"
    if not cache.add(lock, 1, BUSY_SECS):
        raise ChatError("BUSY", "The model is still answering your last line.", 409)
    return _turn(profile, text, lock, shell, job)


def say(profile, text, shell=None, job=None):
    """A whole turn at once. Returns the new lines."""
    return [x for kind, x in start(profile, text, shell, job) if kind == "line"]


def _think_stream(profile, req):
    """One model call. It yields ("piece", text) as the answer arrives,
    returns the bridge's reply, and logs every failure with its code and
    time."""
    for attempt in (1, 2):
        t0, got = time.monotonic(), False
        pieces = bridge.stream("think", timeout=settings.FC_THINK_TIMEOUT, sealed=profile.sealed,
                               server=settings.FC_LEDGER, chat=req, stream=True)
        try:
            while True:
                msg = next(pieces)
                if msg.get("piece"):
                    got = True
                    yield "piece", msg["piece"]
        except StopIteration as done:
            account.forget(profile, settings.FC_LEDGER)
            return done.value
        except bridge.BridgeError as e:
            secs = time.monotonic() - t0
            log.warning("think of user %s failed: %s after %.1f s, %s, %d bytes of request, try %d",
                        profile.user_id, e.code, secs, "in the answer" if got else "before the answer",
                        _size(req), attempt)
            if attempt == 1 and not got and e.code == "CES_ERROR_INTERNAL" and secs < FAST_FAIL_SECS:
                time.sleep(0.5)
                continue
            raise


def _turn(profile, text, lock, shell="", job=""):
    user = profile.user

    def line(**kw):
        return "line", ChatLine.objects.create(user=user, **kw)
    try:
        if shell:
            yield line(kind="shell", text=shell)
        if text:
            yield line(kind="user", text=text)
        if job:
            yield line(kind="job", text=job)
        seen = _desk_now(profile)
        if seen:
            yield "line", seen
        for _ in range(MAX_CALLS):
            if _size(request(user)) / _bytes_per_token(user) * 100 > room(user) * COMPACT_AT:
                folded = _fold(profile)
                if folded:
                    yield line(kind="note", text=folded[0])
                    # After a fold, give the model the desk again.
                    seen = _desk_now(profile)
                    if seen:
                        yield "line", seen
            req = request(user)
            try:
                r = yield from _think_stream(profile, req)
            except bridge.BridgeError as e:
                why = _why(e.code)
                yield line(kind="note", text=why)
                raise ChatError(e.code, why, 402 if "INSUFFICIENT" in e.code else 502)
            calls = [c for c in r.get("tool_calls") or [] if isinstance(c, dict) and c.get("id")]
            yield line(kind="model", text=(r.get("reply") or "").strip(), calls=calls or None,
                       reasoning=r.get("reasoning_content") or "", **_stats(r, req))
            again = yield from _run(profile, calls)
            _seen(profile)   # the model made these changes, so it knows the desk
            if not again:
                break
        else:
            yield line(kind="note", text=STOPPED.format(MAX_CALLS))
    finally:
        _trim(user)
        cache.delete(lock)


def _run(profile, calls):
    """Run the commands of the model's tool calls, in order, each with its
    result line. Returns True when the model reads a result before it goes
    on."""
    sh = aishell.Shell(profile)
    again = False
    ops = commands = 0
    for c in calls:
        def result(text):
            return "line", ChatLine.objects.create(user=profile.user, kind=aishell.RAN, call_id=c["id"],
                                                   text=text)
        command, why = _command(c)
        if why:
            yield result(f"did not run: {why}")
            again = True
            continue
        if sh.failed:
            yield result(f"did not run, because `{sh.failed}` failed")
            continue
        if command.split(None, 1)[0] == "desk":
            ops += 1
            over = ops > MAX_OPS and f"at most {MAX_OPS} desk commands run from one answer"
        else:
            commands += 1
            over = commands > MAX_COMMANDS and f"at most {MAX_COMMANDS} commands run from one answer"
        if over:
            yield result(f"did not run: {over}")
            again = True
            continue
        kind, out, more = sh.run(command)
        yield result(out)
        again = again or more
    return again


def _trim(user):
    n = ChatLine.objects.filter(user=user).count()
    if n > MAX_LINES:
        old = ChatLine.objects.filter(user=user).order_by("id").values_list("id", flat=True)[:n - MAX_LINES]
        ChatLine.objects.filter(id__in=list(old)).delete()


def _why(code):
    if "INSUFFICIENT" in code or code == "CES_ERROR_ORIGIN_NOT_FOUND":
        return "Not enough credit on the ledger for an answer."
    if code.startswith("BRIDGE"):
        return "The site cannot reach CES now. Try again soon."
    if code in ("CES_ERROR_DISABLED", "NO_FILE_STORE", "CANNOT_CONNECT"):
        return "The ledger does not think now."
    return f"The model did not answer ({code})."


def forget(user):
    ChatLine.objects.filter(user=user).delete()


def line_json(x):
    return {"id": x.id, "kind": x.kind, "text": x.text, "folded": x.folded,
            "charged": x.charged, "at": x.created.isoformat()}

"""The model's commands, typed at the person's prompt (fc/shellhub.py). A
command that fails stops the rest of the answer."""

from . import ceshargs, desk, push

RAN = "run"                # the kind of chat line (fc/models.py) that a line makes
RUN_SECS = 60              # the longest a line runs
MAX_RESULT = 8 * 1024      # the most of one line's output that the model reads


def cap(text, n):
    """The first `n` bytes of `text`, and how many more there were."""
    data = text.encode()
    if len(data) <= n:
        return text
    return data[:n].decode(errors="ignore") + f"\n[... {len(data) - n} more bytes]"


class Shell:
    """The person's shell, for the lines of one model answer."""

    def __init__(self, profile):
        self.profile = profile
        self.failed = None   # the line that failed, which stops the rest

    def fail(self, line, text):
        self.failed = line
        return RAN, text, True

    def run(self, line):
        """Run one command of the model, with its heredoc lines. Returns
        (kind, text, again), with again True when the model reads the result
        before it goes on, and a failed line sets `failed`."""
        head, *rest = line.strip("\n").split("\n")
        head = head.strip()
        _, end = ceshargs.here(head)
        if end is not None and (not rest or rest[-1].strip() != end):
            return self.fail(head, f"`{head}` did not run: its input lines have no end line {end}")
        r = push.ask({"run": {"uid": self.profile.user_id, "line": line.strip("\n"), "timeout": RUN_SECS}},
                     timeout=RUN_SECS + 15)
        if "error" in r:
            return self.fail(head, f"`{head}` did not run: {r['error']}")
        out, result = r["ok"].get("out", ""), r["ok"].get("result", "")
        text = f"ran `{head}`: {result}"
        if out.strip():
            text += "\n" + cap(out.rstrip("\n"), MAX_RESULT)
        if result != "exit 0":
            return self.fail(head, text)
        # A desk op that changes the desk needs no answer, since the model
        # knows the desk that it made.
        prog, _, args = head.partition(" ")
        op = args.split()[0] if args.split() else ""
        return RAN, text, not (prog == "desk" and op and op not in desk.READS)

"""The words of a cesh command line, and the cesh commands that the site's
shell refuses (shell_cesh in fc/hub.py, so this module runs without Django).
A line can end with a heredoc, `<<END` or `<<'END'`, whose lines up to END
are the command's stdin.
"""

import re
import shlex

HEREDOC = re.compile(r"\s<<\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1\s*$")

# Commands with subcommands, which the table names in two words.
GROUPS = {"keys", "keyname", "alias", "file", "compute", "asset", "ramfile", "mail", "autoexec"}
# The options that take no value. Every other option takes one, which is no
# command word. (`-s` is a flag of `keys`, and takes a value on `man`.)
FLAGS = {"-c", "--cache-only", "--extsign", "--hex", "--immutable", "--nonceless", "--off", "--open",
         "--owner-pays", "-p", "--parse-only", "--private", "--public", "-q", "--quiet", "--secp",
         "--secrets", "--trace", "-v", "--verbose", "--wait", "-h", "--help", "--help-all", "--version"}

# The commands that the site's shell refuses, for the person and the model. A
# keys command can show a private key in text that goes to the model.
DISABLED = {
    "chat": "use the chat window",
    "bootstrap": "the site makes your key and chooses your server",
    "mine": "the CPU of the site does not mine",
    "keys": "the site keeps your key, and the model reads this shell",
}


def is_flag(a, words):
    if a == "-s":
        return bool(words) and words[0] == "keys"
    return a in FLAGS


def takes_next(a, words):
    """True when the option `a` takes the next argument as its value. cesh
    (CLI11) reads a short option and its value in one argument ("-lfile"),
    and a group of short flags that ends with an option takes the next
    argument ("-vl file")."""
    if a.startswith("--"):
        return "=" not in a and not is_flag(a, words)
    for i, c in enumerate(a[1:], start=1):
        if not is_flag("-" + c, words):
            return i == len(a) - 1      # its value is the next argument
    return False


def command_words(args):
    """The command words of `cesh <args>`, which are the arguments other than
    options and their values. A lone "-" (stdin or stdout) is a word."""
    words, i = [], 0
    while i < len(args):
        a = args[i]
        if not a or a[0] != "-" or a == "-":
            words.append(a)
        elif takes_next(a, words):
            i += 1          # its value is the next argument
        i += 1
    return words


def disabled(args):
    """Why the site's shell refuses `cesh <args>`, or None."""
    words = command_words(args)
    if words and words[0] in DISABLED:
        return f"{words[0]} is disabled here: {DISABLED[words[0]]}"
    if words and words[0] == "man" and any(a == "--serve" or a.startswith("--serve=") for a in args):
        return "man --serve is disabled here: it starts a web server"
    return None


def split(line):
    """The arguments of a command line, with or without "cesh " in front,
    split as a shell splits words and quotes. None for an open quote."""
    try:
        args = shlex.split(line)
    except ValueError:
        return None
    if args and args[0] == "cesh":
        args = args[1:]
    return args


def here(line):
    """(the line less its heredoc, the end word) when `line` ends with a
    heredoc (<<END), else (line, None)."""
    m = HEREDOC.search(line)
    if not m:
        return line, None
    return line[:m.start()].rstrip(), m.group(2)

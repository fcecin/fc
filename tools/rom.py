"""Build the program window monitor's character ROM from GNU Unifont, whose
.hex file (or .hex.gz) gives every character an 8 by 16 or 16 by 16 glyph.
The ROM has one file per page of 256 characters, <out>/<page>.bin, with page
as code >> 8 in three hex digits, which the monitor fetches when a program
first shows one of its characters. A page is 256 widths (0 for no glyph, 8
or 16), then 256 glyphs of 16 rows of 16 bits, big-endian with the first
pixel in the high bit, and an 8-wide glyph in each row's high byte. The ROM
ships with Unifont's COPYING file.

    python3 -I tools/rom.py unifont-18.0.01.hex.gz app/fc/static/fc/rom
"""

import gzip
import os
import sys

PAGE = 256 + 256 * 32


def glyphs(path):
    """(code, hex bits) for each line of a .hex file."""
    with (gzip.open if path.endswith(".gz") else open)(path, "rt", encoding="ascii") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                code, _, bits = line.partition(":")
                yield int(code, 16), bits


def build(src):
    """The ROM's pages: {page: bytes}."""
    pages = {}
    for code, bits in glyphs(src):
        if len(bits) == 32:
            width, rows = 8, [int(bits[i * 2:i * 2 + 2], 16) << 8 for i in range(16)]
        elif len(bits) == 64:
            width, rows = 16, [int(bits[i * 4:i * 4 + 4], 16) for i in range(16)]
        else:
            raise SystemExit(f"U+{code:04X}: a glyph of {len(bits)} hex digits")
        page = pages.setdefault(code >> 8, bytearray(PAGE))
        g = code & 0xFF
        page[g] = width
        at = 256 + g * 32
        for r, v in enumerate(rows):
            page[at + 2 * r] = v >> 8
            page[at + 2 * r + 1] = v & 0xFF
    return pages


def main(src, out):
    pages = build(src)
    os.makedirs(out, exist_ok=True)
    for p, data in sorted(pages.items()):
        with open(os.path.join(out, f"{p:03x}.bin"), "wb") as f:
            f.write(data)
    n = sum(1 for d in pages.values() for w in d[:256] if w)
    print(f"{len(pages)} pages, {n} glyphs")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    main(sys.argv[1], sys.argv[2])

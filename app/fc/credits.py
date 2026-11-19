"""Credit amounts: 1 credit is 100000000 raw units, and its text is 1.00000000."""

from decimal import Decimal, InvalidOperation

UNIT = 100_000_000
# Half the CES ledger's Int48 balance cap.
MAX_RAW = 2**46


def to_text(raw):
    sign = "-" if raw < 0 else ""
    raw = abs(int(raw))
    return f"{sign}{raw // UNIT}.{raw % UNIT:08d}"


def parse(text):
    """Returns raw units for a positive amount, or None."""
    if not isinstance(text, str) or len(text) > 24:
        return None
    try:
        d = Decimal(text.strip())
    except InvalidOperation:
        return None
    if not d.is_finite() or d <= 0:
        return None
    raw = d * UNIT
    if raw > MAX_RAW or raw != raw.to_integral_value():
        return None
    return int(raw)

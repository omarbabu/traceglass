"""Stable, time-sortable identifiers.

IDs look like ``sp_01jbx3k9q2m4w6c8e0ga`` — a type prefix followed by 20 Crockford base32
characters (48 bits of millisecond time + 52 random bits). They sort by creation time, are
URL-safe, and are unique enough to address any run, span or artifact in a deep link.
"""

from __future__ import annotations

import os
import time

_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"

PREFIXES = {
    "run": "run",
    "span": "sp",
    "event": "ev",
    "artifact": "art",
    "link": "ln",
}


def new_id(kind: str) -> str:
    prefix = PREFIXES.get(kind, kind)
    n = (int(time.time() * 1000) & ((1 << 48) - 1)) << 52 | int.from_bytes(os.urandom(7), "big") >> 4
    chars = []
    for _ in range(20):
        chars.append(_ALPHABET[n & 31])
        n >>= 5
    return f"{prefix}_{''.join(reversed(chars))}"

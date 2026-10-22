"""Turning arbitrary Python values into honest, JSON-safe payloads.

Traceglass distinguishes three situations that most tracers collapse into ``null``:

* the field is **absent** — the instrumentation never reported it;
* the value was **not captured** — we know it existed, but it was not recorded
  (``{"$tg": "not_captured", "reason": ...}``);
* the value is genuinely **empty** — ``null``, ``""``, ``[]``.

Payloads are also redacted (secrets by key name and by value pattern) and truncated
before they ever leave the process. Every transformation leaves a ``$tg`` marker so the UI
can say exactly what happened instead of silently showing less.
"""

from __future__ import annotations

import dataclasses
import math
import re
from typing import Any, Iterable, Optional, Pattern, Sequence, Tuple

MARKER = "$tg"


def not_captured(reason: Optional[str] = None) -> dict:
    """A value that existed but was not recorded (e.g. a framework hid tool arguments)."""
    m = {MARKER: "not_captured"}
    if reason:
        m["reason"] = reason
    return m


NOT_CAPTURED = not_captured()


def is_marker(value: Any, kind: Optional[str] = None) -> bool:
    return isinstance(value, dict) and MARKER in value and (kind is None or value[MARKER] == kind)


DEFAULT_SECRET_KEYS = (
    r"api[-_]?key",
    r"secret",
    r"passw(or)?d",
    r"passwd",
    r"(access|auth|bearer|refresh|id|session)[-_]?token",
    r"^token$",
    r"authorization",
    r"cookie",
    r"private[-_]?key",
    r"access[-_]?key",
    r"credentials?",
)

DEFAULT_SECRET_PATTERNS: Tuple[Tuple[str, str], ...] = (
    ("anthropic-key", r"sk-ant-[A-Za-z0-9_\-]{20,}"),
    ("openai-key", r"sk-(proj-)?[A-Za-z0-9_\-]{20,}"),
    ("aws-access-key", r"\b(AKIA|ASIA)[0-9A-Z]{16}\b"),
    ("github-token", r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    ("slack-token", r"\bxox[abprs]-[A-Za-z0-9\-]{10,}\b"),
    ("bearer-token", r"(?i)\bbearer\s+[A-Za-z0-9\-._~+/]{16,}=*"),
    ("private-key", r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
)


class Redactor:
    """Redacts secrets by dictionary key and by value pattern.

    ``Redactor(keys=[...], patterns=[("name", regex)])`` extends the defaults;
    pass ``defaults=False`` to start from scratch.
    """

    def __init__(
        self,
        keys: Iterable[str] = (),
        patterns: Iterable[Tuple[str, str]] = (),
        defaults: bool = True,
    ) -> None:
        key_list = list(DEFAULT_SECRET_KEYS if defaults else ()) + list(keys)
        self._key_re: Optional[Pattern[str]] = re.compile("|".join(f"(?:{k})" for k in key_list), re.I) if key_list else None
        pats = list(DEFAULT_SECRET_PATTERNS if defaults else ()) + list(patterns)
        self._patterns: Sequence[Tuple[str, Pattern[str]]] = [(name, re.compile(p)) for name, p in pats]

    def key_is_secret(self, key: str) -> bool:
        return bool(self._key_re and self._key_re.search(key))

    def scrub(self, text: str) -> str:
        for name, pat in self._patterns:
            text = pat.sub(f"‹redacted:{name}›", text)
        return text


class NoRedaction(Redactor):
    def __init__(self) -> None:
        super().__init__(defaults=False)


@dataclasses.dataclass
class CaptureConfig:
    max_string: int = 32_000
    max_items: int = 500
    max_depth: int = 12
    redactor: Redactor = dataclasses.field(default_factory=Redactor)


def capture(value: Any, cfg: Optional[CaptureConfig] = None) -> Any:
    """Convert ``value`` into a JSON-safe structure, applying redaction and size limits."""
    return _capture(value, cfg or CaptureConfig(), 0)


def _capture(v: Any, cfg: CaptureConfig, depth: int) -> Any:
    if v is None or isinstance(v, bool) or isinstance(v, int):
        return v
    if isinstance(v, float):
        return v if math.isfinite(v) else str(v)
    if isinstance(v, str):
        s = cfg.redactor.scrub(v)
        if len(s) > cfg.max_string:
            return {MARKER: "truncated", "preview": s[: cfg.max_string], "length": len(s)}
        return s
    if is_marker(v):
        return v
    if depth >= cfg.max_depth:
        return {MARKER: "truncated", "reason": "max_depth", "repr": _safe_repr(v)}
    if isinstance(v, (bytes, bytearray, memoryview)):
        return {MARKER: "bytes", "size": len(v)}
    if isinstance(v, dict):
        out = {}
        for i, (k, val) in enumerate(v.items()):
            if i >= cfg.max_items:
                out[MARKER + ":more"] = len(v) - cfg.max_items
                break
            key = str(k)
            if cfg.redactor.key_is_secret(key) and val not in (None, ""):
                out[key] = {MARKER: "redacted", "reason": "secret-key"}
            else:
                out[key] = _capture(val, cfg, depth + 1)
        return out
    if isinstance(v, (list, tuple, set, frozenset)):
        items = list(v)
        out_list = [_capture(x, cfg, depth + 1) for x in items[: cfg.max_items]]
        if len(items) > cfg.max_items:
            out_list.append({MARKER: "truncated", "omitted_items": len(items) - cfg.max_items})
        return out_list
    if dataclasses.is_dataclass(v) and not isinstance(v, type):
        return _capture(dataclasses.asdict(v), cfg, depth)
    for attr in ("model_dump", "dict", "to_dict"):  # pydantic v2 / v1 / common conventions
        fn = getattr(v, attr, None)
        if callable(fn):
            try:
                return _capture(fn(), cfg, depth)
            except Exception:
                break
    return {MARKER: "repr", "type": type(v).__name__, "repr": cfg.redactor.scrub(_safe_repr(v))[:2000]}


def _safe_repr(v: Any) -> str:
    try:
        return repr(v)
    except Exception:  # pragma: no cover - pathological __repr__
        return f"<unrepresentable {type(v).__name__}>"

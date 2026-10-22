"""Shared scaffolding for the demo agents.

The demos are deterministic: "models" return scripted replies and "tools" return canned
data, so they run offline with no API keys. Everything they *record*, though, goes through
the public Traceglass API exactly the way a real agent's instrumentation would.

Two pieces of demo-only machinery live here and nowhere else:

* ``demo_run`` opens a run on a tracer whose clock is compressed (``ScaledClock``) so a
  realistic 90-second execution takes a few seconds of wall time. The run is tagged with
  ``metadata.time_scale`` so the UI never passes it off as real timing.
* ``pace(seconds)`` stands in for latency (network, model, test suite) in *simulated* seconds.
"""

from __future__ import annotations

import contextlib
import dataclasses
import random
import time
from typing import Any, Callable, Dict, Iterator, List, Optional

import traceglass as tg

OPUS = "claude-opus-5-5"
SONNET = "claude-sonnet-5-5"
HAIKU = "claude-haiku-5-5"

# USD per million tokens (input, output) — Anthropic first-party list prices.
PRICES = {OPUS: (4.00, 20.00), SONNET: (2.00, 10.00), HAIKU: (0.10, 0.50)}

_pace = {"factor": 1.0, "fast": False}


def pace(seconds: float) -> None:
    """Simulate ``seconds`` of latency on the demo's (possibly compressed) clock."""
    if not _pace["fast"] and seconds > 0:
        time.sleep(seconds / _pace["factor"])


def cost(model: str, input_tokens: int, output_tokens: int) -> float:
    pin, pout = PRICES.get(model, (0.0, 0.0))
    return round((input_tokens * pin + output_tokens * pout) / 1_000_000, 6)


def estimate_tokens(text: str) -> int:
    return max(1, int(len(text) / 3.7))


@contextlib.contextmanager
def demo_run(name: str, *, time_scale: float, fast: bool, expected_seconds: float = 0, **run_kwargs: Any) -> Iterator[tg.Run]:
    """Open a run whose clock runs ``time_scale``× faster than real time (demo-only helper).

    The compressed clock is back-dated by ``expected_seconds`` (the run's simulated length) so
    timestamps never land in the future: the run *ends* at roughly the real current time.
    """
    sink = tg.get_tracer().sink  # keep whatever sink the caller configured
    clock: Callable[[], float] = tg.recorder.wall_clock
    if time_scale != 1:
        scaled = tg.ScaledClock(time_scale)
        if not fast:
            scaled._anchor -= expected_seconds * 1000.0
        clock = scaled
    tracer = tg.Tracer(sink, clock=clock)
    _pace.update(factor=time_scale, fast=fast)
    metadata = dict(run_kwargs.pop("metadata", None) or {})
    metadata.update(demo=True, deterministic=True)
    with tg.run(name, tracer=tracer, metadata=metadata, **run_kwargs) as run:
        yield run
    tracer.flush()


@dataclasses.dataclass
class Reply:
    text: str
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None


class ScriptedModel:
    """A stand-in for a chat-completion client that returns pre-written replies.

    ``chat()`` records an ``llm`` span the way you would instrument a real client:
    the request messages as input, the assistant message as output, model, usage and cost.
    """

    def __init__(self, model: str, seed: int = 7) -> None:
        self.model = model
        self._rng = random.Random(seed)

    def chat(
        self,
        name: str,
        *,
        system: str,
        messages: List[Dict[str, Any]],
        reply: str,
        latency: float = 3.0,
        context_tokens: int = 0,
        **attributes: Any,
    ) -> str:
        with tg.span(name, kind="llm", model=self.model, input={"system": system, "messages": messages}) as s:
            s.set_attributes(temperature=0.2, max_tokens=8192, **attributes)
            pace(latency)
            prompt = system + "".join(str(m.get("content", "")) for m in messages)
            tin = estimate_tokens(prompt) + context_tokens + self._rng.randint(40, 160)
            tout = estimate_tokens(reply) + self._rng.randint(5, 40)
            s.set_output({"role": "assistant", "content": reply, "stop_reason": "end_turn"})
            s.usage(input_tokens=tin, output_tokens=tout, cost_usd=cost(self.model, tin, tout))
            return reply


class RateLimitError(Exception):
    """HTTP 429 from a (simulated) upstream API."""

    def __init__(self, message: str, retry_after: float) -> None:
        super().__init__(message)
        self.retry_after = retry_after

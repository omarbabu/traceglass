"""Showcase agents. Run them with ``traceglass demo [name]``.

All demos except ``claude`` are deterministic and offline: scripted model replies, canned
tool data, realistic content. They record through the public Traceglass API, so their source
doubles as instrumentation examples.
"""

from __future__ import annotations

import dataclasses
import importlib
from typing import Callable, Dict


@dataclasses.dataclass(frozen=True)
class Demo:
    name: str
    title: str
    description: str
    module: str
    in_all: bool = True

    def run(self, fast: bool = False) -> str:
        """Run the demo and return the new run id."""
        fn: Callable[..., str] = importlib.import_module(f"{__name__}.{self.module}").run
        return fn(fast=fast)


REGISTRY: Dict[str, Demo] = {
    d.name: d
    for d in [
        Demo("research", "Deep research agent",
             "Parallel sub-researchers, a 429 retry, and a report whose claims cite exact source passages.", "research"),
        Demo("coding", "Coding agent",
             "Reproduces a flaky test, patches it (diff artifact), proves the fix, and waits for maintainer approval.", "coding"),
        Demo("incident", "Incident triage",
             "A commander delegates to four specialists; one fails permanently and the report flags the gap.", "incident"),
        Demo("stress", "Stress test (500+ spans)",
             "12 subagents with ~45 operations each, for checking legibility at scale.", "stress"),
        Demo("live", "Live run (~25s, real time)",
             "A travel-planning agent that runs in real time so you can watch the graph build.", "live"),
        Demo("claude", "Real Claude agent",
             "A real tool-use loop against the Anthropic API (needs credentials). Not included in 'all'.", "claude", in_all=False),
    ]
}

__all__ = ["Demo", "REGISTRY"]

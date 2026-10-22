"""Stress test: an orchestrator with 12 subagents and 500+ spans.

Built for checking that the UI stays legible at scale: collapsing subtrees, filtering by
kind, finding a node, and jumping between errors. Instead of sleeping, it drives a
*virtual clock* so the trace spans ~9 simulated minutes but records in well under a second
— the same technique an importer uses to replay historical traces with their original times.
"""

from __future__ import annotations

import contextlib
import random
from typing import Iterator, List

import traceglass as tg

from ._common import HAIKU, OPUS, SONNET, cost

TOOLS = ["read_file", "grep", "run_query", "http_get", "list_dir", "run_tests", "lint", "fetch_ticket"]
AREAS = ["auth", "billing", "search", "notifications", "checkout", "inventory",
         "profiles", "analytics", "shipping", "pricing", "reviews", "gateway"]


class VirtualClock:
    """A settable clock (ms since epoch)."""

    def __init__(self, start_ms: float) -> None:
        self.t = start_ms

    def __call__(self) -> float:
        return self.t


@contextlib.contextmanager
def at(clock: VirtualClock, start: float, duration: float, span: tg.Span) -> Iterator[tg.Span]:
    clock.t = start
    with span as s:
        yield s
        clock.t = start + duration


def run(fast: bool = False) -> str:
    rng = random.Random(500)
    clock = VirtualClock(tg.recorder.wall_clock() - 10 * 60_000)
    tracer = tg.Tracer(tg.get_tracer().sink, clock=clock)
    t0 = clock.t
    subagent_spans: List[tg.Span] = []
    with tg.run("Stress: monorepo audit with 12 subagents", project="stress", session="scale-test", tracer=tracer,
                input={"task": "Audit all 12 services for deprecated API usage"}, tags=["stress", "500+ spans"],
                metadata={"demo": True, "deterministic": True, "virtual_clock": True}) as r:
        last_end = t0
        with tg.span("orchestrator", kind="agent", model=OPUS) as orch:
            with at(clock, t0 + 200, 6000, tg.span("plan audit", kind="llm", model=OPUS)) as plan:
                plan.set_output({"role": "assistant", "content": f"Split into {len(AREAS)} service audits, run in 3 waves of 4."})
                plan.usage(input_tokens=2400, output_tokens=380, cost_usd=cost(OPUS, 2400, 380))

            for i, area in enumerate(AREAS):
                wave_start = t0 + 7000 + (i // 4) * 165_000 + (i % 4) * 900
                cursor = wave_start
                clock.t = wave_start
                with tg.span(f"auditor: {area}", kind="agent", model=SONNET, input={"service": area}) as agent:
                    findings = 0
                    for it in range(10):
                        d = rng.randint(1800, 4200)
                        with at(clock, cursor, d, tg.span(f"think #{it + 1}", kind="llm", model=SONNET)) as think:
                            tin, tout = rng.randint(1500, 9000), rng.randint(80, 600)
                            think.set_output({"role": "assistant", "content": f"Next: inspect {area} module {it + 1}."})
                            think.usage(input_tokens=tin, output_tokens=tout, cost_usd=cost(SONNET, tin, tout))
                        cursor += d + 150
                        for k in range(rng.randint(2, 4)):
                            tool = rng.choice(TOOLS)
                            d = rng.randint(300, 4000)
                            fails = rng.random() < 0.04
                            with at(clock, cursor, d, tg.span(tool, kind="tool", input={"target": f"{area}/mod{it}/{k}"})) as call:
                                if fails:
                                    call.fail(TimeoutError(f"{tool} timed out after {d}ms"))
                                else:
                                    call.set_output({"ok": True, "matches": rng.randint(0, 7)})
                            cursor += d + 80
                            if fails:
                                retry = rng.randint(400, 2500)
                                with at(clock, cursor, retry, tg.span(tool, kind="tool", attempt=2, retry_of=call,
                                                                      input={"target": f"{area}/mod{it}/{k}"})) as again:
                                    again.set_output({"ok": True, "matches": rng.randint(0, 3)})
                                cursor += retry + 80
                        if it % 2 == 1:
                            d = rng.randint(800, 2400)
                            with at(clock, cursor, d, tg.span("search docs", kind="retrieval")) as ret:
                                doc = tg.artifact(f"{area}: deprecation notes #{it}", f"Deprecated call sites in {area} (batch {it}).",
                                                  kind="document")
                                ret.set_output({"documents": [doc.id]})
                                findings += 1
                            cursor += d + 80
                    with at(clock, cursor, 2500, tg.span("summarize", kind="llm", model=HAIKU)) as summ:
                        summ.set_output({"role": "assistant", "content": f"{area}: {findings} deprecated usages found."})
                        summ.usage(input_tokens=5200, output_tokens=240, cost_usd=cost(HAIKU, 5200, 240))
                        tg.artifact(f"{area} audit summary", f"# {area}\n\n{findings} deprecated usages found.", kind="markdown")
                    agent.set_output({"findings": findings})
                    clock.t = cursor + 2500 + rng.randint(300, 1500)
                    last_end = max(last_end, clock.t)
                subagent_spans.append(agent)

            with at(clock, last_end + 1500, 7000, tg.span("merge findings", kind="llm", model=OPUS, depends_on=subagent_spans)) as merge:
                merge.set_output({"role": "assistant", "content": "Consolidated report across 12 services."})
                merge.usage(input_tokens=22_000, output_tokens=1800, cost_usd=cost(OPUS, 22_000, 1800))
                tg.artifact("Monorepo deprecation audit", "# Audit\n\nConsolidated findings across 12 services.", kind="markdown", final=True)
            orch.set_output({"services": len(AREAS)})
            clock.t = last_end + 9500
        clock.t += 300
    return r.id

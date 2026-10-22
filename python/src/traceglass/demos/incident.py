"""Incident triage: an incident-commander agent delegates to four specialists in parallel.

What it shows in Traceglass:
  * ``delegation`` spans wrapping subagents — handoffs are first-class, not just nesting
  * a subagent that fails permanently (3 timed-out attempts) and the *downstream effect*:
    the commander continues with degraded evidence and says so
  * explicit ``depends_on`` edges from the synthesis step to each specialist
  * a human approval gate (``waiting`` → approved) before a rollback is executed
  * an incident report whose claims cite log excerpts, the deploy history and the runbook
"""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, Optional

import traceglass as tg

from ._common import HAIKU, OPUS, SONNET, ScriptedModel, cost, demo_run, pace

ALERT = {
    "id": "INC-4821",
    "service": "checkout-api",
    "signal": "p99 latency 4.8s (SLO 800ms) for 12 min, error rate 3.1%",
    "started": "2026-10-21T14:02:00Z",
    "severity": "SEV-2",
}

LOG_EXCERPT = """2026-10-21T14:02:11Z WARN  checkout-api  payments-client  upstream payments-api slow response elapsed_ms=4210 route=/v2/authorize
2026-10-21T14:02:13Z ERROR checkout-api  payments-client  pool exhausted: 64/64 connections in use, waited 3000ms
2026-10-21T14:02:13Z ERROR checkout-api  http  POST /checkout 504 upstream_timeout trace=8f1c2a
2026-10-21T14:02:19Z WARN  payments-api  db  slow query 3.9s: SELECT * FROM payment_methods WHERE customer_id = $1 ORDER BY created_at
2026-10-21T14:02:20Z ERROR checkout-api  payments-client  pool exhausted: 64/64 connections in use, waited 3000ms
2026-10-21T14:02:31Z INFO  payments-api  deploy  running version=v2.14.0 commit=a91e0d7
"""

DEPLOYS = [
    {"service": "payments-api", "version": "v2.14.0", "at": "2026-10-21T13:58:40Z", "author": "ci-bot", "change": "Remove composite index payment_methods(customer_id, created_at) (migration 0147)"},
    {"service": "checkout-web", "version": "v8.3.2", "at": "2026-10-21T11:20:05Z", "author": "m.osei", "change": "Copy update on order summary"},
    {"service": "inventory", "version": "v5.0.9", "at": "2026-10-20T22:41:51Z", "author": "ci-bot", "change": "Dependency bumps"},
]

RUNBOOK = """# Runbook: checkout latency (payments path)

## Symptoms
High p99 on POST /checkout with `pool exhausted` errors from payments-client.

## Likely causes
1. Slow queries in payments-api saturating the connection pool.
2. A recent payments-api deploy changing query plans or indexes.

## Mitigation
If a payments-api deploy in the last 60 minutes correlates with the onset, roll it back first and investigate after. Rollbacks of payments-api require approval from the payments on-call.
"""

REPORT = """# INC-4821 — checkout latency spike (SEV-2)

**Status:** mitigated by rollback · **Impact:** ~3.1% of checkouts failed for 14 minutes.

## What happened
payments-api v2.14.0 removed the composite index on payment_methods(customer_id, created_at). The payment-methods lookup degraded to a 3.9s query, which held connections in the payments-client pool until it was exhausted, so checkout requests timed out with 504s.

## Evidence
- The payments-client pool was saturated at 64/64 connections during the spike.
- payments-api began logging 3.9s slow queries on payment_methods four minutes after the deploy.
- The deploy history shows the index removal shipped at 13:58, four minutes before onset.
- The runbook directs rolling back a correlated payments-api deploy first.

## Gaps
Metrics were unavailable (the metrics backend timed out three times), so the latency figures come from the alert, not independent measurement.

## Actions
1. Rolled back payments-api to v2.13.4 (approved by payments on-call).
2. Restore the index in a forward migration before re-deploying v2.14.x.
3. Add a query-plan regression check to the migration pipeline.
"""

commander_model = ScriptedModel(OPUS, seed=31)
analyst_model = ScriptedModel(SONNET, seed=32)
cheap_model = ScriptedModel(HAIKU, seed=33)

EVIDENCE: Dict[str, tg.Artifact] = {}


class MetricsTimeout(TimeoutError):
    pass


# --- tools -----------------------------------------------------------------------------
@tg.tool
def query_logs(service: str, since: str, level: str = "WARN") -> dict:
    pace(3.2)
    EVIDENCE["logs"] = tg.artifact("log excerpt: checkout-api + payments-api", LOG_EXCERPT, kind="text", mime="text/plain",
                                   metadata={"source": "logs", "lines": LOG_EXCERPT.count("\n")})
    return {"matched": 1843, "returned": 6, "artifact": EVIDENCE["logs"].id}


@tg.tool
def list_deploys(window: str) -> list:
    pace(1.1)
    EVIDENCE["deploys"] = tg.artifact("deploy history (24h)", json.dumps(DEPLOYS, indent=2), kind="json", mime="application/json")
    return DEPLOYS


@tg.retrieval("search_runbooks")
def search_runbooks(query: str) -> list:
    pace(1.6)
    EVIDENCE["runbook"] = tg.artifact("Runbook: checkout latency (payments path)", RUNBOOK, kind="document",
                                      uri="wiki://sre/runbooks/checkout-latency", metadata={"score": 0.91})
    return [{"title": "Runbook: checkout latency (payments path)", "score": 0.91}]


@tg.tool
def rollback_deploy(service: str, to_version: str) -> dict:
    pace(18.0)
    return {"service": service, "version": to_version, "healthy": True, "p99_ms_after": 310}


def query_metrics_with_retries(query: str) -> dict:
    previous: Optional[tg.Span] = None
    for attempt in range(1, 4):
        with tg.span("query_metrics", kind="tool", input={"promql": query, "range": "30m"}, attempt=attempt, retry_of=previous) as s:
            pace(10.0)  # the metrics backend never answers
            s.fail(MetricsTimeout(f"metrics-gateway did not respond within 10s (attempt {attempt}/3)"))
            previous = s
        if attempt < 3:
            pace(1.5 * attempt)  # backoff
    raise MetricsTimeout("metrics backend unavailable after 3 attempts")


# --- specialists -----------------------------------------------------------------------
def log_analyst() -> str:
    query_logs("checkout-api,payments-api", since="14:00Z")
    return analyst_model.chat(
        "cluster errors",
        system="You are an SRE log analyst. Cluster error lines and name the most likely failure chain.",
        messages=[{"role": "user", "content": LOG_EXCERPT}],
        reply="Two clusters: (1) payments-client pool exhaustion → 504s on /checkout; (2) a 3.9s slow query on payment_methods in payments-api. (2) likely causes (1).",
        latency=5.5,
    )


def metrics_analyst() -> str:
    data = query_metrics_with_retries('histogram_quantile(0.99, sum(rate(http_server_duration_bucket{service="checkout-api"}[1m])) by (le))')
    return str(data)


def runbook_retriever() -> str:
    search_runbooks("checkout latency pool exhausted payments")
    return cheap_model.chat(
        "extract mitigation",
        system="Extract the mitigation steps that apply to the incident.",
        messages=[{"role": "user", "content": RUNBOOK}],
        reply="Applies: correlated payments-api deploy → roll back first; needs payments on-call approval.",
        latency=1.8,
    )


def deploy_historian() -> str:
    deploys = list_deploys("24h")
    return cheap_model.chat(
        "correlate deploys",
        system="Find deploys that correlate with the incident onset.",
        messages=[{"role": "user", "content": json.dumps({"onset": ALERT["started"], "deploys": deploys})}],
        reply="payments-api v2.14.0 deployed at 13:58:40Z, 3m20s before onset; it drops an index used by the payment-methods lookup.",
        latency=2.2,
    )


SPECIALISTS: Dict[str, Callable[[], str]] = {
    "log-analyst": log_analyst,
    "metrics-analyst": metrics_analyst,
    "runbook-retriever": runbook_retriever,
    "deploy-historian": deploy_historian,
}
MODELS = {"log-analyst": SONNET, "metrics-analyst": SONNET, "runbook-retriever": HAIKU, "deploy-historian": HAIKU}


def delegate(name: str, brief: str) -> Dict[str, Any]:
    """Hand a brief to a specialist subagent. Returns the delegation span and the finding (or error)."""
    with tg.span(f"delegate → {name}", kind="delegation", input={"to": name, "brief": brief}) as handoff:
        try:
            with tg.span(name, kind="agent", model=MODELS[name], input={"brief": brief}) as agent:
                finding = SPECIALISTS[name]()
                agent.set_output({"finding": finding})
            handoff.set_output({"status": "completed", "finding": finding})
            return {"span": handoff, "finding": finding, "error": None}
        except Exception as exc:
            handoff.fail(exc)
            return {"span": handoff, "finding": None, "error": f"{type(exc).__name__}: {exc}"}


def run(fast: bool = False) -> str:
    EVIDENCE.clear()
    with demo_run(
        "Incident triage: checkout latency spike",
        project="sre-copilot",
        session=ALERT["id"],
        task="incident-triage",
        input={"alert": ALERT},
        tags=["incident", "multi-agent", "approval", "degraded"],
        time_scale=25,
        fast=fast,
        expected_seconds=112,
    ) as r:
        with tg.agent("incident-commander", model=OPUS) as commander:
            commander_model.chat(
                "assess alert",
                system="You are the incident commander. Triage fast, delegate in parallel, and never act without evidence.",
                messages=[{"role": "user", "content": json.dumps(ALERT)}],
                reply="SEV-2 on checkout-api. Delegating in parallel: logs, metrics, runbooks, recent deploys.",
                latency=3.0,
            )
            briefs = {
                "log-analyst": "Cluster WARN/ERROR logs for checkout-api and its upstreams since 14:00Z.",
                "metrics-analyst": "Pull p99 latency, error rate and pool saturation for checkout-api, 30m window.",
                "runbook-retriever": "Find the runbook for checkout latency with pool exhaustion symptoms.",
                "deploy-historian": "List deploys in the last 24h across checkout dependencies; correlate with onset.",
            }
            with tg.ThreadPoolExecutor(max_workers=4) as pool:
                results = dict(zip(briefs, pool.map(lambda kv: delegate(*kv), briefs.items())))

            failed = [n for n, res in results.items() if res["error"]]
            for name in failed:
                commander.event("degraded evidence", kind="log", level="warn",
                                message=f"{name} failed ({results[name]['error']}); continuing without it and flagging the gap.",
                                data={"missing": name})

            with tg.span("synthesize hypothesis", kind="llm", model=OPUS,
                         depends_on=[res["span"] for res in results.values()]) as synth:
                findings = {n: res["finding"] or f"UNAVAILABLE: {res['error']}" for n, res in results.items()}
                synth.set_input({"messages": [{"role": "user", "content": json.dumps(findings, indent=2)}]})
                synth.uses(*EVIDENCE.values())
                pace(6.0)
                hypothesis = "payments-api v2.14.0 dropped an index → slow payment-methods query → pool exhaustion → checkout 504s. Recommend rollback."
                synth.set_output({"role": "assistant", "content": hypothesis, "confidence": "high", "gaps": failed})
                synth.usage(input_tokens=3920, output_tokens=402, cost_usd=cost(OPUS, 3920, 402))

            with tg.span("on-call approval: roll back payments-api", kind="approval",
                         input={"action": "rollback", "service": "payments-api", "to": "v2.13.4"}) as gate:
                gate.set_status("waiting")
                gate.event("paged", kind="log", message="Paged payments on-call (@r.tanaka) for rollback approval")
                pace(45.0)
                gate.set_attributes(approver="r.tanaka", decision="approved", channel="#inc-4821")
                gate.set_output({"decision": "approved"})

            rollback_deploy("payments-api", "v2.13.4")
            commander.event("mitigated", kind="state", message="p99 back to 310ms within 3 minutes of rollback")

            with tg.span("write incident report", kind="llm", model=SONNET, depends_on=[synth]) as writer:
                writer.set_input({"messages": [{"role": "user", "content": "Write the incident report."}]})
                pace(5.0)
                writer.set_output({"role": "assistant", "content": REPORT})
                writer.usage(input_tokens=4410, output_tokens=520, cost_usd=cost(SONNET, 4410, 520))
                report = tg.artifact("INC-4821 incident report", REPORT, kind="markdown", final=True,
                                     metadata={"severity": "SEV-2", "status": "mitigated"})
                logs, deploys, runbook = EVIDENCE["logs"], EVIDENCE["deploys"], EVIDENCE["runbook"]
                report.cite(logs, anchor="The payments-client pool was saturated at 64/64 connections during the spike.",
                            quote="pool exhausted: 64/64 connections in use, waited 3000ms")
                report.cite(logs, anchor="payments-api began logging 3.9s slow queries on payment_methods four minutes after the deploy.",
                            quote="slow query 3.9s: SELECT * FROM payment_methods WHERE customer_id = $1 ORDER BY created_at")
                report.cite(deploys, anchor="The deploy history shows the index removal shipped at 13:58, four minutes before onset.",
                            quote="Remove composite index payment_methods(customer_id, created_at) (migration 0147)")
                report.cite(runbook, anchor="The runbook directs rolling back a correlated payments-api deploy first.",
                            quote="If a payments-api deploy in the last 60 minutes correlates with the onset, roll it back first and investigate after.")
                report.cite(results["metrics-analyst"]["span"], relation="derived_from",
                            anchor="Metrics were unavailable (the metrics backend timed out three times)")
                report.cite(gate, relation="approved_by", anchor="approved by payments on-call")
            commander.set_output({"status": "mitigated", "report": report.id})
        r.set_output({"status": "mitigated", "root_cause": "index removed in payments-api v2.14.0"})
        return r.id

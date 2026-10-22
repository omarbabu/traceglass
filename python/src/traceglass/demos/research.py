"""Deep research agent: a lead researcher fans out to three sub-researchers, then writes a
cited report.

What it shows in Traceglass:
  * parallel subagents (fork/join in the graph), each with search → fetch → summarize
  * a 429 rate limit on one search, retried with backoff (``attempt`` / ``retry_of``)
  * retrieved passages recorded as ``document`` artifacts
  * a final report whose every claim is linked to the exact quote that supports it —
    including one claim the evidence *contradicts*
  * a citation-check guardrail and a fact-check step before the answer ships
"""

from __future__ import annotations

from typing import Dict, List

import traceglass as tg

from ._common import HAIKU, OPUS, SONNET, RateLimitError, ScriptedModel, cost, demo_run, pace

QUESTION = "Do retrieval-augmented agents hallucinate less than closed-book models — and when does retrieval stop helping?"

# --- the (synthetic) literature --------------------------------------------------------
# Fictional-but-plausible papers. Every quote the report cites appears verbatim below.
CORPUS: Dict[str, Dict[str, str]] = {
    "okafor2025": {
        "title": "Grounded or Guessing? Measuring Hallucination in Retrieval-Augmented Agents",
        "authors": "A. Okafor, L. Brandt, S. Iyer",
        "venue": "TACL 2025 (synthetic)",
        "url": "https://example.org/papers/okafor2025",
        "text": (
            "We evaluate eleven agent configurations on GroundQA, a benchmark of multi-hop questions "
            "with annotated evidence chains. Across 4,812 multi-hop questions, retrieval-augmented agents "
            "produced unsupported claims in 9.4% of answers, compared with 21.7% for the same models "
            "answering closed-book. The improvement was largest for long-tail entities, where closed-book "
            "error rates exceeded 35%. We find that the benefit depends strongly on retrieval quality: "
            "agents given gold passages hallucinated in only 3.1% of answers."
        ),
    },
    "liang2025": {
        "title": "FaithBench: Auditing Citation Faithfulness in Tool-Using Agents",
        "authors": "M. Liang, R. Castellanos",
        "venue": "EMNLP 2025 (synthetic)",
        "url": "https://example.org/papers/liang2025",
        "text": (
            "Citations are often treated as evidence of grounding, but rarely audited. We collected 19,300 "
            "agent-generated citations and asked three annotators to judge each. Only 71% of citations fully "
            "supported the sentence they were attached to; a further 18% were partially supporting, and 11% "
            "were unrelated to the claim. Citation accuracy degraded when agents summarized more than six "
            "sources in a single generation step."
        ),
    },
    "haddad2024": {
        "title": "When Retrieval Hurts: Distractor Passages and Confident Errors",
        "authors": "N. Haddad, P. Sorensen, J. Wu",
        "venue": "ACL 2024 (synthetic)",
        "url": "https://example.org/papers/haddad2024",
        "text": (
            "Retrieval is usually assumed to be monotonically helpful. We show it is not. When one of the "
            "top-5 retrieved passages contained a plausible distractor, the error rate rose to 27.3%, "
            "exceeding the closed-book baseline. Errors in this regime were also stated with higher "
            "expressed confidence, making them harder for reviewers to catch."
        ),
    },
    "brandt2025": {
        "title": "Verify-then-Answer: Self-Verification Loops for Tool-Using Agents",
        "authors": "L. Brandt, K. Nakamura",
        "venue": "ICLR 2025 (synthetic)",
        "url": "https://example.org/papers/brandt2025",
        "text": (
            "We add an explicit verification pass in which the agent re-retrieves evidence for each claim "
            "before answering. Adding an explicit verify-then-answer step reduced unsupported claims by a "
            "further 38% relative, at the cost of 1.6x latency. Verification was most effective for numeric "
            "claims and least effective for causal claims."
        ),
    },
    "osei2025": {
        "title": "How Much Context Is Enough? Retrieval Depth and Answer Quality",
        "authors": "E. Osei, T. Varga",
        "venue": "NAACL 2025 (synthetic)",
        "url": "https://example.org/papers/osei2025",
        "text": (
            "We vary the number of retrieved passages from 1 to 32. Faithfulness improved steadily up to "
            "eight passages. Gains saturated beyond eight retrieved passages; adding more context increased "
            "latency without improving faithfulness. Reranking recovered most of the benefit of deeper "
            "retrieval at a quarter of the token cost."
        ),
    },
    "romero2025": {
        "title": "Long-Horizon Agents Drift From Their Sources",
        "authors": "C. Romero, D. Abiola, H. Lindqvist",
        "venue": "NeurIPS 2025 (synthetic)",
        "url": "https://example.org/papers/romero2025",
        "text": (
            "We trace provenance for every claim produced by agents on 600 research tasks. In tasks exceeding "
            "20 tool calls, the share of claims traceable to a retrieved source fell from 88% to 61%. Most "
            "untraceable claims were introduced during intermediate summarization, not in the final answer."
        ),
    },
}

SUBTOPICS = [
    {
        "agent": "researcher: benchmark evidence",
        "query": "retrieval augmented agents hallucination rate benchmark closed-book comparison",
        "papers": ["okafor2025", "osei2025"],
        "model": SONNET,
    },
    {
        "agent": "researcher: citation faithfulness",
        "query": "citation faithfulness audit LLM agents attributed claims",
        "papers": ["liang2025", "brandt2025"],
        "model": SONNET,
        "rate_limited": True,
    },
    {
        "agent": "researcher: failure modes",
        "query": "retrieval distractor passages errors long-horizon agents provenance drift",
        "papers": ["haddad2024", "romero2025"],
        "model": SONNET,
    },
]

REPORT = """# Do retrieval-augmented agents hallucinate less?

**Short answer:** usually yes — by roughly half on multi-hop questions — but retrieval is not a guarantee, and the benefit erodes with poor retrieval and long tool chains.

## Findings

1. **Retrieval roughly halves unsupported claims.** On a 4,812-question multi-hop benchmark, retrieval-augmented agents produced unsupported claims in 9.4% of answers versus 21.7% closed-book.
2. **Retrieval quality is the dominant factor.** With gold passages, the hallucination rate dropped to 3.1%.
3. **Retrieval reliably reduces hallucination in every setting.** This common assumption does not hold: a single plausible distractor in the top-5 passages pushed the error rate to 27.3%, worse than answering closed-book.
4. **Citations are not proof of grounding.** Only 71% of agent citations fully supported the sentence they were attached to.
5. **Verification helps, at a price.** A verify-then-answer pass cut unsupported claims by a further 38% relative, with 1.6x latency.
6. **More context is not always better.** Faithfulness gains saturate beyond eight retrieved passages.
7. **Long runs drift from their sources.** Beyond 20 tool calls, the share of traceable claims fell from 88% to 61%.

## Recommendations

- Rerank and cap retrieval depth around eight passages.
- Add a verification pass for numeric claims.
- Audit citations instead of trusting their presence, and keep provenance for intermediate summaries.

*Sources are synthetic papers generated for this Traceglass demo.*
"""

# (claim anchor in REPORT, paper id, quote in paper, relation)
CITATIONS = [
    ("retrieval-augmented agents produced unsupported claims in 9.4% of answers versus 21.7% closed-book", "okafor2025",
     "retrieval-augmented agents produced unsupported claims in 9.4% of answers, compared with 21.7% for the same models answering closed-book", "supported_by"),
    ("With gold passages, the hallucination rate dropped to 3.1%.", "okafor2025",
     "agents given gold passages hallucinated in only 3.1% of answers", "supported_by"),
    ("Retrieval reliably reduces hallucination in every setting.", "haddad2024",
     "When one of the top-5 retrieved passages contained a plausible distractor, the error rate rose to 27.3%, exceeding the closed-book baseline.", "contradicted_by"),
    ("a single plausible distractor in the top-5 passages pushed the error rate to 27.3%, worse than answering closed-book", "haddad2024",
     "When one of the top-5 retrieved passages contained a plausible distractor, the error rate rose to 27.3%, exceeding the closed-book baseline.", "supported_by"),
    ("Only 71% of agent citations fully supported the sentence they were attached to.", "liang2025",
     "Only 71% of citations fully supported the sentence they were attached to", "supported_by"),
    ("A verify-then-answer pass cut unsupported claims by a further 38% relative, with 1.6x latency.", "brandt2025",
     "Adding an explicit verify-then-answer step reduced unsupported claims by a further 38% relative, at the cost of 1.6x latency.", "supported_by"),
    ("Faithfulness gains saturate beyond eight retrieved passages.", "osei2025",
     "Gains saturated beyond eight retrieved passages; adding more context increased latency without improving faithfulness.", "supported_by"),
    ("Beyond 20 tool calls, the share of traceable claims fell from 88% to 61%.", "romero2025",
     "In tasks exceeding 20 tool calls, the share of claims traceable to a retrieved source fell from 88% to 61%.", "supported_by"),
]

lead = ScriptedModel(OPUS, seed=11)
worker = ScriptedModel(SONNET, seed=12)
fast_model = ScriptedModel(HAIKU, seed=13)


# --- tools -----------------------------------------------------------------------------
_search_calls: Dict[str, int] = {}


def web_search(query: str, max_results: int = 5, simulate_429: bool = False) -> List[Dict[str, str]]:
    """A (simulated) academic search API. The first call for a rate-limited topic gets HTTP 429."""
    pace(1.4)
    n = _search_calls[query] = _search_calls.get(query, 0) + 1
    if simulate_429 and n == 1:
        raise RateLimitError("429 Too Many Requests: scholar-index quota exceeded (retry-after: 2s)", retry_after=2.0)
    hits = [pid for st in SUBTOPICS if st["query"] == query for pid in st["papers"]]
    return [{"id": pid, "title": CORPUS[pid]["title"], "url": CORPUS[pid]["url"], "venue": CORPUS[pid]["venue"]} for pid in hits][:max_results]


def search_with_backoff(query: str, simulate_429: bool) -> List[Dict[str, str]]:
    """Call ``web_search`` up to 3 times, honouring ``retry-after`` — each attempt is its own span."""
    previous = None
    for attempt in range(1, 4):
        with tg.span("web_search", kind="tool", input={"query": query, "max_results": 5},
                     attempt=attempt, retry_of=previous) as s:
            try:
                hits = web_search(query, simulate_429=simulate_429)
                s.set_output(hits)
                return hits
            except RateLimitError as exc:
                s.fail(exc)
                s.event("backoff", kind="retry", level="warn", message=f"sleeping {exc.retry_after:.0f}s before retry",
                        data={"retry_after_s": exc.retry_after, "next_attempt": attempt + 1})
                previous = s
        pace(2.0)
    raise RuntimeError("search failed after 3 attempts")


@tg.retrieval("fetch_and_chunk")
def fetch_and_chunk(hits: List[Dict[str, str]]) -> List[str]:
    """Download each paper and keep the most relevant passage as a document artifact."""
    ids = []
    for hit in hits:
        pace(0.9)
        paper = CORPUS[hit["id"]]
        doc = tg.artifact(
            f"{paper['title']}",
            paper["text"],
            kind="document",
            uri=paper["url"],
            metadata={"paper_id": hit["id"], "authors": paper["authors"], "venue": paper["venue"], "synthetic": True},
        )
        DOCS[hit["id"]] = doc
        ids.append(doc.id)
    return ids


DOCS: Dict[str, tg.Artifact] = {}


# --- agents ----------------------------------------------------------------------------
def research_subtopic(topic: dict) -> str:
    with tg.span(topic["agent"], kind="agent", model=topic["model"], input={"query": topic["query"]}) as agent:
        hits = search_with_backoff(topic["query"], simulate_429=topic.get("rate_limited", False))
        fetch_and_chunk(hits)
        docs = [DOCS[h["id"]] for h in hits]
        notes = worker.chat(
            "summarize findings",
            system="You are a careful research assistant. Summarize only what the passages state. Quote numbers exactly.",
            messages=[{"role": "user", "content": "\n\n".join(d.content for d in docs)}],
            reply=" ".join(f"[{h['id']}] {CORPUS[h['id']]['text'].split('. ')[1]}." for h in hits),
            latency=4.5,
        )
        agent.uses(*docs)
        agent.set_output({"notes": notes, "sources": [h["id"] for h in hits]})
        return notes


def run(fast: bool = False) -> str:
    _search_calls.clear()
    DOCS.clear()
    with demo_run(
        "Deep research: Do retrieval-augmented agents hallucinate less?",
        project="research-agent",
        session="lit-review · oct",
        task="literature-review",
        input={"question": QUESTION},
        tags=["research", "multi-agent", "citations"],
        time_scale=20,
        fast=fast,
        expected_seconds=32,
    ) as r:
        with tg.span("lead-researcher", kind="agent", model=OPUS, input={"question": QUESTION}) as lead_agent:
            plan = lead.chat(
                "plan research",
                system="You are the lead researcher. Break the question into independent sub-questions for parallel researchers.",
                messages=[{"role": "user", "content": QUESTION}],
                reply=(
                    "Plan:\n1. Benchmark evidence: hallucination rates with vs. without retrieval.\n"
                    "2. Citation faithfulness: do citations actually support claims?\n"
                    "3. Failure modes: when does retrieval hurt (distractors, long horizons)?"
                ),
                latency=5.0,
            )

            with tg.ThreadPoolExecutor(max_workers=3) as pool:
                futures = [pool.submit(research_subtopic, t) for t in SUBTOPICS]
                notes = [f.result() for f in futures]

            claims = fast_model.chat(
                "extract claims",
                system="Extract atomic, checkable claims with their source ids. One claim per line.",
                messages=[{"role": "user", "content": "\n".join(notes)}],
                reply="\n".join(f"- {c[0]} [{c[1]}]" for c in CITATIONS),
                latency=2.0,
            )

            with tg.span("synthesize report", kind="llm", model=OPUS,
                         input={"system": "Write a decision-ready answer. Every claim must cite a source passage.",
                                "messages": [{"role": "user", "content": f"{QUESTION}\n\nPlan:\n{plan}\n\nClaims:\n{claims}"}]}) as synth:
                synth.uses(*DOCS.values())
                pace(9.0)
                synth.set_output({"role": "assistant", "content": REPORT})
                synth.usage(input_tokens=6840, output_tokens=1210, cost_usd=cost(OPUS, 6840, 1210))
                report = tg.artifact("Research report", REPORT, kind="markdown", final=True,
                                     metadata={"audience": "engineering leadership", "words": len(REPORT.split())})
                for anchor, pid, quote, relation in CITATIONS:
                    report.cite(DOCS[pid], anchor=anchor, quote=quote, relation=relation)

            with tg.guardrail("citation check", policy="every-claim-cited") as check:
                pace(1.2)
                unresolved = [c[0] for c in CITATIONS if c[0] not in REPORT or c[2] not in CORPUS[c[1]]["text"]]
                check.set_input({"claims": len(CITATIONS)})
                check.set_output({"checked": len(CITATIONS), "unresolved": unresolved,
                                  "contradicted": sum(1 for c in CITATIONS if c[3] == "contradicted_by")})

            with tg.step("fact-check numeric claims") as fc:
                pace(2.5)
                fc.event("flagged", kind="log", level="warn",
                         message="Finding 3 states a belief the evidence contradicts — kept, explicitly labelled as a misconception.")
                fc.set_output({"numeric_claims": 9, "verified": 9, "mismatches": 0})

            lead_agent.set_output({"report": report.id})
        r.set_output({"answer": REPORT.split("\n\n")[1]})
        return r.id

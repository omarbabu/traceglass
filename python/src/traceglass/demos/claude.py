"""A real Claude tool-use agent, traced with Traceglass (needs the ``anthropic`` package and
credentials, e.g. ``ANTHROPIC_API_KEY``).

    pip install "traceglass[anthropic]"
    traceglass demo claude

The client is instrumented with ``traceglass.integrations.anthropic.instrument`` so every
``messages.create`` call becomes an ``llm`` span with the real request, response, model and
token usage. Tools are plain Python functions decorated with ``@tg.tool``; documents the
agent reads are recorded as artifacts, and the final answer is linked to them.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

import traceglass as tg

from .research import CORPUS

MODEL = "claude-opus-5-5"
QUESTION = (
    "Using only the local paper corpus, explain whether retrieval-augmented agents hallucinate less than "
    "closed-book models, and name one situation where retrieval makes things worse. Cite paper ids."
)

TOOLS = [
    {
        "name": "search_corpus",
        "description": "Keyword search over a small local corpus of research papers. Returns ids and titles.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "Keywords to search for."}},
            "required": ["query"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "read_paper",
        "description": "Read the abstract of one paper by id.",
        "input_schema": {
            "type": "object",
            "properties": {"paper_id": {"type": "string", "description": "Paper id from search_corpus."}},
            "required": ["paper_id"],
            "additionalProperties": False,
        },
        "strict": True,
    },
]

_read: Dict[str, tg.Artifact] = {}


@tg.tool
def search_corpus(query: str) -> List[Dict[str, str]]:
    words = {w.lower() for w in query.split() if len(w) > 2}
    scored = []
    for pid, paper in CORPUS.items():
        text = (paper["title"] + " " + paper["text"]).lower()
        score = sum(text.count(w) for w in words)
        if score:
            scored.append((score, pid))
    return [{"id": pid, "title": CORPUS[pid]["title"]} for _, pid in sorted(scored, reverse=True)[:4]]


@tg.tool
def read_paper(paper_id: str) -> str:
    paper = CORPUS.get(paper_id)
    if paper is None:
        raise KeyError(f"unknown paper id {paper_id!r}")
    if paper_id not in _read:
        _read[paper_id] = tg.artifact(paper["title"], paper["text"], kind="document", uri=paper["url"],
                                      metadata={"paper_id": paper_id, "synthetic": True})
    return paper["text"]


HANDLERS = {"search_corpus": search_corpus, "read_paper": read_paper}


def run(fast: bool = False) -> str:
    try:
        import anthropic
    except ImportError:
        raise SystemExit('The claude demo needs the Anthropic SDK: pip install "traceglass[anthropic]"')
    try:
        from ..integrations.anthropic import instrument
    except ImportError:
        raise SystemExit("traceglass.integrations.anthropic is unavailable in this install")

    client = instrument(anthropic.Anthropic())
    _read.clear()
    with tg.run("Claude agent: retrieval vs. hallucination", project="claude-agent", input={"question": QUESTION},
                tags=["real-llm", "tool-use"]) as r:
        with tg.agent("claude-researcher", model=MODEL):
            messages: List[Dict[str, Any]] = [{"role": "user", "content": QUESTION}]
            for _ in range(12):
                try:
                    response = client.messages.create(
                        model=MODEL,
                        max_tokens=16000,
                        system="You are a careful research assistant. Use the tools; cite paper ids in [brackets].",
                        tools=TOOLS,
                        messages=messages,
                        output_config={"effort": "medium"},
                        # Server-side refusal fallback (routes declined requests to a suitable model).
                        extra_headers={"anthropic-beta": "server-side-fallback-2026-07-01"},
                        extra_body={"fallbacks": "default"},
                    )
                except (anthropic.AuthenticationError, TypeError) as exc:
                    if isinstance(exc, TypeError) and "authentication" not in str(exc):
                        raise
                    raise SystemExit("No valid Anthropic credentials: set ANTHROPIC_API_KEY (or run `ant auth login`).")
                if response.stop_reason == "refusal":
                    raise SystemExit(f"Request declined: {response.stop_details}")
                if response.stop_reason != "tool_use":
                    break
                messages.append({"role": "assistant", "content": response.content})
                results = []
                for block in response.content:
                    if block.type != "tool_use":
                        continue
                    try:
                        out = HANDLERS[block.name](**block.input)
                        results.append({"type": "tool_result", "tool_use_id": block.id,
                                        "content": out if isinstance(out, str) else json.dumps(out)})
                    except Exception as exc:
                        results.append({"type": "tool_result", "tool_use_id": block.id, "content": str(exc), "is_error": True})
                messages.append({"role": "user", "content": results})
            answer = "".join(b.text for b in response.content if b.type == "text")
            final = tg.artifact("Answer", answer, kind="markdown", final=True)
            for pid, doc in _read.items():
                if f"[{pid}]" in answer or pid in answer:
                    final.cite(doc, anchor=pid, relation="cites")
                else:
                    final.derived_from(doc, note="read by the agent, not cited explicitly")
        r.set_output({"answer": answer})
        return r.id

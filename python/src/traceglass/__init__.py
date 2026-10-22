"""Traceglass — a visual, navigable record of how an AI agent arrived at an answer.

    import traceglass as tg

    with tg.run("Compare papers", project="research-agent", input=question):
        with tg.span("search", kind="tool", input=query) as s:
            s.set_output(hits)
        report = tg.artifact("Report", text, kind="markdown", final=True)
        report.cite(doc, anchor="claim in the report", quote="passage in the source")

Then ``traceglass serve`` and open http://127.0.0.1:7357.
"""

from ._version import __version__

# Public API, loaded lazily (PEP 562): `import traceglass` stays cheap for hot paths such as
# `traceglass hook`, while `tg.run`, `from traceglass import span` etc. behave as usual.
_LAZY = {
    "ARTIFACT_KINDS": "recorder",
    "Artifact": "recorder",
    "FanoutSink": "sinks",
    "HTTPSink": "sinks",
    "JSONLSink": "sinks",
    "MemorySink": "sinks",
    "NOT_CAPTURED": "capture",
    "NullSink": "sinks",
    "Redactor": "capture",
    "Run": "recorder",
    "SPAN_KINDS": "recorder",
    "SQLiteSink": "sinks",
    "ScaledClock": "recorder",
    "Span": "recorder",
    "ThreadPoolExecutor": "recorder",
    "Tracer": "recorder",
    "agent": "recorder",
    "artifact": "recorder",
    "bind": "recorder",
    "configure": "recorder",
    "current_run": "recorder",
    "current_span": "recorder",
    "flush": "recorder",
    "get_tracer": "recorder",
    "guardrail": "recorder",
    "link": "recorder",
    "llm": "recorder",
    "not_captured": "capture",
    "observe": "recorder",
    "retrieval": "recorder",
    "run": "recorder",
    "span": "recorder",
    "start_span": "recorder",
    "step": "recorder",
    "tool": "recorder",
    "trace": "recorder",
}


def __getattr__(name: str):
    mod = _LAZY.get(name)
    if mod is None:
        raise AttributeError(f"module 'traceglass' has no attribute {name!r}")
    import importlib

    value = getattr(importlib.import_module(f".{mod}", __name__), name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(_LAZY))


__all__ = [
    "__version__",
    "ARTIFACT_KINDS",
    "SPAN_KINDS",
    "NOT_CAPTURED",
    "Artifact",
    "FanoutSink",
    "HTTPSink",
    "JSONLSink",
    "MemorySink",
    "NullSink",
    "Redactor",
    "Run",
    "SQLiteSink",
    "ScaledClock",
    "Span",
    "ThreadPoolExecutor",
    "Tracer",
    "agent",
    "artifact",
    "bind",
    "configure",
    "current_run",
    "current_span",
    "flush",
    "get_tracer",
    "guardrail",
    "link",
    "llm",
    "not_captured",
    "observe",
    "retrieval",
    "run",
    "span",
    "start_span",
    "step",
    "tool",
    "trace",
]

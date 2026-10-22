"""The recording API: runs, spans, events, artifacts and evidence links.

Everything here is framework-neutral. Integrations (LangChain, Anthropic, OTLP, Claude Code
transcripts) are thin translators onto these same primitives.
"""

from __future__ import annotations

import atexit
import concurrent.futures
import contextvars
import functools
import inspect
import itertools
import threading
import time
import traceback
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, TypeVar, Union

from ._version import __version__
from .capture import CaptureConfig, NoRedaction, Redactor, capture, not_captured
from .ids import new_id
from .sinks import Sink, default_sink

F = TypeVar("F", bound=Callable[..., Any])

_MISSING: Any = object()

SPAN_KINDS = ("agent", "llm", "tool", "retrieval", "delegation", "approval", "guardrail", "embedding", "step")
STATUSES = ("running", "ok", "error", "cancelled", "skipped", "waiting")


# ---------------------------------------------------------------------------------------
# Clocks
# ---------------------------------------------------------------------------------------
def wall_clock() -> float:
    """Milliseconds since the epoch."""
    return time.time() * 1000.0


class ScaledClock:
    """A clock that runs ``factor`` times faster than real time from the moment it is created.

    Demos use it to compress a realistic 90-second agent run into a few seconds of wall
    time; runs recorded with it are tagged ``metadata.time_scale`` so nobody is misled.
    """

    def __init__(self, factor: float) -> None:
        self.factor = factor
        self._anchor = time.time() * 1000.0
        self._mono = time.monotonic()

    def __call__(self) -> float:
        return self._anchor + (time.monotonic() - self._mono) * 1000.0 * self.factor


# ---------------------------------------------------------------------------------------
# Tracer
# ---------------------------------------------------------------------------------------
class Tracer:
    def __init__(
        self,
        sink: Optional[Sink] = None,
        *,
        project: Optional[str] = None,
        redact: Union[bool, Redactor] = True,
        max_payload_chars: int = 32_000,
        capture_inputs: bool = True,
        capture_outputs: bool = True,
        clock: Callable[[], float] = wall_clock,
    ) -> None:
        self._sink = sink
        self.project = project
        redactor = redact if isinstance(redact, Redactor) else (Redactor() if redact else NoRedaction())
        self.capture_cfg = CaptureConfig(max_string=max_payload_chars, redactor=redactor)
        self.capture_inputs = capture_inputs
        self.capture_outputs = capture_outputs
        self.clock = clock
        self._seq = itertools.count(1)
        self._seq_lock = threading.Lock()

    @property
    def sink(self) -> Sink:
        if self._sink is None:
            self._sink = default_sink()
        return self._sink

    def now(self) -> float:
        return self.clock()

    def next_seq(self) -> int:
        with self._seq_lock:
            return next(self._seq)

    def capture(self, value: Any) -> Any:
        return capture(value, self.capture_cfg)

    def emit(self, record: Dict[str, Any]) -> None:
        record.setdefault("v", 1)
        try:
            self.sink.emit(record)
        except Exception:  # pragma: no cover - tracing must never break the agent
            pass

    def flush(self, timeout: Optional[float] = 10) -> None:
        if self._sink is not None:
            self._sink.flush(timeout)


_tracer: Optional[Tracer] = None
_tracer_lock = threading.Lock()


def get_tracer() -> Tracer:
    global _tracer
    if _tracer is None:
        with _tracer_lock:
            if _tracer is None:
                _tracer = Tracer()
    return _tracer


def configure(sink: Optional[Sink] = None, **kwargs: Any) -> Tracer:
    """Configure the global tracer.

    ``configure(project="research-agent")`` · ``configure(sink=HTTPSink(...))`` ·
    ``configure(redact=Redactor(keys=["ssn"]))`` · ``configure(clock=ScaledClock(20))``
    """
    global _tracer
    old = _tracer
    with _tracer_lock:
        _tracer = Tracer(sink, **kwargs)
    if old is not None and old._sink is not None and old._sink is not sink:
        old.flush(5)
    return _tracer


def flush(timeout: Optional[float] = 10) -> None:
    """Block until all buffered records are written."""
    if _tracer is not None:
        _tracer.flush(timeout)


atexit.register(lambda: flush(5))


_current_run: contextvars.ContextVar[Optional["Run"]] = contextvars.ContextVar("traceglass_run", default=None)
_current_span: contextvars.ContextVar[Optional["Span"]] = contextvars.ContextVar("traceglass_span", default=None)


def current_run() -> Optional["Run"]:
    return _current_run.get()


def current_span() -> Optional["Span"]:
    return _current_span.get()


def _error_payload(exc: BaseException, tracer: Tracer) -> Dict[str, Any]:
    stack = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    return {
        "type": type(exc).__name__,
        "message": tracer.capture_cfg.redactor.scrub(str(exc)),
        "stack": tracer.capture_cfg.redactor.scrub(stack)[-8000:],
    }


def _ref(obj: Any) -> Dict[str, Any]:
    if isinstance(obj, Span):
        return {"kind": "span", "id": obj.id}
    if isinstance(obj, Artifact):
        return {"kind": "artifact", "id": obj.id}
    if isinstance(obj, dict) and "id" in obj:
        return {"kind": obj.get("kind") or ("artifact" if str(obj["id"]).startswith("art_") else "span"), "id": obj["id"]}
    if isinstance(obj, str):
        return {"kind": "artifact" if obj.startswith("art_") else "span", "id": obj}
    raise TypeError(f"cannot link to {obj!r}; pass a Span, Artifact or id")


# ---------------------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------------------
class Run:
    """One invocation of a task. Use as a context manager: ``with tg.run("Compare papers"):``."""

    def __init__(
        self,
        name: str,
        *,
        project: Optional[str] = None,
        session: Optional[str] = None,
        task: Optional[str] = None,
        input: Any = _MISSING,
        tags: Sequence[str] = (),
        metadata: Optional[Dict[str, Any]] = None,
        source: str = "sdk",
        id: Optional[str] = None,
        tracer: Optional[Tracer] = None,
    ) -> None:
        self.tracer = tracer or get_tracer()
        self.id = id or new_id("run")
        self.name = name
        self.project = project or self.tracer.project or "default"
        self.session = session
        self.task = task
        self.tags = list(tags)
        self.metadata = dict(metadata or {})
        if isinstance(self.tracer.clock, ScaledClock):
            self.metadata.setdefault("time_scale", self.tracer.clock.factor)
        self.source = source
        self.status = "running"
        self.started_at: Optional[float] = None
        self.ended_at: Optional[float] = None
        self.final_artifact_id: Optional[str] = None
        self._input = _MISSING if input is _MISSING else self.tracer.capture(input)
        self._output: Any = _MISSING
        self._token: Optional[contextvars.Token] = None
        self._span_token: Optional[contextvars.Token] = None

    # lifecycle -------------------------------------------------------------------------
    def start(self, at: Optional[float] = None) -> "Run":
        if self.started_at is None:
            self.started_at = at if at is not None else self.tracer.now()
            self._emit()
        return self

    def end(self, status: Optional[str] = None, output: Any = _MISSING, at: Optional[float] = None) -> None:
        if self.ended_at is not None:
            return
        if output is not _MISSING:
            self.set_output(output)
        self.status = status or ("ok" if self.status == "running" else self.status)
        self.ended_at = at if at is not None else self.tracer.now()
        self._emit()
        self.tracer.flush()

    def __enter__(self) -> "Run":
        self.start()
        self._token = _current_run.set(self)
        self._span_token = _current_span.set(None)
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if exc is not None:
            self.status = "error"
            self.metadata["error"] = _error_payload(exc, self.tracer)
        self.end()
        if self._span_token is not None:
            _current_span.reset(self._span_token)
        if self._token is not None:
            _current_run.reset(self._token)

    # data ------------------------------------------------------------------------------
    def set_input(self, value: Any) -> "Run":
        self._input = self.tracer.capture(value)
        return self

    def set_output(self, value: Any) -> "Run":
        self._output = self.tracer.capture(value)
        return self

    def set_metadata(self, **kw: Any) -> "Run":
        self.metadata.update(self.tracer.capture(kw))
        return self

    def set_final(self, artifact: "Artifact") -> "Run":
        """Mark the artifact that *is* this run's answer; the UI opens it by default."""
        self.final_artifact_id = artifact.id
        return self

    def record(self) -> Dict[str, Any]:
        rec: Dict[str, Any] = {
            "type": "run",
            "id": self.id,
            "name": self.name,
            "project": self.project,
            "session": self.session,
            "task": self.task,
            "status": self.status,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "tags": self.tags,
            "metadata": self.metadata,
            "source": self.source,
            "sdk": {"name": "traceglass-python", "version": __version__},
        }
        if self._input is not _MISSING:
            rec["input"] = self._input
        if self._output is not _MISSING:
            rec["output"] = self._output
        if self.final_artifact_id:
            rec["final_artifact_id"] = self.final_artifact_id
        return rec

    def _emit(self) -> None:
        self.tracer.emit(self.record())


# ---------------------------------------------------------------------------------------
# Span
# ---------------------------------------------------------------------------------------
class Span:
    """A bounded operation: an agent turn, an LLM call, a tool execution, a retrieval…"""

    def __init__(
        self,
        name: str,
        *,
        kind: str = "step",
        input: Any = _MISSING,
        parent: Union["Span", str, None, Any] = _MISSING,
        run: Optional[Run] = None,
        agent: Optional[str] = None,
        model: Optional[str] = None,
        attributes: Optional[Dict[str, Any]] = None,
        depends_on: Iterable[Union["Span", str]] = (),
        retry_of: Union["Span", str, None] = None,
        attempt: Optional[int] = None,
        id: Optional[str] = None,
        tracer: Optional[Tracer] = None,
    ) -> None:
        active = run or current_run()
        self.tracer = tracer or (active.tracer if active else get_tracer())
        self.id = id or new_id("span")
        self.name = name
        self.kind = kind
        self.model = model
        self.attributes: Dict[str, Any] = dict(attributes or {})
        self.depends: List[str] = [d.id if isinstance(d, Span) else d for d in depends_on]
        self.retry_of = retry_of.id if isinstance(retry_of, Span) else retry_of
        self.attempt = attempt
        self.status = "running"
        self.started_at: Optional[float] = None
        self.ended_at: Optional[float] = None
        self.error_info: Optional[Dict[str, Any]] = None
        self.usage_info: Optional[Dict[str, Any]] = None
        self._input = _MISSING if input is _MISSING else self.tracer.capture(input)
        self._output: Any = _MISSING
        self._parent_arg = parent
        self._run = run
        self._owned_run: Optional[Run] = None
        self._agent = agent
        self.parent_id: Optional[str] = None
        self.run_id: Optional[str] = None
        self.seq: Optional[int] = None
        self._tokens: List[contextvars.Token] = []

    # lifecycle -------------------------------------------------------------------------
    def start(self, at: Optional[float] = None) -> "Span":
        if self.started_at is not None:
            return self
        run = self._run or current_run()
        if run is None:
            # No enclosing run: this span becomes the root of an implicit run named after it.
            run = Run(self.name, tracer=self.tracer, input=_MISSING)
            run._input = self._input
            run.start(at)
            self._owned_run = run
        self._run = run
        self.run_id = run.id
        if self._parent_arg is _MISSING:
            parent = current_span()
            parent = parent if parent is not None and parent.run_id == run.id else None
        else:
            parent = self._parent_arg
        self.parent_id = parent.id if isinstance(parent, Span) else parent
        if self._agent is None:
            if self.kind == "agent":
                self._agent = self.name
            elif isinstance(parent, Span):
                self._agent = parent._agent
        self.seq = self.tracer.next_seq()
        self.started_at = at if at is not None else self.tracer.now()
        self._emit()
        return self

    def end(self, status: Optional[str] = None, output: Any = _MISSING, at: Optional[float] = None) -> "Span":
        if self.started_at is None:
            self.start(at)
        if self.ended_at is not None:
            return self
        if output is not _MISSING:
            self.set_output(output)
        if status:
            self.status = status
        elif self.status in ("running", "waiting"):
            self.status = "ok"
        self.ended_at = at if at is not None else self.tracer.now()
        self._emit()
        if self._owned_run is not None:
            if self._output is not _MISSING:
                self._owned_run._output = self._output
            self._owned_run.end(status="error" if self.status == "error" else "ok", at=self.ended_at)
        return self

    def __enter__(self) -> "Span":
        self.start()
        if self._owned_run is not None:
            self._tokens.append(_current_run.set(self._owned_run))
        self._tokens.append(_current_span.set(self))
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if exc is not None and self.status != "error":
            self.record_error(exc)
        self.end()
        for tok in reversed(self._tokens):
            tok.var.reset(tok)
        self._tokens.clear()

    # data ------------------------------------------------------------------------------
    def set_input(self, value: Any) -> "Span":
        self._input = self.tracer.capture(value)
        return self

    def set_output(self, value: Any) -> "Span":
        self._output = self.tracer.capture(value)
        return self

    def mark_not_captured(self, field: str, reason: str) -> "Span":
        """Declare that ``input``/``output`` existed but the integration could not see it."""
        if field == "input":
            self._input = not_captured(reason)
        elif field == "output":
            self._output = not_captured(reason)
        else:
            self.attributes[field] = not_captured(reason)
        return self

    def set_attribute(self, key: str, value: Any) -> "Span":
        self.attributes[key] = self.tracer.capture(value)
        return self

    def set_attributes(self, **kw: Any) -> "Span":
        for k, v in kw.items():
            self.set_attribute(k, v)
        return self

    def set_status(self, status: str) -> "Span":
        """Change status mid-flight (e.g. an approval gate ``waiting`` on a human); emitted immediately."""
        self.status = status
        if self.started_at is not None and self.ended_at is None:
            self._emit()
        return self

    def set_model(self, model: str) -> "Span":
        self.model = model
        return self

    def usage(
        self,
        input_tokens: Optional[int] = None,
        output_tokens: Optional[int] = None,
        *,
        cache_read_tokens: Optional[int] = None,
        cache_write_tokens: Optional[int] = None,
        cost_usd: Optional[float] = None,
    ) -> "Span":
        u = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_tokens": cache_read_tokens,
            "cache_write_tokens": cache_write_tokens,
            "cost_usd": cost_usd,
        }
        self.usage_info = {**(self.usage_info or {}), **{k: v for k, v in u.items() if v is not None}}
        return self

    def depends_on(self, *spans: Union["Span", str]) -> "Span":
        """Declare a data dependency beyond the parent/child relationship."""
        for s in spans:
            sid = s.id if isinstance(s, Span) else s
            if sid not in self.depends:
                self.depends.append(sid)
        if self.started_at is not None:
            self._emit()
        return self

    def uses(self, *items: Union["Artifact", str], note: Optional[str] = None) -> "Span":
        """Record that this operation consumed an artifact (e.g. retrieved documents)."""
        for a in items:
            link(self, a, relation="used", note=note, run=self._run)
        return self

    def event(self, name: str, *, kind: str = "log", level: str = "info", message: Optional[str] = None, data: Any = None) -> None:
        if self.started_at is None:
            self.start()
        rec: Dict[str, Any] = {
            "type": "event",
            "id": new_id("event"),
            "run_id": self.run_id,
            "span_id": self.id,
            "kind": kind,
            "name": name,
            "level": level,
            "ts": self.tracer.now(),
        }
        if message is not None:
            rec["message"] = self.tracer.capture_cfg.redactor.scrub(message)
        if data is not None:
            rec["data"] = self.tracer.capture(data)
        self.tracer.emit(rec)

    def record_error(self, exc: Union[BaseException, str], *, type: Optional[str] = None) -> "Span":
        if isinstance(exc, BaseException):
            self.error_info = _error_payload(exc, self.tracer)
        else:
            self.error_info = {"type": type or "Error", "message": exc}
        self.status = "error"
        self.event(self.error_info["type"], kind="error", level="error", message=self.error_info["message"])
        return self

    def fail(self, exc: Union[BaseException, str], *, type: Optional[str] = None, at: Optional[float] = None) -> "Span":
        self.record_error(exc, type=type)
        return self.end("error", at=at)

    def record(self) -> Dict[str, Any]:
        rec: Dict[str, Any] = {
            "type": "span",
            "id": self.id,
            "run_id": self.run_id,
            "parent_id": self.parent_id,
            "kind": self.kind,
            "name": self.name,
            "status": self.status,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "seq": self.seq,
            "attributes": self.attributes,
        }
        if self._agent:
            rec["agent"] = self._agent
        if self.model:
            rec["model"] = self.model
        if self.usage_info:
            u = dict(self.usage_info)
            if "input_tokens" in u or "output_tokens" in u:
                u["total_tokens"] = (u.get("input_tokens") or 0) + (u.get("output_tokens") or 0)
            rec["usage"] = u
        if self.depends:
            rec["depends_on"] = self.depends
        if self.retry_of:
            rec["retry_of"] = self.retry_of
        if self.attempt is not None:
            rec["attempt"] = self.attempt
        if self.error_info:
            rec["error"] = self.error_info
        if self._input is not _MISSING:
            rec["input"] = self._input
        if self._output is not _MISSING:
            rec["output"] = self._output
        return rec

    def _emit(self) -> None:
        self.tracer.emit(self.record())


# ---------------------------------------------------------------------------------------
# Artifacts & evidence
# ---------------------------------------------------------------------------------------
ARTIFACT_KINDS = ("answer", "markdown", "text", "code", "diff", "json", "document", "file", "image", "report", "table")


class Artifact:
    """An output worth reviewing: an answer, report, patch, file, or a retrieved document."""

    def __init__(self, rec: Dict[str, Any], content: Any, tracer: Tracer, run: Optional[Run]) -> None:
        self.id: str = rec["id"]
        self.run_id: str = rec["run_id"]
        self.name: str = rec["name"]
        self.kind: str = rec["kind"]
        self.content = content
        self._rec = rec
        self._tracer = tracer
        self._run = run

    def cite(
        self,
        evidence: Union["Artifact", Span, str],
        *,
        anchor: Optional[str] = None,
        quote: Optional[str] = None,
        relation: str = "supported_by",
        note: Optional[str] = None,
        confidence: Optional[float] = None,
    ) -> "Artifact":
        """Link a passage of this artifact (``anchor``) to the evidence behind it.

        ``quote`` locates the supporting passage inside the evidence artifact.
        """
        link(
            self,
            evidence,
            relation=relation,
            subject_anchor=anchor,
            evidence_anchor=quote,
            note=note,
            confidence=confidence,
            run=self._run,
        )
        return self

    def derived_from(self, *sources: Union["Artifact", Span, str], note: Optional[str] = None) -> "Artifact":
        for s in sources:
            link(self, s, relation="derived_from", note=note, run=self._run)
        return self

    def review(self, status: str, *, by: Optional[str] = None, note: Optional[str] = None) -> "Artifact":
        """Record a human review decision: ``approved`` · ``rejected`` · ``needs_changes``."""
        self._rec["review"] = {"status": status, "by": by, "note": note, "at": self._tracer.now()}
        self._tracer.emit(dict(self._rec))
        return self


def artifact(
    name: str,
    content: Any = _MISSING,
    *,
    kind: str = "text",
    mime: Optional[str] = None,
    uri: Optional[str] = None,
    span: Union[Span, str, None, Any] = _MISSING,
    final: bool = False,
    metadata: Optional[Dict[str, Any]] = None,
    derived_from: Iterable[Union["Artifact", Span, str]] = (),
    previous: Union["Artifact", str, None] = None,
    run: Optional[Run] = None,
    id: Optional[str] = None,
) -> Artifact:
    """Record an artifact. By default it is attributed to the current span."""
    run = run or current_run()
    tracer = run.tracer if run else get_tracer()
    if run is None:
        raise RuntimeError("traceglass.artifact() must be called inside a run or span")
    producer = current_span() if span is _MISSING else span
    rec: Dict[str, Any] = {
        "type": "artifact",
        "id": id or new_id("artifact"),
        "run_id": run.id,
        "span_id": producer.id if isinstance(producer, Span) else producer,
        "kind": kind,
        "name": name,
        "created_at": tracer.now(),
        "metadata": tracer.capture(metadata or {}),
    }
    if content is not _MISSING:
        captured = tracer.capture(content)
        rec["content"] = captured
        if isinstance(content, str):
            rec["size"] = len(content)
    if mime:
        rec["mime"] = mime
    if uri:
        rec["uri"] = uri
    if final:
        rec["final"] = True
        run.final_artifact_id = rec["id"]
    if previous is not None:
        rec["previous_id"] = previous.id if isinstance(previous, Artifact) else previous
    tracer.emit(rec)
    art = Artifact(rec, content if content is not _MISSING else None, tracer, run)
    if previous is not None:
        link(art, rec["previous_id"], relation="revises", run=run)
    for src in derived_from:
        link(art, src, relation="derived_from", run=run)
    if final and run.started_at is not None:
        run._emit()
    return art


def _anchor(content: Any, text: Optional[str]) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    if isinstance(content, str):
        i = content.find(text)
        if i >= 0:
            return {"start": i, "end": i + len(text), "text": text}
    return {"text": text}


def link(
    subject: Union[Artifact, Span, str],
    evidence: Union[Artifact, Span, str],
    *,
    relation: str = "derived_from",
    subject_anchor: Optional[str] = None,
    evidence_anchor: Optional[str] = None,
    note: Optional[str] = None,
    confidence: Optional[float] = None,
    declared_by: str = "agent",
    run: Optional[Run] = None,
) -> Dict[str, Any]:
    """Record an evidence/provenance relationship: ``subject`` —relation→ ``evidence``.

    Read it as a sentence: *report* ``supported_by`` *document*; *patch* ``tested_by``
    *test run*; *synthesis step* ``used`` *search results*.
    """
    run = run or current_run()
    if run is None:
        raise RuntimeError("traceglass.link() must be called inside a run")
    tracer = run.tracer
    s_ref, e_ref = _ref(subject), _ref(evidence)
    sa = _anchor(subject.content if isinstance(subject, Artifact) else None, subject_anchor)
    ea = _anchor(evidence.content if isinstance(evidence, Artifact) else None, evidence_anchor)
    if sa:
        s_ref["anchor"] = sa
    if ea:
        e_ref["anchor"] = ea
    rec: Dict[str, Any] = {
        "type": "link",
        "id": new_id("link"),
        "run_id": run.id,
        "subject": s_ref,
        "evidence": e_ref,
        "relation": relation,
        "declared_by": declared_by,
        "created_at": tracer.now(),
    }
    if note:
        rec["note"] = note
    if confidence is not None:
        rec["confidence"] = confidence
    tracer.emit(rec)
    return rec


# ---------------------------------------------------------------------------------------
# Friendly constructors, decorators, context propagation
# ---------------------------------------------------------------------------------------
def run(name: str = "run", **kw: Any) -> Run:
    """``with tg.run("Compare papers", project="research", input=question) as r: ...``"""
    return Run(name, **kw)


def observe(project: Optional[str] = None, name: Optional[str] = None, **kw: Any) -> Run:
    """Wrap any agent invocation in a run. Installs framework hooks that are available.

    ``with tg.observe(project="research-agent"): agent.invoke({...})``
    """
    r = Run(name or project or "run", project=project, **kw)
    try:
        from .integrations import langchain as _lc

        _lc.install_auto()
    except Exception:
        pass
    return r


def span(name: str, kind: str = "step", **kw: Any) -> Span:
    """``with tg.span("search", kind="tool", input=q) as s: s.set_output(hits)``"""
    return Span(name, kind=kind, **kw)


def start_span(name: str, kind: str = "step", **kw: Any) -> Span:
    """Start a span without making it current (for callback-style integrations)."""
    at = kw.pop("at", None)
    return Span(name, kind=kind, **kw).start(at)


class _SpanSpec:
    """Returned by ``tg.tool(...)``, ``tg.agent(...)`` etc. — a decorator *and* a context manager."""

    def __init__(self, name: Optional[str], kind: str, capture_input: bool = True, capture_output: bool = True, **attrs: Any) -> None:
        self.name = name
        self.kind = kind
        self.capture_input = capture_input
        self.capture_output = capture_output
        self.attrs = attrs
        self._stack: List[Span] = []

    def _make(self, name: str, input: Any = _MISSING) -> Span:
        attrs = dict(self.attrs)
        model = attrs.pop("model", None)
        agent = attrs.pop("agent", None)
        return Span(name, kind=self.kind, input=input, model=model, agent=agent, attributes=attrs)

    def __enter__(self) -> Span:
        s = self._make(self.name or self.kind)
        self._stack.append(s)
        return s.__enter__()

    def __exit__(self, *exc: Any) -> None:
        self._stack.pop().__exit__(*exc)

    def __call__(self, fn: F) -> F:
        # "Agent.plan" for methods, "search" for (possibly nested) functions.
        name = self.name or getattr(fn, "__qualname__", getattr(fn, "__name__", "call")).split("<locals>.")[-1]
        sig = None
        try:
            sig = inspect.signature(fn)
        except (TypeError, ValueError):
            pass

        def inputs(args: tuple, kwargs: dict) -> Any:
            if not self.capture_input or not get_tracer().capture_inputs:
                return not_captured("input capture disabled")
            if sig is None:
                return {"args": list(args), "kwargs": kwargs}
            try:
                bound = sig.bind_partial(*args, **kwargs)
            except TypeError:
                return {"args": list(args), "kwargs": kwargs}
            out = dict(bound.arguments)
            for drop in ("self", "cls"):
                out.pop(drop, None)
            return out

        def finish(s: Span, result: Any) -> Any:
            if not self.capture_output or not get_tracer().capture_outputs:
                s.mark_not_captured("output", "output capture disabled")
            elif inspect.isgenerator(result) or inspect.isasyncgen(result):
                s.mark_not_captured("output", "generator output is not captured")
            else:
                s.set_output(result)
            return result

        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def awrapper(*args: Any, **kwargs: Any) -> Any:
                with self._make(name, inputs(args, kwargs)) as s:
                    return finish(s, await fn(*args, **kwargs))

            return awrapper  # type: ignore[return-value]

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            with self._make(name, inputs(args, kwargs)) as s:
                return finish(s, fn(*args, **kwargs))

        return wrapper  # type: ignore[return-value]


def _spec_factory(kind: str) -> Callable[..., Any]:
    def factory(name_or_fn: Any = None, **attrs: Any) -> Any:
        if callable(name_or_fn) and not isinstance(name_or_fn, str):
            return _SpanSpec(None, kind)(name_or_fn)
        return _SpanSpec(name_or_fn, kind, **attrs)

    factory.__name__ = kind
    factory.__doc__ = f"Decorate a function (or open a block) as a ``{kind}`` span: ``@tg.{kind}`` / ``with tg.{kind}('name'):``"
    return factory


agent = _spec_factory("agent")
tool = _spec_factory("tool")
llm = _spec_factory("llm")
retrieval = _spec_factory("retrieval")
step = _spec_factory("step")
guardrail = _spec_factory("guardrail")


def trace(name_or_fn: Any = None, kind: str = "step", **attrs: Any) -> Any:
    """Generic decorator: ``@tg.trace`` or ``@tg.trace(kind="tool", name="search")``."""
    if callable(name_or_fn) and not isinstance(name_or_fn, str):
        return _SpanSpec(None, kind)(name_or_fn)
    return _SpanSpec(name_or_fn or attrs.pop("name", None), kind, **attrs)


def bind(fn: F) -> F:
    """Carry the current run/span into another thread: ``Thread(target=tg.bind(work))``."""
    ctx = contextvars.copy_context()

    @functools.wraps(fn)
    def wrapper(*a: Any, **k: Any) -> Any:
        return ctx.copy().run(fn, *a, **k)

    return wrapper  # type: ignore[return-value]


class ThreadPoolExecutor(concurrent.futures.ThreadPoolExecutor):
    """A ThreadPoolExecutor whose tasks inherit the submitting thread's run and span."""

    def submit(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> "concurrent.futures.Future[Any]":  # type: ignore[override]
        ctx = contextvars.copy_context()
        return super().submit(ctx.run, fn, *args, **kwargs)

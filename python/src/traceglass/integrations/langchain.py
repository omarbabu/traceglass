"""LangChain / LangGraph integration.

Explicit:

    from traceglass.integrations.langchain import TraceglassCallbackHandler
    graph.invoke(inputs, config={"callbacks": [TraceglassCallbackHandler()]})

Automatic — inside ``tg.observe()`` every runnable reports without passing callbacks:

    with tg.observe(project="research-agent"):
        graph.invoke(inputs)

LangChain's internal plumbing runs (tagged ``langsmith:hidden``, e.g. LangGraph channel
writes) are folded into their parents so the graph shows nodes, models, tools and
retrievers rather than framework internals.
"""

from __future__ import annotations

import contextvars
import threading
from typing import Any, Dict, List, Optional, Sequence
from uuid import UUID

from ..recorder import _MISSING, Span, artifact, current_run

try:  # pragma: no cover - exercised implicitly by the tests when installed
    from langchain_core.callbacks.base import BaseCallbackHandler as _Base

    _HAVE_LC = True
except Exception:  # pragma: no cover
    _Base = object  # type: ignore[assignment,misc]
    _HAVE_LC = False


def _require() -> None:
    if not _HAVE_LC:
        raise ImportError(
            "TraceglassCallbackHandler needs langchain-core: pip install 'traceglass[langchain]'"
        )


# -- value conversion ---------------------------------------------------------------------
def _msg(m: Any) -> Dict[str, Any]:
    out: Dict[str, Any] = {"role": getattr(m, "type", "message"), "content": getattr(m, "content", None)}
    for attr in ("name", "tool_call_id"):
        v = getattr(m, attr, None)
        if v:
            out[attr] = v
    tcs = getattr(m, "tool_calls", None)
    if tcs:
        out["tool_calls"] = [dict(tc) if isinstance(tc, dict) else tc for tc in tcs]
    return out


def _lc(v: Any, depth: int = 0) -> Any:
    if depth > 8:
        return v
    if callable(getattr(v, "to_messages", None)) and type(v).__name__.endswith("PromptValue"):
        return {"messages": [_msg(m) for m in v.to_messages()]}
    if hasattr(v, "page_content") and hasattr(v, "metadata"):
        return {"page_content": v.page_content, "metadata": v.metadata}
    if hasattr(v, "content") and hasattr(v, "type") and type(v).__name__.endswith(("Message", "MessageChunk")):
        return _msg(v)
    if isinstance(v, dict):
        return {str(k): _lc(x, depth + 1) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_lc(x, depth + 1) for x in v]
    return v


def _name(serialized: Optional[Dict[str, Any]], kwargs: Dict[str, Any], fallback: str) -> str:
    if kwargs.get("name"):
        return str(kwargs["name"])
    if serialized:
        if serialized.get("name"):
            return str(serialized["name"])
        ident = serialized.get("id")
        if isinstance(ident, list) and ident:
            return str(ident[-1])
    return fallback


def _model(serialized: Optional[Dict[str, Any]], kwargs: Dict[str, Any]) -> Optional[str]:
    params = kwargs.get("invocation_params") or {}
    md = kwargs.get("metadata") or {}
    for v in (params.get("model"), params.get("model_name"), params.get("model_id"), md.get("ls_model_name")):
        if v:
            return str(v)
    sk = (serialized or {}).get("kwargs") or {}
    for k in ("model", "model_name", "model_id"):
        if sk.get(k):
            return str(sk[k])
    return None


class TraceglassCallbackHandler(_Base):  # type: ignore[misc,valid-type]
    """Maps LangChain callbacks onto Traceglass spans.

    ``require_run=True`` (used by the automatic hook) records only while a Traceglass run
    is active; otherwise a root runnable without a run starts an implicit run.
    """

    run_inline = True
    raise_error = False

    def __init__(self, *, require_run: bool = False) -> None:
        _require()
        super().__init__()
        self.require_run = require_run
        self._spans: Dict[UUID, Span] = {}
        self._alias: Dict[UUID, Optional[UUID]] = {}  # hidden runs → their (resolved) parent
        self._lock = threading.Lock()

    # -- core -----------------------------------------------------------------------------
    def _parent(self, parent_run_id: Optional[UUID]) -> Optional[Span]:
        seen = 0
        while parent_run_id is not None and seen < 64:
            if parent_run_id in self._spans:
                return self._spans[parent_run_id]
            if parent_run_id not in self._alias:
                return None
            parent_run_id = self._alias[parent_run_id]
            seen += 1
        return None

    def _start(
        self,
        run_id: UUID,
        parent_run_id: Optional[UUID],
        name: str,
        kind: str,
        input: Any,
        *,
        tags: Optional[Sequence[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        model: Optional[str] = None,
        attributes: Optional[Dict[str, Any]] = None,
    ) -> Optional[Span]:
        with self._lock:
            parent = self._parent(parent_run_id)
            if tags and "langsmith:hidden" in tags and kind == "step":
                self._alias[run_id] = parent_run_id
                return None
            if parent is None and self.require_run and current_run() is None:
                return None
            attrs = dict(attributes or {})
            attrs["framework"] = "langchain"
            if tags:
                attrs["tags"] = list(tags)
            md = {k: v for k, v in (metadata or {}).items() if not str(k).startswith(("ls_", "checkpoint"))}
            if md:
                attrs["metadata"] = md
            span = Span(
                name,
                kind=kind,
                input=_lc(input),
                parent=parent if parent is not None else _MISSING,
                run=parent._run if parent is not None else None,
                model=model,
                attributes=attrs,
            )
            span.start()
            self._spans[run_id] = span
            return span

    def _end(self, run_id: UUID, output: Any = _MISSING, **attrs: Any) -> Optional[Span]:
        with self._lock:
            span = self._spans.pop(run_id, None)
            self._alias.pop(run_id, None)
        if span is None:
            return None
        if attrs:
            span.set_attributes(**attrs)
        span.end(output=_lc(output) if output is not _MISSING else _MISSING)
        return span

    def _error(self, run_id: UUID, error: BaseException) -> None:
        with self._lock:
            span = self._spans.pop(run_id, None)
            self._alias.pop(run_id, None)
        if span is not None:
            span.fail(error)

    # -- chains ---------------------------------------------------------------------------
    def on_chain_start(self, serialized: Dict[str, Any], inputs: Any, *, run_id: UUID, parent_run_id: Optional[UUID] = None, tags: Optional[List[str]] = None, metadata: Optional[Dict[str, Any]] = None, **kwargs: Any) -> None:
        md = metadata or {}
        name = _name(serialized, kwargs, str(md.get("langgraph_node") or "chain"))
        is_graph = name == "LangGraph" or (parent_run_id is None and "langgraph" in str((serialized or {}).get("id", "")).lower())
        # A LangGraph *node* named "agent" is a step of the graph; the graph itself is the agent.
        kind = "agent" if is_graph or ("agent" in name.lower() and not md.get("langgraph_node")) else "step"
        self._start(run_id, parent_run_id, name, kind, inputs, tags=tags, metadata=metadata)

    def on_chain_end(self, outputs: Any, *, run_id: UUID, **kwargs: Any) -> None:
        self._end(run_id, outputs)

    def on_chain_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        self._error(run_id, error)

    # -- models ---------------------------------------------------------------------------
    def on_llm_start(self, serialized: Dict[str, Any], prompts: List[str], *, run_id: UUID, parent_run_id: Optional[UUID] = None, tags: Optional[List[str]] = None, metadata: Optional[Dict[str, Any]] = None, **kwargs: Any) -> None:
        kw = {**kwargs, "metadata": metadata}
        self._start(run_id, parent_run_id, _name(serialized, kwargs, "llm"), "llm", {"prompts": prompts}, tags=tags, metadata=metadata, model=_model(serialized, kw), attributes=_params(kwargs))

    def on_chat_model_start(self, serialized: Dict[str, Any], messages: List[List[Any]], *, run_id: UUID, parent_run_id: Optional[UUID] = None, tags: Optional[List[str]] = None, metadata: Optional[Dict[str, Any]] = None, **kwargs: Any) -> None:
        kw = {**kwargs, "metadata": metadata}
        flat = [_msg(m) for batch in messages for m in batch]
        self._start(run_id, parent_run_id, _name(serialized, kwargs, "chat_model"), "llm", {"messages": flat}, tags=tags, metadata=metadata, model=_model(serialized, kw), attributes=_params(kwargs))

    def on_llm_end(self, response: Any, *, run_id: UUID, **kwargs: Any) -> None:
        with self._lock:
            span = self._spans.get(run_id)
        if span is None:
            return
        gens = [g for batch in (getattr(response, "generations", None) or []) for g in batch]
        outputs = []
        usage: Dict[str, int] = {}
        for g in gens:
            m = getattr(g, "message", None)
            outputs.append(_msg(m) if m is not None else getattr(g, "text", None))
            um = getattr(m, "usage_metadata", None) if m is not None else None
            if um:
                usage["input_tokens"] = usage.get("input_tokens", 0) + int(um.get("input_tokens") or 0)
                usage["output_tokens"] = usage.get("output_tokens", 0) + int(um.get("output_tokens") or 0)
        llm_output = getattr(response, "llm_output", None) or {}
        tu = llm_output.get("token_usage") or llm_output.get("usage") or {}
        if not usage and isinstance(tu, dict) and tu:
            usage = {
                "input_tokens": int(tu.get("prompt_tokens") or tu.get("input_tokens") or 0),
                "output_tokens": int(tu.get("completion_tokens") or tu.get("output_tokens") or 0),
            }
        if usage:
            span.usage(**usage)
        model = llm_output.get("model_name") or llm_output.get("model")
        if model and not span.model:
            span.set_model(str(model))
        self._end(run_id, outputs[0] if len(outputs) == 1 else outputs)

    def on_llm_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        self._error(run_id, error)

    # -- tools ----------------------------------------------------------------------------
    def on_tool_start(self, serialized: Dict[str, Any], input_str: str, *, run_id: UUID, parent_run_id: Optional[UUID] = None, tags: Optional[List[str]] = None, metadata: Optional[Dict[str, Any]] = None, inputs: Optional[Dict[str, Any]] = None, **kwargs: Any) -> None:
        name = _name(serialized, kwargs, "tool")
        self._start(run_id, parent_run_id, name, "tool", inputs if inputs is not None else input_str, tags=tags, metadata=metadata)

    def on_tool_end(self, output: Any, *, run_id: UUID, **kwargs: Any) -> None:
        if hasattr(output, "content") and hasattr(output, "tool_call_id"):
            status = getattr(output, "status", None)
            span = self._end(run_id, output.content)
            if span is not None and status == "error":
                span.status = "error"
                span._emit()
            return
        self._end(run_id, output)

    def on_tool_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        self._error(run_id, error)

    # -- retrievers -----------------------------------------------------------------------
    def on_retriever_start(self, serialized: Dict[str, Any], query: str, *, run_id: UUID, parent_run_id: Optional[UUID] = None, tags: Optional[List[str]] = None, metadata: Optional[Dict[str, Any]] = None, **kwargs: Any) -> None:
        self._start(run_id, parent_run_id, _name(serialized, kwargs, "retriever"), "retrieval", {"query": query}, tags=tags, metadata=metadata)

    def on_retriever_end(self, documents: Sequence[Any], *, run_id: UUID, **kwargs: Any) -> None:
        with self._lock:
            span = self._spans.get(run_id)
        if span is None:
            return
        for i, doc in enumerate(documents or []):
            md = dict(getattr(doc, "metadata", {}) or {})
            title = md.get("title") or md.get("source") or getattr(doc, "id", None) or f"document {i + 1}"
            artifact(str(title), getattr(doc, "page_content", str(doc)), kind="document", span=span, run=span._run, metadata=md)
        self._end(run_id, {"documents": len(documents or [])})

    def on_retriever_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        self._error(run_id, error)

    # -- agent events ---------------------------------------------------------------------
    def _event(self, run_id: UUID, name: str, data: Any, kind: str = "log") -> None:
        with self._lock:
            span = self._spans.get(run_id) or self._parent(self._alias.get(run_id))
        if span is not None:
            span.event(name, kind=kind, data=_lc(data))

    def on_agent_action(self, action: Any, *, run_id: UUID, **kwargs: Any) -> None:
        self._event(run_id, "agent_action", {"tool": getattr(action, "tool", None), "tool_input": getattr(action, "tool_input", None), "log": getattr(action, "log", None)})

    def on_agent_finish(self, finish: Any, *, run_id: UUID, **kwargs: Any) -> None:
        self._event(run_id, "agent_finish", {"return_values": getattr(finish, "return_values", None), "log": getattr(finish, "log", None)})

    def on_retry(self, retry_state: Any, *, run_id: UUID, **kwargs: Any) -> None:
        attempt = getattr(retry_state, "attempt_number", None)
        outcome = getattr(retry_state, "outcome", None)
        err = None
        if outcome is not None and getattr(outcome, "failed", False):
            try:
                err = repr(outcome.exception())
            except Exception:
                err = None
        self._event(run_id, "retry", {"attempt": attempt, "error": err}, kind="retry")


def _params(kwargs: Dict[str, Any]) -> Dict[str, Any]:
    params = kwargs.get("invocation_params") or {}
    keep = {k: params[k] for k in ("temperature", "max_tokens", "stop", "_type") if k in params}
    return {"invocation_params": keep} if keep else {}


# -- automatic installation -----------------------------------------------------------------
_auto_var: contextvars.ContextVar[Optional[Any]] = contextvars.ContextVar("traceglass_langchain_handler", default=None)
_auto_handler: Optional[TraceglassCallbackHandler] = None
_installed = False
_install_lock = threading.Lock()


def install_auto() -> bool:
    """Make every LangChain runnable report to Traceglass while a Traceglass run is active.

    Idempotent. Returns False when langchain-core is not installed.
    """
    global _installed, _auto_handler
    if not _HAVE_LC:
        return False
    with _install_lock:
        if not _installed:
            from langchain_core.tracers.context import register_configure_hook

            _auto_handler = TraceglassCallbackHandler(require_run=True)
            register_configure_hook(_auto_var, inheritable=True, handle_class=TraceglassCallbackHandler)
            _installed = True
    _auto_var.set(_auto_handler)
    return True

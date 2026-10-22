"""OpenTelemetry (OTLP/HTTP JSON) ingestion.

Point any OpenTelemetry exporter at ``http://127.0.0.1:7357/v1/traces`` (JSON encoding).
Spans following the OpenTelemetry GenAI semantic conventions (``gen_ai.*``) or
OpenInference (``openinference.span.kind``, ``llm.*``, ``retrieval.documents.*``) are mapped
onto Traceglass kinds, models, token usage, inputs/outputs and retrieved-document artifacts.
Anything unrecognised is preserved under ``attributes`` — nothing is silently dropped.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

_OI_KINDS = {
    "LLM": "llm",
    "CHAIN": "step",
    "TOOL": "tool",
    "RETRIEVER": "retrieval",
    "RERANKER": "retrieval",
    "EMBEDDING": "embedding",
    "AGENT": "agent",
    "GUARDRAIL": "guardrail",
    "EVALUATOR": "guardrail",
}

_GENAI_OPS = {
    "chat": "llm",
    "text_completion": "llm",
    "generate_content": "llm",
    "embeddings": "embedding",
    "execute_tool": "tool",
    "invoke_agent": "agent",
    "create_agent": "agent",
    "invoke_workflow": "step",
}


def _value(v: Dict[str, Any]) -> Any:
    if not isinstance(v, dict):
        return v
    for key in ("stringValue", "boolValue", "doubleValue"):
        if key in v:
            return v[key]
    if "intValue" in v:
        try:
            return int(v["intValue"])
        except (TypeError, ValueError):
            return v["intValue"]
    if "arrayValue" in v:
        return [_value(x) for x in (v["arrayValue"].get("values") or [])]
    if "kvlistValue" in v:
        return {kv["key"]: _value(kv.get("value", {})) for kv in (v["kvlistValue"].get("values") or [])}
    if "bytesValue" in v:
        return {"$tg": "bytes", "base64": v["bytesValue"][:200]}
    return None


def _attrs(items: Optional[List[Dict[str, Any]]]) -> Dict[str, Any]:
    return {a["key"]: _value(a.get("value", {})) for a in (items or []) if "key" in a}


def _maybe_json(v: Any) -> Any:
    if isinstance(v, str) and v[:1] in "[{":
        try:
            return json.loads(v)
        except ValueError:
            return v
    return v


def _ms(nanos: Any) -> Optional[float]:
    try:
        return int(nanos) / 1e6 if nanos not in (None, "", "0", 0) else None
    except (TypeError, ValueError):
        return None


def _int(v: Any) -> Optional[int]:
    try:
        return int(v) if v is not None and not isinstance(v, bool) else None
    except (TypeError, ValueError):
        return None


def _hex(v: Optional[str]) -> Optional[str]:
    if not v:
        return None
    # OTLP/JSON uses hex ids; some exporters send base64. Normalise to lowercase hex-ish text.
    return v.lower() if re.fullmatch(r"[0-9a-fA-F]+", v) else v


def _first(attrs: Dict[str, Any], *keys: str) -> Any:
    for k in keys:
        if attrs.get(k) not in (None, ""):
            return attrs[k]
    return None


def _kind(name: str, attrs: Dict[str, Any]) -> str:
    oi = attrs.get("openinference.span.kind")
    if isinstance(oi, str) and oi.upper() in _OI_KINDS:
        return _OI_KINDS[oi.upper()]
    op = attrs.get("gen_ai.operation.name")
    if isinstance(op, str) and op in _GENAI_OPS:
        return _GENAI_OPS[op]
    if any(k.startswith("gen_ai.tool.") for k in attrs):
        return "tool"
    if any(k.startswith("gen_ai.") for k in attrs) or "llm.model_name" in attrs:
        return "llm"
    return "step"


def _indexed(attrs: Dict[str, Any], prefix: str) -> List[Dict[str, Any]]:
    """Collect flattened OpenInference lists like ``retrieval.documents.0.document.content``."""
    out: Dict[int, Dict[str, Any]] = {}
    pat = re.compile(re.escape(prefix) + r"\.(\d+)\.(.+)")
    for k, v in attrs.items():
        m = pat.fullmatch(k)
        if m:
            out.setdefault(int(m.group(1)), {})[m.group(2)] = v
    return [out[i] for i in sorted(out)]


def otlp_to_records(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    runs: Dict[str, Dict[str, Any]] = {}
    for rs in payload.get("resourceSpans", []) or []:
        res = _attrs((rs.get("resource") or {}).get("attributes"))
        project = _first(res, "openinference.project.name", "service.name") or "otel"
        for ss in rs.get("scopeSpans", []) or rs.get("instrumentationLibrarySpans", []) or []:
            scope = (ss.get("scope") or ss.get("instrumentationLibrary") or {}).get("name")
            for sp in ss.get("spans", []) or []:
                rec, docs = _span(sp, project, scope)
                records.append(rec)
                records.extend(docs)
                run = runs.setdefault(
                    rec["run_id"],
                    {"type": "run", "id": rec["run_id"], "project": project, "source": "otlp", "metadata": {"resource": res}},
                )
                if not rec.get("parent_id"):
                    run["name"] = rec["name"]
                    run["status"] = "error" if rec["status"] == "error" else ("running" if rec["ended_at"] is None else "ok")
                    if "input" in rec:
                        run["input"] = rec["input"]
                    if "output" in rec:
                        run["output"] = rec["output"]
                    sess = rec["attributes"].get("session.id") or rec["attributes"].get("gen_ai.conversation.id")
                    if sess:
                        run["session"] = sess
                for k, pick in (("started_at", min), ("ended_at", max)):
                    if rec.get(k) is not None:
                        run[k] = pick(run[k], rec[k]) if run.get(k) is not None else rec[k]
    for run in runs.values():
        run.setdefault("name", f"trace {run['id'][:12]}")
    return list(runs.values()) + records


def _span(sp: Dict[str, Any], project: str, scope: Optional[str]) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    attrs = _attrs(sp.get("attributes"))
    trace_id = _hex(sp.get("traceId")) or "unknown"
    span_id = _hex(sp.get("spanId")) or "unknown"
    run_id = f"otel_{trace_id}"
    name = sp.get("name") or "span"
    kind = _kind(name, attrs)
    code = (sp.get("status") or {}).get("code")
    status = "error" if code in (2, "STATUS_CODE_ERROR", "ERROR") else "ok"
    end = _ms(sp.get("endTimeUnixNano"))
    rec: Dict[str, Any] = {
        "type": "span",
        "id": f"otel_{span_id}",
        "run_id": run_id,
        "parent_id": f"otel_{_hex(sp['parentSpanId'])}" if sp.get("parentSpanId") else None,
        "kind": kind,
        "name": _first(attrs, "gen_ai.tool.name", "tool.name") if kind == "tool" and _first(attrs, "gen_ai.tool.name", "tool.name") else name,
        "status": status if end is not None else "running",
        "started_at": _ms(sp.get("startTimeUnixNano")),
        "ended_at": end,
        "attributes": {k: v for k, v in attrs.items()},
        "otel": {"trace_id": trace_id, "span_id": span_id, "scope": scope, "otel_kind": sp.get("kind")},
    }
    model = _first(attrs, "gen_ai.response.model", "gen_ai.request.model", "llm.model_name")
    if model:
        rec["model"] = model
    agent = _first(attrs, "gen_ai.agent.name", "agent.name")
    if agent:
        rec["agent"] = agent
    usage = {
        "input_tokens": _first(attrs, "gen_ai.usage.input_tokens", "gen_ai.usage.prompt_tokens", "llm.token_count.prompt"),
        "output_tokens": _first(attrs, "gen_ai.usage.output_tokens", "gen_ai.usage.completion_tokens", "llm.token_count.completion"),
    }
    usage = {k: n for k, n in ((k, _int(v)) for k, v in usage.items()) if n is not None}
    if usage:
        usage["total_tokens"] = sum(usage.values())
        rec["usage"] = usage
    inp = _first(attrs, "input.value", "gen_ai.input.messages", "gen_ai.prompt", "gen_ai.tool.call.arguments")
    if inp is None:
        msgs = _indexed(attrs, "llm.input_messages")
        inp = {"messages": msgs} if msgs else None
    if inp is not None:
        rec["input"] = _maybe_json(inp)
    out = _first(attrs, "output.value", "gen_ai.output.messages", "gen_ai.completion", "gen_ai.tool.call.result")
    if out is None:
        msgs = _indexed(attrs, "llm.output_messages")
        out = {"messages": msgs} if msgs else None
    if out is not None:
        rec["output"] = _maybe_json(out)
    if status == "error":
        msg = (sp.get("status") or {}).get("message")
        exc = next((e for e in sp.get("events", []) or [] if e.get("name") == "exception"), None)
        ea = _attrs(exc.get("attributes")) if exc else {}
        rec["error"] = {
            "type": ea.get("exception.type") or "Error",
            "message": ea.get("exception.message") or msg or "span status ERROR",
            "stack": ea.get("exception.stacktrace"),
        }
    extras: List[Dict[str, Any]] = []
    for i, ev in enumerate(sp.get("events", []) or []):
        ea = _attrs(ev.get("attributes"))
        extras.append(
            {
                "type": "event",
                "id": f"otel_{span_id}_e{i}",
                "run_id": run_id,
                "span_id": rec["id"],
                "kind": "error" if ev.get("name") == "exception" else "log",
                "name": ev.get("name") or "event",
                "level": "error" if ev.get("name") == "exception" else "info",
                "ts": _ms(ev.get("timeUnixNano")),
                "data": ea or None,
            }
        )
    for i, doc in enumerate(_indexed(attrs, "retrieval.documents")):
        content = doc.get("document.content")
        if content is None:
            continue
        extras.append(
            {
                "type": "artifact",
                "id": f"otel_{span_id}_d{i}",
                "run_id": run_id,
                "span_id": rec["id"],
                "kind": "document",
                "name": str(doc.get("document.id") or f"document {i + 1}"),
                "content": content,
                "created_at": rec["ended_at"] or rec["started_at"],
                "metadata": {k: v for k, v in doc.items() if k != "document.content"},
            }
        )
    return rec, extras

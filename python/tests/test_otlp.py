from traceglass.otlp import otlp_to_records
from traceglass.store import Store


def kv(k, v):
    if isinstance(v, bool):
        return {"key": k, "value": {"boolValue": v}}
    if isinstance(v, int):
        return {"key": k, "value": {"intValue": str(v)}}
    if isinstance(v, float):
        return {"key": k, "value": {"doubleValue": v}}
    return {"key": k, "value": {"stringValue": v}}


def payload(spans, service="svc"):
    return {"resourceSpans": [{"resource": {"attributes": [kv("service.name", service)]}, "scopeSpans": [{"scope": {"name": "lib"}, "spans": spans}]}]}


T = "0af7651916cd43dd8448eb211c80319c"


def genai_payload():
    return payload([
        {"traceId": T, "spanId": "a1", "name": "invoke_agent researcher", "startTimeUnixNano": "1000000000", "endTimeUnixNano": "9000000000",
         "attributes": [kv("gen_ai.operation.name", "invoke_agent"), kv("gen_ai.agent.name", "researcher"), kv("session.id", "sess-9")], "status": {"code": 1}},
        {"traceId": T, "spanId": "b2", "parentSpanId": "a1", "name": "chat claude", "startTimeUnixNano": "2000000000", "endTimeUnixNano": "3000000000",
         "attributes": [kv("gen_ai.operation.name", "chat"), kv("gen_ai.request.model", "claude-opus-5-5"), kv("gen_ai.usage.input_tokens", 120), kv("gen_ai.usage.output_tokens", 30),
                        kv("gen_ai.input.messages", '[{"role":"user","content":"hi"}]')]},
        {"traceId": T, "spanId": "c3", "parentSpanId": "a1", "name": "execute_tool search", "startTimeUnixNano": "4000000000", "endTimeUnixNano": "5000000000",
         "attributes": [kv("gen_ai.operation.name", "execute_tool"), kv("gen_ai.tool.name", "web_search")],
         "status": {"code": 2, "message": "timeout"},
         "events": [{"name": "exception", "timeUnixNano": "4500000000", "attributes": [kv("exception.type", "TimeoutError"), kv("exception.message", "took too long")]}]},
    ])


def test_genai_mapping():
    recs = otlp_to_records(genai_payload())
    run = next(r for r in recs if r["type"] == "run")
    spans = {r["id"]: r for r in recs if r["type"] == "span"}
    assert run["id"] == f"otel_{T}" and run["project"] == "svc" and run["name"] == "invoke_agent researcher"
    assert run["session"] == "sess-9" and run["status"] == "ok" and run["started_at"] == 1000 and run["ended_at"] == 9000
    agent, llm, tool = spans["otel_a1"], spans["otel_b2"], spans["otel_c3"]
    assert agent["kind"] == "agent" and agent["agent"] == "researcher" and agent["parent_id"] is None
    assert llm["kind"] == "llm" and llm["model"] == "claude-opus-5-5" and llm["parent_id"] == "otel_a1"
    assert llm["usage"] == {"input_tokens": 120, "output_tokens": 30, "total_tokens": 150}
    assert llm["input"] == [{"role": "user", "content": "hi"}]
    assert tool["kind"] == "tool" and tool["name"] == "web_search" and tool["status"] == "error"
    assert tool["error"] == {"type": "TimeoutError", "message": "took too long", "stack": None}
    ev = next(r for r in recs if r["type"] == "event")
    assert ev["span_id"] == "otel_c3" and ev["kind"] == "error" and ev["ts"] == 4500


def test_openinference_mapping_with_documents():
    recs = otlp_to_records(payload([
        {"traceId": T, "spanId": "r1", "name": "retrieve", "startTimeUnixNano": "1", "endTimeUnixNano": "2000000",
         "attributes": [kv("openinference.span.kind", "RETRIEVER"), kv("input.value", "what is x"),
                        kv("retrieval.documents.0.document.id", "doc-a"), kv("retrieval.documents.0.document.content", "X is a letter."),
                        kv("retrieval.documents.0.document.score", 0.9), kv("retrieval.documents.1.document.content", "Second doc.")]},
        {"traceId": T, "spanId": "l1", "parentSpanId": "r1", "name": "llm", "startTimeUnixNano": "1", "endTimeUnixNano": "2",
         "attributes": [kv("openinference.span.kind", "LLM"), kv("llm.model_name", "m"), kv("llm.token_count.prompt", 5), kv("llm.token_count.completion", 7),
                        kv("llm.input_messages.0.message.role", "user"), kv("llm.input_messages.0.message.content", "hi"), kv("output.value", '{"a": 1}')]},
    ], service="ignored") | {})
    spans = {r["id"]: r for r in recs if r["type"] == "span"}
    docs = [r for r in recs if r["type"] == "artifact"]
    assert spans["otel_r1"]["kind"] == "retrieval" and spans["otel_r1"]["input"] == "what is x"
    assert [d["name"] for d in docs] == ["doc-a", "document 2"]
    assert docs[0]["content"] == "X is a letter." and docs[0]["span_id"] == "otel_r1" and docs[0]["metadata"]["document.score"] == 0.9
    llm = spans["otel_l1"]
    assert llm["kind"] == "llm" and llm["model"] == "m" and llm["usage"]["total_tokens"] == 12
    assert llm["input"] == {"messages": [{"message.role": "user", "message.content": "hi"}]} and llm["output"] == {"a": 1}


def test_project_from_openinference_resource_and_unfinished_span():
    p = {"resourceSpans": [{"resource": {"attributes": [kv("openinference.project.name", "proj"), kv("service.name", "svc")]},
                            "scopeSpans": [{"spans": [{"traceId": T, "spanId": "x", "name": "root", "startTimeUnixNano": "1000000"}]}]}]}
    recs = otlp_to_records(p)
    run = recs[0]
    assert run["project"] == "proj" and run["status"] == "running"
    assert recs[1]["status"] == "running" and recs[1]["kind"] == "step"


def test_run_assembled_across_batches(tmp_path):
    st = Store(tmp_path / "db.sqlite")
    p = genai_payload()
    spans = p["resourceSpans"][0]["scopeSpans"][0]["spans"]
    child_only = payload([s for s in spans if s.get("parentSpanId")])
    root_only = payload([s for s in spans if not s.get("parentSpanId")])
    st.write(otlp_to_records(child_only))
    st.write(otlp_to_records(root_only))
    b = st.get_bundle(f"otel_{T}")
    assert len(b["spans"]) == 3 and b["run"]["name"] == "invoke_agent researcher"
    assert b["run"]["started_at"] == 1000 and b["run"]["ended_at"] == 9000
    assert st.list_runs()[0]["summary"]["tokens"] == 150


def test_empty_payload():
    assert otlp_to_records({}) == []

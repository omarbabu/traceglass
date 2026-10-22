import asyncio
import threading

import pytest

import traceglass as tg
from traceglass.capture import CaptureConfig, Redactor, capture


def spans(sink):
    return {s["name"]: s for s in sink.of_type("span")}


def test_run_and_nested_spans(mem):
    with tg.run("task", input={"q": "hi"}, session="s1", tags=["x"]) as r:
        with tg.span("outer", kind="agent") as outer:
            with tg.span("inner", kind="tool", input={"a": 1}) as inner:
                inner.set_output([1, 2])
    run = mem.of_type("run")[0]
    assert run["id"] == r.id and run["status"] == "ok" and run["session"] == "s1"
    assert run["input"] == {"q": "hi"} and run["project"] == "test"
    assert run["ended_at"] >= run["started_at"]
    s = spans(mem)
    assert s["outer"]["run_id"] == r.id and s["outer"].get("parent_id") is None
    assert s["inner"]["parent_id"] == outer.id
    assert s["inner"]["agent"] == "outer"  # agent name propagates to children
    assert s["inner"]["output"] == [1, 2] and s["inner"]["status"] == "ok"
    assert s["inner"]["seq"] > s["outer"]["seq"]


def test_span_emitted_running_then_final(mem):
    with tg.run("r"):
        with tg.span("work"):
            pass
    states = [x["status"] for x in mem.records if x["type"] == "span"]
    assert states == ["running", "ok"]


def test_decorators_capture_args_and_return(mem):
    @tg.tool
    def search(query, k=2):
        return [query] * k

    class Agent:
        @tg.agent("planner")
        def plan(self, goal):
            return search(goal, k=1)

    with tg.run("r"):
        Agent().plan("ship")
    s = spans(mem)
    assert s["planner"]["kind"] == "agent" and s["planner"]["input"] == {"goal": "ship"}
    assert s["search"]["input"] == {"query": "ship", "k": 1}
    assert s["search"]["output"] == ["ship"]
    assert s["search"]["parent_id"] == s["planner"]["id"]


def test_async_decorator_and_gather_parenting(mem):
    @tg.tool
    async def fetch(i):
        await asyncio.sleep(0.001)
        return i * 2

    @tg.agent
    async def orchestrate():
        return await asyncio.gather(fetch(1), fetch(2))

    async def main():
        with tg.run("async"):
            return await orchestrate()

    assert asyncio.run(main()) == [2, 4]
    recs = mem.of_type("span")
    orch = next(r for r in recs if r["kind"] == "agent")
    fetches = [r for r in recs if r["name"].endswith("fetch")]
    assert len(fetches) == 2 and all(f["parent_id"] == orch["id"] for f in fetches)


def test_thread_pool_inherits_context(mem):
    with tg.run("threads"):
        with tg.span("parent") as p:
            with tg.ThreadPoolExecutor(4) as ex:
                list(ex.map(lambda i: tg.span(f"child{i}").__enter__().end(), range(3)))
            t = threading.Thread(target=tg.bind(lambda: tg.span("bound").start().end()))
            t.start()
            t.join()
    s = spans(mem)
    assert all(s[f"child{i}"]["parent_id"] == p.id for i in range(3))
    assert s["bound"]["parent_id"] == p.id


def test_implicit_run_for_orphan_span(mem):
    with tg.span("standalone", kind="tool", input="x") as sp:
        sp.set_output("y")
    runs = mem.of_type("run")
    assert len(runs) == 1 and runs[0]["name"] == "standalone"
    assert runs[0]["status"] == "ok" and runs[0]["output"] == "y"
    assert spans(mem)["standalone"]["run_id"] == runs[0]["id"]


def test_errors_mark_span_and_run(mem):
    with pytest.raises(ValueError):
        with tg.run("boom"):
            with tg.span("bad"):
                raise ValueError("nope")
    s = spans(mem)["bad"]
    assert s["status"] == "error" and s["error"]["type"] == "ValueError" and "nope" in s["error"]["message"]
    assert "Traceback" in s["error"]["stack"]
    assert mem.of_type("run")[0]["status"] == "error"
    assert any(e["kind"] == "error" for e in mem.of_type("event"))


def test_retries_and_dependencies(mem):
    with tg.run("r"):
        a = tg.span("search", kind="tool", attempt=1).start()
        a.fail("rate limited", type="RateLimit")
        b = tg.span("search", kind="tool", attempt=2, retry_of=a).start().end()
        c = tg.span("synth", kind="llm").start()
        c.depends_on(b)
        c.usage(10, 5, cost_usd=0.01)
        c.end()
    recs = {r["id"]: r for r in mem.of_type("span")}
    assert recs[a.id]["status"] == "error" and recs[a.id]["error"]["type"] == "RateLimit"
    assert recs[b.id]["retry_of"] == a.id and recs[b.id]["attempt"] == 2
    assert recs[c.id]["depends_on"] == [b.id]
    assert recs[c.id]["usage"] == {"input_tokens": 10, "output_tokens": 5, "cost_usd": 0.01, "total_tokens": 15}


def test_artifacts_links_and_anchors(mem):
    with tg.run("r") as r:
        with tg.retrieval("fetch") as ret:
            doc = tg.artifact("Paper", "Alpha beats beta by 3 points.", kind="document", uri="https://x/paper")
        with tg.llm("write", model="m") as w:
            w.uses(doc)
            rep = tg.artifact("Report", "# R\nAlpha wins.", kind="markdown", final=True, derived_from=[w])
            rep.cite(doc, anchor="Alpha wins.", quote="beats beta", confidence=0.9)
            rep.review("approved", by="alice")
    arts = {a["name"]: a for a in mem.of_type("artifact")}
    assert arts["Paper"]["span_id"] == ret.id and arts["Report"]["span_id"] == w.id
    assert arts["Report"]["final"] is True and arts["Report"]["review"]["status"] == "approved"
    run = mem.of_type("run")[0]
    assert run["final_artifact_id"] == rep.id
    links = mem.of_type("link")
    rel = {l["relation"]: l for l in links}
    assert rel["used"]["subject"] == {"kind": "span", "id": w.id}
    assert rel["derived_from"]["evidence"] == {"kind": "span", "id": w.id}
    cite = rel["supported_by"]
    assert cite["subject"]["anchor"] == {"start": 4, "end": 15, "text": "Alpha wins."}
    assert cite["evidence"]["anchor"]["start"] == 6 and cite["confidence"] == 0.9
    assert all(l["run_id"] == r.id for l in links)


def test_anchor_not_found_keeps_text_only(mem):
    with tg.run("r"):
        a = tg.artifact("A", "hello")
        b = tg.artifact("B", "world")
        a.cite(b, anchor="missing", quote="world")
    link = mem.of_type("link")[0]
    assert link["subject"]["anchor"] == {"text": "missing"}


def test_artifact_outside_run_raises(mem):
    with pytest.raises(RuntimeError):
        tg.artifact("x", "y")


def test_not_captured_vs_empty(mem):
    with tg.run("r"):
        with tg.span("hidden") as s:
            s.mark_not_captured("input", "framework hides tool args")
            s.set_output(None)
        with tg.span("absent"):
            pass
    s = spans(mem)
    assert s["hidden"]["input"] == {"$tg": "not_captured", "reason": "framework hides tool args"}
    raw = [r for r in mem.records if r["type"] == "span" and r["name"] == "hidden"][-1]
    assert "output" in raw and raw["output"] is None  # genuinely empty, and recorded as such
    assert "input" not in s["absent"] and "output" not in s["absent"]


def test_decorator_capture_disabled(mem):
    @tg.tool(capture_input=False, capture_output=False)
    def secret(x):
        return x

    with tg.run("r"):
        secret(1)
    s = spans(mem)["secret"]
    assert s["input"]["$tg"] == "not_captured" and s["output"]["$tg"] == "not_captured"


def test_redaction_by_key_and_pattern(mem):
    with tg.run("r", input={"api_key": "abc", "password": "p", "max_tokens": 5, "input_tokens": 3}):
        with tg.span("call", input={"headers": {"Authorization": "Bearer abcdefghijklmnopqrstuvwxyz"}, "note": "key sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUVWXYZ"}):
            pass
    run = mem.of_type("run")[0]
    assert run["input"]["api_key"]["$tg"] == "redacted" and run["input"]["password"]["$tg"] == "redacted"
    assert run["input"]["max_tokens"] == 5 and run["input"]["input_tokens"] == 3
    inp = spans(mem)["call"]["input"]
    assert inp["headers"]["Authorization"]["$tg"] == "redacted"
    assert "sk-ant" not in inp["note"] and "‹redacted:anthropic-key›" in inp["note"]


def test_aws_and_custom_patterns():
    r = Redactor(keys=["ssn"], patterns=[("ticket", r"TKT-\d+")])
    cfg = CaptureConfig(redactor=r)
    out = capture({"ssn": "123", "t": "see TKT-42 and AKIAABCDEFGHIJKLMNOP"}, cfg)
    assert out["ssn"]["$tg"] == "redacted"
    assert "TKT-42" not in out["t"] and "AKIA" not in out["t"]


def test_redaction_can_be_disabled():
    tracer = tg.Tracer(tg.MemorySink(), redact=False)
    assert tracer.capture({"api_key": "abc"}) == {"api_key": "abc"}


def test_truncation_and_limits():
    cfg = CaptureConfig(max_string=10, max_items=3, max_depth=2)
    out = capture({"s": "x" * 50, "l": list(range(10)), "deep": {"a": {"b": {"c": 1}}}}, cfg)
    assert out["s"]["$tg"] == "truncated" and out["s"]["length"] == 50 and len(out["s"]["preview"]) == 10
    assert out["l"][:3] == [0, 1, 2] and out["l"][3]["omitted_items"] == 7
    assert out["deep"]["a"]["$tg"] == "truncated"
    assert capture(b"123") == {"$tg": "bytes", "size": 3}
    assert capture({str(i): i for i in range(5)}, cfg)["$tg:more"] == 2


def test_capture_odd_values():
    class Thing:
        def __repr__(self):
            return "<Thing>"

    out = capture({"f": float("nan"), "o": Thing(), "set": {1}})
    assert out["f"] == "nan" and out["o"]["$tg"] == "repr" and out["o"]["repr"] == "<Thing>" and out["set"] == [1]


def test_scaled_clock_tags_run(mem):
    tg.configure(sink=mem, clock=tg.ScaledClock(10))
    with tg.run("fast") as r:
        pass
    assert r.metadata["time_scale"] == 10


def test_events(mem):
    with tg.run("r"):
        with tg.span("s") as s:
            s.event("retrying", kind="retry", level="warn", message="429", data={"attempt": 2})
    ev = mem.of_type("event")[0]
    assert ev["span_id"] == s.id and ev["kind"] == "retry" and ev["data"] == {"attempt": 2}


def test_observe_is_a_run(mem):
    with tg.observe(project="proj") as r:
        with tg.span("x"):
            pass
    assert mem.of_type("run")[0]["project"] == "proj" and r.name == "proj"

import { describe, expect, it } from "vitest";
import type { Artifact, Bundle, Link, Span } from "../types";
import { ancestors, buildModel, lineage, mergeRecord, ROOT, spanDuration, statusAt } from "./graph";
import { buildView } from "./layout";

const span = (id: string, p: Partial<Span> = {}): Span => ({ id, run_id: "run_t", kind: "step", name: id, status: "ok", parent_id: null, ...p });
const artifact = (id: string, p: Partial<Artifact> = {}): Artifact => ({ id, run_id: "run_t", kind: "text", name: id, ...p });
const ref = (x: string) => ({ kind: x.startsWith("art_") ? ("artifact" as const) : ("span" as const), id: x });
const link = (id: string, subject: string, evidence: string, relation = "supported_by"): Link => ({ id, run_id: "run_t", subject: ref(subject), evidence: ref(evidence), relation });
const bundle = (p: Partial<Bundle> = {}): Bundle => ({
  run: { id: "run_t", name: "test run", status: "ok", started_at: 0, ended_at: 1000 },
  spans: [],
  events: [],
  artifacts: [],
  links: [],
  ...p,
});

describe("buildModel", () => {
  it("orders children by start time then seq, and walks depth-first", () => {
    const m = buildModel(
      bundle({
        spans: [
          span("b", { parent_id: "root", started_at: 20, ended_at: 30, seq: 3 }),
          span("root", { kind: "agent", started_at: 0, ended_at: 100, seq: 1 }),
          span("a2", { parent_id: "root", started_at: 10, ended_at: 15, seq: 2.5 }),
          span("a1", { parent_id: "root", started_at: 10, ended_at: 15, seq: 2 }),
          span("a1x", { parent_id: "a1", started_at: 11, ended_at: 12, seq: 4 }),
        ],
      }),
    );
    expect(m.children.get(ROOT)).toEqual(["root"]);
    expect(m.children.get("root")).toEqual(["a1", "a2", "b"]);
    expect(m.order).toEqual(["root", "a1", "a1x", "a2", "b"]);
    expect(m.depth.get("a1x")).toBe(2);
    expect(m.descendants.get("root")).toBe(4);
    expect(m.descendants.get("b")).toBe(0);
    expect(m.t0).toBe(0);
    expect(m.t1).toBe(1000);
  });

  it("treats orphans and self-parented spans as roots", () => {
    const m = buildModel(bundle({ spans: [span("x", { parent_id: "missing", started_at: 1 }), span("y", { parent_id: "y", started_at: 2 })] }));
    expect(m.children.get(ROOT)).toEqual(["x", "y"]);
    expect(m.order).toEqual(["x", "y"]);
  });

  it("breaks cyclic parent pointers instead of dropping or looping", () => {
    const m = buildModel(bundle({ spans: [span("r", { started_at: 0 }), span("p", { parent_id: "q", started_at: 1 }), span("q", { parent_id: "p", started_at: 2 })] }));
    // p is promoted to a root (cycle broken at p→q); q stays p's child.
    expect(m.order).toEqual(["r", "p", "q"]);
    expect(m.children.get(ROOT)).toEqual(["r", "p"]);
    expect(ancestors(m, "p")).toEqual([]);
    expect(ancestors(m, "q")).toEqual(["p"]);
    const v = buildView(m, { collapsed: new Set(), hiddenKinds: new Set() });
    expect(v.nodes.map((n) => n.id).sort()).toEqual(["__task__", "p", "q", "r"]);
  });

  it("indexes artifacts, events, links, retries and dependents; finds the final artifact", () => {
    const m = buildModel(
      bundle({
        run: { id: "run_t", name: "r", status: "ok", final_artifact_id: "art_final" },
        spans: [span("s1", { started_at: 0, ended_at: 5 }), span("s2", { started_at: 6, ended_at: 9, retry_of: "s1", depends_on: ["s1"] })],
        artifacts: [artifact("art_doc", { span_id: "s1", created_at: 4 }), artifact("art_loose", { span_id: "gone" }), artifact("art_final", { span_id: "s2", final: true, created_at: 50 })],
        events: [{ id: "e1", run_id: "run_t", span_id: "s1", kind: "error", name: "boom", ts: 3 }],
        links: [link("l1", "art_final", "art_doc")],
      }),
    );
    expect(m.artifactsBySpan.get("s1")!.map((a) => a.id)).toEqual(["art_doc"]);
    expect(m.artifactsBySpan.get(ROOT)!.map((a) => a.id)).toEqual(["art_loose"]);
    expect(m.eventsBySpan.get("s1")!).toHaveLength(1);
    expect(m.linksBySubject.get("art_final")!.map((l) => l.id)).toEqual(["l1"]);
    expect(m.linksByEvidence.get("art_doc")!.map((l) => l.id)).toEqual(["l1"]);
    expect(m.retries.get("s1")).toEqual(["s2"]);
    expect(m.dependents.get("s1")).toEqual(["s2"]);
    expect(m.finalArtifact?.id).toBe("art_final");
    expect(m.t1).toBe(50); // the run has no end time, so the latest artifact bounds the timeline
  });

  it("falls back to the last artifact flagged final", () => {
    const m = buildModel(bundle({ artifacts: [artifact("art_a", { final: true }), artifact("art_b", { final: true })] }));
    expect(m.finalArtifact?.id).toBe("art_b");
  });

  it("computes token, cost and error totals", () => {
    const m = buildModel(
      bundle({
        spans: [
          span("a", { usage: { input_tokens: 10, output_tokens: 5, total_tokens: 15, cost_usd: 0.5 } }),
          span("b", { usage: { input_tokens: 3, output_tokens: 1 }, status: "error" }),
          span("c"),
        ],
      }),
    );
    expect(m.totals).toEqual({ tokens: 19, cost: 0.5, errors: 1 });
  });
});

describe("lineage", () => {
  // retrieve (retrieval) → doc artifact; plan; synth (llm) depends on plan, uses doc → report (final) cites doc
  const b = bundle({
    spans: [
      span("agent", { kind: "agent", started_at: 0, ended_at: 100 }),
      span("plan", { kind: "llm", parent_id: "agent", started_at: 0, ended_at: 10 }),
      span("retrieve", { kind: "retrieval", parent_id: "agent", started_at: 10, ended_at: 20 }),
      span("synth", { kind: "llm", parent_id: "agent", started_at: 20, ended_at: 30, depends_on: ["plan"] }),
      span("unrelated", { kind: "tool", parent_id: "agent", started_at: 30, ended_at: 40 }),
    ],
    artifacts: [artifact("art_doc", { kind: "document", span_id: "retrieve" }), artifact("art_report", { kind: "markdown", span_id: "synth", final: true })],
    links: [link("l_used", "synth", "art_doc", "used"), link("l_cite", "art_report", "art_doc", "supported_by")],
  });
  const m = buildModel(b);

  it("walks upstream from an answer through producer span, used docs and the retrieval span", () => {
    const lin = lineage(m, ["art_report"]);
    expect([...lin.upstream].sort()).toEqual(["art_doc", "plan", "retrieve", "synth"]);
    expect(lin.upstream.has("unrelated")).toBe(false);
    expect(lin.context.has("agent")).toBe(true);
    expect(lin.edges).toEqual(
      expect.arrayContaining([
        { source: "synth", target: "art_report", relation: "produced" },
        { source: "art_doc", target: "art_report", relation: "supported_by" },
        { source: "art_doc", target: "synth", relation: "used" },
        { source: "retrieve", target: "art_doc", relation: "produced" },
        { source: "plan", target: "synth", relation: "depends_on" },
      ]),
    );
    // edges are de-duplicated
    const keys = lin.edges.map((e) => `${e.source}>${e.target}>${e.relation}`);
    expect(new Set(keys).size).toBe(keys.length);
  });

  it("walks downstream from a source to everything it fed", () => {
    const lin = lineage(m, ["retrieve"]);
    expect([...lin.downstream].sort()).toEqual(["art_doc", "art_report", "synth"]);
  });

  it("propagates failures to failed ancestors and their consumers", () => {
    const fm = buildModel(
      bundle({
        spans: [
          span("orch", { kind: "agent", started_at: 0, ended_at: 100 }),
          span("deleg", { kind: "delegation", parent_id: "orch", status: "error", started_at: 0, ended_at: 50 }),
          span("sub", { kind: "agent", parent_id: "deleg", status: "error", started_at: 1, ended_at: 49 }),
          span("t1", { kind: "tool", parent_id: "sub", status: "error", started_at: 2, ended_at: 10 }),
          span("t2", { kind: "tool", parent_id: "sub", status: "error", started_at: 11, ended_at: 20, retry_of: "t1", attempt: 2 }),
          span("synth", { kind: "llm", parent_id: "orch", started_at: 60, ended_at: 70, depends_on: ["deleg"] }),
        ],
        artifacts: [artifact("art_r", { span_id: "synth", final: true })],
      }),
    );
    const lin = lineage(fm, ["t1"]);
    expect(lin.downstream.has("t2")).toBe(true); // retried by
    expect(lin.downstream.has("sub")).toBe(true); // failed with it
    expect(lin.downstream.has("deleg")).toBe(true);
    expect(lin.downstream.has("orch")).toBe(false); // orchestrator recovered
    expect(lin.downstream.has("synth")).toBe(true); // consumed the failed delegation
    expect(lin.downstream.has("art_r")).toBe(true);
    expect(lin.edges.filter((e) => e.relation === "failed")).toEqual([
      { source: "t1", target: "sub", relation: "failed" },
      { source: "sub", target: "deleg", relation: "failed" },
    ]);
  });

  it("does not propagate from successful spans", () => {
    const lin = lineage(m, ["plan"]);
    expect(lin.edges.some((e) => e.relation === "failed")).toBe(false);
  });
});

describe("helpers", () => {
  it("spanDuration and statusAt", () => {
    expect(spanDuration(span("a", { started_at: 10, ended_at: 25 }))).toBe(15);
    expect(spanDuration(span("a", { started_at: 10, status: "running" }), 40)).toBe(30);
    expect(spanDuration(span("a"))).toBeNull();
    const s = span("a", { started_at: 10, ended_at: 20 });
    expect(statusAt(s, 5)).toBe("future");
    expect(statusAt(s, 15)).toBe("running");
    expect(statusAt(s, 25)).toBe("done");
    expect(statusAt(s, null)).toBe("done");
    expect(statusAt(span("r", { status: "running", started_at: 0 }), null)).toBe("running");
  });

  it("mergeRecord upserts by id without mutating the input", () => {
    const b = bundle({ spans: [span("a", { status: "running", attributes: { x: 1 } })] });
    const next = mergeRecord(b, "span", { ...span("a"), status: "ok", ended_at: 5 });
    expect(next).not.toBe(b);
    expect(b.spans[0].status).toBe("running");
    expect(next.spans).toHaveLength(1);
    expect(next.spans[0]).toMatchObject({ status: "ok", ended_at: 5 });
    const added = mergeRecord(next, "artifact", artifact("art_x"));
    expect(added.artifacts.map((a) => a.id)).toEqual(["art_x"]);
    const run = mergeRecord({ ...b, run: { ...b.run, summary: { spans: 3 } } }, "run", { id: "run_t", name: "renamed", status: "running" });
    expect(run.run).toMatchObject({ name: "renamed", status: "running", summary: { spans: 3 } });
    expect(mergeRecord(b, "unknown", {})).toBe(b);
  });
});

describe("a real research bundle", () => {
  const files = import.meta.glob("./__fixtures__/research.json", { eager: true, import: "default" });
  const real = files["./__fixtures__/research.json"] as Bundle;

  it("has every link anchor resolved against the artifact content", () => {
    const m = buildModel(real);
    expect(real.links.length).toBeGreaterThan(5);
    for (const l of real.links) {
      for (const side of [l.subject, l.evidence]) {
        if (!side.anchor) continue;
        const a = m.artifacts.get(side.id);
        expect(a, `artifact ${side.id}`).toBeDefined();
        const { start, end, text } = side.anchor;
        expect(typeof start).toBe("number");
        expect((a!.content as string).slice(start!, end!)).toBe(text);
      }
    }
  });

  it("traces the final report back to retrieved documents", () => {
    const m = buildModel(real);
    expect(m.finalArtifact).toBeDefined();
    const lin = lineage(m, [m.finalArtifact!.id]);
    const docs = [...lin.upstream].filter((id) => m.artifacts.get(id)?.kind === "document");
    expect(docs.length).toBeGreaterThanOrEqual(3);
    expect([...lin.upstream].some((id) => m.spans.get(id)?.kind === "retrieval")).toBe(true);
  });
});

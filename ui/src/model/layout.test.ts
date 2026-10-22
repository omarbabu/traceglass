import { describe, expect, it } from "vitest";
import type { Artifact, Bundle, Span } from "../types";
import { buildModel, TASK } from "./graph";
import { attachHeight, buildView, defaultCollapsed, layoutView, SIZE, type View } from "./layout";

const span = (id: string, p: Partial<Span> = {}): Span => ({ id, run_id: "run_t", kind: "step", name: id, status: "ok", parent_id: null, ...p });
const artifact = (id: string, p: Partial<Artifact> = {}): Artifact => ({ id, run_id: "run_t", kind: "text", name: id, ...p });
const bundle = (p: Partial<Bundle> = {}): Bundle => ({
  run: { id: "run_t", name: "test run", status: "ok", started_at: 0, ended_at: 1000 },
  spans: [],
  events: [],
  artifacts: [],
  links: [],
  ...p,
});

const none = { collapsed: new Set<string>(), hiddenKinds: new Set<string>() };
const view = (b: Bundle, opts: Partial<typeof none> = {}) => buildView(buildModel(b), { ...none, ...opts });
const edgeSet = (v: View, kind?: string) => v.edges.filter((e) => !kind || e.kind === kind).map((e) => `${e.source}>${e.target}`).sort();
const node = (v: View, id: string) => v.nodes.find((n) => n.id === id);

/** Children of a single root container, each [start, end]. */
function seq(...items: [string, number, number, Partial<Span>?][]): Bundle {
  return bundle({
    spans: [span("root", { kind: "agent", started_at: 0, ended_at: 100 }), ...items.map(([id, s, e, p], i) => span(id, { parent_id: "root", started_at: s, ended_at: e, seq: i + 1, ...p }))],
  });
}

describe("buildView structure", () => {
  it("renders spans with children as containers and leaves as cards, plus the task node", () => {
    const v = view(seq(["a", 0, 10]));
    expect(node(v, "root")).toMatchObject({ type: "container", parent: undefined });
    expect(node(v, "a")).toMatchObject({ type: "span", parent: "root", width: SIZE.span.w, height: SIZE.span.h });
    expect(node(v, TASK)?.type).toBe("task");
    expect(edgeSet(v, "start")).toEqual([`${TASK}>root`]);
    // parents precede children (React Flow requirement)
    expect(v.nodes.findIndex((n) => n.id === "root")).toBeLessThan(v.nodes.findIndex((n) => n.id === "a"));
  });

  it("collapsed containers represent all their descendants and artifacts", () => {
    const b = bundle({
      spans: [
        span("root", { kind: "agent", started_at: 0, ended_at: 100 }),
        span("sub", { kind: "agent", parent_id: "root", started_at: 0, ended_at: 50 }),
        span("leaf", { kind: "tool", parent_id: "sub", started_at: 1, ended_at: 2 }),
        span("deep", { kind: "llm", parent_id: "leaf", started_at: 1, ended_at: 2 }),
      ],
      artifacts: [artifact("art_d", { span_id: "deep" }), artifact("art_s", { span_id: "sub" })],
    });
    const v = view(b, { collapsed: new Set(["sub"]) });
    expect(node(v, "sub")).toMatchObject({ type: "collapsed", width: SIZE.collapsed.w, height: SIZE.collapsed.h });
    expect(node(v, "leaf")).toBeUndefined();
    for (const id of ["sub", "leaf", "deep", "art_d", "art_s"]) expect(v.rep.get(id)).toBe("sub");
    expect(v.attachments.size).toBe(0);
  });

  it("attaches intermediate artifacts as chips and gives the final answer its own node", () => {
    const b = seq(["fetch", 0, 10], ["synth", 10, 20]);
    b.artifacts = [
      artifact("art_doc1", { kind: "document", span_id: "fetch" }),
      artifact("art_doc2", { kind: "document", span_id: "fetch" }),
      artifact("art_final", { kind: "markdown", span_id: "synth", final: true }),
    ];
    const v = view(b);
    expect(v.attachments.get("fetch")).toEqual(["art_doc1", "art_doc2"]);
    expect(v.rep.get("art_doc1")).toBe("fetch");
    expect(node(v, "art_doc1")).toBeUndefined();
    expect(node(v, "fetch")!.height).toBe(SIZE.span.h + attachHeight(2));
    expect(node(v, "art_final")).toMatchObject({ type: "artifact", parent: "root" });
    expect(edgeSet(v, "produce")).toEqual(["synth>art_final"]);
  });

  it("puts artifacts produced by a container (or the run) into its sequence", () => {
    const b = seq(["a", 0, 10]);
    b.artifacts = [artifact("art_c", { span_id: "root", created_at: 15 }), artifact("art_run", { created_at: 200 })];
    const v = view(b);
    expect(node(v, "art_c")).toMatchObject({ type: "artifact", parent: "root" });
    expect(edgeSet(v, "flow")).toContain("a>art_c");
    expect(node(v, "art_run")).toMatchObject({ type: "artifact", parent: undefined });
    expect(edgeSet(v, "flow")).toContain("root>art_run");
  });

  it("caps attachment height after MAX_CHIPS", () => {
    expect(attachHeight(0)).toBe(0);
    expect(attachHeight(5)).toBe(attachHeight(4) + 20);
  });
});

describe("implicit sequencing", () => {
  it("chains sequential siblings without transitive shortcuts", () => {
    const v = view(seq(["a", 0, 10], ["b", 10, 20], ["c", 20, 30]));
    expect(edgeSet(v, "flow")).toEqual(["a>b", "b>c"]);
  });

  it("forks and joins parallel branches", () => {
    const v = view(seq(["p", 0, 10], ["x", 10, 20], ["y", 10, 25], ["j", 30, 40]));
    expect(edgeSet(v, "flow")).toEqual(["p>x", "p>y", "x>j", "y>j"]);
  });

  it("gives a worker in a concurrent pool a single incoming edge", () => {
    const pooled = view(seq(["w1", 0, 10], ["w2", 0, 11], ["w3", 0, 30], ["b", 12, 20]));
    expect(edgeSet(pooled, "flow").filter((e) => e.endsWith(">b"))).toEqual(["w2>b"]);
    const join = view(seq(["w1", 0, 10], ["w2", 0, 11], ["b", 12, 20]));
    expect(edgeSet(join, "flow").filter((e) => e.endsWith(">b"))).toEqual(["w1>b", "w2>b"]);
  });

  it("marks retries", () => {
    const v = view(seq(["s1", 0, 5, { status: "error" }], ["s2", 6, 10, { retry_of: "s1", attempt: 2 }]));
    expect(edgeSet(v, "retry")).toEqual(["s1>s2"]);
    expect(edgeSet(v, "flow")).toEqual([]);
  });

  it("adds retry edges even when the attempts are not adjacent", () => {
    const v = view(seq(["s1", 0, 5, { status: "error" }], ["mid", 5, 6], ["s2", 7, 10, { retry_of: "s1" }]));
    expect(edgeSet(v, "retry")).toEqual(["s1>s2"]);
  });
});

describe("explicit dependencies", () => {
  const b = bundle({
    spans: [
      span("A", { kind: "agent", started_at: 0, ended_at: 50 }),
      span("x", { parent_id: "A", started_at: 0, ended_at: 10 }),
      span("B", { kind: "agent", started_at: 0, ended_at: 60 }),
      span("y0", { parent_id: "B", started_at: 0, ended_at: 5 }),
      span("y", { parent_id: "B", started_at: 20, ended_at: 30, depends_on: ["x"] }),
    ],
  });

  it("draws data edges across containers and skips implicit flow for that node", () => {
    const v = view(b);
    expect(edgeSet(v, "data")).toEqual(["x>y"]);
    expect(edgeSet(v, "flow").some((e) => e.endsWith(">y"))).toBe(false);
  });

  it("re-targets data edges to the collapsed container that hides the dependency", () => {
    const v = view(b, { collapsed: new Set(["A"]) });
    expect(edgeSet(v, "data")).toEqual(["A>y"]);
  });

  it("never connects a node to its own container", () => {
    const own = bundle({
      spans: [span("P", { kind: "agent", started_at: 0, ended_at: 50 }), span("c", { parent_id: "P", started_at: 1, ended_at: 2, depends_on: ["P"] })],
    });
    expect(edgeSet(view(own), "data")).toEqual([]);
  });
});

describe("filters", () => {
  it("hides leaf spans of a kind and reconnects around them", () => {
    const v = view(seq(["a", 0, 10, { kind: "llm" }], ["t", 10, 20, { kind: "tool" }], ["c", 20, 30, { kind: "llm" }]), { hiddenKinds: new Set(["tool"]) });
    expect(node(v, "t")).toBeUndefined();
    expect(edgeSet(v, "flow")).toEqual(["a>c"]);
  });

  it("never hides containers by kind", () => {
    const v = view(seq(["a", 0, 10]), { hiddenKinds: new Set(["agent"]) });
    expect(node(v, "root")?.type).toBe("container");
  });

  it("hides artifact nodes and chips together", () => {
    const b = seq(["fetch", 0, 10], ["synth", 10, 20]);
    b.artifacts = [artifact("art_doc", { span_id: "fetch" }), artifact("art_final", { span_id: "synth", final: true })];
    const v = view(b, { hiddenKinds: new Set(["artifact"]) });
    expect(v.attachments.size).toBe(0);
    expect(v.nodes.some((n) => n.type === "artifact")).toBe(false);
  });
});

describe("defaultCollapsed", () => {
  const big = (subagents: number, each: number) => {
    const spans: Span[] = [span("orch", { kind: "agent", started_at: 0, ended_at: 1e6 })];
    for (let i = 0; i < subagents; i++) {
      spans.push(span(`sub${i}`, { kind: "agent", parent_id: "orch", started_at: i, ended_at: i + 100 }));
      for (let j = 0; j < each; j++) spans.push(span(`sub${i}-t${j}`, { kind: "tool", parent_id: `sub${i}`, started_at: i + j, ended_at: i + j + 1 }));
    }
    return buildModel(bundle({ spans }));
  };

  it("leaves small runs fully expanded", () => {
    expect(defaultCollapsed(big(3, 10)).size).toBe(0);
  });

  it("collapses heavy subagents in large runs", () => {
    const m = big(10, 12);
    expect(m.spans.size).toBeGreaterThan(120);
    const c = defaultCollapsed(m);
    expect(c.has("sub0")).toBe(true);
    expect(c.has("orch")).toBe(false);
    expect(c.size).toBe(10);
  });
});

describe("layoutView", () => {
  it("places every node and keeps children inside their containers", async () => {
    const b = seq(["p", 0, 10], ["x", 10, 20], ["y", 10, 25], ["j", 30, 40]);
    b.artifacts = [artifact("art_f", { span_id: "j", final: true }), artifact("art_doc", { span_id: "x" })];
    const v = view(b);
    for (const dir of ["DOWN", "RIGHT"] as const) {
      const placed = await layoutView(v, dir);
      for (const n of v.nodes) expect(placed.has(n.id), `${n.id} placed (${dir})`).toBe(true);
      const root = placed.get("root")!;
      for (const n of v.nodes.filter((n) => n.parent === "root")) {
        const p = placed.get(n.id)!;
        expect(p.x).toBeGreaterThanOrEqual(0);
        expect(p.y).toBeGreaterThanOrEqual(0);
        expect(p.x + p.width).toBeLessThanOrEqual(root.width + 0.5);
        expect(p.y + p.height).toBeLessThanOrEqual(root.height + 0.5);
      }
      expect(placed.get("x")!.height).toBe(SIZE.span.h + attachHeight(1));
    }
  });

  it("sizes containers whose children are all filtered out", async () => {
    const v = view(seq(["t", 0, 10, { kind: "tool" }]), { hiddenKinds: new Set(["tool"]) });
    const placed = await layoutView(v);
    expect(placed.get("root")!.width).toBeGreaterThan(0);
  });
});

describe("a real research bundle", () => {
  const files = import.meta.glob("./__fixtures__/research.json", { eager: true, import: "default" });
  const real = files["./__fixtures__/research.json"] as Bundle;

  it("produces a consistent view: edges and reps only reference existing nodes", async () => {
    const m = buildModel(real);
    for (const collapsed of [new Set<string>(), defaultCollapsed(m), new Set(m.order.filter((id) => (m.descendants.get(id) ?? 0) > 0 && (m.depth.get(id) ?? 0) >= 1))]) {
      const v = buildView(m, { collapsed, hiddenKinds: new Set() });
      const ids = new Set(v.nodes.map((n) => n.id));
      expect(ids.size).toBe(v.nodes.length); // no duplicates
      for (const e of v.edges) {
        expect(ids.has(e.source), `edge source ${e.source}`).toBe(true);
        expect(ids.has(e.target), `edge target ${e.target}`).toBe(true);
      }
      for (const [, to] of v.rep) expect(ids.has(to)).toBe(true);
      for (const s of m.spans.keys()) expect(v.rep.has(s)).toBe(true);
      for (const a of m.artifacts.keys()) expect(v.rep.has(a)).toBe(true);
      for (const n of v.nodes) if (n.parent) expect(ids.has(n.parent)).toBe(true);
      const placed = await layoutView(v);
      expect(placed.size).toBe(v.nodes.length);
    }
  });
});

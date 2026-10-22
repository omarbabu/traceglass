import type { Artifact, Bundle, Link, Run, Span, TraceEvent } from "../types";

/** An indexed, navigable view of one run. Built once per bundle revision. */
export interface GraphModel {
  run: Run;
  spans: Map<string, Span>;
  artifacts: Map<string, Artifact>;
  /** Children per span id; root spans live under ROOT. Ordered by start time, then seq. */
  children: Map<string, string[]>;
  /** Effective parent of each span (after orphans and cycles are resolved); absent for roots. */
  parent: Map<string, string>;
  artifactsBySpan: Map<string, Artifact[]>;
  eventsBySpan: Map<string, TraceEvent[]>;
  linksBySubject: Map<string, Link[]>;
  linksByEvidence: Map<string, Link[]>;
  links: Link[];
  events: TraceEvent[];
  retries: Map<string, string[]>;
  dependents: Map<string, string[]>;
  /** Spans in depth-first execution order — the order j/k walks and the timeline lists. */
  order: string[];
  depth: Map<string, number>;
  descendants: Map<string, number>;
  t0: number;
  t1: number;
  finalArtifact?: Artifact;
  totals: { tokens: number; cost: number | null; errors: number };
}

export const ROOT = "__root__";
export const TASK = "__task__";

function push<K, V>(m: Map<K, V[]>, k: K, v: V) {
  const arr = m.get(k);
  if (arr) arr.push(v);
  else m.set(k, [v]);
}

const byStart = (a: Span, b: Span) =>
  (a.started_at ?? Infinity) - (b.started_at ?? Infinity) || (a.seq ?? 0) - (b.seq ?? 0);

export function buildModel(bundle: Bundle): GraphModel {
  const spans = new Map(bundle.spans.map((s) => [s.id, s]));
  const artifacts = new Map(bundle.artifacts.map((a) => [a.id, a]));
  const children = new Map<string, string[]>();
  const sorted = [...bundle.spans].sort(byStart);
  for (const s of sorted) {
    const parent = s.parent_id && spans.has(s.parent_id) && s.parent_id !== s.id ? s.parent_id : ROOT;
    push(children, parent, s.id);
  }
  const artifactsBySpan = new Map<string, Artifact[]>();
  for (const a of bundle.artifacts) push(artifactsBySpan, a.span_id && spans.has(a.span_id) ? a.span_id : ROOT, a);
  const eventsBySpan = new Map<string, TraceEvent[]>();
  for (const e of bundle.events) push(eventsBySpan, e.span_id ?? ROOT, e);
  const linksBySubject = new Map<string, Link[]>();
  const linksByEvidence = new Map<string, Link[]>();
  for (const l of bundle.links) {
    push(linksBySubject, l.subject.id, l);
    push(linksByEvidence, l.evidence.id, l);
  }
  const retries = new Map<string, string[]>();
  const dependents = new Map<string, string[]>();
  for (const s of bundle.spans) {
    if (s.retry_of) push(retries, s.retry_of, s.id);
    for (const d of s.depends_on ?? []) push(dependents, d, s.id);
  }

  const order: string[] = [];
  const depth = new Map<string, number>();
  const descendants = new Map<string, number>();
  const seen = new Set<string>();
  const walk = (id: string, d: number): number => {
    if (seen.has(id)) return 0; // defend against cyclic parent pointers in foreign data
    seen.add(id);
    order.push(id);
    depth.set(id, d);
    let n = 0;
    for (const c of children.get(id) ?? []) n += 1 + walk(c, d + 1);
    descendants.set(id, n);
    return n;
  };
  for (const r of children.get(ROOT) ?? []) walk(r, 0);
  // Spans caught in a parent cycle (possible in foreign data) are unreachable from the root:
  // break the cycle and promote them so nothing silently disappears.
  for (const s of sorted)
    if (!seen.has(s.id)) {
      const p = s.parent_id!;
      children.set(p, (children.get(p) ?? []).filter((c) => c !== s.id));
      push(children, ROOT, s.id);
      walk(s.id, 0);
    }

  const parent = new Map<string, string>();
  for (const [p, kids] of children) if (p !== ROOT) for (const c of kids) parent.set(c, p);

  let t0 = bundle.run.started_at ?? Infinity;
  let t1 = bundle.run.ended_at ?? -Infinity;
  for (const s of bundle.spans) {
    if (s.started_at != null) t0 = Math.min(t0, s.started_at);
    t1 = Math.max(t1, s.ended_at ?? s.started_at ?? -Infinity);
  }
  for (const a of bundle.artifacts) if (a.created_at != null) t1 = Math.max(t1, a.created_at);
  if (!isFinite(t0)) t0 = Date.now();
  if (!isFinite(t1) || t1 < t0) t1 = t0;
  if (bundle.run.status === "running") t1 = Math.max(t1, Date.now());

  const finalArtifact =
    (bundle.run.final_artifact_id && artifacts.get(bundle.run.final_artifact_id)) ||
    [...bundle.artifacts].reverse().find((a) => a.final);

  let tokens = 0;
  let cost: number | null = null;
  let errors = 0;
  for (const s of bundle.spans) {
    tokens += s.usage?.total_tokens ?? (s.usage?.input_tokens ?? 0) + (s.usage?.output_tokens ?? 0);
    if (s.usage?.cost_usd != null) cost = (cost ?? 0) + s.usage.cost_usd;
    if (s.status === "error") errors++;
  }

  return {
    totals: { tokens, cost, errors },
    run: bundle.run,
    spans,
    artifacts,
    children,
    parent,
    artifactsBySpan,
    eventsBySpan,
    linksBySubject,
    linksByEvidence,
    links: bundle.links,
    events: bundle.events,
    retries,
    dependents,
    order,
    depth,
    descendants,
    t0,
    t1,
    finalArtifact: finalArtifact || undefined,
  };
}

export function ancestors(m: GraphModel, id: string): string[] {
  const out: string[] = [];
  const art = m.artifacts.get(id);
  let cur: string | undefined = art ? (art.span_id && m.spans.has(art.span_id) ? art.span_id : undefined) : m.parent.get(id);
  const guard = new Set<string>([id]);
  while (cur && !guard.has(cur)) {
    guard.add(cur);
    out.push(cur);
    cur = m.parent.get(cur);
  }
  return out;
}

export interface LineageEdge {
  source: string; // upstream (where information came from)
  target: string; // downstream (what it fed into)
  relation: string;
}

export interface Lineage {
  upstream: Set<string>;
  downstream: Set<string>;
  context: Set<string>;
  edges: LineageEdge[];
}

/** Direct upstream neighbours: what this node was produced from / depended on. */
function upstreamOf(m: GraphModel, id: string): LineageEdge[] {
  const out: LineageEdge[] = [];
  const a = m.artifacts.get(id);
  if (a) {
    if (a.span_id && m.spans.has(a.span_id)) out.push({ source: a.span_id, target: id, relation: "produced" });
    if (a.previous_id) out.push({ source: a.previous_id, target: id, relation: "revises" });
  }
  const s = m.spans.get(id);
  if (s) for (const d of s.depends_on ?? []) if (m.spans.has(d)) out.push({ source: d, target: id, relation: "depends_on" });
  for (const l of m.linksBySubject.get(id) ?? []) out.push({ source: l.evidence.id, target: id, relation: l.relation });
  return out;
}

/** Direct downstream neighbours: what consumed this node. */
function downstreamOf(m: GraphModel, id: string): LineageEdge[] {
  const out: LineageEdge[] = [];
  for (const a of m.artifactsBySpan.get(id) ?? []) out.push({ source: id, target: a.id, relation: "produced" });
  for (const d of m.dependents.get(id) ?? []) out.push({ source: id, target: d, relation: "depends_on" });
  for (const l of m.linksByEvidence.get(id) ?? []) out.push({ source: id, target: l.subject.id, relation: l.relation });
  for (const a of m.artifacts.values()) if (a.previous_id === id) out.push({ source: id, target: a.id, relation: "revises" });
  for (const r of m.retries.get(id) ?? []) out.push({ source: id, target: r, relation: "retry" });
  return out;
}

function closure(m: GraphModel, start: string[], step: (m: GraphModel, id: string) => LineageEdge[], up: boolean, limit = 2000) {
  const nodes = new Set<string>();
  const edges: LineageEdge[] = [];
  const queue = [...start];
  while (queue.length && nodes.size < limit) {
    const id = queue.shift()!;
    for (const e of step(m, id)) {
      const next = up ? e.source : e.target;
      if (!m.spans.has(next) && !m.artifacts.has(next)) continue;
      edges.push(e);
      if (!nodes.has(next) && !start.includes(next)) {
        nodes.add(next);
        queue.push(next);
      }
    }
  }
  return { nodes, edges };
}

/** Provenance of a node: everything upstream of it, everything it affected, plus structural context. */
export function lineage(m: GraphModel, ids: string[]): Lineage {
  const up = closure(m, ids, upstreamOf, true);
  // A failure propagates to every enclosing step that failed with it, and to whatever consumed those.
  const propagated: LineageEdge[] = [];
  for (const id of ids) {
    let child = id;
    if (m.spans.get(id)?.status !== "error") continue;
    for (const a of ancestors(m, id)) {
      if (m.spans.get(a)?.status !== "error") break;
      propagated.push({ source: child, target: a, relation: "failed" });
      child = a;
    }
  }
  const down = closure(m, [...ids, ...propagated.map((e) => e.target)], downstreamOf, false);
  for (const e of propagated) down.nodes.add(e.target);
  down.edges.push(...propagated);
  const context = new Set<string>();
  for (const id of [...ids, ...up.nodes]) for (const a of ancestors(m, id)) context.add(a);
  const seen = new Set<string>();
  const edges = [...up.edges, ...down.edges].filter((e) => {
    const k = `${e.source}>${e.target}>${e.relation}`;
    if (seen.has(k)) return false;
    seen.add(k);
    return true;
  });
  return { upstream: up.nodes, downstream: down.nodes, context, edges };
}

export function spanDuration(s: Span, now = Date.now()): number | null {
  if (s.started_at == null) return null;
  if (s.ended_at != null) return s.ended_at - s.started_at;
  return s.status === "running" ? now - s.started_at : null;
}

/** Status of a span as of time ``t`` (for replay). */
export function statusAt(s: Span, t: number | null): "future" | "running" | "done" {
  if (t == null) return s.status === "running" ? "running" : "done";
  if (s.started_at == null || s.started_at > t) return "future";
  if (s.ended_at == null || s.ended_at > t) return "running";
  return "done";
}

/** Merge a streamed record into a bundle (records are upserts). Returns a new bundle object. */
export function mergeRecord(b: Bundle, type: string, rec: any): Bundle {
  const upsert = <T extends { id: string }>(arr: T[], r: T): T[] => {
    const i = arr.findIndex((x) => x.id === r.id);
    if (i < 0) return [...arr, r];
    const next = arr.slice();
    next[i] = { ...arr[i], ...r };
    return next;
  };
  switch (type) {
    case "run":
      return { ...b, run: { ...b.run, ...rec, summary: b.run.summary } };
    case "span":
      return { ...b, spans: upsert(b.spans, rec) };
    case "event":
      return { ...b, events: upsert(b.events, rec) };
    case "artifact":
      return { ...b, artifacts: upsert(b.artifacts, rec) };
    case "link":
      return { ...b, links: upsert(b.links, rec) };
    default:
      return b;
  }
}

/** Drop a record that the source no longer contains (e.g. a re-imported session changed shape). */
export function removeRecord(b: Bundle, type: string, id: string): Bundle {
  const drop = <T extends { id: string }>(arr: T[]) => (arr.some((x) => x.id === id) ? arr.filter((x) => x.id !== id) : arr);
  switch (type) {
    case "span":
      return { ...b, spans: drop(b.spans) };
    case "event":
      return { ...b, events: drop(b.events) };
    case "artifact":
      return { ...b, artifacts: drop(b.artifacts) };
    case "link":
      return { ...b, links: drop(b.links) };
    default:
      return b;
  }
}

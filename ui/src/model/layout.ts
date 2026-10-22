import ELK, { type ElkExtendedEdge, type ElkNode } from "elkjs/lib/elk.bundled.js";
import type { Artifact, Span } from "../types";
import { ROOT, TASK, type GraphModel } from "./graph";

export type VNodeType = "span" | "container" | "collapsed" | "artifact" | "task" | "segment" | "segment-open";
export type VEdgeKind = "flow" | "data" | "retry" | "produce" | "start";

export interface VNode {
  id: string;
  type: VNodeType;
  parent?: string;
  width: number;
  height: number;
}

export interface VEdge {
  id: string;
  source: string;
  target: string;
  kind: VEdgeKind;
}

export interface View {
  nodes: VNode[];
  edges: VEdge[];
  /** Artifacts rendered as chips inside the node of the span that produced them. */
  attachments: Map<string, string[]>;
  /** Maps every span/artifact id to the id of the node that currently represents it. */
  rep: Map<string, string>;
  /** View-only pages of very long step sequences (not recorded structure). */
  segments: Map<string, Segment>;
}

export interface Segment {
  id: string;
  container: string;
  index: number;
  ids: string[];
  from: number;
  to: number;
  isLast: boolean;
  open: boolean;
}

/** Containers with more direct steps than this are paged into segments of SEG_SIZE. */
export const SEG_THRESHOLD = 30;
export const SEG_SIZE = 20;

export const segmentId = (container: string, index: number) => `seg:${container}:${index}`;

/** The latest page of a container is open by default; earlier pages are folded. */
export function segmentOpen(collapsed: Set<string>, segId: string, isLast: boolean): boolean {
  return isLast ? !collapsed.has(segId) : collapsed.has("open:" + segId);
}

export function toggleSegmentKey(collapsed: Set<string>, segId: string, isLast: boolean): Set<string> {
  const c = new Set(collapsed);
  const key = isLast ? segId : "open:" + segId;
  if (c.has(key)) c.delete(key);
  else c.add(key);
  return c;
}

/** Expand every container and open every segment on the path to ``id`` so it becomes visible. */
export function revealPath(m: GraphModel, id: string, collapsed: Set<string>): Set<string> {
  const out = new Set(collapsed);
  const art = m.artifacts.get(id);
  const start = art ? art.span_id ?? null : id;
  const chain: string[] = [];
  let cur: string | null | undefined = start;
  const guard = new Set<string>();
  while (cur && m.spans.has(cur) && !guard.has(cur)) {
    guard.add(cur);
    chain.push(cur);
    cur = m.parent.get(cur);
  }
  for (const node of chain) {
    if (node !== start || art) out.delete(node);
    const container = m.parent.get(node) ?? ROOT;
    const kids = m.children.get(container) ?? [];
    if (kids.length > SEG_THRESHOLD) {
      const idx = Math.floor(kids.indexOf(node) / SEG_SIZE);
      const last = Math.floor((kids.length - 1) / SEG_SIZE);
      const seg = segmentId(container, idx);
      if (idx === last) out.delete(seg);
      else out.add("open:" + seg);
    }
  }
  return out;
}

export interface ViewOptions {
  collapsed: Set<string>;
  hiddenKinds: Set<string>;
}

export const SIZE = {
  span: { w: 236, h: 58 },
  collapsed: { w: 248, h: 66 },
  artifact: { w: 212, h: 50 },
  task: { w: 236, h: 66 },
  segment: { w: 248, h: 70 },
  groupHeader: 46,
};

interface Item {
  id: string;
  start: number;
  end: number;
  span?: Span;
}

/** Decide which nodes are visible and how they connect, before any geometry. */
export function buildView(m: GraphModel, opts: ViewOptions): View {
  const nodes: VNode[] = [];
  const edges: VEdge[] = [];
  const rep = new Map<string, string>();
  const attachments = new Map<string, string[]>();
  const segments = new Map<string, Segment>();
  const edgeKeys = new Set<string>();
  const parentOf = new Map<string, string | undefined>();
  const hideArtifacts = opts.hiddenKinds.has("artifact");

  const addEdge = (source: string, target: string, kind: VEdgeKind) => {
    if (source === target) return;
    const k = `${source}>${target}`;
    if (edgeKeys.has(k)) return;
    edgeKeys.add(k);
    edges.push({ id: `${kind}:${k}`, source, target, kind });
  };

  const mapRep = (id: string, to: string) => {
    rep.set(id, to);
    for (const c of m.children.get(id) ?? []) mapRep(c, to);
    for (const a of m.artifactsBySpan.get(id) ?? []) rep.set(a.id, to);
  };

  const artifactTime = (a: Artifact) => a.created_at ?? 0;

  const spanEnd = (s: Span) => s.ended_at ?? (s.status === "running" ? Infinity : s.started_at ?? 0);

  const visit = (containerId: string, parent: string | undefined, only?: string[]) => {
    const items: Item[] = [];
    const kids = only ?? m.children.get(containerId) ?? [];
    if (!only && kids.length > SEG_THRESHOLD) {
      // Page very long sequences (a 300-step coding turn) into folded segments; the latest stays open.
      const count = Math.ceil(kids.length / SEG_SIZE);
      for (let i = 0; i < count; i++) {
        const ids = kids.slice(i * SEG_SIZE, (i + 1) * SEG_SIZE);
        const segId = segmentId(containerId, i);
        const isLast = i === count - 1;
        const open = segmentOpen(opts.collapsed, segId, isLast);
        segments.set(segId, { id: segId, container: containerId, index: i, ids, from: i * SEG_SIZE + 1, to: i * SEG_SIZE + ids.length, isLast, open });
        const spans = ids.map((x) => m.spans.get(x)!);
        const start = Math.min(...spans.map((x) => x.started_at ?? 0));
        const end = Math.max(...spans.map(spanEnd));
        parentOf.set(segId, parent);
        rep.set(segId, segId);
        if (open) {
          nodes.push({ id: segId, type: "segment-open", parent, width: 0, height: 0 });
          visit(containerId, segId, ids);
        } else {
          nodes.push({ id: segId, type: "segment", parent, width: SIZE.segment.w, height: SIZE.segment.h });
          for (const x of ids) mapRep(x, segId);
        }
        items.push({ id: segId, start, end });
      }
      addContainerArtifacts(containerId, parent, items);
      sequence(items, containerId === ROOT);
      return;
    }
    for (const sid of kids) {
      const s = m.spans.get(sid)!;
      const hasKids = (m.children.get(sid)?.length ?? 0) > 0;
      const start = s.started_at ?? 0;
      const end = s.ended_at ?? (s.status === "running" ? Infinity : start);
      if (hasKids && !opts.collapsed.has(sid)) {
        nodes.push({ id: sid, type: "container", parent, width: 0, height: 0 });
        parentOf.set(sid, parent);
        rep.set(sid, sid);
        visit(sid, sid);
        items.push({ id: sid, start, end, span: s });
        continue;
      }
      if (!hasKids && opts.hiddenKinds.has(s.kind)) continue;
      const collapsed = hasKids;
      const produced = collapsed || hideArtifacts ? [] : m.artifactsBySpan.get(sid) ?? [];
      // Intermediate artifacts ride along inside their producer; the final answer gets its own node.
      const attached = produced.filter((a) => !a.final).map((a) => a.id);
      if (attached.length) attachments.set(sid, attached);
      nodes.push({
        id: sid,
        type: collapsed ? "collapsed" : "span",
        parent,
        width: collapsed ? SIZE.collapsed.w : SIZE.span.w,
        height: collapsed ? SIZE.collapsed.h : SIZE.span.h + attachHeight(attached.length),
      });
      parentOf.set(sid, parent);
      if (collapsed) mapRep(sid, sid);
      else rep.set(sid, sid);
      for (const a of attached) rep.set(a, sid);
      items.push({ id: sid, start, end, span: s });
      for (const a of produced) {
        if (!a.final) continue;
        nodes.push({ id: a.id, type: "artifact", parent, width: SIZE.artifact.w, height: SIZE.artifact.h });
        parentOf.set(a.id, parent);
        rep.set(a.id, a.id);
        addEdge(sid, a.id, "produce");
      }
    }
    if (!only) addContainerArtifacts(containerId, parent, items);
    sequence(items, containerId === ROOT && !only);
  };

  // Artifacts produced directly by a container span (or by the run itself) take part in its sequence.
  function addContainerArtifacts(containerId: string, parent: string | undefined, items: Item[]) {
    if (hideArtifacts) return;
    for (const a of m.artifactsBySpan.get(containerId) ?? []) {
      nodes.push({ id: a.id, type: "artifact", parent, width: SIZE.artifact.w, height: SIZE.artifact.h });
      parentOf.set(a.id, parent);
      rep.set(a.id, a.id);
      const t = artifactTime(a);
      items.push({ id: a.id, start: t, end: t });
    }
  }

  const isAncestor = (anc: string, id: string): boolean => {
    let cur = parentOf.get(id);
    while (cur) {
      if (cur === anc) return true;
      cur = parentOf.get(cur);
    }
    return false;
  };

  const pendingData: Array<[string, string]> = [];

  const sequence = (items: Item[], atRoot: boolean) => {
    items.sort((a, b) => a.start - b.start || (a.span?.seq ?? 0) - (b.span?.seq ?? 0));
    for (let j = 0; j < items.length; j++) {
      const b = items[j];
      const deps = b.span?.depends_on?.filter((d) => m.spans.has(d)) ?? [];
      if (deps.length) {
        for (const d of deps) pendingData.push([d, b.id]);
        continue;
      }
      const earlier = items.slice(0, j);
      const preds = earlier.filter((a) => a.end <= b.start + 1);
      let maximal = preds.filter((a) => !preds.some((c) => c !== a && c.start >= a.end - 0.5 && c.start > a.start));
      // If something that started earlier is still running, b is one worker in a concurrent pool, not a
      // join point: connect it only to the step it directly followed, so pools don't turn into a mesh.
      const concurrent = earlier.some((a) => a.start < b.start && a.end > b.start + 1);
      if (concurrent && maximal.length > 1) maximal = [maximal.reduce((x, y) => (y.end > x.end ? y : x))];
      for (const a of maximal) addEdge(a.id, b.id, b.span?.retry_of === a.id ? "retry" : "flow");
      if (!preds.length && atRoot) addEdge(TASK, b.id, "start");
    }
  };

  nodes.push({ id: TASK, type: "task", width: SIZE.task.w, height: SIZE.task.h });
  visit(ROOT, undefined);

  // Explicit data dependencies are resolved after every node exists (they may cross containers).
  for (const [d, b] of pendingData) {
    const src = rep.get(d);
    const dst = rep.get(b) ?? b;
    if (!src || src === dst || isAncestor(src, dst) || isAncestor(dst, src)) continue;
    addEdge(src, dst, "data");
  }
  for (const s of m.spans.values()) {
    if (!s.retry_of) continue;
    const src = rep.get(s.retry_of);
    const dst = rep.get(s.id);
    if (src && dst && src !== dst && !isAncestor(src, dst) && !isAncestor(dst, src)) {
      const k = `${src}>${dst}`;
      const existing = edges.find((e) => `${e.source}>${e.target}` === k);
      if (existing) existing.kind = "retry";
      else addEdge(src, dst, "retry");
    }
  }
  return { nodes, edges, rep, attachments, segments };
}

export const MAX_CHIPS = 4;

export function attachHeight(n: number): number {
  if (!n) return 0;
  return 4 + Math.min(n, MAX_CHIPS) * 25 + (n > MAX_CHIPS ? 20 : 0);
}

export interface Placed {
  x: number;
  y: number;
  width: number;
  height: number;
}

const elk = new ELK();

const ROOT_OPTIONS: Record<string, string> = {
  "elk.algorithm": "layered",
  "elk.direction": "RIGHT",
  "elk.hierarchyHandling": "INCLUDE_CHILDREN",
  "elk.layered.spacing.nodeNodeBetweenLayers": "64",
  "elk.layered.spacing.edgeNodeBetweenLayers": "28",
  "elk.spacing.nodeNode": "22",
  "elk.spacing.componentComponent": "40",
  "elk.layered.considerModelOrder.strategy": "NODES_AND_EDGES",
  "elk.layered.crossingMinimization.forceNodeModelOrder": "false",
  "elk.layered.nodePlacement.strategy": "BRANDES_KOEPF",
  "elk.layered.nodePlacement.bk.fixedAlignment": "BALANCED",
  "elk.edgeRouting": "SPLINES",
  "elk.padding": "[top=24,left=24,bottom=24,right=24]",
};

const GROUP_OPTIONS: Record<string, string> = {
  "elk.padding": `[top=${SIZE.groupHeader + 14},left=18,bottom=18,right=18]`,
  "elk.nodeSize.constraints": "MINIMUM_SIZE",
  "elk.nodeSize.minimum": "(300, 90)",
  "elk.layered.spacing.nodeNodeBetweenLayers": "52",
  "elk.spacing.nodeNode": "18",
};

/** Hierarchical layered layout (left → right in time, nested agents as containers). */
export async function layoutView(view: View, direction: "DOWN" | "RIGHT" = "DOWN"): Promise<Map<string, Placed>> {
  const elkNodes = new Map<string, ElkNode>();
  const vertical = direction === "DOWN";
  const rootOptions = {
    ...ROOT_OPTIONS,
    "elk.direction": direction,
    "elk.layered.spacing.nodeNodeBetweenLayers": vertical ? "40" : "64",
    "elk.spacing.nodeNode": vertical ? "28" : "22",
  };
  const groupOptions = {
    ...GROUP_OPTIONS,
    "elk.layered.spacing.nodeNodeBetweenLayers": vertical ? "34" : "52",
    "elk.spacing.nodeNode": vertical ? "24" : "18",
  };
  const root: ElkNode = { id: "root", layoutOptions: rootOptions, children: [], edges: [] };
  for (const n of view.nodes) {
    const en: ElkNode =
      n.type === "container" || n.type === "segment-open"
        ? { id: n.id, layoutOptions: groupOptions, children: [] }
        : { id: n.id, width: n.width, height: n.height };
    elkNodes.set(n.id, en);
    const parent = n.parent ? elkNodes.get(n.parent) : root;
    (parent ?? root).children!.push(en);
  }
  // Empty groups (all children filtered out) still need a size.
  for (const n of view.nodes)
    if (n.type === "container" || n.type === "segment-open") {
      const en = elkNodes.get(n.id)!;
      if (!en.children?.length) {
        en.width = 280;
        en.height = 90;
      }
    }
  root.edges = view.edges
    .filter((e) => elkNodes.has(e.source) && elkNodes.has(e.target))
    .map((e): ElkExtendedEdge => ({ id: e.id, sources: [e.source], targets: [e.target] }));
  const out = await elk.layout(root);
  const placed = new Map<string, Placed>();
  const walk = (n: ElkNode) => {
    for (const c of n.children ?? []) {
      placed.set(c.id, { x: c.x ?? 0, y: c.y ?? 0, width: c.width ?? 0, height: c.height ?? 0 });
      walk(c);
    }
  };
  walk(out);
  return placed;
}

/** Collapse heavy subtrees by default so very large runs open legibly. */
export function defaultCollapsed(m: GraphModel): Set<string> {
  const out = new Set<string>();
  const total = m.spans.size;
  // Coding-agent sessions are long chains of turns: open on the latest turn, fold the rest.
  const src = m.run.source ?? "";
  if ((src === "claude-code" || src === "kiro") && total > 40) {
    const turns = (m.children.get(ROOT) ?? []).filter((id) => (m.descendants.get(id) ?? 0) > 0);
    for (const id of turns.slice(0, -1)) out.add(id);
    const last = turns[turns.length - 1];
    // Inside the open turn, fold subagents that would dominate the view.
    if (last) for (const id of m.order) if (id !== last && (m.descendants.get(id) ?? 0) >= 12 && !turns.includes(id)) out.add(id);
    return out;
  }
  if (total <= 120) return out;
  const budget = 90;
  for (const id of m.order) {
    const d = m.descendants.get(id) ?? 0;
    if (d > 0 && (m.depth.get(id) ?? 0) >= 1 && d >= 6) out.add(id);
  }
  // If collapsing deep containers is not enough, collapse top-level containers too.
  let visible = 0;
  for (const id of m.order) {
    let hidden = false;
    let p = m.parent.get(id);
    while (p) {
      if (out.has(p)) {
        hidden = true;
        break;
      }
      p = m.parent.get(p);
    }
    if (!hidden) visible++;
  }
  if (visible > budget) for (const id of m.children.get(ROOT) ?? []) if ((m.descendants.get(id) ?? 0) > 0) out.add(id);
  return out;
}

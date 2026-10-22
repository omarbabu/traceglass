import {
  Background,
  BackgroundVariant,
  Controls,
  MarkerType,
  MiniMap,
  ReactFlow,
  ReactFlowProvider,
  useReactFlow,
  type Edge,
  type Node,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { ArrowRightLeft, ArrowUpDown, ChevronsDownUp, ChevronsUpDown, Maximize, Route } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { lineage, statusAt, TASK, type GraphModel, type Lineage } from "../model/graph";
import { SPAN_KINDS, relation } from "../model/kinds";
import { buildView, layoutView, type Placed, type View } from "../model/layout";
import { useStore } from "../store";
import { runActivity } from "../types";
import type { Flags } from "./nodes";
import { nodeTypes } from "./nodes";

const FIT_PADDING = { top: "64px", bottom: "28px", left: "36px", right: "36px" } as const;

const EDGE_STYLE: Record<string, React.CSSProperties> = {
  flow: { stroke: "#3b3e48", strokeWidth: 1.5 },
  start: { stroke: "#3b3e48", strokeWidth: 1.5, strokeDasharray: "3 4" },
  data: { stroke: "#7277e0", strokeWidth: 1.6, strokeDasharray: "6 4" },
  retry: { stroke: "#f5c451", strokeWidth: 1.6, strokeDasharray: "2 4" },
  produce: { stroke: "#8f7d5a", strokeWidth: 1.4 },
};

function subtreeStats(m: GraphModel) {
  const tokens = new Map<string, number>();
  const walk = (id: string): number => {
    const s = m.spans.get(id)!;
    let t = s.usage?.total_tokens ?? (s.usage?.input_tokens ?? 0) + (s.usage?.output_tokens ?? 0);
    for (const c of m.children.get(id) ?? []) t += walk(c);
    tokens.set(id, t);
    return t;
  };
  for (const r of m.children.get("__root__") ?? []) walk(r);
  return tokens;
}

function useHighlight(m: GraphModel | null, selected: string | null, focusLink: string | null): Lineage | null {
  return useMemo(() => {
    if (!m || !selected) return null;
    if (focusLink) {
      const l = m.links.find((x) => x.id === focusLink);
      if (l) {
        const lin = lineage(m, [l.evidence.id]);
        lin.downstream = new Set([l.subject.id]);
        lin.edges = [...lin.edges.filter((e) => lin.upstream.has(e.source) || e.target === l.evidence.id), { source: l.evidence.id, target: l.subject.id, relation: l.relation }];
        lin.upstream.add(l.evidence.id);
        return lin;
      }
    }
    if (selected === TASK) return null;
    return lineage(m, [selected]);
  }, [m, selected, focusLink]);
}

function Flow() {
  const model = useStore((s) => s.model)!;
  const collapsed = useStore((s) => s.collapsed);
  const hiddenKinds = useStore((s) => s.hiddenKinds);
  const selected = useStore((s) => s.selected);
  const focusLink = useStore((s) => s.focusLink);
  const replayT = useStore((s) => s.replayT);
  const showEvidence = useStore((s) => s.showEvidence);
  const focusTick = useStore((s) => s.focusTick);
  const fitTick = useStore((s) => s.fitTick);
  const select = useStore((s) => s.select);
  const direction = useStore((s) => s.direction);
  const rf = useReactFlow();

  const view: View = useMemo(() => buildView(model, { collapsed, hiddenKinds }), [model, collapsed, hiddenKinds]);
  const structKey = useMemo(
    () => direction + view.nodes.map((n) => n.id + n.type + (n.parent ?? "")).join("|") + "#" + view.edges.map((e) => e.id).join("|"),
    [view, direction],
  );
  const [placed, setPlaced] = useState<Map<string, Placed> | null>(null);
  const [layoutKey, setLayoutKey] = useState("");
  const runId = model.run.id;
  const fittedRun = useRef<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    layoutView(view, direction)
      .then((p) => {
        if (cancelled) return;
        setPlaced(p);
        setLayoutKey(structKey);
      })
      .catch((e) => console.error("layout failed", e));
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [structKey]);

  const stats = useMemo(() => subtreeStats(model), [model]);
  const hl = useHighlight(model, selected, focusLink);

  const nodes: Node[] = useMemo(() => {
    if (!placed) return [];
    const rep = (id: string) => view.rep.get(id) ?? id;
    const up = new Set<string>();
    const down = new Set<string>();
    const ctx = new Set<string>();
    if (hl) {
      hl.upstream.forEach((x) => up.add(rep(x)));
      hl.downstream.forEach((x) => down.add(rep(x)));
      hl.context.forEach((x) => ctx.add(rep(x)));
    }
    const sel = selected ? rep(selected) : null;
    const flagsFor = (id: string, futureAt: number | null | undefined, span?: { started_at?: number | null; ended_at?: number | null; status: string }): Flags => {
      const f: Flags = {};
      if (id === sel) f.selected = true;
      else if (up.has(id)) f.upstream = true;
      else if (down.has(id)) f.downstream = true;
      if (hl && !f.selected && !f.upstream && !f.downstream && !ctx.has(id)) f.dimmed = true;
      if (replayT != null) {
        if (span) {
          const st = statusAt(span as any, replayT);
          if (st === "future") f.future = true;
          if (st === "running") f.runningAt = true;
        } else if (futureAt != null && futureAt > replayT) f.future = true;
      }
      return f;
    };
    const segStats = (ids: string[]) => {
      const st = { llm: 0, tools: 0, errors: 0, edits: 0, running: false, start: null as number | null, end: null as number | null };
      const walk = (id: string) => {
        const sp = model.spans.get(id);
        if (!sp) return;
        if (sp.kind === "llm") st.llm++;
        if (sp.kind === "tool" || sp.kind === "delegation") st.tools++;
        if (sp.status === "error") st.errors++;
        if (sp.status === "running") st.running = true;
        for (const a of model.artifactsBySpan.get(id) ?? []) if (a.kind === "diff" || a.kind === "code") st.edits++;
        if (sp.started_at != null) st.start = st.start == null ? sp.started_at : Math.min(st.start, sp.started_at);
        const e = sp.ended_at ?? (sp.status === "running" ? Infinity : sp.started_at ?? null);
        if (e != null) st.end = st.end == null ? e : Math.max(st.end, e);
        for (const c of model.children.get(id) ?? []) walk(c);
      };
      ids.forEach(walk);
      return st;
    };
    const out: Node[] = [];
    for (const n of view.nodes) {
      const p = placed.get(n.id);
      if (!p) continue;
      const base = {
        id: n.id,
        type: n.type,
        position: { x: p.x, y: p.y },
        parentId: n.parent,
        width: p.width,
        height: p.height,
        style: { width: p.width, height: p.height },
        draggable: false,
        selectable: false,
      };
      if (n.type === "segment" || n.type === "segment-open") {
        const g = view.segments.get(n.id)!;
        out.push({ ...base, data: { segment: g, stats: segStats(g.ids), flags: flagsFor(n.id, null) } });
      } else if (n.type === "task") {
        out.push({ ...base, data: { run: model.run, flags: flagsFor(n.id, null) } });
      } else if (n.type === "artifact") {
        const a = model.artifacts.get(n.id)!;
        const citations = (model.linksBySubject.get(a.id) ?? []).filter((l) => l.relation !== "derived_from" && l.relation !== "revises").length;
        out.push({ ...base, data: { artifact: a, flags: flagsFor(n.id, a.created_at), citations } });
      } else {
        const s = model.spans.get(n.id)!;
        const flags = flagsFor(n.id, null, s);
        if (n.type === "container") {
          out.push({ ...base, data: { span: s, flags, count: model.descendants.get(s.id) ?? 0, tokens: stats.get(s.id) ?? 0 } });
        } else {
          const att = view.attachments.get(s.id);
          let chipFlags: Record<string, "selected" | "upstream" | "downstream" | undefined> | undefined;
          if (att && hl) {
            chipFlags = {};
            for (const a of att) chipFlags[a] = a === selected ? "selected" : hl.upstream.has(a) ? "upstream" : hl.downstream.has(a) ? "downstream" : undefined;
          } else if (att && selected && att.includes(selected)) chipFlags = { [selected]: "selected" };
          out.push({
            ...base,
            data: {
              span: s,
              flags,
              retries: model.retries.get(s.id)?.length ?? 0,
              hidden: n.type === "collapsed" ? model.descendants.get(s.id) ?? 0 : undefined,
              attached: att?.map((id) => model.artifacts.get(id)!),
              chipFlags,
            },
          });
        }
      }
    }
    return out;
  }, [placed, view, model, hl, selected, replayT, stats]);

  const edges: Edge[] = useMemo(() => {
    if (!placed) return [];
    const visible = new Set(view.nodes.filter((n) => placed.has(n.id)).map((n) => n.id));
    const rep = (id: string) => view.rep.get(id) ?? id;
    const hlPairs = new Set<string>();
    const out: Edge[] = [];
    if (hl) {
      for (const e of hl.edges) {
        if (e.relation === "failed") continue; // shown by node highlight; an edge into its own container reads poorly
        const s = rep(e.source);
        const t = rep(e.target);
        if (s === t || !visible.has(s) || !visible.has(t)) continue;
        const k = `${s}>${t}`;
        if (hlPairs.has(k)) continue;
        hlPairs.add(k);
        const isDown = hl.downstream.has(e.target) && !hl.upstream.has(e.target);
        const color = isDown ? "#f5c451" : "#8b90ff";
        out.push({
          id: `hl:${k}`,
          source: s,
          target: t,
          animated: true,
          zIndex: 1000,
          style: { stroke: color, strokeWidth: 2.2 },
          markerEnd: { type: MarkerType.ArrowClosed, color, width: 16, height: 16 },
          // Labels only where they carry signal: contradictions always, everything else when the path is short.
          label:
            e.relation !== "produced" && e.relation !== "depends_on" && (relation(e.relation).tone === "contradict" || hl.edges.length <= 5)
              ? relation(e.relation).label.toLowerCase()
              : undefined,
          labelStyle: { fill: "#c7c9ff", fontSize: 10 },
          labelBgStyle: { fill: "#17181f" },
          labelBgPadding: [5, 2],
          labelBgBorderRadius: 6,
        });
      }
    }
    const future = (id: string) => {
      if (replayT == null) return false;
      const s = model.spans.get(id);
      if (s) return statusAt(s, replayT) === "future";
      const a = model.artifacts.get(id);
      return a?.created_at != null && a.created_at > replayT;
    };
    for (const e of view.edges) {
      if (!visible.has(e.source) || !visible.has(e.target)) continue;
      if (hlPairs.has(`${e.source}>${e.target}`)) continue;
      const style = { ...EDGE_STYLE[e.kind] };
      if (hl) style.opacity = 0.25;
      if (future(e.target)) style.opacity = 0.05;
      out.push({
        id: e.id,
        source: e.source,
        target: e.target,
        style,
        markerEnd: { type: MarkerType.ArrowClosed, color: style.stroke as string, width: 14, height: 14 },
        label: e.kind === "retry" ? "retry" : undefined,
        labelStyle: { fill: "#f5d38a", fontSize: 10 },
        labelBgStyle: { fill: "#1d1b16" },
        labelBgPadding: [4, 2],
        labelBgBorderRadius: 5,
      });
    }
    if (showEvidence && !hl) {
      const seen = new Set<string>();
      for (const l of model.links) {
        if (l.relation === "derived_from" || l.relation === "revises") continue;
        const s = rep(l.evidence.id);
        const t = rep(l.subject.id);
        if (s === t || !visible.has(s) || !visible.has(t)) continue;
        const k = `${s}>${t}`;
        if (seen.has(k)) continue;
        seen.add(k);
        const tone = relation(l.relation).tone;
        const color = tone === "contradict" ? "#ff8a65" : tone === "support" ? "#56d68a" : "#7d84a8";
        out.push({ id: `ev:${l.id}`, source: s, target: t, zIndex: 900, style: { stroke: color, strokeWidth: 1.3, strokeDasharray: "4 4", opacity: 0.75 } });
      }
    }
    return out;
  }, [placed, view, hl, replayT, showEvidence, model]);

  // Fit when a new run's first layout lands, and on explicit requests.
  useEffect(() => {
    if (!placed || layoutKey !== structKey) return;
    if (fittedRun.current !== runId + direction) {
      fittedRun.current = runId + direction;
      setTimeout(async () => {
        await rf.fitView({ padding: FIT_PADDING, duration: 0, maxZoom: 1.05 });
        // Long coding sessions don't fit legibly: open on the newest activity at a readable zoom instead.
        const coding = model.run.source === "claude-code" || model.run.source === "kiro";
        const target = selected ? view.rep.get(selected) ?? selected : newest;
        if (coding && rf.getZoom() < 0.5 && target) {
          const n = rf.getInternalNode(target);
          if (n) {
            const { x, y } = n.internals.positionAbsolute;
            rf.setCenter(x + (n.measured.width ?? 200) / 2, y + (n.measured.height ?? 60) / 2, { zoom: 0.85, duration: 0 });
          }
        }
      }, 80);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [placed, layoutKey, runId]);

  useEffect(() => {
    if (fitTick && placed) requestAnimationFrame(() => rf.fitView({ padding: FIT_PADDING, duration: 350, maxZoom: 1.05 }));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fitTick]);

  // Follow a live run: keep the most recently started visible step in view as the agent works.
  const follow = useStore((s) => s.follow);
  const live = model.run.status === "running" || runActivity(model.run) === "working";
  const newest = useMemo(() => {
    let best: { id: string; t: number } | null = null;
    for (const n of view.nodes) {
      if (n.type !== "span" && n.type !== "collapsed") continue;
      const sp = model.spans.get(n.id);
      const t = sp?.started_at ?? 0;
      if (!best || t >= best.t) best = { id: n.id, t };
    }
    return best?.id ?? null;
  }, [view, model]);
  useEffect(() => {
    if (!live || !follow || !newest || !placed || layoutKey !== structKey) return;
    const t = setTimeout(() => {
      const n = rf.getInternalNode(newest);
      if (!n) return;
      const { x, y } = n.internals.positionAbsolute;
      rf.setCenter(x + (n.measured.width ?? 200) / 2, y + (n.measured.height ?? 60) / 2, { zoom: Math.max(rf.getZoom(), 0.8), duration: 450 });
    }, 80);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [newest, live, follow, layoutKey]);

  useEffect(() => {
    if (!selected || !focusTick) return;
    const id = view.rep.get(selected) ?? selected;
    const t = setTimeout(() => {
      const n = rf.getInternalNode(id);
      if (!n) return;
      const { x, y } = n.internals.positionAbsolute;
      const w = n.measured.width ?? n.width ?? 200;
      const h = n.measured.height ?? n.height ?? 60;
      rf.setCenter(x + w / 2, y + Math.min(h, 200) / 2, { zoom: Math.max(rf.getZoom(), 0.85), duration: 380 });
    }, 60);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focusTick, layoutKey]);

  return (
    <ReactFlow
      nodes={nodes}
      edges={edges}
      nodeTypes={nodeTypes}
      onNodeClick={(_, n) => {
        if (n.type === "segment" || n.type === "segment-open") return; // segments toggle themselves
        select(n.id);
      }}
      onPaneClick={() => select(null)}
      onMoveStart={(e) => {
        // A user-initiated pan or zoom (not our own animation) pauses follow mode.
        if (e && live && useStore.getState().follow) useStore.getState().set("follow", false);
      }}
      nodesDraggable={false}
      nodesConnectable={false}
      elementsSelectable={false}
      minZoom={0.08}
      maxZoom={2.2}
      onlyRenderVisibleElements={nodes.length > 250}
      proOptions={{ hideAttribution: true }}
      colorMode="dark"
    >
      <Background variant={BackgroundVariant.Dots} gap={22} size={1.1} color="#24262d" />
      <MiniMap
        pannable
        zoomable
        nodeBorderRadius={6}
        nodeColor={(n) =>
          n.type === "container" ? "rgba(139,144,255,0.07)" : n.type === "artifact" ? "#8f7d5a" : n.type === "task" ? "#5b62e8" : (n.data as any)?.span?.status === "error" ? "#ff6b6b" : "#3d4150"
        }
        nodeStrokeColor={(n) => (n.type === "container" ? "#3a3d55" : "transparent")}
        style={{ width: 168, height: 112 }}
      />
      <Controls showInteractive={false} position="bottom-left" />
    </ReactFlow>
  );
}

function Filters() {
  const model = useStore((s) => s.model);
  const hidden = useStore((s) => s.hiddenKinds);
  const toggle = useStore((s) => s.toggleKind);
  const counts = useMemo(() => {
    const c = new Map<string, number>();
    if (!model) return c;
    for (const s of model.spans.values()) if (!(model.children.get(s.id)?.length)) c.set(s.kind, (c.get(s.kind) ?? 0) + 1);
    return c;
  }, [model]);
  if (!model) return null;
  const kinds = Object.keys(SPAN_KINDS).filter((k) => counts.get(k));
  return (
    <div className="chips">
      {kinds.map((k) => (
        <span key={k} className={`chip ${hidden.has(k) ? "off" : ""}`} onClick={() => toggle(k)} title={`Show/hide ${SPAN_KINDS[k].label.toLowerCase()} steps`}>
          <span className="dot" style={{ ["--c" as any]: SPAN_KINDS[k].color }} />
          {SPAN_KINDS[k].label}
          <span className="n">{counts.get(k)}</span>
        </span>
      ))}
      {model.artifacts.size > 0 && (
        <span className={`chip ${hidden.has("artifact") ? "off" : ""}`} onClick={() => toggle("artifact")}>
          <span className="dot" style={{ ["--c" as any]: "var(--k-artifact)" }} />
          Artifacts
          <span className="n">{model.artifacts.size}</span>
        </span>
      )}
    </div>
  );
}

export function Canvas() {
  const model = useStore((s) => s.model);
  const showEvidence = useStore((s) => s.showEvidence);
  const set = useStore((s) => s.set);
  const expandAll = useStore((s) => s.expandAll);
  const collapseAll = useStore((s) => s.collapseAll);
  const fitTick = useStore((s) => s.fitTick);
  const direction = useStore((s) => s.direction);
  const follow = useStore((s) => s.follow);
  if (!model) return null;
  const live = model.run.status === "running" || runActivity(model.run) === "working";
  return (
    <div className="canvas">
      <div className="canvas-toolbar">
        <Filters />
        <div className="grow" />
        {live && (
          <div className="toolbar-group">
            <button className={`btn ${follow ? "on" : "ghost"}`} style={{ height: 26 }} title="Keep the agent's newest step in view" onClick={() => set("follow", !follow)}>
              <span className={`dot ${follow ? "live" : ""}`} style={{ ["--c" as any]: follow ? "var(--s-running)" : "var(--muted)", width: 6, height: 6 }} />
              {follow ? "Following" : "Follow"}
            </button>
          </div>
        )}
        <div className="toolbar-group">
          <button className={`icon-btn ${showEvidence ? "on" : ""}`} title="Show evidence links (v)" onClick={() => set("showEvidence", !showEvidence)}>
            <Route size={15} />
          </button>
          <button
            className="icon-btn"
            title={direction === "DOWN" ? "Lay out left → right" : "Lay out top → bottom"}
            onClick={() => {
              const d = direction === "DOWN" ? "RIGHT" : "DOWN";
              localStorage.setItem("tg.direction", d);
              set("direction", d);
            }}
          >
            {direction === "DOWN" ? <ArrowRightLeft size={15} /> : <ArrowUpDown size={15} />}
          </button>
          <button className="icon-btn" title="Expand all" onClick={expandAll}>
            <ChevronsUpDown size={15} />
          </button>
          <button className="icon-btn" title="Collapse subagents" onClick={collapseAll}>
            <ChevronsDownUp size={15} />
          </button>
          <button className="icon-btn" title="Fit to screen (f)" onClick={() => set("fitTick", fitTick + 1)}>
            <Maximize size={15} />
          </button>
        </div>
      </div>
      <ReactFlowProvider>
        <Flow />
      </ReactFlowProvider>
    </div>
  );
}

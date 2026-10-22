import { Handle, Position, type NodeProps } from "@xyflow/react";
import { ChevronDown, ChevronRight, Flag, RotateCw, Star } from "lucide-react";
import { memo } from "react";
import { fmtDuration, fmtTokens, hueFor, preview } from "../model/format";
import { artifactIcon, spanKind, statusStyle } from "../model/kinds";
import { MAX_CHIPS, type Segment } from "../model/layout";
import { useStore } from "../store";
import { isEstimated, type Artifact, type Run, type Span } from "../types";

export interface Flags {
  selected?: boolean;
  upstream?: boolean;
  downstream?: boolean;
  dimmed?: boolean;
  future?: boolean;
  runningAt?: boolean;
}

function cls(base: string, f: Flags, status?: string) {
  const out = [base];
  if (f.selected) out.push("selected");
  else if (f.upstream) out.push("upstream");
  else if (f.downstream) out.push("downstream");
  if (f.dimmed) out.push("dimmed");
  if (f.future) out.push("future");
  const st = f.runningAt ? "running" : f.future ? undefined : status;
  if (st === "error" || st === "waiting" || st === "running") out.push(st);
  return out.join(" ");
}

function Handles() {
  const vertical = useStore((s) => s.direction) === "DOWN";
  return (
    <>
      <Handle type="target" position={vertical ? Position.Top : Position.Left} isConnectable={false} />
      <Handle type="source" position={vertical ? Position.Bottom : Position.Right} isConnectable={false} />
    </>
  );
}
const handles = <Handles />;

/** "query: agents hallucinate" — the most telling argument of a call, in one line. */
function argPreview(v: unknown): string {
  if (typeof v === "string") return v;
  if (v && typeof v === "object" && !Array.isArray(v)) {
    for (const [k, x] of Object.entries(v as Record<string, unknown>)) {
      if (typeof x === "string" && x.length) return `${k}: ${x}`;
      if (typeof x === "number") return `${k}: ${x}`;
    }
  }
  return "";
}

function llmGist(out: unknown): string {
  const content = (out as any)?.content ?? (Array.isArray(out) ? out : null);
  if (typeof out === "string") return out.replace(/\s+/g, " ").trim();
  if (!Array.isArray(content)) return "";
  const text = content.find((b: any) => b?.type === "text" && typeof b.text === "string" && b.text.trim());
  const tools = content.filter((b: any) => b?.type === "tool_use" || b?.type === "toolUse").map((b: any) => b.name);
  if (text) return text.text.replace(/\s+/g, " ").trim();
  if (tools.length) return "→ " + [...new Set(tools)].join(", ");
  return "";
}

const PREFERRED_ARGS = ["file_path", "path", "pattern", "query", "url", "command", "description", "prompt"];

export function subtitle(s: Span, attached?: number): string {
  // Coding agents state why they call a tool (Kiro: purpose; Claude Code Bash: description) — the best one-liner.
  const why = s.attributes?.["purpose"] ?? (s.input as any)?.description;
  if ((s.kind === "tool" || s.kind === "delegation") && typeof why === "string" && why) return why;
  if (s.kind === "llm") {
    // When the step is already named after its model, say what the model did instead.
    if (s.name === s.model || !s.model) {
      const said = llmGist(s.output);
      if (said) return said;
    }
    return s.model ?? "model";
  }
  if ((s.kind === "tool" || s.kind === "retrieval") && s.input && typeof s.input === "object" && !Array.isArray(s.input)) {
    const inp = s.input as Record<string, unknown>;
    for (const k of PREFERRED_ARGS) if (typeof inp[k] === "string" && inp[k]) return String(inp[k]).split("\n")[0];
  }
  if (s.kind === "retrieval" && attached) return `${attached} documents`;
  if (s.kind === "tool" || s.kind === "retrieval") return argPreview(s.input) || spanKind(s.kind).label;
  if (s.kind === "approval") return String(s.attributes?.["decision"] ?? s.attributes?.["reviewer"] ?? "human gate");
  return s.agent && s.agent !== s.name ? s.agent : spanKind(s.kind).label;
}

export interface SpanNodeData extends Record<string, unknown> {
  span: Span;
  flags: Flags;
  retries: number;
  hidden?: number;
  tokens?: number;
  attached?: Artifact[];
  chipFlags?: Record<string, "selected" | "upstream" | "downstream" | undefined>;
}

function Chips({ items, chipFlags }: { items: Artifact[]; chipFlags?: SpanNodeData["chipFlags"] }) {
  const select = useStore((x) => x.select);
  const shown = items.slice(0, MAX_CHIPS);
  return (
    <div className="att-list">
      {shown.map((a) => {
        const Icon = artifactIcon(a.kind);
        return (
          <div
            key={a.id}
            className={`att ${chipFlags?.[a.id] ?? ""}`}
            title={a.name}
            onClick={(e) => {
              e.stopPropagation();
              select(a.id);
            }}
          >
            <Icon size={11} />
            <span className="ellipsis">{a.name}</span>
          </div>
        );
      })}
      {items.length > MAX_CHIPS && <div className="att-more">+{items.length - MAX_CHIPS} more artifacts</div>}
    </div>
  );
}

export const SpanNode = memo(function SpanNode({ data }: NodeProps) {
  const { span: s, flags, retries, hidden, attached, chipFlags } = data as SpanNodeData;
  const k = spanKind(s.kind);
  const st = statusStyle(s.status);
  const Icon = k.icon;
  const dur = s.ended_at != null && s.started_at != null ? s.ended_at - s.started_at : null;
  const collapsed = hidden != null;
  return (
    <div
      className={cls("tg-node" + (collapsed ? " collapsed-node" : ""), flags, s.status)}
      style={{ ["--c" as any]: k.color }}
      data-id={s.id}
    >
      {handles}
      <div className="stripe" />
      <div className="body">
        <div className="title">
          <Icon size={14} className="icon" />
          <span className="label">{s.name}</span>
          {s.status === "error" && !flags.future && <st.icon size={13} color={st.color} />}
          {s.status === "waiting" && !flags.future && <st.icon size={13} color={st.color} />}
        </div>
        <div className="sub">
          <span className="ellipsis">{subtitle(s, attached?.length)}</span>
          {(s.attempt ?? 1) > 1 && (
            <span className="badge retry" title="Retry attempt">
              <RotateCw size={9} /> {s.attempt}
            </span>
          )}
          {retries > 0 && s.status === "error" && <span className="badge err">retried</span>}
          {collapsed && <span className="badge">+{hidden}</span>}
          {s.usage?.total_tokens ? <span className="badge">{fmtTokens(s.usage.total_tokens)} tok</span> : null}
          <span className={`dur ${isEstimated(s) ? "est" : ""}`} title={isEstimated(s) ? "Estimated — this source only timestamps whole turns" : undefined}>
            {s.status === "running" ? "…" : (isEstimated(s) ? "~" : "") + fmtDuration(dur)}
          </span>
        </div>
        {attached?.length ? <Chips items={attached} chipFlags={chipFlags} /> : null}
      </div>
    </div>
  );
});

export interface GroupNodeData extends Record<string, unknown> {
  span: Span;
  flags: Flags;
  count: number;
  tokens: number;
}

export const GroupNode = memo(function GroupNode({ data, id }: NodeProps) {
  const { span: s, flags, count, tokens } = data as GroupNodeData;
  const k = spanKind(s.kind);
  const st = statusStyle(s.status);
  const toggle = useStore((x) => x.toggleCollapsed);
  const Icon = k.icon;
  const color = s.kind === "agent" ? `hsl(${hueFor(s.agent ?? s.name)} 80% 74%)` : k.color;
  const dur = s.ended_at != null && s.started_at != null ? s.ended_at - s.started_at : null;
  return (
    <div className={cls("group-node", flags, s.status)} style={{ ["--c" as any]: color }} data-id={s.id}>
      {handles}
      <div className="group-head">
        <button
          className="icon-btn"
          style={{ width: 20, height: 20, flex: "none" }}
          title="Collapse (Enter)"
          onClick={(e) => {
            e.stopPropagation();
            toggle(id);
          }}
        >
          <ChevronDown size={14} />
        </button>
        <div className="avatar">
          <Icon size={14} />
        </div>
        <div className="grow" style={{ minWidth: 0 }}>
          <div className="row" style={{ gap: 6 }}>
            <span className="name">{s.name}</span>
            {s.status === "running" ? (
              <st.icon size={12} className="spin" color={st.color} style={{ flex: "none" }} />
            ) : s.status === "error" || s.status === "waiting" ? (
              <st.icon size={12} color={st.color} style={{ flex: "none" }} />
            ) : null}
          </div>
          <div className="sub ellipsis">
            {s.model ?? k.label} · {count} steps{tokens ? ` · ${fmtTokens(tokens)} tok` : ""} · {s.status === "running" ? "running" : fmtDuration(dur)}
          </div>
        </div>
      </div>
    </div>
  );
});

export const CollapsedNode = memo(function CollapsedNode(props: NodeProps) {
  const toggle = useStore((x) => x.toggleCollapsed);
  return (
    <div style={{ width: "100%", height: "100%", position: "relative" }}>
      <SpanNode {...props} />
      <button
        className="icon-btn"
        title="Expand (Enter)"
        style={{ position: "absolute", top: -9, right: -9, width: 20, height: 20, background: "var(--elev-2)", border: "1px solid var(--line-2)" }}
        onClick={(e) => {
          e.stopPropagation();
          toggle(props.id);
        }}
      >
        <ChevronRight size={12} />
      </button>
    </div>
  );
});

export interface ArtifactNodeData extends Record<string, unknown> {
  artifact: Artifact;
  flags: Flags;
  citations: number;
}

export const ArtifactNode = memo(function ArtifactNode({ data }: NodeProps) {
  const { artifact: a, flags, citations } = data as ArtifactNodeData;
  const Icon = a.final ? Star : artifactIcon(a.kind);
  return (
    <div className={cls("tg-node artifact-node" + (a.final ? " final" : ""), flags)} style={{ ["--c" as any]: "var(--k-artifact)" }} data-id={a.id}>
      {handles}
      <div className="body" style={{ paddingLeft: 11 }}>
        <div className="title">
          <Icon size={14} className="icon" />
          <span className="label">{a.name}</span>
        </div>
        <div className="sub">
          <span>{a.final ? "final answer" : a.kind}</span>
          {citations > 0 && <span className="badge">{citations} cited</span>}
          {a.review?.status === "approved" && <span className="badge" style={{ color: "var(--s-ok)" }}>approved</span>}
        </div>
      </div>
    </div>
  );
});

export interface TaskNodeData extends Record<string, unknown> {
  run: Run;
  flags: Flags;
}

export const TaskNode = memo(function TaskNode({ data }: NodeProps) {
  const { run, flags } = data as TaskNodeData;
  const text = preview(run.input, 140) || run.name;
  return (
    <div className={cls("tg-node task-node", flags)} data-id="__task__">
      {handles}
      <div className="body" style={{ paddingLeft: 12 }}>
        <div className="title">
          <Flag size={14} className="icon" />
          <span className="label">Task</span>
        </div>
        <div className="sub">{text}</div>
      </div>
    </div>
  );
});



export interface SegmentStats {
  llm: number;
  tools: number;
  errors: number;
  edits: number;
  running: boolean;
  start: number | null;
  end: number | null;
}

export interface SegmentNodeData extends Record<string, unknown> {
  segment: Segment;
  stats: SegmentStats;
  flags: Flags;
}

function segLine(st: SegmentStats) {
  const parts = [`${st.llm} model calls`, `${st.tools} tools`];
  if (st.edits) parts.push(`${st.edits} edits`);
  return parts.join(" · ");
}

/** A folded page of a long step sequence — a table of contents for long sessions. */
export const SegmentNode = memo(function SegmentNode({ data }: NodeProps) {
  const { segment: g, stats, flags } = data as SegmentNodeData;
  const toggle = useStore((x) => x.toggleSegment);
  const dur = stats.start != null && stats.end != null && isFinite(stats.end) ? stats.end - stats.start : null;
  return (
    <div
      className={cls("tg-node segment-node collapsed-node", flags, stats.running ? "running" : undefined)}
      style={{ ["--c" as any]: "var(--k-step)" }}
      onClick={(e) => {
        e.stopPropagation();
        toggle(g.id, g.isLast);
      }}
      title="Expand this page of steps"
    >
      {handles}
      <div className="stripe" />
      <div className="body">
        <div className="title">
          <ChevronRight size={14} className="icon" />
          <span className="label">
            Steps {g.from}–{g.to}
          </span>
          {stats.errors > 0 && <span className="badge err">{stats.errors} failed</span>}
        </div>
        <div className="sub">
          <span className="ellipsis">{segLine(stats)}</span>
          <span className="dur">{stats.running ? "…" : fmtDuration(dur)}</span>
        </div>
      </div>
    </div>
  );
});

export const SegmentGroupNode = memo(function SegmentGroupNode({ data }: NodeProps) {
  const { segment: g, stats, flags } = data as SegmentNodeData;
  const toggle = useStore((x) => x.toggleSegment);
  return (
    <div className={cls("group-node segment-group", flags)} style={{ ["--c" as any]: "var(--k-step)" }}>
      {handles}
      <div
        className="group-head"
        onClick={(e) => {
          e.stopPropagation();
          toggle(g.id, g.isLast);
        }}
        title="Fold this page of steps"
      >
        <ChevronDown size={14} color="var(--muted)" />
        <div className="grow" style={{ minWidth: 0 }}>
          <div className="name">
            Steps {g.from}–{g.to}
          </div>
          <div className="sub ellipsis">
            {segLine(stats)}
            {stats.errors ? ` · ${stats.errors} failed` : ""}
          </div>
        </div>
      </div>
    </div>
  );
});

export const nodeTypes = {
  segment: SegmentNode,
  "segment-open": SegmentGroupNode,
  span: SpanNode,
  container: GroupNode,
  collapsed: CollapsedNode,
  artifact: ArtifactNode,
  task: TaskNode,
};

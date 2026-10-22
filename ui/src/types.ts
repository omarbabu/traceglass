// Mirrors the canonical Traceglass schema (schema/traceglass.schema.json, v1).

export type SpanKind =
  | "agent" | "llm" | "tool" | "retrieval" | "delegation" | "approval" | "guardrail" | "embedding" | "step";
export type Status = "running" | "ok" | "error" | "cancelled" | "skipped" | "waiting";

export interface Usage {
  input_tokens?: number;
  output_tokens?: number;
  total_tokens?: number;
  cache_read_tokens?: number;
  cache_write_tokens?: number;
  cost_usd?: number;
}

export interface ErrorInfo {
  type?: string;
  message?: string;
  stack?: string | null;
}

export interface RunSummaryStats {
  spans?: number;
  errors?: number;
  artifacts?: number;
  tokens?: number;
  cost_usd?: number | null;
}

export interface Run {
  type?: "run";
  id: string;
  name: string;
  project?: string | null;
  session?: string | null;
  task?: string | null;
  status?: Status;
  started_at?: number | null;
  ended_at?: number | null;
  input?: unknown;
  output?: unknown;
  tags?: string[];
  metadata?: Record<string, unknown>;
  source?: string;
  final_artifact_id?: string;
  summary?: RunSummaryStats;
}

export interface Span {
  type?: "span";
  id: string;
  run_id: string;
  parent_id?: string | null;
  kind: SpanKind | string;
  name: string;
  status: Status;
  started_at?: number | null;
  ended_at?: number | null;
  seq?: number | null;
  agent?: string;
  model?: string;
  usage?: Usage;
  input?: unknown;
  output?: unknown;
  error?: ErrorInfo;
  attributes?: Record<string, unknown>;
  depends_on?: string[];
  retry_of?: string;
  attempt?: number;
}

export interface TraceEvent {
  type?: "event";
  id: string;
  run_id: string;
  span_id?: string | null;
  kind: string;
  name: string;
  level?: string;
  ts?: number | null;
  message?: string;
  data?: unknown;
}

export interface Review {
  status: "approved" | "rejected" | "needs_changes" | "unreviewed" | string;
  by?: string | null;
  note?: string | null;
  at?: number | null;
}

export interface Artifact {
  type?: "artifact";
  id: string;
  run_id: string;
  span_id?: string | null;
  kind: string;
  name: string;
  mime?: string;
  content?: unknown;
  uri?: string;
  size?: number;
  created_at?: number | null;
  final?: boolean;
  previous_id?: string;
  metadata?: Record<string, unknown>;
  review?: Review;
}

export interface Anchor {
  start?: number;
  end?: number;
  text?: string;
}

export interface Ref {
  kind: "span" | "artifact";
  id: string;
  anchor?: Anchor;
}

export interface Link {
  type?: "link";
  id: string;
  run_id: string;
  subject: Ref;
  evidence: Ref;
  relation: string;
  note?: string;
  confidence?: number;
  declared_by?: string;
  created_at?: number;
}

export interface Bundle {
  schema?: string;
  run: Run;
  spans: Span[];
  events: TraceEvent[];
  artifacts: Artifact[];
  links: Link[];
}

export interface SearchHit {
  kind: "run" | "span" | "artifact";
  id: string;
  run_id: string;
  name: string;
  detail?: string;
  status?: string;
}

export interface Change {
  seq: number;
  type: "run" | "span" | "event" | "artifact" | "link" | "delete" | "remove";
  run_id: string;
  /** For "remove": `{id, type}` of the record that no longer exists. */
  record: Run | Span | TraceEvent | Artifact | Link | { id: string; type: string } | null;
}

/** Values the SDK substitutes for payloads it could not, or chose not to, record. */
export interface Marker {
  $tg: "not_captured" | "redacted" | "truncated" | "bytes" | "repr" | string;
  reason?: string;
  preview?: string;
  length?: number;
  size?: number;
  type?: string;
  repr?: string;
  omitted_items?: number;
}

export function isMarker(v: unknown): v is Marker {
  return typeof v === "object" && v !== null && !Array.isArray(v) && "$tg" in (v as object);
}

/** How a coding-agent session is doing right now (set by the Claude Code / Kiro importers). */
export type Activity = "working" | "idle" | "interrupted" | "ended";

export function runActivity(r: Run | undefined): Activity | undefined {
  const a = r?.metadata?.["activity"];
  return a === "working" || a === "idle" || a === "interrupted" || a === "ended" ? a : undefined;
}

export function isEstimated(s: { attributes?: Record<string, unknown> }): boolean {
  return s.attributes?.["timing"] === "estimated";
}

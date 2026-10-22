import type { GraphModel } from "./graph";

/** What a run cost and how it went — the raw material for comparing harnesses. */
export interface RunMetrics {
  wallMs: number;
  modelCalls: number;
  toolCalls: number;
  failedTools: number;
  retries: number;
  inputTokens: number;
  outputTokens: number;
  cacheReadTokens: number;
  totalTokens: number;
  costUsd: number | null;
  files: string[];
  turns: number;
  steps: number;
  models: string[];
  finalText: string;
}

export function runMetrics(m: GraphModel): RunMetrics {
  let modelCalls = 0;
  let toolCalls = 0;
  let failedTools = 0;
  let retries = 0;
  let inputTokens = 0;
  let outputTokens = 0;
  let cacheReadTokens = 0;
  let costUsd: number | null = null;
  const models = new Set<string>();
  for (const s of m.spans.values()) {
    if (s.kind === "llm") modelCalls++;
    if (s.kind === "tool" || s.kind === "delegation") {
      toolCalls++;
      if (s.status === "error") failedTools++;
    }
    if ((s.attempt ?? 1) > 1 || s.retry_of) retries++;
    if (s.model) models.add(s.model);
    const u = s.usage;
    if (u) {
      inputTokens += u.input_tokens ?? 0;
      outputTokens += u.output_tokens ?? 0;
      cacheReadTokens += u.cache_read_tokens ?? 0;
      if (u.cost_usd != null) costUsd = (costUsd ?? 0) + u.cost_usd;
    }
  }
  const files = new Set<string>();
  const cwd = String(m.run.metadata?.["cwd"] ?? "").replace(/\/$/, "");
  for (const a of m.artifacts.values())
    if (a.kind === "diff" || a.kind === "code") {
      const p = String(a.metadata?.["path"] ?? a.name);
      files.add(cwd && p.startsWith(cwd + "/") ? p.slice(cwd.length + 1) : p);
    }
  const run = m.run;
  // Some agent tools report cost only for the whole run (e.g. Claude Code's result JSON).
  const runCost = run.metadata?.["cost_usd"];
  if (costUsd == null && typeof runCost === "number") costUsd = runCost;
  const wallMs = run.ended_at && run.started_at ? run.ended_at - run.started_at : m.t1 - m.t0;
  const fin = m.finalArtifact;
  return {
    wallMs,
    modelCalls,
    toolCalls,
    failedTools,
    retries,
    inputTokens,
    outputTokens,
    cacheReadTokens,
    totalTokens: inputTokens + outputTokens,
    costUsd,
    files: [...files].sort(),
    turns: (m.children.get("__root__") ?? []).length,
    steps: m.spans.size,
    models: [...models],
    finalText: typeof fin?.content === "string" ? fin.content : "",
  };
}

/** "38% less" style delta of b relative to a; null when either side is zero/undefined. */
export function relDelta(a: number, b: number): number | null {
  if (!isFinite(a) || !isFinite(b) || a === 0) return null;
  return (b - a) / a;
}

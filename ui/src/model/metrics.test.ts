import { describe, expect, it } from "vitest";
import type { Bundle } from "../types";
import { buildModel } from "./graph";
import { relDelta, runMetrics } from "./metrics";

const bundle = (over: Partial<Bundle> = {}): Bundle => ({
  run: { id: "r", name: "r", started_at: 0, ended_at: 10_000, metadata: { cwd: "~/code/app", cost_usd: 0.12 } },
  spans: [
    { id: "t", run_id: "r", kind: "step", name: "turn", status: "ok", started_at: 0, ended_at: 10_000 },
    { id: "l1", run_id: "r", parent_id: "t", kind: "llm", name: "m", model: "claude-sonnet-5-5", status: "ok", started_at: 0, ended_at: 2000, usage: { input_tokens: 10, output_tokens: 90, cache_read_tokens: 500 } },
    { id: "x1", run_id: "r", parent_id: "t", kind: "tool", name: "Edit", status: "ok", started_at: 2000, ended_at: 2500 },
    { id: "x2", run_id: "r", parent_id: "t", kind: "tool", name: "Bash", status: "error", started_at: 3000, ended_at: 4000 },
    { id: "x3", run_id: "r", parent_id: "t", kind: "tool", name: "Bash", status: "ok", started_at: 4000, ended_at: 5000, attempt: 2, retry_of: "x2" },
  ],
  events: [],
  artifacts: [
    { id: "a1", run_id: "r", span_id: "x1", kind: "diff", name: "edit", metadata: { path: "~/code/app/src/x.py" } },
    { id: "a2", run_id: "r", span_id: "x1", kind: "diff", name: "edit", metadata: { path: "~/code/app/src/x.py" } },
    { id: "f", run_id: "r", kind: "markdown", name: "Final", final: true, content: "done" },
  ],
  links: [],
  ...over,
});

describe("runMetrics", () => {
  it("counts calls, failures, retries, tokens and distinct files relative to cwd", () => {
    const m = runMetrics(buildModel(bundle()));
    expect(m).toMatchObject({ wallMs: 10_000, modelCalls: 1, toolCalls: 3, failedTools: 1, retries: 1, inputTokens: 10, outputTokens: 90, totalTokens: 100, cacheReadTokens: 500 });
    expect(m.files).toEqual(["src/x.py"]);
    expect(m.models).toEqual(["claude-sonnet-5-5"]);
    expect(m.finalText).toBe("done");
  });

  it("falls back to run-level cost only when spans carry none", () => {
    expect(runMetrics(buildModel(bundle())).costUsd).toBe(0.12);
    const withSpanCost = bundle();
    withSpanCost.spans[1].usage!.cost_usd = 0.5;
    expect(runMetrics(buildModel(withSpanCost)).costUsd).toBe(0.5);
  });

  it("relDelta is relative to the first value", () => {
    expect(relDelta(100, 50)).toBe(-0.5);
    expect(relDelta(0, 5)).toBeNull();
  });
});

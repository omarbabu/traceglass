import type { Activity, Run } from "../types";

export interface SourceStyle {
  label: string;
  short: string;
  color: string;
}

/** Where a run came from. Text badges (no third-party logos), one stable colour each. */
export const SOURCES: Record<string, SourceStyle> = {
  "claude-code": { label: "Claude Code", short: "CC", color: "#e08a62" },
  kiro: { label: "Kiro", short: "KI", color: "#a98bff" },
  otlp: { label: "OpenTelemetry", short: "OT", color: "#f2b64c" },
  sdk: { label: "SDK", short: "SDK", color: "#8a8f9c" },
};

export function sourceOf(r: Run | undefined): string {
  const s = r?.source || "sdk";
  return s === "langchain" ? "sdk" : s;
}

export function sourceStyle(src: string): SourceStyle {
  return SOURCES[src] ?? { label: src, short: src.slice(0, 3).toUpperCase(), color: "#8a8f9c" };
}

export const ACTIVITY: Record<Activity, { label: string; color: string; hint: string }> = {
  working: { label: "Working", color: "var(--s-running)", hint: "The agent is in the middle of a turn" },
  idle: { label: "Idle", color: "var(--s-ok)", hint: "Turn finished — waiting for the next prompt" },
  interrupted: { label: "Interrupted", color: "var(--s-waiting)", hint: "The last turn stopped mid-way and the session went quiet" },
  ended: { label: "Ended", color: "var(--s-muted)", hint: "The session was closed" },
};

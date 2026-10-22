export function fmtDuration(ms: number | null | undefined): string {
  if (ms == null || !isFinite(ms)) return "—";
  if (ms === 0) return "0";
  if (ms < 1) return "<1ms";
  if (ms < 1000) return `${Math.round(ms)}ms`;
  const s = ms / 1000;
  if (s < 10) return `${s.toFixed(2)}s`;
  if (s < 60) return `${s.toFixed(1)}s`;
  const m = Math.floor(s / 60);
  const rem = Math.round(s % 60);
  if (m < 60) return `${m}m ${rem.toString().padStart(2, "0")}s`;
  return `${Math.floor(m / 60)}h ${(m % 60).toString().padStart(2, "0")}m`;
}

export function fmtTokens(n: number | null | undefined): string {
  if (!n) return "0";
  if (n < 1000) return `${n}`;
  if (n < 1_000_000) return `${(n / 1000).toFixed(n < 10_000 ? 1 : 0)}k`;
  return `${(n / 1_000_000).toFixed(2)}M`;
}

export function fmtCost(usd: number | null | undefined): string {
  if (usd == null) return "—";
  if (usd === 0) return "$0";
  if (usd < 0.01) return `$${usd.toFixed(4)}`;
  if (usd < 10) return `$${usd.toFixed(3)}`;
  return `$${usd.toFixed(2)}`;
}

export function fmtRelative(ts: number | null | undefined, now = Date.now()): string {
  if (!ts) return "";
  const d = (now - ts) / 1000;
  if (d < 45) return "just now";
  if (d < 3600) return `${Math.round(d / 60)}m ago`;
  if (d < 86400) return `${Math.round(d / 3600)}h ago`;
  if (d < 86400 * 7) return `${Math.round(d / 86400)}d ago`;
  return new Date(ts).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

export function fmtClock(ts: number | null | undefined): string {
  if (!ts) return "—";
  return new Date(ts).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

export function fmtOffset(ms: number): string {
  const sign = ms < 0 ? "-" : "+";
  return sign + fmtDuration(Math.abs(ms));
}

export function truncate(s: string, n: number): string {
  return s.length > n ? s.slice(0, n - 1).trimEnd() + "…" : s;
}

export function preview(v: unknown, n = 120): string {
  if (v == null) return "";
  if (typeof v === "string") return truncate(v.replace(/\s+/g, " ").trim(), n);
  if (typeof v === "object") {
    const o = v as Record<string, unknown>;
    for (const k of ["question", "query", "task", "prompt", "request", "goal", "objective", "message", "input", "text", "content"]) {
      if (typeof o[k] === "string") return truncate((o[k] as string).replace(/\s+/g, " "), n);
    }
    const first = Object.values(o).find((x) => typeof x === "string" && x.length > 3);
    if (typeof first === "string") return truncate(first.replace(/\s+/g, " "), n);
  }
  try {
    return truncate(JSON.stringify(v), n);
  } catch {
    return "";
  }
}

/** Stable hue for an agent name, so an agent looks the same everywhere it appears. */
export function hueFor(name: string): number {
  let h = 2166136261;
  for (let i = 0; i < name.length; i++) {
    h ^= name.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  const palette = [258, 222, 190, 160, 330, 290, 30, 205, 140, 0, 45, 275];
  return palette[Math.abs(h) % palette.length];
}

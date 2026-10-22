/**
 * Turning arbitrary values into honest, JSON-safe payloads — a port of capture.py.
 *
 * Absent ≠ not captured ≠ empty: transformations leave a `$tg` marker so the UI can say
 * exactly what happened (`not_captured`, `redacted`, `truncated`, `bytes`, `repr`).
 */

export const MARKER = "$tg";

export interface Marker {
  $tg: string;
  [k: string]: unknown;
}

export function notCaptured(reason?: string): Marker {
  return reason ? { $tg: "not_captured", reason } : { $tg: "not_captured" };
}

export function isMarker(v: unknown, kind?: string): v is Marker {
  return typeof v === "object" && v !== null && !Array.isArray(v) && MARKER in v && (kind === undefined || (v as Marker).$tg === kind);
}

export const DEFAULT_SECRET_KEYS: string[] = [
  "api[-_]?key",
  "secret",
  "passw(or)?d",
  "passwd",
  "(access|auth|bearer|refresh|id|session)[-_]?token",
  "^token$",
  "authorization",
  "cookie",
  "private[-_]?key",
  "access[-_]?key",
  "credentials?",
];

export const DEFAULT_SECRET_PATTERNS: [name: string, pattern: RegExp][] = [
  ["anthropic-key", /sk-ant-[A-Za-z0-9_-]{20,}/g],
  ["openai-key", /sk-(proj-)?[A-Za-z0-9_-]{20,}/g],
  ["aws-access-key", /\b(AKIA|ASIA)[0-9A-Z]{16}\b/g],
  ["github-token", /\bgh[pousr]_[A-Za-z0-9]{30,}\b/g],
  ["slack-token", /\bxox[abprs]-[A-Za-z0-9-]{10,}\b/g],
  ["bearer-token", /\bbearer\s+[A-Za-z0-9\-._~+/]{16,}=*/gi],
  ["private-key", /-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----/g],
];

export interface RedactorOptions {
  keys?: string[];
  patterns?: [string, RegExp | string][];
  /** Start from the defaults (true) or from nothing (false). */
  defaults?: boolean;
}

/** Redacts secrets by object key name and by value pattern. */
export class Redactor {
  private keyRe: RegExp | null;
  private patterns: [string, RegExp][];

  constructor(opts: RedactorOptions = {}) {
    const defaults = opts.defaults ?? true;
    const keys = [...(defaults ? DEFAULT_SECRET_KEYS : []), ...(opts.keys ?? [])];
    this.keyRe = keys.length ? new RegExp(keys.map((k) => `(?:${k})`).join("|"), "i") : null;
    this.patterns = [
      ...(defaults ? DEFAULT_SECRET_PATTERNS : []),
      ...(opts.patterns ?? []).map(([n, p]): [string, RegExp] => [n, typeof p === "string" ? new RegExp(p, "g") : p]),
    ].map(([n, p]): [string, RegExp] => [n, p.global ? p : new RegExp(p.source, p.flags + "g")]);
  }

  keyIsSecret(key: string): boolean {
    return !!this.keyRe && this.keyRe.test(key);
  }

  scrub(text: string): string {
    for (const [name, pat] of this.patterns) {
      pat.lastIndex = 0;
      text = text.replace(pat, `‹redacted:${name}›`);
    }
    return text;
  }
}

/** A redactor that redacts nothing. */
export const NO_REDACTION = new Redactor({ defaults: false });

export interface CaptureConfig {
  maxString: number;
  maxItems: number;
  maxDepth: number;
  redactor: Redactor;
}

export function defaultCaptureConfig(redactor: Redactor = new Redactor()): CaptureConfig {
  return { maxString: 32_000, maxItems: 500, maxDepth: 12, redactor };
}

export function capture(value: unknown, cfg: CaptureConfig = defaultCaptureConfig()): unknown {
  return cap(value, cfg, 0, new WeakSet());
}

function safeRepr(v: unknown): string {
  try {
    return String(v);
  } catch {
    return "<unrepresentable>";
  }
}

function cap(v: unknown, cfg: CaptureConfig, depth: number, seen: WeakSet<object>): unknown {
  if (v === null || v === undefined) return null;
  if (typeof v === "boolean") return v;
  if (typeof v === "number") return Number.isFinite(v) ? v : String(v);
  if (typeof v === "bigint") return v.toString();
  if (typeof v === "string") {
    const s = cfg.redactor.scrub(v);
    return s.length > cfg.maxString ? { $tg: "truncated", preview: s.slice(0, cfg.maxString), length: s.length } : s;
  }
  if (typeof v === "function" || typeof v === "symbol")
    return { $tg: "repr", type: typeof v, repr: cfg.redactor.scrub(safeRepr(v)).slice(0, 2000) };
  if (isMarker(v)) return v;
  const obj = v as object;
  if (depth >= cfg.maxDepth) return { $tg: "truncated", reason: "max_depth", repr: safeRepr(v).slice(0, 200) };
  if (seen.has(obj)) return { $tg: "truncated", reason: "cycle" };
  if (obj instanceof Date) return Number.isNaN(obj.getTime()) ? "Invalid Date" : obj.toISOString();
  if (obj instanceof ArrayBuffer) return { $tg: "bytes", size: obj.byteLength };
  if (ArrayBuffer.isView(obj)) return { $tg: "bytes", size: obj.byteLength };
  seen.add(obj);
  try {
    if (obj instanceof Error)
      return cap({ name: obj.name, message: obj.message, ...(obj as unknown as Record<string, unknown>) }, cfg, depth, seen);
    if (obj instanceof Map) return capEntries([...obj.entries()].map(([k, x]) => [String(k), x]), obj.size, cfg, depth, seen);
    if (Array.isArray(obj) || obj instanceof Set) {
      const items = [...(obj as Iterable<unknown>)];
      const out = items.slice(0, cfg.maxItems).map((x) => cap(x, cfg, depth + 1, seen));
      if (items.length > cfg.maxItems) out.push({ $tg: "truncated", omitted_items: items.length - cfg.maxItems });
      return out;
    }
    const toJSON = (obj as { toJSON?: () => unknown }).toJSON;
    if (typeof toJSON === "function") return cap(toJSON.call(obj), cfg, depth, new WeakSet());
    const proto = Object.getPrototypeOf(obj);
    const entries = Object.entries(obj);
    if (proto !== null && proto !== Object.prototype && entries.length === 0)
      return { $tg: "repr", type: proto?.constructor?.name ?? "object", repr: cfg.redactor.scrub(safeRepr(v)).slice(0, 2000) };
    return capEntries(entries, entries.length, cfg, depth, seen);
  } finally {
    seen.delete(obj);
  }
}

function capEntries(entries: [string, unknown][], total: number, cfg: CaptureConfig, depth: number, seen: WeakSet<object>) {
  const out: Record<string, unknown> = {};
  let i = 0;
  for (const [k, x] of entries) {
    if (i++ >= cfg.maxItems) {
      out[`${MARKER}:more`] = total - cfg.maxItems;
      break;
    }
    if (x === undefined) continue; // JSON has no undefined; the key was never really "set"
    out[k] = cfg.redactor.keyIsSecret(k) && x !== null && x !== "" ? { $tg: "redacted", reason: "secret-key" } : cap(x, cfg, depth + 1, seen);
  }
  return out;
}

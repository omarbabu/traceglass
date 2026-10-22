/**
 * The recording API: runs, spans, events, artifacts and evidence links.
 * Records are wire-compatible with the Python SDK (schema traceglass/v1).
 */

import { capture, defaultCaptureConfig, NO_REDACTION, notCaptured, Redactor, type CaptureConfig } from "./capture.js";
import { ContextSlot, contextReady } from "./context.js";
import { newId } from "./ids.js";
import { HTTPSink, NullSink, type Sink, type TraceRecord } from "./sinks.js";

export const VERSION = "0.1.0";

export type SpanKind = "agent" | "llm" | "tool" | "retrieval" | "delegation" | "approval" | "guardrail" | "embedding" | "step";
export type Status = "running" | "ok" | "error" | "cancelled" | "skipped" | "waiting";
export type Json = unknown;

export interface TraceglassOptions {
  /** Base URL of `traceglass serve`. Defaults to $TRACEGLASS_URL or http://127.0.0.1:7357. */
  url?: string;
  /** Overrides `url`: any custom sink (MemorySink for tests, FanoutSink, …). */
  sink?: Sink;
  /** Default project for runs. */
  project?: string;
  /** `true` (default) uses the default secret redactor; pass a Redactor to customise, `false` to disable. */
  redact?: boolean | Redactor;
  maxPayloadChars?: number;
  captureInputs?: boolean;
  captureOutputs?: boolean;
  /** Milliseconds since the epoch. */
  clock?: () => number;
  /** Record nothing (also enabled by TRACEGLASS_DISABLED=1). */
  disabled?: boolean;
}

export interface RunOptions {
  project?: string;
  session?: string;
  task?: string;
  input?: Json;
  tags?: string[];
  metadata?: Record<string, unknown>;
  source?: string;
  id?: string;
}

export interface SpanOptions {
  kind?: SpanKind | string;
  input?: Json;
  model?: string;
  agent?: string;
  attributes?: Record<string, unknown>;
  dependsOn?: (Span | string)[];
  retryOf?: Span | string;
  attempt?: number;
  /** Explicit parent; `null` makes this a root span. Defaults to the current span (Node) or none. */
  parent?: Span | string | null;
  id?: string;
}

export interface UsageInfo {
  inputTokens?: number;
  outputTokens?: number;
  cacheReadTokens?: number;
  cacheWriteTokens?: number;
  costUsd?: number;
}

export interface EventOptions {
  kind?: string;
  level?: "debug" | "info" | "warning" | "error" | string;
  message?: string;
  data?: Json;
}

export interface ArtifactOptions {
  kind?: "answer" | "markdown" | "text" | "code" | "diff" | "json" | "document" | "file" | "image" | "report" | "table" | string;
  mime?: string;
  uri?: string;
  /** Producing span. Defaults to the current span (Node); `null` attributes it to the run. */
  span?: Span | string | null;
  final?: boolean;
  metadata?: Record<string, unknown>;
  derivedFrom?: (Artifact | Span | string)[];
  previous?: Artifact | string;
  id?: string;
}

export interface LinkOptions {
  relation?: "derived_from" | "cites" | "supported_by" | "contradicted_by" | "used" | "tested_by" | "revises" | string;
  subjectAnchor?: string;
  evidenceAnchor?: string;
  note?: string;
  confidence?: number;
  declaredBy?: "agent" | "framework" | "human" | string;
}

export interface CiteOptions {
  /** Passage of this artifact that the evidence supports. */
  anchor?: string;
  /** Passage inside the evidence artifact. */
  quote?: string;
  relation?: LinkOptions["relation"];
  note?: string;
  confidence?: number;
}

type Ref = { kind: "span" | "artifact"; id: string; anchor?: Anchor };
type Anchor = { start?: number; end?: number; text: string };

interface Ctx {
  run: Run;
  span: Span | null;
}

function envVar(name: string): string | undefined {
  const p = (globalThis as { process?: { env?: Record<string, string | undefined> } }).process;
  return p?.env?.[name];
}

function errorPayload(err: unknown, redactor: Redactor) {
  if (err instanceof Error) {
    const ctor = err.constructor?.name;
    const type = ctor && ctor !== "Error" ? ctor : err.name || "Error";
    return { type, message: redactor.scrub(err.message), stack: err.stack ? redactor.scrub(err.stack).slice(-8000) : null };
  }
  return { type: "Error", message: redactor.scrub(String(err)), stack: null };
}

function refOf(obj: Artifact | Span | string | { id: string; kind?: string }): Ref {
  if (obj instanceof Span) return { kind: "span", id: obj.id };
  if (obj instanceof Artifact) return { kind: "artifact", id: obj.id };
  if (typeof obj === "string") return { kind: obj.startsWith("art_") ? "artifact" : "span", id: obj };
  if (obj && typeof obj === "object" && "id" in obj)
    return { kind: obj.kind === "artifact" || obj.kind === "span" ? obj.kind : obj.id.startsWith("art_") ? "artifact" : "span", id: obj.id };
  throw new TypeError(`cannot link to ${String(obj)}; pass a Span, Artifact or id`);
}

function anchorIn(content: unknown, text?: string): Anchor | undefined {
  if (!text) return undefined;
  if (typeof content === "string") {
    const i = content.indexOf(text);
    if (i >= 0) return { start: i, end: i + text.length, text };
  }
  return { text };
}

const MISSING: unique symbol = Symbol("missing");
type Maybe<T> = T | typeof MISSING;

// ---------------------------------------------------------------------------------------
// Traceglass (the tracer)
// ---------------------------------------------------------------------------------------
export class Traceglass {
  readonly sink: Sink;
  readonly project: string | undefined;
  readonly cfg: CaptureConfig;
  readonly captureInputs: boolean;
  readonly captureOutputs: boolean;
  readonly clock: () => number;
  /** @internal */ readonly slot = new ContextSlot<Ctx>();
  private seq = 0;

  constructor(opts: TraceglassOptions = {}) {
    const disabled = opts.disabled ?? ["1", "true", "yes"].includes((envVar("TRACEGLASS_DISABLED") ?? "").toLowerCase());
    this.sink = disabled ? new NullSink() : opts.sink ?? new HTTPSink({ url: opts.url ?? envVar("TRACEGLASS_URL") });
    this.project = opts.project;
    const redactor = opts.redact instanceof Redactor ? opts.redact : opts.redact === false ? NO_REDACTION : new Redactor();
    this.cfg = { ...defaultCaptureConfig(redactor), maxString: opts.maxPayloadChars ?? 32_000 };
    this.captureInputs = opts.captureInputs ?? true;
    this.captureOutputs = opts.captureOutputs ?? true;
    this.clock = opts.clock ?? Date.now;
    const proc = (globalThis as { process?: { once?: (ev: string, fn: () => void) => void } }).process;
    proc?.once?.("beforeExit", () => void this.flush());
  }

  /** @internal */ now(): number {
    return this.clock();
  }
  /** @internal */ nextSeq(): number {
    return ++this.seq;
  }
  /** @internal */ capture(v: unknown): unknown {
    return capture(v, this.cfg);
  }
  /** @internal */ emit(rec: TraceRecord): void {
    try {
      this.sink.emit({ ...rec, v: 1 });
    } catch {
      /* tracing must never break the agent */
    }
  }

  currentRun(): Run | undefined {
    return this.slot.get()?.run;
  }

  currentSpan(): Span | undefined {
    return this.slot.get()?.span ?? undefined;
  }

  /** Start a run you will end yourself with `run.end()`. */
  startRun(name: string, opts: RunOptions = {}): Run {
    return new Run(this, name, opts).start();
  }

  /**
   * Run `fn` inside a new run. Spans opened inside (via `tg.span` or `run.span`) attach to it.
   *
   *     await tg.run("Compare papers", { project: "research", input: q }, async (run) => { ... })
   */
  run<T>(name: string, fn: (run: Run) => T | Promise<T>): Promise<T>;
  run<T>(name: string, opts: RunOptions, fn: (run: Run) => T | Promise<T>): Promise<T>;
  async run<T>(name: string, a: RunOptions | ((run: Run) => T | Promise<T>), b?: (run: Run) => T | Promise<T>): Promise<T> {
    const opts = typeof a === "function" ? {} : a;
    const fn = (typeof a === "function" ? a : b)!;
    await contextReady();
    const run = this.startRun(name, opts);
    try {
      const out = await this.slot.run({ run, span: null }, () => fn(run));
      if (out !== undefined && !run.hasOutput()) run.setOutput(out);
      await run.end();
      return out;
    } catch (err) {
      run.status = "error";
      run.metadata.error = errorPayload(err, this.cfg.redactor);
      await run.end();
      throw err;
    }
  }

  /**
   * Run `fn` as a span of the current run (or of a new implicit run named after the span).
   */
  span<T>(name: string, fn: (span: Span) => T | Promise<T>): Promise<T>;
  span<T>(name: string, opts: SpanOptions, fn: (span: Span) => T | Promise<T>): Promise<T>;
  async span<T>(name: string, a: SpanOptions | ((span: Span) => T | Promise<T>), b?: (span: Span) => T | Promise<T>): Promise<T> {
    const opts = typeof a === "function" ? {} : a;
    const fn = (typeof a === "function" ? a : b)!;
    await contextReady();
    const ctx = this.slot.get();
    if (ctx) return ctx.run.span(name, opts, fn);
    // No enclosing run: the span becomes the root of an implicit run named after it.
    const run = new Run(this, name, { input: opts.input }).start();
    try {
      const out = await this.slot.run({ run, span: null }, () => run.span(name, opts, fn));
      if (out !== undefined) run.setOutput(out);
      await run.end();
      return out;
    } catch (err) {
      await run.end({ status: "error" });
      throw err;
    }
  }

  /** Record a provenance relationship: `subject` —relation→ `evidence`. */
  link(subject: Artifact | Span | string, evidence: Artifact | Span | string, opts: LinkOptions & { run?: Run } = {}): TraceRecord {
    const run = opts.run ?? this.currentRun() ?? (subject instanceof Artifact || subject instanceof Span ? subject.run : undefined);
    if (!run) throw new Error("traceglass link() needs a run: call it inside tg.run(), or on run.link()");
    return run.link(subject, evidence, opts);
  }

  async flush(): Promise<void> {
    await this.sink.flush();
  }

  async close(): Promise<void> {
    await this.sink.close();
  }
}

// ---------------------------------------------------------------------------------------
// Run
// ---------------------------------------------------------------------------------------
export class Run {
  readonly id: string;
  name: string;
  project: string;
  session: string | null;
  task: string | null;
  tags: string[];
  metadata: Record<string, unknown>;
  source: string;
  status: Status = "running";
  startedAt: number | null = null;
  endedAt: number | null = null;
  finalArtifactId: string | null = null;
  private input: Maybe<unknown>;
  private output: Maybe<unknown> = MISSING;

  constructor(readonly tg: Traceglass, name: string, opts: RunOptions = {}) {
    this.id = opts.id ?? newId("run");
    this.name = name;
    this.project = opts.project ?? tg.project ?? "default";
    this.session = opts.session ?? null;
    this.task = opts.task ?? null;
    this.tags = [...(opts.tags ?? [])];
    this.metadata = { ...(opts.metadata ?? {}) };
    this.source = opts.source ?? "sdk";
    this.input = opts.input === undefined ? MISSING : tg.capture(opts.input);
  }

  start(at?: number): this {
    if (this.startedAt === null) {
      this.startedAt = at ?? this.tg.now();
      this.emit();
    }
    return this;
  }

  /** End the run and flush buffered records. */
  async end(opts: { status?: Status; output?: Json; at?: number } = {}): Promise<void> {
    if (this.endedAt !== null) return;
    if (opts.output !== undefined) this.setOutput(opts.output);
    this.status = opts.status ?? (this.status === "running" ? "ok" : this.status);
    this.endedAt = opts.at ?? this.tg.now();
    this.emit();
    await this.tg.flush();
  }

  setInput(v: Json): this {
    this.input = this.tg.capture(v);
    return this;
  }
  setOutput(v: Json): this {
    this.output = this.tg.capture(v);
    return this;
  }
  /** @internal */ hasOutput(): boolean {
    return this.output !== MISSING;
  }
  setMetadata(kv: Record<string, unknown>): this {
    Object.assign(this.metadata, this.tg.capture(kv) as object);
    return this;
  }
  /** Mark the artifact that *is* this run's answer; the UI opens it by default. */
  setFinal(a: Artifact): this {
    this.finalArtifactId = a.id;
    if (this.startedAt !== null) this.emit();
    return this;
  }

  /** Start a span you will end yourself (callback-style integrations). Not made current. */
  startSpan(name: string, opts: SpanOptions = {}): Span {
    return new Span(this, name, opts).start();
  }

  /**
   * With a callback: run it as a span (parented to the current span on Node) and return its result.
   * Without: start and return the span — call `span.end()` yourself.
   */
  span<T>(name: string, opts: SpanOptions, fn: (span: Span) => T | Promise<T>): Promise<T>;
  span(name: string, opts?: SpanOptions): Span;
  span<T>(name: string, opts: SpanOptions = {}, fn?: (span: Span) => T | Promise<T>): Promise<T> | Span {
    let parent = opts.parent;
    if (parent === undefined) {
      const cur = this.tg.slot.get();
      parent = cur?.run === this ? cur.span : null;
    }
    const s = new Span(this, name, { ...opts, parent });
    s.start();
    return fn ? s.within(fn) : s;
  }

  artifact(name: string, content?: Json, opts: ArtifactOptions = {}): Artifact {
    let producer: Span | string | null;
    if ("span" in opts && opts.span !== undefined) producer = opts.span;
    else {
      const cur = this.tg.slot.get();
      producer = cur?.run === this ? cur.span : null;
    }
    const tg = this.tg;
    const rec: TraceRecord = {
      type: "artifact",
      id: opts.id ?? newId("artifact"),
      run_id: this.id,
      span_id: producer instanceof Span ? producer.id : producer,
      kind: opts.kind ?? "text",
      name,
      created_at: tg.now(),
      metadata: tg.capture(opts.metadata ?? {}),
    };
    if (content !== undefined) {
      rec.content = tg.capture(content);
      if (typeof content === "string") rec.size = content.length;
    }
    if (opts.mime) rec.mime = opts.mime;
    if (opts.uri) rec.uri = opts.uri;
    if (opts.final) {
      rec.final = true;
      this.finalArtifactId = rec.id;
    }
    const previousId = opts.previous instanceof Artifact ? opts.previous.id : opts.previous;
    if (previousId) rec.previous_id = previousId;
    tg.emit(rec);
    const art = new Artifact(this, rec, content);
    if (previousId) this.link(art, previousId, { relation: "revises" });
    for (const src of opts.derivedFrom ?? []) this.link(art, src, { relation: "derived_from" });
    if (opts.final && this.startedAt !== null) this.emit();
    return art;
  }

  link(subject: Artifact | Span | string, evidence: Artifact | Span | string, opts: LinkOptions = {}): TraceRecord {
    const s = refOf(subject);
    const e = refOf(evidence);
    const sa = anchorIn(subject instanceof Artifact ? subject.content : undefined, opts.subjectAnchor);
    const ea = anchorIn(evidence instanceof Artifact ? evidence.content : undefined, opts.evidenceAnchor);
    if (sa) s.anchor = sa;
    if (ea) e.anchor = ea;
    const rec: TraceRecord = {
      type: "link",
      id: newId("link"),
      run_id: this.id,
      subject: s,
      evidence: e,
      relation: opts.relation ?? "derived_from",
      declared_by: opts.declaredBy ?? "agent",
      created_at: this.tg.now(),
    };
    if (opts.note) rec.note = opts.note;
    if (opts.confidence !== undefined) rec.confidence = opts.confidence;
    this.tg.emit(rec);
    return rec;
  }

  record(): TraceRecord {
    const rec: TraceRecord = {
      type: "run",
      id: this.id,
      name: this.name,
      project: this.project,
      session: this.session,
      task: this.task,
      status: this.status,
      started_at: this.startedAt,
      ended_at: this.endedAt,
      tags: this.tags,
      metadata: this.metadata,
      source: this.source,
      sdk: { name: "traceglass-typescript", version: VERSION },
    };
    if (this.input !== MISSING) rec.input = this.input;
    if (this.output !== MISSING) rec.output = this.output;
    if (this.finalArtifactId) rec.final_artifact_id = this.finalArtifactId;
    return rec;
  }

  private emit() {
    this.tg.emit(this.record());
  }
}

// ---------------------------------------------------------------------------------------
// Span
// ---------------------------------------------------------------------------------------
export class Span {
  readonly id: string;
  readonly runId: string;
  name: string;
  kind: string;
  model: string | undefined;
  agent: string | undefined;
  parentId: string | null;
  attributes: Record<string, unknown>;
  depends: string[];
  retryOf: string | undefined;
  attempt: number | undefined;
  status: Status = "running";
  startedAt: number | null = null;
  endedAt: number | null = null;
  seq: number | null = null;
  error: { type: string; message: string; stack: string | null } | undefined;
  private usageInfo: Record<string, number> | undefined;
  private input: Maybe<unknown>;
  private output: Maybe<unknown> = MISSING;

  constructor(readonly run: Run, name: string, opts: SpanOptions = {}) {
    const tg = run.tg;
    this.id = opts.id ?? newId("span");
    this.runId = run.id;
    this.name = name;
    this.kind = opts.kind ?? "step";
    this.model = opts.model;
    const parent = opts.parent ?? null;
    this.parentId = parent instanceof Span ? parent.id : parent;
    this.agent = opts.agent ?? (this.kind === "agent" ? name : parent instanceof Span ? parent.agent : undefined);
    this.attributes = opts.attributes ? (tg.capture(opts.attributes) as Record<string, unknown>) : {};
    this.depends = (opts.dependsOn ?? []).map((d) => (d instanceof Span ? d.id : d));
    this.retryOf = opts.retryOf instanceof Span ? opts.retryOf.id : opts.retryOf;
    this.attempt = opts.attempt;
    this.input = opts.input === undefined ? MISSING : tg.captureInputs ? tg.capture(opts.input) : notCaptured("input capture disabled");
  }

  private get tg(): Traceglass {
    return this.run.tg;
  }

  start(at?: number): this {
    if (this.startedAt !== null) return this;
    this.seq = this.tg.nextSeq();
    this.startedAt = at ?? this.tg.now();
    this.emit();
    return this;
  }

  end(opts: { status?: Status; output?: Json; at?: number } = {}): this {
    if (this.startedAt === null) this.start(opts.at);
    if (this.endedAt !== null) return this;
    if (opts.output !== undefined) this.setOutput(opts.output);
    if (opts.status) this.status = opts.status;
    else if (this.status === "running" || this.status === "waiting") this.status = "ok";
    this.endedAt = opts.at ?? this.tg.now();
    this.emit();
    return this;
  }

  /** @internal Run `fn` with this span as the current span; ends the span afterwards. */
  async within<T>(fn: (span: Span) => T | Promise<T>): Promise<T> {
    try {
      const out = await this.tg.slot.run({ run: this.run, span: this }, () => fn(this));
      if (out !== undefined && this.output === MISSING) this.setOutput(out);
      this.end();
      return out;
    } catch (err) {
      if (this.status !== "error") this.recordError(err);
      this.end();
      throw err;
    }
  }

  /** Open a child span. With a callback it runs inside it; without, the child is returned started. */
  span<T>(name: string, opts: SpanOptions, fn: (span: Span) => T | Promise<T>): Promise<T>;
  span(name: string, opts?: SpanOptions): Span;
  span<T>(name: string, opts: SpanOptions = {}, fn?: (span: Span) => T | Promise<T>): Promise<T> | Span {
    const child = new Span(this.run, name, { ...opts, parent: opts.parent === undefined ? this : opts.parent });
    child.start();
    return fn ? child.within(fn) : child;
  }

  setInput(v: Json): this {
    this.input = this.tg.captureInputs ? this.tg.capture(v) : notCaptured("input capture disabled");
    return this;
  }

  setOutput(v: Json): this {
    this.output = this.tg.captureOutputs ? this.tg.capture(v) : notCaptured("output capture disabled");
    return this;
  }

  /** Declare that a field existed but the integration could not see it. */
  markNotCaptured(field: "input" | "output" | string, reason: string): this {
    if (field === "input") this.input = notCaptured(reason);
    else if (field === "output") this.output = notCaptured(reason);
    else this.attributes[field] = notCaptured(reason);
    return this;
  }

  setAttribute(key: string, value: Json): this {
    this.attributes[key] = this.tg.capture(value);
    return this;
  }

  setAttributes(kv: Record<string, unknown>): this {
    for (const [k, v] of Object.entries(kv)) this.setAttribute(k, v);
    return this;
  }

  setModel(model: string): this {
    this.model = model;
    return this;
  }

  /** Change status mid-flight (e.g. an approval gate `waiting` on a human); emitted immediately. */
  setStatus(status: Status): this {
    this.status = status;
    if (this.startedAt !== null && this.endedAt === null) this.emit();
    return this;
  }

  usage(u: UsageInfo): this {
    const map: Record<string, number | undefined> = {
      input_tokens: u.inputTokens,
      output_tokens: u.outputTokens,
      cache_read_tokens: u.cacheReadTokens,
      cache_write_tokens: u.cacheWriteTokens,
      cost_usd: u.costUsd,
    };
    const next = { ...(this.usageInfo ?? {}) };
    for (const [k, v] of Object.entries(map)) if (v !== undefined && v !== null) next[k] = v;
    this.usageInfo = next;
    return this;
  }

  /** Declare a data dependency beyond the parent/child relationship. */
  dependsOn(...spans: (Span | string)[]): this {
    for (const s of spans) {
      const id = s instanceof Span ? s.id : s;
      if (!this.depends.includes(id)) this.depends.push(id);
    }
    if (this.startedAt !== null) this.emit();
    return this;
  }

  /** Record that this operation consumed artifacts (e.g. retrieved documents). */
  uses(...items: (Artifact | string)[]): this {
    for (const a of items) this.run.link(this, a, { relation: "used" });
    return this;
  }

  event(name: string, opts: EventOptions = {}): void {
    if (this.startedAt === null) this.start();
    const rec: TraceRecord = {
      type: "event",
      id: newId("event"),
      run_id: this.runId,
      span_id: this.id,
      kind: opts.kind ?? "log",
      name,
      level: opts.level ?? "info",
      ts: this.tg.now(),
    };
    if (opts.message !== undefined) rec.message = this.tg.cfg.redactor.scrub(opts.message);
    if (opts.data !== undefined) rec.data = this.tg.capture(opts.data);
    this.tg.emit(rec);
  }

  recordError(err: unknown, opts: { type?: string } = {}): this {
    this.error = typeof err === "string" ? { type: opts.type ?? "Error", message: err, stack: null } : errorPayload(err, this.tg.cfg.redactor);
    if (opts.type) this.error.type = opts.type;
    this.status = "error";
    this.event(this.error.type, { kind: "error", level: "error", message: this.error.message });
    return this;
  }

  fail(err: unknown, opts: { type?: string; at?: number } = {}): this {
    this.recordError(err, opts);
    return this.end({ status: "error", at: opts.at });
  }

  record(): TraceRecord {
    const rec: TraceRecord = {
      type: "span",
      id: this.id,
      run_id: this.runId,
      parent_id: this.parentId,
      kind: this.kind,
      name: this.name,
      status: this.status,
      started_at: this.startedAt,
      ended_at: this.endedAt,
      seq: this.seq,
      attributes: this.attributes,
    };
    if (this.agent) rec.agent = this.agent;
    if (this.model) rec.model = this.model;
    if (this.usageInfo) {
      const u: Record<string, number> = { ...this.usageInfo };
      if ("input_tokens" in u || "output_tokens" in u) u.total_tokens = (u.input_tokens ?? 0) + (u.output_tokens ?? 0);
      rec.usage = u;
    }
    if (this.depends.length) rec.depends_on = [...this.depends];
    if (this.retryOf) rec.retry_of = this.retryOf;
    if (this.attempt !== undefined) rec.attempt = this.attempt;
    if (this.error) rec.error = this.error;
    if (this.input !== MISSING) rec.input = this.input;
    if (this.output !== MISSING) rec.output = this.output;
    return rec;
  }

  private emit() {
    this.tg.emit(this.record());
  }
}

// ---------------------------------------------------------------------------------------
// Artifact
// ---------------------------------------------------------------------------------------
export class Artifact {
  readonly id: string;
  readonly runId: string;
  readonly name: string;
  readonly kind: string;

  constructor(readonly run: Run, private rec: TraceRecord, readonly content: unknown) {
    this.id = rec.id;
    this.runId = rec.run_id as string;
    this.name = rec.name as string;
    this.kind = rec.kind as string;
  }

  /** Link a passage of this artifact (`anchor`) to the evidence behind it (`quote` inside the evidence). */
  cite(evidence: Artifact | Span | string, opts: CiteOptions = {}): this {
    this.run.link(this, evidence, {
      relation: opts.relation ?? "supported_by",
      subjectAnchor: opts.anchor,
      evidenceAnchor: opts.quote,
      note: opts.note,
      confidence: opts.confidence,
    });
    return this;
  }

  derivedFrom(...sources: (Artifact | Span | string)[]): this {
    for (const s of sources) this.run.link(this, s, { relation: "derived_from" });
    return this;
  }

  /** Record a human review decision: `approved` · `rejected` · `needs_changes`. */
  review(status: "approved" | "rejected" | "needs_changes" | string, opts: { by?: string; note?: string } = {}): this {
    this.rec.review = { status, by: opts.by ?? null, note: opts.note ?? null, at: this.run.tg.now() };
    this.run.tg.emit({ ...this.rec });
    return this;
  }
}

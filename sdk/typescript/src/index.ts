/**
 * @traceglass/sdk — record agent runs for Traceglass from TypeScript / JavaScript.
 *
 *     import { Traceglass } from "@traceglass/sdk";
 *     const tg = new Traceglass({ project: "research-agent" });
 *     await tg.run("Compare papers", { input: question }, async (run) => {
 *       const hits = await run.span("search", { kind: "tool", input: q }, () => search(q));
 *       run.artifact("Report", text, { kind: "markdown", final: true }).cite(doc, { anchor, quote });
 *     });
 */

export { Artifact, Run, Span, Traceglass, VERSION } from "./tracer.js";
export type {
  ArtifactOptions,
  CiteOptions,
  EventOptions,
  LinkOptions,
  RunOptions,
  SpanKind,
  SpanOptions,
  Status,
  TraceglassOptions,
  UsageInfo,
} from "./tracer.js";
export { capture, defaultCaptureConfig, isMarker, MARKER, NO_REDACTION, notCaptured, Redactor } from "./capture.js";
export type { CaptureConfig, Marker, RedactorOptions } from "./capture.js";
export { FanoutSink, HTTPSink, MemorySink, NullSink } from "./sinks.js";
export type { HTTPSinkOptions, Sink, TraceRecord } from "./sinks.js";
export { newId } from "./ids.js";
export { contextReady, hasAsyncContext } from "./context.js";

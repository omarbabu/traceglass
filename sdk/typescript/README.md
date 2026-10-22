# @traceglass/sdk

Record agent runs from TypeScript/JavaScript and explore them in [Traceglass](../../README.md) — a visual,
navigable record of how an AI agent arrived at an answer.

- Zero runtime dependencies; Node ≥ 18 (global `fetch`), Deno, Bun and browsers.
- Wire-compatible with the Python SDK (schema `traceglass/v1`): same records, ids, redaction and `$tg` markers.
- Implicit parenting via `AsyncLocalStorage` on Node — spans opened inside a callback attach to the right
  parent across `await` and `Promise.all`. In browsers, open children from the parent (`span.span(...)`).
- Secrets are redacted before anything leaves the process (key names like `api_key`, `authorization`, and
  value patterns such as provider keys and bearer tokens).

```ts
import { Traceglass } from "@traceglass/sdk";

const tg = new Traceglass({ project: "research-agent" }); // → http://127.0.0.1:7357 (or $TRACEGLASS_URL)

await tg.run("Compare papers", { input: { question } }, async (run) => {
  const hits = await tg.span("search", { kind: "tool", input: { query } }, () => search(query));

  const docs = await tg.span("fetch", { kind: "retrieval" }, async () =>
    hits.map((h) => run.artifact(h.title, h.text, { kind: "document" })),
  );

  await tg.span("synthesize", { kind: "llm", model: "claude-opus-5-5" }, async (s) => {
    s.uses(...docs);
    s.usage({ inputTokens: 5200, outputTokens: 640, costUsd: 0.04 });
    const report = run.artifact("Report", text, { kind: "markdown", final: true });
    report.cite(docs[0], { anchor: "a sentence in the report", quote: "the passage that supports it" });
  });
});
```

Start the viewer with `traceglass serve` (from the Python package) and open http://127.0.0.1:7357.

## API at a glance

| | |
|---|---|
| `tg.run(name, opts?, fn)` / `tg.startRun(name, opts?)` → `run.end()` | a run: one invocation of a task |
| `tg.span(name, opts?, fn)` · `run.span(...)` · `span.span(...)` | a bounded operation; without `fn` it returns a started span you `end()` |
| `opts.kind` | `agent` · `llm` · `tool` · `retrieval` · `delegation` · `approval` · `guardrail` · `embedding` · `step` |
| `span.setOutput` · `setAttributes` · `usage` · `event` · `fail(err)` · `setStatus("waiting")` | details of a span |
| `{ retryOf, attempt }` · `{ dependsOn: [span] }` · `span.uses(artifact)` | retries and data dependencies |
| `run.artifact(name, content, { kind, final, span })` | answers, reports, diffs, retrieved documents |
| `artifact.cite(evidence, { anchor, quote, relation })` · `review("approved")` | provenance and human review |
| `span.markNotCaptured("input", reason)` | say a value existed but wasn't recorded — not the same as empty |

Sinks: `HTTPSink` (default, batched `POST /api/ingest`), `MemorySink` (tests), `FanoutSink`, `NullSink`.
Pass `new Traceglass({ sink })`, `redact: false | new Redactor({ keys, patterns })`, or set
`TRACEGLASS_DISABLED=1` to record nothing.

## Develop

```bash
npm install
npm test            # vitest
npm run typecheck
npm run example     # needs `traceglass serve` running
```

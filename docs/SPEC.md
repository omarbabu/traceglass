# Traceglass — design spec (v0.1)

This is the working spec the project is built against. It started from the original product brief ("an agentic visualization framework: a visual, navigable record of how an AI agent arrived at an answer") and records the decisions — and the changes to that brief — made while building it.

## 1. Thesis

> Agent observability tells you what ran. Agentic visualization helps you understand, navigate and review what happened.

The unit of value is not the trace; it is the **reviewable artifact**. A reviewer holding a report, a patch, or an incident summary should be able to click any meaningful part of it and walk backwards to the steps, tool results and sources that produced it — and forwards from any failure to everything it affected.

Positioning: LangSmith, Arize Phoenix and the OpenTelemetry GenAI conventions already cover trace collection, evaluation and a shared vocabulary. Traceglass does not compete on collection. It reuses those conventions (OTLP ingestion is a first-class input) and owns the **experience of navigating execution and reviewing artifacts**, local-first, without an account or a hosted service.

## 2. Principles

1. **Every run is a navigable workspace.** Stable, time-sortable IDs; deep links address a run, a step, an artifact, and a moment (`#/run/<id>/<node>?t=<ms>`).
2. **Outputs have provenance.** Artifacts and evidence links are first-class records, not log lines.
3. **The graph is semantic.** Agents are containers; time flows top to bottom; kinds (model call, tool, retrieval, delegation, approval, guardrail) are visually distinct; data dependencies, retries and "produced" relationships are distinct edge types.
4. **Framework-agnostic.** One canonical model; integrations are translators. The core imports no framework.
5. **Lightweight.** The Python core has zero runtime dependencies. One `pip install`, one command, a useful UI in under a minute.
6. **Honest.** The UI shows the observable execution path and the evidence the agent *declared*. It never implies access to private reasoning, never presents a link as verification, and distinguishes absent / not captured / redacted / truncated / empty.

## 3. Canonical model

Five record types, all upserts keyed by `id`, all carrying `run_id` and `v: 1`. Full reference: [schema.md](schema.md); machine-readable: [`schema/traceglass.schema.json`](../schema/traceglass.schema.json).

| Record | Purpose |
|---|---|
| **run** | One invocation of a task: name, project, session, status, input/output, `final_artifact_id`. |
| **span** | A bounded operation with `kind`, parent, start/end, status, input/output, model + usage, `depends_on`, `retry_of` + `attempt`, error. |
| **event** | A point in time inside a span: log, retry, error, state change. |
| **artifact** | Something worth reviewing — answer, report, patch, file, *or a retrieved document* — attributed to the span that produced it, optionally versioned (`previous_id`) and reviewed (`review`). |
| **link** | `subject —relation→ evidence`, each side a span or artifact with an optional text anchor. Relations: `supported_by`, `contradicted_by`, `cites`, `derived_from`, `used`, `tested_by`, `revises`. |

Causal order (parent/child, `depends_on`, links) and temporal order (timestamps, `seq`) are both preserved: they answer different questions, and the layout uses both. When a framework does not declare dependencies, the UI infers sequencing among siblings from time (fork/join for parallel branches; a single edge for workers in a concurrent pool) and draws declared dependencies distinctly.

**Capture markers** (`{"$tg": ...}`): `not_captured` (with reason), `redacted`, `truncated` (with preview and original length), `bytes`, `repr`. An absent field means the integration never reported it.

## 4. Architecture

```
agent ─▶ SDK / adapter ─▶ sink ─▶ SQLite (WAL) ◀─ tail change log ─ server ─SSE─▶ UI
                          │                                           ▲
                          └─ HTTP sink ─▶ POST /api/ingest ───────────┘
                    OTLP JSON exporter ─▶ POST /v1/traces ────────────┘
```

- **Default sink writes SQLite directly** in a background thread. The server tails a `changes` table and streams it over server-sent events. Result: live view across processes with no configuration and no network dependency for the agent. (Changed from the brief's "in-process events plus HTTP/WebSocket": SSE is one-directional, which is all live view needs, and works with the stdlib server.)
- **HTTP ingestion** for other languages and remote processes; **OTLP/HTTP JSON** for anything already speaking OpenTelemetry.
- **Exports** (single HTML file with data inlined; static site) make runs shareable without a server.

## 5. The experience

- **Left rail — run navigator.** Grouped by project → session; filter; live status; `[`/`]` between runs.
- **Center — execution canvas.** Hierarchical layout (ELK) with agents as containers; intermediate artifacts attached to their producing step as chips; the final answer as its own node. Heavy subtrees auto-collapse on large runs; kind filters; evidence-link overlay.
- **Bottom — timeline.** Waterfall in execution order with a scrubber and replay (1–32×); the canvas reflects the replay instant.
- **Right — inspector.** Run overview (metrics, final answer, failures); step (error + downstream effects, timing, model/usage, input/output rendered as chat when they are messages, events, lineage); artifact (preview with evidence highlights, evidence list, details, review status).
- **Selection is provenance.** Selecting a node highlights its upstream lineage (what it came from) and downstream lineage (what it affected), including failure propagation to enclosing steps. Clicking a highlighted claim focuses that single evidence link and shows the source passage in context.
- **Keyboard-first.** ⌘K palette across runs, steps, artifacts and actions; `j/k/h/l`, `Enter`, `e`, `a`, `space`, `f`, `t`, `v`, `b`, `c`, `/`, `?`.

## 6. Scope

**In v0.1:** Python SDK; SQLite store; local server with live stream; UI (graph, timeline, inspector, palette, replay, deep links); LangChain/LangGraph, Anthropic SDK and OTLP JSON integrations; live and closed **Claude Code and Kiro CLI sessions** (importers, `traceglass watch`, lifecycle hooks — see [coding-agents.md](coding-agents.md)); TypeScript SDK; HTML and static-site export; redaction; deterministic demos.

**Deliberately out:** dashboards, alerting, team management, accounts, hosted service, graph or vector database prerequisites, aggregate analytics.

## 7. Changes from the original brief

| Brief | Decision | Why |
|---|---|---|
| React Flow *or* a custom canvas | React Flow + ELK hierarchical layout, top-to-bottom by default | Agent runs are long sequences of wide nodes; vertical layered layout with agent containers is ~2× more legible than left-to-right at the same zoom. Left-to-right remains a toggle. |
| Artifacts as graph nodes | Final answers are nodes; intermediate artifacts are chips inside their producer | Keeps provenance clickable without retrieved documents tripling graph width. |
| HTTP/WebSocket transport | SQLite change-log tailing + SSE; HTTP ingest | Zero-config live view across processes; stdlib only. |
| "Make sensitive payloads collapsible or redacted" | Redacted by default *in the agent process*, with explicit markers | Secrets never reach the store. |
| — | Explicit `not_captured` / `truncated` markers | "Distinguish missing information from empty information" needs a wire-level representation, not just UI copy. |
| — | Failure propagation in lineage | Selecting a failed tool call should show the subagent and delegation that failed with it, and the work done on degraded evidence. |
| — | Single-file HTML export | Attaching a run to a PR or incident ticket is the most common way reviews actually happen. |
| — | Claude Code + Kiro: importers, a watcher and hooks | CLI coding agents are where "where did this change come from?" matters most; their transcripts already exist on disk, so live view needs no agent changes. Hooks are optional and only add timing. |
| — | View-only paging of long step sequences | A 300-step turn is a needle at any zoom; folded pages with per-page counts act as a table of contents. Pages are labelled as a view grouping, not recorded structure. |
| — | Estimated timing is marked, never hidden | Kiro timestamps turns, not steps; in-turn durations render as `~` with hatched bars and an explanation, exact once hooks are installed. |
| Python first, TypeScript next | Both in v0.1, same wire format | Proves the core is not coupled to one language. |

## 8. Risks and open questions

- **Declared vs. verified evidence.** Links are claims made by the agent or framework. Verifying them (e.g. entailment checks) is a separate feature and must be labelled as such when added.
- **Payload size.** Long contexts are truncated at 32k chars per string by default; artifacts may need content-addressed blob storage later.
- **Clock skew.** Spans from multiple processes/hosts are laid out by timestamps; skew can create misleading implicit sequencing. Declared `depends_on` always wins.
- **Naming.** "Traceglass" is a working name pending registry and trademark checks.

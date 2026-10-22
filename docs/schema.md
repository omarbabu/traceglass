# Canonical model — schema v1

All records are JSON objects with `type`, `id`, `run_id` (except runs, whose `id` *is* the run id) and `v: 1`. Records are **upserts**: send a span when it starts and again when it ends; later fields win, `attributes`/`metadata` merge, and for runs `started_at`/`ended_at` widen. Times are **epoch milliseconds**.

Machine-readable: [`schema/traceglass.schema.json`](../schema/traceglass.schema.json). Transport: `POST /api/ingest` with `{"records": [...]}` (Content-Type `application/json`).

## IDs

`<prefix>_<20 Crockford base32 chars>` — 48 bits of millisecond time + 52 random bits, so IDs sort by creation time and are safe in URLs. Prefixes: `run_`, `sp_`, `ev_`, `art_`, `ln_`. Importers may use their own stable IDs (e.g. `otel_<span id>`, `cc_<session id>`) so re-imports upsert.

## run

| field | type | notes |
|---|---|---|
| `name` | string | shown in the navigator |
| `project`, `session`, `task` | string? | navigator grouping |
| `status` | `running` · `ok` · `error` · `cancelled` | derived from root spans if omitted |
| `started_at`, `ended_at` | number? | |
| `input`, `output` | any | captured payloads |
| `final_artifact_id` | string? | the artifact that *is* the answer; opened by `a` |
| `tags` | string[] | |
| `metadata` | object | e.g. `time_scale` for compressed demo clocks |
| `source` | string | `sdk`, `otlp`, `claude-code`, `langchain`… |

## span

| field | type | notes |
|---|---|---|
| `parent_id` | string? | containment; spans with children render as containers |
| `kind` | `agent` · `llm` · `tool` · `retrieval` · `delegation` · `approval` · `guardrail` · `embedding` · `step` | |
| `name` | string | |
| `status` | `running` · `ok` · `error` · `waiting` · `cancelled` · `skipped` | `waiting` = blocked on a human/external gate |
| `started_at`, `ended_at`, `seq` | number | `seq` breaks timestamp ties |
| `agent` | string? | owning agent (inherited from the nearest `agent` ancestor) |
| `model` | string? | |
| `usage` | `{input_tokens, output_tokens, total_tokens, cache_read_tokens, cache_write_tokens, cost_usd}` | |
| `input`, `output` | any | |
| `error` | `{type, message, stack}` | |
| `attributes` | object | anything else; integrations put framework details here |
| `depends_on` | string[] | data dependencies beyond containment |
| `retry_of`, `attempt` | string?, number? | retry chains |

## event

`span_id`, `kind` (`log` · `retry` · `error` · `state` · …), `name`, `level` (`info` · `warning` · `error`), `ts`, `message?`, `data?`.

## artifact

| field | type | notes |
|---|---|---|
| `span_id` | string? | producer |
| `kind` | `answer` · `markdown` · `report` · `text` · `code` · `diff` · `json` · `document` · `file` · `image` · `table` | `document` = retrieved source material |
| `name`, `content`, `mime`, `uri`, `size` | | content may be a capture marker |
| `final` | bool | |
| `previous_id` | string? | versioning (also emits a `revises` link) |
| `review` | `{status, by, note, at}` | human decision: `approved` · `rejected` · `needs_changes` |
| `metadata` | object | |

## link

```json
{
  "type": "link", "id": "ln_…", "run_id": "run_…",
  "subject":  {"kind": "artifact", "id": "art_report", "anchor": {"start": 412, "end": 470, "text": "…claim…"}},
  "evidence": {"kind": "artifact", "id": "art_doc",    "anchor": {"start": 88,  "end": 141, "text": "…passage…"}},
  "relation": "supported_by",
  "declared_by": "agent",
  "note": "optional", "confidence": 0.8
}
```

Read it as a sentence: *subject* **relation** *evidence*. Relations with UI semantics: `supported_by`, `cites` (support), `contradicted_by` (contradiction), `derived_from`, `used`, `tested_by`, `revises`. Unknown relations are shown verbatim.

Anchors carry `text` and, when the SDK can locate it in the content, `start`/`end` character offsets.

## Capture markers

| marker | meaning |
|---|---|
| *(field absent)* | the integration never reported it |
| `{"$tg": "not_captured", "reason": "…"}` | it existed but was not recorded |
| `{"$tg": "redacted", "reason": "secret-key"}` | removed in-process; string values have `‹redacted:kind›` substituted inline |
| `{"$tg": "truncated", "preview": "…", "length": 81234}` | shortened to the payload limit |
| `{"$tg": "bytes", "size": n}` · `{"$tg": "repr", "type": "…", "repr": "…"}` | non-JSON values |
| `null`, `""`, `[]` | genuinely empty |

## Bundle (read API)

`GET /api/runs/<id>` returns `{schema: "traceglass/v1", run, spans, events, artifacts, links}`; `run.summary` adds counts, tokens and cost.

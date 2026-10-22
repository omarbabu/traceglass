/**
 * The demo-video tour: a real Claude Code run replayed live, reviewed, traced to evidence,
 * and compared against the same task done by a different model.
 *
 * Open an exported HTML file containing the runs with `#/?tour=main` to watch it play.
 */
import { buildModel } from "../model/graph";
import { useStore } from "../store";
import type { Bundle, Run } from "../types";
import {
  badge,
  camera,
  cameraOn,
  cameraReset,
  caption,
  card,
  click,
  cursorTo,
  cut,
  hideCaption,
  hideCursor,
  layoutRect,
  q,
  scrollBy,
  until,
  useTour,
  wait,
} from "./engine";

const S = () => useStore.getState();

function modelsOf(b: Bundle): string {
  return b.spans
    .map((s) => s.model ?? "")
    .filter(Boolean)
    .join(" ");
}

/** Replay a recorded run as if it were happening now, compressed into `ms`. Returns the speed-up. */
/** Shift every timestamp in a bundle by `d` ms (so a recorded run can "start now"). */
function shifted(b: Bundle, d: number): Bundle {
  const sh = (t: number | null | undefined) => (t == null ? t : t + d);
  return {
    ...b,
    run: { ...b.run, started_at: sh(b.run.started_at), ended_at: sh(b.run.ended_at) },
    spans: b.spans.map((s) => ({ ...s, started_at: sh(s.started_at), ended_at: sh(s.ended_at) })),
    artifacts: b.artifacts.map((a) => ({ ...a, created_at: sh(a.created_at) })),
    events: b.events.map((e) => ({ ...e, ts: sh(e.ts) })),
    links: b.links.map((l) => ({ ...l, created_at: sh(l.created_at) ?? undefined })),
  };
}

async function livePlayback(recorded: Bundle, ms: number): Promise<number> {
  const first = Math.min(...recorded.spans.map((s) => s.started_at ?? Infinity), recorded.run.started_at ?? Infinity);
  const full = shifted(recorded, Date.now() - first);
  const t0 = Math.min(...full.spans.map((s) => s.started_at ?? Infinity), full.run.started_at ?? Infinity);
  const t1 = Math.max(...full.spans.map((s) => s.ended_at ?? s.started_at ?? 0), full.run.ended_at ?? 0);
  const speed = (t1 - t0) / ms;
  const at = (t: number | null | undefined) => ((t ?? t0) - t0) / speed;
  type Ev = { t: number; apply: (b: Bundle) => Bundle };
  const evs: Ev[] = [];
  for (const s of full.spans) {
    const running = { ...s, status: "running" as const, ended_at: null, output: undefined };
    evs.push({ t: at(s.started_at), apply: (b) => ({ ...b, spans: [...b.spans, running] }) });
    evs.push({ t: at(s.ended_at ?? s.started_at), apply: (b) => ({ ...b, spans: b.spans.map((x) => (x.id === s.id ? s : x)) }) });
  }
  for (const a of full.artifacts) evs.push({ t: at(a.created_at), apply: (b) => ({ ...b, artifacts: [...b.artifacts, a] }) });
  for (const e of full.events) evs.push({ t: at(e.ts), apply: (b) => ({ ...b, events: [...b.events, e] }) });
  evs.sort((x, y) => x.t - y.t);

  const liveRun: Run = { ...full.run, status: "running", ended_at: null, final_artifact_id: undefined, metadata: { ...full.run.metadata, activity: "working" } };
  let b: Bundle = { ...full, run: liveRun, spans: [], artifacts: [], events: [], links: [] };
  const listWith = (r: Run) => S().runs.map((x) => (x.id === r.id ? { ...x, ...r, summary: x.summary } : x));
  useStore.setState({ runs: listWith(liveRun), follow: true, collapsed: new Set(), selected: null });
  S().loadBundle(b);

  const start = performance.now();
  let i = 0;
  while (i < evs.length) {
    const now = performance.now() - start;
    let dirty = false;
    while (i < evs.length && evs[i].t <= now) {
      b = evs[i++].apply(b);
      dirty = true;
    }
    if (dirty) S().loadBundle(b);
    await wait(90);
  }
  const done = { ...full };
  useStore.setState({ runs: listWith(full.run) });
  S().loadBundle(done);
  return speed;
}

function centerOf(sel: string) {
  const el = q(sel);
  if (!el) return { x: innerWidth / 2, y: innerHeight / 2 };
  const r = layoutRect(el);
  return { x: r.x + r.w / 2, y: r.y + r.h / 2 };
}

export async function mainTour() {
  window.__tgTour = { ready: false, done: false, duration: 45000 };
  try {
    document.body.classList.add("touring");
    useTour.getState().set({ active: true });
    useStore.setState({ showTimeline: true, showNavigator: true, direction: "DOWN" });

    // ---- data
    const runs = S().runs;
    const cc = runs.filter((r) => r.source === "claude-code");
    const bundles = (await Promise.all(cc.map((r) => S().backend.getRun(r.id)))).filter(Boolean) as Bundle[];
    const A = bundles.find((b) => /opus/.test(modelsOf(b))) ?? bundles[0];
    const B = bundles.find((b) => b !== A && /sonnet|haiku/.test(modelsOf(b))) ?? bundles.find((b) => b !== A);
    const research = runs.find((r) => /deep research/i.test(r.name));
    if (!A) throw new Error("tour needs a Claude Code run");

    // ---- scene 0: title card over an empty live canvas
    useTour.getState().set({ card: { kind: "intro", visible: true } });
    const skeleton: Bundle = { ...A, run: { ...A.run, status: "running", ended_at: null, metadata: { ...A.run.metadata, activity: "working" } }, spans: [], artifacts: [], events: [], links: [] };
    S().loadBundle(skeleton);
    await document.fonts?.ready;
    await wait(400);
    window.__tgTour.ready = true;
    await card("intro", 3300);

    // ---- scene 1: an agent at work
    const total = Math.max(...A.spans.map((s) => s.ended_at ?? 0)) - Math.min(...A.spans.map((s) => s.started_at ?? Infinity));
    const LIVE_MS = 8000;
    const live = livePlayback(A, LIVE_MS);
    const speed = total / LIVE_MS;
    badge(`Real Claude Code session · ${speed.toFixed(speed < 10 ? 1 : 0)}× speed`);
    await caption("Watch a coding agent work — every model call and tool call, as it happens", "Live view");
    const canvas = centerOf(".canvas");
    await camera(1.18, canvas.x, canvas.y + 30, 1700);
    await wait(2700);
    await caption("It reads the code, makes the change, runs the tests — each step lands as it happens", "Execution graph");
    await live;
    badge(null);
    await caption("Then the run becomes a record you can review", "Review");
    useStore.setState({ fitTick: S().fitTick + 1 });
    await cameraReset(1000);
    await wait(150);

    // ---- scene 2: where did this change come from?
    const file = q('.file-row[data-path="shop/checkout.py"]') ?? q(".file-row");
    await cursorTo(file, 1000);
    await click(file);
    await caption("Where did this change come from? One click traces it to the call that made it", "Provenance");
    await wait(350);
    await cameraOn(q(".inspector .code") ?? q(".inspector"), 1.34, 1100, -30, 30);
    await wait(1200);
    await camera(1.22, centerOf(".canvas").x, centerOf(".canvas").y, 1200);
    await wait(800);
    hideCursor();

    // ---- scene 3: the failing test it fixed
    const failing = A.spans.find((s) => s.kind === "tool" && /FAIL/.test(typeof s.output === "string" ? s.output : JSON.stringify(s.output ?? "")));
    if (failing) {
      await cameraReset(900);
      S().select(null);
      useStore.setState({ runTab: "conversation" });
      await wait(450);
      const row = q(`.conv-step[data-id="${failing.id}"]`);
      row?.scrollIntoView({ block: "center" });
      await cursorTo(row, 950);
      await click(row);
      await wait(250);
      const outTab = q(".inspector .tab", "Output");
      await cursorTo(outTab, 700);
      await click(outTab);
      await caption("It failed a test, then fixed it — and you can see both", "Inspect");
      const body = q(".inspector .insp-body");
      if (body) await scrollBy(body, body.scrollHeight - body.clientHeight - body.scrollTop - 40, 900);
      await cameraOn(q(".inspector .payload-box") ?? q(".inspector"), 1.4, 1100, 0, 120);
      await wait(1700);
      hideCursor();
    }

    // ---- scene 4: evidence
    if (research) {
      await hideCaption();
      await cut(async () => {
        await cameraReset(1);
        const rb = await S().backend.getRun(research.id);
        const fin = rb ? buildModel(rb).finalArtifact : undefined;
        await S().openRun(research.id, fin?.id ?? null);
        await until(() => q("mark.ev.contradict"), 4000);
        await wait(700);
      }, 520);
      await caption("Claims linked to the evidence behind them — even evidence that disagrees", "Evidence");
      const mark = q("mark.ev.contradict") ?? q("mark.ev");
      await cursorTo(mark, 950, { x: 30, y: 0 });
      await click(mark);
      await wait(300);
      await camera(1.12, centerOf(".main").x + 170, centerOf(".main").y, 1100);
      await wait(1100);
      hideCursor();
    }

    // ---- scene 5: compare harnesses
    if (B) {
      await hideCaption();
      await cut(async () => {
        await cameraReset(1);
        await S().openCompare(A.run.id, B.run.id);
        await until(() => q(".cmp-row"), 4000);
      }, 520);
      await caption("Same task, two models: time, tokens, tool calls and cost", "Compare");
      await wait(500);
      await cameraOn(q(".cmp-table"), 1.2, 1300, 0, -12);
      await wait(1700);
      await cameraOn(q(".cmp-lanes"), 1.16, 1200, 0, 70);
      await caption("Speed, cost and quality — measured side by side, not hidden behind one score", "Evaluate");
      await wait(2300);
    }

    // ---- outro
    await hideCaption();
    await card("outro", 3900, true);
    window.__tgTour.done = true;
  } catch (e) {
    window.__tgTour = { ...(window.__tgTour ?? { ready: true }), ready: true, done: true, error: String(e) };
    console.error("tour failed", e);
  }
}

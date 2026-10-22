import { Pause, Play, Radio, SkipBack, Star } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { fmtDuration, fmtOffset, hueFor } from "../model/format";
import { statusAt } from "../model/graph";
import { spanKind } from "../model/kinds";
import { useStore } from "../store";
import { isEstimated } from "../types";

const SPEEDS = [1, 4, 8, 32];

function niceStep(totalMs: number, target = 8): number {
  const raw = totalMs / target;
  const steps = [10, 25, 50, 100, 250, 500, 1000, 2000, 5000, 10000, 15000, 30000, 60000, 120000, 300000, 600000, 1800000, 3600000,
    7200000, 10800000, 21600000, 43200000, 86400000, 172800000, 604800000];
  return steps.find((s) => s >= raw) ?? 604800000;
}

export function Timeline() {
  const model = useStore((s) => s.model);
  const collapsed = useStore((s) => s.collapsed);
  const selected = useStore((s) => s.selected);
  const replayT = useStore((s) => s.replayT);
  const playing = useStore((s) => s.playing);
  const speed = useStore((s) => s.speed);
  const select = useStore((s) => s.select);
  const setReplay = useStore((s) => s.setReplay);
  const setPlaying = useStore((s) => s.setPlaying);
  const setSpeed = useStore((s) => s.setSpeed);
  const [height, setHeight] = useState(() => Number(localStorage.getItem("tg.tl.h")) || 230);
  const lanesRef = useRef<HTMLDivElement>(null);
  const bodyRef = useRef<HTMLDivElement>(null);

  const rows = useMemo(() => {
    if (!model) return [];
    const out: string[] = [];
    const hiddenUnder = (id: string) => {
      let p = model.parent.get(id);
      while (p) {
        if (collapsed.has(p)) return true;
        p = model.parent.get(p);
      }
      return false;
    };
    for (const id of model.order) if (!hiddenUnder(id)) out.push(id);
    return out;
  }, [model, collapsed]);

  // Replay loop.
  useEffect(() => {
    if (!playing || !model) return;
    let raf = 0;
    let last = performance.now();
    const tick = (now: number) => {
      const dt = now - last;
      last = now;
      const st = useStore.getState();
      const t = (st.replayT ?? model.t0) + dt * st.speed;
      if (t >= model.t1) {
        st.setReplay(model.t1);
        st.setPlaying(false);
        return;
      }
      st.setReplay(t);
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [playing, model]);

  // Keep the selected row in view.
  useEffect(() => {
    if (!selected || !bodyRef.current) return;
    const i = rows.indexOf(selected);
    if (i < 0) return;
    const el = bodyRef.current;
    const top = 24 + i * 24;
    if (top < el.scrollTop + 24 || top > el.scrollTop + el.clientHeight - 30) el.scrollTo({ top: top - el.clientHeight / 2, behavior: document.body.classList.contains("touring") ? "auto" : "smooth" });
  }, [selected, rows]);

  if (!model) return null;
  const { t0, t1 } = model;
  const total = Math.max(1, t1 - t0);
  const pct = (t: number) => `${((t - t0) / total) * 100}%`;
  const step = niceStep(total);
  const ticks: number[] = [];
  for (let t = 0; t <= total; t += step) ticks.push(t);

  const scrubFrom = (clientX: number) => {
    const lane = lanesRef.current?.getBoundingClientRect();
    if (!lane) return;
    const f = Math.min(1, Math.max(0, (clientX - lane.left) / lane.width));
    setReplay(t0 + f * total);
  };

  const startScrub = (e: React.MouseEvent) => {
    setPlaying(false);
    scrubFrom(e.clientX);
    const move = (ev: MouseEvent) => scrubFrom(ev.clientX);
    const up = () => {
      window.removeEventListener("mousemove", move);
      window.removeEventListener("mouseup", up);
    };
    window.addEventListener("mousemove", move);
    window.addEventListener("mouseup", up);
  };

  const startResize = (e: React.MouseEvent) => {
    const y0 = e.clientY;
    const h0 = height;
    const move = (ev: MouseEvent) => {
      const h = Math.min(window.innerHeight * 0.7, Math.max(120, h0 - (ev.clientY - y0)));
      setHeight(h);
      localStorage.setItem("tg.tl.h", String(Math.round(h)));
    };
    const up = () => {
      window.removeEventListener("mousemove", move);
      window.removeEventListener("mouseup", up);
    };
    window.addEventListener("mousemove", move);
    window.addEventListener("mouseup", up);
  };

  const timeScale = Number(model.run.metadata?.["time_scale"] ?? 0);

  return (
    <div className="timeline" style={{ ["--tl-h" as any]: `${height}px` }}>
      <div className="tl-resize" onMouseDown={startResize} />
      <div className="tl-head">
        <span className="tl-title">Timeline</span>
        <button className="icon-btn" title="Restart replay" onClick={() => setReplay(t0)}>
          <SkipBack size={14} />
        </button>
        <button className="btn" title="Play / pause replay (space)" onClick={() => setPlaying(!playing)} style={{ height: 26 }}>
          {playing ? <Pause size={13} /> : <Play size={13} />}
          {playing ? "Pause" : "Replay"}
        </button>
        <div className="speed">
          {SPEEDS.map((s) => (
            <button key={s} className={s === speed ? "on" : ""} onClick={() => setSpeed(s)}>
              {s}×
            </button>
          ))}
        </div>
        <span className="tl-time">
          {replayT != null ? fmtOffset(replayT - t0) : "—"} / {fmtDuration(total)}
        </span>
        {replayT != null && (
          <button className="btn ghost" onClick={() => setReplay(null)} title="Show the complete run">
            <Radio size={13} /> Exit replay
          </button>
        )}
        <div className="grow" />
        {timeScale > 1 && <span className="faint" style={{ fontSize: 11.5 }}>demo recorded at {timeScale}× time compression</span>}
        <span className="faint" style={{ fontSize: 11.5 }}>{rows.length} steps</span>
      </div>
      <div className="tl-body" ref={bodyRef}>
        <div className="tl-grid">
          <div className="tl-axis">
            <div className="label-col" />
            <div className="ticks" ref={lanesRef} onMouseDown={startScrub} title="Drag to scrub through the run">
              {ticks.map((t) => (
                <div key={t} className="tick" style={{ left: `${(t / total) * 100}%` }}>
                  {fmtDuration(t)}
                </div>
              ))}
            </div>
          </div>
          {rows.map((id) => {
            const s = model.spans.get(id)!;
            const k = spanKind(s.kind);
            const Icon = k.icon;
            const d = model.depth.get(id) ?? 0;
            const isGroup = (model.children.get(id)?.length ?? 0) > 0;
            const start = s.started_at ?? t0;
            const end = s.ended_at ?? (s.status === "running" ? Math.max(t1, Date.now()) : start);
            const st = statusAt(s, replayT);
            const color = s.kind === "agent" ? `hsl(${hueFor(s.agent ?? s.name)} 80% 72%)` : k.color;
            const visEnd = replayT != null && st === "running" ? replayT : end;
            const arts = model.artifactsBySpan.get(id) ?? [];
            return (
              <div key={id} className={`tl-row ${selected === id ? "active" : ""}`} onClick={() => select(id, { focus: true })}>
                <div className="label-col" style={{ paddingLeft: 10 + d * 13 }}>
                  <Icon size={12} color={color} style={{ flex: "none" }} />
                  <span className="ellipsis" style={{ color: s.status === "error" ? "#ff9b9b" : undefined, fontWeight: isGroup ? 560 : 400 }}>
                    {s.name}
                  </span>
                  <span className="faint" style={{ marginLeft: "auto", fontSize: 11, flex: "none" }}>
                    {s.ended_at != null && s.started_at != null ? (isEstimated(s) ? "~" : "") + fmtDuration(s.ended_at - s.started_at) : ""}
                  </span>
                </div>
                <div className="lane">
                  <div
                    className={`bar ${isGroup ? "group" : ""} ${isEstimated(s) && !isGroup ? "estimated" : ""} ${s.status === "error" && st !== "running" ? "error" : ""} ${st === "running" ? "running" : ""} ${st === "future" ? "future" : ""}`}
                    style={{ left: pct(start), width: `calc(${pct(Math.max(visEnd, start))} - ${pct(start)})`, ["--c" as any]: color }}
                    title={`${s.name} · ${fmtDuration(end - start)}`}
                  />
                  {arts.map((a) =>
                    a.created_at != null ? (
                      <span key={a.id} className="bar-mark" style={{ left: pct(a.created_at), opacity: replayT != null && a.created_at > replayT ? 0.1 : 1 }} title={a.name}>
                        <Star size={9} fill="currentColor" />
                      </span>
                    ) : null,
                  )}
                </div>
              </div>
            );
          })}
          {replayT != null && (
            <div style={{ position: "absolute", top: 0, bottom: 0, left: "var(--tl-label, 280px)", right: 0, pointerEvents: "none" }}>
              <div className="playhead" style={{ left: pct(replayT) }} />
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

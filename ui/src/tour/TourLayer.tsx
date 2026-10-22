import { createPortal } from "react-dom";
import { BrandMark } from "../components/Brand";
import { useTour } from "./engine";

function Cursor() {
  const c = useTour((s) => s.cursor);
  if (!c.visible) return null;
  return (
    <div className="tour-cursor" style={{ transform: `translate(${c.x}px, ${c.y}px) scale(${c.down ? 0.86 : 1})` }}>
      <svg width="26" height="30" viewBox="0 0 26 30" aria-hidden>
        <defs>
          <filter id="tcs" x="-50%" y="-50%" width="200%" height="200%">
            <feDropShadow dx="0" dy="2" stdDeviation="2.2" floodColor="#000" floodOpacity="0.55" />
          </filter>
        </defs>
        <path d="M3 2 L3 23 L8.6 17.6 L12.4 26.4 L16.2 24.8 L12.5 16.2 L20.2 16.2 Z" fill="#fff" stroke="#111" strokeWidth="1.4" strokeLinejoin="round" filter="url(#tcs)" />
      </svg>
    </div>
  );
}

function Ripples() {
  const r = useTour((s) => s.ripples);
  return (
    <>
      {r.map((p) => (
        <span key={p.id} className="tour-ripple" style={{ left: p.x, top: p.y }} />
      ))}
    </>
  );
}

function CaptionBar() {
  const c = useTour((s) => s.caption);
  if (!c) return null;
  return (
    <div key={c.id} className={`tour-caption ${c.visible ? "in" : "out"}`}>
      {c.kicker && <div className="tour-kicker">{c.kicker}</div>}
      <div className="tour-text">{c.text}</div>
    </div>
  );
}

function Card() {
  const card = useTour((s) => s.card);
  if (!card) return null;
  return (
    <div className={`tour-card ${card.visible ? "in" : "out"}`}>
      <div className="tour-glow" />
      {card.kind === "intro" ? (
        <div className="tour-card-body">
          <div className="tour-line l1">The next frontier in AI isn’t intelligence.</div>
          <div className="tour-line l2">It’s trust.</div>
        </div>
      ) : (
        <div className="tour-card-body">
          <div className="tour-brand">
            <BrandMark className="tour-mark" />
            <span>Traceglass</span>
          </div>
          <div className="tour-line l3">Inspect what your agents did.</div>
          <div className="tour-line l4">Evaluate how well they did it.</div>
          <div className="tour-foot">Local-first · Claude Code · Kiro · LangGraph · OpenTelemetry</div>
        </div>
      )}
    </div>
  );
}

function Badge() {
  const b = useTour((s) => s.badge);
  if (!b) return null;
  return (
    <div className="tour-badge">
      <span className="dot live" style={{ ["--c" as any]: "#ff5f57" }} />
      {b}
    </div>
  );
}

function Dip() {
  const d = useTour((s) => s.dip);
  return d > 0 ? <div className="tour-dip" style={{ opacity: d }} /> : null;
}

export function TourLayer() {
  const active = useTour((s) => s.active);
  if (!active) return null;
  return createPortal(
    <div className="tour-layer">
      <Badge />
      <CaptionBar />
      <Ripples />
      <Cursor />
      <Dip />
      <Card />
    </div>,
    document.body,
  );
}

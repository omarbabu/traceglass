/**
 * A tiny cinematic engine for scripted product tours (and the demo video).
 *
 * Everything is driven by setTimeout / requestAnimationFrame / performance.now, so the
 * recorder can virtualize time and render every frame deterministically at 60 fps — and the
 * same tour plays in real time in a normal browser for previewing.
 */
import { create } from "zustand";

export const ease = {
  inOut: (t: number) => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2),
  out: (t: number) => 1 - Math.pow(1 - t, 3),
  inOutQuint: (t: number) => (t < 0.5 ? 16 * t ** 5 : 1 - Math.pow(-2 * t + 2, 5) / 2),
};

export const wait = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

/** Animate a value from `from` to `to` over `ms`, calling `apply` every frame. */
export function tween(ms: number, apply: (t: number) => void, fn = ease.inOut): Promise<void> {
  return new Promise((resolve) => {
    const t0 = performance.now();
    const step = () => {
      const t = Math.min(1, (performance.now() - t0) / ms);
      apply(fn(t));
      if (t < 1) requestAnimationFrame(step);
      else resolve();
    };
    requestAnimationFrame(step);
  });
}

export interface Caption {
  id: number;
  kicker?: string;
  text: string;
  visible: boolean;
}

export interface TitleCard {
  kind: "intro" | "outro";
  visible: boolean;
}

interface TourState {
  active: boolean;
  cursor: { x: number; y: number; visible: boolean; down: boolean };
  ripples: { id: number; x: number; y: number }[];
  caption: Caption | null;
  card: TitleCard | null;
  badge: string | null;
  dip: number; // 0..1 black dip for scene cuts
  set(p: Partial<TourState>): void;
}

export const useTour = create<TourState>((set) => ({
  active: false,
  cursor: { x: 0, y: 0, visible: false, down: false },
  ripples: [],
  caption: null,
  card: null,
  badge: null,
  dip: 0,
  set: (p) => set(p),
}));

const st = () => useTour.getState();
let rippleId = 0;
let captionId = 0;

// ---------------------------------------------------------------- camera (CSS transform)
const cam = { s: 1, tx: 0, ty: 0 };

function camEl(): HTMLElement | null {
  return document.querySelector<HTMLElement>(".app");
}

function applyCam() {
  const el = camEl();
  if (!el) return;
  el.style.transformOrigin = "0 0";
  el.style.transform = `translate3d(${cam.tx}px, ${cam.ty}px, 0) scale(${cam.s})`;
}

function clampCam(s: number, tx: number, ty: number) {
  const W = window.innerWidth;
  const H = window.innerHeight;
  return { s, tx: Math.min(0, Math.max(W - W * s, tx)), ty: Math.min(0, Math.max(H - H * s, ty)) };
}

/** Untransformed (layout) rect of an element. */
export function layoutRect(el: Element) {
  const r = el.getBoundingClientRect();
  return { x: (r.left - cam.tx) / cam.s, y: (r.top - cam.ty) / cam.s, w: r.width / cam.s, h: r.height / cam.s };
}

/** Zoom so that layout point (cx, cy) sits at the viewport centre at scale s. */
export function camera(s: number, cx: number, cy: number, ms = 1100, fn = ease.inOutQuint) {
  const W = window.innerWidth;
  const H = window.innerHeight;
  const to = clampCam(s, W / 2 - cx * s, H / 2 - cy * s);
  const from = { ...cam };
  return tween(
    ms,
    (t) => {
      cam.s = from.s + (to.s - from.s) * t;
      cam.tx = from.tx + (to.tx - from.tx) * t;
      cam.ty = from.ty + (to.ty - from.ty) * t;
      applyCam();
    },
    fn,
  );
}

export function cameraOn(el: Element | null, s: number, ms = 1100, dx = 0, dy = 0) {
  if (!el) return Promise.resolve();
  const r = layoutRect(el);
  return camera(s, r.x + r.w / 2 + dx, r.y + r.h / 2 + dy, ms);
}

export const cameraReset = (ms = 1000) => camera(1, window.innerWidth / 2, window.innerHeight / 2, ms);

// ---------------------------------------------------------------- cursor
export async function cursorTo(target: Element | { x: number; y: number } | null, ms = 900, offset = { x: 0, y: 0 }) {
  if (!target) return;
  let x: number;
  let y: number;
  if (target instanceof Element) {
    const r = target.getBoundingClientRect();
    x = r.left + Math.min(r.width / 2, 60) + offset.x;
    y = r.top + r.height / 2 + offset.y;
  } else {
    x = target.x;
    y = target.y;
  }
  const from = { ...st().cursor };
  if (!from.visible) {
    st().set({ cursor: { x: x + 140, y: y + 90, visible: true, down: false } });
    from.x = x + 140;
    from.y = y + 90;
  }
  // A gentle arc reads as a human hand, not a robot.
  const mx = (from.x + x) / 2 + (y - from.y) * 0.12;
  const my = (from.y + y) / 2 - (x - from.x) * 0.12;
  await tween(
    ms,
    (t) => {
      const u = 1 - t;
      st().set({ cursor: { ...st().cursor, x: u * u * from.x + 2 * u * t * mx + t * t * x, y: u * u * from.y + 2 * u * t * my + t * t * y } });
    },
    ease.inOut,
  );
}

export async function click(el: Element | null) {
  const c = st().cursor;
  st().set({ cursor: { ...c, down: true }, ripples: [...st().ripples, { id: ++rippleId, x: c.x, y: c.y }] });
  await wait(110);
  if (el) (el as HTMLElement).click();
  st().set({ cursor: { ...st().cursor, down: false } });
  const id = rippleId;
  setTimeout(() => st().set({ ripples: st().ripples.filter((r) => r.id !== id) }), 700);
  await wait(160);
}

export const hideCursor = () => st().set({ cursor: { ...st().cursor, visible: false } });

// ---------------------------------------------------------------- captions, cards, badge, cuts
export async function caption(text: string, kicker?: string) {
  const prev = st().caption;
  if (prev?.visible) {
    st().set({ caption: { ...prev, visible: false } });
    await wait(260);
  }
  st().set({ caption: { id: ++captionId, text, kicker, visible: true } });
}

export async function hideCaption() {
  const c = st().caption;
  if (c) st().set({ caption: { ...c, visible: false } });
  await wait(300);
}

export async function card(kind: "intro" | "outro", holdMs: number, keep = false) {
  st().set({ card: { kind, visible: true } });
  await wait(holdMs);
  if (!keep) {
    st().set({ card: { kind, visible: false } });
    await wait(700);
    st().set({ card: null });
  }
}

export const badge = (text: string | null) => st().set({ badge: text });

/** A quick dip to black between scenes; `during` runs while the screen is dark. */
export async function cut(during: () => void | Promise<void>, ms = 380) {
  await tween(ms / 2, (t) => st().set({ dip: t }), ease.inOut);
  await during();
  await wait(60);
  await tween(ms / 2, (t) => st().set({ dip: 1 - t }), ease.inOut);
}

/** Smoothly scroll an element (e.g. the conversation) by `dy` pixels. */
export function scrollBy(el: Element | null, dy: number, ms: number) {
  if (!el) return Promise.resolve();
  const from = el.scrollTop;
  return tween(ms, (t) => (el.scrollTop = from + dy * t), ease.inOut);
}

// ---------------------------------------------------------------- DOM helpers
export function q(selector: string, text?: string | RegExp): HTMLElement | null {
  const els = Array.from(document.querySelectorAll<HTMLElement>(selector));
  if (text == null) return els[0] ?? null;
  return els.find((e) => (typeof text === "string" ? e.textContent?.includes(text) : text.test(e.textContent ?? ""))) ?? null;
}

export async function until(fn: () => unknown, timeoutMs = 8000, every = 50): Promise<void> {
  const t0 = performance.now();
  while (!fn()) {
    if (performance.now() - t0 > timeoutMs) return;
    await wait(every);
  }
}

declare global {
  interface Window {
    __tgTour?: { ready: boolean; done: boolean; duration?: number; error?: string };
  }
}

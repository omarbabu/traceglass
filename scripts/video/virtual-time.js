/*
 * Virtual time for deterministic screen recording.
 *
 * Injected with page.addInitScript() before any page script runs. Replaces the page's clocks and
 * schedulers so nothing advances unless the recorder calls window.__tgAdvance(ms):
 *
 *   performance.now · Date.now · new Date() · setTimeout/clearTimeout · setInterval/clearInterval ·
 *   requestAnimationFrame/cancelAnimationFrame · requestIdleCallback · CSS/Web Animations
 *
 * Real macrotasks (MessageChannel, used by React's scheduler) and microtasks are left alone; the
 * recorder lets them drain after every advance so React commits and effects run between frames.
 */
(() => {
  if (window.__tgVirtualTime) return;

  const real = {
    setTimeout: window.setTimeout.bind(window),
    clearTimeout: window.clearTimeout.bind(window),
    raf: window.requestAnimationFrame ? window.requestAnimationFrame.bind(window) : null,
    Date: window.Date,
  };
  const RealDate = real.Date;
  const EPOCH = RealDate.UTC(2026, 9, 21, 17, 0, 0); // 2026-10-21T17:00:00Z
  let now = 0; // virtual milliseconds since load

  // ---- clocks ----------------------------------------------------------------------------
  try {
    Object.defineProperty(performance, "now", { value: () => now, configurable: true, writable: true });
  } catch {
    performance.now = () => now;
  }

  function VDate(...args) {
    if (!new.target) return new RealDate(EPOCH + now).toString();
    return args.length ? new RealDate(...args) : new RealDate(EPOCH + now);
  }
  VDate.prototype = RealDate.prototype;
  VDate.now = () => EPOCH + Math.floor(now);
  VDate.UTC = RealDate.UTC;
  VDate.parse = RealDate.parse;
  Object.defineProperty(VDate, "name", { value: "Date" });
  window.Date = VDate;

  // ---- timers ----------------------------------------------------------------------------
  let nextTimer = 1;
  let order = 0;
  const timers = new Map(); // id -> { at, seq, fn, args, every }

  const toFn = (fn) => (typeof fn === "function" ? fn : () => (0, eval)(String(fn)));
  window.setTimeout = function (fn, delay, ...args) {
    const id = nextTimer++;
    timers.set(id, { at: now + Math.max(0, Number(delay) || 0), seq: order++, fn: toFn(fn), args, every: null });
    return id;
  };
  window.setInterval = function (fn, delay, ...args) {
    const id = nextTimer++;
    const every = Math.max(1, Number(delay) || 0);
    timers.set(id, { at: now + every, seq: order++, fn: toFn(fn), args, every });
    return id;
  };
  window.clearTimeout = window.clearInterval = (id) => {
    timers.delete(id);
  };

  // ---- frames ----------------------------------------------------------------------------
  let nextFrame = 1;
  let frameQueue = new Map();
  window.requestAnimationFrame = (cb) => {
    const id = nextFrame++;
    frameQueue.set(id, cb);
    return id;
  };
  window.cancelAnimationFrame = (id) => {
    frameQueue.delete(id);
  };
  window.requestIdleCallback = (cb) => window.setTimeout(() => cb({ didTimeout: false, timeRemaining: () => 12 }), 1);
  window.cancelIdleCallback = (id) => window.clearTimeout(id);

  // ---- CSS / Web Animations --------------------------------------------------------------
  const born = new WeakMap(); // Animation -> virtual time first seen
  function syncAnimations() {
    if (!document.getAnimations) return;
    for (const a of document.getAnimations()) {
      if (!born.has(a)) born.set(a, now);
      const t = now - born.get(a);
      try {
        const end = a.effect && a.effect.getComputedTiming ? a.effect.getComputedTiming().endTime : Infinity;
        if (Number.isFinite(end) && t >= end) {
          a.finish();
          continue;
        }
        if (a.playState !== "paused") a.pause();
        a.currentTime = t;
      } catch {
        /* animation removed mid-iteration */
      }
    }
  }

  const microtasks = async (n = 4) => {
    for (let i = 0; i < n; i++) await Promise.resolve();
  };
  const realTick = () => new Promise((r) => real.setTimeout(r, 0));
  const realFrame = () => new Promise((r) => (real.raf ? real.raf(() => r()) : real.setTimeout(r, 16)));

  function nextDue(limit) {
    let best = null;
    let bestId = 0;
    for (const [id, t] of timers) {
      if (t.at > limit) continue;
      if (!best || t.at < best.at || (t.at === best.at && t.seq < best.seq)) {
        best = t;
        bestId = id;
      }
    }
    return best ? [bestId, best] : null;
  }

  let errors = 0;
  function run(fn, ...args) {
    try {
      fn(...args);
    } catch (e) {
      errors++;
      console.error("[virtual-time]", e);
    }
  }

  /**
   * Advance virtual time by `ms`: fire due timers in order (including ones they schedule that are
   * due by the target), run one batch of animation-frame callbacks at the new time, sync CSS
   * animations, then let real macrotasks/microtasks and one real rendering frame settle.
   */
  async function advance(ms) {
    // Snap to a microsecond grid so repeated 1000/60 steps never drift below a timer's due time.
    const target = Math.round((now + Math.max(0, ms)) * 1000) / 1000;
    let fired = 0;
    for (;;) {
      const due = nextDue(target);
      if (!due) break;
      const [id, t] = due;
      now = Math.max(now, t.at);
      if (t.every != null) {
        t.at += t.every;
        t.seq = order++;
      } else timers.delete(id);
      run(t.fn, ...t.args);
      await microtasks(2);
      if (++fired > 20000) {
        console.warn("[virtual-time] >20000 timers in one advance — runaway polling?");
        break;
      }
    }
    now = target;
    const cbs = [...frameQueue.values()];
    frameQueue = new Map();
    for (const cb of cbs) run(cb, now);
    await microtasks(4);
    // Let React's MessageChannel scheduler commit, then a real rendering update (layout,
    // ResizeObserver) and any React work it triggers, so the screenshot shows a settled frame.
    await realTick();
    await realTick();
    syncAnimations();
    await realFrame();
    await realTick();
    await realFrame();
    syncAnimations();
    return { now, fired, pendingTimers: timers.size, errors };
  }

  window.__tgAdvance = advance;
  window.__tgVirtualTime = { get now() { return now; }, epoch: EPOCH, get timers() { return timers.size; }, syncAnimations };
})();

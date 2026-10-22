# Deterministic demo recorder

**The Traceglass demo video, end to end:** `scripts/video/make-demo.sh` runs two real headless Claude Code sessions (two models, same task) on the sample repo in `demo-project/`, imports and sanitizes them (`sanitize.py`), assembles the tour HTML (`build_tour.py`), plays the scripted tour (`ui/src/tour/main.ts`) and records it with `record.mjs` into `docs/media/`. `--skip-runs` re-records from the last runs (e.g. after a UI change). Preview the tour live by opening the tour HTML with `#/?tour=main` in Chrome.

---

Renders a web page **frame by frame on a virtual clock**, captures every frame at 2× device scale, and encodes a 1080p60 H.264 MP4 (plus an optional square cut and a README GIF). Capture can be as slow as it likes: every frame is exactly 1/60 s apart in page time, so motion is perfectly smooth and two renders are byte-identical.

```bash
cd scripts/video && npm install            # playwright-core (uses your installed Google Chrome)
node record.mjs --url "file:///…/tour.html#/?tour=hero" \
  --out ../../docs/media/traceglass-demo.mp4 --gif ../../docs/media/traceglass-demo.gif --square
npm test                                   # determinism + smoothness checks
node test/real-ui.mjs file:///tmp/export.html   # React Flow / ELK / replay under virtual time
```

## How it works

1. `virtual-time.js` is injected before any page script and replaces the page's clocks and schedulers: `performance.now`, `Date.now` / `new Date()` (epoch 2026-10-21T17:00:00Z + virtual ms), `setTimeout`/`setInterval`, `requestAnimationFrame`, `requestIdleCallback`. Nothing advances on its own.
2. `window.__tgAdvance(ms)` moves the clock: due timers fire in order (including ones scheduled while firing), one batch of animation-frame callbacks runs at the new time, every CSS / Web Animation is paused and set to its virtual `currentTime`, then real macrotasks (React's MessageChannel scheduler), microtasks and two real rendering frames (layout, ResizeObserver) settle.
3. The recorder pre-rolls (uncaptured) until `window.__tgTour.ready`, then loops `advance(1000/60) → capture` until `window.__tgTour.done` (or `--max-seconds`). Frames come from CDP `Page.captureScreenshot` with an explicit `clip.scale` (needed to get device pixels).
4. Frames are **streamed** into ffmpeg (`image2pipe`) as they are captured, so a 45 s 2× recording needs ~100 MB of disk rather than gigabytes of PNGs. Pass `--keep-frames` or `--frames-dir` to write frames to disk instead. ffmpeg encodes: lanczos downscale to 1920×1080, `libx264` High, CRF 16, `preset slow`, yuv420p, bt709 primaries/transfer/matrix (tagged in both the container and the bitstream), limited range, `+faststart`. `--square` center-crops 1080×1080. `--gif` builds a palette-optimised GIF (960 px @ 20 fps, stepping down until ≤ 12 MB).

## Flags

| flag | default | |
|---|---|---|
| `--url URL` | — | page to record (opaque; usually a `file://` export with `#/…?tour=…`) |
| `--out FILE.mp4` | — | 1920×1080 60 fps H.264 |
| `--square` | off | also write `FILE-square.mp4` (1080×1080) |
| `--gif FILE.gif` | — | README-sized GIF |
| `--viewport WxH` | `1440x810` | CSS pixels |
| `--scale N` | `2` | device scale (frames are W·N × H·N) |
| `--fps N` | `60` | |
| `--max-seconds S` | `60` | hard stop |
| `--ready-immediately S` | — | for pages without `__tgTour`: ready after S virtual seconds, stop at `--max-seconds` |
| `--preroll S` | `15` | max virtual seconds to wait for `ready` |
| `--frames-dir DIR` / `--keep-frames` | temp, deleted | keep the PNGs |
| `--no-encode` | | capture only |

Env: `CHROME_PATH`, `FFMPEG`.

## Page contract

```js
window.__tgTour = { ready: false, done: false, duration: 42000 /* ms, for the ETA */ };
```

Rules for pages recorded this way:

- **Schedule with the virtualised APIs** (`requestAnimationFrame`, `setTimeout`, `performance.now`, CSS animations/transitions). They're deterministic. `event.timeStamp` is real time — don't read it.
- **No smooth scrolling by the browser.** `scrollTo({behavior: "smooth"})` and `scroll-behavior: smooth` run on real time on the compositor; animate `scrollTop` yourself in rAF.
- **No `:hover` / `:focus` styling to show interaction** — the recorder never moves a real mouse. Use classes (e.g. a fake cursor that adds `.is-hover`).
- **No text carets.** Caret blinking runs on real time; use `caret-color: transparent` during tours.
- **No network.** Loading is not synchronised with frames; embed data (the `traceglass export` HTML does).
- **Polling is fine, but cheap.** Virtual time jumps 16.7 ms per frame and every due timer fires; a runaway `setTimeout(…, 0)` loop is cut off at 20,000 firings per advance with a warning.
- Signal `ready` only after layout/fit has settled, and `done` after the last shot's hold.

## Performance (this machine, Chrome headless, 2880×1620 frames)

- Traceglass UI: ~12 frames/s captured → a 45 s / 2,700-frame video in ~4 min, encode ~1 min.
- Simple test page at 1280×720: ~15–20 frames/s.

## Troubleshooting

- **Frames are 1× size** — Chrome ignores `deviceScaleFactor` for raw CDP screenshots without `clip.scale`; the recorder sets it.
- **Nothing moves** — the page probably drives motion from real time (event timestamps, compositor scroll, `transitionend` waits on a real timer). See the rules above.
- **"page never reported ready"** — set `window.__tgTour.ready = true` or pass `--ready-immediately`.
- **Different frames between runs** — something reads real time: check `new Date(x)` built from real data is fine; caret blink, media elements, and `<video>`/`<canvas>` driven by real clocks are not.

#!/usr/bin/env node
/**
 * Deterministic 60 fps recorder: renders a page frame-by-frame on a virtual clock, captures each
 * frame at 2× device scale over CDP, and encodes a 1080p60 H.264 MP4 (plus optional square cut and
 * README GIF). Capture speed doesn't matter — every frame is exactly 1/fps apart in page time.
 *
 *   node scripts/video/record.mjs --url file:///…/tour.html#/?tour=hero --out docs/media/traceglass-demo.mp4 \
 *        [--gif docs/media/traceglass-demo.gif] [--square] [--viewport 1440x810] [--scale 2] [--fps 60] \
 *        [--max-seconds 60] [--ready-immediately SECONDS] [--keep-frames] [--frames-dir DIR] [--no-encode]
 */
import { spawn, spawnSync } from "node:child_process";
import { once } from "node:events";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, statSync, writeFileSync } from "node:fs";
import { writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright-core";

const HERE = dirname(fileURLToPath(import.meta.url));
const CHROME = process.env.CHROME_PATH || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const FFMPEG = process.env.FFMPEG || (existsSync("/opt/homebrew/bin/ffmpeg") ? "/opt/homebrew/bin/ffmpeg" : "ffmpeg");

function parseArgs(argv) {
  const o = { viewport: "1440x810", scale: 2, fps: 60, maxSeconds: 60, keepFrames: false, square: false, encode: true, preroll: 15 };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    const v = () => argv[++i];
    if (a === "--url") o.url = v();
    else if (a === "--out") o.out = v();
    else if (a === "--gif") o.gif = v();
    else if (a === "--square") o.square = true;
    else if (a === "--viewport") o.viewport = v();
    else if (a === "--scale") o.scale = Number(v());
    else if (a === "--fps") o.fps = Number(v());
    else if (a === "--max-seconds") o.maxSeconds = Number(v());
    else if (a === "--ready-immediately") o.readyAfter = Number(v());
    else if (a === "--keep-frames") o.keepFrames = true;
    else if (a === "--frames-dir") o.framesDir = v();
    else if (a === "--no-encode") o.encode = false;
    else if (a === "--preroll") o.preroll = Number(v());
    else if (a === "--quiet") o.quiet = true;
    else if (a === "-h" || a === "--help") {
      console.log(readFileSync(fileURLToPath(import.meta.url), "utf8").split("*/")[0]);
      process.exit(0);
    } else throw new Error(`unknown flag ${a}`);
  }
  if (!o.url) throw new Error("--url is required");
  if (!o.out && o.encode) throw new Error("--out is required (or --no-encode)");
  const [w, h] = o.viewport.split("x").map(Number);
  o.width = w;
  o.height = h;
  return o;
}

const fmtTime = (s) => (s < 60 ? `${s.toFixed(0)}s` : `${Math.floor(s / 60)}m${String(Math.round(s % 60)).padStart(2, "0")}s`);

export async function captureFrames(o) {
  // Streaming (the default for --out without --keep-frames/--frames-dir) pipes PNGs straight into
  // ffmpeg, so a 45 s 2× capture needs ~100 MB of disk instead of several GB of frames.
  const stream = o.out && o.encode && !o.keepFrames && !o.framesDir;
  const framesDir = stream ? null : o.framesDir ? resolve(o.framesDir) : mkdtempSync(join(tmpdir(), "tg-frames-"));
  if (framesDir) mkdirSync(framesDir, { recursive: true });
  let ffproc = null;
  if (stream) {
    mkdirSync(dirname(resolve(o.out)), { recursive: true });
    ffproc = spawn(
      FFMPEG,
      ["-hide_banner", "-loglevel", "error", "-y", "-f", "image2pipe", "-framerate", String(o.fps), "-c:v", "png", "-i", "-",
        "-vf", `scale=1920:1080:${SCALE},format=yuv420p,${TAGS}`, "-r", String(o.fps), ...X264, resolve(o.out)],
      { stdio: ["pipe", "inherit", "inherit"] },
    );
  }
  const browser = await chromium.launch({
    executablePath: CHROME,
    headless: true,
    args: ["--force-color-profile=srgb", "--hide-scrollbars", "--font-render-hinting=none", "--disable-lcd-text"],
  });
  const context = await browser.newContext({ viewport: { width: o.width, height: o.height }, deviceScaleFactor: o.scale, colorScheme: "dark" });
  await context.addInitScript({ content: readFileSync(join(HERE, "virtual-time.js"), "utf8") });
  if (o.readyAfter != null) {
    await context.addInitScript({
      content: `window.__tgTour = window.__tgTour || { ready: false, done: false, external: true };`,
    });
  }
  const page = await context.newPage();
  const logs = [];
  page.on("console", (m) => (m.type() === "error" || m.type() === "warning") && logs.push(`[${m.type()}] ${m.text()}`));
  page.on("pageerror", (e) => logs.push(`[pageerror] ${e.message}`));
  await page.goto(o.url, { waitUntil: "load" });
  const cdp = await context.newCDPSession(page);
  const step = 1000 / o.fps;

  // Pre-roll: advance (uncaptured) until the page says it's ready.
  const preMax = Math.round((o.preroll * 1000) / step);
  let pre = 0;
  for (; pre < preMax; pre++) {
    const state = await page.evaluate(
      async ([dt, readyAfter]) => {
        await window.__tgAdvance(dt);
        const vt = window.__tgVirtualTime.now;
        if (readyAfter != null && window.__tgTour?.external && vt >= readyAfter * 1000) window.__tgTour.ready = true;
        return { ready: Boolean(window.__tgTour?.ready), now: vt };
      },
      [step, o.readyAfter ?? null],
    );
    if (state.ready) break;
  }
  if (pre >= preMax) logs.push(`[recorder] page never reported ready within ${o.preroll}s of virtual time — recording anyway`);
  const startVirtual = await page.evaluate(() => window.__tgVirtualTime.now);

  const maxFrames = Math.round(o.maxSeconds * o.fps);
  const writes = [];
  const t0 = Date.now();
  let i = 0;
  let lastLog = 0;
  for (; i < maxFrames; i++) {
    const target = startVirtual + i * step;
    const done = await page.evaluate(async (target) => {
      const dt = target - window.__tgVirtualTime.now;
      if (dt > 0) await window.__tgAdvance(dt);
      else window.__tgVirtualTime.syncAnimations();
      return Boolean(window.__tgTour?.done);
    }, target);
    // An explicit clip with scale is what makes CDP return device pixels (otherwise it returns CSS-px size).
    const { data } = await cdp.send("Page.captureScreenshot", {
      format: "png",
      optimizeForSpeed: true,
      fromSurface: true,
      captureBeyondViewport: false,
      clip: { x: 0, y: 0, width: o.width, height: o.height, scale: o.scale },
    });
    if (ffproc) {
      if (!ffproc.stdin.write(Buffer.from(data, "base64"))) await once(ffproc.stdin, "drain");
    } else {
      writes.push(writeFile(join(framesDir, `${String(i).padStart(6, "0")}.png`), Buffer.from(data, "base64")));
      if (writes.length > 24) await writes.shift();
    }
    if (!o.quiet && (Date.now() - lastLog > 1000 || done)) {
      lastLog = Date.now();
      const el = (Date.now() - t0) / 1000;
      const fpsCap = (i + 1) / el;
      const dur = Number((await page.evaluate(() => window.__tgTour?.duration)) || 0);
      const totalFrames = dur ? Math.min(maxFrames, Math.ceil((dur / 1000) * o.fps)) : maxFrames;
      const eta = Math.max(0, (totalFrames - i - 1) / fpsCap);
      process.stdout.write(`\r  ◆ frame ${i + 1}/${totalFrames}  ${(i / o.fps).toFixed(2)}s  ${fpsCap.toFixed(1)} fps  ETA ${fmtTime(eta)}   `);
    }
    if (done) {
      i++;
      break;
    }
  }
  await Promise.all(writes);
  if (ffproc) {
    ffproc.stdin.end();
    const [code] = await once(ffproc, "exit");
    if (code !== 0) throw new Error("ffmpeg failed (stream)");
  }
  if (!o.quiet) process.stdout.write("\n");
  const elapsed = (Date.now() - t0) / 1000;
  await browser.close();
  return { framesDir, streamed: Boolean(ffproc), frames: i, elapsed, captureFps: i / elapsed, logs };
}

function ff(args, label) {
  const r = spawnSync(FFMPEG, ["-hide_banner", "-loglevel", "error", "-y", ...args], { stdio: ["ignore", "inherit", "inherit"] });
  if (r.status !== 0) throw new Error(`ffmpeg failed (${label})`);
}

const COLOR = ["-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv"];
const X264 = [
  "-c:v", "libx264", "-profile:v", "high", "-preset", "slow", "-crf", "16", "-pix_fmt", "yuv420p",
  "-x264-params", "colorprim=bt709:transfer=bt709:colormatrix=bt709:fullrange=off",
  "-movflags", "+faststart", ...COLOR,
];
const TAGS = "setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709:range=tv";
const SCALE = "flags=lanczos+accurate_rnd+full_chroma_int:out_color_matrix=bt709:out_range=tv";

export function encode(framesDir, o) {
  // When frames were streamed, the 1080p master already exists; derive the other cuts from it.
  const input = framesDir ? ["-framerate", String(o.fps), "-i", join(framesDir, "%06d.png")] : ["-i", resolve(o.out)];
  const outs = [];
  if (o.out && !framesDir) {
    outs.push(resolve(o.out));
    if (o.square) {
      const sq = resolve(o.out).replace(/\.mp4$/, "") + "-square.mp4";
      ff([...input, "-vf", `crop=1080:1080,format=yuv420p,${TAGS}`, "-r", String(o.fps), ...X264, sq], "square");
      outs.push(sq);
    }
  } else if (o.out) {
    mkdirSync(dirname(resolve(o.out)), { recursive: true });
    ff([...input, "-vf", `scale=1920:1080:${SCALE},format=yuv420p,${TAGS}`, "-r", String(o.fps), ...X264, resolve(o.out)], "mp4");
    outs.push(resolve(o.out));
    if (o.square) {
      const sq = resolve(o.out).replace(/\.mp4$/, "") + "-square.mp4";
      ff([...input, "-vf", `scale=-2:1080:${SCALE},crop=1080:1080,format=yuv420p,${TAGS}`, "-r", String(o.fps), ...X264, sq], "square");
      outs.push(sq);
    }
  }
  if (o.gif) {
    mkdirSync(dirname(resolve(o.gif)), { recursive: true });
    // Try progressively smaller settings until the GIF fits the README budget.
    for (const [w, fps] of [[960, 20], [880, 18], [800, 15], [720, 12]]) {
      const vf = `fps=${fps},scale=${w}:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=256:stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle`;
      ff([...input, "-vf", vf, "-loop", "0", resolve(o.gif)], "gif");
      if (statSync(resolve(o.gif)).size <= 12 * 1024 * 1024) break;
    }
    outs.push(resolve(o.gif));
  }
  return outs;
}

async function main() {
  const o = parseArgs(process.argv.slice(2));
  console.log(`\n  ◆ Recording ${o.url}\n    ${o.width}×${o.height} @${o.scale}× → ${o.width * o.scale}×${o.height * o.scale} frames, ${o.fps} fps (virtual time)\n`);
  const cap = await captureFrames(o);
  console.log(`  ✓ ${cap.frames} frames (${(cap.frames / o.fps).toFixed(2)}s of video) in ${fmtTime(cap.elapsed)} — ${cap.captureFps.toFixed(1)} frames/s`);
  if (cap.logs.length) console.log("  page messages:\n    " + cap.logs.slice(0, 20).join("\n    "));
  if (o.encode) {
    const t = Date.now();
    const outs = encode(cap.framesDir, o);
    for (const f of outs) console.log(`  ✓ ${f}  ${(statSync(f).size / 1024 / 1024).toFixed(1)} MB`);
    console.log(`  encoded in ${fmtTime((Date.now() - t) / 1000)}`);
  }
  if (cap.framesDir && !o.keepFrames && !o.framesDir) rmSync(cap.framesDir, { recursive: true, force: true });
  else if (cap.framesDir) console.log(`  frames kept in ${cap.framesDir}`);
  writeFileSync(join(tmpdir(), "tg-last-recording.json"), JSON.stringify({ ...cap, logs: cap.logs.slice(0, 50) }, null, 2));
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch((e) => {
    console.error("\n  ✗ " + (e.stack || e));
    process.exit(1);
  });
}

// Determinism + smoothness tests for the virtual-time recorder.
import { createHash } from "node:crypto";
import { readFileSync, readdirSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { chromium } from "playwright-core";
import { captureFrames } from "../record.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));
const url = pathToFileURL(join(HERE, "test.html")).href;
const base = { url, width: 640, height: 360, scale: 2, fps: 60, maxSeconds: 3, preroll: 5, quiet: true };
let failures = 0;
const check = (ok, msg) => { console.log(`  ${ok ? "✓" : "✗"} ${msg}`); if (!ok) failures++; };

// 1. Determinism: two independent renders → identical frames.
const hashes = [];
for (const run of [1, 2]) {
  const dir = join(tmpdir(), `tgv-test-${run}`);
  rmSync(dir, { recursive: true, force: true });
  const cap = await captureFrames({ ...base, framesDir: dir });
  const files = readdirSync(dir).sort();
  hashes.push(files.map((f) => createHash("sha1").update(readFileSync(join(dir, f))).digest("hex")));
  console.log(`  run ${run}: ${cap.frames} frames, ${cap.captureFps.toFixed(1)} frames/s at ${base.width * 2}×${base.height * 2}`);
  if (cap.logs.length) console.log("   ", cap.logs.join("\n    "));
}
check(hashes[0].length === hashes[1].length && hashes[0].length >= 120, `same frame count (${hashes[0].length}) and stopped on done (~2 s)`);
const sample = [0, 1, 30, 37, 60, 90, hashes[0].length - 1];
check(sample.every((i) => hashes[0][i] === hashes[1][i]), `sampled frames byte-identical across runs (${sample.join(", ")})`);
const allSame = hashes[0].every((h, i) => h === hashes[1][i]);
check(allSame, `all ${hashes[0].length} frames byte-identical`);
check(new Set(hashes[0]).size === hashes[0].length, "every frame differs from the previous (motion each frame)");

// 2. Smoothness + semantics, measured inside the page.
const browser = await chromium.launch({ executablePath: "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", headless: true });
const ctx = await browser.newContext({ viewport: { width: 640, height: 360 } });
await ctx.addInitScript({ content: readFileSync(join(HERE, "..", "virtual-time.js"), "utf8") });
const page = await ctx.newPage();
await page.goto(url);
const r = await page.evaluate(async () => {
  for (let i = 0; i < 6; i++) await window.__tgAdvance(1000 / 60); // 100 ms → ready
  const out = [];
  for (let i = 0; i < 90; i++) {
    await window.__tgAdvance(1000 / 60);
    const fade = document.getElementById("fade");
    out.push({
      t: performance.now(),
      count: document.getElementById("count").textContent,
      date: document.getElementById("date").textContent,
      opacity: Number(getComputedStyle(fade).opacity),
      spin: getComputedStyle(document.querySelector(".spin")).transform,
    });
  }
  return { out, box: window.__boxLog };
});
const d = r.box.slice(1).map((x, i) => x - r.box[i]);
const expect = 0.3 * (1000 / 60);
check(d.length > 60 && d.every((v) => Math.abs(v - expect) < 1e-3), `rAF box advances ${expect.toFixed(4)} px (±0.001) every frame (${d.length} frames)`);
const at = (ms) => r.out.find((o) => Math.abs(o.t - ms) < 8.4);
check(at(500).count === "2" && at(1000).count === "4", `setInterval(250) counter: ${at(500).count} at 0.5 s, ${at(1000).count} at 1.0 s`);
check(at(1000).date.startsWith("2026-10-21T17:00:00.9") || at(1000).date.startsWith("2026-10-21T17:00:01.0"), `Date follows virtual epoch (${at(1000).date})`);
const op = [600, 850, 1100, 1200].map((ms) => at(ms)?.opacity ?? NaN);
check(op[0] < 0.1 && op[1] > 0.4 && op[1] < 0.6 && op[2] >= 0.99, `CSS transition progresses with virtual time (opacity ${op.map((x) => x.toFixed(2)).join(" → ")})`);
const spins = new Set(r.out.slice(0, 60).map((o) => o.spin));
check(spins.size > 50, `CSS infinite spinner rotates each frame (${spins.size} distinct transforms in 60 frames)`);
await browser.close();
console.log(failures ? `\n  ${failures} check(s) failed` : "\n  all checks passed");
process.exit(failures ? 1 : 0);

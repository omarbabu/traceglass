// Real Traceglass UI under virtual time: ELK layout completes, d3-zoom camera moves animate and settle,
// and the rAF-driven replay advances the playhead deterministically.
// usage: node test/real-ui.mjs file:///tmp/tgv-test.html
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright-core";

const HERE = dirname(fileURLToPath(import.meta.url));
const url = process.argv[2];
const browser = await chromium.launch({ executablePath: "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", headless: true });
const ctx = await browser.newContext({ viewport: { width: 1440, height: 810 }, deviceScaleFactor: 1 });
await ctx.addInitScript({ content: readFileSync(join(HERE, "..", "virtual-time.js"), "utf8") });
const page = await ctx.newPage();
const errors = [];
page.on("pageerror", (e) => errors.push(e.message));
await page.goto(url);
let failures = 0;
const check = (ok, msg) => { console.log(`  ${ok ? "✓" : "✗"} ${msg}`); if (!ok) failures++; };
const adv = (ms) => page.evaluate(async (ms) => { const n = Math.round(ms / (1000 / 60)); for (let i = 0; i < n; i++) await window.__tgAdvance(1000 / 60); return window.__tgVirtualTime.now; }, ms);
const viewport = () => page.evaluate(() => getComputedStyle(document.querySelector(".react-flow__viewport")).transform);

await adv(2000);
const nodes = await page.evaluate(() => document.querySelectorAll(".react-flow__node").length);
check(nodes > 10, `ELK layout completed under virtual time (${nodes} nodes rendered)`);
const before = await viewport();
await page.keyboard.press("a"); // jump to final answer → animated setCenter (d3-zoom transition)
const steps = [];
for (let i = 0; i < 40; i++) { await adv(1000 / 60); steps.push(await viewport()); }
const distinct = new Set(steps.slice(0, 20)).size;
const first = steps.findIndex((v) => v !== before);
check(first >= 0 && first <= 6, `camera starts moving within 6 frames (frame ${first}; the UI waits 60 ms before centring)`);
check(distinct >= 12, `camera animates smoothly over frames (${distinct} distinct transforms in 20 frames)`);
check(steps[38] === steps[39], "camera settles (transition completes in virtual time)");
await page.keyboard.press("Escape");
await adv(200);
await page.keyboard.press(" "); // replay: rAF loop on performance.now
const ph = [];
for (let i = 0; i < 30; i++) { await adv(1000 / 60); ph.push(await page.evaluate(() => document.querySelector(".playhead")?.style.left ?? null)); }
const pv = ph.filter(Boolean).map((x) => parseFloat(x));
const deltas = pv.slice(1).map((x, i) => x - pv[i]);
check(pv.length >= 25 && deltas.every((d) => d > 0), `replay playhead advances every frame (${pv.length} frames)`);
check(Math.max(...deltas) - Math.min(...deltas) < 1e-3, `playhead speed is constant (Δ ${Math.min(...deltas).toFixed(4)}–${Math.max(...deltas).toFixed(4)} %/frame)`);
check(errors.length === 0, `no page errors${errors.length ? ": " + errors.join("; ") : ""}`);
await browser.close();
console.log(failures ? `\n  ${failures} check(s) failed` : "\n  all checks passed");
process.exit(failures ? 1 : 0);

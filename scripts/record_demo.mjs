#!/usr/bin/env node
// Records a screen-capture walkthrough of the Signet Scanner dashboard.
//
// It serves this repo over a throwaway localhost port (the dashboard fetches
// data/*.json, so file:// will not do), drives index.html in headless Chromium
// with a scripted tour -- metal screens, the carat chips, the Value filter,
// the sort order -- and captures it to video with a caption bar burnt in.
//
// Nothing in index.html is modified: the captions, the pointer and the
// centred column are injected into the live page at record time.
//
//   node scripts/record_demo.mjs [--out docs/demo.mp4] [--size 1280x720]
//                                [--keep-webm] [--slow 1.0]
//
// Requires playwright (npm i -D playwright) with its Chromium. The MP4
// encode needs an ffmpeg with H.264: $FFMPEG, one on PATH, or the
// imageio-ffmpeg wheel (pip install imageio-ffmpeg). Without one the raw
// WebM Playwright produced is kept instead.

import { createServer } from "node:http";
import { spawnSync } from "node:child_process";
import { createReadStream } from "node:fs";
import fs from "node:fs/promises";
import path from "node:path";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

// ---------------------------------------------------------------- arguments
const argv = process.argv.slice(2);
const flag = (name, fallback) => {
  const i = argv.indexOf(name);
  return i === -1 ? fallback : argv[i + 1];
};
const OUT = path.resolve(ROOT, flag("--out", "docs/demo.mp4"));
const KEEP_WEBM = argv.includes("--keep-webm");
const SLOW = Number(flag("--slow", "1")) || 1;   // stretch every dwell
const [WIDTH, HEIGHT] = flag("--size", "1280x720").split("x").map(Number);

// ------------------------------------------------------------ static server
const MIME = {
  ".html": "text/html; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".ico": "image/x-icon",
};

function startServer(root) {
  const server = createServer((req, res) => {
    // Strip the cache-buster the dashboard appends to every fetch.
    const rel = decodeURIComponent(req.url.split("?")[0]).replace(/^\/+/, "");
    const file = path.resolve(root, rel || "index.html");
    if (!file.startsWith(root)) { res.writeHead(403).end("no"); return; }
    createReadStream(file)
      .on("open", () => res.writeHead(200, {
        "Content-Type": MIME[path.extname(file).toLowerCase()] || "application/octet-stream",
        "Cache-Control": "no-store",
      }))
      .on("error", () => res.writeHead(404).end("not found"))
      .pipe(res);
  });
  return new Promise(resolve =>
    server.listen(0, "127.0.0.1", () => resolve({
      server, base: `http://127.0.0.1:${server.address().port}/`,
    })));
}

// --------------------------------------------------- in-page demo furniture
// Injected once per page load: the caption bar, the full-screen cards, a
// visible pointer (Playwright's real cursor is never drawn) and an eased
// scroll, so the capture reads as a deliberate tour rather than a jump cut.
const DEMO_RUNTIME = () => {
  const style = document.createElement("style");
  style.textContent = `
    /* The dashboard is phone-first; keep it in a tablet-width column so the
       cards are not stretched across a 1280px frame. */
    body { max-width: 760px; margin: 0 auto; padding-bottom: 96px; }
    #demo-cap { position: fixed; left: 50%; transform: translateX(-50%);
      bottom: 22px; width: min(860px, calc(100vw - 48px)); z-index: 2147483000;
      padding: 13px 20px; border-radius: 14px; border: 1px solid #3a2e10;
      background: rgba(11,14,20,.93); box-shadow: 0 10px 34px rgba(0,0,0,.55);
      font: 600 17px/1.35 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      color: #f0e6cf; opacity: 0; transition: opacity .45s ease; }
    #demo-cap.on { opacity: 1; }
    #demo-cap .s { display: block; margin-top: 5px; font-size: 13.5px;
      font-weight: 500; color: #9aa3b5; }
    #demo-cap .b { color: #d4af37; }
    #demo-card { position: fixed; inset: 0; z-index: 2147483100;
      display: flex; flex-direction: column; align-items: center;
      justify-content: center; gap: 14px; text-align: center;
      background: #0b0e14; opacity: 0; transition: opacity .6s ease;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      pointer-events: none; }
    #demo-card.on { opacity: 1; }
    #demo-card .t { font-size: 46px; font-weight: 800; color: #f2f4f8;
      letter-spacing: -.5px; }
    #demo-card .t .coin { color: #d4af37; }
    #demo-card .u { font-size: 19px; font-weight: 500; color: #9aa3b5;
      max-width: 760px; line-height: 1.5; }
    #demo-ptr { position: fixed; z-index: 2147483200; width: 22px; height: 22px;
      margin: -11px 0 0 -11px; border-radius: 50%; border: 2px solid #d4af37;
      background: rgba(212,175,55,.22); opacity: 0; pointer-events: none;
      transition: transform .55s cubic-bezier(.4,0,.2,1), opacity .3s ease; }
    #demo-ptr.on { opacity: 1; }
    #demo-ptr.tap { animation: demo-tap .45s ease-out; }
    @keyframes demo-tap { 0% { box-shadow: 0 0 0 0 rgba(212,175,55,.55); }
      100% { box-shadow: 0 0 0 26px rgba(212,175,55,0); } }`;
  document.head.appendChild(style);

  const cap = document.createElement("div");
  cap.id = "demo-cap";
  const card = document.createElement("div");
  card.id = "demo-card";
  card.innerHTML = '<div class="t"></div><div class="u"></div>';
  const ptr = document.createElement("div");
  ptr.id = "demo-ptr";
  document.body.append(cap, card, ptr);

  const wait = ms => new Promise(r => setTimeout(r, ms));

  window.__demo = {
    async caption(text, sub) {
      if (cap.classList.contains("on")) { cap.classList.remove("on"); await wait(300); }
      cap.innerHTML = text + (sub ? `<span class="s">${sub}</span>` : "");
      cap.classList.add("on");
    },
    hideCaption() { cap.classList.remove("on"); },
    async card(title, under) {
      card.querySelector(".t").innerHTML = title;
      card.querySelector(".u").innerHTML = under || "";
      card.classList.add("on");
      await wait(600);
    },
    async hideCard() { card.classList.remove("on"); await wait(600); },
    async point(sel) {
      const el = document.querySelector(sel);
      if (!el) return false;
      el.scrollIntoView({ block: "nearest", inline: "center" });
      await wait(220);
      const r = el.getBoundingClientRect();
      ptr.style.transform = `translate(${r.left + r.width / 2}px, ${r.top + r.height / 2}px)`;
      ptr.classList.add("on");
      await wait(600);
      ptr.classList.remove("tap");
      void ptr.offsetWidth;            // restart the pulse
      ptr.classList.add("tap");
      return true;
    },
    hidePointer() { ptr.classList.remove("on"); },
    scrollTo(y, ms) {
      return new Promise(done => {
        const from = window.scrollY;
        const to = Math.max(0, Math.min(y, document.body.scrollHeight - innerHeight));
        const t0 = performance.now();
        const step = now => {
          const k = Math.min(1, (now - t0) / ms);
          const eased = k < 0.5 ? 2 * k * k : 1 - Math.pow(-2 * k + 2, 2) / 2;
          window.scrollTo(0, from + (to - from) * eased);
          k < 1 ? requestAnimationFrame(step) : done();
        };
        requestAnimationFrame(step);
      });
    },
  };
};

// ------------------------------------------------------------------- driver
// require() rather than import(): it honours NODE_PATH and a globally
// installed playwright, which bare ESM specifiers do not.
const require = createRequire(import.meta.url);
let chromium;
try {
  ({ chromium } = require("playwright"));
} catch {
  console.error("playwright is not installed -- run: npm i -D playwright");
  process.exit(1);
}

const { server, base } = await startServer(ROOT);
const scratch = await fs.mkdtemp(path.join(await fs.realpath("/tmp"), "signet-demo-"));

const pause = ms => new Promise(r => setTimeout(r, ms * SLOW));

async function openDashboard(context) {
  const page = await context.newPage();
  await page.goto(base, { waitUntil: "domcontentloaded" });
  // The first render only happens once data/index.json and every carat file
  // have landed, so wait for real cards rather than a fixed sleep.
  await page.waitForSelector(".card, .empty, .err", { timeout: 30_000 });
  await page.evaluate(DEMO_RUNTIME);
  return page;
}

const browser = await chromium.launch();

// Probe pass (not recorded): ask the real page which screens actually hold
// listings and which carat has the most, so the tour never dwells on an
// empty tab when a scan has not run for it yet.
const probeCtx = await browser.newContext({ viewport: { width: WIDTH, height: HEIGHT } });
const probe = await openDashboard(probeCtx);
const SCREENS = [
  ["gold",      "● Gold",     "Heavy solid-gold signets &amp; intaglios, 15g+", "Melt/Cost ratio against a live spot price, best value first"],
  ["silver",    "○ Silver",   "The same hunt in sterling silver",               "Priced per gram as well as against melt"],
  ["yurman",    "◆ Yurman",   "David Yurman signets, worldwide",                "Brand-verified listings only &mdash; no lookalikes"],
  ["brands",    "✦ Brands",   "Named-brand signet houses",                      "John Hardy, Konstantino, Lagos, Scott Kay, Tom Wood, Gurhan"],
  ["scottish",  "⚑ Scottish", "Scottish crest, cairngorm &amp; agate signets",  "Kept on an Edinburgh assay mark or a Scottish motif"],
  ["ending",    "⏳ Ending",   "Closing within 24 hours",                        "Every screen pooled, soonest first"],
];
const counts = {};
for (const [key] of SCREENS) {
  await probe.click(`.mchip[data-m="${key}"]`).catch(() => {});
  await probe.waitForTimeout(250);
  counts[key] = await probe.locator(".card").count();
}
await probe.click('.mchip[data-m="gold"]');
await probe.waitForTimeout(250);
const carats = await probe.$$eval("#controls .chip", els => els
  .filter(el => /^\d+ct/.test(el.textContent.trim()))
  .map(el => ({ v: el.dataset.v, n: Number(el.textContent.trim().split(/\s+/)[1]) || 0 })));
const topCarat = carats.sort((a, b) => b.n - a.n)[0];
const valueCount = await probe.$$eval("#controls .chip", els => {
  const el = els.find(e => e.dataset.v === "value");
  return el ? Number(el.textContent.replace(/\D+/g, "")) || 0 : 0;
});
await probeCtx.close();
console.log("screens:", counts, "top carat:", topCarat, "value:", valueCount);

// Recording pass.
const context = await browser.newContext({
  viewport: { width: WIDTH, height: HEIGHT },
  recordVideo: { dir: scratch, size: { width: WIDTH, height: HEIGHT } },
  deviceScaleFactor: 1,
});
const page = await openDashboard(context);
const demo = (fn, ...args) => page.evaluate(fn, ...args);

async function tap(selector) {
  const hit = await demo(sel => window.__demo.point(sel), selector);
  if (!hit) return false;
  await pause(250);
  await page.click(selector);
  await pause(450);
  await demo(() => window.__demo.hidePointer());
  return true;
}

// 1. Title card.
await demo(() => window.__demo.card(
  '<span class="coin">●</span> Signet Scanner',
  "Heavy solid-gold signets on eBay, scored against their melt value"));
await pause(2300);
await demo(() => window.__demo.card(
  "16 eBay marketplaces, one question",
  "Is the current bid below the gold it is made of?"));
await pause(2400);
await demo(() => window.__demo.hideCard());

// 2. The gold screen.
await demo(() => window.__demo.caption(
  'Each card is one live auction &mdash; <span class="b">carat, weight, price, melt value</span>',
  "Melt/Cost under 1.00x means the ring is cheaper than its own gold"));
await pause(2900);
await demo(() => window.__demo.scrollTo(900, 4200));
await pause(4500);
await demo(() => window.__demo.caption(
  "Non-UK listings carry a landed cost",
  "Import VAT and postage folded in before anything is called value"));
await pause(2600);
await demo(() => window.__demo.scrollTo(0, 1100));
await pause(1300);

// 3. Filters: value, carat, sort.
if (valueCount) {
  await demo(() => window.__demo.caption(
    '<span class="b">★ Value</span> &mdash; the shortlist',
    "Bid under melt x threshold, with the landed cost included"));
  await pause(1400);
  await tap('#controls .chip[data-v="value"]');
  await pause(2800);
  await tap('#controls .chip[data-v="value"]');     // clear it again
}
if (topCarat) {
  await demo(c => window.__demo.caption(
    `Filter to <span class="b">${c}ct</span>`,
    "Carat sets the fineness the melt maths is run at"), topCarat.v);
  await pause(1300);
  await tap(`#controls .chip[data-v="${topCarat.v}"]`);
  await pause(2500);
  await tap('#controls .chip[data-v="0"]');
}
await demo(() => window.__demo.caption(
  "Re-sort: heaviest first",
  "Or cheapest, or closest to closing"));
await pause(1300);
await demo(() => window.__demo.point("#sort"));
await page.selectOption("#sort", "heavy");
await pause(2400);
await demo(() => window.__demo.hidePointer());
await page.selectOption("#sort", "best");
await pause(700);

// 4. The other screens, skipping any a scan has not filled yet.
for (const [key, label, title, sub] of SCREENS.slice(1)) {
  if (!counts[key]) continue;
  await demo(([l, t, s]) => window.__demo.caption(
    `<span class="b">${l}</span> &mdash; ${t}`, s), [label, title, sub]);
  await pause(1200);
  await tap(`.mchip[data-m="${key}"]`);
  await pause(2400);
  // One screen gets a scroll to show there is depth behind each tab; the
  // rest stay at the top, or the tour outstays its welcome.
  if (key === "silver" && counts[key] > 4) {
    await demo(() => window.__demo.scrollTo(560, 2600));
    await pause(2800);
    await demo(() => window.__demo.scrollTo(0, 800));
    await pause(800);
  }
}

// 5. End card.
await demo(() => window.__demo.hideCaption());
await pause(500);
await demo(() => window.__demo.card(
  "Scans run on a schedule",
  'A GitHub Action re-runs the scanner and commits the JSON &mdash; the page is static<br><br>'
  + '<span style="color:#d4af37">github.com/stumacmar/Gold-scanner-</span>'));
await pause(3000);

const videoPath = await page.video().path();
await context.close();
await browser.close();
server.close();

// ----------------------------------------------------------------- encoding
// Playwright only ever writes VP8/WebM, and its bundled ffmpeg can only
// encode that, so an MP4 needs a full build.
function findFfmpeg() {
  if (process.env.FFMPEG) return process.env.FFMPEG;
  const onPath = spawnSync("ffmpeg", ["-version"], { stdio: "ignore" });
  if (onPath.status === 0) return "ffmpeg";
  const wheel = spawnSync("python3",
    ["-c", "import imageio_ffmpeg;print(imageio_ffmpeg.get_ffmpeg_exe())"],
    { encoding: "utf8" });
  if (wheel.status === 0) return wheel.stdout.trim();
  return null;
}

await fs.mkdir(path.dirname(OUT), { recursive: true });
const ffmpeg = findFfmpeg();
let final = OUT;

if (ffmpeg && OUT.endsWith(".mp4")) {
  const run = spawnSync(ffmpeg, [
    "-y", "-i", videoPath,
    "-c:v", "libx264", "-preset", "slow", "-crf", "26",
    "-pix_fmt", "yuv420p", "-r", "25",
    "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
    "-movflags", "+faststart", OUT,
  ], { stdio: "inherit" });
  if (run.status !== 0) { console.error("ffmpeg failed -- keeping the WebM"); final = null; }
} else if (OUT.endsWith(".mp4")) {
  console.error("no H.264 ffmpeg found (set $FFMPEG or pip install imageio-ffmpeg)"
    + " -- keeping the WebM");
  final = null;
}

if (final === null || KEEP_WEBM) {
  const webm = OUT.replace(/\.mp4$/, "") + ".webm";
  await fs.copyFile(videoPath, webm);
  console.log("wrote", webm);
  if (final === null) final = webm;
}
if (final !== null && final === OUT) console.log("wrote", OUT);
await fs.rm(scratch, { recursive: true, force: true });

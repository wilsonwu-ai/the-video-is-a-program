#!/usr/bin/env node
// render.mjs: deterministic frame capture for a composition.html that exposes window.seek(t).
//
//   node tools/render.mjs <composition.html> <outDir> [--fps 30] [--width 1920] [--height 1080]
//                         [--start 0] [--end <s>] [--quality 92]
//
// The page must set window.DURATION (seconds), window.seek(t) and window.__READY = true.
// Frames are written as <outDir>/f000000.jpg, f000001.jpg, ... at 1/fps intervals, so
//   ffmpeg -framerate <fps> -i <outDir>/f%06d.jpg ...
// turns them into a video. Prints one JSON line at the end: {frames, fps, duration, outDir, ms}.
import { execSync } from 'node:child_process';
import { mkdirSync, rmSync, existsSync } from 'node:fs';
import { resolve, join } from 'node:path';
import { pathToFileURL } from 'node:url';

const argv = process.argv.slice(2);
if (argv.length < 2) {
  console.error('usage: render.mjs <composition.html> <outDir> [--fps 30] [--width 1920] [--height 1080] [--start 0] [--end s] [--quality 92]');
  process.exit(1);
}
const opt = (name, def) => {
  const i = argv.indexOf('--' + name);
  return i >= 0 && argv[i + 1] !== undefined ? argv[i + 1] : def;
};
const html = resolve(argv[0]);
const outDir = resolve(argv[1]);
const fps = Number(opt('fps', 30));
const width = Number(opt('width', 1920));
const height = Number(opt('height', 1080));
const startT = Number(opt('start', 0));
const endArg = opt('end', null);
const quality = Number(opt('quality', 92));

if (!existsSync(html)) { console.error('render.mjs: no such file ' + html); process.exit(1); }

// Playwright is installed globally on this machine; resolve it without a local package.json.
const pwDir = process.env.PLAYWRIGHT_DIR || join(execSync('npm root -g').toString().trim(), 'playwright');
const { chromium } = await import(pathToFileURL(join(pwDir, 'index.mjs')).href);

const t0 = Date.now();
rmSync(outDir, { recursive: true, force: true });
mkdirSync(outDir, { recursive: true });

const browser = await chromium.launch({ args: ['--allow-file-access-from-files', '--disable-gpu-vsync'] });
const page = await browser.newPage({ viewport: { width, height }, deviceScaleFactor: 1 });
let pageErrors = 0;
page.on('pageerror', e => { pageErrors++; console.error('PAGE ERROR: ' + e.message); });
page.on('console', m => { if (m.type() === 'error') console.error('CONSOLE ERROR: ' + m.text()); });

await page.goto(pathToFileURL(html).href, { waitUntil: 'load' });
await page.waitForFunction(() => window.__READY === true, null, { timeout: 30000 });
await page.evaluate(() => document.fonts && document.fonts.ready);
const duration = await page.evaluate(() => window.DURATION);
if (!(duration > 0)) { console.error('render.mjs: window.DURATION is not a positive number'); await browser.close(); process.exit(2); }
const endT = endArg !== null ? Number(endArg) : duration;
const nFrames = Math.max(1, Math.round((endT - startT) * fps));

let lastPct = -1;
for (let i = 0; i < nFrames; i++) {
  const t = startT + i / fps;
  await page.evaluate(t => window.seek(t), t);
  await page.screenshot({ path: join(outDir, 'f' + String(i).padStart(6, '0') + '.jpg'), type: 'jpeg', quality, animations: 'disabled', caret: 'hide' });
  const pct = Math.floor((i + 1) * 10 / nFrames);
  if (pct !== lastPct) { lastPct = pct; console.error(`render ${pct * 10}% (${i + 1}/${nFrames})`); }
}
await browser.close();
console.log(JSON.stringify({ frames: nFrames, fps, duration: endT - startT, outDir, ms: Date.now() - t0, pageErrors }));
if (pageErrors) process.exit(3);

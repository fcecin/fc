#!/usr/bin/env node
// fcmon: a program window's monitor in a terminal, for debugging. It dials a
// CES program with `cesh dial` (a raw stream), reads its VS1 frames with the
// page's monitor (app/fc/static/fc/monitor.js), presses keys as the page
// does, and prints what the person would see.
//
//   node tools/fcmon.mjs [options] PID
//     --server HOST:PORT  the program's CES server
//     --wallet FILE       the wallet that dials (cesh -r FILE)
//     --home DIR          HOME for cesh (its ~/.cesh)
//     --cesh PATH         the cesh binary (default $CES_BUILD/cesh, or ../ces/build/release/cesh)
//     --keys K1,K2,...    keys to press, each a down and an up ("ArrowRight", "a", "Space");
//                         "K:MS" holds the key down MS milliseconds (default 30)
//     --every MS          the time from one key to the next (default 250)
//     --wait MS           the time to watch after the last key (default 1500)
//     --log               each message, with its time
//     --frames            the grid after each frame
//     --color             the grid in its colors (24-bit ANSI), for a person
//
// At the end it prints the title, the framed grid, the frames and bytes, the
// time from each key to its next frame, and the program's errors. It exits 1
// when the cable broke or the program sent an error, else 0.
import { spawn } from 'node:child_process';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const { Monitor, key } = require(new URL('../app/fc/static/fc/monitor.js', import.meta.url).pathname);

function args(argv) {
  const o = { every: 250, wait: 1500, keys: [], cesh: `${process.env.CES_BUILD || new URL('../../ces/build/release', import.meta.url).pathname}/cesh` };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    const next = () => argv[++i];
    if (a === '--server') o.server = next();
    else if (a === '--wallet') o.wallet = next();
    else if (a === '--home') o.home = next();
    else if (a === '--cesh') o.cesh = next();
    else if (a === '--keys') {
      o.keys = next().split(',').filter(Boolean).map((k) => {
        const [name, hold] = k.split(':');
        return { name: name === 'Space' ? ' ' : name, hold: hold ? +hold : 30 };
      });
    }
    else if (a === '--every') o.every = +next();
    else if (a === '--wait') o.wait = +next();
    else if (a === '--log') o.log = true;
    else if (a === '--frames') o.frames = true;
    else if (a === '--color') o.color = true;
    else if (/^\d+$/.test(a)) o.pid = a;
    else throw new Error(`what is ${a}?`);
  }
  if (!o.pid) throw new Error('give the pid of a program');
  return o;
}

// The grid in a frame, as text or in its colors.
function grid(mon, color) {
  const bar = `+${'-'.repeat(mon.cols)}+`;
  const out = [bar];
  for (let y = 0; y < mon.rows; y++) {
    let s = '';
    for (let x = 0; x < mon.cols; x++) {
      const c = mon.cell(x, y);
      const ch = c.code >= 32 && c.code <= 0x10ffff ? String.fromCodePoint(c.code) : '?';
      if (color) {
        s += `\x1b[38;2;${c.fg >> 16};${(c.fg >> 8) & 255};${c.fg & 255}m` +
             `\x1b[48;2;${c.bg >> 16};${(c.bg >> 8) & 255};${c.bg & 255}m${ch}`;
      } else {
        s += ch;
      }
    }
    out.push(`|${s}${color ? '\x1b[0m' : ''}|`);
  }
  out.push(bar);
  return out.join('\n');
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const median = (xs) => [...xs].sort((a, b) => a - b)[Math.floor(xs.length / 2)];

async function main() {
  const o = args(process.argv.slice(2));
  const cmd = [...(o.server ? ['--server', o.server] : []), ...(o.wallet ? ['-r', o.wallet] : []), 'dial', o.pid];
  const env = o.home ? { ...process.env, HOME: o.home } : process.env;
  const child = spawn(o.cesh, cmd, { env, stdio: ['pipe', 'pipe', 'pipe'] });
  const mon = new Monitor();
  const t0 = performance.now();
  const ms = () => (performance.now() - t0).toFixed(0).padStart(6);
  let bytes = 0;
  let pressed = null;          // the time of the last key down still waiting for a frame
  const lags = [];
  let stderr = '';
  let ended = false;
  child.stderr.on('data', (d) => { stderr += d; });
  child.on('exit', () => { ended = true; });
  child.on('error', (e) => { console.error(`fcmon: cannot run ${o.cesh}: ${e.message}`); process.exit(2); });
  child.stdout.on('data', (d) => {
    bytes += d.length;
    for (const m of mon.feed(new Uint8Array(d))) {
      if (m.kind === 'frame' && pressed !== null) { lags.push(performance.now() - pressed); pressed = null; }
      if (o.log) {
        console.log(`${ms()} ms  ${m.kind} ${m.size} B` + (m.kind === 'frame' ? `, ${m.runs} runs, ${m.cells} cells` : '') +
          (m.kind === 'mode' ? ` ${mon.cols}x${mon.rows}` : '') + (m.kind === 'title' ? ` "${mon.title}"` : '') +
          (m.error ? `  ERROR ${m.error}` : ''));
      }
      if (o.frames && m.kind === 'frame') console.log(grid(mon, o.color));
    }
  });
  await sleep(Math.max(o.every, 500));             // for the dial and the first frame
  for (const k of o.keys) {
    if (ended) break;
    if (o.log) console.log(`${ms()} ms  key ${JSON.stringify(k.name)} for ${k.hold} ms`);
    pressed = performance.now();
    child.stdin.write(key('down', {}, k.name));
    await sleep(k.hold);
    child.stdin.write(key('up', {}, k.name));
    await sleep(Math.max(0, o.every - k.hold));
  }
  await sleep(o.wait);
  child.kill('SIGTERM');
  console.log(`title: ${JSON.stringify(mon.title)}`);
  console.log(grid(mon, o.color));
  console.log(`${mon.frames} frames, ${mon.messages} messages, ${(bytes / 1024).toFixed(1)} kB` +
    (lags.length ? `; key to frame: median ${median(lags).toFixed(0)} ms, ` +
      `min ${Math.min(...lags).toFixed(0)}, max ${Math.max(...lags).toFixed(0)} (${lags.length} of ${o.keys.length} keys)` : ''));
  if (mon.broken) console.log(`BROKEN: ${mon.broken}`);
  for (const e of mon.errors) console.log(`ERROR: ${e}`);
  if (stderr.trim()) console.log(`cesh: ${stderr.trim()}`);
  process.exit(mon.broken || mon.errors.length ? 1 : 0);
}

main().catch((e) => { console.error(`fcmon: ${e.message}`); process.exit(2); });

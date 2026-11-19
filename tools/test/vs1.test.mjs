// Tests of a program window's display: VS1 (fc's copy of ces's
// loadables/vs1/vs1.lua), run in the CES sandbox with ljcheck -s, and the
// page's monitor (app/fc/static/fc/monitor.js), which reads what VS1 writes.
// Run: node --test tools/test/*.test.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { existsSync, readFileSync } from 'node:fs';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const ROOT = new URL('../../', import.meta.url).pathname;
const { Monitor, key, pointer, focus, text, keepalive } = require(ROOT + 'app/fc/static/fc/monitor.js');
const LJ = process.env.LJCHECK || '';   // a LuaJIT checker that runs a file in the CES sandbox with -s
const VS1 = readFileSync(ROOT + 'app/fc/static/fc/vs1.lua', 'utf8');

// Run a Lua file in the sandbox with VS1 as its local vs1, and return its
// stdout bytes.
function program(file, args = []) {
  const src = 'local vs1 = (function()\n' + VS1 + '\nend)()\n' + readFileSync(ROOT + 'tools/test/' + file, 'utf8');
  const r = spawnSync(LJ, ['-s', '-', '--', ...args], { input: src });
  assert.equal(r.status, 0, `${file} failed:\n${r.stderr}`);
  return r.stdout;
}

// vs1_test.lua's records: [u8 viewer][u32 size][bytes].
function records(buf) {
  const out = [];
  for (let at = 0; at < buf.length;) {
    const n = buf.readUInt32LE(at + 1);
    out.push({ viewer: buf[at], bytes: new Uint8Array(buf.subarray(at + 5, at + 5 + n)) });
    at += 5 + n;
  }
  return out;
}

const hasLj = LJ !== '' && existsSync(LJ);
const CES_VS1 = new URL('../../../ces/loadables/vs1/vs1.lua', import.meta.url).pathname;   // a ces checkout next to this one

test('the copy of VS1 is the one of ces', { skip: !existsSync(CES_VS1) && `no ${CES_VS1}` }, () => {
  assert.equal(VS1, readFileSync(CES_VS1, 'utf8'));
});

test('VS1 runs in the sandbox, and each viewer sees what the program drew', { skip: !hasLj && 'no LJCHECK' }, () => {
  const recs = records(program('vs1_test.lua'));
  assert.deepEqual(recs.map((r) => r.viewer), [1, 1, 1, 1, 1, 1, 2, 1]);
  const mon = { 1: new Monitor(), 2: new Monitor() };
  const seen = recs.map((r) => {
    const got = mon[r.viewer].feed(r.bytes);
    return { got, text: mon[r.viewer].text(), title: mon[r.viewer].title, cols: mon[r.viewer].cols };
  });
  // 1. The mode, then only the non-blank cells.
  assert.deepEqual(seen[0].got.map((m) => m.kind), ['mode', 'frame']);
  assert.deepEqual([seen[0].got[1].runs, seen[0].got[1].cells], [1, 2]);
  assert.equal(seen[0].text, ' ab\n\n');
  // 3. One cell.
  assert.deepEqual([seen[1].got[0].runs, seen[1].got[0].cells], [1, 1]);
  assert.equal(seen[1].text, ' ab\n\n     █');
  // 4. Clipped at the right edge.
  assert.equal(seen[2].text, ' ab\n    xy\n     █');
  // 5. UTF-8 and colors, and the title once.
  assert.equal(seen[3].text, ' ab\n    xy\né█   █');
  assert.equal(seen[3].title, 'box');
  assert.deepEqual(mon[1].cell(0, 2), { code: 0xe9, fg: 0xffcc00, bg: 0x101020 });
  // 6. Another screen, then back, with a mode and the whole screen each time.
  assert.deepEqual([seen[4].cols, seen[4].text], [3, 'B']);
  assert.deepEqual(seen[5].got.map((m) => m.kind), ['mode', 'title', 'frame']);
  assert.equal(seen[5].text, seen[3].text);
  // 7. A second viewer sees the same screen.
  assert.equal(seen[6].text, seen[3].text);
  assert.equal(seen[6].title, 'box');
  // 8. Boxes.
  assert.equal(seen[7].text, '##b\n##abxy\né█cd █');
  for (const m of Object.values(mon)) assert.deepEqual(m.errors, []);
});

test('what the monitor writes is what VS1 reads', { skip: !hasLj && 'no LJCHECK' }, () => {
  const bytes = [
    key('down', { shift: true }, 'ArrowLeft', 1000), key('repeat', {}, 'a', 1001),
    key('up', { ctrl: true, meta: true }, ' ', 4294967295), key('down', {}, 'ç', 7),
    pointer('down', 2, 0, 0, 8), keepalive(), pointer('move', 0, 79, 24, 9), text('ls\nç'), focus(false, 10), focus(true, 11),
  ];
  const hex = Buffer.concat(bytes.map((b) => Buffer.from(b))).toString('hex');
  assert.equal(program('vs1_events.lua', [hex]).toString(), [
    'key down [ArrowLeft] S 1000', 'key repeat [a]  1001', 'key up [ ] CM 4294967295', 'key down [ç]  7',
    'pointer down 2 1 1 8', 'pointer move 0 80 25 9', 'text [ls\nç]', 'focus false 10', 'focus true 11', '',
  ].join('\n'));
});

test('a message changes the grid only when all of it is here', () => {
  const m = new Monitor();
  const mode = [1, 5, 0, 0, 0, 1, 2, 0, 1, 0];                       // 2 by 1
  const run = [0, 0, 0, 0, 2, 0, 65, 0, 0, 0, 1, 2, 3, 4, 5, 6, 66, 0, 0, 0, 1, 2, 3, 4, 5, 6];
  const frame = [2, run.length, 0, 0, 0, ...run];
  const all = Uint8Array.from([...mode, ...frame]);
  for (let i = 0; i < all.length - 1; i++) {
    m.feed(all.subarray(i, i + 1));
    if (i >= mode.length - 1) assert.equal(m.text(), '', `after byte ${i}`);
  }
  m.feed(all.subarray(all.length - 1));
  assert.equal(m.text(), 'AB');
  assert.deepEqual(m.cell(1, 0), { code: 66, fg: 0x030201, bg: 0x060504 });
  assert.equal(m.frames, 1);
});

test('the monitor does not trust the cable', () => {
  const m = new Monitor();
  m.feed(Uint8Array.from([1, 5, 0, 0, 0, 1, 2, 0, 1, 0]));
  // A run off the screen shows its on-screen cells, drops the rest, and counts as an error.
  const run = [1, 0, 0, 0, 2, 0, ...[67, 0, 0, 0, 0, 0, 0, 0, 0, 0], ...[68, 0, 0, 0, 0, 0, 0, 0, 0, 0]];
  m.feed(Uint8Array.from([2, run.length, 0, 0, 0, ...run]));
  assert.equal(m.text(), ' C');
  assert.match(m.errors[0], /out of the screen/);
  // A code past Unicode reads as U+FFFD.
  const bad = [0, 0, 0, 0, 1, 0, 0xff, 0xff, 0xff, 0xff, 0, 0, 0, 0, 0, 0];
  m.feed(Uint8Array.from([2, bad.length, 0, 0, 0, ...bad]));
  assert.equal(m.text(), '�C');
  // A mode from another version changes nothing.
  m.feed(Uint8Array.from([1, 5, 0, 0, 0, 2, 9, 0, 9, 0]));
  assert.equal(m.cols, 2);
  // A message too big for VS1 breaks the cable for good.
  m.feed(Uint8Array.from([2, 0, 0, 0, 0x7f]));
  assert.match(m.broken, /a message of/);
  m.feed(Uint8Array.from([1, 5, 0, 0, 0, 1, 3, 0, 1, 0]));
  assert.equal(m.cols, 2);
});

// The monitor of a program window: it reads the cable from VS1 (fc/vs1.lua)
// into a grid of character cells, and writes the person's events. It has no
// DOM, so the Node tools and tests run it too, and a message changes the grid
// only once it is whole.
//
// The wire (little-endian): a message is [u8 kind][u32 size][body]. To the
// window: 1 mode [u8 1][u16 cols][u16 rows], which blanks the screen (a
// space, gray 0xaaaaaa on black); 2 frame, runs of [u16 x][u16 y][u16 n]
// and n cells of [u32 code][u24 fg][u24 bg] (x and y from 0); 3 title
// [UTF-8]. From the window: 128 keepalive, with no body, which VS1 skips;
// 129 key [u8 action: 1 down, 2 up, 3 repeat]
// [u8 mods: 1 shift, 2 ctrl, 4 alt, 8 meta][u32 time][UTF-8 name]; 130
// pointer [u8 action: 1 down, 2 up, 3 move][u8 button][u16 x][u16 y]
// [u32 time]; 131 focus [u8 on][u32 time]; 132 text [UTF-8], a line or a
// paste, with "\n" for Enter. time is the page's clock, in ms, when the
// person acted.
(function (root, make) {
  const m = make();
  if (typeof module === 'object' && module.exports) module.exports = m;
  else { root.fc = root.fc || {}; root.fc.monitor = m; }
})(typeof self !== 'undefined' ? self : this, () => {
  'use strict';

  const MODE = 1;
  const FRAME = 2;
  const TITLE = 3;
  const KEEPALIVE = 128;
  const KEY = 129;
  const POINTER = 130;
  const FOCUS = 131;
  const TEXT = 132;
  const NAMES = { [MODE]: 'mode', [FRAME]: 'frame', [TITLE]: 'title', [KEY]: 'key', [POINTER]: 'pointer', [FOCUS]: 'focus' };
  const BLANK_CODE = 32;
  const BLANK_FG = 0xaaaaaa;
  const BLANK_BG = 0x000000;
  const MAX_COLS = 512;
  const MAX_ROWS = 256;
  const MAX_MESSAGE = 8 << 20;     // the largest VS1 message
  const CELL = 10;                 // [u32 code][u24 fg][u24 bg]
  const utf8 = new TextDecoder();

  class Monitor {
    constructor() {
      this.cols = 0;
      this.rows = 0;
      this.code = new Uint32Array(0);
      this.fg = new Uint32Array(0);
      this.bg = new Uint32Array(0);
      this.dirty = new Uint8Array(0);   // 1 for each cell that changed since the last draw
      this.anyDirty = false;
      this.title = '';
      this.part = new Uint8Array(0);    // the start of a message still arriving
      this.broken = '';                 // why the cable is broken, or ''
      this.errors = [];                 // what the program did wrong, for an inspector
      this.messages = 0;
      this.frames = 0;
    }

    reset() { Object.assign(this, new Monitor()); }

    // Bytes from the cable. Every whole message changes the grid, and the
    // start of the next one waits for more bytes. Returns {kind, size, runs,
    // cells} for each message, for an inspector.
    feed(bytes) {
      if (this.broken) return [];
      let buf = bytes;
      if (this.part.length) {
        buf = new Uint8Array(this.part.length + bytes.length);
        buf.set(this.part);
        buf.set(bytes, this.part.length);
      }
      const view = new DataView(buf.buffer, buf.byteOffset, buf.byteLength);
      const got = [];
      let at = 0;
      while (buf.length - at >= 5) {
        const kind = buf[at];
        const size = view.getUint32(at + 1, true);
        if (size > MAX_MESSAGE) {
          this.broken = `a message of ${size} bytes`;
          this.part = new Uint8Array(0);
          return got;
        }
        if (buf.length - at - 5 < size) break;
        got.push(this.apply(kind, buf.subarray(at + 5, at + 5 + size)));
        this.messages++;
        at += 5 + size;
      }
      this.part = buf.slice(at);
      return got;
    }

    apply(kind, body) {
      const view = new DataView(body.buffer, body.byteOffset, body.byteLength);
      const seen = { kind: NAMES[kind] || `kind ${kind}`, size: body.length, runs: 0, cells: 0 };
      if (kind === MODE) {
        if (body.length < 5 || body[0] !== 1) return this.wrong(seen, 'a mode that is not version 1');
        this.mode(view.getUint16(1, true), view.getUint16(3, true));
      } else if (kind === FRAME) {
        this.frames++;
        let p = 0;
        while (p + 6 <= body.length) {
          const x = view.getUint16(p, true);
          const y = view.getUint16(p + 2, true);
          const n = view.getUint16(p + 4, true);
          p += 6;
          if (p + n * CELL > body.length) return this.wrong(seen, 'a run longer than its frame');
          seen.runs++;
          if (y >= this.rows || x + n > this.cols) this.wrong(seen, `a run out of the screen at ${x + 1},${y + 1}`);
          for (let i = 0; i < n; i++, p += CELL) {
            if (y >= this.rows || x + i >= this.cols) continue;
            const c = y * this.cols + x + i;
            this.code[c] = view.getUint32(p, true);
            this.fg[c] = body[p + 4] | (body[p + 5] << 8) | (body[p + 6] << 16);
            this.bg[c] = body[p + 7] | (body[p + 8] << 8) | (body[p + 9] << 16);
            this.dirty[c] = 1;
            seen.cells++;
          }
          this.anyDirty = true;
        }
        if (p !== body.length) this.wrong(seen, 'bytes after the last run of a frame');
      } else if (kind === TITLE) {
        this.title = utf8.decode(body);
      }
      return seen;
    }

    wrong(seen, why) {
      seen.error = why;
      this.errors.push(why);
      if (this.errors.length > 100) this.errors.shift();
      return seen;
    }

    mode(cols, rows) {
      this.cols = Math.max(1, Math.min(MAX_COLS, cols));
      this.rows = Math.max(1, Math.min(MAX_ROWS, rows));
      const n = this.cols * this.rows;
      this.code = new Uint32Array(n).fill(BLANK_CODE);
      this.fg = new Uint32Array(n).fill(BLANK_FG);
      this.bg = new Uint32Array(n).fill(BLANK_BG);
      this.dirty = new Uint8Array(n).fill(1);
      this.anyDirty = true;
    }

    cell(x, y) {
      const c = y * this.cols + x;
      return { code: this.code[c], fg: this.fg[c], bg: this.bg[c] };
    }

    text() {
      const lines = [];
      for (let y = 0; y < this.rows; y++) {
        let s = '';
        for (let x = 0; x < this.cols; x++) {
          const cp = this.code[y * this.cols + x];
          s += cp <= 0x10ffff ? String.fromCodePoint(cp) : '�';
        }
        lines.push(s.replace(/ +$/, ''));
      }
      return lines.join('\n');
    }
  }

  function message(kind, body) {
    const out = new Uint8Array(5 + body.length);
    out[0] = kind;
    new DataView(out.buffer).setUint32(1, body.length, true);
    out.set(body, 5);
    return out;
  }

  const ACTIONS = { down: 1, up: 2, repeat: 3, move: 3 };
  const now = () => Math.round(typeof performance === 'undefined' ? Date.now() : performance.now());

  // A key of the person: action "down", "up" or "repeat", mods {shift, ctrl,
  // alt, meta}, the browser's name ("a", "ArrowLeft"), and time on the page's
  // clock, in ms.
  function key(action, mods, name, time = now()) {
    const n = new TextEncoder().encode(name);
    const body = new Uint8Array(6 + n.length);
    body[0] = ACTIONS[action];
    body[1] = (mods.shift ? 1 : 0) | (mods.ctrl ? 2 : 0) | (mods.alt ? 4 : 0) | (mods.meta ? 8 : 0);
    new DataView(body.buffer).setUint32(2, time >>> 0, true);
    body.set(n, 6);
    return message(KEY, body);
  }

  // The pointer on cell (x, y), from 0: action "down", "up" or "move".
  function pointer(action, button, x, y, time = now()) {
    const body = new Uint8Array(10);
    const v = new DataView(body.buffer);
    body[0] = ACTIONS[action];
    body[1] = button;
    v.setUint16(2, x, true);
    v.setUint16(4, y, true);
    v.setUint32(6, time >>> 0, true);
    return message(POINTER, body);
  }

  function text(s) {
    return message(TEXT, new TextEncoder().encode(s));
  }

  function keepalive() {
    return message(KEEPALIVE, new Uint8Array(0));
  }

  function focus(on, time = now()) {
    const body = new Uint8Array(5);
    body[0] = on ? 1 : 0;
    new DataView(body.buffer).setUint32(1, time >>> 0, true);
    return message(FOCUS, body);
  }

  return { Monitor, key, pointer, focus, text, keepalive, MODE, FRAME, TITLE, KEEPALIVE, KEY, POINTER, FOCUS, TEXT, NAMES };
});

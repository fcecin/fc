// A program window: the monitor of a CES program, which draws with VS1
// (fc/vs1.lua) and sends whole frames through the hub. The monitor
// (fc/monitor.js) keeps the grid, and this file draws it with the character
// ROM (GNU Unifont, fc/rom/), sends the person's input to the program, and
// shows the HUD and a status screen when there is no picture.
(function () {
  'use strict';

  const CW = 8;                        // a cell's width, in ROM pixels
  const CH = 16;
  const ROM = new URL('rom/', document.currentScript.src).href;
  const displays = new Map();          // window name -> its display
  const pages = new Map();             // ROM page -> its bytes, 'loading' or 'none'
  const enc = new TextEncoder();
  const HEAD = 512;                    // the first bytes of a dial, shown for a program that doesn't speak VS1
  const QUIET_START = 3000;            // ms with no frame before the window says why
  const MAX_SCALE = 2;                 // the largest scale, 2 page pixels per ROM pixel
  const RATE_SECS = 3;                 // the data rate's window, in seconds
  const REASONS = {
    CES_ERROR_COMPUTE_INSTANCE_NOT_FOUND: 'no program runs with this pid on that server',
    CES_ERROR_NOT_LISTENING: 'the program takes no dials: it has no ces.conn.set_listener',
    CES_ERROR_PROTO_REJECTED: 'the server does not dial programs',
    CES_ERROR_TIMEOUT: 'the server did not answer in time',
    'error no reply': 'the site got no answer from the server',
    'the server refused the bind': 'the server refused the connection',
  };

  // A ROM page, or null while it loads. When it arrives, every display redraws
  // the cells of that page.
  function rom(page) {
    const p = pages.get(page);
    if (p instanceof Uint8Array) return p;
    if (p === undefined) {
      pages.set(page, 'loading');
      fetch(`${ROM}${page.toString(16).padStart(3, '0')}.bin`)
        .then((r) => (r.ok ? r.arrayBuffer() : null))
        .then((b) => {
          pages.set(page, b ? new Uint8Array(b) : 'none');
          for (const d of displays.values()) d.paged(page);
        }, () => pages.set(page, 'none'));
    }
    return null;
  }

  function rgba(c) {
    return (0xff000000 | ((c & 0xff) << 16) | (c & 0xff00) | ((c >> 16) & 0xff)) >>> 0;
  }

  // A wide glyph (16 pixels) in an 8-pixel cell, where a pixel is on when
  // either of its two is on.
  function squeeze(v) {
    let s = 0;
    for (let c = 0; c < 8; c++) if (v & (0xc000 >> (2 * c))) s |= 0x8000 >> c;
    return s;
  }

  // The display of window `w` ({name, pid, server, title}) in element `el`.
  // `setTitle` puts a title on the window.
  function display(el, w, setTitle) {
    const name = w.name;
    const mon = new fc.monitor.Monitor();
    const stage = document.createElement('div');
    stage.className = 'program-stage';
    el.appendChild(stage);
    const canvas = document.createElement('canvas');
    canvas.className = 'program-screen';
    canvas.tabIndex = 0;
    stage.appendChild(canvas);
    const status = document.createElement('div');
    status.className = 'program-status';
    stage.appendChild(status);
    const hud = document.createElement('div');
    hud.className = 'program-hud';
    el.appendChild(hud);
    const ctx = canvas.getContext('2d');
    let img = null;
    let px = null;
    let state = 'idle';                // 'idle', 'dialing' or 'live'
    let why = '';                      // why the program isn't live
    let opened = false;
    let drawing = false;               // a draw is waiting for the next animation frame
    let cell = -1;
    const held = new Map();            // a held key's code -> the name sent for it
    const stats = { bytes: 0, last: null, keyAt: 0, lag: null, head: new Uint8Array(0), since: 0,
      sent: 0, events: 0, lastIn: 0, rx: [], scale: 0 };
    let checkLater = null;
    let ticking = null;                // the HUD's clock, while the dial is up
    let full = false;
    let hudDue = false;                // a HUD draw is waiting for the next animation frame

    function title() {
      const t = mon.title || w.title || name;
      setTitle(state === 'live' || !opened ? t
        : `${t} · ${state === 'dialing' ? 'dialing' : `${why || 'closed'} · Enter dials`}`);
      diagnose();
      hudSoon();
    }

    // --- The status screen ------------------------------------------------------

    function line(cls, text) {
      const d = document.createElement('div');
      d.className = cls;
      d.textContent = text;
      return d;
    }

    function say(kind, head, lines) {
      status.hidden = false;
      status.className = `program-status ${kind}${mon.cols ? ' over' : ''}`;
      status.replaceChildren(line('head', head), ...lines.map(([cls, text]) => line(cls, text)));
    }

    function seen() {
      const b = stats.head;
      const text = new TextDecoder().decode(b);
      const odd = [...text].filter((c) => c === '\ufffd' || (c < ' ' && c !== '\n' && c !== '\r' && c !== '\t')).length;
      if (odd > text.length / 4) {
        return [...b.slice(0, 48)].map((x) => x.toString(16).padStart(2, '0')).join(' ');
      }
      return text.replace(/\x1b\[[0-?]*[ -/]*[@-~]/g, '').replace(/\r/g, '').split('\n')
        .map((l) => l.replace(/[\x00-\x1f\x7f]/g, '·').slice(0, 64)).filter((l) => l.trim()).slice(0, 4).join('\n');
    }

    function diagnose() {
      const quiet = state === 'live' && !mon.cols && performance.now() - stats.since >= QUIET_START;
      if (!opened) { status.hidden = true; return; }
      if (state === 'dialing') {
        return say('dialing', 'DIALING', [['why', `pid ${w.pid} on ${w.server}`]]);
      }
      if (state === 'live' && (mon.broken || (quiet && stats.bytes))) {
        return say('bad', 'NO VS1 SIGNAL', [
          ['why', `the program sent ${stats.bytes} bytes, and they are not VS1`],
          ['seen', seen()],
          ['do', `a program for a terminal: dial it in the shell with cesh dial ${w.pid}`],
        ]);
      }
      if (quiet) {
        return say('bad', 'NO PICTURE', [
          ['why', 'the dial is up, and the program drew nothing'],
          ['do', 'a VS1 program ships a frame to each viewer that comes: s:ship(conn) in on_open'],
        ]);
      }
      if (state === 'idle') {
        const lost = why === 'no connection to the site';
        const closed = why === 'closed';
        const plain = REASONS[why];
        return say('down', closed ? 'CLOSED' : lost ? 'LINK DOWN' : 'DIAL FAILED', [
          ['why', closed ? 'the dial closed: the program ended it, or it ended' : plain || why],
          ...(plain ? [['code', why]] : []),
          ['do', 'Enter dials again'],
        ]);
      }
      status.hidden = true;
    }

    function sized() {
      canvas.width = mon.cols * CW;
      canvas.height = mon.rows * CH;
      img = ctx.createImageData(canvas.width, canvas.height);
      px = new Uint32Array(img.data.buffer);
      fit();
    }

    // The grid shows centered, at the largest whole scale that fits, up to
    // MAX_SCALE, and a grid bigger than the window shrinks to fit.
    function fit() {
      if (!canvas.width || !stage.clientWidth) return;
      const k = Math.min(stage.clientWidth / canvas.width, stage.clientHeight / canvas.height);
      const s = k >= 1 ? Math.min(MAX_SCALE, Math.floor(k)) : k;
      stats.scale = s;
      canvas.style.width = `${canvas.width * s}px`;
      canvas.style.height = `${canvas.height * s}px`;
      canvas.style.imageRendering = s >= 1 ? 'pixelated' : 'auto';
      hudSoon();
    }

    function later() {
      if (drawing) return;
      drawing = true;
      requestAnimationFrame(draw);
    }

    function draw() {
      drawing = false;
      if (!mon.anyDirty || !px) return;
      mon.anyDirty = false;
      const cols = mon.cols;
      let top = mon.rows;
      let bottom = -1;
      for (let i = 0, n = cols * mon.rows; i < n; i++) {
        if (!mon.dirty[i]) continue;
        mon.dirty[i] = 0;
        const y = (i / cols) | 0;
        if (y < top) top = y;
        if (y > bottom) bottom = y;
        glyph(i % cols, y, mon.code[i], rgba(mon.fg[i]), rgba(mon.bg[i]));
      }
      if (bottom >= 0) ctx.putImageData(img, 0, 0, 0, top * CH, canvas.width, (bottom - top + 1) * CH);
    }

    // One cell. A code with no glyph shows U+FFFD, and a cell whose ROM page
    // is still loading shows the background.
    function glyph(x, y, code, fg, bg) {
      let bits = rom(code >> 8);
      let at = 256 + (code & 0xff) * 32;
      let width = bits ? bits[code & 0xff] : 0;
      if (bits && !width) {
        bits = rom(0xff);
        at = 256 + 0xfd * 32;
        width = bits ? bits[0xfd] : 0;
      }
      const W = canvas.width;
      for (let r = 0, o = y * CH * W + x * CW; r < CH; r++, o += W) {
        let v = width ? (bits[at + 2 * r] << 8) | bits[at + 2 * r + 1] : 0;
        if (width === 16) v = squeeze(v);
        for (let c = 0; c < CW; c++) px[o + c] = v & (0x8000 >> c) ? fg : bg;
      }
    }

    // --- The cable ------------------------------------------------------------

    // After 3 quiet minutes, a keepalive holds the dial open, since the
    // servers close a channel after 10 quiet minutes.
    const QUIET_MS = 3 * 60 * 1000;
    let quiet = 0;
    function awake() {
      clearTimeout(quiet);
      if (state === 'live') quiet = setTimeout(() => send(fc.monitor.keepalive()), QUIET_MS);
    }

    function bytes(b) {
      if (state === 'dialing') live();
      awake();
      if (stats.head.length < HEAD) {
        const h = new Uint8Array(Math.min(HEAD, stats.head.length + b.length));
        h.set(stats.head);
        h.set(b.subarray(0, h.length - stats.head.length), stats.head.length);
        stats.head = h;
      }
      const got = mon.feed(b);
      const now = performance.now();
      stats.bytes += b.length;
      stats.lastIn = now;
      stats.rx.push([now, b.length, 0]);
      for (const m of got) {
        if (m.kind === 'mode') sized();
        if (m.kind === 'title') title();
        if (m.kind === 'frame') {
          stats.last = m;
          stats.rx.push([now, 0, 1]);
          if (stats.keyAt) { stats.lag = performance.now() - stats.keyAt; stats.keyAt = 0; }
        }
      }
      if (mon.anyDirty) later();
      if (mon.broken || mon.cols) diagnose();
      hudSoon();
    }

    // The dial is up: a window with no frame after QUIET_START says why.
    function live() {
      state = 'live';
      stats.since = performance.now();
      clearTimeout(checkLater);
      checkLater = setTimeout(diagnose, QUIET_START);
      clearInterval(ticking);
      ticking = setInterval(hudSoon, 1000);
      title();
      awake();
    }

    function send(b) {
      if (state !== 'live') return;
      const n = enc.encode(name);
      const out = new Uint8Array(1 + n.length + b.length);
      out[0] = n.length;
      out.set(n, 1);
      out.set(b, 1 + n.length);
      fc.hub.sendBytes(out);
      stats.sent += out.length;
      stats.events++;
      awake();
    }

    function dial() {
      why = '';
      mon.reset();
      Object.assign(stats, { bytes: 0, head: new Uint8Array(0), sent: 0, events: 0, lastIn: 0, rx: [], last: null, lag: null });
      if (!fc.hub.send({ term: { dial: { pid: w.pid, server: w.server }, w: name } })) {
        state = 'idle';
        why = 'no connection to the site';
      } else {
        state = 'dialing';
      }
      title();
    }

    function ended(result) {
      state = 'idle';
      clearTimeout(quiet);
      clearTimeout(checkLater);
      clearInterval(ticking);
      why = result === 'closed' ? 'closed' : result.replace(/^error /, '');
      held.clear();
      title();
    }

    // --- Input -------------------------------------------------------------

    const mods = (e) => ({ shift: e.shiftKey, ctrl: e.ctrlKey, alt: e.altKey, meta: e.metaKey });

    canvas.addEventListener('keydown', (e) => {
      if (state !== 'live') {
        if (state === 'idle' && e.key === 'Enter') { e.preventDefault(); dial(); }
        return;
      }
      if (!e.ctrlKey && !e.metaKey) e.preventDefault();     // the browser keeps its shortcuts
      if (!e.repeat || !held.has(e.code)) held.set(e.code, e.key);
      send(fc.monitor.key(e.repeat ? 'repeat' : 'down', mods(e), held.get(e.code), Math.round(e.timeStamp)));
      if (!e.repeat) stats.keyAt = performance.now();
    });
    canvas.addEventListener('keyup', (e) => {
      const k = held.get(e.code) ?? e.key;
      held.delete(e.code);
      send(fc.monitor.key('up', mods(e), k, Math.round(e.timeStamp)));
    });
    // A window that loses focus releases its held keys.
    canvas.addEventListener('blur', () => {
      for (const k of held.values()) send(fc.monitor.key('up', {}, k));
      held.clear();
      send(fc.monitor.focus(false));
    });
    canvas.addEventListener('focus', () => send(fc.monitor.focus(true)));

    function pointAt(e) {
      const r = canvas.getBoundingClientRect();
      const x = Math.floor((e.clientX - r.left) / r.width * mon.cols);
      const y = Math.floor((e.clientY - r.top) / r.height * mon.rows);
      return r.width && x >= 0 && y >= 0 && x < mon.cols && y < mon.rows ? [x, y] : null;
    }
    canvas.addEventListener('pointerdown', (e) => {
      canvas.focus();
      const p = pointAt(e);
      if (p) send(fc.monitor.pointer('down', e.button, p[0], p[1], Math.round(e.timeStamp)));
    });
    canvas.addEventListener('pointerup', (e) => {
      const p = pointAt(e);
      if (p) send(fc.monitor.pointer('up', e.button, p[0], p[1], Math.round(e.timeStamp)));
    });
    canvas.addEventListener('pointermove', (e) => {
      const p = pointAt(e);
      const c = p ? p[1] * mon.cols + p[0] : -1;
      if (c === cell) return;
      cell = c;
      if (p) send(fc.monitor.pointer('move', 0, p[0], p[1], Math.round(e.timeStamp)));
      hudSoon();
    });
    canvas.addEventListener('contextmenu', (e) => { if (state === 'live') e.preventDefault(); });
    canvas.addEventListener('pointerleave', () => { cell = -1; hudSoon(); });

    // --- The HUD ----------------------------------------------------------------

    const hex = (c) => `#${c.toString(16).padStart(6, '0')}`;
    const count = (n, one) => `${n} ${one}${n === 1 ? '' : 's'}`;
    function size(n) {
      return n < 1024 ? `${n} B` : n < 1 << 20 ? `${(n / 1024).toFixed(1)} kB` : `${(n / 1048576).toFixed(2)} MB`;
    }
    function ago(ms) {
      const t = ms / 1000;
      return t < 10 ? `${t.toFixed(1)} s` : t < 120 ? `${Math.round(t)} s` : `${Math.round(t / 60)} min`;
    }

    function rates(now) {
      stats.rx = stats.rx.filter(([t]) => now - t < RATE_SECS * 1000);
      let bytes = 0, frames = 0;
      for (const [, b, f] of stats.rx) { bytes += b; frames += f; }
      return [bytes / RATE_SECS, frames / RATE_SECS];
    }

    function field(row, key, value, cls) {
      const k = document.createElement('span');
      k.className = 'k';
      k.textContent = key;
      const v = document.createElement('span');
      v.className = `v${cls ? ' ' + cls : ''}`;
      v.textContent = value;
      row.append(k, v);
    }

    function drawHud() {
      const now = performance.now();
      const [bps, fps] = rates(now);
      const link = state === 'live' ? 'LIVE' : state === 'dialing' ? 'DIALING' : why === 'closed' ? 'CLOSED' : 'DOWN';
      const at = cell >= 0 && cell < mon.cols * mon.rows ? [cell % mon.cols + 1, ((cell / mon.cols) | 0) + 1] : null;
      const scale = stats.scale >= 1 ? `${stats.scale}x` : stats.scale ? `${stats.scale.toFixed(2)}x` : '';
      const rows = [[
        ['LINK', link, state],
        ['NODE', `${w.server} / PID ${w.pid}`],
        ['RES', mon.cols ? `${mon.cols}x${mon.rows}${scale ? ' @' + scale : ''}` : '--'],
        ['CELL', at ? `${at[0]},${at[1]}` : '--'],
        ['RX', `${size(Math.round(bps))}/s`],
      ]];
      if (full) {
        const f = stats.last;
        rows.push([
          ['UP', state === 'live' ? ago(now - stats.since) : '--'],
          ['FRAMES', `${mon.frames} / ${fps.toFixed(1)} fps`],
          ['LAST', f ? `${size(f.size)}, ${count(f.runs, 'run')}, ${count(f.cells, 'cell')}` : '--'],
        ]);
        rows.push([
          ['IN', size(stats.bytes)],
          ['OUT', `${size(stats.sent)}, ${count(stats.events, 'event')}`],
          ['LAG', stats.lag === null ? '--' : `${stats.lag.toFixed(0)} ms`],
          ['QUIET', stats.lastIn ? ago(now - stats.lastIn) : '--'],
        ]);
        if (at) {
          const c = mon.cell(at[0] - 1, at[1] - 1);
          const ch = c.code > 32 && c.code <= 0x10ffff ? ` ${String.fromCodePoint(c.code)}` : '';
          rows.push([['CHAR', `U+${c.code.toString(16).toUpperCase().padStart(4, '0')}${ch}`],
            ['FG', hex(c.fg)], ['BG', hex(c.bg)]]);
        }
        const err = mon.broken || mon.errors[mon.errors.length - 1];
        if (err) rows.push([['ERR', `${err}${mon.errors.length > 1 ? ` (${mon.errors.length})` : ''}`, 'alert']]);
      }
      const [first, ...rest] = rows.map((r) => {
        const row = document.createElement('div');
        row.className = 'row';
        for (const [k, v, cls] of r) field(row, k, v, cls);
        return row;
      });
      if (rest.length) {
        const more = document.createElement('div');
        more.className = 'more';
        more.append(...rest);
        hud.replaceChildren(first, more);
      } else {
        hud.replaceChildren(first);
      }
    }

    function hudSoon() {
      if (hudDue) return;
      hudDue = true;
      requestAnimationFrame(() => { hudDue = false; drawHud(); });
    }

    function inspect() {
      full = !full;
      drawHud();
    }

    // --- The window -------------------------------------------------------------

    // The display opens when its element first gets a size, so a folded window
    // dials when it opens, and the grid follows the window's size.
    const sizes = new ResizeObserver(() => requestAnimationFrame(() => {
      if (!el.isConnected || !stage.offsetWidth || !stage.offsetHeight) return;
      if (!opened) {
        opened = true;
        rom(0x00);
        rom(0xff);
        dial();
      }
      fit();
    }));
    sizes.observe(stage);
    title();

    const d = {
      bytes,
      fromHub(m) {
        if (m.live) live();
        if (m.end !== undefined) ended(m.end);
      },
      // A ROM page arrived: redraw its cells (and the U+FFFD ones for page 0xff).
      paged(page) {
        let any = false;
        for (let i = 0, n = mon.cols * mon.rows; i < n; i++) {
          if (mon.code[i] >> 8 === page || page === 0xff) { mon.dirty[i] = 1; any = true; }
        }
        if (any) { mon.anyDirty = true; later(); }
      },
      inspect,
      text: () => ({ title: mon.title, state, cols: mon.cols, rows: mon.rows, grid: mon.text(),
        status: status.hidden ? '' : status.innerText }),
      lost() { if (state !== 'idle') ended('error no connection to the site'); },
      close() {
        clearInterval(ticking);
        if (state !== 'idle') fc.hub.send({ term: { stop: true, w: name } });
        sizes.disconnect();
        displays.delete(name);
      },
    };
    displays.set(name, d);
    return d;
  }

  // A program window's bytes from the hub: [u8 n][window name, n bytes][bytes].
  fc.hub.on('bytes', (b) => {
    const name = new TextDecoder().decode(b.subarray(1, 1 + b[0]));
    const d = displays.get(name);
    if (d) d.bytes(b.subarray(1 + b[0]));
  });
  fc.hub.on('term', (m) => {
    if (m.w === undefined) return;          // the shell's
    const d = displays.get(m.w);
    if (d) d.fromHub(m);
  });
  fc.hub.on('close', () => { for (const d of displays.values()) d.lost(); });

  window.fc = window.fc || {};
  window.fc.program = { display, text: (name) => displays.get(name)?.text() };
})();

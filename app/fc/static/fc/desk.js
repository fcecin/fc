// The desktop: WinBox windows that match the desktop the site keeps
// (fc/desk.py), which the hub sends on every change. A drag, a resize or a
// fold sends an op ("move chat 10 135"), and the page shows the site's
// answer, so every open page shows the same desktop.
(function () {
  'use strict';

  const SEND_MS = 300;        // a move with no button down waits this long
  let rev = 0;
  let started = false;
  const wins = new Map();     // name -> {box, w}
  let mounts = {};            // kind -> a kernel window's element (the chat)
  const KERNEL = new Set(['chat', 'shell']);
  let applying = false;       // true while applying the site's state, which sends no op back
  let folding = false;        // true while WinBox folds a window, so its bar's place is ignored

  function top() {
    const bar = document.querySelector('.bar');
    return bar ? Math.ceil(bar.getBoundingClientRect().bottom) : 0;
  }

  function quote(s) { return '"' + String(s).replace(/\\/g, '\\\\').replace(/"/g, '\\"') + '"'; }

  // While an op of this page is pending (a drag still held) or in flight, the
  // screen is ahead of the site, and a desk from the site would undo the
  // change until the reply. So the page parks the newest such desk, and shows
  // it after the reply if it is newer. `local` counts the person's changes
  // here, and `flying` the ops in flight.
  let local = 0;
  let flying = 0;
  let parked = null;

  function fromSite(s) {
    if (flying || pending.size) { if (!parked || s.rev > parked.rev) parked = s; return; }
    match(s);
  }

  function settle() {
    if (flying || pending.size || !parked) return;
    const s = parked;
    parked = null;
    match(s);
  }

  async function send(op) {
    local++;
    flying++;
    let r = null;
    try { r = await fc.api('POST', '/api/desk', { op }); } catch (e) { /* refused */ }
    flying--;
    if (r) { match(r); settle(); }
    else { parked = null; await sync(); }   // the site refused: show what it holds
    return r;
  }

  // An op for a window, sent once the button is released, so a drag or a resize is
  // one op with its last place. With no button down (a key, a script), the
  // op goes after SEND_MS of stillness.
  const pending = new Map();   // key -> op (a function that gives the op text)
  const timers = new Map();
  let held = false;
  function later(key, op) {
    local++;
    pending.set(key, op);
    clearTimeout(timers.get(key));
    if (!held) timers.set(key, setTimeout(() => flush(key), SEND_MS));
  }
  function flush(key) {
    clearTimeout(timers.get(key));
    timers.delete(key);
    const op = pending.get(key);
    pending.delete(key);
    if (op) send(op());
  }
  addEventListener('pointerdown', () => { held = true; }, true);
  addEventListener('pointerup', () => { held = false; for (const k of [...pending.keys()]) flush(k); }, true);
  addEventListener('pointercancel', () => { held = false; for (const k of [...pending.keys()]) flush(k); }, true);

  function textBody(w) {
    const el = document.createElement('div');
    el.className = 'text-widget';
    el.textContent = w.text;
    return el;
  }

  function shellBody() {
    const el = document.createElement('div');
    el.className = 'shell-widget';
    fc.term.mount(el);
    return el;
  }

  const INSPECT = 'data:image/svg+xml,' + encodeURIComponent(
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16"><circle cx="6.5" cy="6.5" r="4.5" ' +
    'fill="none" stroke="#fff" stroke-width="1.7"/><path d="M10 10l4.5 4.5" stroke="#fff" stroke-width="1.7"/></svg>');

  // A program window (fc/program.js), whose program can set its title.
  function programBody(w) {
    const el = document.createElement('div');
    el.className = 'program-widget';
    const display = fc.program.display(el, w, (title) => {
      const it = wins.get(w.name);
      if (it) it.box.setTitle(title || it.w.title);
    });
    return { el, display };
  }

  // The first place of a kernel window, when the site has none yet: the chat
  // at the bottom center, the shell at the top left.
  function firstPlace(kind) {
    if (kind === 'shell') {
      return { x: 12, y: 12, w: Math.min(640, innerWidth - 24), h: Math.round((innerHeight - top()) * 0.3) };
    }
    const width = Math.min(860, innerWidth - 24);
    const height = Math.round((innerHeight - top()) * 0.62);
    return { x: Math.max(0, Math.round((innerWidth - width) / 2)),
             y: Math.max(0, innerHeight - top() - height - 12), w: width, h: height };
  }

  function body(w) {
    if (w.kind === 'chat') return { el: mounts.chat };
    if (w.kind === 'shell') return { el: shellBody() };
    if (w.kind === 'program') return programBody(w);
    return { el: textBody(w) };
  }

  function create(w) {
    if (wins.has(w.name)) return;
    let g = { x: w.x, y: w.y, w: w.w, h: w.h };
    let placed = false;
    if (KERNEL.has(w.kind) && !w.w) { g = firstPlace(w.kind); placed = true; }
    const name = w.name;
    // WinBox calls onmove and onresize while it builds the window, with the
    // site's place, so these send no op.
    applying = true;
    let box;
    const b = body(w);
    try { box = new WinBox({
      id: `win-${name}`,
      title: w.title,
      mount: b.el,
      // The desk keeps a window's place, size and fold, so the maximize and
      // full-screen buttons go.
      class: KERNEL.has(w.kind) ? ['fc-window', `fc-${w.kind}`, 'no-close', 'no-max', 'no-full']
        : ['fc-window', `fc-${w.kind}`, 'no-max', 'no-full'],
      top: top(),
      // The site measures y from the top of the desktop, below the bar, and
      // WinBox from the top of the browser window.
      x: g.x, y: g.y + top(), width: g.w || 360, height: g.h || 200,
      minwidth: 200, minheight: 100,
      min: w.folded,
      // A folded window sits on the bottom edge as a bar, in WinBox's place for
      // a fold. Only a drag of an open window sends an op, and only
      // when it differs from what the site holds.
      onmove: (x, y) => {
        const it = wins.get(name);
        if (applying || folding || !it || it.box.min) return;
        const nx = Math.max(0, Math.round(x)), ny = Math.max(0, Math.round(y - top()));
        if (nx !== it.w.x || ny !== it.w.y) later(`m:${name}`, () => `move ${name} ${nx} ${ny}`);
      },
      onresize: (width, height) => {
        const it = wins.get(name);
        if (applying || folding || !it || it.box.min) return;
        const nw = Math.round(width), nh = Math.round(height);
        if (nw !== it.w.w || nh !== it.w.h) later(`s:${name}`, () => `size ${name} ${nw} ${nh}`);
      },
      onminimize: () => { if (!applying) send(`fold ${name}`); },
      onrestore: () => { if (!applying) send(`unfold ${name}`); },
      // The close button asks the site, and the window goes when the site's
      // desktop drops it (a forced close). The chat always stays.
      onclose: (force) => {
        if (KERNEL.has(w.kind)) return true;
        if (force) return false;
        send(`del ${name}`);
        return true;
      },
    }); } finally { applying = false; }
    // WinBox lays out a fold's bar (calling onmove and onresize) before it
    // marks the window folded (box.min), so while it folds, the place and
    // size are the bar's.
    const minimize = box.minimize.bind(box);
    box.minimize = (...a) => {
      folding = true;
      try { return minimize(...a); } finally { folding = false; }
    };
    if (b.display) box.addControl({ index: 0, class: 'wb-inspect', image: INSPECT, click: () => b.display.inspect() });
    wins.set(name, { box, w: { ...w }, display: b.display });
    if (placed) { send(`move ${name} ${g.x} ${g.y}`); send(`size ${name} ${g.w} ${g.h}`); }
  }

  // Make one window match the site's widget. Matching twice changes nothing.
  function update(it, w) {
    const b = it.box;
    // Open first, then place: a folded window keeps WinBox's place on the
    // bottom edge, and gets the site's place when it opens.
    if (!w.folded && b.min) b.restore();
    if (!w.folded) {
      if (Math.round(b.x) !== w.x || Math.round(b.y - top()) !== w.y) b.move(w.x, w.y + top());
      if (w.w && (Math.round(b.width) !== w.w || Math.round(b.height) !== w.h)) b.resize(w.w, w.h);
    }
    if (w.folded && !b.min) b.minimize();
    if (w.title !== it.w.title) b.setTitle(w.title);
    if (w.kind === 'text' && w.text !== it.w.text) b.body.querySelector('.text-widget').textContent = w.text;
    it.w = { ...w };
  }

  function match(s) {
    // {rev} alone means no change, and an older desktop (a reply that came
    // after a newer one) is already out of date.
    if (s.widgets === undefined || (s.rev < rev && wins.size)) return;
    rev = s.rev;
    const names = new Set(s.widgets.map((w) => w.name));
    applying = true;
    try {
      for (const [name, it] of wins) {
        if (!names.has(name)) {
          if (it.display) it.display.close();   // a program window ends its dial
          it.box.close(true);
          wins.delete(name);
        }
      }
      for (const w of s.widgets) {
        const it = wins.get(w.name);
        if (it) update(it, w);
      }
    } finally { applying = false; }
    for (const w of s.widgets) if (!wins.has(w.name)) create(w);
  }

  // Read the desktop once at the start, when the hub's socket opens (it may
  // have missed changes), and after a refused op. A change by the person during the
  // read makes it stale, and that op's reply brings the desktop.
  async function sync() {
    const asked = local;
    try {
      const r = await fc.api('GET', '/api/desk');
      if (asked === local) fromSite(r);
    } catch (e) { /* the next open of the socket reads again */ }
  }

  let told = '';
  function tellScreen() {
    const size = `${Math.round(innerWidth)} ${Math.max(0, Math.round(innerHeight - top()))}`;
    if (size !== told) { told = size; send(`screen ${size}`); }
  }

  // `m` gives the elements of the kernel's windows: {chat: element}.
  async function start(m) {
    if (started) return;
    started = true;
    mounts = m || {};
    fc.hub.on('desk', fromSite);
    fc.hub.on('shell', fc.term.shellSays);
    fc.hub.on('open', sync);
    await sync();
    tellScreen();
  }

  // The bar's height can change (a narrow window wraps it): keep the windows
  // below it.
  let screenTimer = null;
  addEventListener('resize', () => {
    clearTimeout(screenTimer);
    screenTimer = setTimeout(tellScreen, 500);
    const t = top();
    applying = true;
    for (const { box } of wins.values()) { box.top = t; if (!box.min && !box.max) box.move(); }
    applying = false;
  });

  window.fc = window.fc || {};
  window.fc.desk = { start, send, windows: wins, quote };
})();

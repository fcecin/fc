// The shell: a terminal (xterm.js) on the person's bash, through the hub
// (fc/shellhub.py). When the shell ends, Enter starts a new one.
(function () {
  'use strict';

  const SCROLLBACK = 5000;
  const AI = '\x1b[38;2;255;143;182m';   // the assistant's color
  const DIM = '\x1b[38;2;150;140;190m';  // the window's own notes
  const OFF = '\x1b[0m';

  let term = null;
  let fit = null;
  let state = 'none';      // none, starting, live or ended (fc/shellhub.py)

  function write(s) { if (term) term.write(s); }
  function size() { return { cols: term.cols, rows: term.rows }; }
  function send(t) { return fc.hub.send({ term: t }); }
  function note(text) { write(`\r\n${DIM}[${text}]${OFF}\r\n`); }

  function attach() {
    if (term && term.element) send({ open: true, ...size() });
  }

  function restart() {
    term.reset();
    if (!send({ restart: true, ...size() })) note('the page is not connected to the site');
  }

  function setState(s, why) {
    const was = state;
    state = s;
    if (s === 'ended') note(`the shell ended${why ? ': ' + why : ''}. Enter starts a new one`);
    else if (s === 'starting' && was !== 'starting') note('the shell starts');
  }

  function fromHub(m) {
    if (m.w !== undefined) return;     // a program window's (fc/program.js)
    if (m.state !== undefined) return setState(m.state, m.why);
    if (m.out !== undefined) {
      if (m.replay) term.reset();      // a reconnect replays the screen
      write(m.out);
    }
  }

  // --- The assistant's lines ------------------------------------------

  // The desk's "shell" message: a desk op and its output, in the assistant's
  // color, or {clear: true}.
  function shellSays(m) {
    if (m.clear) { if (term) term.clear(); return; }
    for (const e of m.entries || []) {
      const t = e.t.replace(/\n/g, '\r\n');
      if (e.k === 'in') write(`\r\n${AI}$ ${t}${OFF}\r\n`);
      else write((e.k === 'err' ? '\x1b[31m' : '') + t + OFF + '\r\n');
    }
  }

  function data(d) {
    if (state === 'live') send({ in: d });
    else if (state === 'ended' && d === '\r') restart();
    else if (state === 'none' && d === '\r') attach();
  }

  // --- The window -------------------------------------------------------------

  // The terminal opens when its element first gets a size, so a window folded
  // at the start opens it when it unfolds, and it fits the element at every
  // resize after that.
  function mount(el) {
    term = new Terminal({
      scrollback: SCROLLBACK,
      convertEol: false,
      cursorBlink: true,
      allowTransparency: true,
      fontFamily: '"Courier New", ui-monospace, Menlo, Consolas, monospace',
      fontSize: 13,
      lineHeight: 1.15,
      theme: {
        background: 'rgba(0, 0, 0, 0)',
        foreground: '#c9f7e8',
        cursor: '#ffb347',
        cursorAccent: '#1a1034',
        selectionBackground: 'rgba(255, 179, 71, 0.35)',
      },
    });
    fit = new FitAddon.FitAddon();
    term.loadAddon(fit);
    term.onData(data);
    fc.hub.on('term', fromHub);
    fc.hub.on('open', attach);
    fc.hub.on('close', () => {
      if (state === 'none') return;
      state = 'none';
      note('the connection to the site broke');
    });
    let opened = false;
    let pending = false;
    new ResizeObserver(() => {
      if (pending) return;
      pending = true;
      requestAnimationFrame(() => {
        pending = false;
        if (!el.isConnected || !el.offsetWidth || !el.offsetHeight) return;
        if (!opened) {
          opened = true;
          term.open(el);
          try { fit.fit(); } catch (e) { /* measured at the next size */ }
          attach();
          return;
        }
        const before = [term.cols, term.rows];
        try { fit.fit(); } catch (e) { return; }
        if (state === 'live' && (term.cols !== before[0] || term.rows !== before[1])) {
          send({ size: [term.cols, term.rows] });
        }
      });
    }).observe(el);
  }

  window.fc = window.fc || {};
  window.fc.term = { mount, shellSays };
})();

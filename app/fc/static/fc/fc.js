// fc: the code of every page.
(function () {
  'use strict';

  const IN = document.body.dataset.in === '1';

  function cookie(name) {
    const m = document.cookie.match('(^|;)\\s*' + name + '=([^;]*)');
    return m ? decodeURIComponent(m[2]) : '';
  }

  async function api(method, path, body) {
    const opt = { method, headers: {}, credentials: 'same-origin' };
    if (body !== undefined) {
      opt.headers['Content-Type'] = 'application/json';
      opt.body = JSON.stringify(body);
    }
    if (method !== 'GET') opt.headers['X-CSRFToken'] = cookie('csrftoken');
    const r = await fetch(path, opt);
    let j = {};
    try { j = await r.json(); } catch (e) { j = { error: 'BAD_REPLY', message: String(r.status) }; }
    if (!r.ok) { const err = new Error(j.message || j.error); err.code = j.error; throw err; }
    return j;
  }

  // The bar: the person's server. Credit shows where it is spent, in the chat
  // and the navigator.
  async function fillMe() {
    const bar = document.querySelector('[data-me]');
    if (!bar) return null;
    try {
      const me = await api('GET', '/api/me');
      bar.querySelector('[data-me-server]').textContent = me.server;
      bar.querySelector('[data-me-ledger]').hidden = !me.ledger;
      return me;
    } catch (e) { return null; }
  }

  // The page's one WebSocket to the hub. A message is {kind: value} ("desk",
  // "net", "term", "chat"), and `on(kind, fn)` follows a kind. "open" comes at
  // every connection, when a follower reads the state once. Binary messages
  // carry a program window's bytes ("bytes", `sendBytes`). A broken socket
  // reconnects after a wait that doubles, up to 30 s.
  const ELSEWHERE = 4003;
  const hub = (() => {
    const fns = {};
    let wait = 1000;
    let ws = null;
    function on(kind, fn) { (fns[kind] = fns[kind] || []).push(fn); }
    // A message to the hub (the shell's terminal). False while the socket is
    // closed.
    function send(msg) {
      if (!ws || ws.readyState !== WebSocket.OPEN) return false;
      ws.send(JSON.stringify(msg));
      return true;
    }
    function sendBytes(b) {
      if (!ws || ws.readyState !== WebSocket.OPEN) return false;
      ws.send(b);
      return true;
    }
    function emit(kind, x) {
      for (const fn of fns[kind] || []) { try { fn(x); } catch (e) { console.error(e); } }
    }
    function again() { setTimeout(connect, wait); wait = Math.min(wait * 2, 30000); }
    async function connect() {
      let r;
      try { r = await api('GET', '/api/hub'); } catch (e) { again(); return; }
      const base = r.url.startsWith('/')
        ? `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}${r.url}` : r.url;
      ws = new WebSocket(`${base}?t=${encodeURIComponent(r.ticket)}`);
      ws.binaryType = 'arraybuffer';
      ws.onopen = () => { wait = 1000; emit('open'); };
      ws.onmessage = (m) => {
        if (m.data instanceof ArrayBuffer) return emit('bytes', new Uint8Array(m.data));
        let j;
        try { j = JSON.parse(m.data); } catch (e) { return; }
        for (const k of Object.keys(j)) emit(k, j[k]);
      };
      ws.onclose = (e) => {
        ws = null;
        emit('close');
        if (e.code === ELSEWHERE) elsewhere(); else again();
      };
    }
    // A newer page closed this one, so a cover blocks it until the person
    // takes it back, which closes the other page.
    function elsewhere() {
      let c = document.querySelector('.elsewhere');
      if (!c) {
        c = document.createElement('div');
        c.className = 'elsewhere';
        const box = document.createElement('div');
        const p = document.createElement('p');
        p.textContent = 'fc is open in another tab or device.';
        const b = document.createElement('button');
        b.type = 'button';
        b.className = 'primary';
        b.textContent = 'use it here';
        b.addEventListener('click', () => location.reload());
        box.append(p, b);
        c.append(box);
        document.body.append(c);
      }
      emit('elsewhere');
    }
    return { on, send, sendBytes, start: connect };
  })();

  window.fc = { api, fillMe, hub };

  document.addEventListener('DOMContentLoaded', () => {
    const here = document.body.dataset.tab;
    const tab = here && document.querySelector(`.tab[data-tab="${here}"]`);
    if (tab) tab.setAttribute('aria-current', 'page');
    hub.start();   // a visitor's page follows the network
    if (!IN) return;
    fillMe();
  });
})();

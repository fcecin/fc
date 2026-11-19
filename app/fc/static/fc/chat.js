// The chat: the person's conversation with the ledger's model, which the site
// keeps. It shows only the words, since the model types its commands in the
// shell (fc/term.js), and the model's words show as markdown (fc/md.js).
(function () {
  'use strict';

  const box = () => document.querySelector('[data-chat-lines]');
  let busy = false;

  function credit(raw) {
    if (raw === null || raw === undefined) return '';
    let s = (raw / 1e8).toFixed(8).replace(/0+$/, '').replace(/\.$/, '');
    return s;
  }

  function k(n) {
    return n >= 10000 ? `${Math.floor(n / 1000)}k` : n >= 1000 ? `${(n / 1000).toFixed(1)}k` : `${n}`;
  }

  // The status line: your credit on the ledger, which pays the answers, how
  // full the model's window is, and the cost of the last answer.
  function status(c, balance, lastCharged) {
    const el = document.querySelector('[data-chat-status]');
    if (!el || !c) return;
    const pct = c.room ? Math.floor(c.prompt_tokens * 100 / c.room) : 0;
    const ledger = document.querySelector('[data-chat]').dataset.ledger;
    let s = balance === null || balance === undefined ? '' : `${balance} credits on ${ledger} · `;
    s += `context ${c.estimated ? '~' : ''}${k(c.prompt_tokens)} of ${k(c.room)} (${pct}%)`;
    if (c.cached_percent !== null && c.cached_percent !== undefined) s += ` · cache ${c.cached_percent}%`;
    if (lastCharged) s += ` · that answer ${credit(lastCharged)} credit`;
    el.textContent = s;
  }

  // The line kinds that the chat shows. The others (fc/models.py) are for the
  // model.
  const SHOWN = new Set(['user', 'model', 'note', 'job']);

  function line(l) {
    if (!SHOWN.has(l.kind.split(' ')[0]) || (l.kind === 'model' && !l.text)) return null;
    const p = document.createElement('div');
    p.className = `chat-line ${l.kind}`;
    words(p, l);
    if (l.id) p.dataset.id = l.id;
    box().appendChild(p);
    return p;
  }

  // A line's text: markdown for the model's words, and only the first line of
  // a job, whose rest is output for the model.
  function words(p, l) {
    if (l.kind === 'model') {
      p.classList.add('md');
      p.replaceChildren(fc.md.render(l.text));
    } else {
      p.textContent = l.kind === 'job' ? l.text.split('\n')[0] : l.text;
    }
  }

  // Follow new lines while the person is at the bottom.
  let following = true;
  document.addEventListener('scroll', (e) => {
    const b = box();
    if (e.target === b) following = b.scrollHeight - b.scrollTop - b.clientHeight < 24;
  }, true);

  function bottom(force) {
    const b = box();
    if (force) following = true;
    if (following) b.scrollTop = b.scrollHeight;
  }

  async function load() {
    try {
      const r = await fc.api('GET', '/api/chat');
      box().textContent = '';
      for (const l of r.lines) line(l);
      status(r.context, r.balance);
      bottom(true);
    } catch (e) { /* the next action tries again */ }
  }

  function cookie(name) {
    const m = document.cookie.match('(^|;)\\s*' + name + '=([^;]*)');
    return m ? decodeURIComponent(m[2]) : '';
  }

  // One turn, as the site streams it (server-sent events).
  async function stream(text, onEvent) {
    const r = await fetch('/api/chat?stream=1', {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': cookie('csrftoken') },
      body: JSON.stringify({ text }),
    });
    if (!r.ok) {
      let j = {};
      try { j = await r.json(); } catch (e) { j = { error: 'BAD_REPLY', message: String(r.status) }; }
      const err = new Error(j.message || j.error);
      err.code = j.error;
      throw err;
    }
    const reader = r.body.getReader();
    const dec = new TextDecoder();
    let buf = '';
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let i;
      while ((i = buf.indexOf('\n\n')) >= 0) {
        const chunk = buf.slice(0, i);
        buf = buf.slice(i + 2);
        if (chunk.startsWith('data: ')) onEvent(JSON.parse(chunk.slice(6)));
      }
    }
  }

  let form = null;

  async function send() {
    const ta = form.elements.text;
    const text = ta.value.trim();
    if (!text || busy) return;
    ta.value = '';
    grow(ta);
    await turn(text);
    ta.focus();
  }

  // The view of one turn: a waiting line, the model's words as they stream
  // in, and the lines of the turn. `event` takes {"piece"} and {"line"}.
  function turnView() {
    let wait = line({ kind: 'model waiting', text: 'thinking' });
    let panel = null;     // the model's panel while its words stream in
    let raw = '';
    return {
      event(e) {
        if (e.piece !== undefined) {
          raw += e.piece;
          const t = raw.trim();
          if (t) {
            if (wait) { wait.remove(); wait = null; }
            if (!panel) panel = line({ kind: 'model', text: t });
            else words(panel, { kind: 'model', text: t });
          }
        } else if (e.line) {
          const l = e.line;
          if (l.kind === 'user') return;
          if (l.kind === 'model') {
            if (panel) { panel.remove(); panel = null; }
            if (wait) { wait.remove(); wait = null; }
            raw = '';
          }
          // A site note or a job goes before the waiting line.
          const el = line(l);
          if (el && wait) box().insertBefore(el, wait);
          // The model ran a line in the shell, and may answer again.
          if (l.kind === 'run' && !wait) wait = line({ kind: 'model waiting', text: 'thinking' });
        }
        bottom();
      },
      end(failed) {
        if (wait) wait.remove();
        if (failed && panel) panel.remove();
        wait = panel = null;
      },
    };
  }

  function spent(done) {
    let charged = 0;
    for (const l of done.lines) charged += l.charged || 0;
    status(done.context, done.balance, charged);
  }

  async function turn(text) {
    busy = true;
    const mine = text ? line({ kind: 'user', text }) : null;
    const view = turnView();
    bottom(true);
    const button = form.querySelector('button');
    let failed = null;
    const onEvent = (e) => {
      if (e.done) spent(e.done);
      else if (e.error) failed = e;
      else view.event(e);
    };
    try {
      await feel.hold(button, stream(text, onEvent), 'ok');
      if (failed) throw Object.assign(new Error(failed.message), { code: failed.error });
      view.end(false);
    } catch (e) {
      view.end(true);
      await load();
      if (!failed) {
        const n = line({ kind: 'note', text: e.message || e.code });
        if (n) n.classList.add('failed');
      }
    }
    if (mine) mine.dataset.sent = '1';
    busy = false;
    bottom();
  }

  // A turn that the site runs when a shell job wakes the model, with its events
  // from the hub. The person's next line waits for it to end.
  let woken = null;
  function fromSite(e) {
    if (!form) return;
    if (e.note) { line(e.note); bottom(); return; }
    if (e.done || e.error) {
      if (woken) woken.end(!!e.error);
      woken = null;
      busy = false;
      if (e.done) spent(e.done);
      else {
        const n = line({ kind: 'note', text: e.message || e.error });
        if (n) n.classList.add('failed');
      }
      bottom();
      return;
    }
    if (!woken) { woken = turnView(); busy = true; }
    woken.event(e);
  }

  function grow(ta) {
    ta.style.height = 'auto';
    ta.style.height = `${Math.min(ta.scrollHeight, 220)}px`;
  }

  document.addEventListener('DOMContentLoaded', async () => {
    form = document.querySelector('[data-chat-form]');
    if (!form) return;
    const section = document.querySelector('[data-chat]');
    section.hidden = false;
    await fc.desk.start({ chat: section });
    const ta = form.elements.text;
    form.addEventListener('submit', (e) => { e.preventDefault(); send(); });
    ta.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
    });
    ta.addEventListener('input', () => grow(ta));
    document.querySelector('[data-chat-new]').addEventListener('click', async (e) => {
      if (busy) return;
      await feel.hold(e.currentTarget, fc.api('POST', '/api/chat/new'));
      await load();
      ta.focus();
    });
    load().then(() => ta.focus());
    fc.hub.on('chat', fromSite);
  });
})();

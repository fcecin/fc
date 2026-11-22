// Tests of the fc bar of a website (app/fc/static/fc/website.js) on a fake
// document: the payment panel that opens over the website.
// Run: node --test tools/test/website.test.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const ROOT = new URL('../../', import.meta.url).pathname;
const SOURCE = readFileSync(ROOT + 'app/fc/static/fc/website.js', 'utf8');

function element(tag) {
  const e = {
    tag, kids: [], listeners: {}, dataset: {}, hidden: false, disabled: false, textContent: '',
    append(...k) { e.kids.push(...k); },
    replaceChildren(...k) { e.kids = k; },
    addEventListener(type, fn) { (e.listeners[type] ||= []).push(fn); },
    fire(type) { for (const fn of e.listeners[type] || []) fn(); },
    submit() {},
  };
  return e;
}

// Run website.js with a frame, a panel and a fetch that answers `payments`.
function bar(payments, timers) {
  const parts = {
    'form[target="site"]': element('form'),
    'iframe[name="site"]': element('iframe'),
    '[data-payments]': element('section'),
    '[data-balance]': element('span'),
  };
  const posts = [];
  const document = {
    body: { dataset: { site: '7', server: '127.0.0.1:56110' } },
    cookie: 'csrftoken=t',
    querySelector: (s) => parts[s],
    createElement: element,
  };
  const fetch = async (path, opt) => {
    if (opt.method !== 'GET') posts.push(path);
    return { ok: true, json: async () => (opt.method === 'GET' ? { payments } : payments[0]) };
  };
  vm.runInNewContext(SOURCE, { document, fetch, setTimeout: timers.setTimeout, decodeURIComponent });
  return { panel: parts['[data-payments]'], frame: parts['iframe[name="site"]'], posts };
}

const PENDING = {
  id: 3, site: 'ana/shop', state: 'pending', description: 'The booklet', amount: '2.00000000',
  payee: '2'.repeat(64), server: '127.0.0.1:56110', balance: '9.00000000', order: 'booklet-1',
};

const tick = () => new Promise((r) => setImmediate(r));

test('the Pay button opens disabled, and arms after a second', async () => {
  const queued = [];
  const timers = { setTimeout: (fn, ms) => queued.push({ fn, ms }) };
  const { panel, frame, posts } = bar([PENDING], timers);
  frame.fire('load');
  await tick(); await tick();
  const buttons = panel.kids.filter((k) => k.tag === 'button');
  assert.equal(buttons.length, 2);
  assert.ok(buttons.every((b) => b.disabled), 'a click in the first second is not a decision');
  assert.deepEqual(queued.map((q) => q.ms), [1000]);
  queued[0].fn();
  assert.ok(buttons.every((b) => !b.disabled));
  buttons[0].fire('click');
  await tick();
  assert.deepEqual(posts, ['/payments/3/approve']);
});

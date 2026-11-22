// The fc bar of a website. It opens the website in its frame, and it shows
// the payments that the website asks for in fc's own panel, outside the frame.
(function () {
  'use strict';

  const form = document.querySelector('form[target="site"]');
  const frame = document.querySelector('iframe[name="site"]');
  const panel = document.querySelector('[data-payments]');
  const balance = document.querySelector('[data-balance]');
  const site = document.body.dataset.site;
  const server = document.body.dataset.server;

  const RESULT = {
    paid: 'Paid.',
    declined: 'Declined. No credits moved.',
    sending: 'fc sends this payment now.',
    failed: 'The payment did not go through, and the amount did not move.',
    unknown: 'fc sent this payment, and no result came back. fc will not send it again. ' +
             'See your balance before you buy this again.',
  };

  function cookie(name) {
    const m = document.cookie.match('(^|;)\\s*' + name + '=([^;]*)');
    return m ? decodeURIComponent(m[2]) : '';
  }

  async function api(method, path) {
    const opt = { method, headers: {}, credentials: 'same-origin' };
    if (method !== 'GET') opt.headers['X-CSRFToken'] = cookie('csrftoken');
    const r = await fetch(path, opt);
    if (!r.ok) throw new Error(String(r.status));
    return r.json();
  }

  // Text from the website goes in as text only.
  function el(tag, text) {
    const e = document.createElement(tag);
    if (text !== undefined) e.textContent = text;
    return e;
  }

  function open(heading) {
    panel.replaceChildren(el('h2', heading));
    panel.hidden = false;
  }

  function close(p) {
    panel.hidden = true;
    panel.replaceChildren();
    if (p && p.frame) frame.src = p.frame;
  }

  // A page that the frame loads can be the website's request for a payment.
  async function refresh() {
    let j;
    try { j = await api('GET', '/payments/site/' + site); } catch (e) { return; }
    const p = j.payments[0];
    if (!p) close();
    else if (p.state === 'pending') ask(p);
    else result(p);
  }

  function ask(p) {
    open('Payment request from ' + p.site);
    const terms = el('dl');
    const rows = [
      ['For', p.description],
      ['Amount', p.amount + ' credits, and the server\'s fee for the call'],
      ['To', 'the program ' + p.payee.slice(0, 16) + '… on ' + p.server],
      ['Your balance there', p.balance === null ? 'unknown' : p.balance + ' credits'],
      ['Order', p.order],
    ];
    for (const [k, v] of rows) terms.append(el('dt', k), el('dd', v));
    const pay = el('button', 'Pay ' + p.amount + ' credits');
    const no = el('button', 'Decline');
    pay.addEventListener('click', () => decide(p, 'approve', [pay, no]));
    no.addEventListener('click', () => decide(p, 'decline', [pay, no]));
    // The panel opens over the website, when the website chooses, and under
    // the pointer. A click in its first second is not a decision.
    pay.disabled = no.disabled = true;
    setTimeout(() => { pay.disabled = no.disabled = false; }, 1000);
    panel.append(terms, pay, no);
  }

  async function decide(p, how, buttons) {
    for (const b of buttons) b.disabled = true;
    let r;
    try {
      r = await api('POST', '/payments/' + p.id + '/' + how);
    } catch (e) {
      open(p.site + ': ' + p.description);
      const link = el('a', 'your payments');
      link.href = '/payments';
      const text = el('p', 'fc did not answer. See ');
      text.append(link, '.');
      panel.append(text);
      return;
    }
    result(r);
  }

  function result(p) {
    open(p.site + ': ' + p.description);
    panel.append(el('p', RESULT[p.state] || p.state));
    if (p.state === 'paid' && p.reply) panel.append(el('p', 'The website replied: ' + p.reply));
    if (p.state === 'failed' && p.error) panel.append(el('p', 'Reason: ' + p.error));
    if (p.balance !== null) balance.textContent = server + ' · ' + p.balance + ' credits';
    if (p.state === 'sending') {
      // Another click or tab sends it. Its result comes when the visitor asks.
      const again = el('button', 'Check again');
      again.addEventListener('click', async () => {
        again.disabled = true;
        try { result(await api('GET', '/payments/' + p.id)); } catch (e) { again.disabled = false; }
      });
      panel.append(again);
    }
    const back = el('button', 'Back to the website');
    back.addEventListener('click', () => close(p));
    panel.append(back);
  }

  frame.addEventListener('load', refresh);
  form.submit();
})();

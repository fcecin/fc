// Markdown for the model's words in the chat. parse() builds a tree of plain
// nodes, which the Node tests check, and render() builds the DOM with
// createElement and text nodes only, so the model's text never becomes HTML.
// Links go only to http, https or mailto, and an image shows as a link, so
// the page loads nothing that the model names. An unclosed fence or mark
// shows as it is, so an answer that is still arriving shows whole.
(function (root, make) {
  const m = make();
  if (typeof module === 'object' && module.exports) module.exports = m;
  else { root.fc = root.fc || {}; root.fc.md = m; }
})(typeof self !== 'undefined' ? self : this, () => {
  'use strict';

  const SAFE = /^(?:https?:\/\/|mailto:)\S+$/i;
  const PUNCT = /[!-/:-@[-`{-~]/;
  const ALNUM = /[\p{L}\p{N}]/u;
  const SPACE = /\s/;
  const MAX_DEPTH = 12;               // the deepest nesting of quotes and marks, past which it is text

  const el = (tag, kids = [], attrs) => (attrs ? { tag, kids, attrs } : { tag, kids });
  const a = (href, kids) => el('a', kids, { href, target: '_blank', rel: 'noopener noreferrer' });

  // --- Blocks -----------------------------------------------------------------

  const FENCE = /^( {0,3})(`{3,}|~{3,})[ \t]*([^\s`]*)[^`]*$/;
  const HEADING = /^ {0,3}(#{1,6})(?:[ \t]+(.*?))?(?:[ \t]+#+)?[ \t]*$/;
  const RULE = /^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$/;
  const QUOTE = /^ {0,3}> ?(.*)$/;
  const BULLET = /^( *)[-*+](?:[ \t]+(.*)|$)/;
  const ORDERED = /^( *)(\d{1,9})[.)](?:[ \t]+(.*)|$)/;
  const DELIM = /^ *\|? *:?-+:? *(?:\| *:?-+:? *)*\|? *$/;

  const indent = (l) => l.length - l.trimStart().length;

  // The marker of a list item: {indent, col (where its text starts),
  // ordered, start, text}, or null.
  function marker(line) {
    let m = BULLET.exec(line);
    let ordered = false;
    let start = 1;
    if (!m) {
      m = ORDERED.exec(line);
      if (!m) return null;
      ordered = true;
      start = parseInt(m[2], 10);
    }
    const text = ordered ? m[3] || '' : m[2] || '';
    const col = text ? line.length - text.length : m[1].length + 2;
    return { indent: m[1].length, col, ordered, start, text };
  }

  function cells(line) {
    let s = line.trim();
    if (s.startsWith('|')) s = s.slice(1);
    if (s.endsWith('|') && !s.endsWith('\\|')) s = s.slice(0, -1);
    const out = [];
    let cur = '';
    let code = false;
    for (let k = 0; k < s.length; k++) {
      const c = s[k];
      if (c === '\\' && s[k + 1] === '|') { cur += '|'; k++; continue; }
      if (c === '`') code = !code;
      if (c === '|' && !code) { out.push(cur.trim()); cur = ''; continue; }
      cur += c;
    }
    out.push(cur.trim());
    return out;
  }

  function tableAt(lines, i) {
    return i + 1 < lines.length && lines[i].includes('|') && DELIM.test(lines[i + 1])
      && cells(lines[i]).length === cells(lines[i + 1]).length;
  }

  // A line that ends a paragraph, since it starts another block. A list
  // interrupts a paragraph only with a bullet or the number 1.
  function starts(lines, i) {
    const l = lines[i];
    if (FENCE.test(l) || HEADING.test(l) || RULE.test(l) || QUOTE.test(l) || tableAt(lines, i)) return true;
    const m = marker(l);
    return !!m && m.text !== '' && (!m.ordered || m.start === 1);
  }

  function blocks(lines, depth) {
    const out = [];
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      if (!line.trim()) { i++; continue; }
      if (depth > MAX_DEPTH) { out.push(el('p', inline(lines.slice(i).join('\n'), 0))); break; }
      let m;
      if ((m = FENCE.exec(line))) {
        const [, pad, mark, lang] = m;
        const end = new RegExp(`^ {0,3}${mark[0] === '`' ? '`' : '~'}{${mark.length},}[ \\t]*$`);
        const code = [];
        for (i++; i < lines.length && !end.test(lines[i]); i++) {
          code.push(lines[i].slice(Math.min(pad.length, indent(lines[i]))));
        }
        i++;
        out.push(el('pre', [el('code', [code.join('\n')])], lang ? { 'data-lang': lang } : undefined));
        continue;
      }
      if ((m = HEADING.exec(line))) {
        out.push(el(`h${m[1].length}`, inline(m[2] || '', 0)));
        i++;
        continue;
      }
      if (RULE.test(line)) { out.push(el('hr')); i++; continue; }
      if (QUOTE.test(line)) {
        const inner = [];
        while (i < lines.length) {
          const q = QUOTE.exec(lines[i]);
          if (q) inner.push(q[1]);
          else if (lines[i].trim() && inner.length && inner[inner.length - 1].trim() && !starts(lines, i)) inner.push(lines[i]);
          else break;
          i++;
        }
        out.push(el('blockquote', blocks(inner, depth + 1)));
        continue;
      }
      if (marker(line)) {
        const [node, next] = list(lines, i, depth);
        out.push(node);
        i = next;
        continue;
      }
      if (tableAt(lines, i)) {
        const head = cells(lines[i]);
        const aligns = cells(lines[i + 1]).map((c) => (c.startsWith(':') && c.endsWith(':') ? 'center'
          : c.endsWith(':') ? 'right' : c.startsWith(':') ? 'left' : ''));
        const cell = (tag, text, k) => el(tag, inline(text, 0), aligns[k] ? { class: `al-${aligns[k]}` } : undefined);
        const rows = [];
        for (i += 2; i < lines.length && lines[i].trim() && lines[i].includes('|'); i++) {
          const r = cells(lines[i]);
          rows.push(el('tr', head.map((_, k) => cell('td', r[k] || '', k))));
        }
        const table = [el('thead', [el('tr', head.map((c, k) => cell('th', c, k)))])];
        if (rows.length) table.push(el('tbody', rows));
        out.push(el('div', [el('table', table)], { class: 'md-table' }));
        continue;
      }
      const para = [line.trim()];
      for (i++; i < lines.length && lines[i].trim() && !starts(lines, i); i++) para.push(lines[i].trim());
      out.push(el('p', inline(para.join('\n'), 0)));
    }
    return out;
  }

  // A list from line i: its items, each with the lines under it. Returns
  // [the node, the next line].
  function list(lines, i, depth) {
    const first = marker(lines[i]);
    const items = [];
    let loose = false;
    let gap = false;                   // blank lines before the next item
    while (i < lines.length) {
      const m = marker(lines[i]);
      if (!m || m.ordered !== first.ordered || RULE.test(lines[i])) break;
      if (gap) loose = true;
      const task = /^\[[ xX]\][ \t]+/.test(m.text);
      const body = [m.text.replace(/^\[([ xX])\][ \t]+/, (_, c) => (c === ' ' ? '☐ ' : '☑ '))];
      let blank = 0;
      for (i++; i < lines.length; i++) {
        const l = lines[i];
        if (!l.trim()) { blank++; continue; }
        if (indent(l) > m.indent) {
          if (blank) { body.push(''); loose = true; }
          body.push(l.slice(Math.min(indent(l), m.col)));
        } else if (!blank && !marker(l) && !starts(lines, i)) {
          body.push(l.trim());           // a lazy line of the item's paragraph
        } else {
          break;
        }
        blank = 0;
      }
      gap = blank > 0;
      items.push([blocks(body, depth + 1), task]);
    }
    const kids = items.map(([k, task]) => el('li', loose ? k : k.flatMap((b) => (b.tag === 'p' ? b.kids : [b])),
      task ? { class: 'task' } : undefined));
    const attrs = first.ordered && first.start !== 1 ? { start: String(first.start) } : undefined;
    return [el(first.ordered ? 'ol' : 'ul', kids, attrs), i];
  }

  // --- Inlines ----------------------------------------------------------------

  function run(s, i, c) {
    let n = 0;
    while (s[i + n] === c) n++;
    return n;
  }

  function closeTicks(s, i, n) {
    for (let j = s.indexOf('`', i); j >= 0; j = s.indexOf('`', j)) {
      const m = run(s, j, '`');
      if (m === n) return j;
      j += m;
    }
    return -1;
  }

  // Emphasis at i: ** or __ (strong), * or _ (em), ~~ (del). A mark opens
  // before a non-space and closes after one, and _ works only at word edges.
  // Returns [the node, the next index], or null. `open` keeps the marks of `s`
  // that found no close, since a later mark of the same kind finds none either.
  function emphasis(s, i, depth, open) {
    const c = s[i];
    const n = run(s, i, c);
    if ((c === '~' && n !== 2) || SPACE.test(s[i + n] || ' ')) return null;
    if (c === '_' && i && ALNUM.test(s[i - 1])) return null;
    const want = c === '~' || n >= 2 ? 2 : 1;
    const tag = c === '~' ? 'del' : want === 2 ? 'strong' : 'em';
    if (open.has(c + want)) return null;
    for (let j = i + want; j < s.length;) {
      const d = s[j];
      if (d === '\\') { j += 2; continue; }
      if (d === '`') {
        const m = run(s, j, '`');
        const e = closeTicks(s, j + m, m);
        j = e >= 0 ? e + m : j + m;
        continue;
      }
      if (d !== c) { j++; continue; }
      const m = run(s, j, c);
      if (j > i + want && !SPACE.test(s[j - 1]) && !(c === '_' && ALNUM.test(s[j + m] || ''))) {
        // In a longer run, the mark closes at its end: ***a*** is em in strong.
        if (want === 2 && m >= 2) return [el(tag, inline(s.slice(i + 2, j + m - 2), depth + 1)), j + m];
        if (want === 1 && m !== 2) return [el(tag, inline(s.slice(i + 1, j + m - 1), depth + 1)), j + m];
      }
      j += m;
    }
    open.add(c + want);
    return null;
  }

  // [text](url) or ![alt](url) at i (the "["). Returns [the node, the next
  // index], or null for an unsafe URL, which shows as text.
  const TARGET = /\(\s*<?([^\s<>()]*(?:\([^\s<>()]*\)[^\s<>()]*)*)>?(?:\s+(?:"[^"]*"|'[^']*'))?\s*\)/y;
  function link(s, i, depth, image) {
    let k = i + 1;
    for (let level = 1; k < s.length; k++) {
      const d = s[k];
      if (d === '\\') { k++; continue; }
      if (d === '[') level++;
      else if (d === ']' && --level === 0) break;
    }
    if (k >= s.length) return null;
    TARGET.lastIndex = k + 1;
    const m = TARGET.exec(s);
    if (!m || !SAFE.test(m[1])) return null;
    const label = s.slice(i + 1, k);
    return [a(m[1], image ? [label || m[1]] : inline(label, depth + 1)), TARGET.lastIndex];
  }

  // A bare URL ends before a space, and before the punctuation after it.
  const BARE = /https?:\/\/[^\s<]+/iy;
  const ANGLE = /<((?:https?:\/\/|mailto:)[^\s<>]+)>/iy;
  function trimUrl(u) {
    for (;;) {
      const last = u[u.length - 1];
      if (/[.,:;!?'"*_~]/.test(last)) u = u.slice(0, -1);
      else if (last === ')' && (u.match(/\(/g) || []).length < (u.match(/\)/g) || []).length) u = u.slice(0, -1);
      else return u;
    }
  }

  function inline(s, depth) {
    const out = [];
    const open = new Set();
    let text = '';
    const push = (node) => {
      if (text) { out.push(text); text = ''; }
      out.push(node);
    };
    for (let i = 0; i < s.length;) {
      const c = s[i];
      if (c === '\\' && PUNCT.test(s[i + 1] || '')) { text += s[i + 1]; i += 2; continue; }
      if (c === '\\' && s[i + 1] === '\n') { i++; continue; }
      if (c === '\n') { push(el('br')); i++; continue; }
      if (c === '`') {
        const n = run(s, i, '`');
        const e = closeTicks(s, i + n, n);
        if (e < 0) { text += s.slice(i, i + n); i += n; continue; }
        let code = s.slice(i + n, e).replace(/\n/g, ' ');
        if (code.length > 2 && code[0] === ' ' && code[code.length - 1] === ' ' && code.trim()) code = code.slice(1, -1);
        push(el('code', [code]));
        i = e + n;
        continue;
      }
      if (c === '*' || c === '_' || c === '~') {
        const got = depth < MAX_DEPTH ? emphasis(s, i, depth, open) : null;
        if (got) { push(got[0]); i = got[1]; continue; }
        const n = run(s, i, c);
        text += s.slice(i, i + n);
        i += n;
        continue;
      }
      if (c === '[' || (c === '!' && s[i + 1] === '[')) {
        const got = depth < MAX_DEPTH ? link(s, c === '!' ? i + 1 : i, depth, c === '!') : null;
        if (got) { push(got[0]); i = got[1]; continue; }
      }
      if (c === '<') {
        ANGLE.lastIndex = i;
        const m = ANGLE.exec(s);
        if (m) { push(a(m[1], [m[1]])); i = ANGLE.lastIndex; continue; }
      }
      if ((c === 'h' || c === 'H') && !(i && ALNUM.test(s[i - 1]))) {
        BARE.lastIndex = i;
        const m = BARE.exec(s);
        if (m) {
          const url = trimUrl(m[0]);
          if (url.length > 8) { push(a(url, [url])); i += url.length; continue; }
        }
      }
      text += c;
      i++;
    }
    if (text) out.push(text);
    return out;
  }

  // --- The tree and the DOM -------------------------------------------------------

  function parse(text) {
    return blocks(String(text).replace(/\r\n?/g, '\n').replace(/\t/g, '    ').split('\n'), 0);
  }

  function dom(n, doc) {
    if (typeof n === 'string') return doc.createTextNode(n);
    const e = doc.createElement(n.tag);
    for (const [k, v] of Object.entries(n.attrs || {})) e.setAttribute(k, v);
    for (const kid of n.kids) e.appendChild(dom(kid, doc));
    return e;
  }

  function render(text, doc = document) {
    const frag = doc.createDocumentFragment();
    for (const n of parse(text)) frag.appendChild(dom(n, doc));
    return frag;
  }

  return { parse, render };
});

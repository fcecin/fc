// Tests of the chat's markdown (app/fc/static/fc/md.js): the tree from
// parse(), and the DOM that render() builds from it on a fake document.
// Run: node --test tools/test/md.test.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const ROOT = new URL('../../', import.meta.url).pathname;
const md = require(ROOT + 'app/fc/static/fc/md.js');

// A tree as short HTML for comparing: the tags, their attrs and the text.
function html(nodes) {
  return nodes.map((n) => {
    if (typeof n === 'string') return n.replace(/&/g, '&amp;').replace(/</g, '&lt;');
    const attrs = Object.entries(n.attrs || {}).filter(([k]) => k !== 'target' && k !== 'rel')
      .map(([k, v]) => ` ${k}="${v}"`).join('');
    return n.tag === 'br' || n.tag === 'hr' ? `<${n.tag}>` : `<${n.tag}${attrs}>${html(n.kids)}</${n.tag}>`;
  }).join('');
}
const h = (text) => html(md.parse(text));

test('paragraphs, and a new line in one is a line break', () => {
  assert.equal(h('one\ntwo\n\nthree'), '<p>one<br>two</p><p>three</p>');
  assert.equal(h('  \n\n'), '');
});

test('headings, rules and quotes', () => {
  assert.equal(h('# Top\n## Two ##\n###### six\n#hashtag'), '<h1>Top</h1><h2>Two</h2><h6>six</h6><p>#hashtag</p>');
  assert.equal(h('a\n---\nb\n***'), '<p>a</p><hr><p>b</p><hr>');
  assert.equal(h('> said\nlazy\n> > deep'), '<blockquote><p>said<br>lazy</p><blockquote><p>deep</p></blockquote></blockquote>');
});

test('inline marks', () => {
  assert.equal(h('**bold** and *em* and _em_ and __strong__ and ~~gone~~'),
    '<p><strong>bold</strong> and <em>em</em> and <em>em</em> and <strong>strong</strong> and <del>gone</del></p>');
  assert.equal(h('***both*** and *a **b** c*'), '<p><strong><em>both</em></strong> and <em>a <strong>b</strong> c</em></p>');
  assert.equal(h('**Note:** it works'), '<p><strong>Note:</strong> it works</p>');
  // Marks that open nothing stay text.
  assert.equal(h('snake_case_name and foo_bar_ and 2 * 3 * 4 and ~/demo and **open'),
    '<p>snake_case_name and foo_bar_ and 2 * 3 * 4 and ~/demo and **open</p>');
  // An unclosed mark leaves a later mark of another kind alone.
  assert.equal(h('*a and **b**'), '<p>*a and <strong>b</strong></p>');
  assert.equal(h('\\*not em\\* and a\\\nb'), '<p>*not em* and a<br>b</p>');
});

test('code spans and fences', () => {
  assert.equal(h('run `cesh compute ps` now, and `` a`b ``'), '<p>run <code>cesh compute ps</code> now, and <code>a`b</code></p>');
  assert.equal(h('`**no marks**` and `open'), '<p><code>**no marks**</code> and `open</p>');
  assert.equal(h('```lua\nlocal x = 1 -- *x*\n\nreturn x\n```\nafter'),
    '<pre data-lang="lua"><code>local x = 1 -- *x*\n\nreturn x</code></pre><p>after</p>');
  // An unclosed fence, while the answer still arrives, takes the rest.
  assert.equal(h('text\n~~~\ncode'), '<p>text</p><pre><code>code</code></pre>');
});

test('lists', () => {
  assert.equal(h('Steps:\n1. one\n2. two'), '<p>Steps:</p><ol><li>one</li><li>two</li></ol>');
  assert.equal(h('3) three\n4) four'), '<ol start="3"><li>three</li><li>four</li></ol>');
  assert.equal(h('- a\n  - a1\n    - a11\n- b'),
    '<ul><li>a<ul><li>a1<ul><li>a11</li></ul></li></ul></li><li>b</li></ul>');
  assert.equal(h('1. **Put** it:\n   ```\n   cesh file put x\n   ```\n2. Launch'),
    '<ol><li><strong>Put</strong> it:<pre><code>cesh file put x</code></pre></li><li>Launch</li></ol>');
  assert.equal(h('- a\n\n- b'), '<ul><li><p>a</p></li><li><p>b</p></li></ul>');
  assert.equal(h('- [ ] todo\n- [x] done\n- [link](https://e.com)'),
    '<ul><li class="task">\u2610 todo</li><li class="task">\u2611 done</li><li><a href="https://e.com">link</a></li></ul>');
  assert.equal(h('* a\n* b\n\ntext'), '<ul><li>a</li><li>b</li></ul><p>text</p>');
  assert.equal(h('- a\n1. b'), '<ul><li>a</li></ul><ol><li>b</li></ol>');
  assert.equal(h('the year\n2026. was good'), '<p>the year<br>2026. was good</p>');
});

test('tables', () => {
  assert.equal(h('| pid | what |\n|---:|:---:|\n| 7 | `fortune` |\n| 16 | hud \\| test |'),
    '<div class="md-table"><table><thead><tr><th class="al-right">pid</th><th class="al-center">what</th></tr></thead>' +
    '<tbody><tr><td class="al-right">7</td><td class="al-center"><code>fortune</code></td></tr>' +
    '<tr><td class="al-right">16</td><td class="al-center">hud | test</td></tr></tbody></table></div>');
  assert.equal(h('a | b\n--|--'), '<div class="md-table"><table><thead><tr><th>a</th><th>b</th></tr></thead></table></div>');
  assert.equal(h('a | b\n---'), '<p>a | b</p><hr>');
});

test('links go only to http, https and mailto', () => {
  assert.equal(h('[the site](https://fc.example/x_(y)) and ![img](https://e.com/i.png)'),
    '<p><a href="https://fc.example/x_(y)">the site</a> and <a href="https://e.com/i.png">img</a></p>');
  assert.equal(h('[bad](javascript:alert(1)) [path](/h/abc/x.lua)'), '<p>[bad](javascript:alert(1)) [path](/h/abc/x.lua)</p>');
  assert.equal(h('see https://fc.example/a_b. Or <mailto:x@y.z>, (http://e.com/p)'),
    '<p>see <a href="https://fc.example/a_b">https://fc.example/a_b</a>. Or <a href="mailto:x@y.z">mailto:x@y.z</a>, (<a href="http://e.com/p">http://e.com/p</a>)</p>');
  assert.equal(h('[**bold** link](https://e.com)'), '<p><a href="https://e.com"><strong>bold</strong> link</a></p>');
});

test('HTML in the text stays text', () => {
  assert.deepEqual(md.parse('<img src=x onerror=alert(1)> <b>x</b>'), [{ tag: 'p', kids: ['<img src=x onerror=alert(1)> <b>x</b>'] }]);
});

test('deep nesting ends as text, not a deep stack', () => {
  const t = md.parse('>'.repeat(5000) + ' x\n' + '*'.repeat(5000) + 'y' + '*'.repeat(5000));
  assert.ok(Array.isArray(t));
  assert.ok(md.parse('*a '.repeat(3000)).length === 1);
});

// render(), on a fake document that records what the code built.
function fakeDoc() {
  const node = (tag) => ({ tag, attrs: {}, kids: [], setAttribute(k, v) { this.attrs[k] = v; }, appendChild(c) { this.kids.push(c); return c; } });
  return { createElement: node, createTextNode: (text) => ({ text }), createDocumentFragment: () => node('#frag') };
}

test('render makes elements and text nodes only', () => {
  const f = md.render('**a** [x](https://e.com) <script>', fakeDoc());
  const p = f.kids[0];
  assert.equal(p.tag, 'p');
  assert.deepEqual(p.kids[0].kids, [{ text: 'a' }]);
  assert.deepEqual(p.kids[2].attrs, { href: 'https://e.com', target: '_blank', rel: 'noopener noreferrer' });
  assert.deepEqual(p.kids[3], { text: ' <script>' });
});

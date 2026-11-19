// fc's CSP (style-src 'self') blocks the <style> elements and the style
// attributes that xterm.js makes. So this turns every <style> element into a
// constructable sheet in document.adoptedStyleSheets, which the CSP allows,
// and keeps the element out of the page. Load it before xterm.js.
(function () {
  'use strict';
  const sheets = new WeakMap();   // element -> {sheet, css}

  function sheetOf(el) {
    let s = sheets.get(el);
    if (!s) {
      s = { sheet: new CSSStyleSheet(), css: '' };
      sheets.set(el, s);
      document.adoptedStyleSheets = [...document.adoptedStyleSheets, s.sheet];
    }
    return s;
  }

  for (const prop of ['textContent', 'innerText', 'innerHTML']) {
    Object.defineProperty(HTMLStyleElement.prototype, prop, {
      configurable: true,
      get() { const s = sheets.get(this); return s ? s.css : ''; },
      set(css) {
        const s = sheetOf(this);
        s.css = String(css);
        s.sheet.replaceSync(s.css);
      },
    });
  }
  Object.defineProperty(HTMLStyleElement.prototype, 'sheet', {
    configurable: true,
    get() { return sheetOf(this).sheet; },
  });

  // A <style> element becomes its sheet, and stays out of the page.
  const isStyle = (n) => n instanceof HTMLStyleElement;
  const appendChild = Node.prototype.appendChild;
  Node.prototype.appendChild = function (n) {
    if (isStyle(n)) { sheetOf(n); return n; }
    return appendChild.call(this, n);
  };
  const insertBefore = Node.prototype.insertBefore;
  Node.prototype.insertBefore = function (n, ref) {
    if (isStyle(n)) { sheetOf(n); return n; }
    return insertBefore.call(this, n, ref);
  };
  for (const name of ['append', 'prepend']) {
    const f = Element.prototype[name];
    Element.prototype[name] = function (...nodes) {
      for (const n of nodes) if (isStyle(n)) sheetOf(n);
      return f.apply(this, nodes.filter((n) => !isStyle(n)));
    };
  }

  const setAttribute = Element.prototype.setAttribute;
  Element.prototype.setAttribute = function (name, value) {
    if (this.style && String(name).toLowerCase() === 'style') {
      this.style.cssText = String(value);
      return;
    }
    return setAttribute.call(this, name, value);
  };

  const remove = Element.prototype.remove;
  HTMLStyleElement.prototype.remove = function () {
    const s = sheets.get(this);
    if (s) {
      document.adoptedStyleSheets = document.adoptedStyleSheets.filter((x) => x !== s.sheet);
      sheets.delete(this);
    }
    return remove.call(this);
  };
})();

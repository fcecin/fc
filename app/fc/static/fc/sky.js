// The sky: the server where you are and its neighbors, as wireframe planets
// over a grid, placed by the site's layout of the network (fc/net.py). Drag
// to turn, wheel to come near, click a planet to fly to it, and "go there" to
// move your marker along the link.
(function () {
  'use strict';

  const IN = document.body.dataset.in === '1';
  const STILL = matchMedia('(prefers-reduced-motion: reduce)').matches;
  const R = 30;            // the radius of a planet
  const GROUND = -150;     // the grid's height
  const GRID = 50, REACH = 950;
  const COL = {
    ink: '#251a47', ink1: '#4a3d78', ink2: '#8a7fb0', magenta: '#e2337f', teal: '#0e9f9f',
    sun: '#ffb347', leaf: '#2f9e5f', paper: '#fffaf3',
  };
  const MONO = '12px "Courier New", ui-monospace, Menlo, monospace';
  const MONO_B = 'bold 13px "Courier New", ui-monospace, Menlo, monospace';

  function rng(s) {
    let h = 2166136261;
    for (let i = 0; i < s.length; i++) h = Math.imul(h ^ s.charCodeAt(i), 16777619);
    return () => {
      h = Math.imul(h ^ (h >>> 15), 2246822507);
      h = Math.imul(h ^ (h >>> 13), 3266489909);
      h ^= h >>> 16;
      return (h >>> 0) / 4294967296;
    };
  }
  const lerp = (a, b, t) => a + (b - a) * t;
  const ease = (u) => (u < 0.5 ? 4 * u * u * u : 1 - Math.pow(-2 * u + 2, 3) / 2);
  const V = (x, y, z) => ({ x, y, z });
  const add = (a, b) => V(a.x + b.x, a.y + b.y, a.z + b.z);
  const mix = (a, b, t) => V(lerp(a.x, b.x, t), lerp(a.y, b.y, t), lerp(a.z, b.z, t));
  const dist3 = (a, b) => Math.hypot(a.x - b.x, a.y - b.y, a.z - b.z);
  function rgba(hex, a) {
    const n = parseInt(hex.slice(1), 16);
    return `rgba(${n >> 16},${(n >> 8) & 255},${n & 255},${a})`;
  }

  // --- Planet places -------------------------------------------

  const GLIDE = 1400;      // ms for a planet to glide to a new place
  const placeOf = (n) => (n.pos ? V(n.pos[0], n.pos[1], n.pos[2]) : V(0, 0, 0));

  // --- The bodies ------------------------------------------------------------

  class Body {
    constructor(n, pos) {
      const r = rng(n.address);
      this.address = n.address;
      this.n = n;
      this.pos = pos;
      this.tilt = (r() - 0.5) * 0.9;
      this.tiltDir = r() * Math.PI * 2;
      this.spin = r() * Math.PI * 2;
      this.speed = (0.1 + r() * 0.15) * (r() < 0.5 ? -1 : 1);
      this.meridians = 5 + Math.floor(r() * 5);
      this.lats = 3 + Math.floor(r() * 3);
      this.vis = 0;
      this.want = 1;
    }
    get color() {
      const n = this.n;
      if (!n.up) return COL.ink2;
      if (n.ledger) return COL.magenta;
      if (n.main) return COL.leaf;
      return n.compute ? COL.teal : COL.ink1;
    }
    // A point on the surface (lat, lon) as an offset from the center.
    surface(lat, lon, rad) {
      const cl = Math.cos(lat);
      let x = cl * Math.cos(lon + this.spin) * rad, y = Math.sin(lat) * rad, z = cl * Math.sin(lon + this.spin) * rad;
      const ct = Math.cos(this.tilt), st = Math.sin(this.tilt);
      const y1 = y * ct - z * st, z1 = y * st + z * ct;
      const cd = Math.cos(this.tiltDir), sd = Math.sin(this.tiltDir);
      return V(x * cd - z1 * sd, y1, x * sd + z1 * cd);
    }
  }

  // --- The state ---------------------------------------------------------------

  const bodies = new Map();
  let graph = null;
  let hereAddr = null;
  let focus = null;              // the address of the planet in view
  let hover = null;
  let travel = null;             // {from, to, t0, dur, ok, failed, back}
  const packets = [];            // credit on its way: {from, to, t0, dur}
  const cam = { target: V(0, 0, 0), yaw: 0.7, pitch: 0.36, dist: 860, wantDist: 860 };
  const near = () => (W && W < 720 ? 0.62 : 1);
  let lastTouch = 0, idle0 = performance.now();
  let canvas, g2, W = 0, H = 0, F = 1, dpr = 1, midY = 0;
  let probe, probeFor = null, probeW = 0, probeH = 0, probeX = null, probeY = null;

  // --- Projection --------------------------------------------------------------

  // A world point in camera space, {x, y, z}, with the camera at the origin.
  function toCam(p) {
    const dx = p.x - cam.target.x, dy = p.y - cam.target.y, dz = p.z - cam.target.z;
    const cy = Math.cos(cam.yaw), sy = Math.sin(cam.yaw), cp = Math.cos(cam.pitch), sp = Math.sin(cam.pitch);
    const x = dx * cy - dz * sy, z = dx * sy + dz * cy;
    return V(x, dy * cp + z * sp, -dy * sp + z * cp + cam.dist);
  }
  function rot(o) { // a direction in camera space
    const cy = Math.cos(cam.yaw), sy = Math.sin(cam.yaw), cp = Math.cos(cam.pitch), sp = Math.sin(cam.pitch);
    const x = o.x * cy - o.z * sy, z = o.x * sy + o.z * cy;
    return V(x, o.y * cp + z * sp, -o.y * sp + z * cp);
  }
  function screen(c) { return { x: W / 2 + (c.x * F) / c.z, y: midY - (c.y * F) / c.z, s: F / c.z }; }
  function project(p) { const c = toCam(p); return c.z < 15 ? null : Object.assign(screen(c), { d: c.z }); }

  // --- Drawing -----------------------------------------------------------------

  function polyline(pts, front, back) {
    // pts: [{s: screen point or null, f: front or back}]
    for (let i = 1; i < pts.length; i++) {
      const a = pts[i - 1], b = pts[i];
      if (!a.s || !b.s) continue;
      const path = a.f && b.f ? front : back;
      path.moveTo(a.s.x, a.s.y); path.lineTo(b.s.x, b.s.y);
    }
  }

  function drawGround() {
    const ox = Math.round(cam.target.x / GRID) * GRID, oz = Math.round(cam.target.z / GRID) * GRID;
    const LEVELS = 6, paths = { x: [], z: [] };
    for (let l = 0; l < LEVELS; l++) { paths.x.push(new Path2D()); paths.z.push(new Path2D()); }
    const N = Math.round(REACH / GRID), SEG = 20;
    for (const axis of ['x', 'z']) {
      for (let i = -N; i <= N; i++) {
        let prev = null, prevP = null;
        for (let k = 0; k <= SEG; k++) {
          const t = -REACH + (2 * REACH * k) / SEG;
          const w = axis === 'x' ? V(ox + i * GRID, GROUND, oz + t) : V(ox + t, GROUND, oz + i * GRID);
          const s = project(w);
          if (s && prev) {
            const mid = mix(w, prevP, 0.5);
            const fade = 1 - Math.hypot(mid.x - cam.target.x, mid.z - cam.target.z) / REACH;
            if (fade > 0) {
              const l = Math.min(LEVELS - 1, Math.floor(fade * LEVELS));
              paths[axis][l].moveTo(prev.x, prev.y); paths[axis][l].lineTo(s.x, s.y);
            }
          }
          prev = s; prevP = w;
        }
      }
    }
    g2.lineWidth = 1;
    for (let l = 0; l < LEVELS; l++) {
      const a = ((l + 1) / LEVELS) * 0.34;
      g2.strokeStyle = rgba(COL.magenta, a); g2.stroke(paths.x[l]);
      g2.strokeStyle = rgba(COL.teal, a); g2.stroke(paths.z[l]);
    }
  }

  // The sun on the horizon, with its lower half cut into bands.
  function drawSun() {
    const A = 0.45, c = Math.cos(A - cam.yaw);
    if (c < 0.15) return;
    const far = REACH * 0.92;
    const s = project(V(cam.target.x + Math.sin(A) * far, GROUND, cam.target.z + Math.cos(A) * far));
    if (!s) return;
    const x = s.x, horizon = s.y;
    const r = Math.min(W, H) * 0.17;
    if (x < -r || x > W + r) return;
    const a = Math.min(1, (c - 0.15) * 3) * 0.9;
    g2.save();
    g2.beginPath();
    g2.rect(0, 0, W, horizon + 1);
    g2.clip();
    const grad = g2.createLinearGradient(0, horizon - r, 0, horizon + r * 0.2);
    grad.addColorStop(0, rgba(COL.sun, 0.75 * a));
    grad.addColorStop(1, rgba(COL.magenta, 0.55 * a));
    g2.fillStyle = grad;
    g2.beginPath(); g2.arc(x, horizon - r * 0.15, r, 0, Math.PI * 2); g2.fill();
    g2.globalCompositeOperation = 'destination-out';
    for (let i = 0; i < 7; i++) {
      const y = horizon - r * 0.62 + (i / 7) * r * 0.62, h = 1.2 + i * 1.5;
      g2.fillRect(x - r - 2, y, 2 * r + 4, h);
    }
    g2.restore();
  }

  function arc(a, b, u) {
    const lift = 40 + dist3(a, b) * 0.12;
    const m = V((a.x + b.x) / 2, Math.max(a.y, b.y) + lift, (a.z + b.z) / 2);
    const p = mix(mix(a, m, u), mix(m, b, u), u);
    return p;
  }

  function drawLinks(now) {
    if (!graph) return;
    const here = bodies.get(hereAddr);
    if (!here) return;
    for (const b of bodies.values()) {
      if (b === here || b.vis < 0.02) continue;
      const hot = b.address === focus || b.address === hover || (travel && travel.to === b);
      g2.beginPath();
      let started = false;
      for (let k = 0; k <= 32; k++) {
        const s = project(arc(here.pos, b.pos, k / 32));
        if (!s) { started = false; continue; }
        if (!started) { g2.moveTo(s.x, s.y); started = true; } else g2.lineTo(s.x, s.y);
      }
      g2.setLineDash([3, 7]);
      g2.lineDashOffset = STILL ? 0 : -(now / 50) % 10;
      g2.strokeStyle = rgba(hot ? COL.magenta : COL.ink1, (hot ? 0.85 : 0.45) * Math.min(b.vis, here.vis));
      g2.lineWidth = hot ? 2 : 1.2;
      g2.stroke();
      g2.setLineDash([]);
      g2.lineDashOffset = 0;
    }
  }

  function drawStalk(b, s) {
    const a = b.vis;
    const foot = V(b.pos.x, GROUND, b.pos.z);
    const top = project(V(b.pos.x, b.pos.y - R, b.pos.z)), bot = project(foot);
    if (!top || !bot) return;
    g2.setLineDash([2, 4]);
    g2.strokeStyle = rgba(b.color, 0.5 * a);
    g2.lineWidth = 1;
    g2.beginPath(); g2.moveTo(top.x, top.y); g2.lineTo(bot.x, bot.y); g2.stroke();
    g2.setLineDash([]);
    g2.beginPath();
    for (let k = 0; k <= 28; k++) {
      const t = (k / 28) * Math.PI * 2;
      const p = project(V(foot.x + Math.cos(t) * R * 0.9, GROUND, foot.z + Math.sin(t) * R * 0.9));
      if (!p) return;
      if (k === 0) g2.moveTo(p.x, p.y); else g2.lineTo(p.x, p.y);
    }
    g2.strokeStyle = rgba(b.color, 0.55 * a);
    g2.stroke();
  }

  function drawPlanet(b, now) {
    const s = project(b.pos);
    if (!s) return;
    const a = b.vis, here = b.address === hereAddr, hot = b.address === focus || b.address === hover;
    const col = b.color;
    const c0 = toCam(b.pos);
    drawStalk(b, s);

    const front = new Path2D(), back = new Path2D();
    const ring = (fn) => {
      const pts = [];
      for (let k = 0; k <= 40; k++) {
        const o = fn(k / 40);
        const c = toCam(add(b.pos, o));
        const n = rot(o);
        pts.push({ s: c.z < 15 ? null : screen(c), f: n.x * c.x + n.y * c.y + n.z * c.z < 0 });
      }
      polyline(pts, front, back);
    };
    for (let i = 1; i <= b.lats; i++) {
      const lat = -Math.PI / 2 + (i * Math.PI) / (b.lats + 1);
      ring((u) => b.surface(lat, u * Math.PI * 2, R));
    }
    for (let i = 0; i < b.meridians; i++) {
      const lon = (i * Math.PI) / b.meridians;
      ring((u) => b.surface(-Math.PI / 2 + u * Math.PI * 2, lon, R));
    }
    const rings = [];
    if (b.n.ledger) {
      for (const rr of [1.55, 1.78]) {
        const f2 = new Path2D(), b2 = new Path2D(), pts = [];
        for (let k = 0; k <= 56; k++) {
          const o = b.surface(0, (k / 56) * Math.PI * 2, R * rr);
          const c = toCam(add(b.pos, o));
          pts.push({ s: c.z < 15 ? null : screen(c), f: c.z < c0.z });
        }
        polyline(pts, f2, b2);
        rings.push([f2, b2]);
      }
    }

    // The main server's moon, on a tilted orbit.
    let moon = null;
    if (b.n.main) {
      const orbit = (t) => {
        const x = Math.cos(t) * R * 2.1, z = Math.sin(t) * R * 2.1;
        return V(x, z * Math.sin(0.55), z * Math.cos(0.55));
      };
      const f2 = new Path2D(), b2 = new Path2D(), pts = [];
      for (let k = 0; k <= 64; k++) {
        const c = toCam(add(b.pos, orbit((k / 64) * Math.PI * 2)));
        pts.push({ s: c.z < 15 ? null : screen(c), f: c.z < c0.z });
      }
      polyline(pts, f2, b2);
      const mc = toCam(add(b.pos, orbit((STILL ? 0 : now / 2600) + b.spin)));
      moon = { f: f2, b: b2, s: mc.z < 15 ? null : screen(mc), front: mc.z < c0.z };
    }
    const drawMoon = () => {
      if (!moon.s) return;
      const mr = R * 0.24 * moon.s.s;
      g2.beginPath(); g2.arc(moon.s.x, moon.s.y, mr, 0, Math.PI * 2);
      g2.fillStyle = rgba(COL.paper, 0.9 * a); g2.fill();
      g2.strokeStyle = rgba(COL.leaf, 0.95 * a); g2.lineWidth = 1.4; g2.stroke();
    };

    const lw = here ? 1.7 : hot ? 1.6 : 1.1;
    g2.lineWidth = lw * 0.8;
    g2.strokeStyle = rgba(col, 0.22 * a);
    g2.stroke(back);
    for (const [, b2] of rings) { g2.strokeStyle = rgba(COL.sun, 0.5 * a); g2.stroke(b2); }
    if (moon) {
      g2.setLineDash([2, 5]); g2.strokeStyle = rgba(COL.leaf, 0.35 * a); g2.stroke(moon.b); g2.setLineDash([]);
      if (!moon.front) drawMoon();
    }
    // The body: a disc of air that hides most of what is behind it.
    const rs = R * s.s;
    const grad = g2.createRadialGradient(s.x - rs * 0.35, s.y - rs * 0.35, rs * 0.1, s.x, s.y, rs);
    grad.addColorStop(0, rgba(COL.paper, 0.75 * a));
    grad.addColorStop(1, rgba(col, 0.16 * a));
    g2.fillStyle = grad;
    g2.beginPath(); g2.arc(s.x, s.y, rs, 0, Math.PI * 2); g2.fill();
    g2.strokeStyle = rgba(col, (hot ? 0.95 : 0.7) * a);
    g2.lineWidth = lw * 1.2;
    g2.stroke();
    g2.lineWidth = lw;
    if (!b.n.up) g2.setLineDash([3, 4]);
    g2.strokeStyle = rgba(col, 0.9 * a);
    g2.stroke(front);
    g2.setLineDash([]);
    for (const [f2] of rings) { g2.strokeStyle = rgba(COL.sun, 0.95 * a); g2.lineWidth = 1.6; g2.stroke(f2); }
    if (moon) {
      g2.setLineDash([2, 5]); g2.strokeStyle = rgba(COL.leaf, 0.75 * a); g2.lineWidth = 1.2; g2.stroke(moon.f); g2.setLineDash([]);
      if (moon.front) drawMoon();
    }

    const ly = s.y + rs + 18;
    label(b.address, s.x, ly, here ? COL.ink : COL.ink1, a, here ? MONO_B : MONO, 'center');
    const sub = !b.n.up ? 'does not answer' : b.n.ledger ? 'home' : b.n.main ? 'main' :
      b.n.balance && parseFloat(b.n.balance) > 0 ? `${b.n.balance} credit` : '';
    if (sub) label(sub, s.x, ly + 15, b.n.ledger ? COL.magenta : b.n.main ? COL.leaf : COL.ink1, a * 0.85, MONO, 'center');
    b.screen = { x: s.x, y: s.y, r: rs, d: s.d };
  }

  function label(text, x, y, color, a, font, align) {
    g2.font = font;
    g2.textAlign = align || 'left';
    g2.lineJoin = 'round';
    g2.strokeStyle = rgba(COL.paper, 0.85 * a);
    g2.lineWidth = 4;
    g2.strokeText(text, x, y);
    g2.fillStyle = rgba(color, a);
    g2.fillText(text, x, y);
    g2.textAlign = 'left';
  }

  function avatarAt(now) {
    const here = bodies.get(hereAddr);
    if (!here) return null;
    const bob = STILL ? 0 : Math.sin(now / 600) * 4;
    const over = (b) => add(b.pos, V(0, R + 30 + bob, 0));
    if (!travel) return over(here);
    const u = travelU(now);
    const a = over(travel.from), b = over(travel.to);
    return arc(a, b, u);
  }
  function travelU(now) {
    const t = travel;
    if (t.back) return Math.max(0, t.backFrom * (1 - (now - t.back) / 600));
    let u = t.dur ? (now - t.t0) / t.dur : 1;
    // Until the server answers, wait near the end, and keep moving.
    if (!t.ok && u > 0.86) u = 0.86 + Math.sin((now - t.t0) / 180) * 0.01;
    return ease(Math.min(1, u));
  }

  function drawAvatar(now) {
    if (!IN) return;
    const p = avatarAt(now);
    if (!p) return;
    const sz = 9, t = STILL ? 0 : now / 900;
    const vs = [V(0, sz * 1.4, 0), V(0, -sz * 1.4, 0)];
    for (let k = 0; k < 4; k++) vs.push(V(Math.cos(t + (k * Math.PI) / 2) * sz, 0, Math.sin(t + (k * Math.PI) / 2) * sz));
    const ps = vs.map((v) => project(add(p, v)));
    if (ps.some((x) => !x)) return;
    g2.strokeStyle = COL.magenta;
    g2.lineWidth = 1.6;
    g2.beginPath();
    for (let k = 2; k < 6; k++) {
      const n = k === 5 ? 2 : k + 1;
      g2.moveTo(ps[k].x, ps[k].y); g2.lineTo(ps[n].x, ps[n].y);
      g2.moveTo(ps[k].x, ps[k].y); g2.lineTo(ps[0].x, ps[0].y);
      g2.moveTo(ps[k].x, ps[k].y); g2.lineTo(ps[1].x, ps[1].y);
    }
    g2.stroke();
    const top = project(add(p, V(0, sz * 1.4 + 6, 0)));
    if (top) label(me || 'you', top.x, top.y - 4, COL.magenta, 1, MONO_B, 'center');
  }

  function drawPackets(now) {
    for (let i = packets.length - 1; i >= 0; i--) {
      const k = packets[i], u = (now - k.t0) / k.dur;
      if (u >= 1) { packets.splice(i, 1); feel.sound.coin(); continue; }
      for (let j = 0; j < 5; j++) {
        const uu = ease(Math.max(0, u - j * 0.03));
        const s = project(arc(k.from.pos, k.to.pos, uu));
        if (!s) continue;
        g2.beginPath(); g2.arc(s.x, s.y, Math.max(1.5, (5 - j) * s.s * 1.1), 0, Math.PI * 2);
        g2.fillStyle = rgba(COL.sun, 1 - j * 0.18); g2.fill();
      }
    }
  }

  // --- The probe: what is there for you -------------------------------------------

  function credit(n) { return n.balance === undefined || n.balance === null ? null : n.balance; }

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }

  function buildProbe() {
    const b = focus && bodies.get(focus);
    if (!b || travel) { probe.hidden = true; probeFor = null; return; }
    // Keep the probe while the person types into it.
    if (probeFor === b.address && probe.contains(document.activeElement) && document.activeElement !== document.body) return;
    const n = b.n, here = b.address === hereAddr;
    const key = JSON.stringify([n, here, IN]);
    if (probeFor === b.address && probe.dataset.key === key) return;
    probe.dataset.key = key;
    probeFor = b.address;
    probe.textContent = '';
    probe.style.setProperty('--tone', b.color);
    probe.append(el('h2', null, n.address));
    const what = !n.up ? 'does not answer' : n.ledger ? 'home · programs do not run here' :
      n.main ? (n.compute ? 'main · runs programs' : 'main') :
      n.compute ? 'runs programs' : 'programs do not run here';
    probe.append(el('p', 'what', what));
    if (IN) {
      // A server's state is read when a person looks, so ask again here.
      const again = el('button', 'again', 'ask again'); again.type = 'button';
      again.addEventListener('click', () => ask(b.address, again));
      probe.append(again);
    }
    if (IN && n.up) {
      const c = credit(n);
      const p = el('p', 'bal');
      if (n.account === false && !(c && parseFloat(c) > 0)) p.textContent = 'you have no account here';
      else { p.append('you have '); p.append(el('strong', null, `${c || '0'} credit`)); p.append(' here'); }
      probe.append(p);
    }
    if (IN && here) {
      probe.append(el('p', 'you', 'you are here'));
      const f = el('form', 'inline');
      const to = el('input'); to.name = 'to'; to.placeholder = 'handle or key'; to.required = true; to.spellcheck = false;
      to.setAttribute('aria-label', 'to');
      const amt = el('input'); amt.name = 'amount'; amt.inputMode = 'decimal'; amt.placeholder = 'amount'; amt.required = true;
      amt.setAttribute('aria-label', 'amount');
      const btn = el('button', null, 'send'); btn.type = 'submit';
      f.append(to, amt, btn);
      f.addEventListener('submit', (e) => { e.preventDefault(); send(to.value.trim(), amt.value.trim(), btn, f); });
      probe.append(f);
    } else if (IN && n.up) {
      const go_ = el('button', 'primary', 'go there'); go_.type = 'button';
      go_.addEventListener('click', () => go(b.address, go_));
      const row = el('div', 'acts');
      row.append(go_);
      probe.append(row);
      const c = credit(n);
      if (c && parseFloat(c) > 0 && bodies.get(hereAddr)) {
        const f = el('form', 'inline');
        const amt = el('input'); amt.name = 'amount'; amt.inputMode = 'decimal'; amt.required = true;
        amt.placeholder = 'amount'; amt.setAttribute('aria-label', 'amount');
        const btn = el('button', null, 'bring to where you are'); btn.type = 'submit';
        f.append(amt, btn);
        f.addEventListener('submit', (e) => { e.preventDefault(); bring(b, amt.value.trim(), btn); });
        probe.append(f);
      }
    }
    probe.hidden = false;
    probeW = probe.offsetWidth; probeH = probe.offsetHeight;
    if (probe.dataset.for !== b.address) { probeX = null; placeProbe.at = undefined; }
    probe.dataset.for = b.address;
  }

  // The places for the probe of a planet at screen point `s` with radius `r`,
  // to its right or left, or in a top corner of the free sky. On a tie, the
  // first wins.
  function probeSpots(s, r) {
    const [top] = scene();
    const y = Math.max(18, Math.min(H - probeH - 28, s.y - probeH * 0.4));
    return [
      { at: 'right', x: Math.min(W - probeW - 28, s.x + r + 90), y },
      { at: 'left', x: Math.max(28, s.x - r - 90 - probeW), y },
      { at: 'top right', x: W - probeW - 28, y: top + 18 },
      { at: 'top left', x: 28, y: top + 18 },
    ];
  }

  // How much the probe at `p` covers the boxes `others` ([x0, y0, x1, y1]),
  // plus four times its cover of its own planet `own`, which it avoids most.
  function spotCover(p, others, own) {
    const box = [p.x, p.y, p.x + probeW, p.y + probeH];
    const over = (a) => Math.max(0, Math.min(a[2], box[2]) - Math.max(a[0], box[0])) *
      Math.max(0, Math.min(a[3], box[3]) - Math.max(a[1], box[1]));
    return others.reduce((n, o) => n + over(o), 0) + 4 * over(own);
  }

  function placeProbe() {
    const b = probeFor && bodies.get(probeFor);
    if (!b || !b.screen || probe.hidden) return;
    const s = b.screen;
    if (W < 720) {
      probe.style.transform = '';
      probe.classList.add('docked');
      const bar = document.querySelector('.bar');
      probe.style.top = `${bar ? Math.max(0, bar.getBoundingClientRect().bottom) : 0}px`;
      return;
    }
    probe.classList.remove('docked');
    probe.style.top = '';
    const others = [...bodies.values()].filter((o) => o !== b && o.screen && o.vis >= 0.5)
      .map((o) => [o.screen.x - o.screen.r, o.screen.y - o.screen.r, o.screen.x + o.screen.r, o.screen.y + o.screen.r + 40]);
    const spots = probeSpots(s, s.r).map((p) => ({ ...p, cover: spotCover(p, others, [s.x - s.r, s.y - s.r, s.x + s.r, s.y + s.r]) }));
    let pick = spots.reduce((m, p) => (p.cover < m.cover ? p : m));
    const kept = spots.find((p) => p.at === placeProbe.at);
    if (kept && kept !== pick && kept.cover - pick.cover < 4000) pick = kept;
    placeProbe.at = pick.at;
    const x = pick.x, y = pick.y;
    if (probeX === null) { probeX = x; probeY = y; }
    probeX = lerp(probeX, x, 0.12); probeY = lerp(probeY, y, 0.12);
    probe.style.transform = `translate(${probeX.toFixed(1)}px, ${probeY.toFixed(1)}px)`;
    const ex = Math.max(probeX - 6, Math.min(probeX + probeW + 6, s.x));
    const ey = Math.max(probeY - 6, Math.min(probeY + probeH + 6, s.y));
    const ang = Math.atan2(ey - s.y, ex - s.x);
    g2.strokeStyle = rgba(b.color, 0.7);
    g2.lineWidth = 1;
    g2.setLineDash([2, 4]);
    g2.beginPath();
    g2.moveTo(s.x + Math.cos(ang) * (s.r + 6), s.y + Math.sin(ang) * (s.r + 6));
    g2.lineTo(ex, ey);
    g2.stroke();
    g2.setLineDash([]);
  }

  // The top and the middle of the scene's free space, under what covers it.
  function scene() {
    let top = 0;
    const bar = document.querySelector('.bar');
    if (bar) top = Math.max(0, bar.getBoundingClientRect().bottom);
    if (probe.classList.contains('docked') && !probe.hidden) top = Math.max(top, probe.getBoundingClientRect().bottom);
    return [top, top + (H - top) * 0.55];
  }

  // --- The view ------------------------------------------------------------------

  // Where the camera looks: at the middle of the planets on show when the
  // planet in view is yours, else at the planet in view. With `ahead`, it uses
  // where the planets are going.
  function aim(ahead) {
    const at = (b) => (ahead && b.goal) || b.pos;
    const f = bodies.get(focus);
    if (f && focus !== hereAddr) return at(f);
    const shown = [...bodies.values()].filter((b) => b.want === 1);
    if (!shown.length) return null;
    const c = shown.reduce((m, b) => add(m, at(b)), V(0, 0, 0));
    return V(c.x / shown.length, c.y / shown.length, c.z / shown.length);
  }

  // The sky turns to where the planets and their names cover each other least.
  // Of VIEWS turns around the planet in view, it takes the middle of the
  // longest run of the best turns, and sways inside that run when idle, or all
  // the way round when every turn is clear. A drag turns it anywhere.
  const VIEWS = 72;
  const MAX_SWAY = 0.6;          // the widest sway from the run's middle, in radians
  let view = null;               // {yaw, half, t0}

  // The cover at `yaw`, in px²: [how much the planets, their names, your
  // marker and the probe cover each other, how much of them is off screen].
  function cover(yaw, target, dist) {
    const keep = { yaw: cam.yaw, target: cam.target, dist: cam.dist, midY };
    const [top, mid] = scene();
    Object.assign(cam, { yaw, target, dist });
    midY = mid;
    const boxes = [];
    let marker = null, probed = null;
    for (const b of bodies.values()) {
      if (b.want !== 1) continue;
      const p = b.goal || b.pos, s = project(p);
      if (!s) continue;
      const r = R * s.s;
      g2.font = b.address === hereAddr ? MONO_B : MONO;
      const half = Math.max(r, g2.measureText(b.address).width / 2 + 4);
      const box = [s.x - half, s.y - r, s.x + half, s.y + r + 36];
      boxes.push(box);
      if (b.address === focus) probed = { s, r, box };
      // Your marker and its name float over the planet where you are.
      const m = IN && b.address === hereAddr && project(add(p, V(0, R + 50, 0)));
      if (m) {
        g2.font = MONO_B;
        const w = Math.max(24, g2.measureText(me || 'you').width / 2 + 4);
        marker = [m.x - w, m.y - 18, m.x + w, s.y - r - 2];
      }
    }
    midY = keep.midY;
    Object.assign(cam, { yaw: keep.yaw, target: keep.target, dist: keep.dist });
    const over = (a, c) => Math.max(0, Math.min(a[2], c[2]) - Math.max(a[0], c[0])) *
      Math.max(0, Math.min(a[3], c[3]) - Math.max(a[1], c[1]));
    let covered = 0, off = 0;
    for (let i = 0; i < boxes.length; i++) {
      const a = boxes[i];
      off += (a[2] - a[0]) * (a[3] - a[1]) - over(a, [0, top, W, H]);
      for (let j = i + 1; j < boxes.length; j++) covered += over(a, boxes[j]);
      if (marker) covered += over(a, marker);
    }
    if (probed && probeW && !probe.hidden && !probe.classList.contains('docked')) {
      const { s, r, box } = probed;
      const others = boxes.filter((o) => o !== box);
      covered += Math.min(...probeSpots(s, r).map((p) => spotCover(p, others, box)));
    }
    return [covered, off];
  }

  // The good turns cover little more than the best turn (two names' corners
  // can touch), and put little more off screen than the best of those.
  const COVER_SLACK = 400;       // px²
  const OFF_SLACK = 4000;        // px²
  function chooseView(now) {
    const target = aim(true);
    if (!target || !W) return;
    const step = (Math.PI * 2) / VIEWS;
    const scores = [];
    for (let k = 0; k < VIEWS; k++) scores.push(cover(k * step, target, cam.wantDist));
    const least = Math.min(...scores.map(([c]) => c));
    const clear = scores.map(([c]) => c <= least + COVER_SLACK);
    const leastOff = Math.min(...scores.filter((_, k) => clear[k]).map(([, o]) => o));
    const good = scores.map(([, o], k) => clear[k] && o <= leastOff + OFF_SLACK);
    if (good.every(Boolean)) { view = { yaw: cam.yaw, half: Infinity, t0: now }; return; }
    let best = null;
    for (let k = 0; k < VIEWS; k++) {
      if (!good[k] || good[(k + VIEWS - 1) % VIEWS]) continue;
      let n = 0;
      while (good[(k + n) % VIEWS]) n++;
      if (!best || n > best.n) best = { k, n };
    }
    view = { yaw: (best.k + (best.n - 1) / 2) * step, half: Math.min(MAX_SWAY, ((best.n - 1) / 2) * step), t0: now, scores };
  }

  function idleTurn(now, dt) {
    const ramp = Math.min(1, (now - Math.max(lastTouch, idle0)) / 6000);
    if (!view || view.half === Infinity) { cam.yaw += dt * 0.035 * ramp; return; }
    const want = view.yaw + view.half * Math.sin(((now - view.t0) / 1000) * 0.08);
    cam.yaw += Math.atan2(Math.sin(want - cam.yaw), Math.cos(want - cam.yaw)) * Math.min(1, dt * 0.8) * ramp;
  }

  // --- The frame ---------------------------------------------------------------

  function frame(now) {
    requestAnimationFrame(frame);
    if (document.hidden || !W) return;
    const dt = Math.min(0.05, (now - (frame.last || now)) / 1000);
    frame.last = now;

    for (const b of bodies.values()) {
      b.vis = lerp(b.vis, b.want, STILL ? 1 : 0.06);
      if (!STILL && b.n.up) b.spin += b.speed * dt;
      if (b.goal) {
        const u = STILL ? 1 : Math.min(1, (now - b.t0) / GLIDE);
        b.pos = mix(b.from, b.goal, ease(u));
        if (u >= 1) b.goal = null;
      }
    }
    let want = null;
    if (travel) want = avatarAt(now);
    else want = aim(false);
    if (want) cam.target = mix(cam.target, want, STILL ? 1 : travel ? 0.08 : 0.06);
    cam.dist = lerp(cam.dist, cam.wantDist, STILL ? 1 : 0.06);
    if (!STILL && !travel && now - lastTouch > 4000) idleTurn(now, dt);

    if (travel && !travel.back && travel.ok && (now - travel.t0) / travel.dur >= 1) arrive(now);
    if (travel && travel.back && now - travel.back > 600) { travel = null; buildProbe(); }

    const [, mid] = scene();
    midY = midY ? lerp(midY, mid, STILL ? 1 : 0.1) : mid;

    g2.setTransform(dpr, 0, 0, dpr, 0, 0);
    g2.clearRect(0, 0, W, H);
    drawSun();
    drawGround();
    drawLinks(now);
    const order = [...bodies.values()].filter((b) => b.vis > 0.02)
      .map((b) => [b, toCam(b.pos).z]).sort((p, q) => q[1] - p[1]);
    for (const [b] of order) drawPlanet(b, now);
    drawPackets(now);
    drawAvatar(now);
    placeProbe();
  }

  function size() {
    dpr = window.devicePixelRatio || 1;
    W = canvas.clientWidth; H = canvas.clientHeight;
    canvas.width = Math.round(W * dpr); canvas.height = Math.round(H * dpr);
    F = Math.min(W, H * 1.5) * 0.95;
    if (!size.done) { cam.dist = cam.wantDist = 860 * near(); size.done = true; }
    if (probe && !probe.hidden) { probeW = probe.offsetWidth; probeH = probe.offsetHeight; }
    if (cam.placed) chooseView(performance.now());
  }

  // --- Actions ----------------------------------------------------------------------

  function say(text) {
    const s = document.querySelector('[data-sky-say]');
    if (!s) return;
    s.textContent = text || '';
    s.classList.remove('show'); void s.offsetWidth;
    if (text) s.classList.add('show');
  }

  function setFocus(address, quiet) {
    if (focus === address) return;
    focus = address;
    cam.wantDist = (address === hereAddr ? 860 : 560) * near();
    if (!quiet) feel.sound.hover();
    buildProbe();
    chooseView(performance.now());
    if (!quiet) ask(address, null);
  }

  // Read one server again now. Only a person asks, and the site limits it.
  async function ask(address, button) {
    if (!IN || travel) return;
    try { merge(await feel.hold(button, fc.api('GET', `/api/net?fresh=${encodeURIComponent(address)}`))); }
    catch (e) { say(e.message || e.code); }
  }

  // A trip from where you are to `address`. `ok` turns true when the server
  // takes the move.
  function startTravel(address) {
    const from = bodies.get(hereAddr), to = bodies.get(address);
    if (!from || !to || travel) return false;
    travel = { from, to, t0: performance.now(), dur: STILL ? 0 : 1500, ok: false, failed: false, back: 0 };
    feel.sound.whoosh(1.5);
    focus = address;
    cam.wantDist = 620 * near();
    buildProbe();
    return true;
  }

  async function go(address, button) {
    if (!IN || travel || address === hereAddr) return;
    if (!startTravel(address)) return;
    try {
      await feel.hold(button, fc.api('POST', '/api/me', { server: address }));
      if (travel) travel.ok = true;
    } catch (e) {
      say(e.message || e.code);
      if (travel) { travel.backFrom = travelU(performance.now()); travel.back = performance.now(); }
      focus = hereAddr;
    }
  }

  function arrive() {
    hereAddr = travel.to.address;
    focus = hereAddr;
    cam.wantDist = 860 * near();
    travel = null;
    feel.sound.arrive();
    fc.fillMe();
    load();
  }

  async function bring(from, amount, button) {
    const here = bodies.get(hereAddr);
    try {
      await feel.hold(button, fc.api('POST', '/api/move', { from_server: from.address, amount }));
      packets.push({ from, to: here, t0: performance.now(), dur: STILL ? 1 : 1600 });
      say(`${amount} credit is on its way to ${hereAddr}.`);
      setTimeout(() => { fc.fillMe(); load(); }, 2500);
    } catch (e) { say(e.message || e.code); }
  }

  async function send(to, amount, button, form) {
    try {
      await feel.hold(button, fc.api('POST', '/api/send', { to, amount }), 'coin');
      form.reset();
      say(`Sent ${amount} credit to ${to}.`);
      fc.fillMe(); load();
    } catch (e) { say(e.message || e.code); }
  }

  // --- Data ------------------------------------------------------------------------

  function merge(g) {
    graph = g;
    if (!travel) hereAddr = g.here;
    const seen = new Set();
    for (const n of g.nodes) {
      seen.add(n.address);
      const p = placeOf(n);
      const b = bodies.get(n.address);
      if (!b) { bodies.set(n.address, new Body(n, p)); continue; }
      b.n = n;
      b.want = 1;
      const to = b.goal || b.pos;
      if (to.x !== p.x || to.y !== p.y || to.z !== p.z) Object.assign(b, { from: b.pos, goal: p, t0: performance.now() });
    }
    for (const b of bodies.values()) if (!seen.has(b.address)) b.want = 0;
    if (!focus || !bodies.has(focus) || bodies.get(focus).want === 0) focus = hereAddr;
    const first = !cam.placed;
    if (first) { const t = aim(true); if (t) cam.target = t; cam.placed = true; }
    buildProbe();
    chooseView(performance.now());
    if (first && view && view.half !== Infinity) cam.yaw = view.yaw;
  }

  async function load() {
    try { merge(await fc.api('GET', '/api/net')); }
    catch (e) { say(e.message || e.code); }
  }

  // --- Input ---------------------------------------------------------------------------

  function hit(x, y) {
    let best = null;
    for (const b of bodies.values()) {
      if (b.vis < 0.5 || !b.screen) continue;
      const s = b.screen;
      if (Math.hypot(s.x - x, s.y - y) < s.r + 10 && (!best || s.d < best.screen.d)) best = b;
    }
    return best ? { body: best } : null;
  }

  let me = '';

  function wireInput() {
    let drag = null, lastClick = { t: 0, a: null };
    const pos = (e) => { const r = canvas.getBoundingClientRect(); return [e.clientX - r.left, e.clientY - r.top]; };
    canvas.addEventListener('pointerdown', (e) => {
      drag = { x: e.clientX, y: e.clientY, yaw: cam.yaw, pitch: cam.pitch, moved: false };
      canvas.setPointerCapture(e.pointerId);
      lastTouch = performance.now();
    });
    canvas.addEventListener('pointermove', (e) => {
      lastTouch = performance.now();
      if (drag) {
        if (Math.abs(e.clientX - drag.x) + Math.abs(e.clientY - drag.y) > 4) drag.moved = true;
        if (drag.moved) {
          cam.yaw = drag.yaw + (e.clientX - drag.x) / 240;
          cam.pitch = Math.max(0.05, Math.min(1.15, drag.pitch + (e.clientY - drag.y) / 320));
          canvas.style.cursor = 'grabbing';
        }
        return;
      }
      const [x, y] = pos(e), h = hit(x, y);
      const a = h ? h.body.address : null;
      if (a !== hover) { hover = a; if (a) feel.sound.hover(); }
      canvas.style.cursor = h ? 'pointer' : 'grab';
    });
    canvas.addEventListener('pointerup', (e) => {
      const d = drag;
      drag = null;
      canvas.style.cursor = 'grab';
      if (!d || d.moved) return;
      const [x, y] = pos(e), h = hit(x, y), now = performance.now();
      if (!h) { setFocus(hereAddr); return; }
      const a = h.body.address;
      if (lastClick.a === a && now - lastClick.t < 380 && a !== hereAddr) go(a, null);
      else setFocus(a);
      lastClick = { t: now, a };
    });
    canvas.addEventListener('wheel', (e) => {
      e.preventDefault();
      lastTouch = performance.now();
      cam.wantDist = Math.max(260, Math.min(1600, cam.wantDist * Math.exp(e.deltaY * 0.0012)));
    }, { passive: false });
    addEventListener('keydown', (e) => {
      if (e.target.closest && e.target.closest('input, textarea')) return;
      if (e.key === 'Escape') setFocus(hereAddr);
      if (e.key === 'Enter' && focus && focus !== hereAddr) go(focus, probe.querySelector('button.primary'));
    });
    addEventListener('resize', size);
  }

  // What the sky shows, for scripts to check.
  window.fcSky = {
    planets: () => [...bodies.values()].filter((b) => b.screen && b.vis > 0.5)
      .map((b) => ({ address: b.address, x: b.screen.x, y: b.screen.y, r: b.screen.r })),
    here: () => hereAddr, focus: () => focus, travelling: () => !!travel, view: () => view,
    yaw: () => cam.yaw,
  };

  document.addEventListener('DOMContentLoaded', async () => {
    canvas = document.querySelector('[data-sky]');
    if (!canvas) return;
    probe = document.querySelector('[data-probe]');
    g2 = canvas.getContext('2d');
    canvas.style.cursor = 'grab';
    size();
    wireInput();
    requestAnimationFrame(frame);
    if (IN) fc.fillMe().then((m) => { if (m) me = m.handle || ''; });
    await load();
    // The network changed (fc.hub, "net"), or the socket reconnected, so read
    // it once. A trip reads it when it ends.
    const again = () => { if (!travel) load(); };
    fc.hub.on('net', again);
    fc.hub.on('open', again);
  });
})();

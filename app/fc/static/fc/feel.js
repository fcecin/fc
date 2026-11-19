// Feel: the page's sound and touch. Every button goes down under the finger
// and comes up when the finger leaves, and a button whose act waits for a
// server stays down until the server answers. The sounds come from Web Audio,
// made here.
(function () {
  'use strict';

  let ctx = null, out = null;
  let on = localStorage.getItem('fc.sound') !== '0';

  function audio() {
    if (!on) return null;
    if (!ctx) {
      const AC = window.AudioContext || window.webkitAudioContext;
      if (!AC) return null;
      ctx = new AC();
      const comp = ctx.createDynamicsCompressor();
      out = ctx.createGain();
      out.gain.value = 0.55;
      out.connect(comp);
      comp.connect(ctx.destination);
    }
    if (ctx.state === 'suspended') ctx.resume();
    return ctx.state === 'running' ? ctx : null;
  }

  // One note, a frequency that can glide, with a short attack and a decay.
  function tone(f0, dur, { type = 'sine', gain = 0.2, f1 = f0, at = 0, attack = 0.004 } = {}) {
    const c = audio();
    if (!c) return;
    const t = c.currentTime + at, o = c.createOscillator(), g = c.createGain();
    o.type = type;
    o.frequency.setValueAtTime(f0, t);
    if (f1 !== f0) o.frequency.exponentialRampToValueAtTime(f1, t + dur);
    g.gain.setValueAtTime(0.0001, t);
    g.gain.exponentialRampToValueAtTime(gain, t + attack);
    g.gain.exponentialRampToValueAtTime(0.0001, t + dur);
    o.connect(g); g.connect(out);
    o.start(t); o.stop(t + dur + 0.02);
  }

  let noiseBuf = null;
  // A burst of noise through a band filter that can sweep.
  function noise(dur, { f0 = 2000, f1 = f0, q = 1.2, gain = 0.2, at = 0, attack = 0.002 } = {}) {
    const c = audio();
    if (!c) return;
    if (!noiseBuf) {
      noiseBuf = c.createBuffer(1, c.sampleRate * 2, c.sampleRate);
      const d = noiseBuf.getChannelData(0);
      for (let i = 0; i < d.length; i++) d[i] = Math.random() * 2 - 1;
    }
    const t = c.currentTime + at, s = c.createBufferSource(), f = c.createBiquadFilter(), g = c.createGain();
    s.buffer = noiseBuf;
    f.type = 'bandpass'; f.Q.value = q;
    f.frequency.setValueAtTime(f0, t);
    if (f1 !== f0) f.frequency.exponentialRampToValueAtTime(f1, t + dur);
    g.gain.setValueAtTime(0.0001, t);
    g.gain.exponentialRampToValueAtTime(gain, t + attack);
    g.gain.exponentialRampToValueAtTime(0.0001, t + dur);
    s.connect(f); f.connect(g); g.connect(out);
    s.start(t, Math.random()); s.stop(t + dur + 0.02);
  }

  const sound = {
    press() { noise(0.03, { f0: 1600, q: 2, gain: 0.25 }); tone(150, 0.05, { gain: 0.12, f1: 90 }); },
    release() { noise(0.018, { f0: 3200, q: 3, gain: 0.12 }); },
    ok() { tone(660, 0.12, { type: 'triangle', gain: 0.12 }); tone(990, 0.22, { type: 'triangle', gain: 0.1, at: 0.07 }); },
    err() { tone(150, 0.12, { type: 'square', gain: 0.05 }); tone(110, 0.16, { type: 'square', gain: 0.05, at: 0.1 }); },
    hover() { tone(1900, 0.04, { gain: 0.02 }); },
    coin() { tone(1318, 0.08, { type: 'square', gain: 0.03 }); tone(1975, 0.25, { type: 'square', gain: 0.03, at: 0.06 }); },
    whoosh(d = 1.2) { noise(d, { f0: 260, f1: 2400, q: 0.8, gain: 0.16, attack: d * 0.6 }); },
    arrive() {
      [523.25, 659.25, 783.99, 1046.5].forEach((f, i) => tone(f, 0.9, { type: 'triangle', gain: 0.06, at: i * 0.05 }));
    },
  };

  // --- Touch -----------------------------------------------------------------

  const PRESSABLE = 'button, .button, [role="button"]';

  document.addEventListener('pointerdown', (e) => {
    if (e.button !== 0) return;
    const b = e.target.closest(PRESSABLE);
    if (!b || b.disabled) return;
    b.classList.add('pressed');
    sound.press();
    const up = () => {
      removeEventListener('pointerup', up); removeEventListener('pointercancel', up);
      b.classList.remove('pressed');
      if (!b.classList.contains('held')) sound.release();
    };
    addEventListener('pointerup', up); addEventListener('pointercancel', up);
  }, true);

  // A keyboard key presses the focused button.
  document.addEventListener('keydown', (e) => {
    if ((e.key === 'Enter' || e.key === ' ') && e.target.matches && e.target.matches('button')) {
      e.target.classList.add('pressed'); sound.press();
      setTimeout(() => e.target.classList.remove('pressed'), 90);
    }
  }, true);

  function spring(el, ok) {
    const cls = ok ? 'sprung' : 'shake';
    el.classList.remove('sprung', 'shake'); void el.offsetWidth; el.classList.add(cls);
    setTimeout(() => el.classList.remove(cls), 420);
  }

  // Holds `el` down while `p` waits, then lets it up, or shakes it if `p`
  // failed. `done` names the end's sound, "ok" or "coin", or nothing for a
  // plain key coming up.
  async function hold(el, p, done) {
    if (!el) return p;
    el.classList.add('held');
    el.setAttribute('aria-busy', 'true');
    try {
      const r = await p;
      spring(el, true);
      (sound[done] || sound.release)();
      return r;
    } catch (e) {
      spring(el, false);
      sound.err();
      throw e;
    } finally {
      el.classList.remove('held');
      el.removeAttribute('aria-busy');
    }
  }

  function setSound(v) {
    on = v;
    localStorage.setItem('fc.sound', v ? '1' : '0');
    for (const b of document.querySelectorAll('[data-sound]')) b.textContent = v ? 'sound on' : 'sound off';
    if (v) sound.ok();
  }

  document.addEventListener('DOMContentLoaded', () => {
    for (const b of document.querySelectorAll('[data-sound]')) {
      b.textContent = on ? 'sound on' : 'sound off';
      b.addEventListener('click', () => setSound(!on));
    }
  });

  window.feel = { sound, hold, spring };
})();

/* motion.js: a tiny deterministic timeline for frame-exact video rendering.
 *
 * The whole video is a program: seek(t) recomputes every animated property from
 * scratch for time t, so any frame can be rendered in any order and twice over
 * with identical pixels. No dependencies, no clocks, no requestAnimationFrame.
 *
 *   const tl = Motion.timeline();
 *   tl.set('#title', { opacity: 0 }, 0);
 *   tl.to('#title', { opacity: 1, y: 0 }, { at: 0.2, dur: 0.8, ease: 'outExpo' });
 *   tl.from('#dot', { scale: 0 }, { at: 1.0, dur: 0.6, ease: 'outBack' });
 *   tl.to('.card', { y: -20 }, { at: 2, dur: 0.5, each: 0.08 });   // stagger
 *   tl.to('#n', { value: 900, format: v => Math.round(v).toLocaleString() }, { at: 3, dur: 1.2 });
 *   window.seek = t => tl.seek(t);
 *
 * Properties: x, y (px), scale, scaleX, scaleY, rotate (deg), opacity, blur (px),
 * value (number, written through `format`), dash (stroke-dashoffset), w, h (px),
 * r, cx, cy (SVG attributes), fillOpacity, strokeOpacity, left, top (px), fontSize (px).
 * Do not overlap two tweens on the same element+property; the later one would read a
 * moving start value.
 */
(function (global) {
  'use strict';

  const EASES = {
    linear: t => t,
    inQuad: t => t * t,
    outQuad: t => 1 - (1 - t) * (1 - t),
    inOutQuad: t => (t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2),
    outCubic: t => 1 - Math.pow(1 - t, 3),
    inOutCubic: t => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2),
    outExpo: t => (t >= 1 ? 1 : 1 - Math.pow(2, -10 * t)),
    inExpo: t => (t <= 0 ? 0 : Math.pow(2, 10 * t - 10)),
    outBack: t => {
      const c1 = 1.70158, c3 = c1 + 1;
      return 1 + c3 * Math.pow(t - 1, 3) + c1 * Math.pow(t - 1, 2);
    },
    // Damped spring: a little overshoot, then settles. Good for things "landing".
    spring: t => (t >= 1 ? 1 : 1 - Math.exp(-7 * t) * Math.cos(10 * t)),
    steps: n => t => Math.min(1, Math.floor(t * n) / n),
  };

  // Defaults for properties that have a natural identity value. Properties set to
  // null are read from the element the first time they are animated.
  // opacity is null on purpose: its starting value is read from the element's computed style, so a
  // CSS rule such as `.beat { opacity: 0 }` is respected until the first tween touches it.
  const DEFAULTS = {
    x: 0, y: 0, scale: 1, scaleX: 1, scaleY: 1, rotate: 0, opacity: null, blur: 0, value: 0,
    dash: null, w: null, h: null, r: null, cx: null, cy: null,
    fillOpacity: null, strokeOpacity: null, left: null, top: null, fontSize: null,
  };
  const STATE_DEFAULTS = Object.assign({}, DEFAULTS, { opacity: 1 });
  const TRANSFORM_PROPS = new Set(['x', 'y', 'scale', 'scaleX', 'scaleY', 'rotate']);

  function resolve(target) {
    if (typeof target === 'string') return Array.from(document.querySelectorAll(target));
    if (target instanceof Element) return [target];
    if (target && typeof target.length === 'number') return Array.from(target);
    throw new Error('Motion: cannot resolve target ' + target);
  }

  function num(v, fallback) {
    const n = parseFloat(v);
    return Number.isFinite(n) ? n : fallback;
  }

  // Initial value of a null-default property, read once at build time, before any seek.
  function initial(el, prop) {
    const cs = global.getComputedStyle(el);
    switch (prop) {
      case 'opacity': return num(cs.opacity, 1);
      case 'dash': return num(el.style.strokeDashoffset, num(el.getAttribute('stroke-dashoffset'), num(cs.strokeDashoffset, 0)));
      case 'w': return el.getBoundingClientRect().width;
      case 'h': return el.getBoundingClientRect().height;
      case 'r': case 'cx': case 'cy': return num(el.getAttribute(prop), 0);
      case 'fillOpacity': return num(el.getAttribute('fill-opacity'), num(cs.fillOpacity, 1));
      case 'strokeOpacity': return num(el.getAttribute('stroke-opacity'), num(cs.strokeOpacity, 1));
      case 'left': return num(el.style.left, num(cs.left, 0));
      case 'top': return num(el.style.top, num(cs.top, 0));
      case 'fontSize': return num(cs.fontSize, 16);
      default: return DEFAULTS[prop];
    }
  }

  function apply(el, state, touched) {
    let transform = false;
    for (const p of touched) if (TRANSFORM_PROPS.has(p)) transform = true;
    if (transform) {
      const s = state;
      el.style.transform = `translate(${s.x}px, ${s.y}px) rotate(${s.rotate}deg) scale(${s.scaleX * s.scale}, ${s.scaleY * s.scale})`;
      if (el instanceof SVGElement && !(el instanceof SVGSVGElement)) {
        el.style.transformBox = 'fill-box';
        el.style.transformOrigin = 'center';
      }
    }
    if (touched.has('opacity')) el.style.opacity = String(state.opacity);
    if (touched.has('blur')) el.style.filter = state.blur > 0.01 ? `blur(${state.blur}px)` : '';
    if (touched.has('w')) el.style.width = state.w + 'px';
    if (touched.has('h')) el.style.height = state.h + 'px';
    if (touched.has('left')) el.style.left = state.left + 'px';
    if (touched.has('top')) el.style.top = state.top + 'px';
    if (touched.has('fontSize')) el.style.fontSize = state.fontSize + 'px';
    if (touched.has('dash')) el.style.strokeDashoffset = String(state.dash);
    if (touched.has('r')) el.setAttribute('r', String(state.r));
    if (touched.has('cx')) el.setAttribute('cx', String(state.cx));
    if (touched.has('cy')) el.setAttribute('cy', String(state.cy));
    if (touched.has('fillOpacity')) el.setAttribute('fill-opacity', String(state.fillOpacity));
    if (touched.has('strokeOpacity')) el.setAttribute('stroke-opacity', String(state.strokeOpacity));
    if (touched.has('value')) el.textContent = (el.__format || (v => String(Math.round(v))))(state.value);
  }

  function timeline() {
    // tracks: Map<Element, Map<prop, {init:number, tweens:[]}>>
    const tracks = new Map();
    let end = 0;

    function track(el, prop) {
      let t = tracks.get(el);
      if (!t) { t = new Map(); tracks.set(el, t); }
      let tr = t.get(prop);
      if (!tr) {
        tr = { init: DEFAULTS[prop] === null ? initial(el, prop) : DEFAULTS[prop], tweens: [] };
        t.set(prop, tr);
      }
      return tr;
    }

    function add(target, props, o, mode) {
      const els = resolve(target);
      const at = o.at !== undefined ? o.at : end;
      const dur = o.dur !== undefined ? o.dur : (mode === 'set' ? 0 : 0.6);
      const ease = typeof o.ease === 'function' ? o.ease : (EASES[o.ease || 'outCubic'] || EASES.outCubic);
      els.forEach((el, i) => {
        const start = at + (o.delay || 0) + (o.each || 0) * i;
        for (const [p, v] of Object.entries(props)) {
          if (p === 'format') { el.__format = v; continue; }
          if (!(p in DEFAULTS)) throw new Error('Motion: unknown property ' + p);
          const tr = track(el, p);
          // mode 'to'/'set': animate current -> v.   mode 'from': animate v -> current.
          tr.tweens.push(mode === 'from'
            ? { start, dur, ease, from: v, to: undefined }
            : { start, dur, ease, from: undefined, to: v });
          tr.tweens.sort((a, b) => a.start - b.start);
        }
        end = Math.max(end, start + dur);
      });
      return api;
    }

    function valueAt(tr, t) {
      let v = tr.init;
      for (const tw of tr.tweens) {
        if (t < tw.start) break;
        const from = tw.from !== undefined ? tw.from : v;
        const to = tw.to !== undefined ? tw.to : v;
        if (tw.dur <= 0 || t >= tw.start + tw.dur) { v = to; continue; }
        const k = tw.ease((t - tw.start) / tw.dur);
        v = from + (to - from) * k;
      }
      return v;
    }

    const api = {
      set(target, props, at) { return add(target, props, { at: at === undefined ? 0 : at, dur: 0 }, 'set'); },
      to(target, props, o = {}) { return add(target, props, o, 'to'); },
      from(target, props, o = {}) { return add(target, props, o, 'from'); },
      duration() { return end; },
      seek(t) {
        t = Math.max(0, +t || 0);
        for (const [el, props] of tracks) {
          const state = Object.assign({}, STATE_DEFAULTS);
          const touched = new Set();
          for (const [p, tr] of props) { state[p] = valueAt(tr, t); touched.add(p); }
          apply(el, state, touched);
        }
        return t;
      },
    };
    return api;
  }

  global.Motion = { timeline, EASES, DEFAULTS };
})(window);

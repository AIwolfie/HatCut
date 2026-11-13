/* Hatcut editor.
 *
 * The preview is the interesting part. ffmpeg cannot give us frame-accurate
 * scrubbing without a full render, so instead of faking it we layer the real
 * clips: one muted <video> per visible picture clip, stacked in track order
 * and transformed with CSS to match each clip's transform. Audio comes from a
 * single element that follows whichever audio clip owns the playhead, because
 * mixing several sources in the browser would drift and lie.
 *
 * Everything else is bookkeeping over the JSON API.
 */

'use strict';

// --------------------------------------------------------------- utilities

const $ = (id) => document.getElementById(id);

function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

function timecode(seconds) {
  const s = Math.max(0, Number(seconds) || 0);
  const m = Math.floor(s / 60);
  const rest = s - m * 60;
  return `${m}:${rest.toFixed(2).padStart(5, '0')}`;
}

function shortTime(seconds) {
  const s = Math.max(0, Number(seconds) || 0);
  const m = Math.floor(s / 60);
  const rest = Math.floor(s - m * 60);
  return `${m}:${String(rest).padStart(2, '0')}`;
}

function bytes(count) {
  const n = Number(count) || 0;
  if (n < 1024) return `${n} B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  let value = n / 1024, i = 0;
  while (value >= 1024 && i < units.length - 1) { value /= 1024; i += 1; }
  return `${value.toFixed(value < 10 ? 1 : 0)} ${units[i]}`;
}

function toast(message, kind = '') {
  const node = document.createElement('div');
  node.className = `toast ${kind}`;
  node.textContent = message;
  $('toasts').appendChild(node);
  setTimeout(() => {
    node.style.transition = 'opacity .25s';
    node.style.opacity = '0';
    setTimeout(() => node.remove(), 260);
  }, kind === 'bad' ? 7000 : 3600);
}

// -------------------------------------------------------------------- api

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  const text = await response.text();
  let payload = null;
  try { payload = text ? JSON.parse(text) : null; } catch { payload = { error: text }; }
  if (!response.ok) {
    throw new Error((payload && payload.error) || `HTTP ${response.status}`);
  }
  return payload;
}

// ----------------------------------------------------------- history & waveforms

const undoStack = [];
const redoStack = [];
const MAX_HISTORY = 50;

const waveformCache = new Map();
const waveformLoading = new Set();

function updateUndoRedoUI() {
  const u = $('btn-undo');
  const r = $('btn-redo');
  if (u) u.disabled = undoStack.length === 0;
  if (r) r.disabled = redoStack.length === 0;
}

function pushUndo(snapshot) {
  if (!snapshot || !snapshot.timeline) return;
  undoStack.push(JSON.parse(JSON.stringify({
    timeline: snapshot.timeline,
    name: snapshot.name,
    description: snapshot.description,
    media_cache: snapshot.media_cache,
  })));
  if (undoStack.length > MAX_HISTORY) undoStack.shift();
  redoStack.length = 0;
  updateUndoRedoUI();
}

async function undo() {
  if (undoStack.length === 0) return;
  const prev = undoStack.pop();
  if (state.data && state.data.timeline) {
    redoStack.push(JSON.parse(JSON.stringify({
      timeline: state.data.timeline,
      name: state.data.name,
      description: state.data.description,
      media_cache: state.data.media_cache,
    })));
  }
  try {
    const result = await api('/api/project', { method: 'POST', body: prev });
    applyState(result, { skipHistory: true });
    toast('undo', 'good');
  } catch (err) {
    toast(err.message, 'bad');
  }
  updateUndoRedoUI();
}

async function redo() {
  if (redoStack.length === 0) return;
  const next = redoStack.pop();
  if (state.data && state.data.timeline) {
    undoStack.push(JSON.parse(JSON.stringify({
      timeline: state.data.timeline,
      name: state.data.name,
      description: state.data.description,
      media_cache: state.data.media_cache,
    })));
  }
  try {
    const result = await api('/api/project', { method: 'POST', body: next });
    applyState(result, { skipHistory: true });
    toast('redo', 'good');
  } catch (err) {
    toast(err.message, 'bad');
  }
  updateUndoRedoUI();
}

// ------------------------------------------------------------------ state

const state = {
  data: null,
  selected: null,        // primary selected clip id
  selectedClips: new Set(), // multi-clip selection
  snapping: true,        // magnetic snapping
  playhead: 0,
  playing: false,
  pxPerSecond: 60,
  picking: new Set(),    // paths picked in the file browser
  browse: null,
  renderJob: null,
  pollTimer: null,
  drawMaskMode: false,
};

const trackKindOf = (kind) => ({ video: 'V', audio: 'A', text: 'T' }[kind] || '?');

function allClips() {
  if (!state.data) return [];
  const out = [];
  for (const track of state.data.timeline.tracks) {
    for (const clip of track.clips) out.push({ clip, track });
  }
  return out;
}

function clipById(id) {
  return allClips().find((entry) => entry.clip.id === id) || null;
}

function selectedTrackId() {
  const found = clipById(state.selected);
  return found ? found.track.id : null;
}

// ----------------------------------------------------------- preview stage

const stage = {
  layers: new Map(),   // clip id -> element
  audio: null,
  audioOwner: null,
  clock: 0,
  raf: null,
};

function sourceTime(clip, timelineTime) {
  return clip.in_point + (timelineTime - clip.start) * (clip.speed || 1);
}

function activeVideoClips(t) {
  if (!state.data) return [];
  const out = [];
  for (const track of state.data.timeline.tracks) {
    if (track.kind !== 'video') continue;
    for (const clip of track.clips) {
      if (clip.enabled === false) continue;
      if (t >= clip.start && t < clip.start + clip.duration) out.push(clip);
    }
  }
  return out;
}

function activeTextClips(t) {
  if (!state.data) return [];
  const out = [];
  for (const track of state.data.timeline.tracks) {
    if (track.kind !== 'text') continue;
    for (const clip of track.clips) {
      if (clip.enabled === false) continue;
      if (t >= clip.start && t < clip.start + clip.duration) out.push(clip);
    }
  }
  return out;
}

function activeAudioClip(t) {
  if (!state.data) return null;
  for (const track of state.data.timeline.tracks) {
    if (track.kind !== 'audio' || track.muted) continue;
    for (const clip of track.clips) {
      if (clip.enabled === false) continue;
      if (t >= clip.start && t < clip.start + clip.duration) return clip;
    }
  }
  return null;
}

/** Resolve an ffmpeg text position expression to stage pixels.
 *
 * ffmpeg writes positions in *project* pixels (`w` is the frame width,
 * `text_w` the rendered text width). The preview works in *stage* pixels, so
 * plain numbers get scaled and the centring forms are computed directly.
 * Anything more compound than that -- arithmetic on `t`, multiple terms --
 * cannot be evaluated here without reimplementing ffmpeg's expression parser,
 * so it centres and says so once. A silently misplaced caption is worse than
 * a warning.
 */
function resolvePos(expr, containerSize, elementSize, scale) {
  const text = String(expr ?? '').trim();
  const centre = () => (containerSize - elementSize) / 2;
  if (!text) return centre();
  if (/^\(?\s*[wh]\s*-\s*text_[wh]\s*\)?\s*\/\s*2$/.test(text)) return centre();
  if (/^-?\d+(\.\d+)?$/.test(text)) return Number(text) * scale;
  warnOnce(`pos:${text}`, `preview cannot evaluate the text position "${text}" — centring it; the render still uses it exactly`);
  return centre();
}

const warned = new Set();
function warnOnce(key, message) {
  if (warned.has(key)) return;
  warned.add(key);
  toast(message);
}

function fitStage() {
  const wrap = document.querySelector('.stage-wrap');
  const node = $('stage');
  if (!state.data || !wrap) return;
  const { width, height } = state.data.timeline.settings;
  const pad = 28;
  const availableW = Math.max(80, wrap.clientWidth - pad);
  const availableH = Math.max(60, wrap.clientHeight - pad);
  const scale = Math.min(availableW / width, availableH / height);
  node.style.width = `${Math.round(width * scale)}px`;
  node.style.height = `${Math.round(height * scale)}px`;
  updateMaskOverlay();
}

function proxyUrl(path) {
  return `/api/proxy?path=${encodeURIComponent(path)}`;
}

function ensureLayer(clip) {
  let node = stage.layers.get(clip.id);
  if (!node) {
    node = document.createElement('video');
    node.className = 'layer';
    node.muted = true;
    node.playsInline = true;
    node.preload = 'auto';
    node.src = proxyUrl(clip.source);
    stage.layers.set(clip.id, node);
    $('stage').appendChild(node);
  }
  return node;
}

function ensureTextLayer(clip) {
  let node = stage.layers.get(clip.id);
  if (!node) {
    node = document.createElement('div');
    node.className = 'layer text-layer';
    stage.layers.set(clip.id, node);
    $('stage').appendChild(node);
  }
  return node;
}

function getEffectiveTransform(clip, clipTime = 0) {
  const base = clip.transform || {};
  const kfs = (clip.transform_keyframes || []).slice().sort((a, b) => (a.time || 0) - (b.time || 0));
  if (kfs.length === 0) return base;
  if (kfs.length === 1 || clipTime <= (kfs[0].time || 0)) {
    return { ...base, ...kfs[0] };
  }
  if (clipTime >= (kfs[kfs.length - 1].time || 0)) {
    return { ...base, ...kfs[kfs.length - 1] };
  }
  let prev = kfs[0];
  let next = kfs[kfs.length - 1];
  for (let i = 0; i < kfs.length - 1; i++) {
    if (clipTime >= (kfs[i].time || 0) && clipTime <= (kfs[i + 1].time || 0)) {
      prev = kfs[i];
      next = kfs[i + 1];
      break;
    }
  }
  const dt = Math.max(1e-4, (next.time || 0) - (prev.time || 0));
  const alpha = Math.max(0, Math.min(1, (clipTime - (prev.time || 0)) / dt));
  const lerp = (a, b) => (a ?? 0) + ((b ?? 0) - (a ?? 0)) * alpha;
  return {
    scale: lerp(prev.scale ?? base.scale ?? 1, next.scale ?? base.scale ?? 1),
    x: lerp(prev.x ?? base.x ?? 0, next.x ?? base.x ?? 0),
    y: lerp(prev.y ?? base.y ?? 0, next.y ?? base.y ?? 0),
    rotate: lerp(prev.rotate ?? base.rotate ?? 0, next.rotate ?? base.rotate ?? 0),
    opacity: lerp(prev.opacity ?? base.opacity ?? 1, next.opacity ?? base.opacity ?? 1),
    fit: base.fit || 'contain',
  };
}

function applyTransform(node, clip, projectW, stageW, clipTime = 0) {
  const t = getEffectiveTransform(clip, clipTime);
  const k = stageW / projectW;
  const parts = [];
  if (t.scale && Math.abs(t.scale - 1) > 1e-9) {
    parts.push(`translate(${(t.x || 0) * k}px, ${(t.y || 0) * k}px)`);
    parts.push(`scale(${t.scale})`);
  } else if (t.x || t.y) {
    parts.push(`translate(${(t.x || 0) * k}px, ${(t.y || 0) * k}px)`);
  } else {
    parts.push('translate(0px, 0px)');
  }
  if (t.rotate) parts.push(`rotate(${t.rotate}deg)`);
  node.style.transform = parts.join(' ');
  node.style.opacity = t.opacity === undefined ? '1' : String(t.opacity);

  // Overlay blend mode
  const found = clipById(clip.id);
  const track = found ? found.track : null;
  const rawBlend = clip.blend_mode || (track && track.blend_mode) || 'normal';
  const blend = rawBlend === 'addition' ? 'lighter' : rawBlend;
  node.style.mixBlendMode = blend;

  // Preset color looks CSS preview
  const lookEffect = (clip.effects || []).find((e) => e && e.type === 'preset_look');
  if (lookEffect && lookEffect.params) {
    const lk = lookEffect.params.look;
    const intensity = lookEffect.params.intensity ?? 1;
    let cssFilter = '';
    if (lk === 'film_warm') cssFilter = `sepia(${0.25 * intensity}) saturate(${1 + 0.15 * intensity}) contrast(${1 + 0.1 * intensity})`;
    else if (lk === 'clean_cold') cssFilter = `hue-rotate(${190 * intensity}deg) saturate(${1 - 0.1 * intensity}) contrast(${1 + 0.1 * intensity})`;
    else if (lk === 'cyberpunk') cssFilter = `contrast(${1 + 0.3 * intensity}) saturate(${1 + 0.4 * intensity}) hue-rotate(${-15 * intensity}deg)`;
    else if (lk === 'vintage') cssFilter = `sepia(${0.4 * intensity}) contrast(${1 - 0.1 * intensity}) brightness(${1 + 0.05 * intensity})`;
    else if (lk === 'noir') cssFilter = `grayscale(${intensity}) contrast(${1 + 0.45 * intensity})`;
    else if (lk === 'teal_and_orange') cssFilter = `contrast(${1 + 0.2 * intensity}) saturate(${1 + 0.2 * intensity})`;
    else if (lk === 'bleach_bypass') cssFilter = `contrast(${1 + 0.3 * intensity}) saturate(${Math.max(0, 1 - 0.5 * intensity)})`;
    node.style.filter = cssFilter;
  } else {
    node.style.filter = '';
  }
}

function getEffectiveMaskParams(mask, clipTime = 0) {
  if (!mask || !mask.params) return {};
  const kfs = (mask.keyframes || []).slice().sort((a, b) => (a.time || 0) - (b.time || 0));
  if (kfs.length === 0) return mask.params;
  if (kfs.length === 1 || clipTime <= kfs[0].time) return { ...mask.params, ...(kfs[0].params || {}) };
  if (clipTime >= kfs[kfs.length - 1].time) return { ...mask.params, ...(kfs[kfs.length - 1].params || {}) };

  let prev = kfs[0];
  let next = kfs[kfs.length - 1];
  for (let i = 0; i < kfs.length - 1; i++) {
    if (clipTime >= kfs[i].time && clipTime <= kfs[i + 1].time) {
      prev = kfs[i];
      next = kfs[i + 1];
      break;
    }
  }
  const dt = Math.max(1e-4, next.time - prev.time);
  const alpha = Math.max(0, Math.min(1, (clipTime - prev.time) / dt));

  const out = { ...mask.params };
  const p0 = prev.params || {};
  const p1 = next.params || {};

  const keys = new Set([...Object.keys(mask.params), ...Object.keys(p0), ...Object.keys(p1)]);
  for (const k of keys) {
    if (k === 'points') {
      const pts0 = p0.points || mask.params.points || [];
      const pts1 = p1.points || mask.params.points || [];
      const len = Math.min(pts0.length, pts1.length);
      const lerpedPts = [];
      for (let j = 0; j < len; j++) {
        const x0 = pts0[j].x !== undefined ? pts0[j].x : pts0[j][0];
        const y0 = pts0[j].y !== undefined ? pts0[j].y : pts0[j][1];
        const x1 = pts1[j].x !== undefined ? pts1[j].x : pts1[j][0];
        const y1 = pts1[j].y !== undefined ? pts1[j].y : pts1[j][1];
        lerpedPts.push({
          x: x0 + (x1 - x0) * alpha,
          y: y0 + (y1 - y0) * alpha,
        });
      }
      out.points = lerpedPts;
    } else if (typeof p0[k] === 'number' || typeof p1[k] === 'number') {
      const v0 = typeof p0[k] === 'number' ? p0[k] : (mask.params[k] ?? 0);
      const v1 = typeof p1[k] === 'number' ? p1[k] : (mask.params[k] ?? 0);
      out[k] = v0 + (v1 - v0) * alpha;
    } else {
      out[k] = alpha < 0.5 ? (p0[k] ?? mask.params[k]) : (p1[k] ?? mask.params[k]);
    }
  }
  return out;
}

function applyMask(node, clip, clipTime = 0) {
  const mask = (clip.effects || []).find((e) =>
    e && (e.type === 'rectangle_mask' || e.type === 'ellipse_mask' || e.type === 'freeform_mask' ||
          e.type === 'polygon_mask' || e.type === 'linear_mask' || e.type === 'split_mask')
  );
  if (!mask || !mask.params) {
    node.style.clipPath = '';
    return;
  }
  const p = getEffectiveMaskParams(mask, clipTime);
  if (mask.type === 'rectangle_mask') {
    const x = Math.max(0, p.x ?? 0.2) * 100;
    const y = Math.max(0, p.y ?? 0.2) * 100;
    const w = Math.max(0.01, p.w ?? 0.6) * 100;
    const h = Math.max(0.01, p.h ?? 0.6) * 100;
    const top = y;
    const left = x;
    const bottom = Math.max(0, 100 - (y + h));
    const right = Math.max(0, 100 - (x + w));
    const round = Math.min(50, (p.round ?? 0) * 50);
    node.style.clipPath = `inset(${top.toFixed(2)}% ${right.toFixed(2)}% ${bottom.toFixed(2)}% ${left.toFixed(2)}% round ${round.toFixed(2)}%)`;
  } else if (mask.type === 'ellipse_mask') {
    const cx = (p.cx ?? 0.5) * 100;
    const cy = (p.cy ?? 0.5) * 100;
    const rx = (p.rx ?? 0.3) * 100;
    const ry = (p.ry ?? 0.3) * 100;
    node.style.clipPath = `ellipse(${rx.toFixed(2)}% ${ry.toFixed(2)}% at ${cx.toFixed(2)}% ${cy.toFixed(2)}%)`;
  } else if (mask.type === 'freeform_mask' || mask.type === 'polygon_mask') {
    const pts = p.points || [];
    if (pts.length >= 3) {
      const poly = pts.map((pt) => {
        const px = ((pt.x !== undefined ? pt.x : pt[0]) * 100).toFixed(2);
        const py = ((pt.y !== undefined ? pt.y : pt[1]) * 100).toFixed(2);
        return `${px}% ${py}%`;
      }).join(', ');
      node.style.clipPath = `polygon(${poly})`;
    } else {
      node.style.clipPath = '';
    }
  } else if (mask.type === 'linear_mask' || mask.type === 'split_mask') {
    const cx = (p.x ?? 0.5) * 100;
    const cy = (p.y ?? 0.5) * 100;
    const deg = p.angle ?? 0;
    const rad = deg * Math.PI / 180;
    const dx = Math.cos(rad);
    const dy = Math.sin(rad);
    const nx = -dy;
    const ny = dx;
    const s = p.inverted ? -1 : 1;
    const L = 250;
    const p1x = (cx - L * dx).toFixed(2);
    const p1y = (cy - L * dy).toFixed(2);
    const p2x = (cx + L * dx).toFixed(2);
    const p2y = (cy + L * dy).toFixed(2);
    const p3x = (cx + L * dx + 2 * L * s * nx).toFixed(2);
    const p3y = (cy + L * dy + 2 * L * s * ny).toFixed(2);
    const p4x = (cx - L * dx + 2 * L * s * nx).toFixed(2);
    const p4y = (cy - L * dy + 2 * L * s * ny).toFixed(2);
    node.style.clipPath = `polygon(${p1x}% ${p1y}%, ${p2x}% ${p2y}%, ${p3x}% ${p3y}%, ${p4x}% ${p4y}%)`;
  }
}

let maskOverlayBound = false;

function ensureMaskOverlay() {
  let svg = $('stage-mask-overlay');
  if (!svg) {
    svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.id = 'stage-mask-overlay';
    svg.className.baseVal = 'stage-mask-overlay';
    svg.setAttribute('viewBox', '0 0 100 100');
    svg.setAttribute('preserveAspectRatio', 'none');
    $('stage').appendChild(svg);
  }
  if (!maskOverlayBound) {
    maskOverlayBound = true;
    bindMaskOverlayEvents(svg);
  }
  return svg;
}

function updateMaskOverlay(clipTime = 0) {
  const stageNode = $('stage');
  if (!stageNode) return;
  const svg = ensureMaskOverlay();
  svg.innerHTML = '';
  svg.classList.remove('interactive');

  const found = state.selected ? clipById(state.selected) : null;
  if (!found || found.track.kind === 'audio') return;
  const clip = found.clip;
  const mask = (clip.effects || []).find((e) =>
    e && (e.type === 'rectangle_mask' || e.type === 'ellipse_mask' || e.type === 'freeform_mask' ||
          e.type === 'polygon_mask' || e.type === 'linear_mask' || e.type === 'split_mask')
  );
  if (!mask || !mask.params) return;

  svg.classList.add('interactive');
  const p = getEffectiveMaskParams(mask, clipTime);

  if (mask.type === 'rectangle_mask') {
    const x = (p.x ?? 0.2) * 100;
    const y = (p.y ?? 0.2) * 100;
    const w = (p.w ?? 0.6) * 100;
    const h = (p.h ?? 0.6) * 100;
    const round = (p.round ?? 0) * Math.min(w, h) * 0.5;

    const rect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
    rect.setAttribute('x', x);
    rect.setAttribute('y', y);
    rect.setAttribute('width', w);
    rect.setAttribute('height', h);
    rect.setAttribute('rx', round);
    rect.setAttribute('ry', round);
    rect.className.baseVal = 'stage-mask-path';
    svg.appendChild(rect);

    const handles = [
      { id: 'nw', cx: x, cy: y, cursor: 'nwse-resize' },
      { id: 'ne', cx: x + w, cy: y, cursor: 'nesw-resize' },
      { id: 'se', cx: x + w, cy: y + h, cursor: 'nwse-resize' },
      { id: 'sw', cx: x, cy: y + h, cursor: 'nesw-resize' },
      { id: 'center', cx: x + w / 2, cy: y + h / 2, cursor: 'move' },
    ];
    for (const hInfo of handles) {
      const circle = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
      circle.setAttribute('cx', hInfo.cx);
      circle.setAttribute('cy', hInfo.cy);
      circle.setAttribute('r', hInfo.id === 'center' ? '2.5' : '2');
      circle.setAttribute('style', `cursor: ${hInfo.cursor}`);
      circle.className.baseVal = 'stage-mask-handle';
      circle.dataset.handle = hInfo.id;
      circle.dataset.mask = 'rectangle';
      svg.appendChild(circle);
    }
  } else if (mask.type === 'ellipse_mask') {
    const cx = (p.cx ?? 0.5) * 100;
    const cy = (p.cy ?? 0.5) * 100;
    const rx = (p.rx ?? 0.3) * 100;
    const ry = (p.ry ?? 0.3) * 100;
    const rot = p.rotation ?? 0;

    const g = document.createElementNS('http://www.w3.org/2000/svg', 'g');
    g.setAttribute('transform', `rotate(${rot} ${cx} ${cy})`);

    const ell = document.createElementNS('http://www.w3.org/2000/svg', 'ellipse');
    ell.setAttribute('cx', cx);
    ell.setAttribute('cy', cy);
    ell.setAttribute('rx', rx);
    ell.setAttribute('ry', ry);
    ell.className.baseVal = 'stage-mask-path';
    g.appendChild(ell);

    const handles = [
      { id: 'e', cx: cx + rx, cy: cy, cursor: 'ew-resize' },
      { id: 'w', cx: cx - rx, cy: cy, cursor: 'ew-resize' },
      { id: 's', cx: cx, cy: cy + ry, cursor: 'ns-resize' },
      { id: 'n', cx: cx, cy: cy - ry, cursor: 'ns-resize' },
      { id: 'center', cx: cx, cy: cy, cursor: 'move' },
    ];
    for (const hInfo of handles) {
      const circle = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
      circle.setAttribute('cx', hInfo.cx);
      circle.setAttribute('cy', hInfo.cy);
      circle.setAttribute('r', hInfo.id === 'center' ? '2.5' : '2');
      circle.setAttribute('style', `cursor: ${hInfo.cursor}`);
      circle.className.baseVal = 'stage-mask-handle';
      circle.dataset.handle = hInfo.id;
      circle.dataset.mask = 'ellipse';
      g.appendChild(circle);
    }
    svg.appendChild(g);
  } else if (mask.type === 'freeform_mask' || mask.type === 'polygon_mask') {
    const pts = p.points || [];
    if (pts.length >= 2) {
      const poly = document.createElementNS('http://www.w3.org/2000/svg', 'polygon');
      const ptsAttr = pts.map((pt) => `${((pt.x !== undefined ? pt.x : pt[0]) * 100).toFixed(2)},${((pt.y !== undefined ? pt.y : pt[1]) * 100).toFixed(2)}`).join(' ');
      poly.setAttribute('points', ptsAttr);
      poly.className.baseVal = 'stage-mask-path';
      svg.appendChild(poly);
    }

    pts.forEach((pt, idx) => {
      const circle = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
      circle.setAttribute('cx', (pt.x !== undefined ? pt.x : pt[0]) * 100);
      circle.setAttribute('cy', (pt.y !== undefined ? pt.y : pt[1]) * 100);
      circle.setAttribute('r', '2.2');
      circle.setAttribute('style', 'cursor: move');
      circle.className.baseVal = 'stage-mask-handle';
      circle.dataset.handle = 'vertex';
      circle.dataset.vertexIndex = String(idx);
      circle.dataset.mask = 'freeform';
      svg.appendChild(circle);
    });
  } else if (mask.type === 'linear_mask' || mask.type === 'split_mask') {
    const cx = (p.x ?? 0.5) * 100;
    const cy = (p.y ?? 0.5) * 100;
    const deg = p.angle ?? 0;
    const rad = deg * Math.PI / 180;
    const dx = Math.cos(rad);
    const dy = Math.sin(rad);

    const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
    line.setAttribute('x1', cx - 200 * dx);
    line.setAttribute('y1', cy - 200 * dy);
    line.setAttribute('x2', cx + 200 * dx);
    line.setAttribute('y2', cy + 200 * dy);
    line.className.baseVal = 'stage-mask-line';
    svg.appendChild(line);

    const centerCircle = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
    centerCircle.setAttribute('cx', cx);
    centerCircle.setAttribute('cy', cy);
    centerCircle.setAttribute('r', '2.5');
    centerCircle.setAttribute('style', 'cursor: move');
    centerCircle.className.baseVal = 'stage-mask-handle';
    centerCircle.dataset.handle = 'center';
    centerCircle.dataset.mask = 'linear';
    svg.appendChild(centerCircle);

    const rotCircle = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
    rotCircle.setAttribute('cx', cx + 25 * dx);
    rotCircle.setAttribute('cy', cy + 25 * dy);
    rotCircle.setAttribute('r', '2');
    rotCircle.setAttribute('style', 'cursor: grab');
    rotCircle.className.baseVal = 'stage-mask-handle';
    rotCircle.dataset.handle = 'rotate';
    rotCircle.dataset.mask = 'linear';
    svg.appendChild(rotCircle);
  }
}

function bindMaskOverlayEvents(svg) {
  svg.addEventListener('mousedown', (event) => {
    const found = state.selected ? clipById(state.selected) : null;
    if (!found) return;
    const clip = found.clip;
    const mask = (clip.effects || []).find((e) =>
      e && (e.type === 'rectangle_mask' || e.type === 'ellipse_mask' || e.type === 'freeform_mask' ||
            e.type === 'polygon_mask' || e.type === 'linear_mask' || e.type === 'split_mask')
    );
    if (!mask || !mask.params) return;

    const handleNode = event.target.closest('.stage-mask-handle');
    const rect = svg.getBoundingClientRect();
    if (!rect.width || !rect.height) return;

    if (state.drawMaskMode && (mask.type === 'freeform_mask' || mask.type === 'polygon_mask') && !handleNode) {
      event.preventDefault();
      event.stopPropagation();
      const nx = Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width));
      const ny = Math.max(0, Math.min(1, (event.clientY - rect.top) / rect.height));
      mask.params.points = mask.params.points || [];
      mask.params.points.push({ x: Math.round(nx * 1000) / 1000, y: Math.round(ny * 1000) / 1000 });
      api(`/api/clip/${clip.id}`, { method: 'POST', body: { effects: clip.effects } })
        .then((result) => { applyState(result.state); })
        .catch((err) => toast(err.message, 'bad'));
      return;
    }

    if (!handleNode) return;
    event.preventDefault();
    event.stopPropagation();

    const handleId = handleNode.dataset.handle;
    const vertexIndex = handleNode.dataset.vertexIndex !== undefined ? parseInt(handleNode.dataset.vertexIndex, 10) : null;
    const startX = event.clientX;
    const startY = event.clientY;

    const clipTime = Math.max(0, (state.playhead - clip.start) * (clip.speed || 1));
    const kfs = mask.keyframes || [];
    let activeKf = kfs.find((kf) => Math.abs(kf.time - clipTime) < 0.08);

    const initialParams = JSON.parse(JSON.stringify(activeKf ? activeKf.params : mask.params));
    const targetParams = activeKf ? activeKf.params : mask.params;

    const onMove = (moveEvent) => {
      const dx = (moveEvent.clientX - startX) / rect.width;
      const dy = (moveEvent.clientY - startY) / rect.height;

      if (mask.type === 'rectangle_mask') {
        if (handleId === 'center') {
          targetParams.x = Math.max(0, Math.min(1 - (initialParams.w || 0.6), (initialParams.x || 0.2) + dx));
          targetParams.y = Math.max(0, Math.min(1 - (initialParams.h || 0.6), (initialParams.y || 0.2) + dy));
        } else if (handleId === 'se') {
          targetParams.w = Math.max(0.05, Math.min(1 - (initialParams.x || 0.2), (initialParams.w || 0.6) + dx));
          targetParams.h = Math.max(0.05, Math.min(1 - (initialParams.y || 0.2), (initialParams.h || 0.6) + dy));
        } else if (handleId === 'nw') {
          const newX = Math.max(0, Math.min((initialParams.x || 0.2) + (initialParams.w || 0.6) - 0.05, (initialParams.x || 0.2) + dx));
          const newY = Math.max(0, Math.min((initialParams.y || 0.2) + (initialParams.h || 0.6) - 0.05, (initialParams.y || 0.2) + dy));
          targetParams.w = (initialParams.x || 0.2) + (initialParams.w || 0.6) - newX;
          targetParams.h = (initialParams.y || 0.2) + (initialParams.h || 0.6) - newY;
          targetParams.x = newX;
          targetParams.y = newY;
        } else if (handleId === 'ne') {
          const newY = Math.max(0, Math.min((initialParams.y || 0.2) + (initialParams.h || 0.6) - 0.05, (initialParams.y || 0.2) + dy));
          targetParams.w = Math.max(0.05, Math.min(1 - (initialParams.x || 0.2), (initialParams.w || 0.6) + dx));
          targetParams.h = (initialParams.y || 0.2) + (initialParams.h || 0.6) - newY;
          targetParams.y = newY;
        } else if (handleId === 'sw') {
          const newX = Math.max(0, Math.min((initialParams.x || 0.2) + (initialParams.w || 0.6) - 0.05, (initialParams.x || 0.2) + dx));
          targetParams.w = (initialParams.x || 0.2) + (initialParams.w || 0.6) - newX;
          targetParams.h = Math.max(0.05, Math.min(1 - (initialParams.y || 0.2), (initialParams.h || 0.6) + dy));
          targetParams.x = newX;
        }
      } else if (mask.type === 'ellipse_mask') {
        if (handleId === 'center') {
          targetParams.cx = Math.max(0, Math.min(1, (initialParams.cx || 0.5) + dx));
          targetParams.cy = Math.max(0, Math.min(1, (initialParams.cy || 0.5) + dy));
        } else if (handleId === 'e' || handleId === 'w') {
          targetParams.rx = Math.max(0.02, Math.min(0.5, (initialParams.rx || 0.3) + (handleId === 'e' ? dx : -dx)));
        } else if (handleId === 's' || handleId === 'n') {
          targetParams.ry = Math.max(0.02, Math.min(0.5, (initialParams.ry || 0.3) + (handleId === 's' ? dy : -dy)));
        }
      } else if (mask.type === 'freeform_mask' || mask.type === 'polygon_mask') {
        if (vertexIndex !== null && initialParams.points && initialParams.points[vertexIndex]) {
          const orig = initialParams.points[vertexIndex];
          const ox = orig.x !== undefined ? orig.x : orig[0];
          const oy = orig.y !== undefined ? orig.y : orig[1];
          targetParams.points[vertexIndex] = {
            x: Math.max(0, Math.min(1, Math.round((ox + dx) * 1000) / 1000)),
            y: Math.max(0, Math.min(1, Math.round((oy + dy) * 1000) / 1000)),
          };
        }
      } else if (mask.type === 'linear_mask' || mask.type === 'split_mask') {
        if (handleId === 'center') {
          targetParams.x = Math.max(0, Math.min(1, (initialParams.x ?? 0.5) + dx));
          targetParams.y = Math.max(0, Math.min(1, (initialParams.y ?? 0.5) + dy));
        } else if (handleId === 'rotate') {
          const currentMouseX = moveEvent.clientX - rect.left;
          const currentMouseY = moveEvent.clientY - rect.top;
          const origCenterX = (initialParams.x ?? 0.5) * rect.width;
          const origCenterY = (initialParams.y ?? 0.5) * rect.height;
          const angleRad = Math.atan2(currentMouseY - origCenterY, currentMouseX - origCenterX);
          targetParams.angle = Math.round(angleRad * 180 / Math.PI);
        }
      }

      const layer = stage.layers.get(clip.id);
      if (layer) applyMask(layer, clip, clipTime);
      updateMaskOverlay(clipTime);
    };

    const onUp = () => {
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
      api(`/api/clip/${clip.id}`, { method: 'POST', body: { effects: clip.effects } })
        .then((result) => { applyState(result.state); })
        .catch((err) => toast(err.message, 'bad'));
    };

    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
  });
}

function toCssColor(val) {
  if (!val) return 'transparent';
  val = String(val).trim();
  const atMatch = val.match(/^([a-zA-Z]+)@([0-9.]+)$/);
  if (atMatch) {
    const name = atMatch[1].toLowerCase();
    const alpha = parseFloat(atMatch[2]);
    if (name === 'black') return `rgba(0, 0, 0, ${alpha})`;
    if (name === 'white') return `rgba(255, 255, 255, ${alpha})`;
    return name;
  }
  if (val.startsWith('0x') || val.startsWith('0X')) {
    const hex = val.slice(2);
    if (hex.length === 8) {
      const r = parseInt(hex.slice(0, 2), 16);
      const g = parseInt(hex.slice(2, 4), 16);
      const b = parseInt(hex.slice(4, 6), 16);
      const a = (parseInt(hex.slice(6, 8), 16) / 255).toFixed(2);
      return `rgba(${r}, ${g}, ${b}, ${a})`;
    }
    return '#' + hex;
  }
  return val;
}

function syncStage(t, playing) {
  if (!state.data) return;
  const settings = state.data.timeline.settings;
  const stageNode = $('stage');
  const stageW = stageNode.clientWidth || settings.width;

  const visible = new Set();
  const videos = activeVideoClips(t);
  const texts = activeTextClips(t);

  for (const clip of videos) {
    const node = ensureLayer(clip);
    visible.add(clip.id);
    node.classList.remove('off');
    node.style.zIndex = String(10 + allClips().findIndex((e) => e.clip.id === clip.id));

    const want = Math.max(0, sourceTime(clip, t));
    // Only correct drift beyond a threshold; seeking every frame makes the
    // element stutter and never actually plays.
    if (Math.abs((node.currentTime || 0) - want) > 0.28) {
      try { node.currentTime = want; } catch { /* not seekable yet */ }
    }
    if (playing) {
      node.playbackRate = Math.min(4, Math.max(0.25, clip.speed || 1));
      if (node.paused) node.play().catch(() => {});
    } else if (!node.paused) {
      node.pause();
    }
    const clipTime = Math.max(0, (t - clip.start) * (clip.speed || 1));
    applyTransform(node, clip, settings.width, stageW, clipTime);
    applyMask(node, clip, clipTime);
  }

  for (const clip of texts) {
    const node = ensureTextLayer(clip);
    visible.add(clip.id);
    node.classList.remove('off');
    node.style.zIndex = '50';
    const style = clip.text || {};
    const k = stageW / settings.width;
    node.textContent = style.content || '';
    node.style.fontSize = `${(style.font_size || 64) * k}px`;
    node.style.color = toCssColor(style.color || 'white');
    node.style.fontFamily = style.font_file
      ? `"${style.font_file}", ui-sans-serif, system-ui, sans-serif`
      : 'ui-sans-serif, system-ui, sans-serif';
    node.style.background = style.box ? toCssColor(style.box_color || 'black@0.5') : 'transparent';
    node.style.padding = style.box ? `${(style.box_border || 12) * k}px` : '0';
    node.style.borderRadius = '2px';

    // Position: the element is absolutely placed at 0,0 with width:auto, so
    // measure it before computing the offset.
    node.style.left = '0px';
    node.style.top = '0px';
    const w = node.offsetWidth, h = node.offsetHeight;
    node.style.left = `${resolvePos(style.x, stageW, w, k)}px`;
    node.style.top = `${resolvePos(style.y, stageNode.clientHeight, h, k)}px`;
  }

  for (const [id, node] of stage.layers) {
    if (!visible.has(id)) node.classList.add('off');
  }

  // audio
  const audioClip = activeAudioClip(t);
  if (!stage.audio) {
    stage.audio = new Audio();
    stage.audio.preload = 'auto';
  }
  const ownerId = audioClip ? audioClip.id : null;
  if (ownerId !== stage.audioOwner) {
    stage.audioOwner = ownerId;
    if (audioClip) {
      stage.audio.src = proxyUrl(audioClip.source);
      stage.audio.volume = Math.min(1, Math.max(0, audioClip.volume ?? 1));
    } else {
      stage.audio.removeAttribute('src');
    }
  }
  if (audioClip) {
    const want = Math.max(0, sourceTime(audioClip, t));
    if (Math.abs((stage.audio.currentTime || 0) - want) > 0.35) {
      try { stage.audio.currentTime = want; } catch { /* not ready */ }
    }
    if (playing) { if (stage.audio.paused) stage.audio.play().catch(() => {}); }
    else if (!stage.audio.paused) stage.audio.pause();
  } else if (!stage.audio.paused) {
    stage.audio.pause();
  }

  $('stage-empty').classList.toggle('hidden', videos.length + texts.length > 0);
  const selClip = state.selected ? (clipById(state.selected) || {}).clip : null;
  const selTime = selClip ? Math.max(0, (t - selClip.start) * (selClip.speed || 1)) : 0;
  updateMaskOverlay(selTime);
}

// ------------------------------------------------------------------ clock

function tick() {
  if (!state.playing) return;
  const now = performance.now();
  const delta = (now - stage.clock) / 1000;
  stage.clock = now;
  const total = state.data ? state.data.duration : 0;
  let next = state.playhead + delta;
  if (next >= total) { next = total; setPlaying(false); }
  state.playhead = next;
  syncStage(next, true);
  paintPlayhead();
  updateTimecode();
  if (state.playing) stage.raf = requestAnimationFrame(tick);
}

function setPlaying(value) {
  if (state.playing === value) return;
  state.playing = value;
  $('btn-play').innerHTML = value ? '&#10074;&#10074;' : '&#9654;';
  if (value) {
    stage.clock = performance.now();
    stage.raf = requestAnimationFrame(tick);
  } else if (stage.raf) {
    cancelAnimationFrame(stage.raf);
    stage.raf = null;
    syncStage(state.playhead, false);
  }
}

function seek(t, { keepPlaying = null } = {}) {
  const total = state.data ? state.data.duration : 0;
  state.playhead = Math.max(0, Math.min(total, t));
  if (keepPlaying === false) setPlaying(false);
  syncStage(state.playhead, state.playing);
  paintPlayhead();
  updateTimecode();
}

function updateTimecode() {
  $('timecode').textContent = timecode(state.playhead);
  $('timecode-total').textContent = `/ ${timecode(state.data ? state.data.duration : 0)}`;
}

function paintPlayhead() {
  $('playhead').style.left = `${state.playhead * state.pxPerSecond}px`;
}

// ----------------------------------------------------------------- render

function render() {
  const data = state.data;
  if (!data) return;
  $('version').textContent = `v${data.version}`;
  $('project-name').value = data.name;
  $('dirty-dot').classList.toggle('on', !!data.dirty);
  $('stage-res').textContent = `${data.timeline.settings.width}×${data.timeline.settings.height}`;
  $('stage-fps').textContent = `${data.timeline.settings.fps}fps`;
  $('timeline-duration').textContent = timecode(data.duration);

  const problems = data.problems || [];
  const badge = $('stage-problems');
  badge.classList.toggle('hidden', problems.length === 0);
  badge.textContent = `${problems.length} problem${problems.length === 1 ? '' : 's'}`;
  badge.title = problems.join('\n');

  $('status-right').textContent = data.path
    ? data.path
    : 'not saved yet — Save As from the Export dialog';
  $('status-right').title = data.export_dir ? `exports → ${data.export_dir}` : '';

  renderBin();
  renderTimeline();
  renderInspector();
  fitStage();
  syncStage(state.playhead, state.playing);
  updateTimecode();
  paintPlayhead();
}

function renderBin() {
  const body = $('bin-body');
  const seen = new Map();

  for (const { clip } of allClips()) {
    if (!clip.source) continue;
    if (!seen.has(clip.source)) {
      seen.set(clip.source, {
        path: clip.source,
        name: clip.name || clip.source.split(/[\\/]/).pop(),
        clips: 0,
      });
    }
    seen.get(clip.source).clips += 1;
  }

  // Also surface media the project has probed but not yet placed.
  for (const [path, info] of Object.entries(state.data.media_cache || {})) {
    if (!seen.has(path)) {
      seen.set(path, { path, name: info.name || path, clips: 0,
                       info, unplaced: true });
    }
  }

  if (seen.size === 0) {
    body.innerHTML = '<p class="bin-empty">No media yet. Import files, then drag them onto a track.</p>';
    return;
  }

  const missing = new Set(state.data.missing_media || []);
  const rows = [];
  for (const item of seen.values()) {
    const thumb = proxyThumb(item.path);
    const gone = missing.has(item.path);
    rows.push(`
      <div class="bin-item" draggable="true" data-path="${esc(item.path)}"
           title="${esc(item.path)}">
        <div class="bin-thumb" style="background-image:url('${esc(thumb)}')"></div>
        <div>
          <div class="bin-name">${esc(item.name)}</div>
          <div class="bin-sub">${gone ? 'missing' : (item.clips ? `${item.clips} clip${item.clips === 1 ? '' : 's'}` : 'not placed')}</div>
        </div>
      </div>`);
  }
  body.innerHTML = rows.join('');

  body.querySelectorAll('.bin-item').forEach((node) => {
    node.addEventListener('dragstart', (event) => {
      event.dataTransfer.setData('text/hatcut-path', node.dataset.path);
      event.dataTransfer.effectAllowed = 'copy';
    });
    node.addEventListener('dblclick', () => {
      api('/api/import', { method: 'POST', body: { path: node.dataset.path } })
        .then((result) => applyState(result.state))
        .then(() => toast('added to timeline', 'good'))
        .catch((error) => toast(error.message, 'bad'));
    });
  });
}

function proxyThumb(path) {
  // The thumbnail endpoint reads a frame from the source; for a still it just
  // returns the image. Cheap on repeat because the server caches by mtime.
  return `/api/thumb?path=${encodeURIComponent(path)}&t=0.5`;
}

// --------------------------------------------------------------- timeline

function renderTimeline() {
  const data = state.data;
  const labels = $('track-labels');
  const tracks = $('tracks');
  const ruler = $('ruler');
  const pps = state.pxPerSecond;

  const contentWidth = Math.max(600, (data.duration + 6) * pps) + 200;

  labels.innerHTML = '<div class="ruler-spacer"></div>' + data.timeline.tracks.map((track) => {
    const kindClass = track.kind === 'video' ? 'video' : (track.kind === 'audio' ? 'audio' : 'text');
    const badgeText = (track.name || track.id).toUpperCase();
    return `
    <div class="track-label" data-track="${esc(track.id)}">
      <div class="track-info">
        <span class="track-pill ${kindClass}">${esc(badgeText)}</span>
      </div>
      <div class="tools">
        ${track.kind === 'audio' ? `
          <button class="track-duck-btn ${track.ducking ? 'active' : ''}" data-flag="ducking" data-track="${esc(track.id)}" title="Auto-Ducking: duck music volume when dialogue speaks">🦆 Duck</button>
          <button class="icon-btn ${track.muted ? 'active' : ''}" data-flag="muted" data-track="${esc(track.id)}" title="${track.muted ? 'Unmute' : 'Mute'}">${track.muted ? '🔇' : '🔊'}</button>
        ` : ''}
        ${track.kind === 'video' ? `
          <select class="track-blend-select" data-track-blend="${esc(track.id)}" title="Track blend mode">
            ${['normal', 'screen', 'multiply', 'overlay', 'addition', 'difference', 'darken', 'lighten'].map((m) =>
              `<option value="${m}"${(track.blend_mode || 'normal') === m ? ' selected' : ''}>${m}</option>`).join('')}
          </select>
        ` : ''}
        <button class="icon-btn ${track.locked ? 'active' : ''}" data-flag="locked" data-track="${esc(track.id)}" title="${track.locked ? 'Unlock track' : 'Lock track'}">${track.locked ? '🔒' : '🔓'}</button>
        <button class="icon-btn" data-remove-track="${esc(track.id)}" title="Delete track">&times;</button>
      </div>
    </div>`;
  }).join('');

  tracks.style.width = `${contentWidth}px`;
  ruler.style.width = `${contentWidth}px`;

  tracks.innerHTML = data.timeline.tracks.map((track) => `
    <div class="track" data-track="${esc(track.id)}" data-kind="${esc(track.kind)}">
      ${track.clips.map((clip) => clipMarkup(clip, track)).join('')}
    </div>`).join('');

  // ruler ticks & markers
  const step = chooseStep(pps);
  const ticks = [];
  for (let t = 0; t <= data.duration + 6; t += step) {
    ticks.push(`<div class="ruler-tick" style="left:${t * pps}px"><span>${shortTime(t)}</span></div>`);
  }
  const markersHtml = (data.timeline.markers || []).map((m) => `
    <div class="timeline-marker" style="left:${m.time * pps}px; background-color:${m.color || '#3b82f6'}"
         data-marker-id="${esc(m.id)}" data-time="${m.time}" title="${esc(m.name || 'Marker')} (${timecode(m.time)})">
      <div class="marker-head"></div>
      <div class="marker-line"></div>
    </div>`).join('');
  ruler.innerHTML = ticks.join('') + markersHtml;

  bindTimelineEvents();
  drawWaveforms();
}

function chooseStep(pps) {
  const candidates = [0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300];
  for (const step of candidates) {
    if (step * pps >= 70) return step;
  }
  return 600;
}

function drawWaveforms() {
  const canvases = document.querySelectorAll('.clip-waveform');
  canvases.forEach(async (canvas) => {
    const clipId = canvas.dataset.clipId;
    const source = canvas.dataset.source;
    if (!source) return;

    const found = clipById(clipId);
    if (!found) return;
    const clip = found.clip;

    let peaks = waveformCache.get(source);
    if (!peaks) {
      if (waveformLoading.has(source)) return;
      waveformLoading.add(source);
      try {
        const resp = await api(`/api/peaks?path=${encodeURIComponent(source)}&samples_per_sec=50`);
        peaks = resp.peaks || [];
        waveformCache.set(source, peaks);
        drawWaveforms();
      } catch {
        waveformCache.set(source, []);
      } finally {
        waveformLoading.delete(source);
      }
      return;
    }

    if (!canvas.isConnected) return;
    const rect = canvas.getBoundingClientRect();
    const w = Math.round(rect.width) || canvas.clientWidth;
    const h = Math.round(rect.height) || canvas.clientHeight;
    if (w <= 0 || h <= 0) return;

    if (canvas.width !== w || canvas.height !== h) {
      canvas.width = w;
      canvas.height = h;
    }
    const ctx = canvas.getContext('2d');
    ctx.clearRect(0, 0, w, h);

    if (!peaks || peaks.length === 0) return;

    const mid = h / 2;
    const inPoint = clip.in_point || 0;
    const sourceSpan = (clip.duration || 0) * (clip.speed || 1);
    const peaksPerSec = 50;
    const totalPeaks = peaks.length;

    const startPeak = inPoint * peaksPerSec;
    const peakSpan = sourceSpan * peaksPerSec;
    const vol = Math.min(1.5, Math.max(0, clip.volume ?? 1));

    ctx.fillStyle = 'rgba(74, 222, 128, 0.55)';

    for (let x = 0; x < w; x++) {
      const prog = x / w;
      const peakIdx = Math.floor(startPeak + prog * peakSpan);
      let val = 0;
      if (peakIdx >= 0 && peakIdx < totalPeaks) {
        val = peaks[peakIdx];
      }
      val = Math.min(1, val * vol);
      if (val > 0.01) {
        const barH = Math.max(1, Math.round(val * mid * 0.9));
        ctx.fillRect(x, mid - barH, 1, barH * 2);
      } else {
        ctx.fillRect(x, mid - 0.5, 1, 1);
      }
    }
  });
}

function getSnapPoints(excludeClipIds = new Set()) {
  if (!state.data || !state.data.timeline) return [];
  const points = new Set();
  points.add(state.playhead);
  points.add(0);
  if (state.data.duration > 0) points.add(state.data.duration);
  const maxSec = Math.ceil(state.data.duration + 5);
  for (let s = 0; s <= maxSec; s += 1) points.add(s);
  for (const track of state.data.timeline.tracks) {
    for (const clip of track.clips) {
      if (!excludeClipIds.has(clip.id)) {
        points.add(clip.start);
        points.add(clip.start + clip.duration);
      }
    }
  }
  for (const m of (state.data.timeline.markers || [])) {
    points.add(m.time);
  }
  return Array.from(points);
}

function findSnap(proposedTime, duration = 0, excludeClipIds = new Set()) {
  if (!state.snapping) return { time: proposedTime, snapped: false, snapTarget: null };
  const pps = state.pxPerSecond;
  const thresholdSec = 8 / pps;
  const snapPoints = getSnapPoints(excludeClipIds);

  let bestDist = thresholdSec;
  let snappedTime = proposedTime;
  let snapTarget = null;

  for (const pt of snapPoints) {
    const distStart = Math.abs(proposedTime - pt);
    if (distStart < bestDist) {
      bestDist = distStart;
      snappedTime = pt;
      snapTarget = pt;
    }
    if (duration > 0) {
      const distEnd = Math.abs((proposedTime + duration) - pt);
      if (distEnd < bestDist) {
        bestDist = distEnd;
        snappedTime = pt - duration;
        snapTarget = pt;
      }
    }
  }

  return {
    time: Math.max(0, snappedTime),
    snapped: snapTarget !== null,
    snapTarget: snapTarget,
  };
}

let currentMarkerTarget = null;

function selectMarkerColorChip(color) {
  const hex = (color || '#ffb547').toLowerCase();
  document.querySelectorAll('#marker-color-chips .color-chip').forEach((chip) => {
    chip.classList.toggle('selected', chip.dataset.color.toLowerCase() === hex);
  });
}

function openAddMarker(t = state.playhead) {
  const targetTime = Math.max(0, t);
  currentMarkerTarget = { mode: 'add', time: targetTime };
  $('marker-modal-title').textContent = `Add Marker at ${timecode(targetTime)}`;
  $('marker-name-input').value = '';
  $('btn-delete-marker').classList.add('hidden');
  selectMarkerColorChip('#ffb547');
  $('modal-marker').classList.remove('hidden');
  setTimeout(() => $('marker-name-input').focus(), 50);
}

function openEditMarker(marker) {
  currentMarkerTarget = { mode: 'edit', marker: marker };
  $('marker-modal-title').textContent = `Edit Marker at ${timecode(marker.time)}`;
  $('marker-name-input').value = marker.name || '';
  $('btn-delete-marker').classList.remove('hidden');
  selectMarkerColorChip(marker.color || '#ffb547');
  $('modal-marker').classList.remove('hidden');
  setTimeout(() => $('marker-name-input').focus(), 50);
}

function addMarkerAtPlayhead() {
  openAddMarker(state.playhead);
}

function clipMarkup(clip, track) {
  const pps = state.pxPerSecond;
  const left = clip.start * pps;
  const width = Math.max(3, clip.duration * pps);
  const kind = clip.text ? 'text' : (track.kind === 'audio' ? 'audio'
    : (isImageClip(clip) ? 'image' : 'video'));
  const isSelected = (state.selectedClips && state.selectedClips.has(clip.id)) || clip.id === state.selected;
  const selected = isSelected ? ' selected' : '';
  const disabled = clip.enabled === false ? ' disabled' : '';
  const fade = clip.transition && clip.transition.duration
    ? `<div class="clip-fade" style="width:${Math.min(width, clip.transition.duration * pps)}px"></div>`
    : '';
  const thumb = (kind === 'video' || kind === 'image')
    ? `<div class="clip-thumb" style="background-image:url('${esc(proxyThumb(clip.source))}');background-position:${-clip.in_point * pps}px center"></div>`
    : '';
  const waveform = (kind === 'audio' && clip.source)
    ? `<canvas class="clip-waveform" data-clip-id="${esc(clip.id)}" data-source="${esc(clip.source)}"></canvas>`
    : '';
  const label = clip.text ? (clip.text.content || 'Text')
    : (clip.name || (clip.source || '').split(/[\\/]/).pop() || 'clip');
  const sub = `${clip.duration.toFixed(2)}s${clip.speed && clip.speed !== 1 ? ` · ${clip.speed}×` : ''}${clip.effects && clip.effects.length ? ` · ${clip.effects.length}fx` : ''}`;

  return `
    <div class="clip${selected}${disabled} ${kind}" data-clip="${esc(clip.id)}"
         style="left:${left}px;width:${width}px" title="${esc(label)}">
      ${thumb}${fade}${waveform}
      <div class="handle left" data-handle="left"></div>
      <div class="clip-body">
        <div class="clip-name">${esc(label)}</div>
        <div class="clip-sub">${esc(sub)}</div>
      </div>
      <div class="handle right" data-handle="right"></div>
    </div>`;
}

function isImageClip(clip) {
  const cached = (state.data.media_cache || {})[clip.source];
  return !!(cached && cached.is_image);
}

function bindTimelineEvents() {
  const tracks = $('tracks');
  const ruler = $('ruler');

  // Clip selection & drag
  tracks.querySelectorAll('.clip').forEach((node) => {
    node.addEventListener('mousedown', (event) => {
      const clipId = node.dataset.clip;
      const isShift = event.shiftKey;
      if (isShift) {
        select(clipId, true);
      } else {
        if (!state.selectedClips.has(clipId)) {
          select(clipId, false);
        } else {
          state.selected = clipId;
          renderInspector();
        }
      }
      const handle = event.target.closest('.handle');
      startDrag(event, clipId, handle ? handle.dataset.handle : 'move');
    });
  });

  // Markers interactions
  ruler.querySelectorAll('.timeline-marker').forEach((node) => {
    node.addEventListener('click', (e) => {
      e.stopPropagation();
      const t = parseFloat(node.dataset.time);
      seek(t, { keepPlaying: false });
    });
    node.addEventListener('contextmenu', (e) => {
      e.preventDefault();
      e.stopPropagation();
      const markerId = node.dataset.markerId;
      const marker = (state.data.timeline.markers || []).find((m) => m.id === markerId);
      if (!marker) return;
      openEditMarker(marker);
    });
  });

  // Track drag & drop
  tracks.querySelectorAll('.track').forEach((row) => {
    row.addEventListener('dragover', (event) => {
      event.preventDefault();
      row.classList.add('drop-target');
      event.dataTransfer.dropEffect = 'copy';
    });
    row.addEventListener('dragleave', () => row.classList.remove('drop-target'));
    row.addEventListener('drop', (event) => {
      event.preventDefault();
      row.classList.remove('drop-target');
      const path = event.dataTransfer.getData('text/hatcut-path');
      if (!path) return;
      const rect = row.getBoundingClientRect();
      const start = Math.max(0, (event.clientX - rect.left) / state.pxPerSecond);
      const track = state.data.timeline.tracks.find((t) => t.id === row.dataset.track);
      api('/api/import', {
        method: 'POST',
        body: {
          path, start,
          with_audio: track ? track.kind !== 'text' : true,
          track_id: track && track.kind === 'video' ? track.id : null,
        },
      }).then((result) => {
        applyState(result.state);
        const failed = (result.results || []).find((r) => !r.ok);
        if (failed) toast(failed.error, 'bad');
      }).catch((error) => toast(error.message, 'bad'));
    });
  });

  // Track flags (mute, lock, ducking)
  $('track-labels').querySelectorAll('[data-flag]').forEach((node) => {
    node.addEventListener('click', () => {
      const track = state.data.timeline.tracks.find((t) => t.id === node.dataset.track);
      const flag = node.dataset.flag;
      api(`/api/track/${node.dataset.track}/flags`, {
        method: 'POST', body: { flag, value: !track[flag] },
      }).then((snapshot) => applyState(snapshot)).catch((e) => toast(e.message, 'bad'));
    });
  });

  // Track blend mode
  $('track-labels').querySelectorAll('[data-track-blend]').forEach((selectNode) => {
    selectNode.addEventListener('change', () => {
      const trackId = selectNode.dataset.trackBlend;
      api(`/api/track/${trackId}/flags`, {
        method: 'POST', body: { flag: 'blend_mode', value: selectNode.value },
      }).then((snapshot) => applyState(snapshot)).catch((e) => toast(e.message, 'bad'));
    });
  });

  // Track delete
  $('track-labels').querySelectorAll('[data-remove-track]').forEach((node) => {
    node.addEventListener('click', () => {
      api(`/api/track?id=${encodeURIComponent(node.dataset.removeTrack)}`, { method: 'DELETE' })
        .then((snapshot) => applyState(snapshot))
        .catch((e) => toast(e.message, 'bad'));
    });
  });

  bindRubberbandSelection();
}

let rubberbandBound = false;
function bindRubberbandSelection() {
  if (rubberbandBound) return;
  rubberbandBound = true;
  const scrollWrap = $('track-scroll');
  const tracks = $('tracks');
  const selBox = $('selection-box');
  if (!scrollWrap || !selBox) return;

  scrollWrap.addEventListener('mousedown', (event) => {
    if (event.target.closest('.clip') || event.target.closest('.timeline-marker') || event.target.closest('.ruler')) {
      return;
    }
    event.preventDefault();

    const rect = scrollWrap.getBoundingClientRect();
    const startX = event.clientX - rect.left + scrollWrap.scrollLeft;
    const startY = event.clientY - rect.top + scrollWrap.scrollTop;
    let dragging = false;

    selBox.style.left = `${startX}px`;
    selBox.style.top = `${startY}px`;
    selBox.style.width = '0px';
    selBox.style.height = '0px';
    selBox.classList.remove('hidden');

    const onMove = (e) => {
      dragging = true;
      const curX = e.clientX - rect.left + scrollWrap.scrollLeft;
      const curY = e.clientY - rect.top + scrollWrap.scrollTop;

      const left = Math.min(startX, curX);
      const top = Math.min(startY, curY);
      const width = Math.abs(curX - startX);
      const height = Math.abs(curY - startY);

      selBox.style.left = `${left}px`;
      selBox.style.top = `${top}px`;
      selBox.style.width = `${width}px`;
      selBox.style.height = `${height}px`;

      const boxR = left + width;
      const boxB = top + height;
      const newSel = new Set(event.shiftKey ? state.selectedClips : []);

      tracks.querySelectorAll('.clip').forEach((cNode) => {
        const cl = cNode.offsetLeft;
        const ct = cNode.offsetTop;
        const cr = cl + cNode.offsetWidth;
        const cb = ct + cNode.offsetHeight;

        const hit = !(boxR < cl || left > cr || boxB < ct || top > cb);
        if (hit) {
          newSel.add(cNode.dataset.clip);
          cNode.classList.add('selected');
        } else if (!event.shiftKey) {
          cNode.classList.remove('selected');
        }
      });
      state.selectedClips = newSel;
      state.selected = Array.from(newSel)[0] || null;
    };

    const onUp = () => {
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
      selBox.classList.add('hidden');
      if (!dragging && !event.shiftKey) {
        state.selectedClips.clear();
        state.selected = null;
        renderTimeline();
        renderInspector();
      } else if (dragging) {
        renderInspector();
      }
    };

    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
  });
}

function startDrag(event, clipId, mode) {
  event.preventDefault();
  const found = clipById(clipId);
  if (!found) return;
  const { clip } = found;
  const startX = event.clientX;
  const pps = state.pxPerSecond;
  const snapGuide = $('snap-guide');

  // Multi-clip batch move handling:
  const movingClips = [];
  if (mode === 'move' && state.selectedClips.has(clipId) && state.selectedClips.size > 1) {
    for (const cid of state.selectedClips) {
      const f = clipById(cid);
      if (f) {
        const cNode = $('tracks').querySelector(`[data-clip="${cid}"]`);
        movingClips.push({ clip: f.clip, node: cNode, originalStart: f.clip.start });
      }
    }
  } else {
    const node = $('tracks').querySelector(`[data-clip="${clipId}"]`);
    movingClips.push({ clip, node, originalStart: clip.start });
  }

  const movingIds = new Set(movingClips.map((m) => m.clip.id));
  const original = { start: clip.start, duration: clip.duration, in_point: clip.in_point };
  let moved = false;
  let finalDelta = 0;

  const onMove = (moveEvent) => {
    const rawDelta = (moveEvent.clientX - startX) / pps;
    if (Math.abs(moveEvent.clientX - startX) > 2) moved = true;

    if (mode === 'move') {
      const targetStart = original.start + rawDelta;
      const snapResult = findSnap(targetStart, clip.duration, movingIds);
      const effectiveStart = snapResult.snapped ? snapResult.time : Math.max(0, targetStart);
      finalDelta = effectiveStart - original.start;

      if (snapResult.snapped && snapGuide) {
        snapGuide.style.left = `${snapResult.snapTarget * pps}px`;
        snapGuide.classList.remove('hidden');
      } else if (snapGuide) {
        snapGuide.classList.add('hidden');
      }

      for (const item of movingClips) {
        const nextStart = Math.max(0, item.originalStart + finalDelta);
        if (item.node) item.node.style.left = `${nextStart * pps}px`;
      }
      state.statusPreview = original.start + finalDelta;
    } else if (mode === 'right') {
      const targetDur = original.duration + rawDelta;
      const snapResult = findSnap(original.start + targetDur, 0, movingIds);
      const effectiveDur = snapResult.snapped ? Math.max(0.02, snapResult.snapTarget - original.start) : Math.max(0.02, targetDur);

      if (snapResult.snapped && snapGuide) {
        snapGuide.style.left = `${snapResult.snapTarget * pps}px`;
        snapGuide.classList.remove('hidden');
      } else if (snapGuide) {
        snapGuide.classList.add('hidden');
      }

      const node = movingClips[0].node;
      if (node) node.style.width = `${Math.max(3, effectiveDur * pps)}px`;
      state.statusPreview = effectiveDur;
    } else {
      // mode === 'left' (trim in-point)
      const shift = Math.max(-original.in_point / (clip.speed || 1),
                             Math.min(original.duration - 0.02, rawDelta));
      const targetStart = original.start + shift;
      const snapResult = findSnap(targetStart, 0, movingIds);
      const effectiveShift = snapResult.snapped ? (snapResult.snapTarget - original.start) : shift;

      if (snapResult.snapped && snapGuide) {
        snapGuide.style.left = `${snapResult.snapTarget * pps}px`;
        snapGuide.classList.remove('hidden');
      } else if (snapGuide) {
        snapGuide.classList.add('hidden');
      }

      const node = movingClips[0].node;
      if (node) {
        node.style.left = `${(original.start + effectiveShift) * pps}px`;
        node.style.width = `${Math.max(3, (original.duration - effectiveShift) * pps)}px`;
      }
      state.statusPreview = effectiveShift;
    }
  };

  const onUp = () => {
    document.removeEventListener('mousemove', onMove);
    document.removeEventListener('mouseup', onUp);
    if (snapGuide) snapGuide.classList.add('hidden');
    if (!moved) return;

    if (mode === 'move') {
      if (movingClips.length > 1) {
        const moves = movingClips.map((m) => ({
          id: m.clip.id,
          start: Math.max(0, m.originalStart + finalDelta),
        }));
        api('/api/clips/move', { method: 'POST', body: { moves } })
          .then((res) => applyState(res.state))
          .catch((err) => {
            toast(err.message, 'bad');
            return api('/api/state').then(applyState);
          });
      } else {
        const nextStart = Math.max(0, original.start + finalDelta);
        api(`/api/clip/${clipId}`, { method: 'POST', body: { start: nextStart } })
          .then((res) => applyState(res.state))
          .catch((err) => {
            toast(err.message, 'bad');
            return api('/api/state').then(applyState);
          });
      }
    } else if (mode === 'right') {
      const deltaSec = (state.statusPreview ?? original.duration) - original.duration;
      const nextDur = Math.max(0.02, original.duration + deltaSec);
      api(`/api/clip/${clipId}`, { method: 'POST', body: { duration: nextDur } })
        .then((res) => applyState(res.state))
        .catch((err) => {
          toast(err.message, 'bad');
          return api('/api/state').then(applyState);
        });
    } else {
      const shift = state.statusPreview ?? 0;
      const body = {
        start: Math.max(0, original.start + shift),
        in_point: Math.max(0, original.in_point + shift * (clip.speed || 1)),
        duration: Math.max(0.02, original.duration - shift),
      };
      api(`/api/clip/${clipId}`, { method: 'POST', body })
        .then((res) => applyState(res.state))
        .catch((err) => {
          toast(err.message, 'bad');
          return api('/api/state').then(applyState);
        });
    }
  };

  document.addEventListener('mousemove', onMove);
  document.addEventListener('mouseup', onUp);
}

function select(clipId, multi = false) {
  if (multi) {
    if (state.selectedClips.has(clipId)) {
      state.selectedClips.delete(clipId);
      state.selected = Array.from(state.selectedClips)[0] || null;
    } else {
      state.selectedClips.add(clipId);
      state.selected = clipId;
    }
  } else {
    state.selected = clipId;
    state.selectedClips = clipId ? new Set([clipId]) : new Set();
  }
  renderTimeline();
  renderInspector();
}

function applyState(snapshot, { skipHistory = false } = {}) {
  if (!skipHistory && state.data && state.data.timeline && snapshot && snapshot.timeline) {
    const currentStr = JSON.stringify(state.data.timeline);
    const nextStr = JSON.stringify(snapshot.timeline);
    if (currentStr !== nextStr) {
      pushUndo(state.data);
    }
  }
  if (state.data && snapshot) {
    if (!snapshot.presets && state.data.presets) snapshot.presets = state.data.presets;
    if (!snapshot.transitions && state.data.transitions) snapshot.transitions = state.data.transitions;
    if (!snapshot.effects && state.data.effects) snapshot.effects = state.data.effects;
    if (!snapshot.encoders && state.data.encoders) snapshot.encoders = state.data.encoders;
    if (!snapshot.ffmpeg_version && state.data.ffmpeg_version) snapshot.ffmpeg_version = state.data.ffmpeg_version;
  }
  state.data = snapshot;
  if (state.selected && !clipById(state.selected)) state.selected = null;
  render();
  updateUndoRedoUI();
}

// -------------------------------------------------------------- inspector

function renderInspector() {
  const body = $('inspector-body');
  const title = $('inspector-title');
  const found = state.selected ? clipById(state.selected) : null;

  if (!found) {
    title.textContent = 'Inspector';
    body.innerHTML = '<p class="dim pad">Select a clip to edit its properties.</p>';
    return;
  }

  const { clip, track } = found;
  title.textContent = clip.text ? 'Text clip' : 'Clip';

  const rows = [
    ['Source', clip.source ? clip.source.split(/[\\/]/).pop() : '—'],
    ['Track', `${track.name} (${track.kind})`],
    ['Start', `${clip.start.toFixed(3)}s`],
    ['Length', `${clip.duration.toFixed(3)}s`],
    ['In point', `${clip.in_point.toFixed(3)}s`],
    ['Source span', `${(clip.duration * (clip.speed || 1)).toFixed(3)}s`],
  ];

  let html = `
    <div class="insp-section">
      <h3>Timing</h3>
      <div class="grid-2">
        <label class="field"><span>Start (s)</span>
          <input type="number" step="0.01" id="clip-start" value="${clip.start}"></label>
        <label class="field"><span>Duration (s)</span>
          <input type="number" step="0.01" min="0.02" id="clip-duration" value="${clip.duration}"></label>
      </div>
      <div class="grid-2">
        <label class="field"><span>In point (s)</span>
          <input type="number" step="0.01" min="0" id="clip-in" value="${clip.in_point}"></label>
        <label class="field"><span>Speed</span>
          <input type="number" step="0.05" min="0.05" id="clip-speed" value="${clip.speed || 1}"></label>
      </div>
      ${track.kind === 'audio' ? `
      <label class="field"><span>Volume ${Math.round((clip.volume ?? 1) * 100)}%</span>
        <input type="range" min="0" max="2" step="0.01" id="clip-volume" value="${clip.volume ?? 1}"></label>` : ''}
    </div>`;

  if (track.kind !== 'audio') {
    const t = clip.transform || {};
    html += `
      <div class="insp-section">
        <h3>Transform</h3>
        <div class="grid-2">
          <label class="field"><span>Scale</span>
            <input type="number" step="0.01" min="0.01" id="tf-scale" value="${t.scale ?? 1}"></label>
          <label class="field"><span>Rotation</span>
            <input type="number" step="1" id="tf-rotate" value="${t.rotate ?? 0}"></label>
        </div>
        <div class="grid-2">
          <label class="field"><span>X offset</span>
            <input type="number" step="1" id="tf-x" value="${t.x ?? 0}"></label>
          <label class="field"><span>Y offset</span>
            <input type="number" step="1" id="tf-y" value="${t.y ?? 0}"></label>
        </div>
        <div class="grid-2">
          <label class="field"><span>Opacity ${Math.round((t.opacity ?? 1) * 100)}%</span>
            <input type="range" min="0" max="1" step="0.01" id="tf-opacity" value="${t.opacity ?? 1}"></label>
          <label class="field"><span>Fit</span>
            <select id="tf-fit">
              ${['contain', 'cover', 'stretch'].map((f) =>
                `<option value="${f}"${(t.fit || 'contain') === f ? ' selected' : ''}>${f}</option>`).join('')}
            </select></label>
        </div>
        <label class="field"><span>Blend Mode</span>
          <select id="clip-blend">
            ${['normal', 'screen', 'multiply', 'overlay', 'addition', 'difference', 'darken', 'lighten', 'hardlight', 'softlight'].map((m) =>
              `<option value="${m}"${(clip.blend_mode || 'normal') === m ? ' selected' : ''}>${m}</option>`).join('')}
          </select>
        </label>
        ${(() => {
          const kfs = (clip.transform_keyframes || []).slice().sort((a, b) => (a.time || 0) - (b.time || 0));
          const clipPlayheadOffset = Math.max(0, Math.min(clip.duration, (state.playhead - clip.start) * (clip.speed || 1)));
          const activeKf = kfs.find((k) => Math.abs((k.time || 0) - clipPlayheadOffset) < 0.08);
          return `
            <div class="keyframe-bar" style="margin-top:8px;padding-top:8px;border-top:1px solid var(--line);">
              <div style="display:flex;align-items:center;gap:6px;">
                <span class="kf-diamond ${activeKf ? 'active' : ''}"></span>
                <span style="font-size:12px;font-weight:500;">Motion Keyframes (${kfs.length})</span>
              </div>
              <div class="kf-actions">
                <button class="btn tiny" id="btn-tf-add-kf" title="Add or update keyframe at playhead">+ Add at Playhead</button>
                ${activeKf ? `<button class="btn tiny danger" id="btn-tf-del-kf" title="Remove keyframe at playhead">− Remove</button>` : ''}
              </div>
            </div>
            ${kfs.length > 0 ? `
              <div class="keyframes-list">
                ${kfs.map((kf, i) => `
                  <span class="kf-chip ${Math.abs(kf.time - clipPlayheadOffset) < 0.08 ? 'active' : ''}" data-tf-kf="${kf.time.toFixed(2)}" title="Jump playhead to ${kf.time.toFixed(2)}s">
                    ${kf.time.toFixed(2)}s
                    <span data-del-tf-kf="${i}" style="color:var(--bad);font-weight:bold;margin-left:3px;">&times;</span>
                  </span>
                `).join('')}
              </div>` : ''}
          `;
        })()}
      </div>`;

    // Chroma Key section
    const currentChroma = (clip.effects || []).find((e) => e && e.type === 'chroma_key') || null;
    const cp = (currentChroma && currentChroma.params) || {};
    html += `
      <div class="insp-section">
        <div style="display:flex;align-items:center;justify-content:space-between;">
          <h3>Green Screen (Chroma Key)</h3>
          <label class="field inline" style="margin:0;">
            <input type="checkbox" id="ck-enabled" ${currentChroma ? 'checked' : ''}>
            <span>Enable</span>
          </label>
        </div>
        ${currentChroma ? `
          <div class="grid-2">
            <label class="field"><span>Key Color</span>
              <input type="color" id="ck-color" value="${cssColor(cp.color || '#00FF00')}"></label>
            <label class="field"><span>Tolerance</span>
              <input type="number" step="0.02" min="0.01" max="1" id="ck-sim" value="${cp.similarity ?? 0.3}"></label>
          </div>
          <div class="grid-2">
            <label class="field"><span>Softness</span>
              <input type="number" step="0.02" min="0" max="1" id="ck-blend" value="${cp.blend ?? 0.1}"></label>
            <label class="field"><span>Spill Suppression</span>
              <input type="number" step="0.05" min="0" max="1" id="ck-spill" value="${cp.spill ?? 0.0}"></label>
          </div>
        ` : ''}
      </div>`;

    // Color Look Preset section
    const currentLook = (clip.effects || []).find((e) => e && e.type === 'preset_look') || null;
    const lp = (currentLook && currentLook.params) || {};
    html += `
      <div class="insp-section">
        <div style="display:flex;align-items:center;justify-content:space-between;">
          <h3>Color Look Preset</h3>
          <label class="field inline" style="margin:0;">
            <input type="checkbox" id="look-enabled" ${currentLook ? 'checked' : ''}>
            <span>Enable</span>
          </label>
        </div>
        ${currentLook ? `
          <div class="grid-2">
            <label class="field"><span>Preset Look</span>
              <select id="look-choice">
                ${[
                  ['film_warm', 'Film Warm'],
                  ['clean_cold', 'Clean Cold'],
                  ['cyberpunk', 'Cyberpunk Neon'],
                  ['vintage', 'Vintage Film'],
                  ['noir', 'Noir (Black & White)'],
                  ['teal_and_orange', 'Teal & Orange'],
                  ['bleach_bypass', 'Bleach Bypass'],
                ].map(([id, label]) =>
                  `<option value="${id}"${(lp.look || 'film_warm') === id ? ' selected' : ''}>${label}</option>`).join('')}
              </select>
            </label>
            <label class="field"><span>Intensity ${Math.round((lp.intensity ?? 1) * 100)}%</span>
              <input type="range" min="0" max="1" step="0.05" id="look-intensity" value="${lp.intensity ?? 1}"></label>
          </div>
        ` : ''}
      </div>`;

    // 3D LUT section
    const currentLut = (clip.effects || []).find((e) => e && e.type === 'lut3d') || null;
    const lutParams = (currentLut && currentLut.params) || {};
    html += `
      <div class="insp-section">
        <div style="display:flex;align-items:center;justify-content:space-between;">
          <h3>3D LUT (.cube)</h3>
          <label class="field inline" style="margin:0;">
            <input type="checkbox" id="lut-enabled" ${currentLut ? 'checked' : ''}>
            <span>Enable</span>
          </label>
        </div>
        ${currentLut ? `
          <div class="grid-2">
            <label class="field"><span>LUT File</span>
              <select id="lut-file">
                <option value="cinematic_warm.cube"${(lutParams.file || '') === 'cinematic_warm.cube' ? ' selected' : ''}>Cinematic Warm (.cube)</option>
                <option value="cyberpunk_neon.cube"${(lutParams.file || '') === 'cyberpunk_neon.cube' ? ' selected' : ''}>Cyberpunk Neon (.cube)</option>
                <option value="vintage_film.cube"${(lutParams.file || '') === 'vintage_film.cube' ? ' selected' : ''}>Vintage Film (.cube)</option>
              </select>
            </label>
            <label class="field"><span>Interpolation</span>
              <select id="lut-interp">
                <option value="tetrahedral"${(lutParams.interp || 'tetrahedral') === 'tetrahedral' ? ' selected' : ''}>Tetrahedral</option>
                <option value="trilinear"${(lutParams.interp || '') === 'trilinear' ? ' selected' : ''}>Trilinear</option>
                <option value="nearest"${(lutParams.interp || '') === 'nearest' ? ' selected' : ''}>Nearest</option>
              </select>
            </label>
          </div>
        ` : ''}
      </div>`;

    // Mask section
    const currentMask = (clip.effects || []).find((e) =>
      e && (e.type === 'rectangle_mask' || e.type === 'ellipse_mask' || e.type === 'freeform_mask' ||
            e.type === 'polygon_mask' || e.type === 'linear_mask' || e.type === 'split_mask')
    ) || null;
    let maskType = 'none';
    if (currentMask) {
      if (currentMask.type === 'polygon_mask') maskType = 'freeform';
      else if (currentMask.type === 'split_mask' || currentMask.type === 'linear_mask') maskType = 'linear';
      else maskType = currentMask.type.replace('_mask', '');
    }
    const mp = (currentMask && currentMask.params) || {};

    html += `
      <div class="insp-section">
        <h3>Mask</h3>
        <label class="field"><span>Type</span>
          <select id="mask-type">
            <option value="none"${maskType === 'none' ? ' selected' : ''}>None (No mask)</option>
            <option value="rectangle"${maskType === 'rectangle' ? ' selected' : ''}>Shape: Rectangle</option>
            <option value="ellipse"${maskType === 'ellipse' ? ' selected' : ''}>Shape: Circle / Ellipse</option>
            <option value="linear"${maskType === 'linear' ? ' selected' : ''}>Shape: Linear Split</option>
            <option value="freeform"${maskType === 'freeform' ? ' selected' : ''}>Free Mask: Polygon / Freeform</option>
          </select>
        </label>`;

    if (maskType === 'rectangle') {
      html += `
        <div class="grid-2">
          <label class="field"><span>X offset</span>
            <input type="number" step="0.01" min="0" max="1" id="mask-rect-x" value="${mp.x ?? 0.2}"></label>
          <label class="field"><span>Y offset</span>
            <input type="number" step="0.01" min="0" max="1" id="mask-rect-y" value="${mp.y ?? 0.2}"></label>
        </div>
        <div class="grid-2">
          <label class="field"><span>Width</span>
            <input type="number" step="0.01" min="0.01" max="1" id="mask-rect-w" value="${mp.w ?? 0.6}"></label>
          <label class="field"><span>Height</span>
            <input type="number" step="0.01" min="0.01" max="1" id="mask-rect-h" value="${mp.h ?? 0.6}"></label>
        </div>
        <div class="grid-2">
          <label class="field"><span>Roundness</span>
            <input type="number" step="0.01" min="0" max="1" id="mask-rect-round" value="${mp.round ?? 0}"></label>
          <label class="field"><span>Feather (px)</span>
            <input type="number" step="1" min="0" max="100" id="mask-feather" value="${mp.feathering ?? 0}"></label>
        </div>
        <label class="field inline"><input type="checkbox" id="mask-inverted" ${mp.inverted ? 'checked' : ''}>
          <span>Invert Mask (cut hole)</span></label>`;
    } else if (maskType === 'ellipse') {
      html += `
        <div class="grid-2">
          <label class="field"><span>Center X</span>
            <input type="number" step="0.01" min="0" max="1" id="mask-ell-cx" value="${mp.cx ?? 0.5}"></label>
          <label class="field"><span>Center Y</span>
            <input type="number" step="0.01" min="0" max="1" id="mask-ell-cy" value="${mp.cy ?? 0.5}"></label>
        </div>
        <div class="grid-2">
          <label class="field"><span>Radius X</span>
            <input type="number" step="0.01" min="0.01" max="1" id="mask-ell-rx" value="${mp.rx ?? 0.3}"></label>
          <label class="field"><span>Radius Y</span>
            <input type="number" step="0.01" min="0.01" max="1" id="mask-ell-ry" value="${mp.ry ?? 0.3}"></label>
        </div>
        <div class="grid-2">
          <label class="field"><span>Rotation (°)</span>
            <input type="number" step="1" min="-180" max="180" id="mask-ell-rot" value="${mp.rotation ?? 0}"></label>
          <label class="field"><span>Feather (px)</span>
            <input type="number" step="1" min="0" max="100" id="mask-feather" value="${mp.feathering ?? 0}"></label>
        </div>
        <label class="field inline"><input type="checkbox" id="mask-inverted" ${mp.inverted ? 'checked' : ''}>
          <span>Invert Mask (cut hole)</span></label>`;
    } else if (maskType === 'linear') {
      html += `
        <div class="grid-2">
          <label class="field"><span>Center X</span>
            <input type="number" step="0.01" min="0" max="1" id="mask-lin-x" value="${mp.x ?? 0.5}"></label>
          <label class="field"><span>Center Y</span>
            <input type="number" step="0.01" min="0" max="1" id="mask-lin-y" value="${mp.y ?? 0.5}"></label>
        </div>
        <div class="grid-2">
          <label class="field"><span>Angle (°)</span>
            <input type="number" step="1" min="-180" max="180" id="mask-lin-angle" value="${mp.angle ?? 0}"></label>
          <label class="field"><span>Feather (px)</span>
            <input type="number" step="1" min="0" max="100" id="mask-feather" value="${mp.feathering ?? 0}"></label>
        </div>
        <label class="field inline"><input type="checkbox" id="mask-inverted" ${mp.inverted ? 'checked' : ''}>
          <span>Invert Mask (cut opposite side)</span></label>`;
    } else if (maskType === 'freeform') {
      const pts = mp.points || [];
      html += `
        <div class="grid-2">
          <label class="field"><span>Feather (px)</span>
            <input type="number" step="1" min="0" max="100" id="mask-feather" value="${mp.feathering ?? 0}"></label>
          <label class="field inline" style="margin-top: 18px;"><input type="checkbox" id="mask-inverted" ${mp.inverted ? 'checked' : ''}>
            <span>Invert</span></label>
        </div>
        <div class="mask-actions">
          <button class="btn tiny ${state.drawMaskMode ? 'primary' : ''}" id="btn-mask-draw">${state.drawMaskMode ? 'Drawing Active' : 'Draw Points'}</button>
          <button class="btn tiny" id="btn-mask-reset">Reset Points</button>
          <button class="btn tiny danger" id="btn-mask-clear">Clear Points</button>
        </div>
        <p class="dim small">${state.drawMaskMode ? 'Click on preview to add points. Drag handles to move.' : 'Drag points on preview or click Draw Points to add more.'}</p>
        <div class="mask-points-list">
          ${pts.map((pt, idx) => `
            <div style="display: flex; align-items: center; justify-content: space-between; padding: 2px 0;">
              <span>#${idx + 1}: (${(pt.x !== undefined ? pt.x : pt[0]).toFixed(2)}, ${(pt.y !== undefined ? pt.y : pt[1]).toFixed(2)})</span>
              <button class="icon-btn tiny danger" data-del-pt="${idx}" style="width: 18px; height: 18px;" title="Delete point">&times;</button>
            </div>
          `).join('')}
        </div>`;
    }

    if (maskType !== 'none') {
      const kfs = (currentMask && currentMask.keyframes) || [];
      const clipTime = Math.max(0, (state.playhead - clip.start) * (clip.speed || 1));
      const isAtKf = kfs.some((kf) => Math.abs(kf.time - clipTime) < 0.08);

      html += `
        <div class="keyframe-bar">
          <div style="display:flex;align-items:center;gap:6px;">
            <span class="kf-diamond ${isAtKf ? 'active' : ''}"></span>
            <span style="font-size:12px;font-weight:500;">Keyframes (${kfs.length})</span>
          </div>
          <div class="kf-actions">
            <button class="btn tiny" id="btn-kf-toggle" title="${isAtKf ? 'Remove keyframe at playhead' : 'Add keyframe at playhead'}">
              ${isAtKf ? '− Remove' : '+ Add at Playhead'}
            </button>
          </div>
        </div>`;

      if (kfs.length > 0) {
        html += `
          <div class="keyframes-list">
            ${kfs.map((kf, i) => `
              <span class="kf-chip ${Math.abs(kf.time - clipTime) < 0.08 ? 'active' : ''}" data-kf-time="${kf.time.toFixed(2)}" title="Jump playhead to ${kf.time.toFixed(2)}s">
                ${kf.time.toFixed(2)}s
                <span data-del-kf="${i}" style="color:var(--bad);font-weight:bold;margin-left:3px;">&times;</span>
              </span>
            `).join('')}
          </div>`;
      }
    }

    html += `</div>`;
  }

  if (clip.text) {
    const style = clip.text;
    html += `
      <div class="insp-section">
        <h3>Text</h3>
        <label class="field"><span>Content</span>
          <textarea id="tx-content" spellcheck="false">${esc(style.content)}</textarea></label>
        <div class="grid-2">
          <label class="field"><span>Size (px)</span>
            <input type="number" step="1" min="4" id="tx-size" value="${style.font_size}"></label>
          <label class="field"><span>Colour</span>
            <input type="color" id="tx-color" value="${cssColor(style.color)}"></label>
        </div>
        <label class="field"><span>X expression</span>
          <input type="text" id="tx-x" value="${esc(style.x)}" spellcheck="false"></label>
        <label class="field"><span>Y expression</span>
          <input type="text" id="tx-y" value="${esc(style.y)}" spellcheck="false"></label>
        <label class="field inline"><input type="checkbox" id="tx-box" ${style.box ? 'checked' : ''}>
          <span>Background box</span></label>
      </div>`;
  }

  html += `
    <div class="insp-section">
      <h3>Transition in</h3>
      <div class="grid-2">
        <label class="field"><span>Type</span>
          <select id="tr-type">
            <option value="">none (hard cut)</option>
            ${(state.data.transitions || []).map((t) =>
              `<option value="${esc(t.name)}"${(clip.transition && clip.transition.type === t.name) ? ' selected' : ''}>${esc(t.label || t.name)}</option>`).join('')}
          </select></label>
        <label class="field"><span>Duration (s)</span>
          <input type="number" step="0.05" min="0.05" id="tr-duration"
                 value="${(clip.transition && clip.transition.duration) || 0.5}"></label>
      </div>
      <p class="dim small">A transition set on a picture clip is applied to its linked audio too, so the two do not drift apart.</p>
    </div>

    <div class="insp-section">
      <h3>Properties</h3>
      <dl class="readout">${rows.map(([k, v]) =>
        `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join('')}</dl>
    </div>`;

  body.innerHTML = html;
  bindInspector(clip);
}

function cssColor(value) {
  const text = String(value || 'white').split('@')[0].trim();
  const named = { white: '#ffffff', black: '#000000', yellow: '#ffff00',
                  red: '#ff0000', green: '#00ff00', blue: '#0000ff' };
  if (named[text.toLowerCase()]) return named[text.toLowerCase()];
  if (/^#?[0-9a-f]{6}$/i.test(text)) return text.startsWith('#') ? text : `#${text}`;
  return '#ffffff';
}

function bindInspector(clip) {
  const patch = (body, message) => {
    api(`/api/clip/${clip.id}`, { method: 'POST', body })
      .then((result) => { applyState(result.state); if (message) toast(message, 'good'); })
      .catch((error) => toast(error.message, 'bad'));
  };

  const number = (id) => {
    const node = $(id);
    if (!node) return null;
    return parseFloat(node.value);
  };

  const live = (id, event, handler) => {
    const node = $(id);
    if (node) node.addEventListener(event, handler);
  };

  live('clip-start', 'change', () => patch({ start: number('clip-start') }));
  live('clip-duration', 'change', () => patch({ duration: number('clip-duration') }));
  live('clip-in', 'change', () => patch({ in_point: number('clip-in') }));
  live('clip-speed', 'change', () => patch({ speed: number('clip-speed') }));
  live('clip-volume', 'input', () => patch({ volume: number('clip-volume') }));

  const transform = () => patch({
    transform: {
      scale: number('tf-scale'), rotate: number('tf-rotate'),
      x: number('tf-x'), y: number('tf-y'),
      opacity: number('tf-opacity'), fit: $('tf-fit') ? $('tf-fit').value : undefined,
    },
  });
  ['tf-scale', 'tf-rotate', 'tf-x', 'tf-y', 'tf-opacity', 'tf-fit']
    .forEach((id) => live(id, 'change', transform));

  live('clip-blend', 'change', () => patch({ blend_mode: $('clip-blend').value }));

  live('btn-tf-add-kf', 'click', () => {
    const clipTime = Math.max(0, Math.min(clip.duration, (state.playhead - clip.start) * (clip.speed || 1)));
    const kfs = (clip.transform_keyframes || []).slice();
    const existingIdx = kfs.findIndex((k) => Math.abs((k.time || 0) - clipTime) < 0.08);
    const curT = clip.transform || {};
    const newKf = {
      time: Math.round(clipTime * 1000) / 1000,
      scale: number('tf-scale') ?? curT.scale ?? 1,
      rotate: number('tf-rotate') ?? curT.rotate ?? 0,
      x: number('tf-x') ?? curT.x ?? 0,
      y: number('tf-y') ?? curT.y ?? 0,
      opacity: number('tf-opacity') ?? curT.opacity ?? 1,
    };
    if (existingIdx >= 0) kfs[existingIdx] = newKf;
    else kfs.push(newKf);
    kfs.sort((a, b) => a.time - b.time);
    patch({ transform_keyframes: kfs }, 'Motion keyframe saved');
  });

  live('btn-tf-del-kf', 'click', () => {
    const clipTime = Math.max(0, Math.min(clip.duration, (state.playhead - clip.start) * (clip.speed || 1)));
    const kfs = (clip.transform_keyframes || []).filter((k) => Math.abs((k.time || 0) - clipTime) >= 0.08);
    patch({ transform_keyframes: kfs }, 'Motion keyframe removed');
  });

  document.querySelectorAll('[data-tf-kf]').forEach((chip) => {
    chip.addEventListener('click', (e) => {
      if (e.target.dataset.delTfKf !== undefined) {
        const idx = parseInt(e.target.dataset.delTfKf, 10);
        const kfs = (clip.transform_keyframes || []).slice();
        kfs.splice(idx, 1);
        patch({ transform_keyframes: kfs });
        return;
      }
      const t = parseFloat(chip.dataset.tfKf);
      seek(clip.start + t / (clip.speed || 1), { keepPlaying: false });
    });
  });

  // Chroma Key bindings
  const updateChromaKey = (enabled, params = null) => {
    const effects = (clip.effects || []).filter((e) => e && e.type !== 'chroma_key');
    if (enabled) {
      effects.push({
        type: 'chroma_key',
        params: params || { color: '#00FF00', similarity: 0.3, blend: 0.1, spill: 0.0 },
      });
    }
    patch({ effects });
  };
  live('ck-enabled', 'change', () => {
    updateChromaKey($('ck-enabled').checked);
  });
  const onChromaParam = () => {
    const params = {
      color: $('ck-color') ? $('ck-color').value : '#00FF00',
      similarity: number('ck-sim') ?? 0.3,
      blend: number('ck-blend') ?? 0.1,
      spill: number('ck-spill') ?? 0.0,
    };
    updateChromaKey(true, params);
  };
  ['ck-color', 'ck-sim', 'ck-blend', 'ck-spill'].forEach((id) => live(id, 'change', onChromaParam));

  // Color Look Preset bindings
  const updateColorLook = (enabled, params = null) => {
    const effects = (clip.effects || []).filter((e) => e && e.type !== 'preset_look');
    if (enabled) {
      effects.push({
        type: 'preset_look',
        params: params || { look: 'film_warm', intensity: 1.0 },
      });
    }
    patch({ effects });
  };
  live('look-enabled', 'change', () => {
    updateColorLook($('look-enabled').checked);
  });
  const onLookParam = () => {
    const params = {
      look: $('look-choice') ? $('look-choice').value : 'film_warm',
      intensity: number('look-intensity') ?? 1.0,
    };
    updateColorLook(true, params);
  };
  ['look-choice', 'look-intensity'].forEach((id) => live(id, 'change', onLookParam));

  // 3D LUT bindings
  const updateLut = (enabled, params = null) => {
    const effects = (clip.effects || []).filter((e) => e && e.type !== 'lut3d');
    if (enabled) {
      effects.push({
        type: 'lut3d',
        params: params || { file: 'cinematic_warm.cube', interp: 'tetrahedral' },
      });
    }
    patch({ effects });
  };
  live('lut-enabled', 'change', () => {
    updateLut($('lut-enabled').checked);
  });
  const onLutParam = () => {
    const params = {
      file: $('lut-file') ? $('lut-file').value : 'cinematic_warm.cube',
      interp: $('lut-interp') ? $('lut-interp').value : 'tetrahedral',
    };
    updateLut(true, params);
  };
  ['lut-file', 'lut-interp'].forEach((id) => live(id, 'change', onLutParam));

  const maskEffectTypes = ['rectangle_mask', 'ellipse_mask', 'freeform_mask', 'polygon_mask', 'linear_mask', 'split_mask'];
  const updateMaskEffect = (type, params) => {
    const effects = (clip.effects || []).filter((e) =>
      e && !maskEffectTypes.includes(e.type)
    );
    if (type && type !== 'none') {
      effects.push({ type: type.endsWith('_mask') ? type : `${type}_mask`, params, keyframes: [] });
    }
    patch({ effects });
  };

  const getActiveMask = () => {
    return (clip.effects || []).find((e) =>
      e && maskEffectTypes.includes(e.type)
    );
  };

  live('mask-type', 'change', () => {
    const val = $('mask-type').value;
    if (val === 'none') {
      state.drawMaskMode = false;
      updateMaskEffect(null);
    } else if (val === 'rectangle') {
      state.drawMaskMode = false;
      updateMaskEffect('rectangle_mask', {
        x: 0.2, y: 0.2, w: 0.6, h: 0.6, round: 0.0, feathering: 0.0, inverted: false,
      });
    } else if (val === 'ellipse') {
      state.drawMaskMode = false;
      updateMaskEffect('ellipse_mask', {
        cx: 0.5, cy: 0.5, rx: 0.3, ry: 0.3, rotation: 0.0, feathering: 0.0, inverted: false,
      });
    } else if (val === 'linear') {
      state.drawMaskMode = false;
      updateMaskEffect('linear_mask', {
        x: 0.5, y: 0.5, angle: 0.0, feathering: 0.0, inverted: false,
      });
    } else if (val === 'freeform') {
      updateMaskEffect('freeform_mask', {
        points: [
          { x: 0.3, y: 0.2 }, { x: 0.7, y: 0.2 }, { x: 0.8, y: 0.7 },
          { x: 0.5, y: 0.85 }, { x: 0.2, y: 0.7 },
        ],
        feathering: 0.0, inverted: false,
      });
    }
  });

  const onMaskParamChange = () => {
    const mask = getActiveMask();
    if (!mask) return;
    const clipTime = Math.max(0, (state.playhead - clip.start) * (clip.speed || 1));
    const kfs = mask.keyframes || [];
    let target = mask.params = mask.params || {};
    const activeKf = kfs.find((kf) => Math.abs(kf.time - clipTime) < 0.08);
    if (activeKf) target = activeKf.params = activeKf.params || {};

    if (mask.type === 'rectangle_mask') {
      if ($('mask-rect-x')) target.x = number('mask-rect-x');
      if ($('mask-rect-y')) target.y = number('mask-rect-y');
      if ($('mask-rect-w')) target.w = number('mask-rect-w');
      if ($('mask-rect-h')) target.h = number('mask-rect-h');
      if ($('mask-rect-round')) target.round = number('mask-rect-round');
    } else if (mask.type === 'ellipse_mask') {
      if ($('mask-ell-cx')) target.cx = number('mask-ell-cx');
      if ($('mask-ell-cy')) target.cy = number('mask-ell-cy');
      if ($('mask-ell-rx')) target.rx = number('mask-ell-rx');
      if ($('mask-ell-ry')) target.ry = number('mask-ell-ry');
      if ($('mask-ell-rot')) target.rotation = number('mask-ell-rot');
    } else if (mask.type === 'linear_mask' || mask.type === 'split_mask') {
      if ($('mask-lin-x')) target.x = number('mask-lin-x');
      if ($('mask-lin-y')) target.y = number('mask-lin-y');
      if ($('mask-lin-angle')) target.angle = number('mask-lin-angle');
    }
    if ($('mask-feather')) target.feathering = number('mask-feather');
    if ($('mask-inverted')) target.inverted = $('mask-inverted').checked;
    patch({ effects: clip.effects });
  };

  ['mask-rect-x', 'mask-rect-y', 'mask-rect-w', 'mask-rect-h', 'mask-rect-round',
   'mask-ell-cx', 'mask-ell-cy', 'mask-ell-rx', 'mask-ell-ry', 'mask-ell-rot',
   'mask-lin-x', 'mask-lin-y', 'mask-lin-angle',
   'mask-feather'].forEach((id) => live(id, 'change', onMaskParamChange));
  live('mask-inverted', 'change', onMaskParamChange);

  live('btn-kf-toggle', 'click', () => {
    const mask = getActiveMask();
    if (!mask) return;
    mask.keyframes = mask.keyframes || [];
    const clipTime = Math.max(0, (state.playhead - clip.start) * (clip.speed || 1));
    const kfIdx = mask.keyframes.findIndex((kf) => Math.abs(kf.time - clipTime) < 0.08);
    if (kfIdx >= 0) {
      mask.keyframes.splice(kfIdx, 1);
      toast(`removed keyframe at ${clipTime.toFixed(2)}s`);
    } else {
      const effParams = getEffectiveMaskParams(mask, clipTime);
      mask.keyframes.push({
        time: Math.round(clipTime * 100) / 100,
        params: JSON.parse(JSON.stringify(effParams)),
      });
      mask.keyframes.sort((a, b) => a.time - b.time);
      toast(`added keyframe at ${clipTime.toFixed(2)}s`, 'good');
    }
    patch({ effects: clip.effects });
  });

  document.querySelectorAll('[data-kf-time]').forEach((el) => {
    el.addEventListener('click', (e) => {
      if (e.target.dataset.delKf !== undefined) return;
      const t = parseFloat(el.dataset.kfTime);
      seek(clip.start + t / (clip.speed || 1), { keepPlaying: false });
    });
  });

  document.querySelectorAll('[data-del-kf]').forEach((btn) => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const mask = getActiveMask();
      if (!mask || !mask.keyframes) return;
      const idx = parseInt(btn.dataset.delKf, 10);
      mask.keyframes.splice(idx, 1);
      patch({ effects: clip.effects });
    });
  });

  live('btn-mask-draw', 'click', () => {
    state.drawMaskMode = !state.drawMaskMode;
    renderInspector();
    updateMaskOverlay();
  });

  live('btn-mask-reset', 'click', () => {
    const mask = getActiveMask();
    if (!mask) return;
    mask.params = mask.params || {};
    mask.params.points = [
      { x: 0.3, y: 0.2 }, { x: 0.7, y: 0.2 }, { x: 0.8, y: 0.7 },
      { x: 0.5, y: 0.85 }, { x: 0.2, y: 0.7 },
    ];
    patch({ effects: clip.effects });
  });

  live('btn-mask-clear', 'click', () => {
    const mask = getActiveMask();
    if (!mask) return;
    mask.params = mask.params || {};
    mask.params.points = [];
    patch({ effects: clip.effects });
  });

  document.querySelectorAll('[data-del-pt]').forEach((btn) => {
    btn.addEventListener('click', (e) => {
      const idx = parseInt(e.target.dataset.delPt, 10);
      const mask = getActiveMask();
      if (!mask || !mask.params || !mask.params.points) return;
      mask.params.points.splice(idx, 1);
      patch({ effects: clip.effects });
    });
  });

  live('tx-content', 'change', () => patch({ text: { content: $('tx-content').value } }));
  live('tx-size', 'change', () => patch({ text: { font_size: number('tx-size') } }));
  live('tx-x', 'change', () => patch({ text: { x: $('tx-x').value } }));
  live('tx-y', 'change', () => patch({ text: { y: $('tx-y').value } }));
  live('tx-box', 'change', () => patch({ text: { box: $('tx-box').checked } }));
  live('tx-color', 'change', () => {
    const rgb = $('tx-color').value;
    const r = parseInt(rgb.slice(1, 3), 16), g = parseInt(rgb.slice(3, 5), 16), b = parseInt(rgb.slice(5, 7), 16);
    patch({ text: { color: `0x${[r, g, b].map((v) => v.toString(16).padStart(2, '0')).join('')}` } });
  });

  live('tr-type', 'change', () => {
    const type = $('tr-type').value;
    patch({ transition: type ? { type, duration: number('tr-duration') || 0.5 } : null });
  });
  live('tr-duration', 'change', () => {
    const type = $('tr-type').value;
    if (type) patch({ transition: { type, duration: number('tr-duration') } });
  });
}

// ------------------------------------------------------------ file browser

async function openBrowser() {
  $('browser').classList.remove('hidden');
  state.picking.clear();
  await loadBrowse(state.data && state.data.browse_root);
}

async function loadBrowse(path) {
  try {
    const listing = await api(`/api/browse?dir=${encodeURIComponent(path || '')}`);
    state.browse = listing;
    $('browse-path').value = listing.path;
    $('browse-roots').innerHTML = (listing.roots || []).map((root) =>
      `<button class="btn" data-root="${esc(root.path)}">${esc(root.name)}</button>`).join('');
    $('browse-roots').querySelectorAll('[data-root]').forEach((node) =>
      node.addEventListener('click', () => loadBrowse(node.dataset.root)));
    renderBrowseList();
  } catch (error) {
    toast(error.message, 'bad');
  }
}

function renderBrowseList() {
  const listing = state.browse;
  if (!listing) return;
  const filter = ($('browse-filter').value || '').toLowerCase();
  const entries = (listing.entries || []).filter((e) =>
    !filter || e.name.toLowerCase().includes(filter));

  $('browse-list').innerHTML = entries.length ? entries.map((entry) => `
    <div class="browse-row${state.picking.has(entry.path) ? ' picked' : ''}"
         data-path="${esc(entry.path)}" data-kind="${esc(entry.kind)}">
      <span class="icon">${entry.kind === 'dir' ? '&#128193;' : '&#127916;'}</span>
      <span class="name">${esc(entry.name)}</span>
      <span class="size">${entry.kind === 'media' ? bytes(entry.size_bytes) : ''}</span>
    </div>`).join('') : '<p class="dim pad">Nothing here.</p>';

  $('browse-list').querySelectorAll('.browse-row').forEach((node) => {
    node.addEventListener('click', () => {
      if (node.dataset.kind === 'dir') { loadBrowse(node.dataset.path); return; }
      const path = node.dataset.path;
      if (state.picking.has(path)) state.picking.delete(path);
      else state.picking.add(path);
      renderBrowseList();
    });
    node.addEventListener('dblclick', () => {
      if (node.dataset.kind === 'media') { loadBrowse(state.browse.path); }
    });
  });
}

async function importPicked() {
  const paths = [...state.picking];
  if (!paths.length) { toast('nothing selected'); return; }
  try {
    const result = await api('/api/import', { method: 'POST', body: { paths } });
    applyState(result.state);
    const failed = (result.results || []).filter((r) => !r.ok);
    const ok = (result.results || []).length - failed.length;
    if (ok) toast(`imported ${ok} file${ok === 1 ? '' : 's'}`, 'good');
    failed.forEach((f) => toast(`${f.path.split(/[\\/]/).pop()}: ${f.error}`, 'bad'));
    $('browser').classList.add('hidden');
    state.picking.clear();
  } catch (error) {
    toast(error.message, 'bad');
  }
}

// ------------------------------------------------------------------ export

async function openExport() {
  $('export').classList.remove('hidden');
  const card = $('render-done-card');
  if (card) card.classList.add('hidden');
  const status = $('render-status');
  if (status && !state.renderJob) status.classList.add('hidden');

  if (!state.data || !state.data.presets || !state.data.presets.length) {
    try {
      const pList = await api('/api/presets');
      if (!state.data) state.data = {};
      state.data.presets = pList;
    } catch {}
  }
  const select = $('export-preset');
  const presetsList = (state.data && state.data.presets) || [];
  select.innerHTML = presetsList.map((p) =>
    `<option value="${esc(p.name)}">${esc(p.label)}</option>`).join('');
  if (presetsList.some((p) => p.name === 'web-1080p')) {
    select.value = 'web-1080p';
  } else if (select.options.length > 0) {
    select.value = select.options[0].value;
  }
  describePreset();
  const stem = ((state.data && state.data.name) || 'export').replace(/[^\w\-. ]+/g, '_');
  const preset = presetsList.find((p) => p.name === select.value);
  const exportDir = (state.data && state.data.export_dir) || 'exports';
  const sep = exportDir.includes('/') && !exportDir.includes('\\') ? '/' : '\\';
  $('export-output').value = `${exportDir}${sep}${stem}.${preset ? preset.container : 'mp4'}`;
}

function describePreset() {
  const name = $('export-preset').value;
  const presetsList = (state.data && state.data.presets) || [];
  const preset = presetsList.find((p) => p.name === name);
  if (!preset) return;
  $('export-note').textContent = preset.description || '';
  $('export-crf').value = preset.crf;
  $('export-crf').disabled = !!(preset.gif || preset.audio_only);
  $('export-encoder').disabled = !!(preset.gif || preset.audio_only);

  const stem = ((state.data && state.data.name) || 'export').replace(/[^\w\-. ]+/g, '_');
  const exportDir = (state.data && state.data.export_dir) || 'exports';
  const sep = exportDir.includes('/') && !exportDir.includes('\\') ? '/' : '\\';
  $('export-output').value = `${exportDir}${sep}${stem}.${preset.container}`;
}

async function startRender() {
  const preset = $('export-preset').value;
  const body = {
    preset,
    output: $('export-output').value.trim() || null,
    crf: parseInt($('export-crf').value, 10),
    prefer_hardware: $('export-encoder').value === 'auto',
  };
  const card = $('render-done-card');
  if (card) card.classList.add('hidden');

  try {
    const job = await api('/api/render', { method: 'POST', body });
    state.renderJob = job.id;
    $('render-status').classList.remove('hidden');
    $('render-error').classList.add('hidden');
    $('export-start').disabled = true;
    toast('render started', 'good');
    pollRender();
  } catch (error) {
    toast(error.message, 'bad');
  }
}

async function pollRender() {
  if (!state.renderJob) return;
  try {
    const job = await api(`/api/render/${state.renderJob}`);
    const bar = $('render-bar');
    bar.style.width = `${Math.round(job.progress * 100)}%`;

    const label = job.status === 'running'
      ? `${Math.round(job.progress * 100)}% · ${job.done.toFixed(1)}s / ${job.duration.toFixed(1)}s · ${job.elapsed.toFixed(0)}s elapsed`
      : job.status;
    $('render-label').textContent = label;

    if (job.status === 'done') {
      $('export-start').disabled = false;
      state.renderJob = null;
      $('render-status').classList.add('hidden');
      $('render-error').classList.add('hidden');

      const card = $('render-done-card');
      if (card) {
        card.classList.remove('hidden');
        $('render-done-sub').textContent = `${job.output_name} · ${bytes(job.size_bytes)} · ${job.elapsed.toFixed(1)}s`;
        $('rendered-file-path').value = job.output_absolute || job.output;

        const dlBtn = $('btn-download-export');
        dlBtn.href = job.download_url || `/api/render/${job.id}/download`;
        dlBtn.setAttribute('download', job.output_name || 'export.mp4');

        card.dataset.jobId = job.id;
        card.dataset.filePath = job.output_absolute || job.output;
      }

      toast(`Export completed: ${job.output_name} (${bytes(job.size_bytes)})`, 'good');
      return;
    }
    if (job.status === 'failed' || job.status === 'cancelled') {
      $('export-start').disabled = false;
      state.renderJob = null;
      if (job.error) {
        const node = $('render-error');
        node.textContent = job.error;
        node.classList.remove('hidden');
      }
      toast(job.status === 'cancelled' ? 'render cancelled' : 'render failed',
            job.status === 'failed' ? 'bad' : '');
      return;
    }
  } catch (error) {
    toast(error.message, 'bad');
    state.renderJob = null;
    $('export-start').disabled = false;
    return;
  }
  state.pollTimer = setTimeout(pollRender, 450);
}

// ------------------------------------------------------------------ wiring

function bind() {
  document.querySelectorAll('[data-close]').forEach((node) =>
    node.addEventListener('click', () => $(node.dataset.close).classList.add('hidden')));

  $('btn-play').addEventListener('click', () => setPlaying(!state.playing));
  $('btn-start').addEventListener('click', () => seek(0, { keepPlaying: false }));
  $('btn-end').addEventListener('click', () =>
    seek(state.data ? state.data.duration : 0, { keepPlaying: false }));

  $('zoom').addEventListener('input', (event) => {
    state.pxPerSecond = Number(event.target.value);
    const zoomVal = $('zoom-val');
    if (zoomVal) zoomVal.textContent = `${state.pxPerSecond}px/s`;
    renderTimeline();
    paintPlayhead();
  });

  // Scrubbing: the ruler is sticky inside a scrolled container, so the offset
  // has to come from the ruler's own box, not the container's.
  const scrub = (event) => {
    const rect = $('ruler').getBoundingClientRect();
    seek((event.clientX - rect.left) / state.pxPerSecond, { keepPlaying: false });
  };
  $('ruler').addEventListener('mousedown', (event) => {
    scrub(event);
    const onMove = (moveEvent) => scrub(moveEvent);
    const onUp = () => {
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
    };
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
  });

  $('btn-split').addEventListener('click', () => {
    if (!state.selected) { toast('select a clip first'); return; }
    api(`/api/clip/${state.selected}/split`, { method: 'POST', body: { at: state.playhead } })
      .then((result) => applyState(result.state))
      .catch((error) => toast(error.message, 'bad'));
  });

  $('btn-delete').addEventListener('click', () => {
    const toDelete = Array.from(state.selectedClips.size > 0 ? state.selectedClips : (state.selected ? [state.selected] : []));
    if (toDelete.length === 0) { toast('select clip(s) first'); return; }
    api('/api/clips/delete', { method: 'POST', body: { clip_ids: toDelete } })
      .then((snapshot) => {
        state.selected = null;
        state.selectedClips.clear();
        applyState(snapshot);
      })
      .catch((error) => toast(error.message, 'bad'));
  });

  if ($('btn-snap')) {
    $('btn-snap').addEventListener('click', () => {
      state.snapping = !state.snapping;
      $('btn-snap').classList.toggle('active', state.snapping);
      toast(`Magnetic snapping ${state.snapping ? 'ON' : 'OFF'}`);
    });
  }

  if ($('btn-add-marker')) {
    $('btn-add-marker').addEventListener('click', addMarkerAtPlayhead);
  }

  initSubtitleImport();

  $('btn-ripple-delete').addEventListener('click', () => {
    if (!state.selected) { toast('select a clip first'); return; }
    api(`/api/clip/${state.selected}?ripple=1`, { method: 'DELETE' })
      .then((snapshot) => { state.selected = null; state.selectedClips.clear(); applyState(snapshot); })
      .catch((error) => toast(error.message, 'bad'));
  });

  $('btn-add-text').addEventListener('click', () => {
    $('modal-text').classList.remove('hidden');
    setTimeout(() => {
      $('text-content-input').focus();
      $('text-content-input').select();
    }, 50);
  });

  $('btn-confirm-text').addEventListener('click', async () => {
    const content = $('text-content-input').value.trim();
    if (!content) { toast('Please enter some text'); return; }
    const duration = parseFloat($('text-duration-input').value) || 3.0;
    const fontSize = parseInt($('text-fontsize-input').value, 10) || 64;
    const color = $('text-color-input').value || '#ffffff';
    const boxChoice = $('text-box-input').value;
    const hasBox = boxChoice !== 'none';
    const boxColor = boxChoice === 'solid' ? '#000000' : 'rgba(0,0,0,0.6)';

    $('modal-text').classList.add('hidden');
    try {
      const result = await api('/api/text', {
        method: 'POST',
        body: {
          content,
          start: state.playhead,
          duration,
          font_size: fontSize,
          color,
          box: hasBox,
          box_color: boxColor,
        },
      });
      applyState(result.state);
      toast('Text clip added to timeline', 'good');
    } catch (error) {
      toast(error.message, 'bad');
    }
  });

  $('text-content-input').addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
      e.preventDefault();
      $('btn-confirm-text').click();
    }
  });

  $('btn-save-marker').addEventListener('click', async () => {
    if (!currentMarkerTarget) return;
    const name = $('marker-name-input').value.trim();
    const activeChip = document.querySelector('#marker-color-chips .color-chip.selected');
    const color = activeChip ? activeChip.dataset.color : '#ffb547';
    $('modal-marker').classList.add('hidden');

    try {
      if (currentMarkerTarget.mode === 'add') {
        const res = await api('/api/marker', {
          method: 'POST',
          body: { time: currentMarkerTarget.time, name, color },
        });
        applyState(res.state);
        toast(`Marker added at ${timecode(currentMarkerTarget.time)}`, 'good');
      } else {
        const marker = currentMarkerTarget.marker;
        const res = await api('/api/marker', {
          method: 'POST',
          body: { id: marker.id, time: marker.time, name, color },
        });
        applyState(res.state);
        toast('Marker updated', 'good');
      }
    } catch (err) {
      toast(err.message, 'bad');
    }
  });

  $('btn-delete-marker').addEventListener('click', async () => {
    if (!currentMarkerTarget || currentMarkerTarget.mode !== 'edit') return;
    const marker = currentMarkerTarget.marker;
    $('modal-marker').classList.add('hidden');
    try {
      const res = await api(`/api/marker?id=${encodeURIComponent(marker.id)}`, { method: 'DELETE' });
      applyState(res);
      toast('Marker deleted', 'good');
    } catch (err) {
      toast(err.message, 'bad');
    }
  });

  document.querySelectorAll('#marker-color-chips .color-chip').forEach((chip) => {
    chip.addEventListener('click', () => {
      document.querySelectorAll('#marker-color-chips .color-chip').forEach((c) => c.classList.remove('selected'));
      chip.classList.add('selected');
    });
  });

  $('marker-name-input').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      $('btn-save-marker').click();
    }
  });

  $('btn-copy-export-path').addEventListener('click', () => {
    const path = $('rendered-file-path').value;
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(path).then(() => toast('File path copied to clipboard', 'good'));
    } else {
      $('rendered-file-path').select();
      document.execCommand('copy');
      toast('File path copied to clipboard', 'good');
    }
  });

  $('btn-reveal-exported').addEventListener('click', async () => {
    const card = $('render-done-card');
    const jobId = card.dataset.jobId;
    if (!jobId) return;
    try {
      await api(`/api/render/${jobId}/reveal`, { method: 'POST' });
      toast('Opened in Windows Explorer', 'good');
    } catch (err) {
      toast(`Could not open explorer: ${err.message}`, 'bad');
    }
  });

  $('btn-open-export-dir').addEventListener('click', async () => {
    try {
      await api('/api/open-export-dir', { method: 'POST' });
      toast('Export folder opened in Windows Explorer', 'good');
    } catch (err) {
      toast(`Could not open folder: ${err.message}`, 'bad');
    }
  });

  $('btn-add-track').addEventListener('click', () => {
    api('/api/track', { method: 'POST', body: { kind: 'video' } })
      .then((result) => applyState(result.state))
      .catch((error) => toast(error.message, 'bad'));
  });

  if ($('btn-undo')) $('btn-undo').addEventListener('click', undo);
  if ($('btn-redo')) $('btn-redo').addEventListener('click', redo);

  $('btn-save').addEventListener('click', () => {
    api('/api/project/save', { method: 'POST' })
      .then((result) => { toast(`saved ${result.saved}`, 'good'); })
      .catch((error) => toast(error.message, 'bad'));
  });

  $('project-name').addEventListener('change', (event) => {
    api('/api/settings', { method: 'POST', body: { name: event.target.value } })
      .then(applyState).catch((error) => toast(error.message, 'bad'));
  });

  $('btn-browse').addEventListener('click', openBrowser);
  $('btn-browse-2').addEventListener('click', openBrowser);
  $('browse-go').addEventListener('click', () => loadBrowse($('browse-path').value));
  $('browse-up').addEventListener('click', () => {
    if (state.browse && state.browse.parent) loadBrowse(state.browse.parent);
  });
  $('browse-filter').addEventListener('input', renderBrowseList);
  $('browse-import').addEventListener('click', importPicked);
  $('browse-path').addEventListener('keydown', (event) => {
    if (event.key === 'Enter') loadBrowse($('browse-path').value);
  });

  $('btn-export').addEventListener('click', openExport);
  $('export-preset').addEventListener('change', describePreset);
  $('export-start').addEventListener('click', startRender);
  $('render-cancel').addEventListener('click', () => {
    if (!state.renderJob) return;
    api(`/api/render/${state.renderJob}`, { method: 'DELETE' })
      .then(() => toast('cancelling…'))
      .catch((error) => toast(error.message, 'bad'));
  });

  $('btn-settings').addEventListener('click', () => {
    const s = state.data.timeline.settings;
    $('set-width').value = s.width;
    $('set-height').value = s.height;
    $('set-fps').value = s.fps;
    $('set-rate').value = s.sample_rate;
    $('set-description').value = state.data.description || '';
    $('settings').classList.remove('hidden');
  });

  $('settings-apply').addEventListener('click', () => {
    api('/api/settings', {
      method: 'POST',
      body: {
        width: parseInt($('set-width').value, 10),
        height: parseInt($('set-height').value, 10),
        fps: parseFloat($('set-fps').value),
        sample_rate: parseInt($('set-rate').value, 10),
      },
    }).then((snapshot) => {
      applyState(snapshot);
      $('settings').classList.add('hidden');
      const problems = snapshot.problems || [];
      if (problems.length) toast(problems[0], 'bad');
    }).catch((error) => toast(error.message, 'bad'));
  });

  document.addEventListener('keydown', (event) => {
    if (event.target.matches('input, textarea, select')) return;
    if ((event.ctrlKey || event.metaKey) && !event.altKey) {
      if (event.key.toLowerCase() === 'z' && !event.shiftKey) {
        event.preventDefault();
        undo();
        return;
      }
      if (event.key.toLowerCase() === 'y' || (event.key.toLowerCase() === 'z' && event.shiftKey)) {
        event.preventDefault();
        redo();
        return;
      }
    }
    if (event.code === 'Space') { event.preventDefault(); setPlaying(!state.playing); }
    if (event.key === 'ArrowRight') seek(state.playhead + (event.shiftKey ? 1 : 1 / 30), { keepPlaying: false });
    if (event.key === 'ArrowLeft') seek(state.playhead - (event.shiftKey ? 1 : 1 / 30), { keepPlaying: false });
    if (event.key === 'Home') seek(0, { keepPlaying: false });
    if (event.key === 'End') seek(state.data ? state.data.duration : 0, { keepPlaying: false });
    if (event.key === 'Delete' || event.key === 'Backspace') {
      const toDelete = Array.from(state.selectedClips.size > 0 ? state.selectedClips : (state.selected ? [state.selected] : []));
      if (toDelete.length > 0) {
        api('/api/clips/delete', { method: 'POST', body: { clip_ids: toDelete } })
          .then((snapshot) => {
            state.selected = null;
            state.selectedClips.clear();
            applyState(snapshot);
          })
          .catch(() => {});
      }
    }
    if ((event.key === 'm' || event.key === 'M') && !event.metaKey && !event.ctrlKey) {
      event.preventDefault();
      addMarkerAtPlayhead();
    }
    if (event.key === 's' && !event.metaKey && !event.ctrlKey && state.selected) {
      api(`/api/clip/${state.selected}/split`, { method: 'POST', body: { at: state.playhead } })
        .then((result) => applyState(result.state)).catch(() => {});
    }
  });

  window.addEventListener('resize', () => { fitStage(); syncStage(state.playhead, state.playing); });
}

function initSubtitleImport() {
  const btn = $('btn-subtitles');
  const fileInput = $('sub-file-input');
  const exportBtn = $('btn-export-sub');

  if (btn && fileInput) {
    btn.addEventListener('click', () => fileInput.click());
    fileInput.addEventListener('change', async (e) => {
      const file = e.target.files[0];
      if (!file) return;
      try {
        const text = await file.text();
        const res = await api('/api/subtitles/import', {
          method: 'POST',
          body: { content: text, name: file.name.replace(/\.[^.]+$/, '') },
        });
        applyState(res.state);
        toast(`Imported subtitles: ${file.name}`, 'good');
      } catch (err) {
        toast(`Subtitle error: ${err.message}`, 'bad');
      } finally {
        fileInput.value = '';
      }
    });
  }

  if (exportBtn) {
    exportBtn.addEventListener('click', () => {
      window.location.href = '/api/subtitles/export?format=srt';
    });
  }
}

// -------------------------------------------------------------------- boot

async function boot() {
  bind();
  try {
    const snapshot = await api('/api/state');
    state.data = snapshot;
    render();
    if (!snapshot.timeline.tracks.length) {
      toast('empty project — import media to begin');
    }
  } catch (error) {
    toast(`could not load project: ${error.message}`, 'bad');
  }

  // Keep the stage honest if the project changes underneath us.
  setInterval(async () => {
    if (state.renderJob) return;
    try {
      const snapshot = await api('/api/state');
      state.data = snapshot;
      $('dirty-dot').classList.toggle('on', !!snapshot.dirty);
      renderTimelineLabelsOnly(snapshot);
    } catch { /* server went away; the next action will report it */ }
  }, 15000);
}

function renderTimelineLabelsOnly(snapshot) {
  // Cheap refresh: only the pieces that can drift without an edit.
  const total = timecode(snapshot.duration);
  if ($('timeline-duration').textContent !== total) $('timeline-duration').textContent = total;
  $('timecode-total').textContent = `/ ${total}`;
}

document.addEventListener('DOMContentLoaded', boot);

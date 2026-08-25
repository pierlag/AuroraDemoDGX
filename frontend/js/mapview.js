/* Rendu cartographique : champs continus, particules de vent, isobares. */

import { lut } from './colormaps.js';

const D2R = Math.PI / 180;
const mercY = (lat) => Math.log(Math.tan(Math.PI / 4 + lat * D2R / 2));
const invMercY = (y) => (2 * Math.atan(Math.exp(y)) - Math.PI / 2) / D2R;

const MS_TABLE = {
  1: [['L', 'B']], 2: [['B', 'R']], 3: [['L', 'R']], 4: [['T', 'R']],
  5: [['L', 'T'], ['B', 'R']], 6: [['T', 'B']], 7: [['L', 'T']], 8: [['L', 'T']],
  9: [['T', 'B']], 10: [['L', 'B'], ['T', 'R']], 11: [['T', 'R']], 12: [['L', 'R']],
  13: [['B', 'R']], 14: [['L', 'B']],
};

export class MapView {
  constructor(wrap) {
    this.wrap = wrap;
    this.fieldCanvas = wrap.querySelector('#layer-field');
    this.windCanvas = wrap.querySelector('#layer-wind');
    this.vecCanvas = wrap.querySelector('#layer-vector');
    this.fieldCtx = this.fieldCanvas.getContext('2d');
    this.windCtx = this.windCanvas.getContext('2d');
    this.vecCtx = this.vecCanvas.getContext('2d', { alpha: true });

    this.buf = document.createElement('canvas');
    this.bufCtx = this.buf.getContext('2d');

    this.geo = null;
    this.grid = null;
    this.frames = null;
    this.windU = null;
    this.windV = null;
    this.overlay = null;          // champ de pression pour les isobares
    this.palette = 'temperature';
    this.domain = [0, 1];
    this.step = 0;
    this.cities = [];
    this.selected = null;
    this.hover = null;
    this.layers = { wind: true, isobars: true, cities: true, grid: false };

    this.particles = [];
    this.raf = null;
    this.lastFrame = 0;

    this._onResize = () => this.resize();
    window.addEventListener('resize', this._onResize);
    new ResizeObserver(this._onResize).observe(wrap);
  }

  /* -- configuration ------------------------------------------------------ */

  setGeo(geo) {
    this.geo = geo;
    this.resize();
  }

  setCities(cities) { this.cities = cities; }

  setData({ grid, frames, palette, domain, windU, windV, overlay }) {
    this.grid = grid;
    this.frames = frames;
    this.palette = palette;
    this.domain = domain;
    if (windU !== undefined) this.windU = windU;
    if (windV !== undefined) this.windV = windV;
    if (overlay !== undefined) this.overlay = overlay;
    this.seedParticles();
    this.render();
  }

  setStep(step) {
    this.step = step;
    this.render();
  }

  setLayers(patch) {
    Object.assign(this.layers, patch);
    if (!this.layers.wind) this.windCtx.clearRect(0, 0, this.windCanvas.width, this.windCanvas.height);
    this.render();
  }

  /* -- géométrie ---------------------------------------------------------- */

  resize() {
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const w = this.wrap.clientWidth;
    const h = this.wrap.clientHeight;
    if (!w || !h) return;
    for (const c of [this.fieldCanvas, this.windCanvas, this.vecCanvas]) {
      c.width = Math.round(w * dpr);
      c.height = Math.round(h * dpr);
    }
    this.dpr = dpr;
    this.W = w * dpr;
    this.H = h * dpr;

    if (this.geo) {
      const b = this.geo.bbox;
      const x0 = b.lon_min * D2R;
      const y0 = mercY(b.lat_max);
      const xSpan = (b.lon_max - b.lon_min) * D2R;
      const ySpan = y0 - mercY(b.lat_min);
      const scale = Math.min(this.W / xSpan, this.H / ySpan);
      this.proj = {
        scale,
        x0, y0,
        offX: (this.W - xSpan * scale) / 2,
        offY: (this.H - ySpan * scale) / 2,
      };
    }
    this.seedParticles();
    this.render();
  }

  project(lon, lat) {
    const p = this.proj;
    return [
      (lon * D2R - p.x0) * p.scale + p.offX,
      (p.y0 - mercY(lat)) * p.scale + p.offY,
    ];
  }

  unproject(px, py) {
    const p = this.proj;
    const lon = ((px - p.offX) / p.scale + p.x0) / D2R;
    const lat = invMercY(p.y0 - (py - p.offY) / p.scale);
    return [lon, lat];
  }

  /* -- échantillonnage ---------------------------------------------------- */

  sample(frame, lat, lon) {
    const g = this.grid;
    if (!g || !frame) return NaN;
    const fy = (lat - g.lat0) / g.dlat;
    const fx = (lon - g.lon0) / g.dlon;
    if (fy < 0 || fx < 0 || fy > g.ny - 1 || fx > g.nx - 1) return NaN;
    const i = Math.min(g.ny - 2, Math.floor(fy));
    const j = Math.min(g.nx - 2, Math.floor(fx));
    const ty = fy - i;
    const tx = fx - j;
    const a = frame[i * g.nx + j];
    const b = frame[i * g.nx + j + 1];
    const c = frame[(i + 1) * g.nx + j];
    const d = frame[(i + 1) * g.nx + j + 1];
    return (a * (1 - tx) + b * tx) * (1 - ty) + (c * (1 - tx) + d * tx) * ty;
  }

  /** Interpole entre deux échéances pour une animation fluide. */
  frameAt(frames, step) {
    if (!frames || !frames.length) return null;
    const i0 = Math.max(0, Math.min(frames.length - 1, Math.floor(step)));
    const i1 = Math.min(frames.length - 1, i0 + 1);
    const t = step - i0;
    if (t < 1e-3 || i0 === i1) return frames[i0];
    const a = frames[i0];
    const b = frames[i1];
    if (!this._blend || this._blend.length !== a.length) this._blend = new Float32Array(a.length);
    for (let k = 0; k < a.length; k++) this._blend[k] = a[k] + (b[k] - a[k]) * t;
    return this._blend;
  }

  valueAt(lat, lon) {
    return this.sample(this.frameAt(this.frames, this.step), lat, lon);
  }

  /* -- rendu -------------------------------------------------------------- */

  render() {
    if (!this.proj || !this.geo) return;
    this.renderField();
    this.renderVectors();
  }

  renderField() {
    const ctx = this.fieldCtx;
    ctx.clearRect(0, 0, this.W, this.H);
    const frame = this.frameAt(this.frames, this.step);
    if (!frame || !this.grid) return;

    const scaleDown = 2;
    const bw = Math.max(2, Math.ceil(this.W / scaleDown));
    const bh = Math.max(2, Math.ceil(this.H / scaleDown));
    if (this.buf.width !== bw || this.buf.height !== bh) {
      this.buf.width = bw;
      this.buf.height = bh;
    }
    const img = this.bufCtx.createImageData(bw, bh);
    const px = img.data;
    const table = lut(this.palette);
    const [dmin, dmax] = this.domain;
    const span = dmax - dmin || 1;
    const g = this.grid;
    const p = this.proj;

    for (let y = 0; y < bh; y++) {
      const py = (y + 0.5) * scaleDown;
      const lat = invMercY(p.y0 - (py - p.offY) / p.scale);
      const fy = (lat - g.lat0) / g.dlat;
      const inRow = fy >= 0 && fy <= g.ny - 1;
      const i = Math.min(g.ny - 2, Math.max(0, Math.floor(fy)));
      const ty = fy - i;
      for (let x = 0; x < bw; x++) {
        const o = (y * bw + x) * 4;
        if (!inRow) { px[o + 3] = 0; continue; }
        const pxr = (x + 0.5) * scaleDown;
        const lon = ((pxr - p.offX) / p.scale + p.x0) / D2R;
        const fx = (lon - g.lon0) / g.dlon;
        if (fx < 0 || fx > g.nx - 1) { px[o + 3] = 0; continue; }
        const j = Math.min(g.nx - 2, Math.floor(fx));
        const tx = fx - j;
        const a = frame[i * g.nx + j];
        const b = frame[i * g.nx + j + 1];
        const c = frame[(i + 1) * g.nx + j];
        const d = frame[(i + 1) * g.nx + j + 1];
        const v = (a * (1 - tx) + b * tx) * (1 - ty) + (c * (1 - tx) + d * tx) * ty;
        let t = (v - dmin) / span;
        t = t < 0 ? 0 : t > 1 ? 1 : t;
        const k = (t * 255) | 0;
        const o2 = k * 4;
        px[o] = table[o2];
        px[o + 1] = table[o2 + 1];
        px[o + 2] = table[o2 + 2];
        px[o + 3] = table[o2 + 3];
      }
    }
    this.bufCtx.putImageData(img, 0, 0);
    ctx.imageSmoothingEnabled = true;
    ctx.imageSmoothingQuality = 'high';
    ctx.drawImage(this.buf, 0, 0, bw, bh, 0, 0, this.W, this.H);
  }

  pathShapes(ctx, shapes) {
    for (const shape of shapes) {
      ctx.moveTo(...this.project(shape[0][0], shape[0][1]));
      for (let i = 1; i < shape.length; i++) {
        const [x, y] = this.project(shape[i][0], shape[i][1]);
        ctx.lineTo(x, y);
      }
      ctx.closePath();
    }
  }

  renderVectors() {
    const ctx = this.vecCtx;
    const dpr = this.dpr;
    ctx.clearRect(0, 0, this.W, this.H);

    // 1. Assombrissement hors territoire français.
    ctx.save();
    ctx.beginPath();
    ctx.rect(0, 0, this.W, this.H);
    this.pathShapes(ctx, this.geo.france);
    ctx.fillStyle = 'rgba(5, 9, 20, 0.78)';
    ctx.fill('evenodd');
    ctx.restore();

    // 2. Contours des pays voisins.
    ctx.save();
    ctx.beginPath();
    this.pathShapes(ctx, this.geo.neighbours);
    ctx.strokeStyle = 'rgba(150, 175, 220, 0.22)';
    ctx.lineWidth = 1 * dpr;
    ctx.stroke();
    ctx.restore();

    // 3. Graticule.
    if (this.layers.grid) {
      ctx.save();
      ctx.strokeStyle = 'rgba(255,255,255,0.07)';
      ctx.lineWidth = 1 * dpr;
      ctx.beginPath();
      const b = this.geo.bbox;
      for (let lon = Math.ceil(b.lon_min / 2) * 2; lon <= b.lon_max; lon += 2) {
        const [x1, y1] = this.project(lon, b.lat_min);
        const [x2, y2] = this.project(lon, b.lat_max);
        ctx.moveTo(x1, y1); ctx.lineTo(x2, y2);
      }
      for (let lat = Math.ceil(b.lat_min / 2) * 2; lat <= b.lat_max; lat += 2) {
        const [x1, y1] = this.project(b.lon_min, lat);
        const [x2, y2] = this.project(b.lon_max, lat);
        ctx.moveTo(x1, y1); ctx.lineTo(x2, y2);
      }
      ctx.stroke();
      ctx.restore();
    }

    // 4. Isobares.
    if (this.layers.isobars && this.overlay) this.renderIsobars(ctx);

    // 5. Frontière française.
    ctx.save();
    ctx.beginPath();
    this.pathShapes(ctx, this.geo.france);
    ctx.shadowColor = 'rgba(120, 200, 255, 0.85)';
    ctx.shadowBlur = 14 * dpr;
    ctx.strokeStyle = 'rgba(214, 236, 255, 0.92)';
    ctx.lineWidth = 1.7 * dpr;
    ctx.lineJoin = 'round';
    ctx.stroke();
    ctx.restore();

    // 6. Villes.
    if (this.layers.cities) this.renderCities(ctx);

    // 7. Curseur.
    if (this.hover) {
      const [x, y] = this.project(this.hover.lon, this.hover.lat);
      ctx.save();
      ctx.strokeStyle = 'rgba(255,255,255,0.75)';
      ctx.lineWidth = 1.2 * dpr;
      ctx.beginPath();
      ctx.arc(x, y, 7 * dpr, 0, Math.PI * 2);
      ctx.moveTo(x - 13 * dpr, y); ctx.lineTo(x - 9 * dpr, y);
      ctx.moveTo(x + 9 * dpr, y); ctx.lineTo(x + 13 * dpr, y);
      ctx.moveTo(x, y - 13 * dpr); ctx.lineTo(x, y - 9 * dpr);
      ctx.moveTo(x, y + 9 * dpr); ctx.lineTo(x, y + 13 * dpr);
      ctx.stroke();
      ctx.restore();
    }
  }

  renderIsobars(ctx) {
    const frame = this.frameAt(this.overlay, this.step);
    if (!frame) return;
    const g = this.grid;
    let lo = Infinity;
    let hi = -Infinity;
    for (let i = 0; i < frame.length; i++) {
      if (frame[i] < lo) lo = frame[i];
      if (frame[i] > hi) hi = frame[i];
    }
    const dpr = this.dpr;
    ctx.save();
    ctx.lineWidth = 1.05 * dpr;
    ctx.font = `600 ${10 * dpr}px var(--mono, monospace)`;
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';

    const start = Math.ceil(lo / 4) * 4;
    for (let level = start; level <= hi; level += 4) {
      const segs = this.marchingSquares(frame, g.ny, g.nx, level);
      if (!segs.length) continue;
      const major = level % 20 === 0;
      ctx.strokeStyle = level < 1013
        ? `rgba(140, 190, 255, ${major ? 0.72 : 0.38})`
        : `rgba(255, 208, 150, ${major ? 0.72 : 0.38})`;
      ctx.beginPath();
      for (const s of segs) {
        ctx.moveTo(s[0], s[1]);
        ctx.lineTo(s[2], s[3]);
      }
      ctx.stroke();

      if (major && segs.length > 12) {
        const s = segs[Math.floor(segs.length / 2)];
        ctx.fillStyle = 'rgba(6, 10, 22, 0.9)';
        ctx.beginPath();
        ctx.roundRect(s[0] - 15 * dpr, s[1] - 8 * dpr, 30 * dpr, 16 * dpr, 5 * dpr);
        ctx.fill();
        ctx.fillStyle = level < 1013 ? 'rgba(170,205,255,0.95)' : 'rgba(255,214,160,0.95)';
        ctx.fillText(String(level), s[0], s[1]);
      }
    }
    ctx.restore();
  }

  marchingSquares(data, ny, nx, level) {
    const g = this.grid;
    const segs = [];
    const pt = (edge, i, j, v00, v01, v10, v11) => {
      let x;
      let y;
      if (edge === 'T') { x = j + (level - v00) / (v01 - v00 || 1e-9); y = i; }
      else if (edge === 'R') { x = j + 1; y = i + (level - v01) / (v11 - v01 || 1e-9); }
      else if (edge === 'B') { x = j + (level - v10) / (v11 - v10 || 1e-9); y = i + 1; }
      else { x = j; y = i + (level - v00) / (v10 - v00 || 1e-9); }
      return this.project(g.lon0 + x * g.dlon, g.lat0 + y * g.dlat);
    };
    for (let i = 0; i < ny - 1; i++) {
      for (let j = 0; j < nx - 1; j++) {
        const v00 = data[i * nx + j];
        const v01 = data[i * nx + j + 1];
        const v10 = data[(i + 1) * nx + j];
        const v11 = data[(i + 1) * nx + j + 1];
        let idx = 0;
        if (v00 > level) idx |= 8;
        if (v01 > level) idx |= 4;
        if (v11 > level) idx |= 2;
        if (v10 > level) idx |= 1;
        const cases = MS_TABLE[idx];
        if (!cases) continue;
        for (const [e1, e2] of cases) {
          const p1 = pt(e1, i, j, v00, v01, v10, v11);
          const p2 = pt(e2, i, j, v00, v01, v10, v11);
          segs.push([p1[0], p1[1], p2[0], p2[1]]);
        }
      }
    }
    return segs;
  }

  renderCities(ctx) {
    const dpr = this.dpr;
    const frame = this.frameAt(this.frames, this.step);
    ctx.save();
    ctx.font = `600 ${11 * dpr}px var(--sans, sans-serif)`;
    ctx.textBaseline = 'middle';
    for (const city of this.cities) {
      const isSel = this.selected && this.selected.name === city.name;
      if (!city.major && !isSel) continue;
      const [x, y] = this.project(city.lon, city.lat);
      const value = frame ? this.sample(frame, city.lat, city.lon) : NaN;

      ctx.beginPath();
      ctx.arc(x, y, (isSel ? 5 : 3.2) * dpr, 0, Math.PI * 2);
      ctx.fillStyle = isSel ? '#4cc9ff' : 'rgba(255,255,255,0.92)';
      ctx.shadowColor = isSel ? 'rgba(76,201,255,0.9)' : 'rgba(0,0,0,0.9)';
      ctx.shadowBlur = 8 * dpr;
      ctx.fill();
      ctx.shadowBlur = 0;

      const label = Number.isFinite(value)
        ? `${city.name}  ${this.formatValue(value)}`
        : city.name;
      ctx.textAlign = 'left';
      const tx = x + 8 * dpr;
      const tw = ctx.measureText(label).width;
      ctx.fillStyle = 'rgba(5, 9, 20, 0.72)';
      ctx.beginPath();
      ctx.roundRect(tx - 4 * dpr, y - 9 * dpr, tw + 8 * dpr, 18 * dpr, 5 * dpr);
      ctx.fill();
      ctx.fillStyle = isSel ? '#a7e2ff' : 'rgba(255,255,255,0.94)';
      ctx.fillText(label, tx, y + 0.5 * dpr);
    }
    ctx.restore();
  }

  formatValue(v) {
    const d = this.valueDecimals ?? 0;
    return `${v.toFixed(d)}${this.valueUnit ?? ''}`;
  }

  /* -- particules de vent -------------------------------------------------- */

  seedParticles() {
    if (!this.proj) return;
    const target = Math.round(Math.min(3200, (this.W * this.H) / 2600));
    this.particles = [];
    for (let i = 0; i < target; i++) this.particles.push(this.spawn());
  }

  spawn() {
    return {
      x: Math.random() * this.W,
      y: Math.random() * this.H,
      age: Math.random() * 90,
      max: 60 + Math.random() * 70,
    };
  }

  startAnimation() {
    if (this.raf) return;
    const loop = (ts) => {
      this.raf = requestAnimationFrame(loop);
      const dt = Math.min(50, ts - this.lastFrame);
      this.lastFrame = ts;
      if (this.layers.wind) this.stepParticles(dt);
    };
    this.raf = requestAnimationFrame(loop);
  }

  stopAnimation() {
    if (this.raf) cancelAnimationFrame(this.raf);
    this.raf = null;
  }

  stepParticles() {
    const ctx = this.windCtx;
    if (!this.windU || !this.windV || !this.proj) return;
    const u = this.frameAt(this.windU, this.step);
    const v = this.frameAt(this.windV, this.step);
    if (!u || !v) return;

    ctx.globalCompositeOperation = 'destination-out';
    ctx.fillStyle = 'rgba(0,0,0,0.085)';
    ctx.fillRect(0, 0, this.W, this.H);
    ctx.globalCompositeOperation = 'source-over';
    ctx.lineWidth = 1.25 * this.dpr;
    ctx.lineCap = 'round';

    const secs = 1500; // pas d'advection (s) par image
    const table = lut('particles');
    for (const p of this.particles) {
      p.age += 1;
      if (p.age > p.max) { Object.assign(p, this.spawn()); continue; }
      const [lon, lat] = this.unproject(p.x, p.y);
      const uu = this.sample(u, lat, lon);
      const vv = this.sample(v, lat, lon);
      if (!Number.isFinite(uu) || !Number.isFinite(vv)) { Object.assign(p, this.spawn()); continue; }
      const dlon = (uu * secs) / (111320 * Math.cos(lat * D2R));
      const dlat = (vv * secs) / 111320;
      const [nx2, ny2] = this.project(lon + dlon, lat + dlat);
      if (nx2 < -20 || ny2 < -20 || nx2 > this.W + 20 || ny2 > this.H + 20) {
        Object.assign(p, this.spawn());
        continue;
      }
      const speed = Math.hypot(uu, vv);
      const k = Math.min(255, Math.round((speed / 26) * 255)) * 4;
      const alpha = (table[k + 3] / 255) * (0.42 + Math.min(0.5, speed / 22));
      ctx.strokeStyle = `rgba(${table[k]},${table[k + 1]},${table[k + 2]},${alpha})`;
      ctx.beginPath();
      ctx.moveTo(p.x, p.y);
      ctx.lineTo(nx2, ny2);
      ctx.stroke();
      p.x = nx2;
      p.y = ny2;
    }
  }

  /* -- interactions -------------------------------------------------------- */

  pointerToGeo(evt) {
    const rect = this.vecCanvas.getBoundingClientRect();
    const px = (evt.clientX - rect.left) * this.dpr;
    const py = (evt.clientY - rect.top) * this.dpr;
    const [lon, lat] = this.unproject(px, py);
    return { lon, lat, px: evt.clientX - rect.left, py: evt.clientY - rect.top };
  }

  nearestCity(lat, lon, maxDeg = 0.55) {
    let best = null;
    let bestD = Infinity;
    for (const c of this.cities) {
      const d = Math.hypot(c.lat - lat, (c.lon - lon) * Math.cos(lat * D2R));
      if (d < bestD) { bestD = d; best = c; }
    }
    return bestD <= maxDeg ? best : null;
  }

  destroy() {
    this.stopAnimation();
    window.removeEventListener('resize', this._onResize);
  }
}

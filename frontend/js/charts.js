/* Météogrammes et micro-graphiques rendus sur canvas. */

import { colorAt } from './colormaps.js';

function setup(canvas) {
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  const w = canvas.clientWidth || canvas.parentElement.clientWidth;
  const h = canvas.clientHeight || 160;
  canvas.width = Math.round(w * dpr);
  canvas.height = Math.round(h * dpr);
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  return { ctx, w, h };
}

const HOUR_FMT = new Intl.DateTimeFormat('fr-FR', {
  hour: '2-digit', timeZone: 'Europe/Paris', hour12: false,
});
const DAY_FMT = new Intl.DateTimeFormat('fr-FR', {
  weekday: 'short', timeZone: 'Europe/Paris',
});

/**
 * Météogramme : température (courbe + enveloppe), précipitations (barres),
 * vent (ligne pointillée) et curseur temporel.
 */
export function drawMeteogram(canvas, opts) {
  const { ctx, w, h } = setup(canvas);
  const { valid = [], series = {}, cursor = 0 } = opts;
  const n = valid.length;
  if (!n) return;

  const padL = 30;
  const padR = 28;
  const padT = 14;
  const padB = 20;
  const plotW = w - padL - padR;
  const plotH = h - padT - padB;
  const rainH = plotH * 0.3;
  const tempH = plotH - rainH - 8;

  const temps = series['2t'] || [];
  const lo = series['2t_lo'];
  const hi = series['2t_hi'];
  const rain = series.precip || [];
  const wind = series.wind10 || [];

  const tMin = Math.min(...(lo || temps), ...temps);
  const tMax = Math.max(...(hi || temps), ...temps);
  const pad = Math.max(1.2, (tMax - tMin) * 0.18);
  const y0 = tMin - pad;
  const y1 = tMax + pad;

  const X = (i) => padL + (n === 1 ? plotW / 2 : (i / (n - 1)) * plotW);
  const Y = (v) => padT + tempH - ((v - y0) / (y1 - y0 || 1)) * tempH;

  // --- séparateurs journaliers
  ctx.save();
  let prevDay = null;
  ctx.font = '600 9px system-ui, sans-serif';
  for (let i = 0; i < n; i++) {
    const d = new Date(valid[i]);
    const day = DAY_FMT.format(d);
    const hour = Number(HOUR_FMT.format(d));
    if (day !== prevDay) {
      ctx.strokeStyle = 'rgba(255,255,255,0.14)';
      ctx.setLineDash([2, 3]);
      ctx.beginPath();
      ctx.moveTo(X(i), padT - 4);
      ctx.lineTo(X(i), h - padB + 4);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = 'rgba(200,212,235,0.75)';
      ctx.textAlign = 'left';
      ctx.fillText(day, X(i) + 3, h - padB + 12);
      prevDay = day;
    } else if (hour === 12) {
      ctx.fillStyle = 'rgba(140,152,178,0.55)';
      ctx.textAlign = 'center';
      ctx.fillText('12h', X(i), h - padB + 12);
    }
  }
  ctx.restore();

  // --- graduations température
  ctx.save();
  ctx.font = '9px system-ui, sans-serif';
  ctx.fillStyle = 'rgba(150,162,190,0.75)';
  ctx.strokeStyle = 'rgba(255,255,255,0.06)';
  const stepT = niceStep(y1 - y0);
  for (let v = Math.ceil(y0 / stepT) * stepT; v <= y1; v += stepT) {
    const y = Y(v);
    ctx.beginPath();
    ctx.moveTo(padL, y);
    ctx.lineTo(w - padR, y);
    ctx.stroke();
    ctx.textAlign = 'right';
    ctx.fillText(`${Math.round(v)}°`, padL - 5, y + 3);
  }
  ctx.restore();

  // --- enveloppe d'incertitude
  if (lo && hi) {
    ctx.save();
    ctx.beginPath();
    ctx.moveTo(X(0), Y(hi[0]));
    for (let i = 1; i < n; i++) ctx.lineTo(X(i), Y(hi[i]));
    for (let i = n - 1; i >= 0; i--) ctx.lineTo(X(i), Y(lo[i]));
    ctx.closePath();
    ctx.fillStyle = 'rgba(76,201,255,0.16)';
    ctx.fill();
    ctx.restore();
  }

  // --- précipitations
  if (rain.length) {
    const rMax = Math.max(1.5, ...rain);
    const bw = Math.max(2, plotW / n - 1.5);
    ctx.save();
    for (let i = 0; i < n; i++) {
      if (rain[i] <= 0.02) continue;
      const bh = (rain[i] / rMax) * rainH;
      const grad = ctx.createLinearGradient(0, h - padB - bh, 0, h - padB);
      grad.addColorStop(0, 'rgba(90,190,255,0.95)');
      grad.addColorStop(1, 'rgba(50,110,220,0.35)');
      ctx.fillStyle = grad;
      ctx.beginPath();
      ctx.roundRect(X(i) - bw / 2, h - padB - bh, bw, bh, 2);
      ctx.fill();
    }
    ctx.fillStyle = 'rgba(120,190,255,0.8)';
    ctx.font = '9px system-ui, sans-serif';
    ctx.textAlign = 'right';
    ctx.fillText(`${rMax.toFixed(1)} mm/h`, w - 3, h - padB - rainH - 1);
    ctx.restore();
  }

  // --- vent
  if (wind.length) {
    const wMax = Math.max(30, ...wind);
    ctx.save();
    ctx.strokeStyle = 'rgba(167,139,250,0.75)';
    ctx.lineWidth = 1.2;
    ctx.setLineDash([3, 3]);
    ctx.beginPath();
    for (let i = 0; i < n; i++) {
      const y = padT + tempH - (wind[i] / wMax) * tempH * 0.55;
      i === 0 ? ctx.moveTo(X(i), y) : ctx.lineTo(X(i), y);
    }
    ctx.stroke();
    ctx.restore();
  }

  // --- courbe de température
  if (temps.length) {
    ctx.save();
    const area = ctx.createLinearGradient(0, padT, 0, padT + tempH);
    area.addColorStop(0, 'rgba(255,140,90,0.28)');
    area.addColorStop(1, 'rgba(76,201,255,0.03)');
    ctx.beginPath();
    ctx.moveTo(X(0), padT + tempH);
    for (let i = 0; i < n; i++) ctx.lineTo(X(i), Y(temps[i]));
    ctx.lineTo(X(n - 1), padT + tempH);
    ctx.closePath();
    ctx.fillStyle = area;
    ctx.fill();

    const line = ctx.createLinearGradient(padL, 0, w - padR, 0);
    line.addColorStop(0, '#4cc9ff');
    line.addColorStop(0.5, '#a78bfa');
    line.addColorStop(1, '#ff79c6');
    ctx.beginPath();
    for (let i = 0; i < n; i++) (i === 0 ? ctx.moveTo : ctx.lineTo).call(ctx, X(i), Y(temps[i]));
    ctx.strokeStyle = line;
    ctx.lineWidth = 2;
    ctx.lineJoin = 'round';
    ctx.shadowColor = 'rgba(120,180,255,0.55)';
    ctx.shadowBlur = 8;
    ctx.stroke();
    ctx.restore();
  }

  // --- curseur
  const ci = Math.max(0, Math.min(n - 1, Math.round(cursor)));
  ctx.save();
  ctx.strokeStyle = 'rgba(255,255,255,0.55)';
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(X(ci), padT - 6);
  ctx.lineTo(X(ci), h - padB);
  ctx.stroke();
  if (temps[ci] !== undefined) {
    ctx.beginPath();
    ctx.arc(X(ci), Y(temps[ci]), 3.6, 0, Math.PI * 2);
    ctx.fillStyle = '#fff';
    ctx.shadowColor = 'rgba(255,255,255,0.9)';
    ctx.shadowBlur = 8;
    ctx.fill();
  }
  ctx.restore();
}

function niceStep(span) {
  const raw = span / 4;
  const pow = 10 ** Math.floor(Math.log10(Math.max(raw, 1e-6)));
  const norm = raw / pow;
  const mult = norm < 1.5 ? 1 : norm < 3 ? 2 : norm < 7 ? 5 : 10;
  return mult * pow;
}

/** Petit histogramme horizontal coloré (classements). */
export function rankBar(value, domain, palette) {
  const t = (value - domain[0]) / (domain[1] - domain[0] || 1);
  return {
    width: `${Math.max(2, Math.min(100, t * 100)).toFixed(1)}%`,
    color: colorAt(palette, Math.max(0, Math.min(1, t))),
  };
}

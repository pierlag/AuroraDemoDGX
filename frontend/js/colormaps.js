/* Palettes météorologiques et tables de correspondance (LUT 256 entrées). */

const PALETTES = {
  temperature: [
    [0.00, [38, 10, 66, 255]],
    [0.07, [56, 30, 140, 255]],
    [0.15, [40, 86, 200, 255]],
    [0.24, [44, 146, 226, 255]],
    [0.33, [56, 202, 216, 255]],
    [0.42, [82, 220, 168, 255]],
    [0.50, [150, 226, 108, 255]],
    [0.58, [232, 226, 86, 255]],
    [0.66, [247, 182, 56, 255]],
    [0.75, [240, 118, 46, 255]],
    [0.85, [216, 52, 38, 255]],
    [0.93, [166, 24, 92, 255]],
    [1.00, [240, 118, 226, 255]],
  ],
  temperature850: [
    [0.00, [26, 4, 58, 255]],
    [0.12, [46, 34, 150, 255]],
    [0.26, [40, 122, 214, 255]],
    [0.40, [72, 206, 214, 255]],
    [0.50, [214, 232, 238, 255]],
    [0.60, [226, 206, 96, 255]],
    [0.74, [238, 138, 48, 255]],
    [0.88, [210, 46, 44, 255]],
    [1.00, [128, 12, 74, 255]],
  ],
  wind: [
    [0.00, [12, 22, 54, 220]],
    [0.10, [22, 62, 116, 235]],
    [0.24, [28, 130, 186, 245]],
    [0.38, [48, 208, 200, 255]],
    [0.52, [140, 240, 122, 255]],
    [0.65, [246, 224, 94, 255]],
    [0.78, [247, 156, 55, 255]],
    [0.90, [232, 69, 42, 255]],
    [1.00, [255, 122, 217, 255]],
  ],
  pressure: [
    [0.00, [58, 16, 120, 255]],
    [0.18, [36, 84, 190, 255]],
    [0.36, [96, 186, 224, 255]],
    [0.48, [214, 236, 250, 255]],
    [0.56, [250, 244, 214, 255]],
    [0.72, [244, 172, 84, 255]],
    [0.88, [222, 92, 44, 255]],
    [1.00, [150, 22, 46, 255]],
  ],
  precip: [
    [0.000, [10, 18, 38, 0]],
    [0.012, [26, 62, 110, 90]],
    [0.060, [30, 110, 196, 190]],
    [0.150, [38, 178, 226, 235]],
    [0.290, [56, 224, 152, 250]],
    [0.450, [236, 226, 76, 255]],
    [0.620, [244, 140, 46, 255]],
    [0.800, [226, 46, 62, 255]],
    [1.000, [216, 32, 168, 255]],
  ],
  cloud: [
    [0.00, [12, 20, 42, 0]],
    [0.18, [104, 126, 166, 60]],
    [0.42, [166, 186, 218, 140]],
    [0.70, [212, 226, 246, 205]],
    [1.00, [255, 255, 255, 240]],
  ],
  humidity: [
    [0.00, [108, 72, 28, 255]],
    [0.20, [176, 150, 66, 255]],
    [0.38, [206, 202, 108, 255]],
    [0.52, [126, 200, 106, 255]],
    [0.70, [52, 184, 192, 255]],
    [0.86, [40, 106, 208, 255]],
    [1.00, [28, 44, 158, 255]],
  ],
  geopotential: [
    [0.00, [46, 12, 92, 255]],
    [0.16, [38, 66, 178, 255]],
    [0.34, [40, 154, 214, 255]],
    [0.50, [72, 208, 168, 255]],
    [0.64, [206, 224, 92, 255]],
    [0.78, [242, 158, 56, 255]],
    [0.90, [222, 66, 48, 255]],
    [1.00, [138, 16, 76, 255]],
  ],
  // Palette dédiée aux traînées de particules : toujours lumineuse afin de
  // rester lisible au-dessus d'un champ coloré.
  particles: [
    [0.00, [228, 240, 255, 150]],
    [0.22, [176, 236, 255, 190]],
    [0.45, [255, 255, 224, 215]],
    [0.68, [255, 208, 130, 235]],
    [0.85, [255, 150, 110, 245]],
    [1.00, [255, 140, 210, 255]],
  ],
};

const LUT_CACHE = new Map();

/** Construit une table de 256 couleurs RGBA pour une palette. */
export function lut(name) {
  if (LUT_CACHE.has(name)) return LUT_CACHE.get(name);
  const stops = PALETTES[name] || PALETTES.temperature;
  const table = new Uint8ClampedArray(256 * 4);
  let k = 0;
  for (let i = 0; i < 256; i++) {
    const t = i / 255;
    while (k < stops.length - 2 && t > stops[k + 1][0]) k++;
    const [p0, c0] = stops[k];
    const [p1, c1] = stops[Math.min(k + 1, stops.length - 1)];
    const f = p1 === p0 ? 0 : (t - p0) / (p1 - p0);
    const g = Math.max(0, Math.min(1, f));
    table[i * 4] = c0[0] + (c1[0] - c0[0]) * g;
    table[i * 4 + 1] = c0[1] + (c1[1] - c0[1]) * g;
    table[i * 4 + 2] = c0[2] + (c1[2] - c0[2]) * g;
    table[i * 4 + 3] = c0[3] + (c1[3] - c0[3]) * g;
  }
  LUT_CACHE.set(name, table);
  return table;
}

/** Couleur CSS correspondant à une valeur normalisée [0,1]. */
export function colorAt(name, t) {
  const table = lut(name);
  const i = Math.max(0, Math.min(255, Math.round(t * 255))) * 4;
  return `rgba(${table[i]},${table[i + 1]},${table[i + 2]},${table[i + 3] / 255})`;
}

/** Dégradé CSS linéaire pour les légendes. */
export function gradientCss(name) {
  const stops = PALETTES[name] || PALETTES.temperature;
  const parts = stops.map(([p, c]) =>
    `rgba(${c[0]},${c[1]},${c[2]},${c[3] / 255}) ${(p * 100).toFixed(1)}%`);
  return `linear-gradient(90deg, ${parts.join(', ')})`;
}

/** Libellé qualitatif du temps sensible. */
export function conditionLabel(values) {
  const precip = values.precip ?? 0;
  const cloud = values.tcc ?? null;
  const temp = values['2t'] ?? 10;
  const wind = values.wind10 ?? 0;
  if (precip > 4) return temp < 1 ? 'Neige marquée' : 'Fortes précipitations';
  if (precip > 1.2) return temp < 1 ? 'Neige' : 'Pluie';
  if (precip > 0.15) return temp < 1 ? 'Neige faible' : 'Pluie faible';
  if (wind > 75) return 'Vent tempétueux';
  if (cloud !== null) {
    if (cloud > 82) return 'Ciel couvert';
    if (cloud > 55) return 'Très nuageux';
    if (cloud > 28) return 'Éclaircies';
    return 'Ciel dégagé';
  }
  return wind > 45 ? 'Venteux' : 'Temps calme';
}

export function conditionIcon(values) {
  const precip = values.precip ?? 0;
  const cloud = values.tcc ?? 40;
  const temp = values['2t'] ?? 10;
  if (precip > 1.2) return temp < 1 ? '❄' : '🌧';
  if (precip > 0.15) return temp < 1 ? '🌨' : '🌦';
  if (cloud > 80) return '☁';
  if (cloud > 45) return '⛅';
  return '☀';
}

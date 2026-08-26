/* Orchestration de la vue prévision. */

import { API, decodeAll, fmt, subscribe, toast } from './api.js';
import { conditionIcon, conditionLabel, gradientCss } from './colormaps.js';
import { drawMeteogram, rankBar } from './charts.js';
import { MapView } from './mapview.js';

const $ = (id) => document.getElementById(id);

const state = {
  boot: null,
  variables: {},
  modelState: null,
  forecast: null,
  fields: new Map(),
  wind: null,
  variable: '2t',
  step: 0,
  playing: false,
  speed: 1.2,
  selection: null,
  jobId: null,
  loadingVar: false,
};

let map;

/* ========================================================================== */
/* Démarrage                                                                  */
/* ========================================================================== */

async function boot() {
  map = new MapView($('canvas-wrap'));
  window.__aurora = { state, map };
  bindUi();

  const data = await API.get('/bootstrap');
  state.boot = data;
  state.variables = data.variables;

  map.setGeo(data.geo);
  map.setCities(data.cities);
  map.startAnimation();

  buildVariableSwitch();
  buildSources(data.sources);
  applyModelState(data.model_state);

  // Le champ est libellé UTC : on y écrit directement l'heure UTC, sans décalage.
  $('base-time').value = new Date(`${data.default_base_time}Z`).toISOString().slice(0, 16);
  applyDateBounds();

  renderHistory(data.forecasts, data.storage);
  if (data.forecasts.length) loadForecast(data.forecasts[0].id);

  subscribe({
    model_state: applyModelState,
    job: onJob,
    log: (d) => {
      if (d.level === 'error') toast(d.message, 'err');
    },
  });
}

/* ========================================================================== */
/* Interface                                                                  */
/* ========================================================================== */

function bindUi() {
  $('steps').addEventListener('input', (e) => {
    $('steps-label').textContent = `${e.target.value * 6} h`;
  });
  $('members').addEventListener('input', (e) => {
    $('members-label').textContent = e.target.value;
  });
  $('run-btn').addEventListener('click', runForecast);
  $('src').addEventListener('change', updateSourceHint);
  $('history-clear').addEventListener('click', clearHistory);

  $('play').addEventListener('click', togglePlay);
  $('speed').addEventListener('change', (e) => { state.speed = Number(e.target.value); });
  $('tl-range').addEventListener('input', (e) => {
    const value = Number(e.target.value);
    stopPlay();
    setStep(value);
  });

  const layers = [
    ['lay-wind', 't-wind', 'wind'],
    ['lay-iso', 't-iso', 'isobars'],
    ['lay-cities', 't-cities', 'cities'],
    ['lay-grid', null, 'grid'],
  ];
  for (const [checkId, btnId, key] of layers) {
    const check = $(checkId);
    const btn = btnId ? $(btnId) : null;
    const apply = (on) => {
      check.checked = on;
      btn?.classList.toggle('active', on);
      map.setLayers({ [key]: on });
    };
    check.addEventListener('change', () => apply(check.checked));
    btn?.addEventListener('click', () => apply(!check.checked));
  }

  const wrap = $('canvas-wrap');
  wrap.addEventListener('mousemove', onHover);
  wrap.addEventListener('mouseleave', () => {
    map.hover = null;
    $('readout').classList.remove('show');
    map.renderVectors();
  });
  wrap.addEventListener('click', onMapClick);

  window.addEventListener('keydown', (e) => {
    if (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT') return;
    if (e.code === 'Space') { e.preventDefault(); togglePlay(); }
    if (e.code === 'ArrowRight') { stopPlay(); setStep(Math.min(maxStep(), state.step + 1)); }
    if (e.code === 'ArrowLeft') { stopPlay(); setStep(Math.max(0, state.step - 1)); }
  });

  window.addEventListener('resize', () => {
    if (state.selection) renderCity();
  });
}

function buildVariableSwitch() {
  const nav = $('var-switch');
  nav.innerHTML = '';
  for (const [key, meta] of Object.entries(state.variables)) {
    const btn = document.createElement('button');
    btn.className = 'var-btn';
    btn.dataset.var = key;
    btn.textContent = meta.short;
    btn.title = meta.label;
    btn.addEventListener('click', () => setVariable(key));
    nav.appendChild(btn);
  }
  syncVariableSwitch();
}

function syncVariableSwitch() {
  const available = state.forecast ? Object.keys(state.forecast.meta.variables) : [];
  for (const btn of $('var-switch').children) {
    const ok = !state.forecast || available.includes(btn.dataset.var);
    btn.disabled = !ok;
    btn.classList.toggle('active', btn.dataset.var === state.variable);
  }
}

function buildSources(sources) {
  const sel = $('src');
  sel.innerHTML = '';
  for (const s of sources) {
    const opt = document.createElement('option');
    opt.value = s.id;
    opt.textContent = s.available ? s.name : `${s.name} (indisponible)`;
    opt.disabled = !s.available;
    opt.dataset.desc = s.description;
    opt.dataset.reason = s.reason || '';
    sel.appendChild(opt);
  }
  updateSourceHint();
}

function updateSourceHint() {
  const opt = $('src').selectedOptions[0];
  if (!opt) return;
  $('src-hint').textContent = opt.dataset.reason
    ? `${opt.dataset.desc} — ${opt.dataset.reason}`
    : opt.dataset.desc;
  applyDateBounds();
}

/**
 * ERA5 est une réanalyse : les champs ne sont publiés qu'avec environ cinq jours
 * de décalage, et seuls les réseaux synoptiques (00/06/12/18 UTC) existent.
 */
function applyDateBounds() {
  const input = $('base-time');
  const hint = $('date-hint');
  if ($('src').value !== 'era5_cds') {
    input.removeAttribute('max');
    hint.textContent = 'Réseaux synoptiques : 00, 06, 12 ou 18 h UTC.';
    return;
  }
  const limit = new Date(Date.now() - 6 * 86400000);
  limit.setUTCHours(limit.getUTCHours() - (limit.getUTCHours() % 6), 0, 0, 0);
  const iso = limit.toISOString().slice(0, 16);
  input.max = iso;
  if (!input.value || input.value > iso) input.value = iso;
  hint.textContent = `ERA5 accuse ~5 jours de délai : échéance la plus récente ${iso.replace('T', ' à ')} UTC.`;
}

function applyModelState(s) {
  state.modelState = s;
  const pill = $('model-pill');
  const label = pill.querySelector('span');
  const map_ = {
    ready: ['ok', `${s.model_name} · ${s.device}`],
    busy: ['info', 'Inférence en cours…'],
    loading: ['info', `Chargement · ${Math.round((s.progress || 0) * 100)} %`],
    downloading: ['info', `Téléchargement · ${Math.round((s.progress || 0) * 100)} %`],
    checking: ['info', 'Vérification…'],
    unloading: ['warn', 'Déchargement…'],
    error: ['err', 'Erreur de chargement'],
    idle: ['muted', 'Aucun modèle chargé'],
  };
  const [cls, text] = map_[s.state] || ['muted', s.state];
  pill.className = `pill ${cls}${['loading', 'downloading', 'busy', 'checking'].includes(s.state) ? ' pulse' : ''}`;
  label.textContent = text;
  $('run-btn').disabled = !(s.state === 'ready');
  if (s.state !== 'ready' && s.state !== 'busy') $('run-btn').title = 'Chargez un modèle depuis la console d’administration';
  else $('run-btn').title = '';
}

/* ========================================================================== */
/* Cycle de prévision                                                         */
/* ========================================================================== */

async function runForecast() {
  const value = $('base-time').value;
  // Le champ exprime une heure UTC : on l'interprète comme telle.
  const baseIso = value ? new Date(`${value}:00Z`).toISOString() : null;
  try {
    $('run-btn').disabled = true;
    const job = await API.post('/forecast', {
      source: $('src').value,
      base_time: baseIso,
      steps: Number($('steps').value),
      members: Number($('members').value),
    });
    state.jobId = job.id;
    onJob(job);
  } catch (err) {
    toast(err.message, 'err');
    $('run-btn').disabled = false;
  }
}

function onJob(job) {
  if (state.jobId && job.id !== state.jobId) return;
  state.jobId = job.id;
  const box = $('job-box');
  box.hidden = false;
  $('job-msg').textContent = job.message || job.status;
  $('job-bar').style.width = `${Math.round((job.progress || 0) * 100)}%`;

  if (job.status === 'done') {
    setTimeout(() => { box.hidden = true; }, 1600);
    state.jobId = null;
    $('run-btn').disabled = false;
    loadForecast(job.forecast_id);
    refreshHistory();
  } else if (job.status === 'error') {
    $('run-btn').disabled = false;
    state.jobId = null;
    toast(job.error || 'Échec de la prévision', 'err');
  }
}

async function loadForecast(id) {
  try {
    const data = await API.get(`/forecasts/${id}`);
    state.forecast = data;
    state.fields.clear();
    state.wind = null;
    state.step = 0;

    const available = Object.keys(data.meta.variables);
    if (!available.includes(state.variable)) state.variable = available[0];

    $('stage-empty').hidden = true;
    renderMeta(data.meta);
    renderBadges(data.meta);
    buildTicks(data.meta);
    $('tl-range').max = String(data.meta.lead_hours.length - 1);
    $('tl-range').disabled = false;
    $('play').disabled = false;

    await Promise.all([ensureField(state.variable), ensureWind(), ensureField('msl')]);
    syncVariableSwitch();
    applyToMap();
    selectCity(state.selection?.name || 'Paris');
    refreshHistory();
  } catch (err) {
    toast(err.message, 'err');
  }
}

async function ensureField(name) {
  if (!state.forecast || state.fields.has(name)) return;
  if (!state.forecast.meta.variables[name] && name !== 'msl') return;
  const payload = await API.get(`/forecasts/${state.forecast.meta.id}/field/${name}`);
  state.fields.set(name, decodeAll(payload));
}

async function ensureWind() {
  if (!state.forecast || state.wind) return;
  try {
    const payload = await API.get(`/forecasts/${state.forecast.meta.id}/wind`);
    state.wind = { u: decodeAll(payload.u), v: decodeAll(payload.v) };
  } catch {
    state.wind = null;
  }
}

async function setVariable(name) {
  if (state.loadingVar || name === state.variable) return;
  state.loadingVar = true;
  state.variable = name;
  syncVariableSwitch();
  try {
    await ensureField(name);
    applyToMap();
    renderCity();
    renderRanking();
  } finally {
    state.loadingVar = false;
  }
}

function domainFor(name) {
  const meta = state.variables[name];
  const stats = state.forecast?.meta.variables[name]?.stats;
  if (!stats) return meta.domain;
  if (name === 'precip') return [0, Math.max(1.5, stats.max)];
  if (name === 'tcc') return [0, 100];
  if (name === 'rh') return [Math.max(0, Math.floor(stats.min / 5) * 5), 100];
  // Plage adaptée à l'ensemble de la séquence : contraste maximal, mais couleurs
  // stables d'une échéance à l'autre.
  const span = Math.max(stats.max - stats.min, 0.5);
  const pad = span * 0.06;
  return [stats.min - pad, stats.max + pad];
}

function applyToMap() {
  if (!state.forecast) return;
  const name = state.variable;
  const meta = state.variables[name];
  const frames = state.fields.get(name);
  if (!frames) return;

  map.valueDecimals = meta.decimals;
  map.valueUnit = meta.unit === '°C' ? '°' : '';
  map.setData({
    grid: state.forecast.meta.grid,
    frames,
    palette: meta.palette,
    domain: domainFor(name),
    windU: state.wind?.u ?? null,
    windV: state.wind?.v ?? null,
    overlay: state.fields.get('msl') ?? null,
  });
  map.setStep(state.step);

  $('map-var').textContent = meta.label;
  renderLegend(name);
  updateTimeLabels();
}

function renderLegend(name) {
  const meta = state.variables[name];
  const [a, b] = domainFor(name);
  $('legend').hidden = false;
  $('legend-label').textContent = meta.short;
  $('legend-unit').textContent = meta.unit;
  $('legend-bar').style.background = gradientCss(meta.palette);
  const ticks = $('legend-ticks');
  ticks.innerHTML = '';
  for (let i = 0; i <= 4; i++) {
    const v = a + ((b - a) * i) / 4;
    const el = document.createElement('span');
    el.textContent = fmt.num(v, meta.decimals);
    ticks.appendChild(el);
  }
}

/* ========================================================================== */
/* Temps                                                                      */
/* ========================================================================== */

const maxStep = () => (state.forecast ? state.forecast.meta.lead_hours.length - 1 : 0);

function setStep(step) {
  state.step = Math.max(0, Math.min(maxStep(), step));
  $('tl-range').value = String(state.step);
  map.setStep(state.step);
  updateTimeLabels();
  renderCity();
  renderRanking();
}

function updateTimeLabels() {
  if (!state.forecast) return;
  const meta = state.forecast.meta;
  const i = Math.round(state.step);
  $('tl-time').textContent = fmt.full(meta.valid_times[i]);
  const lead = meta.lead_hours[i];
  $('tl-lead').textContent = lead === 0 ? 'analyse' : `+${lead} h`;
  $('map-sub').textContent =
    `${meta.model_name} · réseau ${fmt.full(meta.base_time)} · ${meta.grid.nx}×${meta.grid.ny} pts`;
}

function buildTicks(meta) {
  const box = $('tl-ticks');
  box.innerHTML = '';
  const n = meta.valid_times.length;
  let prevDay = null;
  for (let i = 0; i < n; i++) {
    const day = fmt.day(meta.valid_times[i]);
    const hour = fmt.hourNum(meta.valid_times[i]);
    const isDay = day !== prevDay;
    if (!isDay && hour !== 12) { prevDay = day; continue; }
    prevDay = day;
    const el = document.createElement('div');
    el.className = `tick${isDay ? ' day' : ''}`;
    el.style.left = `${(i / Math.max(1, n - 1)) * 100}%`;
    el.innerHTML = `<i></i>${isDay ? day : '12h'}`;
    el.addEventListener('click', () => { stopPlay(); setStep(i); });
    el.style.pointerEvents = 'auto';
    el.style.cursor = 'pointer';
    box.appendChild(el);
  }
}

let playRaf = null;
let playLast = 0;

function togglePlay() {
  state.playing ? stopPlay() : startPlay();
}

function startPlay() {
  if (!state.forecast) return;
  state.playing = true;
  $('play').textContent = '❚❚';
  playLast = performance.now();
  const loop = (ts) => {
    if (!state.playing) return;
    playRaf = requestAnimationFrame(loop);
    const dt = (ts - playLast) / 1000;
    playLast = ts;
    let next = state.step + dt * state.speed;
    if (next > maxStep()) next = 0;
    setStep(next);
  };
  playRaf = requestAnimationFrame(loop);
}

function stopPlay() {
  state.playing = false;
  $('play').textContent = '▶';
  if (playRaf) cancelAnimationFrame(playRaf);
  playRaf = null;
  setStep(Math.round(state.step));
}

/* ========================================================================== */
/* Interactions carte                                                         */
/* ========================================================================== */

function onHover(evt) {
  if (!state.forecast) return;
  const { lon, lat, px, py } = map.pointerToGeo(evt);
  const b = state.boot.geo.bbox;
  const inside = lat <= b.lat_max && lat >= b.lat_min && lon >= b.lon_min && lon <= b.lon_max;
  const readout = $('readout');
  if (!inside) { readout.classList.remove('show'); return; }

  const value = map.valueAt(lat, lon);
  const meta = state.variables[state.variable];
  readout.classList.add('show');
  readout.style.left = `${px}px`;
  readout.style.top = `${py}px`;
  readout.innerHTML = Number.isFinite(value)
    ? `<b>${fmt.num(value, meta.decimals)} ${meta.unit}</b>
       <small>${lat.toFixed(2)}° N · ${lon.toFixed(2)}° E</small>`
    : `<small>${lat.toFixed(2)}° N · ${lon.toFixed(2)}° E</small>`;

  map.hover = { lat, lon };
  map.renderVectors();
}

async function onMapClick(evt) {
  if (!state.forecast) return;
  const { lon, lat } = map.pointerToGeo(evt);
  const city = map.nearestCity(lat, lon);
  if (city) { selectCity(city.name); return; }
  try {
    const point = await API.get(
      `/forecasts/${state.forecast.meta.id}/point?lat=${lat.toFixed(3)}&lon=${lon.toFixed(3)}`,
    );
    state.selection = {
      name: 'Point sélectionné',
      lat: point.lat,
      lon: point.lon,
      series: point.series,
    };
    map.selected = state.selection;
    map.renderVectors();
    renderCity();
  } catch (err) {
    toast(err.message, 'err');
  }
}

function selectCity(name) {
  const city = state.forecast?.cities.find((c) => c.name === name)
    || state.forecast?.cities[0];
  if (!city) return;
  state.selection = city;
  map.selected = city;
  map.renderVectors();
  renderCity();
  renderRanking();
}

/* ========================================================================== */
/* Panneaux                                                                   */
/* ========================================================================== */

function renderMeta(meta) {
  $('meta-card').hidden = false;
  const rows = [
    ['Modèle', meta.model_name],
    ['Moteur', meta.engine === 'aurora' ? 'Aurora (PyTorch)' : 'Simulateur local'],
    ['Calcul', meta.device],
    ['Source', meta.source === 'era5_cds' ? 'ERA5 / CDS' : 'Synthétique'],
    ['Réseau', fmt.full(meta.base_time)],
    ['Échéances', `${meta.steps} × ${meta.step_hours} h`],
    ['Membres', String(meta.members)],
    ['Grille', `${meta.grid.nx} × ${meta.grid.ny}`],
  ];
  const energy = meta.energy;
  if (energy?.measured) {
    rows.push(
      ['Durée de calcul', fmt.duration(energy.duration_s)],
      ['Énergie', fmt.energy(energy.total_wh)],
      ['Puissance moyenne', `${energy.gpu_avg_w} W`],
      ['Empreinte', fmt.co2(energy.co2_g)],
    );
  }
  $('run-summary').innerHTML = rows
    .map(([k, v]) => `<div class="kv"><span>${k}</span><b>${v}</b></div>`)
    .join('');
  const note = $('energy-note');
  if (energy?.measured) {
    note.hidden = false;
    const scope = energy.host_power_w > 0
      ? `GPU mesuré + hôte forfaitaire ${energy.host_power_w} W`
      : 'GPU mesuré seul (hors processeur, mémoire et alimentation)';
    const caveat = energy.on_gpu
      ? ''
      : ' — calcul effectué sur processeur, le GPU était au repos.';
    note.textContent = `${scope} · ${energy.carbon_intensity_g_kwh} gCO₂e/kWh${caveat}`;
  } else {
    note.hidden = true;
  }
}

function renderBadges(meta) {
  const badges = $('badges');
  badges.innerHTML = '';
  const add = (cls, text, title) => {
    const el = document.createElement('span');
    el.className = `pill ${cls}`;
    el.innerHTML = `<i class="dot"></i>${text}`;
    if (title) el.title = title;
    badges.appendChild(el);
  };
  if (meta.real_data) {
    add('ok', 'Données réelles ERA5', 'Conditions initiales issues de la réanalyse ERA5');
  } else {
    add('warn', 'Mode démonstration',
      'Champs produits par le simulateur local : ce n’est pas une prévision météorologique.');
  }
  add('info', meta.model_name, 'Modèle utilisé');
  if (meta.members > 1) add('info', `${meta.members} membres`, 'Prévision d’ensemble');

  const dataPill = $('data-pill');
  dataPill.className = `pill ${meta.real_data ? 'ok' : 'warn'}`;
  dataPill.innerHTML = `<i class="dot"></i>${meta.real_data ? 'ERA5' : 'Démo'}`;
}

function seriesValue(series, name, i) {
  const arr = series?.[name];
  if (!arr) return null;
  const i0 = Math.max(0, Math.min(arr.length - 1, Math.floor(i)));
  const i1 = Math.min(arr.length - 1, i0 + 1);
  const t = i - i0;
  return arr[i0] + (arr[i1] - arr[i0]) * t;
}

function renderCity() {
  const sel = state.selection;
  if (!sel || !state.forecast) return;
  const meta = state.forecast.meta;
  const i = state.step;

  const values = {};
  for (const key of Object.keys(state.variables)) {
    const v = seriesValue(sel.series, key, i);
    if (v !== null) values[key] = v;
  }

  $('city-name').textContent = sel.name;
  $('city-coords').textContent = `${sel.lat.toFixed(2)}° N · ${sel.lon.toFixed(2)}° E`;
  $('city-icon').textContent = conditionIcon(values);
  $('city-temp').textContent = values['2t'] !== undefined ? `${values['2t'].toFixed(1)}°` : '—';
  $('city-cond').innerHTML = `${conditionLabel(values)}<br>
    <span style="color:var(--muted)">${fmt.full(meta.valid_times[Math.round(i)])}</span>`;

  const metrics = [
    ['Vent', values.wind10, 'km/h', 0],
    ['Rafales', values.gust, 'km/h', 0],
    ['Pression', values.msl, 'hPa', 0],
    ['Pluie', values.precip, 'mm/h', 1],
    ['Humidité', values.rh, '%', 0],
    ['Nuages', values.tcc, '%', 0],
    ['T 850 hPa', values.t850, '°C', 1],
    ['Z 500 hPa', values.z500, 'dam', 0],
  ].filter(([, v]) => v !== undefined);

  $('city-metrics').innerHTML = metrics
    .map(([k, v, u, d]) => `<div class="metric"><small>${k}</small><b>${v.toFixed(d)}<i>${u}</i></b></div>`)
    .join('');

  drawMeteogram($('meteogram'), {
    valid: meta.valid_times,
    series: sel.series,
    cursor: i,
  });
}

function renderRanking() {
  if (!state.forecast) return;
  const name = state.variable;
  const meta = state.variables[name];
  const i = Math.round(state.step);
  const domain = domainFor(name);

  const rows = state.forecast.cities
    .map((c) => ({ city: c, value: seriesValue(c.series, name, i) }))
    .filter((r) => r.value !== null)
    .sort((a, b) => b.value - a.value)
    .slice(0, 10);

  $('rank-title').textContent = `${meta.short} — top 10 des villes`;
  const box = $('ranking');
  box.innerHTML = '';
  rows.forEach((r, idx) => {
    const bar = rankBar(r.value, domain, meta.palette);
    const el = document.createElement('div');
    el.className = `rank-row${state.selection?.name === r.city.name ? ' active' : ''}`;
    el.innerHTML = `
      <span class="idx mono">${idx + 1}</span>
      <span>${r.city.name}</span>
      <span class="val">${r.value.toFixed(meta.decimals)} ${meta.unit}</span>
      <span class="rank-bar"><i style="width:${bar.width};background:${bar.color}"></i></span>`;
    el.addEventListener('click', () => selectCity(r.city.name));
    box.appendChild(el);
  });
}

/** Ligne énergie/CO₂ d'une prévision, avec le détail de la méthode en infobulle. */
function energyBadge(energy) {
  if (!energy || !energy.measured) return '';
  const lines = [
    energy.on_gpu
      ? `Consommation du GPU mesurée pendant le calcul (${energy.gpu_avg_w} W moyens)`
      : `Calcul sur processeur : le GPU était au repos (${energy.gpu_avg_w} W).`
        + ' Cette valeur ne reflète donc pas le coût réel du calcul.',
    `Durée : ${fmt.duration(energy.duration_s)}`,
    energy.host_power_w > 0
      ? `Hôte ajouté au forfait de ${energy.host_power_w} W`
      : 'Hors processeur, mémoire et pertes d’alimentation',
    `Intensité carbone : ${energy.carbon_intensity_g_kwh} gCO₂e/kWh`,
    `Source : ${energy.method}`,
  ];
  const cls = energy.on_gpu ? 'energy-line' : 'energy-line idle';
  return `<span class="${cls}" title="${lines.join('\n').replace(/"/g, '&quot;')}">
    ⚡ ${fmt.energy(energy.total_wh)} · 🌿 ${fmt.co2(energy.co2_g)}`
    + `${energy.on_gpu ? '' : ' <i>(GPU au repos)</i>'}</span>`;
}

function renderHistory(forecasts, storage) {
  const box = $('history');
  const summary = $('history-summary');
  if (storage) {
    summary.textContent = storage.count
      ? `${storage.count} enregistrée(s) · ${fmt.bytes(storage.bytes / 1024 ** 2)}`
        + (storage.measured_count
          ? ` · ⚡ ${fmt.energy(storage.energy_wh)} · ${fmt.co2(storage.co2_g)}`
          : '')
      : 'Stockage vide';
    $('history-clear').disabled = !storage.count;
  }
  if (!forecasts.length) {
    box.innerHTML = '<div class="empty-state" style="padding:12px 0">'
      + 'Aucune prévision enregistrée.</div>';
    return;
  }
  box.innerHTML = '';
  for (const f of forecasts) {
    const el = document.createElement('div');
    el.className = `history-item${state.forecast?.meta.id === f.id ? ' active' : ''}`;
    el.innerHTML = `<button class="history-del" title="Supprimer cette prévision">×</button>
      <b>${f.model_name}</b>
      <span>${fmt.full(f.base_time)} · ${f.steps}×${f.step_hours} h ·
      ${f.real_data ? 'ERA5' : 'démo'}${
  f.stored_bytes ? ` · ${fmt.bytes(f.stored_bytes / 1024 ** 2)}` : ''
}</span>
      ${energyBadge(f.energy)}`;
    el.addEventListener('click', () => loadForecast(f.id));
    el.querySelector('.history-del').addEventListener('click', (evt) => {
      evt.stopPropagation();
      removeForecast(f);
    });
    box.appendChild(el);
  }
}

async function refreshHistory() {
  try {
    const data = await API.get('/forecasts');
    renderHistory(data.forecasts, data.storage);
    return data;
  } catch (err) {
    toast(err.message, 'err');
    return null;
  }
}

async function removeForecast(entry) {
  const label = `${entry.model_name} — ${fmt.full(entry.base_time)}`;
  if (!confirm(`Supprimer définitivement cette prévision ?\n\n${label}`)) return;
  try {
    await API.del(`/forecasts/${entry.id}`);
    if (state.forecast?.meta.id === entry.id) clearForecastView();
    const data = await refreshHistory();
    if (state.forecast === null && data?.forecasts.length) loadForecast(data.forecasts[0].id);
    toast('Prévision supprimée', 'ok', 2500);
  } catch (err) {
    toast(err.message, 'err');
  }
}

async function clearHistory() {
  if (!confirm('Supprimer toutes les prévisions enregistrées ?\n'
    + 'Les fichiers correspondants seront effacés du disque.')) return;
  try {
    const result = await API.del('/forecasts');
    clearForecastView();
    await refreshHistory();
    toast(`${result.removed} prévision(s) supprimée(s)`, 'ok');
  } catch (err) {
    toast(err.message, 'err');
  }
}

/** Remet la scène à l'état vide après suppression de la prévision affichée. */
function clearForecastView() {
  stopPlay();
  state.forecast = null;
  state.fields.clear();
  state.wind = null;
  state.step = 0;
  state.selection = null;
  map.selected = null;
  map.setData({ grid: null, frames: null, palette: 'temperature', domain: [0, 1],
    windU: null, windV: null, overlay: null });
  $('stage-empty').hidden = false;
  $('legend').hidden = true;
  $('meta-card').hidden = true;
  $('map-var').textContent = '—';
  $('map-sub').textContent = 'Aucune prévision chargée';
  $('tl-time').textContent = '—';
  $('tl-lead').textContent = '';
  $('tl-range').disabled = true;
  $('play').disabled = true;
  $('badges').innerHTML = '';
  $('ranking').innerHTML = '';
  $('city-metrics').innerHTML = '';
  $('city-temp').textContent = '—';
  $('city-cond').textContent = '';
  $('tl-ticks').innerHTML = '';
}

boot().catch((err) => {
  console.error(err);
  toast(`Initialisation impossible : ${err.message}`, 'err', 12000);
});

/* Vue de vérification : notes des prévisions face à l'analyse ERA5. */

import { API, fmt, subscribe, toast } from './api.js';
import { drawCompare, drawScoreProfile, scoreColor } from './charts.js';

const $ = (id) => document.getElementById(id);

const state = {
  entries: [],
  aggregate: null,
  scales: {},
  coverage: null,
  selected: null,
  report: null,
  forecastCities: [],
  compare: { city: 'Paris', variable: '2t' },
};

/* ========================================================================== */
/* Démarrage                                                                  */
/* ========================================================================== */

async function boot() {
  $('run-btn').addEventListener('click', () => launch(false));
  $('force-btn').addEventListener('click', () => launch(true));
  $('compare-city').addEventListener('change', (e) => {
    state.compare.city = e.target.value;
    renderCompare();
  });
  $('compare-var').addEventListener('change', (e) => {
    state.compare.variable = e.target.value;
    renderCompare();
  });
  window.addEventListener('resize', () => {
    renderProfile();
    renderCompare();
  });

  await refresh();

  subscribe({
    verification: onRunState,
    log: (d) => {
      if (d.level === 'error' && d.source === 'verify') toast(d.message, 'err');
    },
  });
}

async function refresh() {
  const data = await API.get('/verification');
  state.entries = data.entries;
  state.aggregate = data.aggregate;
  state.scales = data.scales;
  state.coverage = data.coverage;

  renderCoverage();
  renderAggregate();
  renderRunList();
  onRunState(data.run);

  const verified = state.entries.filter((e) => hasScore(e));
  // À l'ouverture, on met en avant le réseau le mieux couvert : les plus récents
  // n'ont souvent qu'une ou deux échéances vérifiables (délai de publication ERA5).
  const richest = [...verified].sort(
    (a, b) =>
      b.verification.matched - a.verification.matched
      || (b.created || 0) - (a.created || 0),
  )[0];
  const keep = verified.find((e) => e.id === state.selected) || richest;
  if (keep) await select(keep.id);
  else clearDetail();
}

const hasScore = (entry) =>
  entry.verification && entry.verification.score !== null && entry.verification.score !== undefined;

/* ========================================================================== */
/* Bilan global                                                               */
/* ========================================================================== */

function renderCoverage() {
  const cov = state.coverage;
  const pill = $('coverage-pill');
  const days = new Set([...cov.verification_days, ...cov.initial_condition_days]).size;
  pill.className = `pill ${days ? 'ok' : 'warn'}`;
  pill.innerHTML = `<i class="dot"></i>${days} journée(s) ERA5 en cache`;
  pill.title =
    `Analyses disponibles hors ligne : ${days} jour(s).\n`
    + `ERA5 publiée jusqu'au ${cov.latest_available}.\n`
    + (cov.cds_configured
      ? 'Identifiants Copernicus présents : les journées manquantes peuvent être téléchargées.'
      : 'Aucun identifiant Copernicus : seules les journées déjà en cache sont exploitables.');

  $('allow-download').disabled = !cov.cds_configured;
  if (!cov.cds_configured) $('allow-download').checked = false;
}

function setRing(element, score) {
  element.style.setProperty('--ring', String(score ?? 0));
  element.style.setProperty('--ring-color', scoreColor(score));
}

function renderAggregate() {
  const agg = state.aggregate;
  setRing($('global-ring'), agg.score);
  $('global-score').textContent = agg.score === null ? '—' : agg.score.toFixed(0);
  $('global-grade').textContent = agg.count ? agg.grade : 'Aucune vérification';

  $('global-note').innerHTML = agg.count
    ? `Moyenne de <b>${agg.count}</b> réseau(x) vérifié(s), soit <b>${agg.verified_steps}</b> `
      + 'échéance(s) confrontées à l’analyse ERA5 sur l’ensemble des villes de référence. '
      + 'La note vaut 100 pour une prévision exacte et tombe de moitié à chaque fois '
      + 'que l’erreur moyenne double la tolérance de référence.'
    : 'Lancez la vérification pour confronter chaque prévision enregistrée à la '
      + 'réanalyse ERA5 aux mêmes dates de validité.';

  const bars = $('global-days');
  bars.innerHTML = '';
  if (!agg.days.length) {
    bars.innerHTML = '<div class="empty-note">Aucune journée notée.</div>';
  }
  for (const day of agg.days) {
    const el = document.createElement('div');
    el.className = 'day-bar';
    el.title = `${day.label} — moyenne sur ${day.samples} réseau(x) : ${day.grade}`;
    el.innerHTML = `<b style="color:${scoreColor(day.score)}">${day.score.toFixed(0)}</b>
      <i style="height:${Math.max(3, day.score)}%;background:${scoreColor(day.score, 0.8)}"></i>
      <small>${day.label}</small>`;
    bars.appendChild(el);
  }

  const table = $('global-vars');
  table.innerHTML = '';
  const vars = Object.entries(agg.variables || {});
  if (!vars.length) {
    table.innerHTML = '<div class="empty-note">Aucune variable notée.</div>';
    return;
  }
  for (const [key, detail] of vars) {
    const el = document.createElement('div');
    el.className = 'var-row';
    el.title = skillHint(detail.skill);
    el.innerHTML = `
      <span>${detail.label}</span>
      <span class="var-track"><i style="width:${detail.score}%;background:${scoreColor(detail.score, 0.85)}"></i></span>
      <span class="num">${detail.mae} ${detail.unit}</span>
      <span class="note" style="color:${scoreColor(detail.score)}">${detail.score.toFixed(0)}</span>`;
    table.appendChild(el);
  }
}

function skillHint(skill) {
  if (skill === null || skill === undefined) return 'Gain sur la persistance : non calculable';
  const percent = (skill * 100).toFixed(0);
  return skill > 0
    ? `Erreur quadratique réduite de ${percent} % par rapport à la persistance `
      + '(« demain = aujourd’hui »)'
    : `Aucun gain sur la persistance (${percent} %)`;
}

/* ========================================================================== */
/* Liste des réseaux                                                          */
/* ========================================================================== */

function renderRunList() {
  const box = $('run-list');
  box.innerHTML = '';
  if (!state.entries.length) {
    box.innerHTML = '<div class="empty-note">Aucune prévision enregistrée.</div>';
    return;
  }
  for (const entry of state.entries) {
    const report = entry.verification;
    const el = document.createElement('div');
    el.className = `run-item${entry.id === state.selected ? ' active' : ''}`
      + `${hasScore(entry) ? '' : ' pending'}`;

    const status = !entry.real_data
      ? 'simulateur'
      : !report
        ? 'non vérifiée'
        : report.status === 'unavailable'
          ? 'ERA5 indisponible'
          : `${report.matched}/${report.expected} échéances`;

    el.innerHTML = `
      <div>
        <b>${fmt.full(entry.base_time)}</b>
        <span>${entry.steps} × ${entry.step_hours} h · ${status}</span>
      </div>
      <div class="note" style="color:${scoreColor(report?.score ?? null)}">
        ${hasScore(entry) ? report.score.toFixed(0) : '—'}
        <small>${hasScore(entry) ? report.grade : ''}</small>
      </div>`;
    if (report?.reason) el.title = report.reason;
    el.addEventListener('click', () => select(entry.id));
    box.appendChild(el);
  }
}

/* ========================================================================== */
/* Détail d'un réseau                                                         */
/* ========================================================================== */

async function select(id) {
  state.selected = id;
  renderRunList();
  try {
    const data = await API.get(`/verification/${id}`);
    state.report = data.report;
    state.forecastCities = data.cities || [];
    renderDetail();
  } catch (err) {
    state.report = null;
    clearDetail();
    if (err.message) toast(err.message, 'err');
  }
}

function clearDetail() {
  $('detail-title').textContent = '—';
  $('detail-sub').textContent = '';
  $('detail-score').innerHTML = '—';
  $('detail-days').innerHTML = '<div class="empty-note">Aucun réseau vérifié.</div>';
  $('detail-vars').innerHTML = '';
  $('city-list').innerHTML = '<div class="empty-note">Aucune note par ville.</div>';
  $('compare-note').textContent = '';
  const blank = (id) => {
    const canvas = $(id);
    canvas.getContext('2d').clearRect(0, 0, canvas.width, canvas.height);
  };
  blank('profile-chart');
  blank('compare-chart');
}

function renderDetail() {
  const report = state.report;
  if (!report) return;

  $('detail-title').textContent =
    `${report.model_name} · réseau ${fmt.full(report.base_time)}`;
  $('detail-sub').textContent =
    `${report.matched}/${report.expected} échéances vérifiées · ${report.cities ?? 0} villes`
    + ` · analyse ERA5${report.reason ? ` · ${report.reason}` : ''}`;
  $('detail-score').innerHTML = report.score === null
    ? '—'
    : `<span style="color:${scoreColor(report.score)}">${report.score.toFixed(0)}</span>`
      + `<small>${report.grade} · sur 100</small>`;

  const chips = $('detail-days');
  chips.innerHTML = '';
  if (!report.days?.length) {
    chips.innerHTML = '<div class="empty-note">Aucune journée complète à noter.</div>';
  }
  for (const day of report.days || []) {
    const el = document.createElement('div');
    el.className = 'day-chip';
    el.style.borderColor = scoreColor(day.score, 0.42);
    el.title = `${day.label} (${day.date}) — ${day.steps} échéance(s) · ${day.grade}`;
    el.innerHTML = `<b style="color:${scoreColor(day.score)}">${day.score.toFixed(0)}</b>
      <small>${day.label}</small>`;
    chips.appendChild(el);
  }

  renderVariables();
  renderProfile();
  renderCities();
  buildCompareControls();
  renderCompare();
}

function renderVariables() {
  const table = $('detail-vars');
  table.innerHTML = '';
  const entries = Object.entries(state.report.variables || {});
  if (!entries.length) {
    table.innerHTML = '<div class="empty-note">Aucune variable comparable.</div>';
    return;
  }
  const head = document.createElement('div');
  head.className = 'var-head';
  head.innerHTML = '<span>Variable</span><span>Note</span><span>EAM</span>'
    + '<span>Biais</span><span>Gain</span><span></span>';
  table.appendChild(head);

  for (const [key, detail] of entries) {
    const el = document.createElement('div');
    el.className = 'var-row';
    const skill = detail.skill === null || detail.skill === undefined
      ? '—'
      : `${(detail.skill * 100).toFixed(0)} %`;
    el.title = `Tolérance de référence : ${detail.tolerance} ${detail.unit} `
      + `(erreur valant 50/100)\nRMSE : ${detail.rmse} ${detail.unit}\n`
      + `${detail.n} comparaison(s) ville × échéance\n${skillHint(detail.skill)}`;
    el.innerHTML = `
      <span>${detail.label}</span>
      <span class="var-track"><i style="width:${detail.score}%;background:${scoreColor(detail.score, 0.85)}"></i></span>
      <span class="num">${detail.mae} ${detail.unit}</span>
      <span class="num">${detail.bias > 0 ? '+' : ''}${detail.bias} ${detail.unit}</span>
      <span class="num">${skill}</span>
      <span class="note" style="color:${scoreColor(detail.score)}">${detail.score.toFixed(0)}</span>`;
    el.addEventListener('click', () => {
      state.compare.variable = key;
      $('compare-var').value = key;
      renderCompare();
    });
    el.style.cursor = 'pointer';
    table.appendChild(el);
  }
}

function renderProfile() {
  if (!state.report?.leads?.length) return;
  drawScoreProfile(
    $('profile-chart'),
    state.report.leads
      .filter((l) => l.score !== null)
      .map((l) => ({ lead: l.lead, score: l.score })),
  );
}

function renderCities() {
  const box = $('city-list');
  box.innerHTML = '';
  const rows = state.report?.city_scores || [];
  if (!rows.length) {
    box.innerHTML = '<div class="empty-note">Aucune note par ville.</div>';
    return;
  }
  for (const row of rows) {
    const el = document.createElement('div');
    el.className = 'city-row';
    const temp = row.variables['2t'];
    el.title = temp
      ? `Température : erreur moyenne ${temp.mae} °C, biais ${temp.bias > 0 ? '+' : ''}${temp.bias} °C`
      : '';
    el.innerHTML = `
      <span>${row.name}</span>
      <span class="bar"><i style="width:${row.score}%;background:${scoreColor(row.score, 0.85)}"></i></span>
      <span class="note" style="color:${scoreColor(row.score)}">${row.score.toFixed(0)}</span>`;
    el.addEventListener('click', () => {
      state.compare.city = row.name;
      $('compare-city').value = row.name;
      renderCompare();
    });
    el.style.cursor = 'pointer';
    box.appendChild(el);
  }
}

/* ========================================================================== */
/* Comparaison prévu / observé                                                */
/* ========================================================================== */

function buildCompareControls() {
  const report = state.report;
  const cities = $('compare-city');
  const names = (report.observed || []).map((o) => o.name).sort((a, b) => a.localeCompare(b, 'fr'));
  cities.innerHTML = names.map((n) => `<option value="${n}">${n}</option>`).join('');
  if (!names.includes(state.compare.city)) state.compare.city = names[0] || '';
  cities.value = state.compare.city;

  const vars = $('compare-var');
  const available = Object.entries(report.variables || {});
  vars.innerHTML = available
    .map(([key, d]) => `<option value="${key}">${d.label} (${d.unit})</option>`)
    .join('');
  if (!available.some(([key]) => key === state.compare.variable)) {
    state.compare.variable = available[0]?.[0] || '2t';
  }
  vars.value = state.compare.variable;
}

function renderCompare() {
  const report = state.report;
  const note = $('compare-note');
  if (!report?.indices?.length) { note.textContent = ''; return; }

  const { city: cityName, variable } = state.compare;
  const observedCity = (report.observed || []).find((o) => o.name === cityName);
  const forecastCity = state.forecastCities.find((c) => c.name === cityName);
  const detail = report.variables?.[variable];
  if (!observedCity || !forecastCity || !detail) { note.textContent = ''; return; }

  const truth = observedCity.series[variable] || [];
  const modelled = forecastCity.series[variable] || [];

  // Les échéances sans analyse ERA5 sont retirées des deux séries à la fois :
  // superposer un trou fabriquerait un écart qui n'existe pas.
  const times = [];
  const predicted = [];
  const observed = [];
  report.indices.forEach((index, k) => {
    const a = modelled[index];
    const b = truth[k];
    if (!Number.isFinite(a) || !Number.isFinite(b)) return;
    times.push(report.valid[k]);
    predicted.push(a);
    observed.push(b);
  });
  if (!times.length) { note.textContent = ''; return; }

  drawCompare($('compare-chart'), {
    times,
    predicted,
    observed,
    decimals: Math.abs(detail.tolerance) < 3 ? 1 : 0,
  });

  const city = report.city_scores.find((c) => c.name === cityName);
  const local = city?.variables?.[variable];
  note.textContent = local
    ? `${cityName} · ${detail.label} : erreur moyenne ${local.mae} ${detail.unit}, `
      + `biais ${local.bias > 0 ? '+' : ''}${local.bias} ${detail.unit}, `
      + `note ${local.score.toFixed(0)}/100 sur ${times.length} échéances.`
    : '';
}

/* ========================================================================== */
/* Exécution                                                                  */
/* ========================================================================== */

async function launch(force) {
  try {
    const run = await API.post('/verification/run', {
      download: $('allow-download').checked,
      force,
    });
    onRunState(run);
    toast('Vérification lancée', 'ok', 2600);
  } catch (err) {
    toast(err.message, 'err', 9000);
  }
}

let wasRunning = false;

function onRunState(run) {
  if (!run) return;
  const pill = $('run-pill');
  const running = run.status === 'running';
  const style = {
    running: ['info pulse', run.message || 'Vérification…'],
    done: ['ok', run.message || 'Vérification terminée'],
    error: ['err', run.error || 'Échec'],
    idle: ['muted', 'En attente'],
  }[run.status] || ['muted', run.status];
  pill.className = `pill ${style[0]}`;
  pill.innerHTML = `<i class="dot"></i>${style[1]}`;

  $('run-progress').hidden = !running;
  $('run-bar').style.width = `${Math.round((run.progress || 0) * 100)}%`;
  $('run-btn').disabled = running;
  $('force-btn').disabled = running;
  $('run-message').textContent = running
    ? `${run.done}/${run.total} — ${run.current ? `réseau ${run.current}` : run.message}`
    : run.status === 'error'
      ? run.error || ''
      : '';

  if (wasRunning && !running) refresh().catch(() => {});
  wasRunning = running;
}

boot().catch((err) => {
  console.error(err);
  toast(`Initialisation impossible : ${err.message}`, 'err', 12000);
});

/* Console d'administration : cycle de vie du modèle, ressources, journal. */

import { API, fmt, subscribe, toast, token } from './api.js';

const $ = (id) => document.getElementById(id);

/**
 * Les opérations d'administration sont refusées depuis le réseau sans jeton :
 * on le demande une fois puis on le conserve dans ce navigateur.
 */
async function withAdmin(action) {
  try {
    return await action();
  } catch (err) {
    if (!/refusée depuis le réseau/i.test(err.message)) throw err;
    if (/Aucun jeton configuré/i.test(err.message)) {
      toast(err.message, 'err', 12000);
      return undefined;
    }
    const value = prompt("Jeton d'administration (AURORA_ADMIN_TOKEN) :", '');
    if (!value) return undefined;
    token.set(value.trim());
    return action();
  }
}

const state = {
  models: [],
  devices: [],
  modelState: null,
  system: null,
  sources: [],
  preflight: new Map(),
  tunnel: null,
  github: null,
  repos: [],
  pollTimer: null,
};

/* ========================================================================== */

async function boot() {
  bindUi();
  const data = await API.get('/bootstrap');
  state.models = data.models;
  state.devices = data.devices;
  state.sources = data.sources;
  renderModels();
  renderSources();
  renderAccess(data.access);
  applySystem(data.system);
  applyModelState(data.model_state);
  applyTunnel(data.tunnel);
  applyGithub(data.github);
  refreshCache();
  refreshStorage();

  const { logs } = await API.get('/logs');
  logs.forEach((entry) => appendLog(entry.data, entry.ts));

  subscribe({
    onOpen: () => setPill('sse-pill', 'ok', 'Flux connecté'),
    onClose: () => setPill('sse-pill', 'err', 'Flux interrompu'),
    model_state: applyModelState,
    tunnel_state: applyTunnel,
    log: (d) => appendLog(d),
    dependencies: () => refreshSystem(),
    job: (d) => {
      if (d.status === 'done' || d.status === 'error') { refreshSystem(); refreshStorage(); }
    },
  });

  setInterval(refreshSystem, 5000);
}

function bindUi() {
  $('unload-btn').addEventListener('click', unloadModel);
  $('refresh-btn').addEventListener('click', () => { refreshSystem(); refreshCache(); });
  $('purge-btn').addEventListener('click', purgeCache);
  $('storage-clear').addEventListener('click', clearForecastStorage);
  $('install-btn').addEventListener('click', installDeps);
  $('clear-log').addEventListener('click', () => { $('console').innerHTML = ''; });

  $('tunnel-start').addEventListener('click', startTunnel);
  $('tunnel-stop').addEventListener('click', stopTunnel);
  $('tunnel-copy').addEventListener('click', copyTunnelUrl);
  $('tunnel-provider').addEventListener('change', renderTunnelHint);

  $('gh-login').addEventListener('click', startGithubLogin);
  $('gh-logout').addEventListener('click', logoutGithub);
  $('repo-refresh').addEventListener('click', () => loadRepos(true));
  $('repo-private-only').addEventListener('change', () => loadRepos(true));
  $('repo-select').addEventListener('change', updatePublishState);
  $('publish-btn').addEventListener('click', publishDemo);
}

function setPill(id, cls, text, pulse = false) {
  const el = $(id);
  el.className = `pill ${cls}${pulse ? ' pulse' : ''}`;
  el.innerHTML = `<i class="dot"></i>${text}`;
}

/** Bandeau indiquant l'exposition réseau et le régime d'administration. */
function renderAccess(access) {
  if (!access) return;
  const pill = $('access-pill');
  if (!access.exposed) {
    setPill('access-pill', 'ok', 'Boucle locale uniquement');
  } else if (access.token_required) {
    setPill('access-pill', 'info', 'Réseau · administration par jeton');
  } else {
    setPill('access-pill', 'warn', 'Réseau · administration locale seule');
  }
  const urls = access.addresses.map((a) => `http://${a.ip}:${access.port}`);
  pill.title = access.exposed
    ? `Accessible depuis : ${urls.join(', ') || 'aucune adresse détectée'}`
    : 'Le serveur n’écoute que sur 127.0.0.1';

  const box = $('access-box');
  if (!box) return;
  box.innerHTML = access.exposed
    ? `<div class="spec"><span>Vous êtes</span><b>${
        access.local_client ? 'sur la machine hôte' : `un poste distant (${access.client})`
      }</b></div>
       ${urls.map((u) => `<div class="spec"><span>URL</span><b>${u}</b></div>`).join('')}
       <div class="spec"><span>Administration à distance</span><b>${
         access.token_required ? 'jeton requis' : 'désactivée'
       }</b></div>`
    : '<div class="spec"><span>Écoute</span><b>127.0.0.1 uniquement</b></div>';
}

/* ========================================================================== */
/* Ressources                                                                 */
/* ========================================================================== */

async function refreshSystem() {
  try {
    applySystem(await API.get('/system'));
  } catch { /* le serveur peut redémarrer */ }
}

function gauge(label, used, total, unit, extra = '') {
  const pct = total ? Math.min(100, (used / total) * 100) : 0;
  const cls = pct > 88 ? 'hot' : pct > 60 ? '' : 'ok';
  return `<div class="gauge">
    <div class="gauge-head">
      <b>${label}</b>
      <span class="mono">${fmt.num(used, 0)} / ${fmt.num(total, 0)} ${unit} ${extra}</span>
    </div>
    <div class="gauge-track"><div class="gauge-fill ${cls}" style="width:${pct.toFixed(1)}%"></div></div>
  </div>`;
}

function applySystem(sys) {
  state.system = sys;
  $('host-line').textContent =
    `${sys.host} · ${sys.platform} · ${sys.machine} · Python ${sys.dependencies.python}`;

  let html = gauge('Processeur', sys.cpu_percent, 100, '%', `— ${sys.cpu_count} cœurs`);
  html += gauge('Mémoire vive', sys.ram_used_mb / 1024, sys.ram_total_mb / 1024, 'Go');
  for (const g of sys.gpus) {
    html += gauge(g.name, g.used_mb / 1024, g.total_mb / 1024, 'Go',
      g.allocated_mb != null ? `— torch ${(g.allocated_mb / 1024).toFixed(1)} Go` : '');
  }
  if (!sys.gpus.length) {
    html += `<div class="notice" style="font-size:11.3px;padding:9px 11px;border-radius:10px;
      border:1px solid rgba(255,180,87,.28);background:rgba(255,180,87,.08);color:var(--text-dim)">
      Aucun GPU CUDA détecté : les modèles Aurora complets ne pourront tourner que sur CPU
      (très lent) ou nécessitent l'installation d'un PyTorch compatible avec votre carte.</div>`;
  }
  html += gauge('Disque (cache)', sys.disk_total_gb - sys.disk_free_gb, sys.disk_total_gb, 'Go');
  $('gauges').innerHTML = html;

  $('specs').innerHTML = [
    ['Hôte', sys.host],
    ['Système', sys.platform],
    ['Architecture', sys.machine],
    ['Cœurs logiques', String(sys.cpu_count)],
    ['RAM totale', `${(sys.ram_total_mb / 1024).toFixed(1)} Go`],
    ['GPU', sys.gpus.length ? sys.gpus.map((g) => g.name).join(', ') : 'aucun'],
    ['CUDA', sys.dependencies.cuda_version || 'indisponible'],
    ['Adresses réseau', sys.addresses?.length
      ? sys.addresses.map((a) => a.ip).join(', ') : 'boucle locale uniquement'],
    ['Cache', sys.cache_dir],
  ].map(([k, v]) => `<div class="spec"><span>${k}</span><b>${v}</b></div>`).join('');

  const d = sys.dependencies;
  const deps = [
    ['PyTorch', d.torch],
    ['microsoft-aurora', d.aurora],
    ['CUDA disponible', d.cuda_available ? 'oui' : 'non'],
    ['cdsapi', d.cdsapi ? 'installé' : null],
    ['xarray', d.xarray ? 'installé' : null],
    ['netCDF4', d.netcdf4 ? 'installé' : null],
  ];
  $('deps').innerHTML = deps.map(([k, v]) => `
    <div class="dep ${v && v !== 'non' ? 'ok' : 'ko'}">
      <small>${k}</small><b>${v || 'absent'}</b>
    </div>`).join('');

  const missing = !d.torch || !d.aurora;
  $('install-btn').disabled = !missing && !$('install-era5').checked
    ? false
    : $('install-btn').disabled;
}

async function refreshCache() {
  try {
    const cache = await API.get('/models/cache');
    const entries = cache.entries.length
      ? cache.entries.map((e) => `<div class="cache-item"><span>${e.name}</span>
          <b class="mono">${fmt.bytes(e.size_mb)}</b></div>`).join('')
      : '<div style="font-size:11.5px;color:var(--muted)">Aucun poids téléchargé.</div>';
    $('cache-box').innerHTML =
      `<div class="spec" style="margin-bottom:9px"><span>Total</span>
        <b class="mono">${fmt.bytes(cache.total_mb)}</b></div>${entries}`;
  } catch { /* ignore */ }
}

async function purgeCache() {
  if (!confirm('Supprimer tous les poids téléchargés depuis HuggingFace ?')) return;
  try {
    await withAdmin(() => API.del('/models/cache'));
    toast('Cache purgé', 'ok');
    refreshCache();
  } catch (err) {
    toast(err.message, 'err');
  }
}

/* --- prévisions enregistrées --------------------------------------------- */

async function refreshStorage() {
  try {
    const { forecasts, storage } = await API.get('/forecasts');
    $('storage-box').innerHTML = [
      ['Entrées', `${storage.count} / ${storage.max_stored}`],
      ['Espace occupé', fmt.bytes(storage.bytes / 1024 ** 2)],
      ['Emplacement', storage.path],
    ].map(([k, v]) => `<div class="spec"><span>${k}</span><b>${escapeHtml(String(v))}</b></div>`)
      .join('');

    const list = $('stored-list');
    if (!forecasts.length) {
      list.innerHTML = '<div style="font-size:11.5px;color:var(--muted);padding:6px 0">'
        + 'Aucune prévision enregistrée.</div>';
    } else {
      list.innerHTML = '';
      for (const f of forecasts) {
        const row = document.createElement('div');
        row.className = 'stored-item';
        row.innerHTML = `<div class="info">
            <b>${escapeHtml(f.model_name || f.model_id)}</b>
            <span>${fmt.full(f.base_time)} · ${f.steps}×${f.step_hours} h ·
              ${f.real_data ? 'ERA5' : 'démo'}</span>
          </div>
          <span class="size">${fmt.bytes((f.stored_bytes || 0) / 1024 ** 2)}</span>
          <button title="Supprimer">×</button>`;
        row.querySelector('button').addEventListener('click', () => removeStored(f));
        list.appendChild(row);
      }
    }
    $('storage-clear').disabled = !storage.count;
  } catch { /* ignore */ }
}

async function removeStored(entry) {
  if (!confirm(`Supprimer cette prévision ?\n\n${entry.model_name} — ${fmt.full(entry.base_time)}`)) {
    return;
  }
  try {
    await withAdmin(() => API.del(`/forecasts/${entry.id}`));
    refreshStorage();
  } catch (err) {
    toast(err.message, 'err');
  }
}

async function clearForecastStorage() {
  if (!confirm('Supprimer toutes les prévisions enregistrées ?')) return;
  try {
    const result = await withAdmin(() => API.del('/forecasts'));
    if (result) toast(`${result.removed} prévision(s) supprimée(s)`, 'ok');
    refreshStorage();
  } catch (err) {
    toast(err.message, 'err');
  }
}

async function installDeps() {
  const extras = $('install-era5').checked ? ['era5'] : [];
  if (!confirm(
    'Installer PyTorch et microsoft-aurora dans l’environnement du serveur ?\n'
    + 'Plusieurs Go seront téléchargés. La progression s’affiche dans le journal.',
  )) return;
  try {
    const started = await withAdmin(() => API.post('/dependencies/install', { extras }));
    if (started) {
      $('install-btn').disabled = true;
      toast('Installation lancée — suivez le journal', 'ok');
    }
  } catch (err) {
    toast(err.message, 'err');
  }
}

/* ========================================================================== */
/* Modèles                                                                    */
/* ========================================================================== */

function renderModels() {
  const box = $('models');
  box.innerHTML = '';
  for (const m of state.models) {
    const card = document.createElement('article');
    card.className = 'model-card';
    card.dataset.id = m.id;

    const deviceOptions = state.devices
      .map((d) => `<option value="${d.id}">${d.label}</option>`).join('');
    const supports = m.supports
      .map((s) => `<span class="chip">${s}</span>`).join('');

    card.innerHTML = `
      <div class="model-head">
        <div style="flex:1">
          <div class="model-family">${m.family}</div>
          <h3>${m.name}</h3>
        </div>
        <span class="pill ${m.real_data ? 'info' : 'warn'}" style="font-size:10px">
          ${m.real_data ? 'données réelles' : 'démo'}</span>
      </div>
      <p class="model-desc">${m.description}</p>
      <div class="model-specs">
        <div><small>Résolution</small><b>${m.resolution}</b></div>
        <div><small>Paramètres</small><b>${m.params}</b></div>
        <div><small>Poids</small><b>${m.download_mb ? fmt.bytes(m.download_mb) : '—'}</b></div>
        <div><small>VRAM</small><b>${m.min_vram_gb ? `${m.min_vram_gb} Go` : '—'}</b></div>
      </div>
      <div class="chip-row">${supports}</div>
      ${m.engine === 'aurora' ? `
        <label style="display:flex;align-items:center;gap:8px;font-size:11.3px;color:var(--text-dim)">
          <input type="checkbox" class="lora" checked> LoRA activé (MSE optimale)
        </label>` : ''}
      <div class="preflight" data-role="preflight"></div>
      <div class="model-actions">
        <select data-role="device">${deviceOptions}</select>
        <button class="btn btn-primary btn-sm" data-role="load">Charger</button>
      </div>`;

    const deviceSel = card.querySelector('[data-role="device"]');
    if (m.engine === 'simulation') {
      deviceSel.disabled = true;
    } else {
      const cuda = state.devices.find((d) => d.id.startsWith('cuda'));
      if (cuda) deviceSel.value = cuda.id;
    }
    card.querySelector('[data-role="load"]').addEventListener('click', () => loadModel(m, card));
    deviceSel.addEventListener('change', () => runPreflight(m, card));
    box.appendChild(card);
    runPreflight(m, card);
  }
}

async function runPreflight(model, card) {
  const device = card.querySelector('[data-role="device"]').value;
  try {
    const { checks } = await API.post('/models/preflight', { model_id: model.id, device });
    card.querySelector('[data-role="preflight"]').innerHTML = checks
      .map((c) => `<div class="pf ${c.ok ? 'ok' : 'ko'}"><i>${c.ok ? '✔' : '✖'}</i>
        <div><b style="font-weight:600">${c.label}</b> <span>${c.detail}</span></div></div>`)
      .join('');
    card.dataset.ready = checks.every((c) => c.ok) ? '1' : '0';
  } catch { /* ignore */ }
}

async function loadModel(model, card) {
  const device = card.querySelector('[data-role="device"]').value;
  const loraBox = card.querySelector('.lora');
  try {
    const done = await withAdmin(() => API.post('/models/load', {
      model_id: model.id,
      device,
      use_lora: loraBox ? loraBox.checked : true,
    }));
    if (done) toast(`Chargement de « ${model.name} » lancé`, 'ok');
  } catch (err) {
    toast(err.message, 'err');
  }
}

async function unloadModel() {
  try {
    await withAdmin(() => API.post('/models/unload'));
  } catch (err) {
    toast(err.message, 'err');
  }
}

function applyModelState(s) {
  state.modelState = s;
  const busy = ['checking', 'loading', 'downloading', 'unloading'].includes(s.state);
  const labels = {
    idle: ['muted', 'Aucun modèle'],
    checking: ['info', 'Vérification'],
    downloading: ['info', 'Téléchargement'],
    loading: ['info', 'Chargement'],
    ready: ['ok', 'Prêt'],
    busy: ['info', 'Inférence'],
    unloading: ['warn', 'Déchargement'],
    error: ['err', 'Erreur'],
  };
  const [cls, text] = labels[s.state] || ['muted', s.state];
  setPill('state-pill', cls, text, busy || s.state === 'busy');
  setPill('active-pill', cls, text, busy || s.state === 'busy');

  const box = $('active-box');
  box.className = `active-box${s.state === 'idle' ? ' idle' : ''}${s.state === 'error' ? ' error' : ''}`;
  $('active-name').textContent = s.model_name || 'Aucun modèle chargé';
  $('active-stage').textContent = s.error || s.stage || '';
  $('active-bar').style.width = `${Math.round((s.progress || 0) * 100)}%`;

  const rows = s.model_id ? [
    ['Identifiant', s.model_id],
    ['Moteur', s.engine === 'aurora' ? 'Aurora / PyTorch' : 'Simulateur local'],
    ['Périphérique', s.device],
    ['LoRA', s.engine === 'aurora' ? (s.use_lora ? 'activé' : 'désactivé') : '—'],
    ['Temps de chargement', s.load_seconds ? `${s.load_seconds} s` : '—'],
    ['En mémoire depuis', fmt.duration(s.uptime_s)],
    ['Inférences', String(s.inference_count)],
    ['Dernière inférence', s.last_inference
      ? `${s.last_inference.steps} pas en ${s.last_inference.seconds} s` : '—'],
  ] : [];
  $('active-specs').innerHTML = rows
    .map(([k, v]) => `<div class="spec"><span>${k}</span><b>${v}</b></div>`).join('');

  $('unload-btn').disabled = !(s.state === 'ready' || s.state === 'error');
  $('install-btn').disabled = s.install_running;
  if (s.install_running) $('install-btn').textContent = '⏳ Installation en cours…';
  else $('install-btn').textContent = '⤓ Installer PyTorch + microsoft-aurora';

  for (const card of $('models').children) {
    const isActive = card.dataset.id === s.model_id;
    card.classList.toggle('loaded', isActive && ['ready', 'busy'].includes(s.state));
    card.classList.toggle('busy', isActive && busy);
    const btn = card.querySelector('[data-role="load"]');
    if (isActive && ['ready', 'busy'].includes(s.state)) {
      btn.textContent = 'Chargé';
      btn.disabled = true;
    } else {
      btn.textContent = 'Charger';
      btn.disabled = busy || s.state === 'busy';
    }
  }
}

/* ========================================================================== */
/* Tunnel public                                                              */
/* ========================================================================== */

function applyTunnel(t) {
  state.tunnel = t;
  const select = $('tunnel-provider');
  const signature = t.providers.map((p) => `${p.id}:${p.ready}`).join('|');
  if (select.dataset.signature !== signature) {
    select.dataset.signature = signature;
    select.innerHTML = t.providers
      .map((p) => {
        let suffix = '';
        if (!p.available) suffix = ' — non installé';
        else if (!p.ready) suffix = ' — connexion requise';
        return `<option value="${p.id}" ${p.available ? '' : 'disabled'}>`
          + `${p.label}${suffix}</option>`;
      })
      .join('');
    const preferred = t.providers.find((p) => p.ready) || t.providers.find((p) => p.available);
    if (preferred) select.value = preferred.id;
  }
  if (t.provider) select.value = t.provider;

  const labels = {
    stopped: ['muted', 'Fermé'],
    starting: ['info', 'Ouverture…'],
    running: ['ok', 'Ouvert'],
    error: ['err', 'Erreur'],
  };
  const [cls, text] = labels[t.state] || ['muted', t.state];
  setPill('tunnel-pill', cls, text, t.state === 'starting');

  const running = t.state === 'running';
  $('tunnel-start').disabled = running || t.state === 'starting';
  $('tunnel-stop').disabled = !running && t.state !== 'starting';
  select.disabled = running || t.state === 'starting';

  $('tunnel-url-box').hidden = !running;
  if (running) {
    $('tunnel-url').textContent = t.url;
    $('tunnel-url').href = t.url;
  }

  const warning = $('tunnel-warning');
  warning.hidden = !running;
  if (running) {
    warning.innerHTML = `<b>⚠ Cette station est publiquement accessible.</b>
      N'importe qui disposant du lien peut consulter les prévisions et en lancer
      de nouvelles. Le pilotage du modèle reste protégé.
      Lien éphémère : il change à chaque réouverture.`;
  }

  renderTunnelHint();
  updatePublishState();
}

function renderTunnelHint() {
  const t = state.tunnel;
  if (!t) return;
  const chosen = t.providers.find((p) => p.id === $('tunnel-provider').value);
  const hint = $('tunnel-hint');
  if (t.state === 'error') {
    hint.innerHTML = `<span style="color:var(--err)">${escapeHtml(t.error || 'Échec')}</span>`;
    return;
  }
  if (!chosen) { hint.textContent = ''; return; }
  if (!chosen.available) {
    hint.innerHTML = `${escapeHtml(chosen.description)}<br>`
      + `Installation : <a href="${chosen.install}" target="_blank" `
      + 'rel="noopener noreferrer" style="color:var(--accent)">documentation</a>';
    return;
  }
  const parts = [`${chosen.description} Redirige vers le port ${t.port}.`];
  if (chosen.account) {
    parts.push(`<span style="color:var(--ok)">✔ ${escapeHtml(chosen.account)}</span>`);
  } else if (!chosen.anonymous) {
    parts.push('<span style="color:var(--warn)">⚠ Aucune session : '
      + '<code>devtunnel user login -g</code></span>');
  }
  hint.innerHTML = parts.join('<br>');
}

async function startTunnel() {
  const provider = $('tunnel-provider').value;
  try {
    await withAdmin(() => API.post('/tunnel/start', { provider }));
  } catch (err) {
    toast(err.message, 'err');
  }
}

async function stopTunnel() {
  try {
    await withAdmin(() => API.post('/tunnel/stop'));
  } catch (err) {
    toast(err.message, 'err');
  }
}

async function copyTunnelUrl() {
  const url = state.tunnel?.url;
  if (!url) return;
  try {
    // `navigator.clipboard` exige un contexte sécurisé : repli sur la sélection.
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(url);
    } else {
      const area = document.createElement('textarea');
      area.value = url;
      area.style.position = 'fixed';
      area.style.opacity = '0';
      document.body.appendChild(area);
      area.select();
      document.execCommand('copy');
      area.remove();
    }
    toast('URL copiée', 'ok', 2000);
  } catch {
    toast('Copie impossible — sélectionnez le lien manuellement', 'err');
  }
}

/* ========================================================================== */
/* GitHub                                                                     */
/* ========================================================================== */

function applyGithub(g) {
  state.github = g;
  $('issue-title').textContent = g.issue_title;

  if (!g.configured) {
    setPill('gh-pill', 'warn', 'Non configuré');
    $('gh-login').disabled = true;
    $('gh-anon').querySelector('#gh-login').title =
      'Renseignez GITHUB_CLIENT_ID dans le fichier .env';
    return;
  }

  const authed = g.authenticated;
  setPill('gh-pill', authed ? 'ok' : 'muted',
    authed ? g.user?.login || 'Connecté' : 'Déconnecté');
  $('gh-anon').hidden = authed;
  $('gh-user').hidden = !authed;

  if (authed) {
    $('gh-avatar').src = g.user?.avatar_url || '';
    $('gh-login-name').textContent = g.user?.login || '';
    $('gh-full-name').textContent = g.user?.name || '';
    if (!state.repos.length) loadRepos();
  }

  $('gh-device').hidden = !g.pending;
  if (g.pending) {
    $('gh-code').textContent = g.user_code || '————';
    $('gh-verify').textContent = g.verification_uri;
    $('gh-verify').href = g.verification_uri;
  }
  updatePublishState();
}

async function startGithubLogin() {
  try {
    const data = await withAdmin(() => API.post('/github/login'));
    if (!data) return;
    applyGithub(data);
    schedulePoll(2000);
  } catch (err) {
    toast(err.message, 'err', 10000);
  }
}

function schedulePoll(delay) {
  clearTimeout(state.pollTimer);
  state.pollTimer = setTimeout(pollGithubLogin, delay);
}

async function pollGithubLogin() {
  try {
    const data = await API.post('/github/login/poll');
    applyGithub(data);
    if (data.status === 'done') {
      toast(`Connecté à GitHub : ${data.user?.login}`, 'ok');
      state.repos = [];
      loadRepos(true);
      return;
    }
    $('gh-poll-status').textContent = 'En attente d’autorisation sur GitHub…';
    schedulePoll(Math.max(2000, (data.retry_in || 5) * 1000));
  } catch (err) {
    $('gh-poll-status').textContent = err.message;
    toast(err.message, 'err', 9000);
    $('gh-device').hidden = true;
  }
}

async function logoutGithub() {
  clearTimeout(state.pollTimer);
  try {
    const data = await withAdmin(() => API.post('/github/logout'));
    if (data) {
      state.repos = [];
      applyGithub(data);
      $('repo-select').innerHTML = '';
    }
  } catch (err) {
    toast(err.message, 'err');
  }
}

async function loadRepos(force = false) {
  if (!state.github?.authenticated) return;
  if (state.repos.length && !force) return;
  const select = $('repo-select');
  const onlyPrivate = $('repo-private-only').checked;
  select.innerHTML = '<option>Chargement…</option>';
  select.disabled = true;
  try {
    const { repos } = await withAdmin(
      () => API.get(`/github/repos?only_private=${onlyPrivate}`),
    ) || { repos: [] };
    state.repos = repos;
    if (!repos.length) {
      select.innerHTML = '<option value="">Aucun dépôt accessible</option>';
      $('publish-hint').textContent = onlyPrivate
        ? 'Aucun dépôt privé avec les issues activées n’est accessible à cette application GitHub.'
        : 'Aucun dépôt accessible.';
      return;
    }
    select.innerHTML = repos
      .map((r) => `<option value="${escapeHtml(r.full_name)}">
        ${escapeHtml(r.full_name)}${r.private ? ' 🔒' : ''}</option>`)
      .join('');
    select.disabled = false;
    $('repo-refresh').disabled = false;
    $('publish-hint').textContent = `${repos.length} dépôt(s) disponible(s).`;
  } catch (err) {
    select.innerHTML = '<option value="">Erreur de chargement</option>';
    $('publish-hint').textContent = err.message;
  } finally {
    updatePublishState();
  }
}

function updatePublishState() {
  const hasTunnel = state.tunnel?.state === 'running' && Boolean(state.tunnel.url);
  const authed = Boolean(state.github?.authenticated);
  const repo = $('repo-select').value;
  const ready = hasTunnel && authed && Boolean(repo);

  $('publish-btn').disabled = !ready;
  $('repo-refresh').disabled = !authed;

  if (!authed) setPill('publish-pill', 'muted', 'Connexion requise');
  else if (!hasTunnel) setPill('publish-pill', 'warn', 'Tunnel requis');
  else if (!repo) setPill('publish-pill', 'muted', 'Choisir un dépôt');
  else setPill('publish-pill', 'ok', 'Prêt');
}

async function publishDemo() {
  const repo = $('repo-select').value;
  if (!repo) return;
  const btn = $('publish-btn');
  btn.disabled = true;
  btn.textContent = 'Publication…';
  try {
    const result = await withAdmin(() => API.post('/github/publish', { repo }));
    if (result) {
      const verb = result.action === 'created' ? 'Issue créée' : 'Commentaire ajouté';
      toast(`${verb} dans ${result.repo} (#${result.number})`, 'ok', 7000);
      $('publish-result').hidden = false;
      $('publish-link').textContent = `${verb} — ${result.repo} #${result.number}`;
      $('publish-link').href = result.html_url;
    }
  } catch (err) {
    toast(err.message, 'err', 10000);
    $('publish-hint').textContent = err.message;
  } finally {
    btn.textContent = 'Publier';
    updatePublishState();
  }
}

/* ========================================================================== */
/* Sources & journal                                                          */
/* ========================================================================== */

function renderSources() {
  $('sources').innerHTML = state.sources.map((s) => `
    <div class="source-item">
      <div class="row">
        <b>${s.name}</b>
        <span class="pill ${s.available ? 'ok' : 'warn'}" style="font-size:10px">
          <i class="dot"></i>${s.available ? 'disponible' : 'indisponible'}</span>
      </div>
      <p>${s.description}</p>
      <div style="font-size:10.5px;color:var(--muted);margin-top:5px">
        Latence : ${s.latency}</div>
      ${s.credentials ? `<div style="font-size:10.5px;color:var(--ok);margin-top:4px">
        🔑 Identifiants lus depuis ${escapeHtml(s.credentials)}</div>` : ''}
      ${s.reason ? `<div class="reason">⚠ ${s.reason}</div>` : ''}
    </div>`).join('');
}

const TIME_FMT = new Intl.DateTimeFormat('fr-FR', {
  hour: '2-digit', minute: '2-digit', second: '2-digit',
});

function appendLog(data, ts) {
  const box = $('console');
  const line = document.createElement('div');
  line.className = `log-line ${data.level || 'info'}`;
  const time = TIME_FMT.format(new Date((ts || Date.now() / 1000) * 1000));
  line.innerHTML = `<span class="ts">${time}</span>
    <span class="src">${data.source || 'system'}</span>
    <span class="msg">${escapeHtml(data.message)}</span>`;
  box.appendChild(line);
  while (box.children.length > 600) box.removeChild(box.firstChild);
  if ($('autoscroll').checked) box.scrollTop = box.scrollHeight;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

boot().catch((err) => {
  console.error(err);
  toast(`Initialisation impossible : ${err.message}`, 'err', 12000);
});

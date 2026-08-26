/* Client HTTP + flux d'événements serveur. */

const TOKEN_KEY = 'aurora_admin_token';

export const token = {
  get: () => localStorage.getItem(TOKEN_KEY) || '',
  set: (value) => localStorage.setItem(TOKEN_KEY, value),
  clear: () => localStorage.removeItem(TOKEN_KEY),
};

function headers(extra = {}) {
  const value = token.get();
  return value ? { ...extra, 'X-Aurora-Token': value } : extra;
}

export const API = {
  async get(path) {
    const res = await fetch(`/api${path}`, { headers: headers() });
    if (!res.ok) throw new Error((await safeDetail(res)) || res.statusText);
    return res.json();
  },
  async post(path, body) {
    const res = await fetch(`/api${path}`, {
      method: 'POST',
      headers: headers({ 'Content-Type': 'application/json' }),
      body: JSON.stringify(body ?? {}),
    });
    if (!res.ok) throw new Error((await safeDetail(res)) || res.statusText);
    return res.json();
  },
  async del(path) {
    const res = await fetch(`/api${path}`, { method: 'DELETE', headers: headers() });
    if (!res.ok) throw new Error((await safeDetail(res)) || res.statusText);
    return res.json();
  },
};

async function safeDetail(res) {
  try {
    const data = await res.json();
    return data.detail || data.message || null;
  } catch {
    return null;
  }
}

/** Abonnement au flux SSE avec reconnexion automatique. */
export function subscribe(handlers) {
  let source = null;
  let retry = 1000;

  const connect = () => {
    source = new EventSource('/api/events');
    source.onopen = () => {
      retry = 1000;
      handlers.onOpen?.();
    };
    source.onmessage = (evt) => {
      let payload;
      try {
        payload = JSON.parse(evt.data);
      } catch {
        return;
      }
      handlers[payload.type]?.(payload.data, payload);
      handlers.onAny?.(payload);
    };
    source.onerror = () => {
      source.close();
      handlers.onClose?.();
      retry = Math.min(retry * 1.6, 15000);
      setTimeout(connect, retry);
    };
  };

  connect();
  return () => source?.close();
}

/** Décode un champ quantifié (uint16 base64) en Float32Array. */
export function decodeField(payload, stepIndex) {
  const b64 = payload.steps[stepIndex];
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  const u16 = new Uint16Array(bytes.buffer);
  const out = new Float32Array(u16.length);
  for (let i = 0; i < u16.length; i++) out[i] = payload.offset + u16[i] * payload.scale;
  return out;
}

export function decodeAll(payload) {
  return payload.steps.map((_, i) => decodeField(payload, i));
}

/* --- notifications -------------------------------------------------------- */

let toastWrap = null;

export function toast(message, kind = 'info', ttl = 4600) {
  if (!toastWrap) {
    toastWrap = document.createElement('div');
    toastWrap.className = 'toast-wrap';
    document.body.appendChild(toastWrap);
  }
  const el = document.createElement('div');
  el.className = `toast ${kind}`;
  el.textContent = message;
  toastWrap.appendChild(el);
  setTimeout(() => {
    el.style.transition = 'opacity .3s, transform .3s';
    el.style.opacity = '0';
    el.style.transform = 'translateY(8px)';
    setTimeout(() => el.remove(), 320);
  }, ttl);
}

/* --- formatage ------------------------------------------------------------ */

const DTF = new Intl.DateTimeFormat('fr-FR', {
  weekday: 'short', day: 'numeric', month: 'short',
  hour: '2-digit', minute: '2-digit', timeZone: 'Europe/Paris',
});
const DTF_SHORT = new Intl.DateTimeFormat('fr-FR', {
  hour: '2-digit', minute: '2-digit', timeZone: 'Europe/Paris',
});
const DTF_DAY = new Intl.DateTimeFormat('fr-FR', {
  weekday: 'short', day: 'numeric', timeZone: 'Europe/Paris',
});

export const fmt = {
  full: (iso) => DTF.format(new Date(iso)),
  hour: (iso) => DTF_SHORT.format(new Date(iso)),
  day: (iso) => DTF_DAY.format(new Date(iso)),
  hourNum: (iso) => Number(
    new Intl.DateTimeFormat('fr-FR', { hour: '2-digit', hour12: false, timeZone: 'Europe/Paris' })
      .format(new Date(iso)),
  ),
  num: (value, decimals = 0) =>
    value === null || value === undefined || Number.isNaN(value)
      ? '—'
      : value.toFixed(decimals),
  bytes: (mb) => (mb >= 1024 ? `${(mb / 1024).toFixed(1)} Go` : `${Math.round(mb)} Mo`),
  /** Énergie, avec descente jusqu'au µWh : une inférence courte consomme peu. */
  energy: (wh) => {
    if (wh === null || wh === undefined) return '—';
    const mwh = wh * 1000;
    if (wh >= 1000) return `${(wh / 1000).toFixed(2)} kWh`;
    if (wh >= 1) return `${wh.toFixed(2)} Wh`;
    if (mwh >= 10) return `${mwh.toFixed(0)} mWh`;
    if (mwh >= 0.1) return `${mwh.toFixed(2)} mWh`;
    return `${Math.round(wh * 1e6)} µWh`;
  },
  /** Empreinte carbone, jusqu'au µgCO₂e (réseau français très peu carboné). */
  co2: (grams) => {
    if (grams === null || grams === undefined) return '—';
    const mg = grams * 1000;
    if (grams >= 1000) return `${(grams / 1000).toFixed(2)} kgCO₂e`;
    if (grams >= 1) return `${grams.toFixed(2)} gCO₂e`;
    if (mg >= 10) return `${mg.toFixed(0)} mgCO₂e`;
    if (mg >= 0.1) return `${mg.toFixed(2)} mgCO₂e`;
    return `${Math.round(grams * 1e6)} µgCO₂e`;
  },
  duration: (s) => {
    if (s == null) return '—';
    if (s < 1) return `${Math.round(s * 1000)} ms`;
    if (s < 10) return `${s.toFixed(1)} s`;
    if (s < 60) return `${Math.round(s)} s`;
    if (s < 3600) return `${Math.floor(s / 60)} min ${Math.round(s % 60)} s`;
    return `${Math.floor(s / 3600)} h ${Math.floor((s % 3600) / 60)} min`;
  },
};

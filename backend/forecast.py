"""Exécution des prévisions : file de travaux, extraction France, sérialisation."""

from __future__ import annotations

import base64
import math
import threading
import time
import uuid
from collections import OrderedDict
from datetime import datetime, timedelta, timezone

import numpy as np

from .config import BASE_TIMESTEP_H, LAT_MAX, LAT_MIN, LON_MAX, LON_MIN, MAX_STEPS
from .energy import EnergyMeter, log_result
from .events import bus
from .geo import CITIES, france_mask, render_grid
from .model_manager import manager
from .registry import MODELS_BY_ID, VARIABLES
from . import storage

# Nombre de jeux de champs gardés en mémoire vive. Les autres restent sur disque
# et sont rechargés à la demande.
_MAX_CACHED = 4
_forecasts: OrderedDict[str, dict] = OrderedDict()
_jobs: OrderedDict[str, dict] = OrderedDict()
_lock = threading.Lock()

VECTOR_KEYS = ("u10", "v10")


# ---------------------------------------------------------------------------
# Sérialisation compacte (quantification 16 bits)
# ---------------------------------------------------------------------------


def quantize(stack: np.ndarray) -> dict:
    """Quantifie un tableau (steps, ny, nx) en uint16 base64."""
    arr = np.asarray(stack, dtype=np.float32)
    finite = np.isfinite(arr)
    vmin = float(arr[finite].min()) if finite.any() else 0.0
    vmax = float(arr[finite].max()) if finite.any() else 1.0
    if not math.isfinite(vmin) or not math.isfinite(vmax) or vmax - vmin < 1e-9:
        vmax = vmin + 1.0
    scale = (vmax - vmin) / 65534.0
    q = np.clip(np.rint((np.nan_to_num(arr, nan=vmin) - vmin) / scale), 0, 65534).astype("<u2")
    return {
        "min": vmin,
        "max": vmax,
        "scale": scale,
        "offset": vmin,
        "steps": [base64.b64encode(q[i].tobytes()).decode("ascii") for i in range(q.shape[0])],
    }


# ---------------------------------------------------------------------------
# Extraction depuis un Batch Aurora
# ---------------------------------------------------------------------------


def _crop_indices(lat: np.ndarray, lon: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    lat_idx = np.where((lat <= LAT_MAX + 1e-6) & (lat >= LAT_MIN - 1e-6))[0]
    lon180 = ((lon + 180.0) % 360.0) - 180.0
    lon_idx = np.where((lon180 >= LON_MIN - 1e-6) & (lon180 <= LON_MAX + 1e-6))[0]
    order = np.argsort(lon180[lon_idx])
    return lat_idx, lon_idx[order]


def _rh_from_q(q: np.ndarray, t_c: np.ndarray, p_pa: np.ndarray) -> np.ndarray:
    e = q * p_pa / (0.622 + 0.378 * np.maximum(q, 1e-9))
    es = 611.2 * np.exp(17.67 * t_c / (t_c + 243.5))
    return np.clip(100.0 * e / np.maximum(es, 1e-6), 0.0, 100.0)


def extract_france(pred, lat_idx, lon_idx, levels: tuple[int, ...]) -> dict[str, np.ndarray]:
    def surf(name: str):
        t = pred.surf_vars.get(name)
        if t is None:
            return None
        a = t.detach().float().numpy()
        a = a[0, -1] if a.ndim == 4 else a[0]
        return a[np.ix_(lat_idx, lon_idx)]

    def atmos(name: str, level: int):
        t = pred.atmos_vars.get(name)
        if t is None or level not in levels:
            return None
        k = levels.index(level)
        a = t.detach().float().numpy()
        a = a[0, -1, k] if a.ndim == 5 else a[0, k]
        return a[np.ix_(lat_idx, lon_idx)]

    t2 = surf("2t")
    u10 = surf("10u")
    v10 = surf("10v")
    msl = surf("msl")

    out: dict[str, np.ndarray] = {}
    if t2 is not None:
        out["2t"] = t2 - 273.15
    if u10 is not None and v10 is not None:
        speed = np.hypot(u10, v10)
        out["u10"] = u10
        out["v10"] = v10
        out["wind10"] = speed * 3.6
        out["gust"] = speed * 3.6 * 1.55
    if msl is not None:
        out["msl"] = msl / 100.0

    t850 = atmos("t", 850)
    if t850 is not None:
        out["t850"] = t850 - 273.15
    z500 = atmos("z", 500)
    if z500 is not None:
        out["z500"] = z500 / 9.80665 / 10.0

    q1000 = atmos("q", 1000)
    if q1000 is not None and "2t" in out and msl is not None:
        out["rh"] = _rh_from_q(q1000, out["2t"], msl)

    for name, key, factor in (("tcc", "tcc", 100.0), ("tp", "precip", 1000.0),
                              ("scaled_tp_1h", "precip", 1000.0)):
        val = surf(name)
        if val is not None and key not in out:
            out[key] = np.clip(val * factor, 0.0, None)

    gust = surf("i10fg")
    if gust is not None:
        out["gust"] = gust * 3.6
    return out


# ---------------------------------------------------------------------------
# Travaux
# ---------------------------------------------------------------------------


def _job_event(job: dict) -> None:
    bus.emit("job", {k: v for k, v in job.items() if k != "_thread"})


def create_job(params: dict) -> dict:
    job_id = uuid.uuid4().hex[:12]
    job = {
        "id": job_id,
        "status": "queued",
        "progress": 0.0,
        "message": "En file d'attente",
        "params": params,
        "created": time.time(),
        "forecast_id": None,
        "error": None,
        "elapsed": None,
    }
    with _lock:
        _jobs[job_id] = job
        while len(_jobs) > 20:
            _jobs.popitem(last=False)
    threading.Thread(target=_run_job, args=(job_id,), daemon=True).start()
    _job_event(job)
    return job


def get_job(job_id: str) -> dict | None:
    return _jobs.get(job_id)


def get_forecast(forecast_id: str) -> dict | None:
    """Prévision complète, depuis la mémoire ou rechargée depuis le disque."""
    if not storage.valid_id(forecast_id):
        return None
    with _lock:
        cached = _forecasts.get(forecast_id)
        if cached is not None:
            _forecasts.move_to_end(forecast_id)
            return cached

    loaded = storage.load(forecast_id)
    if loaded is None:
        return None
    with _lock:
        _forecasts[forecast_id] = loaded
        while len(_forecasts) > _MAX_CACHED:
            _forecasts.popitem(last=False)
    return loaded


def list_forecasts() -> list[dict]:
    """Index de l'historique persistant."""
    return storage.index()


def delete_forecast(forecast_id: str) -> bool:
    if not storage.valid_id(forecast_id):
        return False
    with _lock:
        _forecasts.pop(forecast_id, None)
    return storage.delete(forecast_id)


def delete_all_forecasts() -> int:
    with _lock:
        _forecasts.clear()
    return storage.delete_all()


def _update(job: dict, **kwargs) -> None:
    job.update(kwargs)
    _job_event(job)


def _run_job(job_id: str) -> None:
    job = _jobs[job_id]
    params = job["params"]
    started = time.time()
    holder = None
    try:
        holder = manager.acquire()
        _update(job, status="running", progress=0.02, message="Préparation")

        meter = EnergyMeter(manager.device)
        with meter:
            if holder["engine"] == "simulation":
                result = _run_simulation(job)
            else:
                result = _run_aurora(job, holder)

        energy = meter.result()
        result["meta"]["energy"] = energy
        log_result(energy)

        storage.save(result)
        with _lock:
            _forecasts[result["meta"]["id"]] = result
            while len(_forecasts) > _MAX_CACHED:
                _forecasts.popitem(last=False)

        elapsed = round(time.time() - started, 2)
        manager.note_inference(
            {
                "forecast_id": result["meta"]["id"],
                "steps": result["meta"]["steps"],
                "seconds": elapsed,
                "at": time.time(),
            }
        )
        _update(
            job,
            status="done",
            progress=1.0,
            message=f"Prévision produite en {elapsed} s",
            forecast_id=result["meta"]["id"],
            elapsed=elapsed,
        )
        bus.log(
            f"Prévision {result['meta']['id']} : {result['meta']['steps']} échéances en {elapsed} s",
            level="success",
            source="forecast",
        )
    except Exception as exc:  # noqa: BLE001
        _update(job, status="error", error=str(exc), message=str(exc), progress=0.0)
        bus.log(f"Échec de la prévision : {exc}", level="error", source="forecast")
    finally:
        if holder is not None:
            manager.release()


# ---------------------------------------------------------------------------
# Moteur simulation
# ---------------------------------------------------------------------------


def _run_simulation(job: dict) -> dict:
    from .simulation import SyntheticAtmosphere  # noqa: PLC0415

    params = job["params"]
    base_time = params["base_time"]
    steps = params["steps"]
    members = max(1, min(int(params.get("members", 1)), 8))
    step_h = params.get("step_hours", BASE_TIMESTEP_H)

    lats, lons = render_grid()
    leads = [i * step_h for i in range(steps + 1)]

    control = SyntheticAtmosphere(base_time, member=0)
    fields: dict[str, list[np.ndarray]] = {}
    member_series: list[dict[str, np.ndarray]] = []

    engines = [control] + [SyntheticAtmosphere(base_time, member=m) for m in range(1, members)]
    city_idx = _city_indices(lats, lons)

    total = len(leads) * members
    done = 0
    per_member_points: list[dict[str, list[np.ndarray]]] = [{} for _ in engines]

    for mi, engine in enumerate(engines):
        for lead in leads:
            f = engine.fields(lead)
            if mi == 0:
                for key, arr in f.items():
                    fields.setdefault(key, []).append(arr)
            for key, arr in f.items():
                per_member_points[mi].setdefault(key, []).append(arr[city_idx])
            done += 1
            _update(
                job,
                progress=0.05 + 0.9 * done / total,
                message=f"Membre {mi + 1}/{members} — échéance +{lead:.0f} h",
            )

    for mi in range(len(engines)):
        member_series.append({k: np.stack(v) for k, v in per_member_points[mi].items()})

    stacked = {k: np.stack(v) for k, v in fields.items()}
    return _package(job, stacked, member_series, lats, lons, leads, base_time, step_h)


# ---------------------------------------------------------------------------
# Moteur Aurora
# ---------------------------------------------------------------------------


def _run_aurora(job: dict, holder: dict) -> dict:
    import torch  # noqa: PLC0415
    from aurora import rollout  # noqa: PLC0415

    from .data_sources import fetch_era5_batch  # noqa: PLC0415

    params = job["params"]
    base_time = params["base_time"]
    steps = params["steps"]
    meta = holder["meta"]
    step_h = meta.get("timestep_h", BASE_TIMESTEP_H)
    source = params.get("source", "era5_cds")

    if meta["id"] in ("aurora-0.4-air-pollution", "aurora-0.25-wave"):
        raise RuntimeError(
            f"« {meta['name']} » exige des conditions initiales spécifiques "
            "(CAMS analysis ou HRES-WAM), non fournies par cette station. "
            "Utilisez une version météorologique d'Aurora avec ERA5."
        )

    if source != "era5_cds":
        raise RuntimeError(
            "Le modèle Aurora nécessite des conditions initiales réelles. "
            "Sélectionnez la source ERA5 (Copernicus CDS)."
        )

    _update(job, progress=0.05, message="Téléchargement des conditions initiales ERA5")
    batch = fetch_era5_batch(base_time, meta)

    lat = batch.metadata.lat.numpy()
    lon = batch.metadata.lon.numpy()
    lat_idx, lon_idx = _crop_indices(lat, lon)
    levels = tuple(int(x) for x in batch.metadata.atmos_levels)
    lats = lat[lat_idx]
    lon180 = ((lon + 180.0) % 360.0) - 180.0
    lons = lon180[lon_idx]

    device = holder["device"]
    model = holder["model"]

    _update(job, progress=0.12, message=f"Inférence sur {device}")
    leads: list[float] = [0.0]
    per_step: list[dict[str, np.ndarray]] = [extract_france(batch, lat_idx, lon_idx, levels)]

    with torch.inference_mode():
        for i, pred in enumerate(rollout(model, batch, steps=steps)):
            pred_cpu = pred.to("cpu")
            per_step.append(extract_france(pred_cpu, lat_idx, lon_idx, levels))
            leads.append((i + 1) * step_h)
            _update(
                job,
                progress=0.12 + 0.85 * (i + 1) / steps,
                message=f"Échéance +{(i + 1) * step_h} h ({i + 1}/{steps})",
            )
            del pred, pred_cpu

    # Certaines variables (précipitations, rafales d'Aurora 1.5) sont produites en
    # sortie mais absentes de l'analyse : on les complète par un pas nul plutôt que
    # de tronquer toute la série, ce qui décalerait les échéances.
    stacked: dict[str, np.ndarray] = {}
    for var in set().union(*(set(step) for step in per_step)):
        if all(var in step for step in per_step):
            stacked[var] = np.stack([step[var] for step in per_step])
        elif all(var in step for step in per_step[1:]):
            zero = np.zeros_like(per_step[1][var])
            stacked[var] = np.stack([zero] + [step[var] for step in per_step[1:]])
        else:
            bus.log(
                f"Variable « {var} » incomplète sur la séquence, ignorée",
                level="warn",
                source="forecast",
            )

    city_idx = _city_indices(lats, lons)
    member_series = [{k: v[:, city_idx[0], city_idx[1]] for k, v in stacked.items()}]
    return _package(job, stacked, member_series, lats, lons, leads, base_time, step_h)


# ---------------------------------------------------------------------------
# Mise en forme du résultat
# ---------------------------------------------------------------------------


def _city_indices(lats: np.ndarray, lons: np.ndarray):
    ii = [int(np.argmin(np.abs(lats - c["lat"]))) for c in CITIES]
    jj = [int(np.argmin(np.abs(lons - c["lon"]))) for c in CITIES]
    return (np.array(ii), np.array(jj))


def _package(
    job: dict,
    fields: dict[str, np.ndarray],
    member_series: list[dict[str, np.ndarray]],
    lats: np.ndarray,
    lons: np.ndarray,
    leads: list[float],
    base_time: datetime,
    step_h: float,
) -> dict:
    forecast_id = uuid.uuid4().hex[:12]
    params = job["params"]
    model_meta = MODELS_BY_ID.get(manager.model_id or "", {})

    lat2d, lon2d = np.meshgrid(lats, lons, indexing="ij")
    mask = france_mask(lat2d, lon2d)

    available = [v for v in VARIABLES if v in fields]

    stats = {}
    for var in available:
        arr = fields[var]
        inside = arr[:, mask] if mask.any() else arr.reshape(arr.shape[0], -1)
        stats[var] = {
            "min": float(np.nanmin(inside)),
            "max": float(np.nanmax(inside)),
            "mean": float(np.nanmean(inside)),
        }

    valid_times = [(base_time + timedelta(hours=float(h))).replace(tzinfo=timezone.utc).isoformat()
                   for h in leads]

    cities_payload = []
    for k, city in enumerate(CITIES):
        series: dict[str, list] = {}
        for var in available:
            if var not in member_series[0]:
                continue
            values = np.stack([m[var][:, k] for m in member_series if var in m])
            series[var] = [round(float(x), 2) for x in values[0]]
            if values.shape[0] > 1:
                series[var + "_lo"] = [round(float(x), 2) for x in np.percentile(values, 10, axis=0)]
                series[var + "_hi"] = [round(float(x), 2) for x in np.percentile(values, 90, axis=0)]
        cities_payload.append({**city, "series": series})

    meta = {
        "id": forecast_id,
        "job_id": job["id"],
        "model_id": manager.model_id,
        "model_name": model_meta.get("name"),
        "engine": model_meta.get("engine"),
        "real_data": bool(model_meta.get("real_data")) and params.get("source") != "synthetic",
        "source": params.get("source"),
        "device": manager.device,
        "members": len(member_series),
        "base_time": base_time.replace(tzinfo=timezone.utc).isoformat(),
        "step_hours": step_h,
        "steps": len(leads) - 1,
        "lead_hours": [float(h) for h in leads],
        "valid_times": valid_times,
        "created": time.time(),
        "grid": {
            "lat0": float(lats[0]),
            "lon0": float(lons[0]),
            "dlat": float(lats[1] - lats[0]) if len(lats) > 1 else 0.0,
            "dlon": float(lons[1] - lons[0]) if len(lons) > 1 else 0.0,
            "ny": int(len(lats)),
            "nx": int(len(lons)),
        },
        "variables": {v: {**VARIABLES[v], "stats": stats[v]} for v in available},
    }

    return {"meta": meta, "fields": fields, "cities": cities_payload, "mask": mask}


def field_payload(forecast_id: str, var: str) -> dict | None:
    fc = get_forecast(forecast_id)
    if fc is None or var not in fc["fields"]:
        return None
    payload = quantize(fc["fields"][var])
    payload.update({"var": var, "grid": fc["meta"]["grid"], "unit": VARIABLES[var]["unit"]})
    return payload


def wind_payload(forecast_id: str) -> dict | None:
    fc = get_forecast(forecast_id)
    if fc is None or "u10" not in fc["fields"] or "v10" not in fc["fields"]:
        return None
    return {
        "u": quantize(fc["fields"]["u10"]),
        "v": quantize(fc["fields"]["v10"]),
        "grid": fc["meta"]["grid"],
    }


def point_series(forecast_id: str, lat: float, lon: float) -> dict | None:
    fc = get_forecast(forecast_id)
    if fc is None:
        return None
    g = fc["meta"]["grid"]
    i = int(round((lat - g["lat0"]) / g["dlat"])) if g["dlat"] else 0
    j = int(round((lon - g["lon0"]) / g["dlon"])) if g["dlon"] else 0
    i = max(0, min(g["ny"] - 1, i))
    j = max(0, min(g["nx"] - 1, j))
    series = {
        var: [round(float(x), 2) for x in arr[:, i, j]]
        for var, arr in fc["fields"].items()
        if var in VARIABLES
    }
    return {
        "lat": g["lat0"] + i * g["dlat"],
        "lon": g["lon0"] + j * g["dlon"],
        "series": series,
        "lead_hours": fc["meta"]["lead_hours"],
        "valid_times": fc["meta"]["valid_times"],
    }


def default_base_time() -> datetime:
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0, tzinfo=None)
    return now - timedelta(hours=now.hour % BASE_TIMESTEP_H)


def clamp_steps(steps: int) -> int:
    return max(1, min(int(steps), MAX_STEPS))

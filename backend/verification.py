"""Vérification des prévisions : confrontation à l'analyse ERA5 et notation.

Chaque prévision enregistrée est comparée, aux mêmes dates de validité, à la
réanalyse ERA5 échantillonnée sur les villes de référence. Le module produit :

  * une note globale par prévision (0 à 100) ;
  * une note par journée d'échéance (J+1, J+2, …) ;
  * l'erreur détaillée par variable, par échéance et par ville ;
  * le gain par rapport à la persistance (« demain = aujourd'hui »).

ERA5 sert de vérité terrain : c'est la source des conditions initiales et
l'étalon employé par Microsoft pour évaluer Aurora. La réanalyse accuse environ
cinq jours de délai de publication : les échéances plus récentes restent donc
non vérifiables tant que les champs correspondants ne sont pas disponibles.
"""

from __future__ import annotations

import json
import shutil
import threading
import time
from collections import OrderedDict
from datetime import date, datetime, timedelta, timezone
from pathlib import PurePosixPath

import numpy as np

from . import storage
from .config import CACHE_DIR, ERA5_LATENCY_DAYS, FORECAST_DIR, LAT_MAX, LAT_MIN, LON_MAX, LON_MIN
from .data_sources import ERA5_DIR, _cds_client, _retrieve, cds_configured
from .events import bus
from .forecast import _rh_from_q
from .geo import CITIES
from .registry import VARIABLES

VERIF_DIR = CACHE_DIR / "era5_verif"
VERIF_DIR.mkdir(parents=True, exist_ok=True)

REPORT_NAME = "verification.json"

# Emprise transmise au CDS : [nord, ouest, sud, est].
AREA = [LAT_MAX, LON_MIN, LAT_MIN, LON_MAX]

# ---------------------------------------------------------------------------
# Barème
# ---------------------------------------------------------------------------

# `tol` : erreur absolue moyenne qui vaut exactement 50/100. C'est l'ordre de
# grandeur de l'erreur d'un bon modèle global à trois ou quatre jours d'échéance.
# `weight` : part de la variable dans la note globale (somme = 1).
SCALES: dict[str, dict[str, float]] = {
    "2t": {"tol": 1.5, "weight": 0.22},
    "wind10": {"tol": 7.0, "weight": 0.16},
    "msl": {"tol": 2.5, "weight": 0.16},
    "rh": {"tol": 10.0, "weight": 0.10},
    "t850": {"tol": 1.5, "weight": 0.10},
    "z500": {"tol": 3.0, "weight": 0.08},
    "tcc": {"tol": 22.0, "weight": 0.06},
    "gust": {"tol": 12.0, "weight": 0.06},
    "precip": {"tol": 0.8, "weight": 0.06},
}

GRADES = ((88.0, "excellent"), (75.0, "très bon"), (60.0, "bon"), (45.0, "moyen"))


def grade(score: float | None) -> str:
    if score is None:
        return "—"
    for threshold, label in GRADES:
        if score >= threshold:
            return label
    return "faible"


def _score(var: str, mae: float) -> float:
    """Note 0–100 décroissant de moitié à chaque tolérance d'erreur franchie."""
    tol = SCALES[var]["tol"]
    return round(100.0 * 0.5 ** (mae / tol), 1)


def _weighted(detail: dict[str, dict]) -> float | None:
    """Note d'ensemble : moyenne pondérée par l'importance météorologique.

    Le poids est modulé par le nombre de comparaisons réellement disponibles :
    une variable observée sur une seule journée ne doit pas peser autant qu'une
    variable suivie sur toute la séquence.
    """
    usable = {v: d for v, d in detail.items() if d.get("score") is not None and d.get("n")}
    if not usable:
        return None
    reference = max(d["n"] for d in usable.values())
    weights = {v: SCALES[v]["weight"] * d["n"] / reference for v, d in usable.items()}
    total = sum(weights.values())
    if total <= 0:
        return None
    return round(sum(usable[v]["score"] * w for v, w in weights.items()) / total, 1)


# ---------------------------------------------------------------------------
# Vérité terrain ERA5
# ---------------------------------------------------------------------------

_SURFACE_REQUEST = [
    "2m_temperature",
    "10m_u_component_of_wind",
    "10m_v_component_of_wind",
    "mean_sea_level_pressure",
    "total_cloud_cover",
    "total_precipitation",
    "instantaneous_10m_wind_gust",
]

# Noms courts possibles dans les fichiers NetCDF du CDS (ils ont varié).
_RAW_SURFACE = {
    "2t": ("t2m", "2t"),
    "u10": ("u10", "10u"),
    "v10": ("v10", "10v"),
    "msl": ("msl",),
    "tcc": ("tcc",),
    "tp": ("tp",),
    "i10fg": ("i10fg", "fg10", "10fg"),
}

_LEVELS_REQUEST = ["temperature", "specific_humidity", "geopotential"]
_WANTED_LEVELS = [500, 850, 1000]

_ALL_HOURS = ["00:00", "06:00", "12:00", "18:00"]

# Journées déjà extraites pendant la session : la lecture des fichiers globaux
# (plusieurs centaines de mégaoctets) ne doit pas être refaite par prévision.
_truth_cache: OrderedDict[str, dict] = OrderedDict()
_truth_lock = threading.RLock()


def latest_truth_day() -> date:
    """Dernière journée pour laquelle ERA5 est censée être publiée."""
    return (datetime.now(timezone.utc) - timedelta(days=ERA5_LATENCY_DAYS)).date()


def _key(moment: datetime | str) -> str:
    """Clé de comparaison des instants, à la minute et en UTC."""
    if isinstance(moment, str):
        moment = datetime.fromisoformat(moment.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M")


def _city_indices(lat: np.ndarray, lon: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    lon180 = ((lon + 180.0) % 360.0) - 180.0
    ii = [int(np.argmin(np.abs(lat - c["lat"]))) for c in CITIES]
    jj = [int(np.argmin(np.abs(lon180 - c["lon"]))) for c in CITIES]
    return np.array(ii), np.array(jj)


def _sources(day: date, download: bool) -> tuple | None:
    """Fichiers (surface, niveaux) utilisables pour une journée.

    Trois origines, dans l'ordre : l'extrait France dédié à la vérification, les
    fichiers globaux déjà téléchargés pour les conditions initiales, puis un
    nouveau téléchargement — restreint à la France, donc mille fois plus léger.
    """
    tag = day.strftime("%Y-%m-%d")
    narrow = (VERIF_DIR / f"{tag}-surface.nc", VERIF_DIR / f"{tag}-levels.nc")
    if all(p.exists() for p in narrow):
        return narrow

    wide_surface = next(
        (
            p
            for p in (ERA5_DIR / f"{tag}-surface-v1p5.nc", ERA5_DIR / f"{tag}-surface.nc")
            if p.exists()
        ),
        None,
    )
    wide_atmos = ERA5_DIR / f"{tag}-atmospheric.nc"
    if wide_surface is not None and wide_atmos.exists():
        return wide_surface, wide_atmos

    if not download:
        return None
    return _download(day)


def _download(day: date) -> tuple:
    tag = day.strftime("%Y-%m-%d")
    client = _cds_client()
    common = {
        "product_type": "reanalysis",
        "year": day.strftime("%Y"),
        "month": day.strftime("%m"),
        "day": day.strftime("%d"),
        "time": _ALL_HOURS,
        "data_format": "netcdf",
        "area": AREA,
    }
    surface = _retrieve(
        client,
        "reanalysis-era5-single-levels",
        {**common, "variable": _SURFACE_REQUEST},
        VERIF_DIR / f"{tag}-surface.nc",
    )
    levels = _retrieve(
        client,
        "reanalysis-era5-pressure-levels",
        {
            **common,
            "variable": _LEVELS_REQUEST,
            "pressure_level": [str(p) for p in _WANTED_LEVELS],
        },
        VERIF_DIR / f"{tag}-levels.nc",
    )
    return surface, levels


def _times(dataset) -> np.ndarray:
    for name in ("valid_time", "time"):
        if name in dataset.variables or name in dataset.coords:
            return np.asarray(dataset[name].values).reshape(-1)
    raise RuntimeError("ERA5 : coordonnée temporelle introuvable")


def _levels(dataset) -> np.ndarray | None:
    for name in ("pressure_level", "level", "isobaricInhPa"):
        if name in dataset.variables or name in dataset.coords:
            return np.asarray(dataset[name].values, dtype=float).reshape(-1)
    return None


def _open_all(path) -> list:
    """Ouvre un fichier ERA5, livré brut ou en archive par le CDS.

    Dès qu'une requête mêle champs instantanés et champs cumulés (les
    précipitations), le CDS répond par une archive ZIP contenant un NetCDF par
    type de pas de temps. Les membres sont extraits une fois à côté de l'archive.
    """
    import zipfile  # noqa: PLC0415

    import xarray as xr  # noqa: PLC0415

    if not zipfile.is_zipfile(path):
        return [xr.open_dataset(path, engine="netcdf4")]

    parts = path.with_suffix(".parts")
    if not parts.is_dir():
        parts.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path) as archive:
            for member in archive.namelist():
                # Extraction sélective : jamais de chemin fourni par l'archive.
                name = PurePosixPath(member).name
                if not name.endswith(".nc") or name != member:
                    continue
                with archive.open(member) as source, (parts / name).open("wb") as target:
                    shutil.copyfileobj(source, target)

    members = sorted(parts.glob("*.nc"))
    if not members:
        raise RuntimeError(f"ERA5 : archive {path.name} sans fichier NetCDF exploitable")
    return [xr.open_dataset(m, engine="netcdf4") for m in members]


def _sample(datasets: list, names: tuple[str, ...], stamps, ii, jj, level: int | None = None):
    """Série d'une variable aux instants demandés, échantillonnée sur les villes."""
    for dataset in datasets:
        for name in names:
            if name not in dataset.variables:
                continue
            field = dataset[name]
            if level is not None:
                plevels = _levels(dataset)
                if plevels is None or level not in plevels:
                    continue
                dim = next(
                    (d for d in ("pressure_level", "level", "isobaricInhPa") if d in field.dims),
                    None,
                )
                if dim is None:
                    continue
                field = field.isel({dim: int(np.argmin(np.abs(plevels - level)))})
            values = np.asarray(field.values, dtype=np.float32)
            values = values.reshape((-1,) + values.shape[-2:])
            own = list(_times(dataset))
            rows = [
                values[own.index(stamp)][ii, jj]
                if stamp in own
                else np.full(len(ii), np.nan, dtype=np.float32)
                for stamp in stamps
            ]
            return np.asarray(rows)
    return None


def _day_truth(day: date, download: bool) -> dict[str, dict[str, list[float]]]:
    """Observations ERA5 d'une journée, par instant puis par variable."""
    tag = day.strftime("%Y-%m-%d")
    with _truth_lock:
        cached = _truth_cache.get(tag)
        if cached is not None:
            _truth_cache.move_to_end(tag)
            return cached

    sources = _sources(day, download)
    if sources is None:
        return {}

    surface_path, levels_path = sources
    surface = _open_all(surface_path)
    levels = _open_all(levels_path)
    try:
        stamps = list(_times(surface[0]))
        ii, jj = _city_indices(
            np.asarray(surface[0].latitude.values, dtype=float),
            np.asarray(surface[0].longitude.values, dtype=float),
        )

        def at_surface(key: str):
            return _sample(surface, _RAW_SURFACE[key], stamps, ii, jj)

        def at_level(name: str, hpa: int):
            return _sample(levels, (name,), stamps, ii, jj, level=hpa)

        values: dict[str, np.ndarray] = {}
        t2 = at_surface("2t")
        msl = at_surface("msl")
        u10, v10 = at_surface("u10"), at_surface("v10")
        if t2 is not None:
            values["2t"] = t2 - 273.15
        if msl is not None:
            values["msl"] = msl / 100.0
        if u10 is not None and v10 is not None:
            values["wind10"] = np.hypot(u10, v10) * 3.6
        gust = at_surface("i10fg")
        if gust is not None:
            values["gust"] = gust * 3.6
        tcc = at_surface("tcc")
        if tcc is not None:
            values["tcc"] = np.clip(tcc * 100.0, 0.0, 100.0)
        precip = at_surface("tp")
        if precip is not None:
            values["precip"] = np.clip(precip * 1000.0, 0.0, None)

        t850 = at_level("t", 850)
        if t850 is not None:
            values["t850"] = t850 - 273.15
        z500 = at_level("z", 500)
        if z500 is not None:
            values["z500"] = z500 / 9.80665 / 10.0
        q1000 = at_level("q", 1000)
        if q1000 is not None and "2t" in values and msl is not None:
            values["rh"] = _rh_from_q(q1000, values["2t"], msl)
    finally:
        for dataset in (*surface, *levels):
            dataset.close()

    # Une variable n'est retenue à un instant donné que si toutes les villes sont
    # renseignées : une valeur manquante fausserait silencieusement la note.
    truth = {}
    for k, stamp in enumerate(stamps):
        observed = {
            var: [round(float(x), 3) for x in arr[k]]
            for var, arr in values.items()
            if np.all(np.isfinite(arr[k]))
        }
        if observed:
            truth[_key(str(np.datetime64(stamp, "s")))] = observed

    with _truth_lock:
        _truth_cache[tag] = truth
        while len(_truth_cache) > 10:
            _truth_cache.popitem(last=False)
    return truth



def truth_coverage() -> dict:
    """Journées ERA5 déjà disponibles hors ligne, par origine."""
    narrow = sorted({p.name[:10] for p in VERIF_DIR.glob("*-surface.nc")})
    wide = sorted(
        {
            p.name[:10]
            for p in ERA5_DIR.glob("*-surface*.nc")
            if (ERA5_DIR / f"{p.name[:10]}-atmospheric.nc").exists()
        }
    )
    return {
        "verification_days": narrow,
        "initial_condition_days": wide,
        "latest_available": latest_truth_day().isoformat(),
        "cds_configured": cds_configured(),
    }


# ---------------------------------------------------------------------------
# Notation d'une prévision
# ---------------------------------------------------------------------------


def _errors(pred: np.ndarray, obs: np.ndarray) -> dict | None:
    """Écarts sur les seuls couples renseignés (une journée ERA5 peut manquer)."""
    err = np.asarray(pred, dtype=float) - np.asarray(obs, dtype=float)
    err = err[np.isfinite(err)]
    if not err.size:
        return None
    return {
        "mae": round(float(np.mean(np.abs(err))), 3),
        "rmse": round(float(np.sqrt(np.mean(err**2))), 3),
        "bias": round(float(np.mean(err)), 3),
        "n": int(err.size),
    }


def _detail(pred: dict[str, np.ndarray], obs: dict[str, np.ndarray], rows) -> dict:
    out = {}
    for var, values in pred.items():
        stats = _errors(values[rows], obs[var][rows])
        if stats is not None:
            out[var] = {**stats, "score": _score(var, stats["mae"])}
    return out


def _report_path(forecast_id: str):
    return FORECAST_DIR / forecast_id / REPORT_NAME


def load_report(forecast_id: str) -> dict | None:
    if not storage.valid_id(forecast_id):
        return None
    try:
        return json.loads(_report_path(forecast_id).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _write_report(report: dict) -> None:
    path = _report_path(report["id"])
    if not path.parent.is_dir():
        return
    path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")


def evaluate(forecast_id: str, download: bool = False) -> dict:
    """Compare une prévision à ERA5 et produit son rapport de notation."""
    payload = storage.read_payload(forecast_id)
    if payload is None:
        raise KeyError(f"Prévision inconnue : {forecast_id}")

    meta = payload["meta"]
    cities = payload["cities"]
    base = {
        "id": forecast_id,
        "model_id": meta.get("model_id"),
        "model_name": meta.get("model_name"),
        "base_time": meta.get("base_time"),
        "step_hours": meta.get("step_hours"),
        "steps": meta.get("steps"),
        "source": meta.get("source"),
        "real_data": bool(meta.get("real_data")),
        "created": meta.get("created"),
        "computed": time.time(),
        "score": None,
        "grade": "—",
        "variables": {},
        "days": [],
        "leads": [],
        "city_scores": [],
        "observed": [],
        "valid": [],
        "lead_hours": [],
    }

    if not meta.get("real_data"):
        return {
            **base,
            "status": "skipped",
            "reason": "Prévision issue du simulateur : aucune vérité terrain associée.",
            "matched": 0,
            "expected": 0,
            "coverage": 0.0,
            "complete": True,
        }

    valid_times = list(meta.get("valid_times") or [])
    lead_hours = [float(h) for h in (meta.get("lead_hours") or [])]
    horizon = [i for i, lead in enumerate(lead_hours) if lead > 0]
    limit = latest_truth_day()

    truth: dict[str, dict[str, list[float]]] = {}
    missing_days: list[str] = []
    for day in sorted({datetime.fromisoformat(t).date() for t in valid_times}):
        if day > limit:
            missing_days.append(day.isoformat())
            continue
        try:
            found = _day_truth(day, download)
        except Exception as exc:  # noqa: BLE001 - une journée absente ne doit rien casser
            bus.log(
                f"Vérification : ERA5 indisponible pour le {day} ({exc})",
                level="warn",
                source="verify",
            )
            found = {}
        if found:
            truth.update(found)
        else:
            missing_days.append(day.isoformat())

    steps = [i for i in horizon if _key(valid_times[i]) in truth]
    expected = len(horizon)
    if not steps:
        return {
            **base,
            "status": "unavailable",
            "reason": (
                "Aucune analyse ERA5 disponible pour ces échéances "
                f"(publication décalée de {ERA5_LATENCY_DAYS} jours)."
            ),
            "matched": 0,
            "expected": expected,
            "coverage": 0.0,
            "complete": False,
            "missing_days": missing_days,
        }

    # Les villes du fichier de prévision sont réalignées par nom : le catalogue
    # peut avoir changé depuis l'enregistrement.
    order = {city["name"]: k for k, city in enumerate(CITIES)}
    kept = [(c, order[c["name"]]) for c in cities if c["name"] in order]
    names = [c["name"] for c, _ in kept]

    # Toutes les variables ne sont pas observées à tous les instants : les
    # tableaux gardent la même forme et les trous sont marqués « non défini »,
    # ce qui préserve l'alignement entre échéances, journées et villes.
    variables = [v for v in SCALES if v in (meta.get("variables") or {})]
    absent = np.full(len(kept), np.nan)
    pred: dict[str, np.ndarray] = {}
    obs: dict[str, np.ndarray] = {}
    for var in variables:
        series = [c["series"].get(var) for c, _ in kept]
        if any(s is None for s in series):
            continue
        rows_pred, rows_obs = [], []
        for i in steps:
            observed = truth[_key(valid_times[i])].get(var)
            if observed is None:
                rows_pred.append(absent)
                rows_obs.append(absent)
                continue
            rows_pred.append(np.asarray([s[i] for s in series], dtype=float))
            rows_obs.append(np.asarray([observed[k] for _, k in kept], dtype=float))
        stacked_pred = np.asarray(rows_pred, dtype=float)
        stacked_obs = np.asarray(rows_obs, dtype=float)
        if np.isfinite(stacked_pred - stacked_obs).any():
            pred[var] = stacked_pred
            obs[var] = stacked_obs

    if not pred:
        return {
            **base,
            "status": "unavailable",
            "reason": "Aucune variable commune entre la prévision et l'analyse ERA5.",
            "matched": 0,
            "expected": expected,
            "coverage": 0.0,
            "complete": False,
            "missing_days": missing_days,
        }

    # Persistance : l'état observé au réseau d'initialisation, figé. C'est la
    # référence naturelle — un modèle qui ne la bat pas n'apporte rien.
    persistence = truth.get(_key(valid_times[0]), {})

    per_variable: dict[str, dict] = {}
    for var, values in pred.items():
        stats = _errors(values, obs[var])
        if stats is None:
            continue
        entry = {
            **stats,
            "score": _score(var, stats["mae"]),
            "label": VARIABLES[var]["short"],
            "unit": VARIABLES[var]["unit"],
            "tolerance": SCALES[var]["tol"],
            "skill": None,
        }
        reference = persistence.get(var)
        if reference is not None:
            flat = np.asarray([reference[k] for _, k in kept], dtype=float)
            model = values - obs[var]
            inertia = flat[None, :] - obs[var]
            usable = np.isfinite(model) & np.isfinite(inertia)
            if usable.any():
                mse_persist = float(np.mean(inertia[usable] ** 2))
                if mse_persist > 1e-9:
                    mse_model = float(np.mean(model[usable] ** 2))
                    entry["skill"] = round(1.0 - mse_model / mse_persist, 3)
        per_variable[var] = entry

    # --- note par échéance ------------------------------------------------
    leads: list[dict] = []
    for row, i in enumerate(steps):
        detail = _detail(pred, obs, [row])
        score = _weighted(detail)
        if score is None:
            continue
        leads.append(
            {
                "index": i,
                "lead": lead_hours[i],
                "valid": valid_times[i],
                "day": int(-(-lead_hours[i] // 24)),
                "score": score,
                "variables": detail,
            }
        )

    # --- note par journée d'échéance --------------------------------------
    # Une « journée » regroupe les échéances d'une tranche de 24 h après le
    # réseau : J+1 couvre +6 h à +24 h, J+2 de +30 h à +48 h, etc.
    origin = datetime.fromisoformat(valid_times[0])
    days: list[dict] = []
    for number in sorted({entry["day"] for entry in leads}):
        rows = [steps.index(entry["index"]) for entry in leads if entry["day"] == number]
        detail = _detail(pred, obs, rows)
        score = _weighted(detail)
        if score is None:
            continue
        days.append(
            {
                "day": number,
                "label": f"J+{number}",
                "date": (origin + timedelta(hours=24 * number)).date().isoformat(),
                "steps": len(rows),
                "score": score,
                "grade": grade(score),
                "variables": detail,
            }
        )

    # --- note par ville ---------------------------------------------------
    city_scores = []
    for column, (city, _) in enumerate(kept):
        detail = {
            var: {**stats, "score": _score(var, stats["mae"])}
            for var, values in pred.items()
            if (stats := _errors(values[:, column], obs[var][:, column])) is not None
        }
        score = _weighted(detail)
        if score is None:
            continue
        city_scores.append(
            {
                "name": city["name"],
                "lat": city["lat"],
                "lon": city["lon"],
                "score": score,
                "variables": detail,
            }
        )
    city_scores.sort(key=lambda c: c["score"], reverse=True)

    observed = [
        {
            "name": names[column],
            "series": {
                var: [
                    round(float(x), 2) if np.isfinite(x) else None
                    for x in obs[var][:, column]
                ]
                for var in pred
            },
        }
        for column in range(len(kept))
    ]

    score = _weighted(per_variable)
    matched = len(leads)
    report = {
        **base,
        "status": "ok" if matched == expected else "partial",
        "reason": None
        if matched == expected
        else f"{expected - matched} échéance(s) sans analyse ERA5 correspondante.",
        "matched": matched,
        "expected": expected,
        "coverage": round(matched / expected, 3) if expected else 0.0,
        "complete": matched == expected,
        "missing_days": missing_days,
        "cities": len(kept),
        "score": score,
        "grade": grade(score),
        "variables": per_variable,
        "days": days,
        "leads": leads,
        "city_scores": city_scores,
        "observed": observed,
        "indices": steps,
        "valid": [valid_times[i] for i in steps],
        "lead_hours": [lead_hours[i] for i in steps],
        "reference": "Analyse ERA5 (Copernicus) échantillonnée sur les villes",
    }
    _write_report(report)
    return report


def summary(report: dict) -> dict:
    """Vue compacte d'un rapport : tout sauf les séries et le détail par ville."""
    return {
        key: report.get(key)
        for key in (
            "id",
            "model_name",
            "base_time",
            "step_hours",
            "steps",
            "status",
            "reason",
            "score",
            "grade",
            "matched",
            "expected",
            "coverage",
            "complete",
            "cities",
            "computed",
            "real_data",
        )
    } | {
        "days": [
            {"day": d["day"], "label": d["label"], "score": d["score"], "grade": d["grade"]}
            for d in report.get("days", [])
        ],
        "variables": {
            var: {"score": d["score"], "mae": d["mae"], "bias": d["bias"], "skill": d["skill"]}
            for var, d in (report.get("variables") or {}).items()
        },
    }


def index() -> dict:
    """Historique complet avec, pour chaque prévision, son rapport s'il existe."""
    entries = []
    for meta in storage.index():
        report = load_report(meta["id"])
        entries.append(
            {
                "id": meta["id"],
                "model_name": meta.get("model_name"),
                "base_time": meta.get("base_time"),
                "steps": meta.get("steps"),
                "step_hours": meta.get("step_hours"),
                "real_data": bool(meta.get("real_data")),
                "created": meta.get("created"),
                "verification": summary(report) if report else None,
            }
        )
    return {
        "entries": entries,
        "aggregate": _aggregate(entries),
        "scales": {
            var: {
                **spec,
                "label": VARIABLES[var]["short"],
                "unit": VARIABLES[var]["unit"],
            }
            for var, spec in SCALES.items()
        },
        "coverage": truth_coverage(),
        "run": snapshot(),
    }


def _aggregate(entries: list[dict]) -> dict:
    """Moyennes tous réseaux confondus : note globale, par jour et par variable."""
    reports = [
        e["verification"]
        for e in entries
        if e["verification"] and e["verification"]["status"] in ("ok", "partial")
    ]
    if not reports:
        return {"count": 0, "score": None, "grade": "—", "days": [], "variables": {}}

    scores = [r["score"] for r in reports if r["score"] is not None]
    by_day: dict[int, list[float]] = {}
    for report in reports:
        for entry in report["days"]:
            if entry["score"] is not None:
                by_day.setdefault(entry["day"], []).append(entry["score"])

    by_variable: dict[str, dict[str, list[float]]] = {}
    for report in reports:
        for var, detail in report["variables"].items():
            bucket = by_variable.setdefault(var, {"score": [], "mae": [], "skill": []})
            bucket["score"].append(detail["score"])
            bucket["mae"].append(detail["mae"])
            if detail.get("skill") is not None:
                bucket["skill"].append(detail["skill"])

    mean = round(sum(scores) / len(scores), 1) if scores else None
    return {
        "count": len(reports),
        "score": mean,
        "grade": grade(mean),
        "verified_steps": sum(r["matched"] for r in reports),
        "days": [
            {
                "day": day,
                "label": f"J+{day}",
                "score": round(sum(values) / len(values), 1),
                "grade": grade(sum(values) / len(values)),
                "samples": len(values),
            }
            for day, values in sorted(by_day.items())
        ],
        "variables": {
            var: {
                "score": round(sum(b["score"]) / len(b["score"]), 1),
                "mae": round(sum(b["mae"]) / len(b["mae"]), 2),
                "skill": round(sum(b["skill"]) / len(b["skill"]), 3) if b["skill"] else None,
                "label": VARIABLES[var]["short"],
                "unit": VARIABLES[var]["unit"],
                "samples": len(b["score"]),
            }
            for var, b in sorted(by_variable.items())
        },
    }


# ---------------------------------------------------------------------------
# Exécution en arrière-plan
# ---------------------------------------------------------------------------

_state: dict = {
    "status": "idle",
    "message": "",
    "progress": 0.0,
    "done": 0,
    "total": 0,
    "current": None,
    "error": None,
    "updated": time.time(),
}
_state_lock = threading.RLock()


def snapshot() -> dict:
    with _state_lock:
        return dict(_state)


def _publish(**changes) -> None:
    with _state_lock:
        _state.update(changes, updated=time.time())
        payload = dict(_state)
    bus.emit("verification", payload)


def start(ids: list[str] | None = None, download: bool = True, force: bool = False) -> dict:
    """Lance la vérification en arrière-plan (téléchargements CDS compris)."""
    with _state_lock:
        if _state["status"] == "running":
            raise RuntimeError("Une vérification est déjà en cours.")

    targets = ids or [m["id"] for m in storage.index() if m.get("real_data")]
    if not targets:
        raise RuntimeError("Aucune prévision sur données réelles à vérifier.")
    if download and not cds_configured():
        raise RuntimeError(
            "Identifiants Copernicus CDS absents : la vérification ne peut utiliser "
            "que les journées ERA5 déjà présentes dans le cache."
        )

    _publish(status="running", progress=0.0, done=0, total=len(targets), error=None,
             message=f"Vérification de {len(targets)} prévision(s)", current=None)
    threading.Thread(target=_worker, args=(targets, download, force), daemon=True).start()
    return snapshot()


def _worker(targets: list[str], download: bool, force: bool) -> None:
    done = 0
    scored = 0
    try:
        for forecast_id in targets:
            _publish(current=forecast_id, message=f"Analyse de {forecast_id}")
            try:
                cached = load_report(forecast_id)
                if cached and cached.get("complete") and not force:
                    report = cached
                else:
                    report = evaluate(forecast_id, download=download)
                if report["status"] in ("ok", "partial"):
                    scored += 1
                    bus.log(
                        f"Vérification {forecast_id} : note {report['score']}/100 "
                        f"({report['grade']}) sur {report['matched']} échéance(s)",
                        level="success",
                        source="verify",
                    )
            except Exception as exc:  # noqa: BLE001 - un échec isolé ne stoppe pas le lot
                bus.log(
                    f"Vérification {forecast_id} impossible : {exc}",
                    level="error",
                    source="verify",
                )
            done += 1
            _publish(done=done, progress=done / len(targets))
        _publish(
            status="done",
            current=None,
            progress=1.0,
            message=f"{scored} prévision(s) notée(s) sur {len(targets)}",
        )
    except Exception as exc:  # noqa: BLE001
        _publish(status="error", error=str(exc), message=str(exc), current=None)

"""Sources de conditions initiales pour Aurora."""

from __future__ import annotations

import os
import pickle
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

from .config import CACHE_DIR, ROOT
from .events import bus

PRESSURE_LEVELS = [50, 100, 150, 200, 250, 300, 400, 500, 600, 700, 850, 925, 1000]

ERA5_DIR = CACHE_DIR / "era5"
ERA5_DIR.mkdir(parents=True, exist_ok=True)


def _read_rc(path: Path) -> dict[str, str]:
    """Analyse un fichier `.cdsapirc` (`cle: valeur` par ligne)."""
    conf: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or ":" not in line:
                continue
            key, value = line.split(":", 1)
            conf[key.strip()] = value.strip()
    except OSError:
        return {}
    return conf


def cds_credentials() -> tuple[str | None, str | None, str | None]:
    """Identifiants CDS : (url, clé, origine).

    Ordre de recherche : variables d'environnement, `.cdsapirc` à la racine du
    projet, puis `~/.cdsapirc` (emplacement par défaut de la bibliothèque cdsapi).
    """
    env_url = os.environ.get("CDSAPI_URL")
    env_key = os.environ.get("CDSAPI_KEY")
    if env_url and env_key:
        return env_url, env_key, "variables d'environnement"

    candidates = (
        (ROOT / ".cdsapirc", ".cdsapirc (racine du projet)"),
        (Path.home() / ".cdsapirc", "~/.cdsapirc"),
    )
    for path, label in candidates:
        if not path.exists():
            continue
        conf = _read_rc(path)
        if conf.get("url") and conf.get("key"):
            return conf["url"], conf["key"], label
    return None, None, None


def cds_configured() -> bool:
    url, key, _ = cds_credentials()
    return bool(url and key)


def list_sources() -> list[dict]:
    from .system_info import dependency_status  # import tardif

    deps = dependency_status()
    _, _, origin = cds_credentials()
    era5_ready = bool(deps["cdsapi"] and deps["xarray"] and deps["netcdf4"] and origin)
    reasons = []
    if not deps["cdsapi"]:
        reasons.append("paquet cdsapi absent")
    if not deps["xarray"] or not deps["netcdf4"]:
        reasons.append("xarray/netCDF4 absents")
    if not origin:
        reasons.append(
            "aucun identifiant CDS : créez .cdsapirc à la racine du projet "
            "ou dans votre dossier personnel"
        )

    return [
        {
            "id": "synthetic",
            "name": "Atmosphère synthétique",
            "provider": "local",
            "available": True,
            "real_data": False,
            "latency": "instantané",
            "credentials": None,
            "description": (
                "État initial généré localement à partir de centres d'action aléatoires "
                "mais physiquement cohérents. Aucune donnée d'observation."
            ),
            "reason": None,
        },
        {
            "id": "era5_cds",
            "name": "ERA5 — Copernicus CDS",
            "provider": "ECMWF / Copernicus",
            "available": era5_ready,
            "real_data": True,
            "latency": "5 à 60 min (file d'attente CDS)",
            "credentials": origin,
            "description": (
                "Réanalyse ERA5 à 0.25°, 13 niveaux de pression. Source recommandée pour "
                "les versions pré-entraînées d'Aurora. Requiert un compte Copernicus."
            ),
            "reason": " · ".join(reasons) if reasons else None,
        },
    ]


# ---------------------------------------------------------------------------
# ERA5 / CDS
# ---------------------------------------------------------------------------

_PRESSURE_VARS = [
    "temperature",
    "u_component_of_wind",
    "v_component_of_wind",
    "specific_humidity",
    "geopotential",
]

# Variable Aurora -> (nom du jeu CDS, noms courts NetCDF possibles).
# Le CDS a renommé plusieurs variables au fil des versions : on tente
# successivement les alias connus plutôt que de coder un seul nom en dur.
_CLASSIC_SURFACE = {
    "2t": ("2m_temperature", ("t2m", "2t")),
    "10u": ("10m_u_component_of_wind", ("u10", "10u")),
    "10v": ("10m_v_component_of_wind", ("v10", "10v")),
    "msl": ("mean_sea_level_pressure", ("msl",)),
}

# Aurora 1.5 attend 18 variables de surface en entrée. Les 7 variables de sortie
# seule (i10fg, blh, uvb_1h, ssrd_1h, ttr_1h, scaled_tp_1h, scaled_sf_1h) sont
# complétées par des zéros par le modèle lui-même ; `insolation` est calculée.
_V1P5_SURFACE = {
    **_CLASSIC_SURFACE,
    "2d": ("2m_dewpoint_temperature", ("d2m", "2d")),
    "tcwv": ("total_column_water_vapour", ("tcwv",)),
    "tcc": ("total_cloud_cover", ("tcc",)),
    "100u": ("100m_u_component_of_wind", ("u100", "100u")),
    "100v": ("100m_v_component_of_wind", ("v100", "100v")),
    "sp": ("surface_pressure", ("sp",)),
    "lcc": ("low_cloud_cover", ("lcc",)),
    "mcc": ("medium_cloud_cover", ("mcc",)),
    "hcc": ("high_cloud_cover", ("hcc",)),
    "skt": ("skin_temperature", ("skt",)),
    "stl1": ("soil_temperature_level_1", ("stl1",)),
    "swvl1": ("volumetric_soil_water_layer_1", ("swvl1",)),
    "ci": ("sea_ice_cover", ("siconc", "ci")),
    # Le modèle applique lui-même la transformation logarithmique : on fournit
    # la hauteur de neige brute, malgré le préfixe « scaled_ ».
    "scaled_sd": ("snow_depth", ("sd", "snowdepth")),
}

SURFACE_SPECS = {"classic": _CLASSIC_SURFACE, "v1p5": _V1P5_SURFACE}

_ATMOS_SPECS = {
    "t": ("t",),
    "u": ("u",),
    "v": ("v",),
    "q": ("q",),
    "z": ("z",),
}


def era5_family(model_meta: dict | None) -> str:
    """Famille de variables ERA5 requise par un modèle Aurora."""
    cls = (model_meta or {}).get("cls") or ""
    return "v1p5" if cls.startswith("AuroraV1p5") else "classic"


def _cds_client():
    import cdsapi  # noqa: PLC0415

    url, key, origin = cds_credentials()
    if not url or not key:
        raise RuntimeError(
            "Aucun identifiant Copernicus CDS trouvé. Créez un fichier .cdsapirc "
            "à la racine du projet ou dans votre dossier personnel."
        )
    bus.log(f"ERA5 : identifiants CDS lus depuis {origin}", source="data")
    return cdsapi.Client(url=url, key=key, quiet=True, progress=False)


def _retrieve(client, dataset: str, request: dict, target: Path) -> Path:
    if target.exists():
        bus.log(f"ERA5 : cache utilisé pour {target.name}", source="data")
        return target
    bus.log(f"ERA5 : téléchargement de {target.name}…", source="data")
    client.retrieve(dataset, request, str(target))
    bus.log(f"ERA5 : {target.name} téléchargé", level="success", source="data")
    return target


def _download_day(client, day: datetime, hours: list[str], family: str) -> tuple[Path, Path]:
    tag = day.strftime("%Y-%m-%d")
    common = {
        "product_type": "reanalysis",
        "year": day.strftime("%Y"),
        "month": day.strftime("%m"),
        "day": day.strftime("%d"),
        "time": hours,
        "data_format": "netcdf",
    }
    # Les variables en niveaux de pression sont identiques pour toutes les familles :
    # le fichier atmosphérique est partagé. Le nom historique du fichier de surface
    # est conservé pour la famille « classic » afin de réutiliser le cache existant.
    suffix = "" if family == "classic" else f"-{family}"
    surf = _retrieve(
        client,
        "reanalysis-era5-single-levels",
        {**common, "variable": [spec[0] for spec in SURFACE_SPECS[family].values()]},
        ERA5_DIR / f"{tag}-surface{suffix}.nc",
    )
    atmos = _retrieve(
        client,
        "reanalysis-era5-pressure-levels",
        {
            **common,
            "variable": _PRESSURE_VARS,
            "pressure_level": [str(p) for p in PRESSURE_LEVELS],
        },
        ERA5_DIR / f"{tag}-atmospheric.nc",
    )
    return surf, atmos


def _resolve(dataset, candidates: tuple[str, ...], label: str):
    """Retrouve une variable dans un jeu NetCDF ERA5 dont le nom court peut varier."""
    for name in candidates:
        if name in dataset.variables:
            return dataset[name]
    raise RuntimeError(
        f"ERA5 : variable « {label} » introuvable (essais : {', '.join(candidates)}). "
        f"Variables présentes : {', '.join(sorted(map(str, dataset.data_vars)))}"
    )


def _clean(array: np.ndarray, label: str) -> np.ndarray:
    """ERA5 laisse des NaN sur les domaines non définis (banquise sur terre, etc.)."""
    if np.isnan(array).any():
        bus.log(
            f"ERA5 : valeurs manquantes dans « {label} », remplacées par 0",
            level="warn",
            source="data",
        )
        array = np.nan_to_num(array, nan=0.0)
    return array


def _v1p5_static_vars():
    """Variables statiques officielles d'Aurora 1.5 (36 champs, dépôt HuggingFace)."""
    import torch  # noqa: PLC0415
    from huggingface_hub import hf_hub_download  # noqa: PLC0415

    bus.log("Aurora 1.5 : récupération des 36 variables statiques…", source="data")
    args = ("microsoft/aurora", "aurora-0.25-v1.5-static.pickle")
    try:
        path = hf_hub_download(*args)
    except Exception as exc:  # noqa: BLE001 - repli sur le cache si le Hub est injoignable
        bus.log(f"HuggingFace injoignable ({exc.__class__.__name__}) : repli sur le cache local",
                level="warn", source="data")
        try:
            path = hf_hub_download(*args, local_files_only=True)
        except Exception as cache_exc:  # noqa: BLE001
            raise RuntimeError(
                "Variables statiques d'Aurora 1.5 indisponibles : ni téléchargement "
                f"ni cache local ({cache_exc})."
            ) from exc

    with open(path, "rb") as handle:
        raw = pickle.load(handle)  # noqa: S301 - fichier officiel Microsoft, cache local
    bus.log(
        f"Aurora 1.5 : {len(raw)} variables statiques chargées", level="success", source="data"
    )
    return {
        key: torch.from_numpy(np.asarray(value, dtype=np.float32)) for key, value in raw.items()
    }


def _era5_static_vars(client):
    """Variables statiques ERA5 (z, slt, lsm) pour les versions pré-entraînées."""
    import torch  # noqa: PLC0415
    import xarray as xr  # noqa: PLC0415

    path = _retrieve(
        client,
        "reanalysis-era5-single-levels",
        {
            "product_type": "reanalysis",
            "variable": ["geopotential", "land_sea_mask", "soil_type"],
            "year": "2023",
            "month": "01",
            "day": "01",
            "time": "00:00",
            "data_format": "netcdf",
        },
        ERA5_DIR / "static.nc",
    )
    dataset = xr.open_dataset(path, engine="netcdf4")

    def field(name: str):
        arr = np.asarray(dataset[name].values, dtype=np.float32)
        return torch.from_numpy(arr[0] if arr.ndim == 3 else arr)

    return {"z": field("z"), "slt": field("slt"), "lsm": field("lsm")}


def fetch_era5_batch(base_time: datetime, model_meta: dict | None = None):
    """Construit un `aurora.Batch` ERA5 adapté à la famille du modèle chargé."""
    import torch  # noqa: PLC0415
    import xarray as xr  # noqa: PLC0415
    from aurora import Batch, Metadata  # noqa: PLC0415

    if base_time.hour % 6 != 0:
        raise ValueError("ERA5 : l'heure d'initialisation doit être 00, 06, 12 ou 18 UTC.")

    family = era5_family(model_meta)
    prev_time = base_time - timedelta(hours=6)
    client = _cds_client()

    bus.log(
        f"ERA5 : préparation d'un état initial « {family} » "
        f"({len(SURFACE_SPECS[family])} variables de surface) pour "
        f"{base_time:%Y-%m-%d %H:%M} UTC",
        source="data",
    )

    all_hours = ["00:00", "06:00", "12:00", "18:00"]
    if prev_time.date() == base_time.date():
        surf_p, atmos_p = _download_day(client, base_time, all_hours, family)
        idx = [prev_time.hour // 6, base_time.hour // 6]
        surf_sel = xr.open_dataset(surf_p, engine="netcdf4").isel(valid_time=idx)
        atmos_sel = xr.open_dataset(atmos_p, engine="netcdf4").isel(valid_time=idx)
    else:
        s_prev, a_prev = _download_day(client, prev_time, ["18:00"], family)
        s_cur, a_cur = _download_day(client, base_time, ["00:00"], family)
        surf_sel = xr.concat(
            [xr.open_dataset(s_prev, engine="netcdf4"), xr.open_dataset(s_cur, engine="netcdf4")],
            dim="valid_time",
        )
        atmos_sel = xr.concat(
            [xr.open_dataset(a_prev, engine="netcdf4"), xr.open_dataset(a_cur, engine="netcdf4")],
            dim="valid_time",
        )

    surf_vars = {}
    for aurora_name, (_cds_name, candidates) in SURFACE_SPECS[family].items():
        values = np.asarray(_resolve(surf_sel, candidates, aurora_name).values, dtype=np.float32)
        surf_vars[aurora_name] = torch.from_numpy(_clean(values, aurora_name)[None])

    atmos_vars = {}
    for aurora_name, candidates in _ATMOS_SPECS.items():
        values = np.asarray(_resolve(atmos_sel, candidates, aurora_name).values, dtype=np.float32)
        atmos_vars[aurora_name] = torch.from_numpy(_clean(values, aurora_name)[None])

    lat = np.asarray(surf_sel.latitude.values, dtype=np.float32)
    lon = np.asarray(surf_sel.longitude.values, dtype=np.float32)

    if family == "v1p5":
        static_vars = _v1p5_static_vars()
        # L'insolation prescrite n'est pas fournie par ERA5 : elle est calculée
        # depuis les dates de validité, comme le fait le modèle pendant le rollout.
        from aurora import insolation  # noqa: PLC0415

        sol = insolation([prev_time, base_time], lat, lon, enforce_2d=True)
        surf_vars["insolation"] = torch.from_numpy(np.asarray(sol, dtype=np.float32)[None])
    else:
        static_vars = _era5_static_vars(client)

    return Batch(
        surf_vars=surf_vars,
        static_vars=static_vars,
        atmos_vars=atmos_vars,
        metadata=Metadata(
            lat=torch.from_numpy(lat),
            lon=torch.from_numpy(lon),
            time=(base_time,),
            atmos_levels=tuple(int(p) for p in atmos_sel.pressure_level.values),
        ),
    )

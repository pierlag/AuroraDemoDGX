"""Vérification de la compatibilité des batchs ERA5 avec les familles de modèles Aurora.

Exécution : .venv/bin/python scripts/check_batch_compat.py
"""

from __future__ import annotations

import inspect
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import torch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aurora import (  # noqa: E402
    AuroraPretrained,
    AuroraV1p5,
    Batch,
    Metadata,
    insolation,
    rollout,
)

from backend.data_sources import PRESSURE_LEVELS, SURFACE_SPECS, era5_family  # noqa: E402

NY, NX = 32, 64
LAT = np.linspace(90, -90, NY, dtype=np.float32)
LON = np.linspace(0, 360, NX + 1, dtype=np.float32)[:-1]


def expected_vars(cls) -> dict[str, tuple[str, ...]]:
    """Valeurs par défaut déclarées par la classe ou héritées de la classe de base."""
    out: dict[str, tuple[str, ...]] = {}
    for key in ("surf_vars", "static_vars", "atmos_vars", "output_only_surf_vars"):
        for klass in cls.__mro__:
            init = klass.__dict__.get("__init__")
            if init is None:
                continue
            param = inspect.signature(init).parameters.get(key)
            if param is not None and param.default is not inspect.Parameter.empty:
                out[key] = param.default
                break
    return out


def check_variable_sets() -> bool:
    ok = True
    for label, cls, family in (
        ("AuroraPretrained", AuroraPretrained, "classic"),
        ("AuroraV1p5", AuroraV1p5, "v1p5"),
    ):
        exp = expected_vars(cls)
        required_surf = set(exp["surf_vars"]) - set(exp.get("output_only_surf_vars", ()))
        built_surf = set(SURFACE_SPECS[family])
        if family == "v1p5":
            built_surf.add("insolation")

        missing = required_surf - built_surf
        extra = built_surf - set(exp["surf_vars"])
        status = "OK " if not missing and not extra else "ÉCHEC"
        if missing or extra:
            ok = False
        print(f"[{status}] {label} — surface : {len(built_surf)} fournies / "
              f"{len(required_surf)} requises")
        if missing:
            print(f"         manquantes : {sorted(missing)}")
        if extra:
            print(f"         en trop    : {sorted(extra)}")
    return ok


def check_static_pickle() -> bool:
    from backend.data_sources import _v1p5_static_vars

    expected = set(expected_vars(AuroraV1p5)["static_vars"])
    provided = set(_v1p5_static_vars())
    missing, extra = expected - provided, provided - expected
    ok = not missing and not extra
    print(f"[{'OK ' if ok else 'ÉCHEC'}] Variables statiques v1.5 : "
          f"{len(provided)} fournies / {len(expected)} requises")
    if missing:
        print(f"         manquantes : {sorted(missing)}")
    if extra:
        print(f"         en trop    : {sorted(extra)}")
    return ok


def build_batch(family: str, base_time: datetime) -> Batch:
    """Reproduit la structure produite par `fetch_era5_batch`, sur une grille réduite."""
    rng = np.random.default_rng(0)

    def surf(scale: float = 1.0):
        return torch.from_numpy(rng.random((1, 2, NY, NX), dtype=np.float32) * scale)

    surf_vars = {name: surf(300.0) for name in SURFACE_SPECS[family]}
    atmos_vars = {
        name: torch.from_numpy(
            rng.random((1, 2, len(PRESSURE_LEVELS), NY, NX), dtype=np.float32) * 300.0
        )
        for name in ("t", "u", "v", "q", "z")
    }

    if family == "v1p5":
        static_names = expected_vars(AuroraV1p5)["static_vars"]
        sol = insolation([base_time - timedelta(hours=6), base_time], LAT, LON, enforce_2d=True)
        surf_vars["insolation"] = torch.from_numpy(np.asarray(sol, dtype=np.float32)[None])
    else:
        static_names = ("z", "slt", "lsm")

    static_vars = {
        name: torch.from_numpy(rng.random((NY, NX), dtype=np.float32)) for name in static_names
    }

    return Batch(
        surf_vars=surf_vars,
        static_vars=static_vars,
        atmos_vars=atmos_vars,
        metadata=Metadata(
            lat=torch.from_numpy(LAT),
            lon=torch.from_numpy(LON),
            time=(base_time,),
            atmos_levels=tuple(PRESSURE_LEVELS),
        ),
    )


def check_forward() -> bool:
    """Passe réellement un batch v1.5 dans le réseau via `rollout`, comme en production."""
    base_time = datetime(2026, 8, 19, 12)
    model = AuroraV1p5(
        encoder_depths=(1, 1, 1),
        encoder_num_heads=(2, 2, 2),
        decoder_depths=(1, 1, 1),
        decoder_num_heads=(2, 2, 2),
        embed_dim=32,
        num_heads=2,
        latent_levels=2,
        autocast=False,
    )
    model.eval()
    batch = build_batch("v1p5", base_time)
    with torch.inference_mode():
        preds = [p.to("cpu") for p in rollout(model, batch, steps=2)]
    print(f"[OK ] Rollout AuroraV1p5 : {len(preds)} échéances, "
          f"{len(preds[0].surf_vars)} variables de surface prédites")
    for key in ("scaled_tp_1h", "tcc", "i10fg", "2t"):
        print(f"         {key:14} {'présent' if key in preds[0].surf_vars else 'ABSENT'}")
    analysis_only = set(batch.surf_vars) - set(preds[0].surf_vars)
    if analysis_only:
        print(f"         absentes des prévisions : {sorted(analysis_only)}")
    return True


def main() -> int:
    print(f"famille pour AuroraV1p5Ensemble : {era5_family({'cls': 'AuroraV1p5Ensemble'})}")
    print(f"famille pour Aurora             : {era5_family({'cls': 'Aurora'})}\n")
    results = [check_variable_sets(), check_static_pickle()]
    print()
    try:
        results.append(check_forward())
    except Exception as exc:  # noqa: BLE001
        print(f"[ÉCHEC] Rollout : {type(exc).__name__}: {exc}")
        results.append(False)
    print()
    print("RÉSULTAT :", "tout est conforme" if all(results) else "des écarts subsistent")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())

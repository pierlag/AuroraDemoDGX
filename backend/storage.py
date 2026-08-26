"""Persistance des prévisions sur disque.

Chaque prévision occupe un dossier `data/forecasts/{id}/` contenant ses
métadonnées (JSON) et ses champs (NPZ compressé). L'index est reconstruit au
démarrage : l'historique survit au redémarrage du serveur.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import threading
import time
from pathlib import Path

import numpy as np

from .config import FORECAST_DIR
from .events import bus

# Les identifiants sont générés par `uuid4().hex[:12]`. Toute valeur reçue depuis
# une URL est vérifiée avant d'être transformée en chemin.
ID_RE = re.compile(r"^[0-9a-f]{6,32}$")

MAX_STORED = int(os.environ.get("AURORA_MAX_STORED_FORECASTS", "40"))

_lock = threading.RLock()


def valid_id(forecast_id: str) -> bool:
    return bool(ID_RE.match(forecast_id or ""))


def _dir(forecast_id: str) -> Path:
    if not valid_id(forecast_id):
        raise ValueError(f"Identifiant de prévision invalide : {forecast_id!r}")
    return FORECAST_DIR / forecast_id


def _dir_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                continue
    return total


# ---------------------------------------------------------------------------
# Écriture
# ---------------------------------------------------------------------------


def save(result: dict) -> None:
    """Écrit une prévision sur disque de façon atomique."""
    meta = result["meta"]
    target = _dir(meta["id"])
    staging = Path(tempfile.mkdtemp(dir=FORECAST_DIR, prefix=".tmp-"))
    try:
        np.savez_compressed(
            staging / "fields.npz",
            **{key: np.asarray(value, dtype=np.float32) for key, value in result["fields"].items()},
        )
        (staging / "meta.json").write_text(
            json.dumps({"meta": meta, "cities": result["cities"]}, ensure_ascii=False),
            encoding="utf-8",
        )
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        staging.rename(target)
        bus.log(
            f"Prévision {meta['id']} enregistrée ({_dir_size(target) / 1024**2:.1f} Mo)",
            source="storage",
        )
    except Exception as exc:  # noqa: BLE001
        shutil.rmtree(staging, ignore_errors=True)
        bus.log(f"Enregistrement impossible : {exc}", level="error", source="storage")
        return
    prune()


def prune() -> None:
    """Conserve les `MAX_STORED` prévisions les plus récentes."""
    entries = index()
    for meta in entries[MAX_STORED:]:
        delete(meta["id"], quiet=True)


# ---------------------------------------------------------------------------
# Lecture
# ---------------------------------------------------------------------------


def _read_meta(folder: Path) -> dict | None:
    try:
        payload = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    meta = payload.get("meta")
    if not isinstance(meta, dict) or "id" not in meta:
        return None
    meta["stored_bytes"] = _dir_size(folder)
    return meta


def index() -> list[dict]:
    """Métadonnées de toutes les prévisions sur disque, de la plus récente à la plus ancienne."""
    with _lock:
        metas = []
        for folder in FORECAST_DIR.iterdir():
            if not folder.is_dir() or folder.name.startswith(".tmp-"):
                continue
            meta = _read_meta(folder)
            if meta is not None:
                metas.append(meta)
    metas.sort(key=lambda m: m.get("created", 0), reverse=True)
    return metas


def load(forecast_id: str) -> dict | None:
    """Recharge une prévision complète (métadonnées, villes et champs)."""
    folder = _dir(forecast_id)
    if not folder.is_dir():
        return None
    meta = _read_meta(folder)
    if meta is None:
        return None
    try:
        with np.load(folder / "fields.npz") as archive:
            fields = {name: archive[name] for name in archive.files}
    except (OSError, ValueError) as exc:
        bus.log(f"Champs illisibles pour {forecast_id} : {exc}", level="error", source="storage")
        return None
    payload = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
    return {"meta": meta, "cities": payload.get("cities", []), "fields": fields}


# ---------------------------------------------------------------------------
# Suppression
# ---------------------------------------------------------------------------


def delete(forecast_id: str, quiet: bool = False) -> bool:
    folder = _dir(forecast_id)
    if not folder.is_dir():
        return False
    with _lock:
        shutil.rmtree(folder, ignore_errors=True)
    if not quiet:
        bus.log(f"Prévision {forecast_id} supprimée", level="warn", source="storage")
    return True


def delete_all() -> int:
    removed = 0
    for meta in index():
        if delete(meta["id"], quiet=True):
            removed += 1
    if removed:
        bus.log(f"{removed} prévision(s) supprimée(s)", level="warn", source="storage")
    return removed


def stats() -> dict:
    entries = index()
    measured = [m["energy"] for m in entries if (m.get("energy") or {}).get("measured")]
    return {
        "count": len(entries),
        "bytes": sum(m.get("stored_bytes", 0) for m in entries),
        "max_stored": MAX_STORED,
        "path": str(FORECAST_DIR),
        "oldest": entries[-1]["created"] if entries else None,
        "newest": entries[0]["created"] if entries else None,
        "energy_wh": round(sum(e["total_wh"] for e in measured), 8),
        "co2_g": round(sum(e["co2_g"] for e in measured), 10),
        "compute_s": round(sum(e["duration_s"] for e in measured), 1),
        "measured_count": len(measured),
        "updated": time.time(),
    }

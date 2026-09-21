"""Planification quotidienne des prévisions.

Une seule échéance par jour, exprimée dans le fuseau horaire de la machine : à
l'heure dite, la station relance une prévision avec les paramètres enregistrés
(source, nombre d'échéances, membres d'ensemble). Le réseau d'initialisation
n'est pas figé — c'est toujours le plus récent que la source sache fournir.

Le réglage survit au redémarrage du serveur : il est écrit dans
`data/state/schedule.json`, avec la date du dernier déclenchement pour qu'un
redémarrage ne relance pas deux fois la même journée.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone

from . import forecast as fc
from .config import BASE_TIMESTEP_H, ERA5_LATENCY_DAYS, MAX_STEPS, STATE_DIR
from .data_sources import list_sources
from .events import bus
from .model_manager import manager
from .registry import MODELS_BY_ID

SCHEDULE_FILE = STATE_DIR / "schedule.json"

TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
DEVICE_RE = re.compile(r"^(cpu|mps|cuda(:\d{1,2})?)$")

MAX_MEMBERS = 8

# Fenêtre de rattrapage : si la station était éteinte à l'heure prévue, elle
# lance quand même la prévision tant qu'on reste dans ce délai.
GRACE_S = 2 * 3600

# Le premier chargement d'un modèle Aurora télécharge plusieurs gigaoctets.
LOAD_TIMEOUT_S = 45 * 60

# Cadence de réveil du planificateur.
TICK_S = 20.0

DEFAULTS: dict = {
    "enabled": False,
    "time": "07:00",
    "source": "era5_cds",
    "steps": 20,
    "members": 1,
    # Modèle chargé automatiquement si la mémoire est vide à l'heure dite.
    "model_id": None,
    "device": "cpu",
    "use_lora": True,
}

_lock = threading.RLock()
_config: dict = dict(DEFAULTS)
_runtime: dict = {
    "running": False,
    "stage": None,
    "last_date": None,
    "last_run": None,
    "last_status": None,
    "last_error": None,
    "last_forecast_id": None,
    "last_job_id": None,
}


# ---------------------------------------------------------------------------
# Persistance
# ---------------------------------------------------------------------------


def load() -> None:
    try:
        stored = json.loads(SCHEDULE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    with _lock:
        for key in DEFAULTS:
            if key in stored:
                _config[key] = stored[key]
        for key in ("last_date", "last_run", "last_status", "last_forecast_id"):
            if key in stored:
                _runtime[key] = stored[key]


def _persist() -> None:
    with _lock:
        payload = {
            **_config,
            **{k: _runtime[k] for k in ("last_date", "last_run", "last_status",
                                        "last_forecast_id")},
        }
    try:
        fd = os.open(SCHEDULE_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
    except OSError as exc:
        bus.log(f"Planification : écriture impossible ({exc})", level="warn", source="schedule")


# ---------------------------------------------------------------------------
# État
# ---------------------------------------------------------------------------


def _next_run(now: datetime | None = None) -> datetime | None:
    """Prochain déclenchement en heure locale, ou None si la planification dort."""
    with _lock:
        if not _config["enabled"]:
            return None
        hour, minute = (int(x) for x in _config["time"].split(":"))
        already = _runtime["last_date"]

    now = now or datetime.now()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now or already == target.strftime("%Y-%m-%d"):
        target += timedelta(days=1)
    return target


def snapshot() -> dict:
    with _lock:
        config = dict(_config)
        runtime = dict(_runtime)
    upcoming = _next_run()
    return {
        **config,
        **runtime,
        "next_run": upcoming.isoformat() if upcoming else None,
        "next_run_in_s": round((upcoming - datetime.now()).total_seconds()) if upcoming else None,
        "timezone": datetime.now().astimezone().tzname(),
        "base_time_preview": _base_time(config["source"]).replace(
            tzinfo=timezone.utc
        ).isoformat(),
        "max_steps": MAX_STEPS,
        "max_members": MAX_MEMBERS,
        "step_hours": BASE_TIMESTEP_H,
    }


def _publish(**changes) -> None:
    with _lock:
        _runtime.update(changes)
    bus.emit("schedule", snapshot())


# ---------------------------------------------------------------------------
# Réglage
# ---------------------------------------------------------------------------


def update(patch: dict) -> dict:
    """Valide puis enregistre les paramètres de la planification."""
    clean: dict = {}

    if "time" in patch and patch["time"] is not None:
        value = str(patch["time"]).strip()
        if not TIME_RE.match(value):
            raise ValueError("Heure invalide : format attendu HH:MM sur 24 heures.")
        clean["time"] = value

    if "source" in patch and patch["source"] is not None:
        sources = {s["id"]: s for s in list_sources()}
        source = sources.get(patch["source"])
        if source is None:
            raise ValueError(f"Source inconnue : {patch['source']}")
        clean["source"] = source["id"]

    if "steps" in patch and patch["steps"] is not None:
        clean["steps"] = fc.clamp_steps(patch["steps"])

    if "members" in patch and patch["members"] is not None:
        clean["members"] = max(1, min(int(patch["members"]), MAX_MEMBERS))

    if "model_id" in patch:
        model_id = patch["model_id"] or None
        if model_id is not None and model_id not in MODELS_BY_ID:
            raise ValueError(f"Modèle inconnu : {model_id}")
        clean["model_id"] = model_id

    if "device" in patch and patch["device"] is not None:
        device = str(patch["device"]).strip()
        if not DEVICE_RE.match(device):
            raise ValueError(f"Périphérique invalide : {device}")
        clean["device"] = device

    if "use_lora" in patch and patch["use_lora"] is not None:
        clean["use_lora"] = bool(patch["use_lora"])

    if "enabled" in patch and patch["enabled"] is not None:
        clean["enabled"] = bool(patch["enabled"])

    with _lock:
        was = _config["enabled"]
        _config.update(clean)
        config = dict(_config)

    _persist()
    if config["enabled"] and not was:
        upcoming = _next_run()
        bus.log(
            f"Planification activée : prévision quotidienne à {config['time']}"
            + (f" — prochaine le {upcoming:%d/%m à %H:%M}" if upcoming else ""),
            level="success",
            source="schedule",
        )
    elif was and not config["enabled"]:
        bus.log("Planification désactivée", level="warn", source="schedule")

    bus.emit("schedule", snapshot())
    return snapshot()


# ---------------------------------------------------------------------------
# Exécution
# ---------------------------------------------------------------------------


def _base_time(source: str) -> datetime:
    """Réseau d'initialisation le plus récent que la source sache fournir."""
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0, tzinfo=None)
    if source == "era5_cds":
        now -= timedelta(days=ERA5_LATENCY_DAYS)
    return now - timedelta(hours=now.hour % BASE_TIMESTEP_H)


def _await_model(config: dict) -> None:
    """S'assure qu'un modèle est en mémoire, quitte à le charger d'abord."""
    if manager.is_ready:
        return
    if not config["model_id"]:
        raise RuntimeError(
            "Aucun modèle en mémoire et aucun modèle de secours configuré : "
            "choisissez-en un dans la planification."
        )

    _publish(stage=f"Chargement de {MODELS_BY_ID[config['model_id']]['name']}")
    manager.load(config["model_id"], config["device"], config["use_lora"])

    deadline = time.time() + LOAD_TIMEOUT_S
    while time.time() < deadline:
        if manager.state == "ready":
            return
        if manager.state == "error":
            raise RuntimeError(manager.error or "chargement du modèle en échec")
        time.sleep(2.0)
    raise RuntimeError("Délai de chargement du modèle dépassé.")


def _run(trigger: str) -> None:
    with _lock:
        if _runtime["running"]:
            raise RuntimeError("Une prévision planifiée est déjà en cours.")
        config = dict(_config)
        _runtime["running"] = True

    _publish(stage="Préparation", last_status="running", last_error=None,
             last_run=time.time(), last_forecast_id=None, last_job_id=None)
    try:
        sources = {s["id"]: s for s in list_sources()}
        source = sources.get(config["source"])
        if source is None:
            raise RuntimeError(f"Source inconnue : {config['source']}")
        if not source["available"]:
            raise RuntimeError(f"Source indisponible : {source['reason']}")

        _await_model(config)

        base_time = _base_time(config["source"])
        bus.log(
            f"Prévision {trigger} : {config['steps']} échéances, "
            f"{config['members']} membre(s), réseau {base_time:%Y-%m-%d %H:%M} UTC",
            source="schedule",
        )
        job = fc.create_job(
            {
                "source": config["source"],
                "base_time": base_time,
                "steps": fc.clamp_steps(config["steps"]),
                "members": config["members"],
            }
        )
        _publish(stage="Inférence", last_job_id=job["id"])

        while True:
            time.sleep(2.0)
            current = fc.get_job(job["id"])
            if current is None:
                raise RuntimeError("Travail perdu avant la fin de l'inférence.")
            if current["status"] == "done":
                _publish(
                    stage=None,
                    last_status="done",
                    last_forecast_id=current["forecast_id"],
                )
                bus.log(
                    f"Prévision planifiée terminée : {current['forecast_id']}",
                    level="success",
                    source="schedule",
                )
                return
            if current["status"] == "error":
                raise RuntimeError(current["error"] or "inférence en échec")
    except Exception as exc:  # noqa: BLE001 - l'erreur est rapportée, la boucle continue
        _publish(stage=None, last_status="error", last_error=str(exc))
        bus.log(f"Prévision planifiée impossible : {exc}", level="error", source="schedule")
    finally:
        with _lock:
            _runtime["running"] = False
        _persist()
        bus.emit("schedule", snapshot())


def run_now() -> dict:
    """Déclenche immédiatement une prévision avec les paramètres planifiés."""
    with _lock:
        if _runtime["running"]:
            raise RuntimeError("Une prévision planifiée est déjà en cours.")
    threading.Thread(target=_run, args=("manuelle",), daemon=True).start()
    return snapshot()


# ---------------------------------------------------------------------------
# Boucle de veille
# ---------------------------------------------------------------------------


def _due(now: datetime) -> bool:
    with _lock:
        if not _config["enabled"] or _runtime["running"]:
            return False
        hour, minute = (int(x) for x in _config["time"].split(":"))
        already = _runtime["last_date"]

    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if already == now.strftime("%Y-%m-%d"):
        return False
    return timedelta(0) <= now - target <= timedelta(seconds=GRACE_S)


def _loop() -> None:
    while True:
        time.sleep(TICK_S)
        try:
            now = datetime.now()
            if not _due(now):
                continue
            with _lock:
                _runtime["last_date"] = now.strftime("%Y-%m-%d")
            _persist()
            _run("planifiée")
        except Exception as exc:  # noqa: BLE001 - la veille ne doit jamais s'arrêter
            bus.log(f"Planificateur : {exc}", level="error", source="schedule")


_thread: threading.Thread | None = None


def start() -> None:
    """Démarre la veille. Sans effet si elle tourne déjà."""
    global _thread  # noqa: PLW0603 - une seule veille pour tout le processus
    load()
    if _thread is not None and _thread.is_alive():
        return
    _thread = threading.Thread(target=_loop, name="aurora-scheduler", daemon=True)
    _thread.start()
    with _lock:
        enabled, at = _config["enabled"], _config["time"]
    if enabled:
        upcoming = _next_run()
        bus.log(
            f"Prévision quotidienne planifiée à {at}"
            + (f" — prochaine le {upcoming:%d/%m à %H:%M}" if upcoming else ""),
            source="schedule",
        )

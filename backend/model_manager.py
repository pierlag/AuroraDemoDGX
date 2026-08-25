"""Gestion du cycle de vie du modèle : chargement, déchargement, supervision."""

from __future__ import annotations

import gc
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from .config import HF_HOME
from .events import bus
from .registry import MODELS_BY_ID
from .system_info import dependency_status, gpu_info

STATES = ("idle", "checking", "downloading", "loading", "ready", "busy", "unloading", "error")


def _dir_size_mb(path: Path) -> float:
    total = 0
    if not path.exists():
        return 0.0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                continue
    return total / 1024**2


class ModelManager:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._model = None
        self._worker: threading.Thread | None = None
        self._cancel = threading.Event()
        self.state = "idle"
        self.model_id: str | None = None
        self.device: str = "cpu"
        self.use_lora: bool = True
        self.error: str | None = None
        self.progress: float = 0.0
        self.stage: str = ""
        self.loaded_at: float | None = None
        self.load_seconds: float | None = None
        self.inference_count: int = 0
        self.last_inference: dict | None = None
        self.install_running: bool = False

    # -- état --------------------------------------------------------------
    def _set(self, **kwargs) -> None:
        with self._lock:
            for key, value in kwargs.items():
                setattr(self, key, value)
        bus.emit("model_state", self.snapshot())

    def snapshot(self) -> dict:
        meta = MODELS_BY_ID.get(self.model_id or "", {})
        return {
            "state": self.state,
            "model_id": self.model_id,
            "model_name": meta.get("name"),
            "engine": meta.get("engine"),
            "real_data": meta.get("real_data", False),
            "device": self.device,
            "use_lora": self.use_lora,
            "error": self.error,
            "progress": round(self.progress, 3),
            "stage": self.stage,
            "loaded_at": self.loaded_at,
            "load_seconds": self.load_seconds,
            "uptime_s": (time.time() - self.loaded_at) if self.loaded_at else None,
            "inference_count": self.inference_count,
            "last_inference": self.last_inference,
            "install_running": self.install_running,
            "supports": meta.get("supports", []),
            "timestep_h": meta.get("timestep_h", 6),
            "fine_lead_times": meta.get("fine_lead_times", False),
            "ensemble": meta.get("ensemble", False),
        }

    @property
    def is_ready(self) -> bool:
        return self.state in ("ready", "busy")

    @property
    def model(self):
        return self._model

    def acquire(self):
        """Réserve le modèle pour une inférence.

        Le contrôle d'état et la lecture du modèle se font sous le même verrou :
        un déchargement concurrent ne peut plus s'intercaler entre les deux.
        """
        with self._lock:
            if self.state != "ready" or self._model is None:
                raise RuntimeError(
                    f"Aucun modèle disponible pour l'inférence (état : {self.state}). "
                    "Chargez un modèle depuis la console d'administration."
                )
            self.state = "busy"
            holder = self._model
        bus.emit("model_state", self.snapshot())
        return holder

    def release(self) -> None:
        with self._lock:
            if self.state == "busy":
                self.state = "ready"
        bus.emit("model_state", self.snapshot())

    # -- préflight ---------------------------------------------------------
    def preflight(self, model_id: str, device: str) -> list[dict]:
        meta = MODELS_BY_ID.get(model_id)
        checks: list[dict] = []
        if meta is None:
            return [{"ok": False, "label": "Modèle inconnu", "detail": model_id}]

        if meta["engine"] == "simulation":
            return [{"ok": True, "label": "Moteur local", "detail": "Aucune dépendance requise"}]

        deps = dependency_status()
        checks.append(
            {
                "ok": bool(deps["torch"]),
                "label": "PyTorch",
                "detail": deps["torch"] or "non installé — utilisez « Installer les dépendances »",
            }
        )
        checks.append(
            {
                "ok": bool(deps["aurora"]),
                "label": "microsoft-aurora",
                "detail": deps["aurora"] or "non installé",
            }
        )
        if device.startswith("cuda"):
            import psutil  # noqa: PLC0415

            gpus = gpu_info()
            idx = int(device.split(":")[1]) if ":" in device else 0
            gpu = next((g for g in gpus if g["index"] == idx), None)
            if gpu is None:
                checks.append({"ok": False, "label": "GPU", "detail": f"{device} indisponible"})
            else:
                free_gb = (gpu["total_mb"] - gpu["used_mb"]) / 1024
                ram_total_gb = psutil.virtual_memory().total / 1024**3
                unified = abs(gpu["total_mb"] / 1024 - ram_total_gb) / ram_total_gb < 0.12
                note = ""
                if unified:
                    # Mémoire unifiée (Grace-Blackwell, Jetson…) : le cache page
                    # est réclamable, `mem_get_info` le compte pourtant comme
                    # occupé et sous-estime fortement la mémoire mobilisable.
                    free_gb = max(free_gb, psutil.virtual_memory().available / 1024**3)
                    note = " (mémoire unifiée)"
                checks.append(
                    {
                        "ok": free_gb >= meta["min_vram_gb"] * 0.9,
                        "label": f"Mémoire {gpu['name']}{note}",
                        "detail": f"{free_gb:.1f} Go mobilisables / {meta['min_vram_gb']} Go requis",
                    }
                )
        else:
            import psutil  # noqa: PLC0415

            free_gb = psutil.virtual_memory().available / 1024**3
            checks.append(
                {
                    "ok": free_gb >= meta["min_ram_gb"] * 0.8,
                    "label": "Mémoire vive",
                    "detail": f"{free_gb:.1f} Go disponibles / {meta['min_ram_gb']} Go conseillés",
                }
            )
        return checks

    # -- chargement --------------------------------------------------------
    def load(self, model_id: str, device: str = "cpu", use_lora: bool = True) -> dict:
        with self._lock:
            if self.state in ("loading", "downloading", "unloading", "busy"):
                raise RuntimeError(f"Opération déjà en cours (état : {self.state}).")
            if model_id not in MODELS_BY_ID:
                raise KeyError(f"Modèle inconnu : {model_id}")
            if self._model is not None:
                self._release()
            self._cancel.clear()
            self._worker = threading.Thread(
                target=self._load_worker, args=(model_id, device, use_lora), daemon=True
            )
        self._set(
            state="checking",
            model_id=model_id,
            device=device,
            use_lora=use_lora,
            error=None,
            progress=0.0,
            stage="Vérification de l'environnement",
        )
        self._worker.start()
        return self.snapshot()

    def _load_worker(self, model_id: str, device: str, use_lora: bool) -> None:
        meta = MODELS_BY_ID[model_id]
        started = time.time()
        try:
            bus.log(f"Chargement de « {meta['name'] } » sur {device}", source="model")
            if meta["engine"] == "simulation":
                self._set(state="loading", progress=0.4, stage="Initialisation du moteur")
                from .simulation import SyntheticAtmosphere  # noqa: PLC0415

                self._model = {"engine": "simulation", "factory": SyntheticAtmosphere}
            else:
                self._load_aurora(meta, device, use_lora)

            self._set(
                state="ready",
                progress=1.0,
                stage="Prêt",
                loaded_at=time.time(),
                load_seconds=round(time.time() - started, 2),
            )
            bus.log(
                f"« {meta['name']} » chargé en {time.time() - started:.1f} s",
                level="success",
                source="model",
            )
        except Exception as exc:  # noqa: BLE001
            self._model = None
            self._set(state="error", error=str(exc), stage="Échec", progress=0.0)
            bus.log(f"Échec du chargement : {exc}", level="error", source="model")

    def _load_aurora(self, meta: dict, device: str, use_lora: bool) -> None:
        checks = self.preflight(meta["id"], device)
        failed = [c for c in checks if not c["ok"]]
        if failed:
            raise RuntimeError("; ".join(f"{c['label']} : {c['detail']}" for c in failed))

        import torch  # noqa: PLC0415
        import aurora as aurora_pkg  # noqa: PLC0415

        cls = getattr(aurora_pkg, meta["cls"], None)
        if cls is None:
            raise RuntimeError(
                f"La classe {meta['cls']} est absente de la version installée de microsoft-aurora."
            )

        kwargs = {}
        if meta["cls"] in ("Aurora", "AuroraHighRes") and not use_lora:
            kwargs["use_lora"] = False

        self._set(state="loading", progress=0.05, stage="Instanciation du réseau")
        model = cls(**kwargs)

        self._set(state="downloading", progress=0.08, stage="Récupération des poids (HuggingFace)")
        stop = threading.Event()
        watcher = threading.Thread(
            target=self._watch_download, args=(meta, stop), daemon=True
        )
        watcher.start()
        try:
            if meta["cls"] in ("Aurora", "AuroraHighRes") and not use_lora:
                model.load_checkpoint(strict=False)
            else:
                model.load_checkpoint()
        finally:
            stop.set()

        self._set(state="loading", progress=0.85, stage=f"Transfert vers {device}")
        model.eval()
        model = model.to(device)
        if device.startswith("cuda"):
            torch.cuda.synchronize()
        self._model = {"engine": "aurora", "model": model, "device": device, "meta": meta}

    def _watch_download(self, meta: dict, stop: threading.Event) -> None:
        hf = Path(HF_HOME)
        start_mb = _dir_size_mb(hf)
        expected = max(meta.get("download_mb", 1), 1)
        while not stop.wait(1.5):
            got = max(_dir_size_mb(hf) - start_mb, 0.0)
            ratio = min(got / expected, 0.99)
            self._set(
                progress=0.08 + 0.72 * ratio,
                stage=f"Téléchargement des poids — {got:.0f} / {expected} Mo",
            )

    # -- déchargement ------------------------------------------------------
    def _release(self) -> None:
        model_holder = self._model
        self._model = None
        if model_holder and model_holder.get("engine") == "aurora":
            try:
                model_holder["model"].to("cpu")
            except Exception:  # noqa: BLE001
                pass
            model_holder.pop("model", None)
        del model_holder
        gc.collect()
        try:
            import torch  # noqa: PLC0415

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
                torch.cuda.ipc_collect()
        except Exception:  # noqa: BLE001
            pass

    def unload(self) -> dict:
        with self._lock:
            if self.state in ("loading", "downloading", "busy"):
                raise RuntimeError(f"Impossible de décharger pendant l'état « {self.state} ».")
            if self._model is None and self.state != "error":
                return self.snapshot()
        name = MODELS_BY_ID.get(self.model_id or "", {}).get("name", self.model_id)
        self._set(state="unloading", stage="Libération de la mémoire", progress=0.0)
        self._release()
        self._set(
            state="idle",
            model_id=None,
            error=None,
            stage="",
            loaded_at=None,
            load_seconds=None,
            progress=0.0,
        )
        bus.log(f"« {name} » déchargé — mémoire libérée", level="success", source="model")
        return self.snapshot()

    def note_inference(self, info: dict) -> None:
        with self._lock:
            self.inference_count += 1
            self.last_inference = info
        bus.emit("model_state", self.snapshot())

    # -- gestion du cache de poids ----------------------------------------
    def cache_info(self) -> dict:
        hf = Path(HF_HOME)
        entries = []
        root = hf / "hub"
        if root.exists():
            for child in sorted(root.iterdir()):
                if child.is_dir() and child.name.startswith("models--"):
                    entries.append(
                        {
                            "name": child.name.replace("models--", "").replace("--", "/"),
                            "size_mb": round(_dir_size_mb(child), 1),
                            "path": str(child),
                        }
                    )
        return {"path": str(hf), "total_mb": round(_dir_size_mb(hf), 1), "entries": entries}

    def purge_cache(self) -> dict:
        if self.state in ("loading", "downloading", "busy"):
            raise RuntimeError("Opération impossible pendant le chargement.")
        hub = Path(HF_HOME) / "hub"
        if hub.exists():
            shutil.rmtree(hub, ignore_errors=True)
        bus.log("Cache des poids HuggingFace purgé", level="warn", source="model")
        return self.cache_info()

    # -- installation des dépendances lourdes ------------------------------
    def install_dependencies(self, extras: list[str] | None = None) -> None:
        if self.install_running:
            raise RuntimeError("Une installation est déjà en cours.")
        packages = ["torch", "microsoft-aurora"]
        for extra in extras or []:
            if extra == "era5":
                packages += ["cdsapi", "xarray", "netCDF4"]
        threading.Thread(target=self._install_worker, args=(packages,), daemon=True).start()

    def _install_worker(self, packages: list[str]) -> None:
        self._set(install_running=True)
        cmd = [sys.executable, "-m", "pip", "install", "--upgrade", *packages]
        bus.log(f"$ {' '.join(cmd)}", source="pip")
        try:
            proc = subprocess.Popen(  # noqa: S603
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.rstrip()
                if line:
                    bus.log(line, source="pip")
            code = proc.wait()
            if code == 0:
                bus.log("Installation terminée avec succès", level="success", source="pip")
            else:
                bus.log(f"pip a retourné le code {code}", level="error", source="pip")
        except Exception as exc:  # noqa: BLE001
            bus.log(f"Installation impossible : {exc}", level="error", source="pip")
        finally:
            self._set(install_running=False)
            bus.emit("dependencies", dependency_status())


manager = ModelManager()

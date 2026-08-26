"""Mesure de l'énergie consommée par une inférence et de son empreinte carbone.

Trois méthodes sont tentées, de la plus fiable à la plus approximative :

1. compteur d'énergie cumulatif NVML — intégré par le matériel, sans erreur
   d'échantillonnage ;
2. échantillonnage de la puissance instantanée via NVML ;
3. échantillonnage via `nvidia-smi`.

La mesure porte sur le **GPU seul** : ni le processeur, ni la mémoire, ni les
pertes de l'alimentation n'y figurent. Une consommation d'hôte forfaitaire peut
être ajoutée via `AURORA_HOST_POWER_W` pour approcher le total à la prise.
"""

from __future__ import annotations

import shutil
import subprocess
import threading
import time

from .config import CARBON_INTENSITY_G_KWH, HOST_POWER_W
from .events import bus


def _nvml():
    try:
        import pynvml  # noqa: PLC0415

        pynvml.nvmlInit()
        return pynvml
    except Exception:  # noqa: BLE001 - NVML absent ou pilote inaccessible
        return None


def _smi_power() -> float | None:
    if not shutil.which("nvidia-smi"):
        return None
    try:
        out = subprocess.run(  # noqa: S603 - argv fixe
            ["nvidia-smi", "--query-gpu=power.draw", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        first = out.stdout.strip().splitlines()[0]
        return float(first)
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return None


class EnergyMeter:
    """Mesure l'énergie consommée pendant un bloc d'exécution."""

    def __init__(self, device: str = "cpu") -> None:
        self.device = device
        self.index = int(device.split(":")[1]) if device.startswith("cuda:") else 0
        self.method = "none"
        self.started = 0.0
        self.duration = 0.0
        self.joules = 0.0
        self._pynvml = None
        self._handle = None
        self._energy0: int | None = None
        self._samples: list[tuple[float, float]] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- cycle de vie ------------------------------------------------------
    def __enter__(self) -> EnergyMeter:
        self.start()
        return self

    def __exit__(self, *_exc) -> None:
        self.stop()

    def start(self) -> None:
        self.started = time.time()
        self._pynvml = _nvml()
        if self._pynvml is not None:
            try:
                self._handle = self._pynvml.nvmlDeviceGetHandleByIndex(self.index)
                self._energy0 = self._pynvml.nvmlDeviceGetTotalEnergyConsumption(self._handle)
                self.method = "nvml-counter"
                return
            except Exception:  # noqa: BLE001 - compteur cumulatif non géré
                self._energy0 = None
            if self._handle is not None:
                self.method = "nvml-sampling"
        elif _smi_power() is not None:
            self.method = "nvidia-smi"

        if self.method in ("nvml-sampling", "nvidia-smi"):
            self._thread = threading.Thread(target=self._sample_loop, daemon=True)
            self._thread.start()

    def _read_power(self) -> float | None:
        if self.method == "nvml-sampling" and self._handle is not None:
            try:
                return self._pynvml.nvmlDeviceGetPowerUsage(self._handle) / 1000.0
            except Exception:  # noqa: BLE001
                return None
        return _smi_power()

    def _sample_loop(self) -> None:
        while not self._stop.wait(0.5):
            watts = self._read_power()
            if watts is not None:
                self._samples.append((time.time(), watts))

    def stop(self) -> None:
        self.duration = max(time.time() - self.started, 1e-6)
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)

        if self.method == "nvml-counter" and self._energy0 is not None:
            try:
                energy1 = self._pynvml.nvmlDeviceGetTotalEnergyConsumption(self._handle)
                self.joules = max(energy1 - self._energy0, 0) / 1000.0
            except Exception:  # noqa: BLE001
                self.method = "none"
        elif self._samples:
            # Intégration trapézoïdale des échantillons de puissance.
            total = 0.0
            for (t0, w0), (t1, w1) in zip(self._samples, self._samples[1:]):
                total += (w0 + w1) / 2 * (t1 - t0)
            if len(self._samples) == 1:
                total = self._samples[0][1] * self.duration
            self.joules = total
        else:
            self.method = "none"

        if self._pynvml is not None:
            try:
                self._pynvml.nvmlShutdown()
            except Exception:  # noqa: BLE001
                pass

    # -- résultat ----------------------------------------------------------
    def result(self) -> dict:
        gpu_wh = self.joules / 3600.0
        host_wh = HOST_POWER_W * self.duration / 3600.0
        total_wh = gpu_wh + host_wh
        on_gpu = self.device.startswith("cuda")
        return {
            "measured": self.method != "none",
            "method": self.method,
            "device": self.device,
            "on_gpu": on_gpu,
            "duration_s": round(self.duration, 2),
            # Les inférences courtes se comptent en µWh : on garde assez de
            # décimales pour que l'arrondi n'écrase pas la mesure.
            "gpu_wh": round(gpu_wh, 8),
            "gpu_avg_w": round(self.joules / self.duration, 1) if self.joules else 0.0,
            "host_wh": round(host_wh, 8),
            "host_power_w": HOST_POWER_W,
            "total_wh": round(total_wh, 8),
            "co2_g": round(total_wh / 1000.0 * CARBON_INTENSITY_G_KWH, 10),
            "carbon_intensity_g_kwh": CARBON_INTENSITY_G_KWH,
        }


def describe(result: dict) -> str:
    if not result.get("measured"):
        return "consommation non mesurable sur ce matériel"
    scope = "" if result.get("on_gpu") else " — calcul sur processeur, GPU au repos"
    return (
        f"{result['total_wh'] * 1000:.0f} mWh · {result['co2_g']:.3f} gCO₂e "
        f"({result['gpu_avg_w']:.0f} W moyens, {result['method']}){scope}"
    )


def log_result(result: dict) -> None:
    bus.log(f"Énergie : {describe(result)}", source="energy")

"""Sonde matérielle : CPU, mémoire, GPU, disponibilité des dépendances."""

from __future__ import annotations

import importlib.util
import ipaddress
import platform
import shutil
import socket
import subprocess
import sys

import psutil

from .config import CACHE_DIR


_VIRTUAL_IFACE_PREFIXES = ("docker", "br-", "veth", "virbr", "tun", "tap", "lxc", "kube")


def local_addresses() -> list[dict]:
    """Adresses IPv4 utilisables pour joindre le serveur depuis le réseau local.

    Les interfaces virtuelles (ponts Docker, tunnels…) sont reléguées en fin de
    liste : elles ne sont presque jamais l'adresse à communiquer.
    """
    found: list[dict] = []
    for iface, addrs in psutil.net_if_addrs().items():
        for addr in addrs:
            if addr.family != socket.AF_INET:
                continue
            try:
                ip = ipaddress.ip_address(addr.address)
            except ValueError:
                continue
            if ip.is_loopback or ip.is_link_local:
                continue
            virtual = iface.startswith(_VIRTUAL_IFACE_PREFIXES)
            found.append(
                {
                    "interface": iface,
                    "ip": str(ip),
                    "private": ip.is_private,
                    "virtual": virtual,
                }
            )
    found.sort(key=lambda item: (item["virtual"], not item["private"], item["interface"]))
    return found


def _has(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def dependency_status() -> dict:
    torch_version = None
    cuda_available = False
    cuda_version = None
    if _has("torch"):
        try:
            import torch  # noqa: PLC0415

            torch_version = torch.__version__
            cuda_available = bool(torch.cuda.is_available())
            cuda_version = getattr(torch.version, "cuda", None)
        except Exception:  # pragma: no cover - environnement dégradé
            torch_version = "erreur d'import"

    aurora_version = None
    if _has("aurora"):
        try:
            import aurora  # noqa: PLC0415

            aurora_version = getattr(aurora, "__version__", "installé")
        except Exception:  # pragma: no cover
            aurora_version = "erreur d'import"

    return {
        "python": sys.version.split()[0],
        "torch": torch_version,
        "aurora": aurora_version,
        "cuda_available": cuda_available,
        "cuda_version": cuda_version,
        "cdsapi": _has("cdsapi"),
        "xarray": _has("xarray"),
        "cfgrib": _has("cfgrib"),
        "netcdf4": _has("netCDF4"),
    }


def gpu_info() -> list[dict]:
    """Informations GPU via torch, sinon via nvidia-smi."""
    gpus: list[dict] = []
    if _has("torch"):
        try:
            import torch  # noqa: PLC0415

            if torch.cuda.is_available():
                for i in range(torch.cuda.device_count()):
                    props = torch.cuda.get_device_properties(i)
                    free, total = torch.cuda.mem_get_info(i)
                    gpus.append(
                        {
                            "index": i,
                            "name": props.name,
                            "total_mb": round(total / 1024**2),
                            "used_mb": round((total - free) / 1024**2),
                            "allocated_mb": round(torch.cuda.memory_allocated(i) / 1024**2),
                            "source": "torch",
                        }
                    )
                return gpus
        except Exception:  # pragma: no cover
            pass

    if shutil.which("nvidia-smi"):
        try:
            out = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=index,name,memory.total,memory.used",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                timeout=6,
                check=False,
            )
            for line in out.stdout.strip().splitlines():
                parts = [p.strip() for p in line.split(",")]
                if len(parts) >= 4:
                    gpus.append(
                        {
                            "index": int(parts[0]),
                            "name": parts[1],
                            "total_mb": int(float(parts[2])),
                            "used_mb": int(float(parts[3])),
                            "allocated_mb": None,
                            "source": "nvidia-smi",
                        }
                    )
        except Exception:  # pragma: no cover
            pass
    return gpus


def system_snapshot() -> dict:
    vm = psutil.virtual_memory()
    disk = psutil.disk_usage(str(CACHE_DIR))
    return {
        "host": platform.node(),
        "platform": f"{platform.system()} {platform.release()}",
        "machine": platform.machine(),
        "cpu_count": psutil.cpu_count(logical=True),
        "cpu_percent": psutil.cpu_percent(interval=None),
        "ram_total_mb": round(vm.total / 1024**2),
        "ram_used_mb": round(vm.used / 1024**2),
        "ram_percent": vm.percent,
        "disk_free_gb": round(disk.free / 1024**3, 1),
        "disk_total_gb": round(disk.total / 1024**3, 1),
        "gpus": gpu_info(),
        "dependencies": dependency_status(),
        "cache_dir": str(CACHE_DIR),
        "addresses": local_addresses(),
    }


def available_devices() -> list[dict]:
    devices = [{"id": "cpu", "label": f"CPU ({psutil.cpu_count(logical=True)} cœurs)"}]
    for gpu in gpu_info():
        devices.append(
            {
                "id": f"cuda:{gpu['index']}",
                "label": f"{gpu['name']} — {gpu['total_mb'] // 1024} Go",
            }
        )
    if _has("torch"):
        try:
            import torch  # noqa: PLC0415

            if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
                devices.append({"id": "mps", "label": "Apple MPS"})
        except Exception:  # pragma: no cover
            pass
    return devices

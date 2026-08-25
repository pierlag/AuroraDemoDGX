"""Tunnel public : exposition temporaire de la station via une URL accessible."""

from __future__ import annotations

import re
import shutil
import subprocess
import threading
import time
from collections import deque

from .config import PORT
from .events import bus

# Chaque fournisseur est décrit par un exécutable, des arguments fixes et le motif
# permettant de reconnaître l'URL publique dans sa sortie. Aucun élément de la
# ligne de commande ne provient de l'utilisateur.
PROVIDERS: dict[str, dict] = {
    "cloudflared": {
        "label": "Cloudflare Quick Tunnel",
        "binary": "cloudflared",
        "args": ["tunnel", "--no-autoupdate", "--url"],
        "pattern": re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com"),
        "anonymous": True,
        "description": (
            "URL publique immédiate, sans compte. Le lien est éphémère et change "
            "à chaque démarrage."
        ),
        "install": "https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/",
    },
    "devtunnel": {
        "label": "Microsoft Dev Tunnels (GitHub)",
        "binary": "devtunnel",
        "args": ["host", "--allow-anonymous", "-p"],
        # Forme : https://{id}-{port}.{region}.devtunnels.ms
        "pattern": re.compile(r"https://[a-z0-9.-]+\.devtunnels\.ms"),
        # devtunnel annonce aussi une URL d'inspection du trafic, à ne pas publier.
        "exclude": re.compile(r"-inspect\."),
        "anonymous": False,
        "description": (
            "Tunnel adossé à un compte GitHub ou Microsoft. Nécessite une "
            "connexion préalable : devtunnel user login -g"
        ),
        "install": "https://learn.microsoft.com/azure/developer/dev-tunnels/get-started",
    },
}


class TunnelManager:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._process: subprocess.Popen | None = None
        self._logs: deque[str] = deque(maxlen=120)
        self._account_cache: tuple[float, str | None] = (0.0, None)
        self.state = "stopped"  # stopped | starting | running | error
        self.provider: str | None = None
        self.url: str | None = None
        self.error: str | None = None
        self.started_at: float | None = None

    # -- description ------------------------------------------------------
    def _devtunnel_account(self) -> str | None:
        """Compte associé au CLI devtunnel, en cache pour ne pas relancer le
        processus à chaque rafraîchissement d'état."""
        stamp, cached = self._account_cache
        if time.time() - stamp < 30.0:
            return cached
        account = None
        binary = shutil.which("devtunnel")
        if binary:
            try:
                out = subprocess.run(  # noqa: S603 - argv fixe
                    [binary, "user", "show"],
                    capture_output=True,
                    text=True,
                    timeout=8,
                    check=False,
                )
                first = out.stdout.strip().splitlines()[0] if out.stdout.strip() else ""
                if first.lower().startswith("logged in"):
                    account = first.rstrip(".")
            except (OSError, subprocess.SubprocessError, IndexError):
                account = None
        self._account_cache = (time.time(), account)
        return account

    def available_providers(self) -> list[dict]:
        out = []
        for key, spec in PROVIDERS.items():
            path = shutil.which(spec["binary"])
            entry = {
                "id": key,
                "label": spec["label"],
                "description": spec["description"],
                "anonymous": spec["anonymous"],
                "available": bool(path),
                "path": path,
                "install": spec["install"],
                "account": None,
                "ready": bool(path),
            }
            if key == "devtunnel" and path:
                entry["account"] = self._devtunnel_account()
                entry["ready"] = bool(entry["account"])
            out.append(entry)
        return out

    def snapshot(self) -> dict:
        return {
            "state": self.state,
            "provider": self.provider,
            "provider_label": PROVIDERS.get(self.provider or "", {}).get("label"),
            "url": self.url,
            "error": self.error,
            "started_at": self.started_at,
            "uptime_s": (time.time() - self.started_at) if self.started_at else None,
            "port": PORT,
            "providers": self.available_providers(),
            "logs": list(self._logs)[-12:],
        }

    def _set(self, **kwargs) -> None:
        with self._lock:
            for key, value in kwargs.items():
                setattr(self, key, value)
        bus.emit("tunnel_state", self.snapshot())

    # -- cycle de vie ------------------------------------------------------
    def start(self, provider: str) -> dict:
        spec = PROVIDERS.get(provider)
        if spec is None:
            raise KeyError(f"Fournisseur de tunnel inconnu : {provider}")
        with self._lock:
            if self.state in ("starting", "running"):
                raise RuntimeError("Un tunnel est déjà actif. Arrêtez-le avant d'en ouvrir un autre.")
            binary = shutil.which(spec["binary"])
            if binary is None:
                raise RuntimeError(
                    f"« {spec['binary']} » est introuvable sur cette machine. "
                    f"Installation : {spec['install']}"
                )
            if provider == "devtunnel" and not self._devtunnel_account():
                raise RuntimeError(
                    "Aucune session devtunnel. Connectez-vous à GitHub dans un "
                    "terminal : devtunnel user login -g"
                )
            self._logs.clear()

        target = f"http://127.0.0.1:{PORT}" if provider == "cloudflared" else str(PORT)
        argv = [binary, *spec["args"], target]

        self._set(state="starting", provider=provider, url=None, error=None, started_at=None)
        bus.log(f"Tunnel : démarrage via {spec['label']}…", source="tunnel")

        try:
            process = subprocess.Popen(  # noqa: S603 - argv fixe, aucune entrée utilisateur
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            self._set(state="error", error=str(exc))
            raise RuntimeError(f"Lancement impossible : {exc}") from exc

        self._process = process
        threading.Thread(target=self._reader, args=(process, spec), daemon=True).start()
        return self.snapshot()

    def _reader(self, process: subprocess.Popen, spec: dict) -> None:
        assert process.stdout is not None
        for raw in process.stdout:
            line = raw.rstrip()
            if not line:
                continue
            self._logs.append(line)
            if self.url is None:
                match = spec["pattern"].search(line)
                exclude = spec.get("exclude")
                if match and not (exclude and exclude.search(match.group(0))):
                    self._set(state="running", url=match.group(0), started_at=time.time())
                    bus.log(f"Tunnel ouvert : {match.group(0)}", level="success", source="tunnel")
            lowered = line.lower()
            if "error" in lowered or "failed" in lowered:
                bus.log(line[:200], level="warn", source="tunnel")

        code = process.wait()
        if self.state != "stopped":
            if self.url:
                bus.log(f"Tunnel fermé (code {code})", level="warn", source="tunnel")
                self._set(state="stopped", url=None, started_at=None)
            else:
                tail = " · ".join(list(self._logs)[-3:]) or f"code {code}"
                self._set(state="error", error=f"Le tunnel n'a pas démarré ({tail})")
                bus.log(f"Échec du tunnel : {tail}", level="error", source="tunnel")
        self._process = None

    def stop(self) -> dict:
        with self._lock:
            process = self._process
        if process is None:
            self._set(state="stopped", url=None, provider=None, started_at=None)
            return self.snapshot()

        self._set(state="stopped", url=None, started_at=None)
        process.terminate()
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill()
        self._process = None
        bus.log("Tunnel fermé", source="tunnel")
        self._set(state="stopped", provider=None)
        return self.snapshot()


tunnel = TunnelManager()

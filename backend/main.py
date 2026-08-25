"""API HTTP de la station de prévision Aurora France."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import secrets
import signal
import time
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from . import forecast as fc
from .config import ADMIN_TOKEN, FRONTEND_DIR, HOST, MAX_STEPS, PORT
from .data_sources import list_sources
from .events import bus
from .geo import CITIES, cities_outside_france, shapes_geojson
from .github_client import session as github
from .model_manager import manager
from .registry import MODELS, VARIABLES
from .system_info import (
    available_devices,
    dependency_status,
    local_addresses,
    system_snapshot,
)
from . import storage
from .tunnel import tunnel


def _is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def require_admin(request: Request) -> None:
    """Protège les opérations d'administration lorsqu'elles viennent du réseau.

    Le cycle de vie du modèle, l'installation de dépendances et la purge du cache
    sont des actions sensibles : elles restent libres depuis la machine hôte, mais
    exigent un jeton dès qu'elles proviennent d'un autre poste.
    """
    client = request.client.host if request.client else ""
    if _is_loopback(client):
        return
    supplied = request.headers.get("X-Aurora-Token", "")
    if ADMIN_TOKEN and secrets.compare_digest(supplied, ADMIN_TOKEN):
        return
    raise HTTPException(
        status_code=403,
        detail=(
            "Opération d'administration refusée depuis le réseau. "
            + (
                "Jeton absent ou invalide."
                if ADMIN_TOKEN
                else "Aucun jeton configuré : démarrez le serveur avec "
                "AURORA_ADMIN_TOKEN=… pour autoriser l'administration à distance."
            )
        ),
    )


ADMIN = [Depends(require_admin)]

# Cadence du flux SSE : réveil court (réactivité à l'arrêt), ping espacé (trafic).
_SSE_POLL = 1.0
_SSE_PING = 20.0


def _announce_shutdown_to_streams() -> None:
    """Prévient les flux SSE dès le premier Ctrl+C.

    Uvicorn attend la fermeture des connexions actives *avant* d'exécuter l'arrêt
    du `lifespan`. Un flux SSE ne se terminant jamais de lui-même, le serveur
    resterait bloqué jusqu'à un second Ctrl+C, qui annule brutalement les tâches
    ASGI (d'où la pile `CancelledError`). On se greffe donc sur le gestionnaire de
    signal installé par uvicorn pour libérer les flux avant qu'il n'attende.
    """
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            previous = signal.getsignal(sig)
        except ValueError:  # hors du thread principal
            return
        if not callable(previous):  # SIG_DFL / SIG_IGN : on ne touche à rien
            continue

        def handler(signum: int, frame, _previous=previous) -> None:
            bus.request_shutdown()
            _previous(signum, frame)

        try:
            signal.signal(sig, handler)
        except ValueError:
            return


@asynccontextmanager
async def lifespan(_app: FastAPI):
    bus.bind_loop(asyncio.get_running_loop())
    _announce_shutdown_to_streams()
    bus.log("Station Aurora France démarrée", level="success")
    snap = system_snapshot()
    bus.log(
        f"{snap['platform']} · {snap['cpu_count']} cœurs · "
        f"{snap['ram_total_mb'] // 1024} Go RAM · "
        f"{len(snap['gpus'])} GPU détecté(s)",
    )
    if HOST in ("0.0.0.0", "::"):  # noqa: S104
        for addr in local_addresses():
            bus.log(f"Accessible sur http://{addr['ip']}:{PORT} ({addr['interface']})")
        bus.log(
            "Administration à distance "
            + ("protégée par jeton" if ADMIN_TOKEN else "désactivée (aucun jeton configuré)"),
            level="info" if ADMIN_TOKEN else "warn",
        )
    stored = storage.stats()
    bus.log(
        f"Historique : {stored['count']} prévision(s) sur disque "
        f"({stored['bytes'] / 1024**2:.1f} Mo)",
    )
    strays = cities_outside_france()
    if strays:
        bus.log(
            f"{len(strays)} ville(s) hors du contour national : {', '.join(strays)}",
            level="warn",
        )
    yield


app = FastAPI(title="Aurora France", version="1.0.0", docs_url="/api/docs", lifespan=lifespan)
api = APIRouter(prefix="/api")


class LoadRequest(BaseModel):
    model_id: str
    device: str = "cpu"
    use_lora: bool = True


class ForecastRequest(BaseModel):
    source: str = "synthetic"
    base_time: str | None = None
    steps: int = Field(default=20, ge=1, le=MAX_STEPS)
    members: int = Field(default=1, ge=1, le=8)


class InstallRequest(BaseModel):
    extras: list[str] = []


class TunnelRequest(BaseModel):
    provider: str = "cloudflared"


class PublishRequest(BaseModel):
    repo: str = Field(min_length=3, max_length=201)


# ---------------------------------------------------------------------------
# Métadonnées
# ---------------------------------------------------------------------------


@api.get("/bootstrap")
def bootstrap(request: Request) -> dict:
    return {
        "geo": shapes_geojson(),
        "cities": CITIES,
        "variables": VARIABLES,
        "models": MODELS,
        "sources": list_sources(),
        "devices": available_devices(),
        "model_state": manager.snapshot(),
        "system": system_snapshot(),
        "forecasts": fc.list_forecasts(),
        "storage": storage.stats(),
        "default_base_time": fc.default_base_time().isoformat(),
        "max_steps": MAX_STEPS,
        "tunnel": tunnel.snapshot(),
        "github": github.snapshot(),
        "access": {
            "client": request.client.host if request.client else None,
            "local_client": _is_loopback(request.client.host if request.client else ""),
            "exposed": HOST in ("0.0.0.0", "::"),  # noqa: S104
            "token_required": bool(ADMIN_TOKEN),
            "port": PORT,
            "addresses": local_addresses(),
        },
    }


@api.get("/system")
def system() -> dict:
    return system_snapshot()


@api.get("/dependencies")
def dependencies() -> dict:
    return dependency_status()


# ---------------------------------------------------------------------------
# Modèle
# ---------------------------------------------------------------------------


@api.get("/models")
def models() -> dict:
    return {"models": MODELS, "state": manager.snapshot(), "devices": available_devices()}


@api.get("/models/state")
def model_state() -> dict:
    return manager.snapshot()


@api.post("/models/preflight")
def preflight(req: LoadRequest) -> dict:
    return {"checks": manager.preflight(req.model_id, req.device)}


@api.post("/models/load", dependencies=ADMIN)
def load_model(req: LoadRequest) -> dict:
    try:
        return manager.load(req.model_id, req.device, req.use_lora)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@api.post("/models/unload", dependencies=ADMIN)
def unload_model() -> dict:
    try:
        return manager.unload()
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@api.get("/models/cache")
def cache_info() -> dict:
    return manager.cache_info()


@api.delete("/models/cache", dependencies=ADMIN)
def purge_cache() -> dict:
    try:
        return manager.purge_cache()
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@api.post("/dependencies/install", dependencies=ADMIN)
def install_dependencies(req: InstallRequest) -> dict:
    try:
        manager.install_dependencies(req.extras)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"started": True}


# ---------------------------------------------------------------------------
# Prévisions
# ---------------------------------------------------------------------------


@api.post("/forecast")
def start_forecast(req: ForecastRequest) -> dict:
    if not manager.is_ready:
        raise HTTPException(
            status_code=409,
            detail="Aucun modèle chargé. Ouvrez la console d'administration pour en charger un.",
        )
    if req.base_time:
        try:
            base_time = datetime.fromisoformat(req.base_time.replace("Z", "+00:00")).replace(
                tzinfo=None
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Date d'initialisation invalide.") from exc
    else:
        base_time = fc.default_base_time()

    sources = {s["id"]: s for s in list_sources()}
    src = sources.get(req.source)
    if src is None:
        raise HTTPException(status_code=422, detail=f"Source inconnue : {req.source}")
    if not src["available"]:
        raise HTTPException(status_code=409, detail=f"Source indisponible : {src['reason']}")

    return fc.create_job(
        {
            "source": req.source,
            "base_time": base_time,
            "steps": fc.clamp_steps(req.steps),
            "members": req.members,
        }
    )


@api.get("/jobs/{job_id}")
def job_status(job_id: str) -> dict:
    job = fc.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Travail inconnu")
    return job


@api.get("/forecasts")
def forecasts() -> dict:
    return {"forecasts": fc.list_forecasts(), "storage": storage.stats()}


@api.get("/forecasts/storage")
def forecasts_storage() -> dict:
    return storage.stats()


@api.delete("/forecasts", dependencies=ADMIN)
def delete_forecasts() -> dict:
    removed = fc.delete_all_forecasts()
    return {"removed": removed, "storage": storage.stats()}


@api.get("/forecasts/{forecast_id}")
def forecast_detail(forecast_id: str) -> dict:
    result = fc.get_forecast(forecast_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Prévision inconnue ou expirée")
    return {"meta": result["meta"], "cities": result["cities"]}


@api.delete("/forecasts/{forecast_id}", dependencies=ADMIN)
def delete_forecast(forecast_id: str) -> dict:
    if not fc.delete_forecast(forecast_id):
        raise HTTPException(status_code=404, detail="Prévision inconnue")
    return {"deleted": forecast_id, "storage": storage.stats()}


@api.get("/forecasts/{forecast_id}/field/{var}")
def forecast_field(forecast_id: str, var: str) -> JSONResponse:
    payload = fc.field_payload(forecast_id, var)
    if payload is None:
        raise HTTPException(status_code=404, detail="Champ indisponible pour cette prévision")
    return JSONResponse(payload, headers={"Cache-Control": "public, max-age=600"})


@api.get("/forecasts/{forecast_id}/wind")
def forecast_wind(forecast_id: str) -> JSONResponse:
    payload = fc.wind_payload(forecast_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="Champ de vent indisponible")
    return JSONResponse(payload, headers={"Cache-Control": "public, max-age=600"})


@api.get("/forecasts/{forecast_id}/point")
def forecast_point(
    forecast_id: str,
    lat: float = Query(..., ge=-90, le=90),
    lon: float = Query(..., ge=-180, le=180),
) -> dict:
    payload = fc.point_series(forecast_id, lat, lon)
    if payload is None:
        raise HTTPException(status_code=404, detail="Prévision inconnue")
    return payload


# ---------------------------------------------------------------------------
# Tunnel public
# ---------------------------------------------------------------------------


@api.get("/tunnel")
def tunnel_state() -> dict:
    return tunnel.snapshot()


@api.post("/tunnel/start", dependencies=ADMIN)
def tunnel_start(req: TunnelRequest) -> dict:
    try:
        return tunnel.start(req.provider)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@api.post("/tunnel/stop", dependencies=ADMIN)
def tunnel_stop() -> dict:
    return tunnel.stop()


# ---------------------------------------------------------------------------
# GitHub
# ---------------------------------------------------------------------------


@api.get("/github")
def github_state() -> dict:
    return github.snapshot()


@api.post("/github/login", dependencies=ADMIN)
def github_login() -> dict:
    try:
        return github.start_login()
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@api.post("/github/login/poll", dependencies=ADMIN)
def github_login_poll() -> dict:
    try:
        return github.poll_login()
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@api.post("/github/logout", dependencies=ADMIN)
def github_logout() -> dict:
    return github.logout()


@api.get("/github/repos", dependencies=ADMIN)
def github_repos(only_private: bool = Query(default=True)) -> dict:
    try:
        return {"repos": github.repositories(only_private=only_private)}
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@api.post("/github/publish", dependencies=ADMIN)
def github_publish(req: PublishRequest) -> dict:
    state = tunnel.snapshot()
    if not state["url"]:
        raise HTTPException(
            status_code=409,
            detail="Aucun tunnel actif : ouvrez d'abord une URL publique.",
        )
    model = manager.snapshot()
    try:
        return github.publish_demo(
            req.repo,
            state["url"],
            {
                "provider_label": state["provider_label"],
                "model_name": model.get("model_name"),
                "device": model.get("device"),
                "forecasts": len(fc.list_forecasts()),
            },
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Flux temps réel
# ---------------------------------------------------------------------------


@api.get("/logs")
def logs() -> dict:
    return {"logs": bus.history()}


@api.get("/events")
async def events() -> StreamingResponse:
    queue = bus.subscribe()

    async def stream():
        try:
            hello = {"type": "hello", "data": manager.snapshot()}
            yield f"data: {json.dumps(hello)}\n\n"
            last_ping = time.monotonic()
            # Réveil fréquent : le flux doit pouvoir se clore rapidement quand le
            # serveur s'arrête, sans attendre l'intervalle complet du ping.
            while not bus.closing:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=_SSE_POLL)
                except asyncio.TimeoutError:
                    if time.monotonic() - last_ping >= _SSE_PING:
                        last_ping = time.monotonic()
                        yield ": ping\n\n"
                    continue
                yield f"data: {json.dumps(event, default=str)}\n\n"
                last_ping = time.monotonic()
        finally:
            bus.unsubscribe(queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


app.include_router(api)


# ---------------------------------------------------------------------------
# Front-end statique
# ---------------------------------------------------------------------------


class NoCacheAssets:
    """Sert les fichiers du front sans cache : outil local, itératif.

    Middleware ASGI pur plutôt que `@app.middleware("http")` : ce dernier repose
    sur `BaseHTTPMiddleware`, qui recopie chaque réponse dans un flux mémoire —
    inadapté au SSE (latence, mémoire) et source de piles `CancelledError` à
    l'arrêt du serveur.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"].startswith("/api"):
            await self.app(scope, receive, send)
            return

        async def send_no_cache(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)["Cache-Control"] = "no-store, must-revalidate"
            await send(message)

        await self.app(scope, receive, send_no_cache)


app.add_middleware(NoCacheAssets)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/admin")
def admin() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "admin.html")


app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="static")

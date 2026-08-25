"""Authentification GitHub (device flow) et publication de la démonstration."""

from __future__ import annotations

import json
import os
import re
import threading
import time

import httpx

from .config import DEMO_ISSUE_TITLE, GITHUB_CLIENT_ID, GITHUB_TOKEN_FILE
from .events import bus

DEVICE_CODE_URL = "https://github.com/login/device/code"
ACCESS_TOKEN_URL = "https://github.com/login/oauth/access_token"
API = "https://api.github.com"
SCOPE = "repo read:user"

# `owner/repo` strictement : empêche toute injection de segment dans l'URL d'API.
REPO_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}/[A-Za-z0-9._-]{1,100}$")

_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "aurora-france-station",
}


class GitHubSession:
    """Conserve le jeton côté serveur. Il n'est jamais transmis au navigateur."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._token: str | None = None
        self._user: dict | None = None
        self._device: dict | None = None
        self.load()

    # -- persistance -------------------------------------------------------
    def load(self) -> None:
        if not GITHUB_TOKEN_FILE.exists():
            return
        try:
            data = json.loads(GITHUB_TOKEN_FILE.read_text(encoding="utf-8"))
            self._token = data.get("token")
            self._user = data.get("user")
        except (OSError, json.JSONDecodeError):
            self._token = None

    def _persist(self) -> None:
        if self._token is None:
            GITHUB_TOKEN_FILE.unlink(missing_ok=True)
            return
        payload = {"token": self._token, "user": self._user, "saved": time.time()}
        # Écriture avec permissions restreintes dès la création du fichier.
        fd = os.open(GITHUB_TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)

    # -- état --------------------------------------------------------------
    def snapshot(self) -> dict:
        pending = self._device or {}
        return {
            "configured": bool(GITHUB_CLIENT_ID),
            "client_id": GITHUB_CLIENT_ID or None,
            "authenticated": bool(self._token),
            "user": self._user,
            "pending": bool(pending.get("device_code")),
            "verification_uri": pending.get("verification_uri"),
            "user_code": pending.get("user_code"),
            "expires_at": pending.get("expires_at"),
            "issue_title": DEMO_ISSUE_TITLE,
        }

    def _client(self) -> httpx.Client:
        if not self._token:
            raise RuntimeError("Aucune session GitHub. Connectez-vous d'abord.")
        return httpx.Client(
            base_url=API,
            headers={**_HEADERS, "Authorization": f"Bearer {self._token}"},
            timeout=25.0,
        )

    # -- device flow -------------------------------------------------------
    def start_login(self) -> dict:
        if not GITHUB_CLIENT_ID:
            raise RuntimeError(
                "GITHUB_CLIENT_ID absent : renseignez-le dans le fichier .env à la racine."
            )
        response = httpx.post(
            DEVICE_CODE_URL,
            data={"client_id": GITHUB_CLIENT_ID, "scope": SCOPE},
            headers={"Accept": "application/json", "User-Agent": _HEADERS["User-Agent"]},
            timeout=20.0,
        )
        payload = response.json()
        if "error" in payload:
            raise RuntimeError(_explain(payload))

        self._device = {
            "device_code": payload["device_code"],
            "user_code": payload["user_code"],
            "verification_uri": payload.get("verification_uri", "https://github.com/login/device"),
            "interval": max(int(payload.get("interval", 5)), 5),
            "expires_at": time.time() + int(payload.get("expires_in", 900)),
            "last_poll": 0.0,
        }
        bus.log(
            f"GitHub : saisissez le code {payload['user_code']} sur "
            f"{self._device['verification_uri']}",
            source="github",
        )
        return self.snapshot()

    def poll_login(self) -> dict:
        device = self._device
        if not device:
            raise RuntimeError("Aucune demande de connexion en cours.")
        if time.time() > device["expires_at"]:
            self._device = None
            raise RuntimeError("Le code a expiré. Relancez la connexion.")
        # GitHub impose un intervalle minimal entre deux sondages.
        wait = device["interval"] - (time.time() - device["last_poll"])
        if wait > 0:
            return {**self.snapshot(), "status": "pending", "retry_in": round(wait, 1)}
        device["last_poll"] = time.time()

        response = httpx.post(
            ACCESS_TOKEN_URL,
            data={
                "client_id": GITHUB_CLIENT_ID,
                "device_code": device["device_code"],
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            },
            headers={"Accept": "application/json", "User-Agent": _HEADERS["User-Agent"]},
            timeout=20.0,
        )
        payload = response.json()
        error = payload.get("error")

        if error == "authorization_pending":
            return {**self.snapshot(), "status": "pending", "retry_in": device["interval"]}
        if error == "slow_down":
            device["interval"] = int(payload.get("interval", device["interval"] + 5))
            return {**self.snapshot(), "status": "pending", "retry_in": device["interval"]}
        if error:
            self._device = None
            raise RuntimeError(_explain(payload))

        self._token = payload["access_token"]
        self._device = None
        self._user = self._fetch_user()
        self._persist()
        bus.log(
            f"GitHub : connecté en tant que {self._user.get('login')}",
            level="success",
            source="github",
        )
        return {**self.snapshot(), "status": "done"}

    def logout(self) -> dict:
        self._token = None
        self._user = None
        self._device = None
        self._persist()
        bus.log("GitHub : session fermée", source="github")
        return self.snapshot()

    def _fetch_user(self) -> dict:
        with self._client() as client:
            data = _check(client.get("/user")).json()
        return {
            "login": data.get("login"),
            "name": data.get("name"),
            "avatar_url": data.get("avatar_url"),
            "html_url": data.get("html_url"),
        }

    # -- dépôts ------------------------------------------------------------
    def repositories(self, only_private: bool = True) -> list[dict]:
        collected: list[dict] = []
        with self._client() as client:
            for page in range(1, 4):  # 300 dépôts au maximum
                response = _check(
                    client.get(
                        "/user/repos",
                        params={
                            "per_page": 100,
                            "page": page,
                            "sort": "updated",
                            "affiliation": "owner,collaborator,organization_member",
                        },
                    )
                )
                chunk = response.json()
                if not chunk:
                    break
                collected.extend(chunk)
                if len(chunk) < 100:
                    break

        repos = [
            {
                "full_name": item["full_name"],
                "private": item["private"],
                "description": item.get("description"),
                "updated_at": item.get("updated_at"),
                "has_issues": item.get("has_issues", True),
                "permissions": (item.get("permissions") or {}).get("push", False),
            }
            for item in collected
            if item.get("has_issues", True)
        ]
        if only_private:
            repos = [r for r in repos if r["private"]]
        return repos

    # -- publication -------------------------------------------------------
    def publish_demo(self, repo: str, url: str, extra: dict | None = None) -> dict:
        if not REPO_RE.match(repo):
            raise ValueError(f"Nom de dépôt invalide : {repo}")
        if not url.startswith("https://"):
            raise ValueError("L'URL du tunnel doit être en HTTPS.")

        body = _issue_body(url, extra or {})
        with self._client() as client:
            existing = self._find_issue(client, repo)
            if existing is None:
                created = _check(
                    client.post(
                        f"/repos/{repo}/issues",
                        json={"title": DEMO_ISSUE_TITLE, "body": body},
                    )
                ).json()
                bus.log(
                    f"GitHub : issue « {DEMO_ISSUE_TITLE} » créée dans {repo}",
                    level="success",
                    source="github",
                )
                return {
                    "action": "created",
                    "number": created["number"],
                    "html_url": created["html_url"],
                    "repo": repo,
                }

            comment = _check(
                client.post(
                    f"/repos/{repo}/issues/{existing['number']}/comments",
                    json={"body": body},
                )
            ).json()
            if existing.get("state") == "closed":
                _check(client.patch(f"/repos/{repo}/issues/{existing['number']}",
                                    json={"state": "open"}))
            bus.log(
                f"GitHub : commentaire ajouté à l'issue #{existing['number']} de {repo}",
                level="success",
                source="github",
            )
            return {
                "action": "commented",
                "number": existing["number"],
                "html_url": comment["html_url"],
                "repo": repo,
            }

    def _find_issue(self, client: httpx.Client, repo: str) -> dict | None:
        for page in range(1, 4):
            response = _check(
                client.get(
                    f"/repos/{repo}/issues",
                    params={"state": "all", "per_page": 100, "page": page},
                )
            )
            items = response.json()
            if not items:
                return None
            for item in items:
                # L'API des issues renvoie aussi les pull requests : on les écarte.
                if "pull_request" in item:
                    continue
                if item.get("title", "").strip().casefold() == DEMO_ISSUE_TITLE.casefold():
                    return item
            if len(items) < 100:
                return None
        return None


def _issue_body(url: str, extra: dict) -> str:
    stamp = time.strftime("%d/%m/%Y à %H:%M:%S", time.localtime())
    lines = [
        "### Démonstration publique — Aurora France",
        "",
        f"**URL** : {url}",
        "",
        f"- Ouverte le {stamp}",
    ]
    if extra.get("provider_label"):
        lines.append(f"- Tunnel : {extra['provider_label']}")
    if extra.get("model_name"):
        lines.append(f"- Modèle chargé : {extra['model_name']} ({extra.get('device', '—')})")
    if extra.get("forecasts") is not None:
        lines.append(f"- Prévisions en mémoire : {extra['forecasts']}")
    lines += [
        "",
        "> Ce lien est **temporaire** : il cesse de fonctionner dès l'arrêt du tunnel "
        "ou de la station, et change à chaque réouverture.",
    ]
    return "\n".join(lines)


def _explain(payload: dict) -> str:
    error = payload.get("error", "")
    messages = {
        "device_flow_disabled": (
            "Le « device flow » est désactivé sur cette application GitHub. "
            "Activez-le dans Settings → Developer settings → votre application."
        ),
        "expired_token": "Le code a expiré. Relancez la connexion.",
        "access_denied": "Autorisation refusée sur GitHub.",
        "incorrect_client_credentials": "GITHUB_CLIENT_ID invalide.",
        "unsupported_grant_type": "Type d'autorisation non pris en charge.",
    }
    return messages.get(error, payload.get("error_description") or f"Erreur GitHub : {error}")


def _check(response: httpx.Response) -> httpx.Response:
    if response.status_code == 401:
        raise RuntimeError("Session GitHub expirée ou révoquée. Reconnectez-vous.")
    if response.status_code == 403 and "rate limit" in response.text.lower():
        raise RuntimeError("Limite de requêtes GitHub atteinte. Réessayez plus tard.")
    if response.status_code == 404:
        raise RuntimeError(
            "Ressource introuvable : le dépôt n'existe pas, les issues y sont "
            "désactivées, ou l'application GitHub n'y est pas installée."
        )
    if response.status_code >= 400:
        try:
            detail = response.json().get("message", response.text[:200])
        except ValueError:
            detail = response.text[:200]
        raise RuntimeError(f"GitHub {response.status_code} : {detail}")
    return response


session = GitHubSession()

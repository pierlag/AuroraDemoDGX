"""Configuration centrale de l'application Aurora France."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
FRONTEND_DIR = ROOT / "frontend"

# Les réglages locaux (identifiant OAuth GitHub…) vivent dans un .env non versionné.
load_dotenv(ROOT / ".env")

DATA_DIR = Path(os.environ.get("AURORA_DATA_DIR", ROOT / "data"))
CACHE_DIR = DATA_DIR / "cache"
FORECAST_DIR = DATA_DIR / "forecasts"
STATE_DIR = DATA_DIR / "state"

for _d in (DATA_DIR, CACHE_DIR, FORECAST_DIR, STATE_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# 0.0.0.0 : écoute sur la boucle locale *et* sur les adresses du réseau local.
HOST = os.environ.get("AURORA_HOST", "0.0.0.0")  # noqa: S104 - exposition LAN assumée
PORT = int(os.environ.get("AURORA_PORT", "8077"))

# Jeton protégeant les opérations d'administration depuis le réseau. Les clients
# en boucle locale en sont toujours dispensés. S'il n'est pas défini, les postes
# distants n'ont accès qu'à la consultation.
ADMIN_TOKEN = os.environ.get("AURORA_ADMIN_TOKEN", "").strip()

# Identifiant OAuth GitHub (public par nature) utilisé pour le device flow.
GITHUB_CLIENT_ID = os.environ.get("GITHUB_CLIENT_ID", "").strip()
GITHUB_TOKEN_FILE = STATE_DIR / "github_token.json"

# Titre de l'issue de publication de la démonstration.
DEMO_ISSUE_TITLE = os.environ.get("AURORA_DEMO_ISSUE_TITLE", "Public demo").strip()

# Emprise géographique de la France métropolitaine (+ Corse et marge maritime).
LAT_MAX = 51.6
LAT_MIN = 41.0
LON_MIN = -6.0
LON_MAX = 10.5

# Résolution de la grille de restitution (degrés). 0.1° -> ~166 x 107 points.
RENDER_STEP = 0.1

# Pas de temps de base du modèle Aurora (heures).
BASE_TIMESTEP_H = 6

MAX_STEPS = 40  # 40 x 6 h = 10 jours

# Répertoire de cache HuggingFace utilisé pour les poids Aurora.
HF_HOME = os.environ.get("HF_HOME", str(CACHE_DIR / "huggingface"))
os.environ.setdefault("HF_HOME", HF_HOME)

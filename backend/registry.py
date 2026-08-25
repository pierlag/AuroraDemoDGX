"""Catalogue des modèles Aurora exposés par la console d'administration."""

from __future__ import annotations

# engine == "aurora"     -> nécessite torch + microsoft-aurora, poids HuggingFace
# engine == "simulation" -> moteur physique local, aucune dépendance lourde
MODELS: list[dict] = [
    {
        "id": "simulation",
        "engine": "simulation",
        "name": "Simulateur atmosphérique local",
        "family": "Démonstration",
        "cls": None,
        "checkpoint": None,
        "resolution": "0.1°",
        "grid": "166 × 107 (France)",
        "params": "—",
        "download_mb": 0,
        "min_ram_gb": 1,
        "min_vram_gb": 0,
        "timestep_h": 6,
        "supports": ["2t", "wind10", "gust", "msl", "precip", "tcc", "rh", "t850", "z500"],
        "description": (
            "Moteur barotrope géostrophique embarqué : centres d'action mobiles, "
            "advection thermique, effet orographique. Aucune donnée réelle — sert à "
            "valider la chaîne de traitement et à explorer l'interface hors ligne."
        ),
        "real_data": False,
    },
    {
        "id": "aurora-0.25-small-pretrained",
        "engine": "aurora",
        "name": "Aurora 0.25° Small Pretrained",
        "family": "Pré-entraîné",
        "cls": "AuroraSmallPretrained",
        "checkpoint": "aurora-0.25-small-pretrained.ckpt",
        "resolution": "0.25°",
        "grid": "721 × 1440",
        "params": "≈ 60 M",
        "download_mb": 500,
        "min_ram_gb": 8,
        "min_vram_gb": 4,
        "timestep_h": 6,
        "supports": ["2t", "wind10", "gust", "msl", "rh", "t850", "z500"],
        "description": (
            "Version réduite destinée au débogage. Utile pour vérifier l'installation "
            "et la chaîne d'inférence sans mobiliser 40 Go de VRAM."
        ),
        "real_data": True,
    },
    {
        "id": "aurora-0.25-pretrained",
        "engine": "aurora",
        "name": "Aurora 0.25° Pretrained",
        "family": "Pré-entraîné",
        "cls": "AuroraPretrained",
        "checkpoint": "aurora-0.25-pretrained.ckpt",
        "resolution": "0.25°",
        "grid": "721 × 1440",
        "params": "≈ 1.3 Md",
        "download_mb": 5300,
        "min_ram_gb": 48,
        "min_vram_gb": 40,
        "timestep_h": 6,
        "supports": ["2t", "wind10", "gust", "msl", "rh", "t850", "z500"],
        "description": (
            "Modèle de fondation généraliste. Recommandé pour des conditions initiales "
            "ERA5 et pour tout jeu de données ne disposant pas de version spécialisée."
        ),
        "real_data": True,
    },
    {
        "id": "aurora-0.25-finetuned",
        "engine": "aurora",
        "name": "Aurora 0.25° Fine-Tuned",
        "family": "Spécialisé météo",
        "cls": "Aurora",
        "checkpoint": "aurora-0.25-finetuned.ckpt",
        "resolution": "0.25°",
        "grid": "721 × 1440",
        "params": "≈ 1.3 Md",
        "download_mb": 5300,
        "min_ram_gb": 48,
        "min_vram_gb": 40,
        "timestep_h": 6,
        "supports": ["2t", "wind10", "gust", "msl", "rh", "t850", "z500"],
        "description": (
            "Affiné sur IFS HRES T0 : meilleure version 0.25° pour la prévision "
            "déterministe. LoRA activé par défaut (désactivable pour des champs plus réalistes)."
        ),
        "real_data": True,
    },
    {
        "id": "aurora-0.25-v1.5",
        "engine": "aurora",
        "name": "Aurora 1.5 (0.25°)",
        "family": "Spécialisé météo",
        "cls": "AuroraV1p5",
        "checkpoint": "aurora-0.25-v1.5.ckpt",
        "resolution": "0.25°",
        "grid": "721 × 1440",
        "params": "≈ 1.3 Md",
        "download_mb": 5600,
        "min_ram_gb": 40,
        "min_vram_gb": 32,
        "timestep_h": 6,
        "supports": ["2t", "wind10", "gust", "msl", "precip", "tcc", "rh", "t850", "z500"],
        "fine_lead_times": True,
        "description": (
            "26 variables de surface, 36 variables statiques, pas de temps horaire via "
            "les lead-time embeddings, précipitations et couverture nuageuse en sortie."
        ),
        "real_data": True,
    },
    {
        "id": "aurora-0.25-v1.5-ensemble",
        "engine": "aurora",
        "name": "Aurora 1.5 Ensemble (0.25°)",
        "family": "Probabiliste",
        "cls": "AuroraV1p5Ensemble",
        "checkpoint": "aurora-0.25-v1.5-ensemble.ckpt",
        "resolution": "0.25°",
        "grid": "721 × 1440",
        "params": "≈ 1.3 Md",
        "download_mb": 5600,
        "min_ram_gb": 40,
        "min_vram_gb": 32,
        "timestep_h": 6,
        "supports": ["2t", "wind10", "gust", "msl", "precip", "tcc", "rh", "t850", "z500"],
        "fine_lead_times": True,
        "ensemble": True,
        "description": (
            "Version stochastique d'Aurora 1.5 : injection de bruit dans le backbone pour "
            "générer des membres d'ensemble et quantifier l'incertitude."
        ),
        "real_data": True,
    },
    {
        "id": "aurora-0.25-12h-pretrained",
        "engine": "aurora",
        "name": "Aurora 0.25° 12 h Pretrained",
        "family": "Pré-entraîné",
        "cls": "Aurora12hPretrained",
        "checkpoint": "aurora-0.25-12h-pretrained.ckpt",
        "resolution": "0.25°",
        "grid": "721 × 1440",
        "params": "≈ 1.3 Md",
        "download_mb": 5300,
        "min_ram_gb": 48,
        "min_vram_gb": 40,
        "timestep_h": 12,
        "supports": ["2t", "wind10", "gust", "msl", "rh", "t850", "z500"],
        "description": "Pas de temps de 12 h : moins d'itérations pour les longues échéances.",
        "real_data": True,
    },
    {
        "id": "aurora-0.1-finetuned",
        "engine": "aurora",
        "name": "Aurora 0.1° Fine-Tuned",
        "family": "Haute résolution",
        "cls": "AuroraHighRes",
        "checkpoint": "aurora-0.1-finetuned.ckpt",
        "resolution": "0.1°",
        "grid": "1801 × 3600",
        "params": "≈ 1.3 Md",
        "download_mb": 5400,
        "min_ram_gb": 64,
        "min_vram_gb": 80,
        "timestep_h": 6,
        "supports": ["2t", "wind10", "gust", "msl", "rh", "t850", "z500"],
        "description": (
            "Résolution 0.1° (~11 km) sur IFS HRES analysis. Restitue le détail "
            "orographique des Alpes, des Pyrénées et des littoraux."
        ),
        "real_data": True,
    },
    {
        "id": "aurora-0.4-air-pollution",
        "engine": "aurora",
        "name": "Aurora 0.4° Air Pollution",
        "family": "Qualité de l'air",
        "cls": "AuroraAirPollution",
        "checkpoint": "aurora-0.4-air-pollution.ckpt",
        "resolution": "0.4°",
        "grid": "451 × 900",
        "params": "≈ 1.3 Md",
        "download_mb": 5400,
        "min_ram_gb": 48,
        "min_vram_gb": 40,
        "timestep_h": 12,
        "supports": ["2t", "wind10", "msl", "pm2p5", "pm10", "no2", "o3"],
        "description": (
            "Affiné sur les réanalyses CAMS : particules fines, NO₂, SO₂, ozone. "
            "Conditions initiales CAMS analysis requises."
        ),
        "real_data": True,
    },
    {
        "id": "aurora-0.25-wave",
        "engine": "aurora",
        "name": "Aurora 0.25° Wave",
        "family": "Océan",
        "cls": "AuroraWave",
        "checkpoint": "aurora-0.25-wave.ckpt",
        "resolution": "0.25°",
        "grid": "721 × 1440",
        "params": "≈ 1.3 Md",
        "download_mb": 5400,
        "min_ram_gb": 48,
        "min_vram_gb": 40,
        "timestep_h": 6,
        "supports": ["2t", "wind10", "msl", "swh", "mwd", "mwp"],
        "description": (
            "État de mer : hauteur significative, direction et période des vagues. "
            "Pertinent pour le golfe de Gascogne, la Manche et le golfe du Lion."
        ),
        "real_data": True,
    },
]

MODELS_BY_ID = {m["id"]: m for m in MODELS}


VARIABLES: dict[str, dict] = {
    "2t": {
        "label": "Température à 2 m",
        "short": "Température",
        "unit": "°C",
        "palette": "temperature",
        "domain": [-18, 42],
        "decimals": 1,
        "aurora_var": "2t",
    },
    "wind10": {
        "label": "Vent moyen à 10 m",
        "short": "Vent",
        "unit": "km/h",
        "palette": "wind",
        "domain": [0, 130],
        "decimals": 0,
        "aurora_var": "10u/10v",
    },
    "gust": {
        "label": "Rafales estimées",
        "short": "Rafales",
        "unit": "km/h",
        "palette": "wind",
        "domain": [0, 180],
        "decimals": 0,
        "aurora_var": "dérivé",
    },
    "msl": {
        "label": "Pression au niveau de la mer",
        "short": "Pression",
        "unit": "hPa",
        "palette": "pressure",
        "domain": [975, 1042],
        "decimals": 1,
        "aurora_var": "msl",
    },
    "precip": {
        "label": "Précipitations",
        "short": "Pluie",
        "unit": "mm/h",
        "palette": "precip",
        "domain": [0, 14],
        "decimals": 1,
        "aurora_var": "tp",
    },
    "tcc": {
        "label": "Couverture nuageuse",
        "short": "Nuages",
        "unit": "%",
        "palette": "cloud",
        "domain": [0, 100],
        "decimals": 0,
        "aurora_var": "tcc",
    },
    "rh": {
        "label": "Humidité relative",
        "short": "Humidité",
        "unit": "%",
        "palette": "humidity",
        "domain": [15, 100],
        "decimals": 0,
        "aurora_var": "q (1000 hPa)",
    },
    "t850": {
        "label": "Température 850 hPa",
        "short": "T 850",
        "unit": "°C",
        "palette": "temperature850",
        "domain": [-28, 26],
        "decimals": 1,
        "aurora_var": "t @ 850",
    },
    "z500": {
        "label": "Géopotentiel 500 hPa",
        "short": "Z 500",
        "unit": "dam",
        "palette": "geopotential",
        "domain": [498, 600],
        "decimals": 0,
        "aurora_var": "z @ 500",
    },
}

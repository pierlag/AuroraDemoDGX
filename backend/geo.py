"""Géométrie : contours de la France, villes, masque terre/mer et relief."""

from __future__ import annotations

import numpy as np

from .config import LAT_MAX, LAT_MIN, LON_MAX, LON_MIN, RENDER_STEP

# ---------------------------------------------------------------------------
# Contours (lon, lat) — tracé stylisé mais fidèle des frontières françaises.
# ---------------------------------------------------------------------------

FRANCE_MAINLAND = [
    (2.38, 51.03), (1.85, 50.95), (1.58, 50.87), (1.61, 50.72), (1.55, 50.22),
    (1.08, 49.93), (0.68, 49.86), (0.20, 49.71), (0.05, 49.52), (0.13, 49.43),
    (0.23, 49.42), (0.07, 49.36),
    (-0.25, 49.28), (-0.45, 49.33), (-0.62, 49.34), (-1.05, 49.39), (-1.24, 49.42),
    (-1.27, 49.59), (-1.26, 49.67), (-1.62, 49.64), (-1.94, 49.72), (-1.85, 49.55),
    (-1.79, 49.37), (-1.60, 48.83), (-1.51, 48.63), (-1.83, 48.62), (-2.02, 48.65),
    (-2.31, 48.68), (-2.55, 48.60), (-2.73, 48.53), (-2.90, 48.68), (-3.05, 48.78),
    (-3.44, 48.82), (-3.70, 48.72), (-3.98, 48.72), (-4.30, 48.65), (-4.49, 48.39),
    (-4.77, 48.36), (-4.62, 48.20), (-4.74, 48.04), (-4.54, 48.02), (-4.37, 47.80),
    (-4.10, 47.79), (-3.92, 47.87), (-3.60, 47.75), (-3.37, 47.74), (-3.12, 47.48),
    (-2.94, 47.55), (-2.76, 47.65), (-2.45, 47.50), (-2.20, 47.27), (-2.10, 47.11),
    (-2.23, 46.99), (-1.85, 46.79), (-1.78, 46.50), (-1.42, 46.32), (-1.22, 46.20),
    (-1.16, 46.13),
    (-1.09, 45.99), (-1.05, 45.75), (-1.03, 45.62), (-1.06, 45.57), (-0.72, 45.40),
    (-1.16, 44.66), (-1.25, 44.42), (-1.31, 44.20), (-1.45, 43.64), (-1.60, 43.49),
    (-1.79, 43.39), (-1.42, 43.05), (-0.75, 42.95), (-0.30, 42.85), (0.15, 42.72),
    (0.66, 42.84), (1.00, 42.61), (1.43, 42.60), (1.72, 42.50), (2.16, 42.43),
    (2.65, 42.34), (3.04, 42.47), (3.13, 42.72), (3.03, 43.02), (3.30, 43.25),
    (3.55, 43.28), (3.70, 43.40), (4.13, 43.53), (4.42, 43.42), (4.85, 43.35),
    (5.05, 43.40), (5.35, 43.35), (5.36, 43.21), (5.60, 43.17), (5.90, 43.07),
    (6.02, 43.10), (6.37, 43.13),
    (6.65, 43.17), (6.74, 43.43), (7.12, 43.55), (7.28, 43.68), (7.44, 43.73),
    (7.53, 43.78),
    (7.53, 44.06), (6.95, 44.28), (7.00, 44.55), (6.75, 45.02), (7.05, 45.22),
    (6.80, 45.40), (6.85, 45.65), (6.90, 45.82), (6.96, 45.91), (6.87, 46.01),
    (6.80, 46.05), (6.30, 46.25),
    (6.14, 46.15), (6.10, 46.42), (6.45, 46.78), (6.70, 47.05), (7.00, 47.35),
    (7.59, 47.58), (7.62, 48.00), (7.80, 48.60), (8.10, 48.80), (8.23, 48.97),
    (7.98, 49.05), (7.45, 49.18), (6.90, 49.22), (6.36, 49.47), (5.98, 49.55),
    (5.79, 49.54), (5.45, 49.51), (4.86, 49.79), (4.83, 50.16), (4.23, 49.96),
    (3.66, 50.34), (3.45, 50.50), (3.27, 50.53), (3.24, 50.72), (3.13, 50.79),
    (2.90, 50.72), (2.60, 50.95), (2.38, 51.03),
]

CORSICA = [
    (9.35, 43.02), (9.45, 42.83), (9.53, 42.60), (9.55, 42.30), (9.40, 41.95),
    (9.40, 41.70), (9.28, 41.55), (9.22, 41.37), (8.90, 41.42), (8.75, 41.60),
    (8.60, 41.80), (8.55, 41.85), (8.70, 41.98), (8.65, 42.15), (8.55, 42.35), (8.70, 42.55),
    (8.75, 42.70), (9.00, 42.80), (9.20, 42.95), (9.35, 43.02),
]

FRANCE_SHAPES = [FRANCE_MAINLAND, CORSICA]

# Masses continentales voisines (contours très grossiers, usage physique only).
NEIGHBOURS = [
    # Angleterre / Pays de Galles (partie sud)
    [(-5.7, 50.05), (-3.5, 50.2), (-1.9, 50.6), (0.8, 50.75), (1.4, 51.4),
     (0.0, 52.0), (-2.0, 53.4), (-3.1, 53.4), (-4.8, 52.9), (-5.3, 51.7), (-5.7, 50.05)],
    # Irlande (est)
    [(-10.4, 51.5), (-6.0, 52.1), (-6.0, 54.5), (-8.0, 55.3), (-10.4, 53.5), (-10.4, 51.5)],
    # Belgique / Pays-Bas / Allemagne de l'ouest
    [(2.6, 51.1), (4.3, 51.5), (5.0, 53.4), (8.5, 54.0), (10.5, 54.0), (10.5, 47.5),
     (8.2, 48.9), (7.6, 48.0), (7.6, 47.5), (6.9, 49.2), (5.4, 49.5), (4.8, 50.2),
     (3.3, 50.6), (2.6, 51.1)],
    # Suisse / Italie du nord
    [(6.1, 46.2), (7.0, 45.9), (7.6, 44.1), (8.5, 44.2), (10.5, 43.8), (10.5, 47.5),
     (8.5, 47.6), (6.1, 46.4), (6.1, 46.2)],
    # Espagne / Péninsule ibérique (partie nord)
    [(-6.0, 43.6), (-1.8, 43.4), (-1.4, 43.0), (0.7, 42.7), (3.1, 42.4), (3.3, 41.0),
     (-6.0, 41.0), (-6.0, 43.6)],
    # Sardaigne (extrême nord)
    [(9.2, 41.25), (9.6, 41.15), (9.6, 41.0), (8.2, 41.0), (8.2, 41.15), (9.2, 41.25)],
]

# ---------------------------------------------------------------------------
# Villes de référence
# ---------------------------------------------------------------------------

CITIES = [
    {"name": "Paris", "lat": 48.857, "lon": 2.352, "pop": 2148000, "major": True},
    {"name": "Marseille", "lat": 43.297, "lon": 5.370, "pop": 870000, "major": True},
    {"name": "Lyon", "lat": 45.764, "lon": 4.836, "pop": 522000, "major": True},
    {"name": "Toulouse", "lat": 43.605, "lon": 1.444, "pop": 493000, "major": True},
    {"name": "Nice", "lat": 43.700, "lon": 7.265, "pop": 342000, "major": True},
    {"name": "Nantes", "lat": 47.218, "lon": -1.554, "pop": 314000, "major": True},
    {"name": "Montpellier", "lat": 43.611, "lon": 3.877, "pop": 295000, "major": True},
    {"name": "Strasbourg", "lat": 48.573, "lon": 7.752, "pop": 284000, "major": True},
    {"name": "Bordeaux", "lat": 44.838, "lon": -0.579, "pop": 257000, "major": True},
    {"name": "Lille", "lat": 50.633, "lon": 3.059, "pop": 233000, "major": True},
    {"name": "Rennes", "lat": 48.117, "lon": -1.678, "pop": 217000, "major": True},
    {"name": "Brest", "lat": 48.390, "lon": -4.486, "pop": 139000, "major": True},
    {"name": "Clermont-Ferrand", "lat": 45.777, "lon": 3.087, "pop": 147000, "major": True},
    {"name": "Ajaccio", "lat": 41.927, "lon": 8.737, "pop": 71000, "major": True},
    {"name": "Grenoble", "lat": 45.188, "lon": 5.724, "pop": 158000, "major": False},
    {"name": "Dijon", "lat": 47.322, "lon": 5.041, "pop": 158000, "major": False},
    {"name": "Nîmes", "lat": 43.837, "lon": 4.360, "pop": 148000, "major": False},
    {"name": "Le Havre", "lat": 49.494, "lon": 0.108, "pop": 170000, "major": False},
    {"name": "Toulon", "lat": 43.125, "lon": 5.930, "pop": 178000, "major": False},
    {"name": "Reims", "lat": 49.258, "lon": 4.032, "pop": 182000, "major": False},
    {"name": "Angers", "lat": 47.478, "lon": -0.563, "pop": 155000, "major": False},
    {"name": "Limoges", "lat": 45.833, "lon": 1.261, "pop": 130000, "major": False},
    {"name": "Tours", "lat": 47.394, "lon": 0.684, "pop": 137000, "major": False},
    {"name": "Amiens", "lat": 49.895, "lon": 2.302, "pop": 133000, "major": False},
    {"name": "Perpignan", "lat": 42.699, "lon": 2.895, "pop": 121000, "major": False},
    {"name": "Metz", "lat": 49.120, "lon": 6.176, "pop": 117000, "major": False},
    {"name": "Besançon", "lat": 47.238, "lon": 6.024, "pop": 118000, "major": False},
    {"name": "Caen", "lat": 49.183, "lon": -0.370, "pop": 106000, "major": False},
    {"name": "Orléans", "lat": 47.902, "lon": 1.909, "pop": 116000, "major": False},
    {"name": "Rouen", "lat": 49.443, "lon": 1.099, "pop": 112000, "major": False},
    {"name": "Nancy", "lat": 48.692, "lon": 6.184, "pop": 105000, "major": False},
    {"name": "Pau", "lat": 43.296, "lon": -0.370, "pop": 77000, "major": False},
    {"name": "La Rochelle", "lat": 46.160, "lon": -1.151, "pop": 77000, "major": False},
    {"name": "Biarritz", "lat": 43.483, "lon": -1.559, "pop": 25000, "major": False},
    {"name": "Annecy", "lat": 45.899, "lon": 6.129, "pop": 130000, "major": False},
    {"name": "Chamonix", "lat": 45.923, "lon": 6.869, "pop": 8600, "major": False},
    {"name": "Bastia", "lat": 42.701, "lon": 9.450, "pop": 48000, "major": False},
    {"name": "Cherbourg", "lat": 49.639, "lon": -1.616, "pop": 79000, "major": False},
    {"name": "Lorient", "lat": 47.748, "lon": -3.367, "pop": 57000, "major": False},
    {"name": "Avignon", "lat": 43.949, "lon": 4.806, "pop": 93000, "major": False},
    {"name": "Troyes", "lat": 48.297, "lon": 4.074, "pop": 61000, "major": False},
    {"name": "Le Mans", "lat": 48.006, "lon": 0.199, "pop": 145000, "major": False},
    {"name": "Poitiers", "lat": 46.580, "lon": 0.340, "pop": 90000, "major": False},
    {"name": "Mulhouse", "lat": 47.750, "lon": 7.340, "pop": 109000, "major": False},
    {"name": "Saint-Étienne", "lat": 45.440, "lon": 4.387, "pop": 174000, "major": False},
]

# ---------------------------------------------------------------------------
# Grille de restitution
# ---------------------------------------------------------------------------


def render_grid() -> tuple[np.ndarray, np.ndarray]:
    """Retourne (lats décroissantes, lons croissantes) de la grille France."""
    lats = np.arange(LAT_MAX, LAT_MIN - 1e-9, -RENDER_STEP)
    lons = np.arange(LON_MIN, LON_MAX + 1e-9, RENDER_STEP)
    return lats, lons


def _points_in_polygon(lon: np.ndarray, lat: np.ndarray, poly: list) -> np.ndarray:
    """Ray casting vectorisé : True à l'intérieur du polygone."""
    inside = np.zeros(lon.shape, dtype=bool)
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        cond = ((yi > lat) != (yj > lat)) & (
            lon < (xj - xi) * (lat - yi) / (yj - yi + 1e-12) + xi
        )
        inside ^= cond
        j = i
    return inside


def france_mask(lat2d: np.ndarray, lon2d: np.ndarray) -> np.ndarray:
    mask = np.zeros(lat2d.shape, dtype=bool)
    for shape in FRANCE_SHAPES:
        mask |= _points_in_polygon(lon2d, lat2d, shape)
    return mask


def land_mask(lat2d: np.ndarray, lon2d: np.ndarray) -> np.ndarray:
    mask = france_mask(lat2d, lon2d)
    for shape in NEIGHBOURS:
        mask |= _points_in_polygon(lon2d, lat2d, shape)
    return mask


_OROGRAPHY = [
    # (lat, lon, sigma_lat, sigma_lon, altitude_m)
    (45.60, 6.90, 0.85, 1.45, 2250.0),   # Alpes
    (44.90, 6.40, 0.55, 0.75, 1500.0),   # Alpes du Sud
    (42.80, 0.40, 0.32, 1.75, 1750.0),   # Pyrénées
    (45.25, 3.00, 1.05, 1.15, 950.0),    # Massif central
    (46.70, 6.00, 0.50, 0.65, 900.0),    # Jura
    (48.20, 7.00, 0.55, 0.35, 780.0),    # Vosges
    (47.20, 4.05, 0.40, 0.45, 420.0),    # Morvan
    (49.75, 5.10, 0.35, 0.80, 350.0),    # Ardennes
    (42.20, 9.05, 0.38, 0.28, 1450.0),   # Corse
    (44.10, 5.40, 0.45, 0.55, 800.0),    # Préalpes de Provence
    (48.55, 6.80, 0.30, 0.30, 400.0),    # Plateau lorrain
]


def orography(lat2d: np.ndarray, lon2d: np.ndarray) -> np.ndarray:
    """Relief synthétique lissé (mètres) utilisé par le moteur de simulation.

    Volontairement continu au trait de côte : appliquer le masque terre/mer
    introduirait une marche dont le gradient produirait un forçage
    orographique aberrant le long des littoraux.
    """
    z = np.zeros(lat2d.shape, dtype=np.float32)
    for la, lo, sla, slo, amp in _OROGRAPHY:
        z += amp * np.exp(
            -(((lat2d - la) / sla) ** 2 + ((lon2d - lo) / slo) ** 2)
        )
    return z


def shapes_geojson() -> dict:
    """Contours exportés vers le front-end."""
    return {
        "france": [[[lon, lat] for lon, lat in shape] for shape in FRANCE_SHAPES],
        "neighbours": [[[lon, lat] for lon, lat in shape] for shape in NEIGHBOURS],
        "bbox": {"lat_min": LAT_MIN, "lat_max": LAT_MAX, "lon_min": LON_MIN, "lon_max": LON_MAX},
    }


def cities_outside_france() -> list[str]:
    """Villes du catalogue qui ne tombent pas dans le contour national.

    Le classement et les étiquettes de la carte ne doivent comporter que des
    villes françaises : cette vérification garde le catalogue et le tracé
    cohérents entre eux.
    """
    lat = np.array([[c["lat"]] for c in CITIES], dtype=float)
    lon = np.array([[c["lon"]] for c in CITIES], dtype=float)
    inside = france_mask(lat, lon).ravel()
    return [city["name"] for city, ok in zip(CITIES, inside) if not ok]

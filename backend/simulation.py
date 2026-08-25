"""Moteur de simulation atmosphérique local.

Modèle barotrope simplifié : centres d'action mobiles, vent géostrophique dérivé
du champ de pression, advection thermique, forçage orographique et cycle diurne.
Il ne s'agit **pas** d'une prévision météorologique : ce moteur sert à valider la
chaîne de traitement et à faire vivre l'interface en l'absence du modèle Aurora.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta

import numpy as np

from .geo import land_mask, orography, render_grid

EARTH_R = 6_371_000.0
OMEGA = 7.292e-5
RHO = 1.2


def _bilinear_noise(shape: tuple[int, int], ny: int, nx: int, rng: np.random.Generator) -> np.ndarray:
    """Champ aléatoire lisse obtenu par interpolation bilinéaire d'une grille grossière."""
    coarse = rng.standard_normal((ny, nx))
    ys = np.linspace(0, ny - 1, shape[0])
    xs = np.linspace(0, nx - 1, shape[1])
    y0 = np.clip(np.floor(ys).astype(int), 0, ny - 2)
    x0 = np.clip(np.floor(xs).astype(int), 0, nx - 2)
    wy = (ys - y0)[:, None]
    wx = (xs - x0)[None, :]
    c00 = coarse[np.ix_(y0, x0)]
    c01 = coarse[np.ix_(y0, x0 + 1)]
    c10 = coarse[np.ix_(y0 + 1, x0)]
    c11 = coarse[np.ix_(y0 + 1, x0 + 1)]
    top = c00 * (1 - wx) + c01 * wx
    bot = c10 * (1 - wx) + c11 * wx
    return (top * (1 - wy) + bot * wy).astype(np.float32)


def _box_blur(a: np.ndarray, radius: int = 2, passes: int = 3) -> np.ndarray:
    """Flou moyenneur séparable, sans dépendance à SciPy."""
    out = a.astype(np.float32)
    width = 2 * radius + 1
    for _ in range(passes):
        pad = np.pad(out, ((radius, radius), (0, 0)), mode="edge")
        out = sum(pad[k : k + out.shape[0], :] for k in range(width)) / width
        pad = np.pad(out, ((0, 0), (radius, radius)), mode="edge")
        out = sum(pad[:, k : k + out.shape[1]] for k in range(width)) / width
    return out.astype(np.float32)


class SyntheticAtmosphere:
    """Atmosphère synthétique déterministe, reproductible pour une date donnée."""

    def __init__(self, base_time: datetime, seed: int | None = None, member: int = 0):
        self.base_time = base_time
        seed = seed if seed is not None else int(base_time.strftime("%Y%m%d%H"))
        self.rng = np.random.default_rng(seed + member * 7919)
        self.member = member

        self.lats, self.lons = render_grid()
        self.lat2d, self.lon2d = np.meshgrid(self.lats, self.lons, indexing="ij")
        # Fraction continentale lissée : un masque binaire créerait un saut
        # d'humidité et de température le long de tout le littoral.
        self.land = _box_blur(land_mask(self.lat2d, self.lon2d).astype(np.float32))
        self.orog = orography(self.lat2d, self.lon2d)

        self.lat_step = float(self.lats[1] - self.lats[0])
        self.lon_step = float(self.lons[1] - self.lons[0])
        self.dy = self.lat_step * 111_320.0
        self.dx = self.lon_step * 111_320.0 * np.cos(np.deg2rad(self.lat2d))
        self.f = 2 * OMEGA * np.sin(np.deg2rad(self.lat2d))

        doy = base_time.timetuple().tm_yday
        self.season = math.cos(2 * math.pi * (doy - 197) / 365.25)  # +1 mi-juillet

        self.systems = self._draw_systems()
        self.noise_t = _bilinear_noise(self.lat2d.shape, 9, 13, self.rng)
        self.noise_h = _bilinear_noise(self.lat2d.shape, 11, 15, self.rng)
        self.noise_c = _bilinear_noise(self.lat2d.shape, 13, 17, self.rng)

        # Gradient du relief pour le forçage orographique.
        self.dhdy = np.gradient(self.orog, axis=0) / self.dy
        self.dhdx = np.gradient(self.orog, axis=1) / self.dx

    # ------------------------------------------------------------------
    def _draw_systems(self) -> list[dict]:
        rng = self.rng
        systems: list[dict] = [
            # Centres d'action semi-permanents.
            {"lat": 36.5, "lon": -26.0, "amp": 15.0 + 4 * self.season, "rlat": 9.0,
             "rlon": 15.0, "u": 0.02, "v": 0.01, "wrap": False},
            {"lat": 62.0, "lon": -21.0, "amp": -17.0 + 5 * self.season, "rlat": 8.0,
             "rlon": 13.0, "u": 0.06, "v": -0.01, "wrap": False},
        ]
        for k in range(int(rng.integers(4, 7))):
            depression = rng.random() < 0.58
            # Une partie des systèmes est initialisée au large pour arriver
            # sur le domaine au fil de l'échéance.
            far_west = k >= 3
            lon0 = rng.uniform(-38.0, -24.0) if far_west else rng.uniform(-24.0, 14.0)
            amp = float(rng.uniform(5.0, 18.0)) * (-1.0 if depression else 1.0)
            systems.append(
                {
                    "lat": float(rng.uniform(39.0, 57.0)),
                    "lon": float(lon0),
                    "amp": amp,
                    "rlat": float(rng.uniform(5.0, 9.5)),
                    "rlon": float(rng.uniform(6.5, 13.5)),
                    "u": float(rng.uniform(0.30, 0.80) if far_west else rng.uniform(0.18, 0.62)),
                    "v": float(rng.uniform(-0.14, 0.10)),
                    "wrap": True,
                }
            )
        return systems

    # ------------------------------------------------------------------
    def _mslp(self, lead_h: float) -> np.ndarray:
        p = np.full(self.lat2d.shape, 1013.2, dtype=np.float32)
        p += 0.9 * np.float32(self.season)
        for s in self.systems:
            lat_c = s["lat"] + s["v"] * lead_h
            lon_c = s["lon"] + s["u"] * lead_h
            if s.get("wrap"):
                # Les systèmes mobiles sont recyclés : succession continue de
                # perturbations atlantiques quelle que soit l'échéance.
                lon_c = ((lon_c + 55.0) % 110.0) - 55.0
                lat_c = s["lat"] + 6.0 * math.sin(lead_h / 130.0 + s["lon"])
            # Léger creusement puis comblement des systèmes mobiles.
            life = 1.0 - 0.30 * abs(math.sin(lead_h / 130.0 + s["rlat"]))
            p += (s["amp"] * life) * np.exp(
                -(
                    ((self.lat2d - lat_c) / s["rlat"]) ** 2
                    + ((self.lon2d - lon_c) / s["rlon"]) ** 2
                )
            )
        return p

    def _wind(self, mslp: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        p_pa = mslp * 100.0
        dpdy = np.gradient(p_pa, axis=0) / self.dy
        dpdx = np.gradient(p_pa, axis=1) / self.dx
        ug = -dpdy / (RHO * self.f)
        vg = dpdx / (RHO * self.f)

        # Friction : rotation vers les basses pressions + atténuation.
        alpha = np.deg2rad(14.0 + 18.0 * self.land)
        k = 0.86 - 0.30 * self.land - np.clip(self.orog / 9000.0, 0, 0.18)
        ca, sa = np.cos(alpha), np.sin(alpha)
        u = k * (ug * ca - vg * sa)
        v = k * (ug * sa + vg * ca)

        speed = np.hypot(u, v)
        cap = 38.0
        scale = np.where(speed > cap, cap / np.maximum(speed, 1e-6), 1.0)
        return u * scale, v * scale

    def _convergence(self, u: np.ndarray, v: np.ndarray) -> np.ndarray:
        dudx = np.gradient(u, axis=1) / self.dx
        dvdy = np.gradient(v, axis=0) / self.dy
        return -(dudx + dvdy) * 1e4  # ordre de grandeur ±3

    # ------------------------------------------------------------------
    def fields(self, lead_h: float) -> dict[str, np.ndarray]:
        valid = self.base_time + timedelta(hours=lead_h)
        mslp = self._mslp(lead_h)
        u10, v10 = self._wind(mslp)
        speed = np.hypot(u10, v10)
        conv = self._convergence(u10, v10)
        # Forçage orographique : composante du vent le long de la pente (≈ m/s
        # de vitesse verticale), ramenée à la même échelle que la convergence.
        uplift = np.clip((u10 * self.dhdx + v10 * self.dhdy) * 20.0, -6.0, 9.0)

        drift = np.float32(math.sin(lead_h / 37.0) * 0.6)

        # --- Température ------------------------------------------------
        t_sea_level = (
            14.6
            - 0.60 * (self.lat2d - 46.5)
            + 9.8 * self.season
            + 1.6 * self.noise_t
            + 0.9 * drift
        )
        advection = np.clip(0.42 * v10 + 0.10 * u10, -9.0, 9.0)
        lapse = -6.3 * (self.orog / 1000.0)

        lst = (valid.hour + valid.minute / 60.0) + self.lon2d / 15.0
        diurnal_amp = (5.6 + 3.4 * (0.5 + 0.5 * self.season)) * self.land + 0.7
        cloud_pre = np.clip(48 + 17 * conv - 1.15 * (mslp - 1013.0), 0, 100)
        diurnal = diurnal_amp * np.cos(2 * math.pi * (lst - 14.6) / 24.0) * (
            1.0 - 0.55 * cloud_pre / 100.0
        )
        sea_damp = (1.0 - self.land) * (-0.35 * (t_sea_level - (11.0 + 7.5 * self.season)))

        t2m = t_sea_level + advection + lapse + diurnal + sea_damp

        # --- Humidité, nuages, précipitations ---------------------------
        rh = (
            63.0
            + 13.0 * (1.0 - self.land)
            + 8.5 * conv
            - 0.34 * (mslp - 1013.0)
            + 1.8 * uplift
            + 7.5 * self.noise_h
            - 0.35 * np.clip(diurnal, -6, 12)
        )
        rh = np.clip(rh, 18.0, 100.0)

        tcc = (
            42.0
            + 17.0 * conv
            - 1.05 * (mslp - 1013.0)
            + 3.0 * uplift
            + 13.0 * self.noise_c
            + 0.45 * (rh - 65.0)
        )
        tcc = np.clip(tcc, 0.0, 100.0)

        forcing = np.clip(0.45 * conv + 0.35 * uplift, -1.5, 8.0)
        wet = np.clip((rh - 75.0) / 8.0, 0.0, None)
        convective = (
            np.clip(self.season, 0, 1)
            * self.land
            * np.clip(np.cos(2 * math.pi * (lst - 16.5) / 24.0), 0, 1)
            * np.clip(t2m - 24.0, 0, 10)
            * 0.05
            * np.clip(self.noise_c, 0, None)
        )
        precip = np.clip(wet * (0.35 + forcing) + convective, 0.0, 22.0)
        precip = np.where(precip < 0.06, 0.0, precip) ** 0.92

        # --- Rafales ----------------------------------------------------
        turb = 1.36 + 0.14 * np.clip(conv, 0, 3) + 0.18 * self.land + np.clip(self.orog / 6000.0, 0, 0.35)
        gust = np.minimum(speed * turb + 0.4 * np.clip(precip, 0, 6), 62.0)

        # --- Altitude ---------------------------------------------------
        t850 = t_sea_level - 13.6 + 0.62 * advection + 0.8 * self.noise_t
        z500 = (
            552.0
            + 0.30 * (mslp - 1013.0)
            + 1.25 * (t850 - float(np.mean(t850)))
            + 3.2 * self.season
        )

        return {
            "2t": t2m.astype(np.float32),
            "u10": u10.astype(np.float32),
            "v10": v10.astype(np.float32),
            "wind10": (speed * 3.6).astype(np.float32),
            "gust": (gust * 3.6).astype(np.float32),
            "msl": mslp.astype(np.float32),
            "precip": precip.astype(np.float32),
            "tcc": tcc.astype(np.float32),
            "rh": rh.astype(np.float32),
            "t850": t850.astype(np.float32),
            "z500": z500.astype(np.float32),
        }

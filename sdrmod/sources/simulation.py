"""Source simulée : quelques émetteurs fictifs, pour tester sans matériel.

Les émetteurs sont placés autour de la fréquence centrale au moment de
l'ouverture, puis restent à fréquence fixe : on peut donc s'accorder dessus.
"""

from __future__ import annotations

import time

import numpy as np

from .base import Source


class SourceSimulation(Source):
    nom = "Simulation"
    temps_reel = True

    # (décalage par rapport au centre initial en Hz, type, amplitude)
    EMETTEURS = [
        (+200_000, "nfm", 0.05),    # = VFO par défaut : tonalité 1 kHz
        (-250_000, "am", 0.05),     # tonalité 600 Hz
        (+412_500, "usb", 0.03),    # deux tonalités 700 / 1900 Hz au-dessus de la porteuse
        (-500_000, "cw", 0.04),     # porteuse manipulée
        (+700_000, "wfm", 0.05),    # 1 kHz + 3 kHz, excursion 75 kHz
    ]

    def ouvrir(self, taux: int, frequence: float) -> None:
        self.taux = taux
        self.centre = frequence
        self.emetteurs = [(frequence + d, t, a) for d, t, a in self.EMETTEURS]
        self._phases = [0.0] * len(self.emetteurs)
        self._n = 0
        self._gain = 1.0
        self._t_suivant = time.monotonic()
        self._rng = np.random.default_rng()

    def lire(self, n: int) -> np.ndarray:
        if self.temps_reel:
            self._t_suivant += n / self.taux
            attente = self._t_suivant - time.monotonic()
            if attente > 0:
                time.sleep(attente)
            else:
                self._t_suivant = time.monotonic()

        fs = self.taux
        t = (self._n + np.arange(n)) / fs
        k = np.arange(n)
        x = (self._rng.standard_normal(n) + 1j * self._rng.standard_normal(n)) * 0.002

        for i, (f_abs, type_, amp) in enumerate(self.emetteurs):
            rel = f_abs - self.centre
            if abs(rel) > fs / 2 * 0.95:
                continue
            w = 2 * np.pi * rel / fs
            porteuse = self._phases[i] + w * k
            self._phases[i] = (self._phases[i] + w * n) % (2 * np.pi)

            if type_ == "nfm":
                ph = porteuse + (3000 / 1000) * np.sin(2 * np.pi * 1000 * t)
                x += amp * np.exp(1j * ph)
            elif type_ == "wfm":
                ph = porteuse + (50_000 / 1000) * np.sin(2 * np.pi * 1000 * t) \
                    + (25_000 / 3000) * np.sin(2 * np.pi * 3000 * t)
                x += amp * np.exp(1j * ph)
            elif type_ == "am":
                env = 1 + 0.7 * np.sin(2 * np.pi * 600 * t)
                x += amp * env * np.exp(1j * porteuse)
            elif type_ == "usb":
                x += amp * (np.exp(1j * (porteuse + 2 * np.pi * 700 * t))
                            + np.exp(1j * (porteuse + 2 * np.pi * 1900 * t)))
            elif type_ == "cw":
                manip = (np.floor(t / 0.12) % 3 != 2).astype(np.float64)
                x += amp * manip * np.exp(1j * porteuse)

        self._n += n
        return (x * self._gain).astype(np.complex64)

    def regler_frequence(self, frequence: float) -> None:
        self.centre = frequence

    def regler_gain(self, gain: float | None) -> None:
        self._gain = 1.0 if gain is None else 10 ** ((gain - 30) / 20)

    def gains(self) -> list[float]:
        return [float(g) for g in range(0, 50, 5)]

    def infos(self) -> dict:
        return {"fabricant": "SDR Modulaire", "modele": "Source simulée", "tuner": "aucun",
                "plages": [(1e6, 6_000e6)], "gains": self.gains(), "taux_max": 2_880_000,
                "bias_t": False, "echantillonnage_direct": False,
                "remarque": "5 émetteurs fictifs placés autour de la fréquence de départ."}

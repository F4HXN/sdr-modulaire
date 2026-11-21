"""Sortie audio (sounddevice/PortAudio) avec tampon circulaire et maîtrise de la latence."""

from __future__ import annotations

import logging
import threading

import numpy as np

log = logging.getLogger(__name__)


class Anneau:
    def __init__(self, capacite: int) -> None:
        self.buf = np.zeros(capacite, dtype=np.float32)
        self.cap = capacite
        self.r = 0
        self.n = 0

    def ecrire(self, x: np.ndarray) -> None:
        x = x[-self.cap:]
        exces = self.n + len(x) - self.cap
        if exces > 0:
            self.jeter(exces)
        w = (self.r + self.n) % self.cap
        k = min(len(x), self.cap - w)
        self.buf[w:w + k] = x[:k]
        self.buf[: len(x) - k] = x[k:]
        self.n += len(x)

    def lire(self, m: int) -> np.ndarray:
        out = np.zeros(m, dtype=np.float32)
        k = min(m, self.n)
        premier = min(k, self.cap - self.r)
        out[:premier] = self.buf[self.r:self.r + premier]
        out[premier:k] = self.buf[: k - premier]
        self.r = (self.r + k) % self.cap
        self.n -= k
        return out

    def jeter(self, m: int) -> None:
        m = min(m, self.n)
        self.r = (self.r + m) % self.cap
        self.n -= m


class SortieAudio:
    LATENCE_CIBLE = 0.15   # s
    LATENCE_MAX = 0.5      # s : au-delà on rattrape (dérive d'horloge SDR / carte son)

    def __init__(self, taux: int = 48_000) -> None:
        self.taux = taux
        self.anneau = Anneau(taux * 2)
        self.verrou = threading.Lock()
        self.amorce = True
        self.flux = None
        self.erreur = ""
        try:
            import sounddevice as sd
            self.flux = sd.OutputStream(samplerate=taux, channels=1, dtype="float32",
                                        blocksize=1024, callback=self._rappel)
        except Exception as exc:
            self.erreur = f"Audio indisponible : {exc}"
            log.warning(self.erreur)

    @property
    def disponible(self) -> bool:
        return self.flux is not None

    def demarrer(self) -> None:
        if self.flux is not None and not self.flux.active:
            self.flux.start()

    def arreter(self) -> None:
        if self.flux is not None and self.flux.active:
            self.flux.stop()
        with self.verrou:
            self.anneau.jeter(self.anneau.n)
            self.amorce = True

    def ecrire(self, x: np.ndarray) -> None:
        with self.verrou:
            self.anneau.ecrire(x.astype(np.float32, copy=False))
            if self.anneau.n > self.LATENCE_MAX * self.taux:
                self.anneau.jeter(self.anneau.n - int(self.LATENCE_CIBLE * self.taux))

    def _rappel(self, sortie, trames, _temps, _statut) -> None:
        with self.verrou:
            if self.amorce and self.anneau.n < self.LATENCE_CIBLE * self.taux:
                sortie[:, 0] = 0
                return
            self.amorce = False
            if self.anneau.n < trames:
                self.amorce = True       # sous-alimentation : on réamorce
            sortie[:, 0] = self.anneau.lire(trames)

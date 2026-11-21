"""Briques DSP réutilisables, toutes à état (continuité entre blocs)."""

from __future__ import annotations

import math

import numpy as np
from scipy import signal


def plan_decimation(taux: int, taux_audio: int) -> tuple[int, int]:
    """Découpe la décimation totale en deux étages.

    Étage 1 : taux -> taux intermédiaire (~200-300 kHz, suffisant pour la WFM).
    Étage 2 : intermédiaire -> audio.
    """
    rapport = taux / taux_audio
    total = int(round(rapport))
    if abs(rapport - total) > 1e-9 or total < 2:
        raise ValueError(f"Taux {taux} non multiple entier de {taux_audio}")
    for d2 in (5, 4, 6, 3, 8, 2):
        if total % d2 == 0:
            d1 = total // d2
            if taux / d1 >= 150_000:
                return d1, d2
    return total, 1


def taille_bloc(taux: int, decimation_totale: int, duree: float = 0.05) -> int:
    """Taille de bloc multiple de la décimation (continuité) et de 256 (librtlsdr)."""
    base = decimation_totale * 256 // math.gcd(decimation_totale, 256)
    return base * max(1, round(taux * duree / base))


def passe_bas(fs: float, coupure: float, n_coefs: int) -> np.ndarray:
    return signal.firwin(n_coefs, coupure, fs=fs, window=("kaiser", 7.0))


def passe_bande_complexe(fs: float, bas: float, haut: float, n_coefs: int) -> np.ndarray:
    """Filtre complexe qui ne laisse passer que [bas, haut] (fréquences signées)."""
    centre = (bas + haut) / 2
    demi = (haut - bas) / 2
    n = np.arange(n_coefs) - (n_coefs - 1) / 2
    return (passe_bas(fs, demi, n_coefs) * np.exp(2j * np.pi * centre * n / fs)).astype(np.complex64)


class NCO:
    """Oscillateur local à phase continue : décale le spectre de -freq."""

    def __init__(self, fs: float) -> None:
        self.fs = fs
        self.freq = 0.0
        self._phase = 0.0
        self._n = np.zeros(0)

    def melanger(self, x: np.ndarray) -> np.ndarray:
        n = len(x)
        if len(self._n) != n:
            self._n = np.arange(n, dtype=np.float64)
        w = -2.0 * np.pi * self.freq / self.fs
        lo = np.exp(1j * (self._phase + w * self._n)).astype(np.complex64)
        self._phase = (self._phase + w * n) % (2.0 * np.pi)
        return x * lo


class FIR:
    """Filtre FIR à état, par convolution rapide (overlap-add)."""

    def __init__(self, coefs: np.ndarray) -> None:
        self.coefs = np.asarray(coefs)
        self._etat: np.ndarray | None = None

    def traiter(self, x: np.ndarray) -> np.ndarray:
        if self._etat is None:
            dtype = np.result_type(x.dtype, self.coefs.dtype)
            self._etat = np.zeros(len(self.coefs) - 1, dtype=dtype)
        tampon = np.concatenate((self._etat, x))
        y = signal.oaconvolve(tampon, self.coefs, mode="valid")
        self._etat = tampon[len(tampon) - (len(self.coefs) - 1):]
        return y


class IIR:
    """Filtre IIR (sections du second ordre) à état."""

    def __init__(self, sos: np.ndarray) -> None:
        self.sos = sos
        self._zi: np.ndarray | None = None

    def traiter(self, x: np.ndarray) -> np.ndarray:
        if self._zi is None:
            dtype = np.complex128 if np.iscomplexobj(x) else np.float64
            self._zi = np.zeros((self.sos.shape[0], 2), dtype=dtype)
        y, self._zi = signal.sosfilt(self.sos, x, zi=self._zi)
        return y


class Decimateur:
    """Filtre anti-repliement elliptique + décimation entière."""

    def __init__(self, facteur: int, fs: float, coupure: float) -> None:
        self.facteur = facteur
        self.filtre = IIR(signal.ellip(8, 0.1, 80, coupure, fs=fs, output="sos")) if facteur > 1 else None

    def traiter(self, x: np.ndarray) -> np.ndarray:
        if self.filtre is None:
            return x
        y = self.filtre.traiter(x)[:: self.facteur]
        return y.astype(np.complex64 if np.iscomplexobj(y) else np.float32)


class AGC:
    """AGC audio simple : attaque immédiate, relâchement lent, rampe sans « zip »."""

    def __init__(self, cible: float = 0.3, relachement: float = 0.05, gain_max: float = 1e4) -> None:
        self.cible = cible
        self.relachement = relachement
        self.gain_max = gain_max
        self.gain = 1.0

    def traiter(self, x: np.ndarray) -> np.ndarray:
        crete = float(np.max(np.abs(x))) if len(x) else 0.0
        voulu = min(self.cible / (crete + 1e-9), self.gain_max)
        if voulu < self.gain:
            nouveau = voulu
        else:
            nouveau = self.gain + (voulu - self.gain) * self.relachement
        rampe = np.linspace(self.gain, nouveau, len(x), dtype=np.float32)
        self.gain = nouveau
        return x * rampe


def puissance_dbfs(x: np.ndarray) -> float:
    if len(x) == 0:
        return -150.0
    p = float(np.mean(np.abs(x) ** 2))
    return 10.0 * math.log10(p + 1e-20)

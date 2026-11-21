import numpy as np
from scipy import signal

from ..dsp import FIR, IIR, passe_bas
from .base import Demodulateur


def _discriminateur(y: np.ndarray, precedent: complex) -> tuple[np.ndarray, complex]:
    z = np.concatenate(([precedent], y))
    return np.angle(z[1:] * np.conj(z[:-1])), y[-1]


class NFM(Demodulateur):
    nom = "NFM"
    ordre = 20
    largeur_defaut = 12_500.0
    largeur_min = 5_000.0
    largeur_max = 25_000.0
    excursion = 5_000.0

    def construire(self) -> None:
        self.filtre = FIR(passe_bas(self.fs, self.largeur / 2, 255))
        self.passe_bas_audio = IIR(signal.butter(4, 3_500, fs=self.fs, output="sos"))
        self._prec = 0j

    def traiter(self, x: np.ndarray) -> np.ndarray:
        y = self.filtre.traiter(x)
        self._mesurer(y)
        d, self._prec = _discriminateur(y, self._prec)
        audio = d * (self.fs / (2 * np.pi * self.excursion))
        return (self.passe_bas_audio.traiter(audio) * 0.5).astype(np.float32)


class WFM(Demodulateur):
    """FM large bande mono (radiodiffusion), désaccentuation 50 µs (Europe)."""

    nom = "WFM"
    ordre = 30
    domaine = "intermediaire"
    largeur_defaut = 180_000.0
    largeur_min = 180_000.0
    largeur_max = 180_000.0
    excursion = 75_000.0
    tau = 50e-6

    def construire(self) -> None:
        # le filtre de canal est assuré par l'étage de décimation 1 (~0,42 × fs)
        a = np.exp(-1.0 / (self.fs * self.tau))
        self._desaccent_b = np.array([1 - a])
        self._desaccent_a = np.array([1, -a])
        self._desaccent_zi = np.zeros(1)
        self.passe_bas_audio = IIR(signal.butter(6, 15_000, fs=self.fs, output="sos"))
        self._prec = 0j

    def traiter(self, x: np.ndarray) -> np.ndarray:
        self._mesurer(x)
        d, self._prec = _discriminateur(x, self._prec)
        audio = d * (self.fs / (2 * np.pi * self.excursion))
        audio, self._desaccent_zi = signal.lfilter(self._desaccent_b, self._desaccent_a, audio,
                                                   zi=self._desaccent_zi)
        audio = self.passe_bas_audio.traiter(audio)[:: self.decim_sortie]
        return (audio * 0.7).astype(np.float32)

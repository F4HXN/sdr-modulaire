import numpy as np
from scipy import signal

from ..dsp import FIR, IIR, passe_bas
from .base import Demodulateur


class AM(Demodulateur):
    nom = "AM"
    ordre = 10
    largeur_defaut = 8_000.0
    largeur_min = 2_000.0
    largeur_max = 20_000.0
    gain_manuel = 10.0

    def construire(self) -> None:
        self.filtre = FIR(passe_bas(self.fs, self.largeur / 2, 255))
        self.anti_continu = IIR(signal.butter(1, 40, "highpass", fs=self.fs, output="sos"))

    def traiter(self, x: np.ndarray) -> np.ndarray:
        y = self.filtre.traiter(x)
        self._mesurer(y)
        return self.anti_continu.traiter(np.abs(y)).astype(np.float32)

"""BLU (USB/LSB) et CW par filtre passe-bande complexe + partie réelle."""

import numpy as np

from ..dsp import FIR, passe_bande_complexe
from .base import Demodulateur


class BLU(Demodulateur):
    abstrait = True
    largeur_defaut = 2_700.0
    largeur_min = 1_000.0
    largeur_max = 4_000.0
    coupure_basse = 200.0
    signe = +1
    n_coefs = 511
    gain_manuel = 20.0

    def _limites(self) -> tuple[float, float]:
        bas, haut = self.coupure_basse, self.coupure_basse + self.largeur
        return (bas, haut) if self.signe > 0 else (-haut, -bas)

    def construire(self) -> None:
        bas, haut = self._limites()
        self.filtre = FIR(passe_bande_complexe(self.fs, bas, haut, self.n_coefs))

    def bande(self) -> tuple[float, float]:
        bas, haut = self._limites()
        return (bas + self.decalage_nco, haut + self.decalage_nco)

    def traiter(self, x: np.ndarray) -> np.ndarray:
        y = self.filtre.traiter(x)
        self._mesurer(y)
        return y.real.astype(np.float32)


class USB(BLU):
    nom = "USB"
    ordre = 40


class LSB(BLU):
    nom = "LSB"
    ordre = 50
    signe = -1


class CW(BLU):
    """Un signal pile sur la fréquence affichée donne une note de `tonalite` Hz."""

    nom = "CW"
    ordre = 60
    tonalite = 700.0
    decalage_nco = -700.0
    largeur_defaut = 500.0
    largeur_min = 100.0
    largeur_max = 1_200.0
    pas_largeur = 50.0
    n_coefs = 1023

    def _limites(self) -> tuple[float, float]:
        return (self.tonalite - self.largeur / 2, self.tonalite + self.largeur / 2)

"""Classe de base des démodulateurs.

Pour ajouter un mode : créer un fichier dans ce dossier avec une sous-classe
de Demodulateur ayant un attribut `nom`. Il apparaît tout seul dans la liste.
"""

from __future__ import annotations

import numpy as np

from ..dsp import puissance_dbfs


class Demodulateur:
    nom = ""
    ordre = 100                  # position dans la liste des modes
    abstrait = False

    # "audio" : reçoit l'IQ déjà ramené à 48 kHz
    # "intermediaire" : reçoit l'IQ à ~240 kHz et décime lui-même jusqu'à l'audio (WFM)
    domaine = "audio"

    largeur_defaut = 10_000.0
    largeur_min = 1_000.0
    largeur_max = 20_000.0
    pas_largeur = 100.0

    decalage_nco = 0.0           # décalage ajouté à l'oscillateur local (CW : BFO)
    gain_manuel = 1.0            # gain appliqué quand l'AGC est coupée

    def __init__(self) -> None:
        self.largeur = self.largeur_defaut
        self.niveau_dbfs = -150.0
        self.fs = 0.0
        self.taux_audio = 0
        self.decim_sortie = 1

    def preparer(self, fs_entree: float, taux_audio: int, decim_sortie: int) -> None:
        """fs_entree : taux de ce que recevra traiter() ; decim_sortie : facteur
        restant jusqu'à l'audio (utile seulement pour le domaine intermédiaire)."""
        self.fs = fs_entree
        self.taux_audio = taux_audio
        self.decim_sortie = decim_sortie
        self.construire()

    def regler_largeur(self, largeur: float) -> None:
        self.largeur = float(min(max(largeur, self.largeur_min), self.largeur_max))
        if self.fs:
            self.construire()

    def construire(self) -> None:
        """(Re)calcule les filtres. Appelé après preparer() et à chaque changement de largeur."""

    def bande(self) -> tuple[float, float]:
        """Bande passante affichée, relative à la fréquence du VFO (Hz)."""
        return (-self.largeur / 2, self.largeur / 2)

    def traiter(self, x: np.ndarray) -> np.ndarray:
        raise NotImplementedError

    def _mesurer(self, y: np.ndarray) -> None:
        self.niveau_dbfs = puissance_dbfs(y)

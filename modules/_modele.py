"""Modèle de module SDR Modulaire : copier ce fichier sous un nouveau nom (sans « _ » devant).

Les fichiers dont le nom commence par « _ » ne sont pas chargés : celui-ci ne sert que de point de départ.
Guide complet : MODULES.md à la racine du dépôt.
"""

from collections import deque

import numpy as np
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from sdrmod.modules import Module


class MonModule(Module):
    nom = "Mon module"                                  # affiché dans le menu Modules (unique)
    description = "Ce que fait le module, en une phrase."

    def demarrer(self) -> None:
        """Appelé à l'activation : préparer l'état, puis s'abonner aux sujets utiles."""
        self.dernier_niveau = -120.0
        self.a_afficher: deque = deque()                  # du thread DSP vers l'interface
        self.abonner("niveau", self._sur_niveau)          # désabonnement automatique au retrait
        # self.abonner("audio", self._sur_audio)          # float32, 48 kHz
        # self.abonner("iq", self._sur_iq)                # complex64, toute la bande reçue
        # self.abonner("vfo", self._sur_vfo)              # fréquence (Hz), mode

    # ---- Rappels du bus : thread DSP, ~20 fois par seconde. Rapides, jamais de widget. ----
    def _sur_niveau(self, dbfs: float) -> None:
        self.dernier_niveau = dbfs

    def _sur_audio(self, audio: np.ndarray, taux: int) -> None:
        pass

    def _sur_iq(self, iq: np.ndarray, centre: float, taux: int) -> None:
        pass

    def _sur_vfo(self, frequence: float, mode: str) -> None:
        pass

    # ---- Interface (facultatif) : un panneau ajouté à droite de la fenêtre. ----
    def creer_widget(self) -> QWidget | None:
        w = QWidget()
        lay = QVBoxLayout(w)
        self.lbl = QLabel("—")
        lay.addWidget(self.lbl)
        lay.addStretch(1)
        self.minuterie = QTimer(w, interval=200, timeout=self._rafraichir)
        self.minuterie.start()
        return w

    def _rafraichir(self) -> None:                        # thread de l'interface : widgets permis
        self.lbl.setText(f"{self.dernier_niveau:.1f} dBFS à {self.ctx.frequence() / 1e6:.4f} MHz")

    def arreter(self) -> None:
        """Appelé au retrait : arrêter minuteries, fermer fichiers et connexions."""
        if hasattr(self, "minuterie"):
            self.minuterie.stop()

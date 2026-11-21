"""Journal d'activité : note l'heure, la fréquence et le niveau chaque fois qu'un signal apparaît.

Module d'exemple du tutoriel (https://rtl-sdr.fr/sdr-modulaire-modules.html) :
- abonnement à deux sujets du bus (« niveau » et « vfo ») ;
- rappels du bus rapides, sans toucher aux widgets (thread DSP) ;
- passage des données vers l'interface par une file lue par un QTimer ;
- fichier de données dans le dossier propre au module.
"""

import time
from collections import deque

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QLabel, QListWidget, QVBoxLayout, QWidget

from sdrmod.modules import Module

SEUIL = -60.0       # dBFS : au-dessus, on considère qu'un signal est présent
HYSTERESIS = 3.0    # dB : évite de noter dix fois un signal qui oscille autour du seuil


class JournalActivite(Module):
    nom = "Journal d'activité"
    description = "Note l'heure, la fréquence et le niveau chaque fois qu'un signal dépasse le seuil."

    def demarrer(self) -> None:
        self.present = False
        self.frequence = self.ctx.frequence()
        self.a_noter: deque = deque()          # rempli par le thread DSP, vidé par l'interface
        self.fichier = self.ctx.dossier_donnees("journal_activite") / "activite.csv"
        self.abonner("niveau", self._sur_niveau)
        self.abonner("vfo", self._sur_vfo)

    # ---- rappels du bus : rapides, aucun widget ----
    def _sur_vfo(self, frequence: float, mode: str) -> None:
        self.frequence = frequence

    def _sur_niveau(self, dbfs: float) -> None:
        if not self.present and dbfs > SEUIL:
            self.present = True
            self.a_noter.append((time.strftime("%Y-%m-%d %H:%M:%S"), self.frequence, dbfs))
        elif self.present and dbfs < SEUIL - HYSTERESIS:
            self.present = False

    # ---- interface (thread GUI) ----
    def creer_widget(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(QLabel(f"Signaux au-dessus de {SEUIL:.0f} dBFS :"))
        self.liste = QListWidget()
        lay.addWidget(self.liste, 1)
        self.minuterie = QTimer(w, interval=200, timeout=self._rafraichir)
        self.minuterie.start()
        return w

    def _rafraichir(self) -> None:
        while self.a_noter:
            quand, f, dbfs = self.a_noter.popleft()
            self.liste.insertItem(0, f"{quand[11:]}  {f / 1e6:10.4f} MHz  {dbfs:6.1f} dBFS")
            with open(self.fichier, "a", encoding="utf-8") as fic:
                fic.write(f"{quand};{f:.0f};{dbfs:.1f}\n")

    def arreter(self) -> None:
        if hasattr(self, "minuterie"):
            self.minuterie.stop()

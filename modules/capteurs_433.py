# SPDX-License-Identifier: MIT
"""Capteurs à 433,92 MHz pour SDR Modulaire (application Python/Qt).

Dépendance externe : rtl_433 doit être installé et accessible dans le PATH.
Le décodeur reçoit les échantillons IQ de SDR Modulaire sur son entrée standard.
"""

import json
import os
import shutil
import time
from collections import deque

from PySide6.QtCore import QProcess, QTimer, Qt
from PySide6.QtWidgets import QLabel, QListWidget, QListWidgetItem, QPushButton, QVBoxLayout, QWidget

from sdrmod.modules import Module

FREQUENCE = 433_920_000


class Capteurs433(Module):
    nom = "Capteurs 433 MHz"
    description = "Affiche les trames de capteurs compatibles avec rtl_433 à 433,92 MHz."

    def demarrer(self):
        self.blocs = deque(maxlen=8)
        self.dernier_iq = 0
        self.bande = None
        self.erreur = ""
        self.abonner("iq", self._sur_iq)

    def _sur_iq(self, iq, centre, taux):
        # Le moteur produit un nouveau tableau par bloc. Le thread DSP ne fait aucune E/S.
        self.blocs.append((iq, round(centre), int(taux)))

    def creer_widget(self):
        panneau = QWidget()
        disposition = QVBoxLayout(panneau)
        accord = QPushButton("Accorder sur 433,92 MHz")
        accord.clicked.connect(lambda: self.ctx.regler_frequence(FREQUENCE))
        disposition.addWidget(accord)
        self.statut = QLabel()
        self.statut.setWordWrap(True)
        self.statut.setTextFormat(Qt.TextFormat.PlainText)
        disposition.addWidget(self.statut)
        self.liste = QListWidget()
        self.liste.setWordWrap(True)
        disposition.addWidget(self.liste)

        self.executable = shutil.which("rtl_433")
        self.decodeur = QProcess(panneau)
        self.decodeur.setStandardErrorFile(QProcess.nullDevice())
        self.decodeur.readyReadStandardOutput.connect(self._lire_trames)
        self.decodeur.errorOccurred.connect(self._erreur_decodeur)
        self.decodeur.finished.connect(self._fin_decodeur)
        self.minuterie = QTimer(panneau, interval=50, timeout=self._alimenter)
        self.minuterie.start()
        self._alimenter()
        return panneau

    def _alimenter(self):
        if not self.executable:
            self.statut.setText("Installez rtl_433 et ajoutez-le au PATH.")
            return

        if not self.blocs:
            if time.monotonic() - self.dernier_iq > 1:
                self._couper_decodeur()
                self.bande = None
                self.statut.setText("En attente des échantillons du récepteur.")
            return

        self.dernier_iq = time.monotonic()
        for _ in range(8):
            if not self.blocs:
                break
            iq, centre, taux = self.blocs.popleft()
            bande = (centre, taux)
            if bande != self.bande:
                self._couper_decodeur()
                self.bande = bande
                self.erreur = ""
                if abs(FREQUENCE - centre) < 0.45 * taux:
                    self.decodeur.start(self.executable, [
                        "-c", os.devnull, "-r", "cf32:-", "-s", str(taux),
                        "-f", str(centre), "-C", "si", "-F", "json",
                    ])

            if abs(FREQUENCE - centre) >= 0.45 * taux:
                self.statut.setText("Accordez le récepteur sur 433,92 MHz.")
                continue
            if self.erreur:
                self.statut.setText(self.erreur)
                continue
            if self.decodeur.state() != QProcess.ProcessState.NotRunning:
                self.statut.setText("Écoute des capteurs compatibles…")
                # Limite la mémoire si le décodeur ne suit pas le débit du récepteur.
                if self.decodeur.bytesToWrite() < 2 * iq.nbytes:
                    self.decodeur.write(iq.astype("<c8", copy=False).tobytes())

    def _lire_trames(self):
        while self.decodeur.canReadLine():
            try:
                trame = json.loads(bytes(self.decodeur.readLine()))
            except (ValueError, UnicodeError):
                continue
            if not isinstance(trame, dict) or "model" not in trame:
                continue
            valeurs = " · ".join(
                f"{cle} : {valeur}" for cle, valeur in trame.items()
                if cle not in {"time", "model"}
            )
            entree = QListWidgetItem(f"{time.strftime('%H:%M:%S')} · {trame['model']}\n{valeurs}")
            self.liste.insertItem(0, entree)
            if self.liste.count() > 100:
                self.liste.takeItem(100)

    def _erreur_decodeur(self, _erreur):
        self.erreur = f"Erreur de rtl_433 : {self.decodeur.errorString()}."
        self.statut.setText(self.erreur)

    def _fin_decodeur(self, code, _statut):
        self.erreur = f"rtl_433 s’est arrêté (code {code}). Réactivez le module."
        self.statut.setText(self.erreur)

    def _couper_decodeur(self):
        if self.decodeur.state() != QProcess.ProcessState.NotRunning:
            self.decodeur.kill()
            self.decodeur.waitForFinished(500)

    def arreter(self):
        if hasattr(self, "minuterie"):
            self.minuterie.stop()
            self._couper_decodeur()
        self.blocs.clear()

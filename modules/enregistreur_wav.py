"""Enregistreur audio : écrit l'audio démodulé dans un fichier WAV (mono 48 kHz, 16 bits)."""

import threading
import time
import wave
from pathlib import Path

import numpy as np
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QLabel, QPushButton, QVBoxLayout, QWidget

from sdrmod.modules import Module

DOSSIER = Path.home() / "sdr-enregistrements"


class EnregistreurWav(Module):
    nom = "Enregistreur audio"
    description = f"Enregistre l'audio démodulé en WAV dans {DOSSIER}."

    def demarrer(self) -> None:
        self.fichier = None
        self.chemin = None
        self.debut = 0.0
        self.verrou = threading.Lock()
        self.abonner("audio", self._sur_audio)

    def _sur_audio(self, audio: np.ndarray, taux: int) -> None:
        with self.verrou:
            if self.fichier is not None:
                self.fichier.writeframes((audio * 32767).astype("<i2").tobytes())

    def creer_widget(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        self.bouton = QPushButton("● Enregistrer")
        self.bouton.setCheckable(True)
        self.bouton.toggled.connect(self._basculer)
        self.lbl = QLabel("Prêt")
        self.lbl.setWordWrap(True)
        lay.addWidget(self.bouton)
        lay.addWidget(self.lbl)
        lay.addStretch(1)
        self.minuterie = QTimer(w, interval=500, timeout=self._rafraichir)
        self.minuterie.start()
        return w

    def _basculer(self, actif: bool) -> None:
        if actif:
            DOSSIER.mkdir(parents=True, exist_ok=True)
            nom = time.strftime("%Y%m%d-%H%M%S")
            f_mhz = self.ctx.frequence() / 1e6
            self.chemin = DOSSIER / f"{nom}_{f_mhz:.4f}MHz_{self.ctx.mode()}.wav"
            fichier = wave.open(str(self.chemin), "wb")
            fichier.setnchannels(1)
            fichier.setsampwidth(2)
            fichier.setframerate(48_000)
            with self.verrou:
                self.fichier = fichier
            self.debut = time.time()
            self.bouton.setText("■ Arrêter")
        else:
            self._fermer()
            self.bouton.setText("● Enregistrer")

    def _rafraichir(self) -> None:
        if self.fichier is not None:
            self.lbl.setText(f"Enregistrement : {time.time() - self.debut:.0f} s\n{self.chemin.name}")
        elif self.chemin:
            self.lbl.setText(f"Dernier fichier :\n{self.chemin}")

    def _fermer(self) -> None:
        with self.verrou:
            fichier, self.fichier = self.fichier, None
        if fichier is not None:
            fichier.close()

    def arreter(self) -> None:
        self._fermer()
        if hasattr(self, "minuterie"):
            self.minuterie.stop()

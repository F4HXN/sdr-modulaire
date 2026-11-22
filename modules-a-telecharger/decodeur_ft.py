"""Réception FT8 et FT4 en USB avec le moteur jt9 de WSJT-X."""

from __future__ import annotations

import math
import re
import shutil
import subprocess
import time
import wave
from pathlib import Path
from queue import Empty, Full, Queue
from tempfile import TemporaryDirectory
from threading import Event, Thread

import numpy as np
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QComboBox, QDialog, QHBoxLayout, QLabel, QPlainTextEdit,
                               QPushButton, QSizePolicy, QVBoxLayout, QWidget)
from scipy.signal import resample_poly

from sdrmod.modules import Module

MODES = {"FT8": (15, "-8"), "FT4": (7.5, "-5")}
TAUX_JT9 = 12_000
MESSAGE = re.compile(r"^\d{6}\s+(-?\d+)\s+([+-]?[\d.]+)\s+(\d+)\s+[~+]\s+(.+)$")


def decoder(executable, dossier: Path, signal, taux: int, utc: float, mode: str) -> list[str]:
    """Convertit un créneau en WAV 12 kHz et extrait les lignes décodées par jt9."""
    duree, option = MODES[mode]
    heure = time.strftime("%H:%M:%S", time.gmtime(utc))
    fichier = dossier / time.strftime("%y%m%d_%H%M%S.wav", time.gmtime(utc))
    diviseur = math.gcd(taux, TAUX_JT9)
    audio = resample_poly(signal, TAUX_JT9 // diviseur, taux // diviseur)
    pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2")
    with wave.open(str(fichier), "wb") as wav:
        wav.setparams((1, 2, TAUX_JT9, 0, "NONE", "not compressed"))
        wav.writeframes(pcm.tobytes())
    try:
        resultat = subprocess.run(
            [executable, option, "-d", "3", "-p", str(int(duree)),
             "-a", str(dossier), "-t", str(dossier), str(fichier)],
            cwd=dossier, capture_output=True, text=True, errors="replace", timeout=2 * duree,
            check=True,
        )
    finally:
        fichier.unlink(missing_ok=True)
    lignes = set()
    for ligne in resultat.stdout.splitlines():
        correspondance = MESSAGE.match(ligne.strip())
        if correspondance:
            snr, dt, hz, texte = correspondance.groups()
            lignes.add((int(hz), int(snr), float(dt), texte.strip()))
    return [f"{heure}  {snr:+3d}  {dt:+4.1f}  {hz:4d} Hz  {texte}"
            for hz, snr, dt, texte in sorted(lignes)]


class DecodeurFT(Module):
    nom = "Décodeur FT8 / FT4"
    description = "Décode les messages FT8 et FT4 reçus en USB avec jt9 (WSJT-X). Horloge UTC synchronisée requise."

    def demarrer(self) -> None:
        self.executable = shutil.which("jt9")
        self.reglage = ("FT8", self.ctx.frequence(), self.ctx.mode(), 0)
        self.tampon = None
        self.fin_audio = None
        self.flux = None
        self.dernier_audio = 0
        self.bilan = None
        self.travaux = Queue(maxsize=1)
        self.resultats = Queue(maxsize=4)
        self.fin = Event()
        if self.executable:
            self.abonner("vfo", self._vfo)
            self.abonner("audio", self._audio)
            Thread(target=self._travail, name="FT8/FT4", daemon=True).start()

    def _vfo(self, frequence: float, mode: str) -> None:
        if self.reglage[1:3] != (frequence, mode):
            self.reglage = (self.reglage[0], frequence, mode, self.reglage[3] + 1)

    def _audio(self, audio, taux: int) -> None:
        self._recevoir(audio, taux, time.time_ns())

    def _recevoir(self, audio, taux: int, maintenant: int) -> None:
        reglage = self.reglage
        mode, _, radio, _ = reglage
        if radio != "USB" or not len(audio):
            return
        taille = round(taux * MODES[mode][0])
        arrivee = maintenant * taux // 1_000_000_000 - len(audio)
        flux = (reglage, taux)
        if self.flux != flux or self.fin_audio is None or abs(arrivee - self.fin_audio) > taux / 2:
            self.flux, self.fin_audio, self.tampon = flux, arrivee, None
        debut = self.fin_audio
        self.fin_audio = debut + len(audio)
        self.dernier_audio = maintenant
        i = 0
        while i < len(audio):
            instant = debut + i
            position = instant % taille
            n = min(len(audio) - i, taille - position)
            if position == 0:
                self.tampon = np.empty(taille, dtype=np.float32)
            if self.tampon is not None:
                self.tampon[position:position + n] = audio[i:i + n]
                if position + n == taille:
                    utc = (instant - position) / taux
                    try:
                        self.travaux.put_nowait((reglage, self.tampon, taux, utc))
                    except Full:
                        pass
                    self.tampon = None
            i += n

    def _travail(self) -> None:
        with TemporaryDirectory(prefix="sdrmod-ft-") as dossier:
            while not self.fin.is_set():
                try:
                    reglage, signal, taux, utc = self.travaux.get(timeout=0.2)
                except Empty:
                    continue
                if reglage != self.reglage:
                    continue
                try:
                    lignes = decoder(self.executable, Path(dossier), signal, taux, utc, reglage[0])
                    heure = time.strftime("%H:%M:%S", time.gmtime(utc))
                    bilan = f"{heure} UTC · {len(lignes)} message(s)"
                except (OSError, subprocess.SubprocessError) as erreur:
                    lignes, bilan = [], f"Erreur jt9 : {erreur}"
                try:
                    self.resultats.put_nowait((reglage, lignes, bilan))
                except Full:
                    pass

    def creer_widget(self) -> QWidget:
        panneau = QWidget()
        panneau.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        self.disposition = QVBoxLayout(panneau)
        self.disposition.setContentsMargins(4, 4, 4, 4)
        self.contenu = QWidget()
        self.disposition.addWidget(self.contenu)
        layout = QVBoxLayout(self.contenu)
        layout.setContentsMargins(0, 0, 0, 0)
        ligne = QHBoxLayout()
        choix = QComboBox()
        choix.addItems(list(MODES))
        choix.setAccessibleName("Mode numérique")
        choix.currentTextChanged.connect(self._choisir)
        usb = QPushButton("Mode USB")
        usb.clicked.connect(lambda: self.ctx.regler_mode("USB"))
        effacer = QPushButton("Effacer")
        effacer.clicked.connect(lambda: self.zone.clear())
        self.ouvrir = QPushButton("Agrandir")
        self.ouvrir.clicked.connect(self.agrandir)
        self.reduire = QPushButton("Réduire")
        self.reduire.hide()
        for bouton in (choix, usb, effacer, self.ouvrir, self.reduire):
            ligne.addWidget(bouton)
        layout.addLayout(ligne)
        self.zone = QPlainTextEdit()
        self.zone.setFont(QFont("DejaVu Sans Mono", 10))
        self.zone.setReadOnly(True)
        self.zone.setMaximumBlockCount(200)
        self.zone.setPlaceholderText("UTC · dB · Δt · fréquence audio · message")
        self.hauteur_fenetre = self.zone.maximumHeight()
        self.hauteur_apercu = 4 * self.zone.fontMetrics().lineSpacing() + 12
        self.zone.setMaximumHeight(self.hauteur_apercu)
        layout.addWidget(self.zone, 1)
        self.etat = QLabel()
        self.etat.setWordWrap(True)
        layout.addWidget(self.etat)
        self.dialogue = QDialog(panneau, Qt.WindowType.Window)
        self.dialogue.setWindowTitle("Décodeur FT8 / FT4 — SDR Modulaire")
        self.dialogue.resize(850, 420)
        self.disposition_dialogue = QVBoxLayout(self.dialogue)
        self.dialogue.finished.connect(self._ranger)
        self.reduire.clicked.connect(self.dialogue.close)
        self.minuterie = QTimer(panneau, interval=250, timeout=self._rafraichir)
        self.minuterie.start()
        self._rafraichir()
        return panneau

    def _choisir(self, mode: str) -> None:
        self.reglage = (mode, *self.reglage[1:3], self.reglage[3] + 1)
        self.zone.clear()

    def agrandir(self) -> None:
        self.disposition.removeWidget(self.contenu)
        self.disposition_dialogue.addWidget(self.contenu)
        self.zone.setMaximumHeight(self.hauteur_fenetre)
        self.zone.setFont(QFont("DejaVu Sans Mono", 14))
        self.ouvrir.hide()
        self.reduire.show()
        self.dialogue.show()
        self.contenu.show()
        self.reduire.setFocus()

    def _ranger(self) -> None:
        self.disposition_dialogue.removeWidget(self.contenu)
        self.disposition.addWidget(self.contenu)
        self.contenu.show()
        self.zone.setMaximumHeight(self.hauteur_apercu)
        self.zone.setFont(QFont("DejaVu Sans Mono", 10))
        self.reduire.hide()
        self.ouvrir.show()

    def _rafraichir(self) -> None:
        while True:
            try:
                reglage, lignes, bilan = self.resultats.get_nowait()
            except Empty:
                break
            if reglage == self.reglage:
                self.bilan = (reglage, bilan)
                if lignes:
                    self.zone.appendPlainText("\n".join(lignes))
        if not self.executable:
            statut = "jt9 introuvable. Installez WSJT-X."
        elif self.reglage[2] != "USB":
            statut = "Sélectionnez USB et accordez-vous sur une fréquence FT8 ou FT4."
        elif time.time_ns() - self.dernier_audio > 1_000_000_000:
            statut = "En attente du signal audio."
        elif self.bilan and self.bilan[0] == self.reglage:
            statut = self.bilan[1]
        else:
            statut = "En attente d’un créneau UTC complet. Vérifiez l’horloge système."
        if self.etat.text() != statut:
            self.etat.setText(statut)

    def arreter(self) -> None:
        self.fin.set()
        if hasattr(self, "minuterie"):
            self.minuterie.stop()
            self.dialogue.close()

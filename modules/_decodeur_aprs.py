# SPDX-License-Identifier: MIT
"""Réception APRS AFSK 1200 dans SDR Modulaire (application Python/Qt sous Linux).

Dépendance externe : Dire Wolf, accessible dans le PATH.
Le module démodule son propre canal à partir des échantillons IQ du récepteur.
"""

import re
import shutil
import time
import wave
from collections import deque
from queue import Empty, Full, Queue
from threading import Event, Thread

import numpy as np
from scipy import signal
from PySide6.QtCore import QProcess, QTemporaryFile, QTimer, Qt
from PySide6.QtWidgets import QLabel, QListWidget, QPushButton, QVBoxLayout, QWidget

from sdrmod.demods.fm import NFM
from sdrmod.dsp import Decimateur, IIR, NCO, plan_decimation
from sdrmod.modules import Module

FREQUENCE = 144_800_000
TAUX = 48_000
TRAME = re.compile(r"^\[0(?:\.\d+)?\]\s+([A-Z0-9-]+>[^:\s]+:.*)$")


class DecodeurAPRS(Module):
    nom = "Décodeur APRS"
    description = "Décode et affiche les trames AX.25 en AFSK 1200 avec Dire Wolf."

    def demarrer(self):
        self.blocs = Queue(maxsize=8)
        self.audio = deque(maxlen=8)
        self.fin = Event()
        self.reception = (0, False)
        self.capture = bytearray()
        self.capture_iq = bytearray()
        self.fin_capture = 0
        self.abonner("iq", self._sur_iq)

    def _sur_iq(self, iq, centre, taux):
        if not hasattr(self, "traitement") or self.fin.is_set():
            return
        try:
            self.blocs.put_nowait((iq, centre, taux))
        except Full:
            pass

    def _demoduler(self):
        bande = None
        while not self.fin.is_set():
            try:
                iq, centre, taux = self.blocs.get(timeout=0.1)
            except Empty:
                continue
            couvert = abs(FREQUENCE - centre) + 7_500 < 0.42 * taux
            self.reception = (time.monotonic(), couvert)
            if not couvert:
                bande = None
                self.audio.clear()
                continue
            if bande != (centre, taux):
                bande = (centre, taux)
                self.audio.clear()
                d1, d2 = plan_decimation(taux, TAUX)
                inter = taux / d1
                nco = NCO(taux)
                nco.freq = FREQUENCE - centre
                dec1 = Decimateur(d1, taux, 0.42 * inter)
                dec2 = Decimateur(d2, inter, 0.42 * TAUX)
                demod = NFM()
                demod.largeur = 15_000
                demod.preparer(TAUX, TAUX, 1)
                passe_haut = IIR(signal.butter(2, 300, fs=TAUX, btype="highpass", output="sos"))

            canal = dec2.traiter(dec1.traiter(nco.melanger(iq)))
            audio = passe_haut.traiter(demod.traiter(canal))
            pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2")
            self.audio.append((pcm.tobytes(), canal))

    def creer_widget(self):
        panneau = QWidget()
        disposition = QVBoxLayout(panneau)
        accord = QPushButton("Accorder sur 144,800 MHz en NFM")
        accord.clicked.connect(self._accorder)
        disposition.addWidget(accord)
        self.statut = QLabel()
        self.statut.setWordWrap(True)
        self.statut.setTextFormat(Qt.TextFormat.PlainText)
        disposition.addWidget(self.statut)
        self.liste = QListWidget()
        self.liste.setWordWrap(True)
        disposition.addWidget(self.liste)

        executable = shutil.which("direwolf")
        if not executable:
            self.statut.setText("Installez Dire Wolf et ajoutez-le au PATH.")
            return panneau

        self.config = QTemporaryFile(panneau)
        if not self.config.open():
            self.statut.setText("Impossible de créer la configuration temporaire de Dire Wolf.")
            return panneau
        contenu = b"ADEVICE stdin null\nAGWPORT 0\nKISSPORT 0\n"
        ecrit = self.config.write(contenu) == len(contenu) and self.config.flush()
        self.config.close()
        if not ecrit:
            self.statut.setText("Impossible d’enregistrer la configuration temporaire de Dire Wolf.")
            return panneau

        self.decodeur = QProcess(panneau)
        self.decodeur.setStandardErrorFile(QProcess.nullDevice())
        self.decodeur.readyReadStandardOutput.connect(self._lire_trames)
        self.decodeur.errorOccurred.connect(self._erreur_decodeur)
        self.decodeur.finished.connect(self._fin_decodeur)
        self.minuterie = QTimer(panneau, interval=50, timeout=self._alimenter)
        self.minuterie.start()
        self.decodeur.start(executable, [
            "-c", self.config.fileName(), "-t", "0", "-q", "hd",
            "-r", str(TAUX), "-n", "1", "-b", "16", "-B", "1200", "-",
        ])
        self.traitement = Thread(target=self._demoduler, name="sdr-aprs", daemon=True)
        self.traitement.start()
        capture = QPushButton("Capturer le canal APRS pendant 15 s")
        capture.clicked.connect(self._lancer_capture)
        disposition.insertWidget(2, capture)
        self.statut_capture = QLabel()
        self.statut_capture.setWordWrap(True)
        self.statut_capture.setTextFormat(Qt.TextFormat.PlainText)
        disposition.insertWidget(3, self.statut_capture)
        return panneau

    def _accorder(self):
        self.ctx.regler_mode("NFM")
        self.ctx.regler_frequence(FREQUENCE)

    def _lancer_capture(self):
        self.capture.clear()
        self.capture_iq.clear()
        self.fin_capture = time.monotonic() + 15
        self.statut_capture.setText("Capture en cours. Envoyez une trame maintenant.")

    def _enregistrer_capture(self):
        self.fin_capture = 0
        if not self.capture:
            self.statut_capture.setText("Capture vide. Vérifiez que le récepteur est en marche et accordé.")
            return
        try:
            repertoire = self.ctx.dossier_donnees("decodeur_aprs")
            chemin = repertoire / time.strftime("capture-%Y%m%d-%H%M%S.wav")
            with wave.open(str(chemin), "wb") as fichier:
                fichier.setparams((1, 2, TAUX, 0, "NONE", "not compressed"))
                fichier.writeframes(self.capture)
            chemin.with_suffix(".cf32").write_bytes(self.capture_iq)
        except OSError as erreur:
            self.statut_capture.setText(f"Impossible d’enregistrer la capture ({erreur}).")
        else:
            self.statut_capture.setText(f"Capture enregistrée : {chemin.name}")
        finally:
            self.capture.clear()
            self.capture_iq.clear()

    def _alimenter(self):
        for _ in range(8):
            if self.decodeur.bytesToWrite() >= TAUX // 2:
                break
            try:
                pcm, canal = self.audio.popleft()
            except IndexError:
                break
            self.decodeur.write(pcm)
            if self.fin_capture:
                self.capture.extend(pcm)
                self.capture_iq.extend(canal.astype("<c8", copy=False).tobytes())
        if self.fin_capture and time.monotonic() >= self.fin_capture:
            self._enregistrer_capture()

        instant, couvert = self.reception
        if time.monotonic() - instant > 1:
            self.statut.setText("En attente des échantillons du récepteur.")
        elif not couvert:
            self.statut.setText("144,800 MHz hors de la bande reçue. Cliquez sur Accorder.")
        else:
            self.statut.setText("Écoute de 144,800 MHz · AFSK 1200 bauds")

    def _lire_trames(self):
        while self.decodeur.canReadLine():
            ligne = bytes(self.decodeur.readLine()).decode("utf-8", errors="replace").rstrip()
            correspondance = TRAME.fullmatch(ligne)
            if correspondance:
                self.liste.insertItem(0, f"{time.strftime('%H:%M:%S')} · {correspondance[1]}")
                if self.liste.count() > 100:
                    self.liste.takeItem(100)

    def _erreur_decodeur(self, _erreur):
        if self.fin.is_set():
            return
        self.minuterie.stop()
        self.statut.setText(f"Erreur de Dire Wolf : {self.decodeur.errorString()}.")

    def _fin_decodeur(self, code, _statut):
        if self.minuterie.isActive():
            self.minuterie.stop()
            self.statut.setText(f"Dire Wolf s’est arrêté (code {code}). Réactivez le module.")

    def arreter(self):
        self.fin.set()
        if hasattr(self, "minuterie"):
            self.minuterie.stop()
            if self.decodeur.state() != QProcess.ProcessState.NotRunning:
                self.decodeur.kill()
                self.decodeur.waitForFinished(500)
        if hasattr(self, "config"):
            self.config.remove()
        if hasattr(self, "traitement"):
            self.traitement.join(timeout=0.5)

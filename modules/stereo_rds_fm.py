# SPDX-License-Identifier: MIT
"""Stéréo FM et RDS pour SDR Modulaire sous Linux.

Dépendances : numpy, scipy, PySide6 et sounddevice, déjà utilisées par l'application.
"""
import logging
from collections import deque
from queue import Empty, Full, Queue
from threading import Event, Lock, Thread
from time import monotonic

import numpy as np
from scipy import signal
from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (QCheckBox, QLabel, QPushButton, QSizePolicy, QSlider,
                               QToolButton, QVBoxLayout, QWidget)

from sdrmod.audio import Anneau
from sdrmod.dsp import Decimateur, IIR, NCO, plan_decimation
from sdrmod.modules import Module

TAUX_AUDIO = 48_000
OFFSETS = (0x0FC, 0x198, 0x168, 0x1B4)
G0 = ("áàéèíìóòúùÑÇŞβ¡Ĳâäêëîïôöûüñçşǧıĳ"
      "ªα©‰Ǧěňőπ€£$←↑→↓º¹²³±İńűµ¿÷°¼½¾§"
      "ÁÀÉÈÍÌÓÒÚÙŘČŠŽÐĿÂÄÊËÎÏÔÖÛÜřčšžđŀ"
      "ÃÅÆŒŷÝÕØÞŊŔĆŚŹŦðãåæœŵýõøþŋŕćśźŧ ")
SPECIAUX = {36: "¤", 94: "―", 96: "‖", 126: "¯"}
log = logging.getLogger(__name__)


def texte_rds(octets):
    texte = []
    for code in octets:
        if code == 13:
            break
        if code >= 128:
            texte.append(G0[code - 128])
        else:
            texte.append(SPECIAUX.get(code, chr(code) if 32 <= code < 127 else " "))
    return "".join(texte).strip()


def reste_rds(mot):
    for bit in range(25, 9, -1):
        if mot & (1 << bit):
            mot ^= 0x5B9 << (bit - 10)
    return mot


class SynchronisationRDS:
    def __init__(self, phase):
        self.phase = phase
        self.somme = self.precedent = 0j
        self.fenetre = self.compteur = 0
        self.blocs = []

    def ajouter(self, echantillon):
        self.somme += echantillon if self.phase < 0.5 else -echantillon
        self.phase += 1187.5 / TAUX_AUDIO
        if self.phase < 1:
            return None
        self.phase -= 1
        bit = (self.precedent.conjugate() * self.somme).real < 0
        self.precedent, self.somme = self.somme, 0j
        self.fenetre = ((self.fenetre << 1) | int(bit)) & 0x3FFFFFF
        self.compteur += 1
        if self.compteur < 26:
            return None

        position = len(self.blocs)
        reste = reste_rds(self.fenetre)
        if reste == OFFSETS[position] or (position == 2 and reste == 0x350):
            self.blocs.append(self.fenetre >> 10)
            self.compteur = 0
            if len(self.blocs) == 4:
                groupe, self.blocs = self.blocs, []
                return groupe
        else:
            self.blocs = []
            if reste == OFFSETS[0]:
                self.blocs.append(self.fenetre >> 10)
                self.compteur = 0
        return None


class TexteStation:
    def __init__(self):
        self.pi = None
        self.nom = self.message = ""
        self.ps = {}
        self.rt = {}
        self.version = None

    def lire(self, groupe):
        pi, b, c, d = groupe
        if pi != self.pi:
            self.__init__()
            self.pi = pi
        type_groupe = b >> 12
        if type_groupe == 0:
            self.ps[b & 3] = (d >> 8, d & 255)
            if len(self.ps) == 4:
                self.nom = texte_rds(o for i in range(4) for o in self.ps[i])
        if type_groupe == 2:
            version = b & 0x0810
            if self.version != version:
                self.version = version
                self.rt.clear()
                self.message = ""
            octets = (d >> 8, d & 255)
            if not b & 0x0800:
                octets = (c >> 8, c & 255) + octets
            self.rt[b & 15] = octets
            texte = []
            for i in range(16):
                if i not in self.rt:
                    break
                texte.extend(self.rt[i])
                if 13 in texte or i == 15:
                    self.message = texte_rds(texte)
                    break


class ReceptionFM:
    """Filtre un canal IQ, démodule le multiplex FM et extrait les deux pistes et le RDS."""

    def __init__(self, taux, decalage):
        d1, self.dec_audio = plan_decimation(taux, TAUX_AUDIO)
        self.taux = int(taux / d1)
        self.nco = NCO(taux)
        self.nco.freq = decalage
        self.decimation = Decimateur(d1, taux, min(100_000, 0.44 * self.taux))
        self.pilote = IIR(signal.butter(3, 400, fs=self.taux, output="sos"))
        self.audio = IIR(signal.butter(6, 15_000, fs=self.taux, output="sos"))
        self.rds = IIR(signal.butter(4, 2_600, fs=self.taux, output="sos"))
        self.phase = 0
        self.precedent = 0j
        self.accentuation = np.exp(-1 / (TAUX_AUDIO * 50e-6))
        self.zi = np.zeros((1, 2))
        self.synchros = [SynchronisationRDS(i / 8) for i in range(8)]
        self.station = TexteStation()
        self.instant_rds = 0

    def traiter(self, iq):
        canal = self.decimation.traiter(self.nco.melanger(iq))
        prec = np.concatenate(([self.precedent], canal[:-1]))
        self.precedent = canal[-1]
        mpx = np.angle(canal * np.conjugate(prec))
        phase = (self.phase + np.arange(len(mpx))) * (2 * np.pi * 19_000 / self.taux)
        self.phase = (self.phase + len(mpx)) % self.taux
        base = np.exp(1j * phase)
        pilote = self.pilote.traiter(2 * mpx * base)
        niveau = np.abs(pilote)
        phase_stereo = phase + np.pi / 2 - np.angle(pilote)
        melange = np.clip((niveau - 0.025) / .052, 0, 1)
        diff = 2 * mpx * np.sin(2 * phase_stereo) * melange
        stereo = self.audio.traiter(mpx + 1j * diff)[::self.dec_audio]
        canaux = np.column_stack((stereo.real + stereo.imag, stereo.real - stereo.imag))
        a = self.accentuation
        canaux, self.zi = signal.lfilter([1 - a], [1, -a], canaux, axis=0, zi=self.zi)

        rds = self.rds.traiter(mpx * base ** 3)[::self.dec_audio]
        for echantillon in rds:
            for synchro in self.synchros:
                groupe = synchro.ajouter(echantillon)
                if groupe is not None:
                    self.station.lire(groupe)
                    self.instant_rds = monotonic()
        return (0.6 * canaux).astype(np.float32), float(niveau.mean()) > .035


class SortieStereo:
    def __init__(self):
        self.anneaux = [Anneau(TAUX_AUDIO), Anneau(TAUX_AUDIO)]
        self.verrou = Lock()
        self.volume = .5
        self.flux = None
        self.amorce = True

    def ouvrir(self):
        import sounddevice as sd
        flux = sd.OutputStream(samplerate=TAUX_AUDIO, channels=2, dtype="float32",
                               blocksize=1024, callback=self._rappel)
        try:
            flux.start()
        except Exception:
            flux.close()
            raise
        self.flux = flux

    def fermer(self):
        if self.flux is not None:
            self.flux.close()
            self.flux = None
        self.vider()

    def vider(self):
        with self.verrou:
            for anneau in self.anneaux:
                anneau.jeter(anneau.n)
            self.amorce = True

    def ecrire(self, audio):
        with self.verrou:
            if self.flux is None:
                return
            for i, anneau in enumerate(self.anneaux):
                anneau.ecrire(audio[:, i])
                if anneau.n > TAUX_AUDIO * .4:
                    anneau.jeter(anneau.n - int(TAUX_AUDIO * .12))

    def _rappel(self, sortie, n, _temps, _statut):
        with self.verrou:
            if self.amorce and self.anneaux[0].n < TAUX_AUDIO * .12:
                sortie.fill(0)
                return
            self.amorce = self.anneaux[0].n < n
            for i, anneau in enumerate(self.anneaux):
                sortie[:, i] = np.clip(anneau.lire(n) * self.volume, -1, 1)


class TexteAdapte(QLabel):
    """Libellé à retour à la ligne qui réserve toute sa hauteur.

    Dans un dock, Qt ne tient pas compte de heightForWidth : un QLabel à plusieurs lignes
    reçoit la hauteur d'une seule et son texte est coupé. On impose donc la hauteur
    nécessaire à la largeur réelle, à chaque redimensionnement et à chaque nouveau texte.
    """

    def __init__(self, texte=""):
        super().__init__(texte)
        self.setWordWrap(True)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)

    def _ajuster(self):
        hauteur = self.heightForWidth(max(self.width(), 1))
        if hauteur > 0 and hauteur != self.minimumHeight():
            self.setMinimumHeight(hauteur)

    def setText(self, texte):
        if texte != self.text():
            super().setText(texte)
            self._ajuster()

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._ajuster()


class StereoRdsFM(Module):
    nom = "Stéréo et RDS en FM"
    description = "Réception FM stéréo, nom de station et radiotexte RDS en mode WFM."

    def demarrer(self):
        self.blocs = Queue(maxsize=8)
        self.fin = Event()
        self.accord = (self.ctx.frequence(), self.ctx.mode())
        self.sortie = SortieStereo()
        self.etat = None
        self.erreur = ""
        self.erreur_audio = ""
        self.abonner("vfo", self._sur_vfo)
        self.abonner("iq", self._sur_iq)
        self.traitement = Thread(target=self._recevoir, daemon=True)
        self.traitement.start()

    def _sur_vfo(self, frequence, mode):
        self.accord = (frequence, mode)

    def _sur_iq(self, iq, centre, taux):
        accord = self.accord
        if accord[1] != "WFM" or abs(accord[0] - centre) + 100_000 > taux * .48:
            return
        try:
            self.blocs.put_nowait((iq, centre, taux, accord))
        except Full:
            pass

    def _recevoir(self):
        bande = None
        try:
            while not self.fin.is_set():
                try:
                    iq, centre, taux, accord = self.blocs.get(timeout=.1)
                except Empty:
                    bande = None
                    continue
                if accord != self.accord:
                    continue
                config = (centre, taux, accord)
                if config != bande:
                    decodeur = ReceptionFM(taux, accord[0] - centre)
                    bande = config
                    self.sortie.vider()
                audio, stereo = decodeur.traiter(iq)
                if accord == self.accord:
                    station = decodeur.station
                    self.etat = (accord, monotonic(), stereo, station.pi, station.nom,
                                 station.message, decodeur.instant_rds)
                    self.sortie.ecrire(audio)
        except Exception as exc:
            log.exception("Erreur du décodeur FM")
            self.erreur = str(exc)

    def creer_widget(self):
        panneau = QWidget()
        panneau.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        disposition = QVBoxLayout(panneau)
        disposition.setContentsMargins(8, 6, 8, 6)
        disposition.setSpacing(4)
        disposition.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.bouton_wfm = QPushButton("Passer en WFM")
        self.bouton_wfm.clicked.connect(lambda: self.ctx.regler_mode("WFM"))
        disposition.addWidget(self.bouton_wfm)
        self.statut = TexteAdapte()
        self.station = TexteAdapte()
        self.message = TexteAdapte()
        for label in (self.statut, self.station, self.message):
            label.setTextFormat(Qt.TextFormat.PlainText)
            disposition.addWidget(label)
        options = QToolButton()
        options.setText("Réglages audio")
        options.setCheckable(True)
        options.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        options.setArrowType(Qt.ArrowType.RightArrow)
        disposition.addWidget(options)
        reglages = QWidget()
        formulaire = QVBoxLayout(reglages)
        formulaire.setContentsMargins(0, 0, 0, 0)
        formulaire.setSpacing(4)
        disposition.addWidget(reglages)
        reglages.hide()

        def afficher_reglages(ouvert):
            reglages.setVisible(ouvert)
            options.setArrowType(Qt.ArrowType.DownArrow if ouvert else Qt.ArrowType.RightArrow)

        options.toggled.connect(afficher_reglages)
        self.ecoute = QCheckBox("Écouter sur la sortie stéréo")
        self.ecoute.toggled.connect(self._ecouter)
        formulaire.addWidget(self.ecoute)
        aide = TexteAdapte("Pour écouter, mettez le volume principal à zéro et réglez le volume stéréo ici.")
        formulaire.addWidget(aide)
        volume = QSlider(Qt.Orientation.Horizontal)
        volume.setAccessibleName("Volume stéréo")
        volume.setRange(0, 100)
        volume.setValue(50)
        volume.valueChanged.connect(lambda v: setattr(self.sortie, "volume", v / 100))
        formulaire.addWidget(volume)
        self.minuterie = QTimer(panneau, interval=200, timeout=self._rafraichir)
        self.minuterie.start()
        self._rafraichir()
        return panneau

    def _ecouter(self, active):
        self.erreur_audio = ""
        if not active:
            self.sortie.fermer()
            return
        try:
            self.sortie.ouvrir()
        except Exception as exc:
            self.ecoute.setChecked(False)
            self.erreur_audio = f"Sortie stéréo indisponible : {exc}"
        self.ecoute.setToolTip(self.erreur_audio)

    def _rafraichir(self):
        self.bouton_wfm.setVisible(self.accord[1] != "WFM")
        if self.erreur:
            self.statut.setText(f"Erreur du décodeur : {self.erreur}")
            return
        etat = self.etat
        if not etat or etat[0] != self.accord:
            self.statut.setText("Accordez une station en mode WFM.")
            self.station.clear()
            self.station.hide()
            self.message.clear()
            self.message.hide()
            return
        _, instant, stereo, pi, nom, message, rds = etat
        if monotonic() - instant > 1:
            self.statut.setText("En attente de la réception FM.")
        else:
            reception = "RDS reçu" if monotonic() - rds < 5 else "En attente du RDS"
            self.statut.setText(f"{'● Stéréo' if stereo else '○ Mono'} · {reception}")
        self.station.setText(nom or (f"PI {pi:04X}" if pi is not None else ""))
        self.message.setText(message)
        self.station.setVisible(bool(self.station.text()))
        self.message.setVisible(bool(message))
        if self.erreur_audio:
            self.statut.setText(self.erreur_audio)

    def arreter(self):
        if hasattr(self, "minuterie"):
            self.minuterie.stop()
        self.fin.set()
        self.traitement.join(timeout=1)
        self.sortie.fermer()

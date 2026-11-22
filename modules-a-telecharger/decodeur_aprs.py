# SPDX-License-Identifier: MIT
"""Décodeur APRS (AFSK 1200 bauds, AX.25) pour SDR Modulaire, sans logiciel externe.

Le module extrait son propre canal (144,800 MHz par défaut) des IQ de la clé : il décode même
quand le VFO écoute autre chose dans la bande reçue. Chaîne : NCO → décimation à 48 kHz →
discriminateur FM → 24 kHz → démodulateur AFSK → trames AX.25 affichées au format
« SOURCE>DEST,RELAIS*:texte ».

Démodulateur AFSK : corrélateurs complexes sur 1 200 et 2 200 Hz (un bit de long), plusieurs
décisions |marque| − g·|espace| en parallèle (g de 0,5 à 2 compense la pente entre les deux
tonalités : préaccentuation, désaccentuation, filtres de l'émetteur), passe-bas de la décision,
instants des changements de tonalité interpolés, nombre de bits par palier, NRZI, HDLC, CRC.
Sur les 100 trames bruitées de « gen_packets -n 100 » (Dire Wolf) : 72 trames, comme Dire Wolf
(70) ; avec désaccentuation ou préaccentuation : 69 et 68 (Dire Wolf : 71).
Même algorithme dans SDR Web (web/aprs.js).
"""

from __future__ import annotations

import math
import queue
import threading
import time

import numpy as np
from PySide6.QtCore import QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (QCheckBox, QComboBox, QHBoxLayout, QLabel, QListWidget, QPushButton,
                               QSizePolicy, QVBoxLayout, QWidget)
from scipy import signal

from sdrmod.dsp import Decimateur, NCO, plan_decimation
from sdrmod.modules import Module

# ======================================================================= démodulateur AFSK
TAUX_AFSK = 24_000
DEBIT, MARQUE, ESPACE = 1200, 1200.0, 2200.0
PENTES = tuple(2 ** (k / 4) for k in range(-4, 5))   # 0,5 à 2 : pente entre les deux tonalités
LISSAGE = 900.0      # Hz : passe-bas de Butterworth d'ordre 2 sur la décision (moins de passages parasites)


def passe_bas_2(fc: float, fs: float) -> tuple[float, float, float, float, float]:
    """Butterworth d'ordre 2 (transformation bilinéaire) : b0, b1, b2, a1, a2. Même calcul dans aprs.js."""
    k = math.tan(math.pi * fc / fs)
    q = 1 / (1 + math.sqrt(2) * k + k * k)
    b0 = k * k * q
    return b0, 2 * b0, b0, 2 * (k * k - 1) * q, (1 - math.sqrt(2) * k + k * k) * q


def crc_ax25(octets: bytes) -> int:
    c = 0xFFFF
    for o in octets:
        c ^= o
        for _ in range(8):
            c = (c >> 1) ^ 0x8408 if c & 1 else c >> 1
    return c ^ 0xFFFF


class _Tranche:
    """Une pente g : passages par zéro → bits → trames HDLC."""

    def __init__(self, g: float, spb: float, sos: np.ndarray) -> None:
        self.g, self.spb, self.sos = g, spb, sos
        self.zi = np.zeros((1, 2))
        self.prec = 0.0            # dernière valeur de la décision (bloc précédent)
        self.dernier = 0.0         # instant (échantillons, absolu) du dernier changement de tonalité
        self.reg = 0               # 8 derniers bits (recherche des fanions)
        self.uns = 0
        self.bits: list[int] | None = None   # bits de la trame en cours (None : hors trame)

    def traiter(self, d: np.ndarray, t0: int, sortie: list) -> None:
        d, self.zi = signal.sosfilt(self.sos, d, zi=self.zi)
        x = np.concatenate(([self.prec], d))
        signe = np.signbit(x)
        for i in np.flatnonzero(signe[1:] != signe[:-1]):          # changement entre x[i] et x[i+1]
            a, b = x[i], x[i + 1]
            t = t0 + i - 1 + (a / (a - b) if a != b else 0.5)       # instant interpolé du passage par zéro
            n = int(round((t - self.dernier) / self.spb))
            self.dernier = t
            if n < 1:
                continue
            # NRZI : un changement = 0, puis n − 1 bits à 1 (au-delà de 7 : hors trame de toute façon)
            self._bit(0, sortie, t)
            for _ in range(min(n - 1, 7)):
                self._bit(1, sortie, t)
        if len(d):
            self.prec = float(d[-1])

    def _bit(self, b: int, sortie: list, t: float) -> None:
        self.reg = ((self.reg << 1) | b) & 0xFF
        if self.reg == 0x7E:                                        # fanion 01111110
            if self.bits is not None and len(self.bits) >= 7 + 18 * 8:
                bits = self.bits[:-7]                               # retire le début du fanion
                if len(bits) % 8 == 0:
                    o = bytes(sum(bits[i + k] << k for k in range(8)) for i in range(0, len(bits), 8))
                    if crc_ax25(o[:-2]) == o[-2] | (o[-1] << 8):
                        sortie.append((t, o[:-2]))
            self.bits, self.uns = [], 0
            return
        if self.bits is None:
            return
        if b:
            self.uns += 1
            if self.uns > 6:                                        # 7 uns : abandon de trame
                self.bits = None
                return
            self.bits.append(1)
        else:
            if self.uns == 5:                                       # bit de bourrage
                self.uns = 0
                return
            self.uns = 0
            self.bits.append(0)
        if len(self.bits) > 8 * 340:
            self.bits = None


class DemodAFSK:
    """Audio en blocs (TAUX_AFSK) → trames AX.25 (sans le CRC), chacune rendue une seule fois."""

    def __init__(self, fs: int = TAUX_AFSK) -> None:
        self.fs = fs
        self.spb = fs / DEBIT
        self.n = int(round(self.spb))
        self.t = 0                                                  # échantillons déjà reçus
        self.queue_m = np.zeros(self.n - 1, dtype=np.complex128)    # fin du bloc précédent (moyennes glissantes)
        self.queue_s = np.zeros(self.n - 1, dtype=np.complex128)
        b0, b1, b2, a1, a2 = passe_bas_2(LISSAGE, fs)
        sos = np.array([[b0, b1, b2, 1.0, a1, a2]])
        self.tranches = [_Tranche(g, self.spb, sos) for g in PENTES]
        self.recentes: list[tuple[float, bytes]] = []

    def traiter(self, audio: np.ndarray) -> list[bytes]:
        w = 2 * np.pi * (self.t + np.arange(len(audio))) / self.fs
        tm = np.concatenate((self.queue_m, audio * np.exp(-1j * MARQUE * w)))
        ts = np.concatenate((self.queue_s, audio * np.exp(-1j * ESPACE * w)))
        cm, cs = np.cumsum(np.concatenate(([0], tm))), np.cumsum(np.concatenate(([0], ts)))
        am, as_ = np.abs(cm[self.n:] - cm[:-self.n]), np.abs(cs[self.n:] - cs[:-self.n])   # sur un bit
        self.queue_m, self.queue_s = tm[len(tm) - (self.n - 1):], ts[len(ts) - (self.n - 1):]
        trouvees: list = []
        for tr in self.tranches:
            tr.traiter(am - tr.g * as_, self.t, trouvees)
        self.t += len(audio)
        nouvelles = []
        for t, o in sorted(trouvees, key=lambda e: e[0]):
            if any(o == r and abs(t - tr) < self.fs for tr, r in self.recentes):
                continue                                             # déjà rendue par une autre pente
            self.recentes.append((t, o))
            nouvelles.append(o)
        self.recentes = [(t, o) for t, o in self.recentes if self.t - t < 2 * self.fs]
        return nouvelles


# ======================================================================= AX.25 → texte
def _adresse(o: bytes) -> tuple[str, bool, bool]:
    ind = "".join(chr(c >> 1) for c in o[:6]).strip()
    ssid = (o[6] >> 1) & 0x0F
    return (f"{ind}-{ssid}" if ssid else ind), bool(o[6] & 0x80), bool(o[6] & 1)


def trame_vers_texte(o: bytes) -> str | None:
    """« SOURCE>DEST,RELAIS*,…:info », ou None si ce n'est pas une trame UI valide."""
    adresses, i = [], 0
    while i + 7 <= len(o):
        adresses.append(_adresse(o[i:i + 7]))
        i += 7
        if adresses[-1][2]:
            break
    if len(adresses) < 2 or not adresses[-1][2] or len(adresses) > 10 or i + 2 > len(o):
        return None
    if any(not a[0] or not all(c.isascii() and (c.isalnum() or c == "-") for c in a[0]) for a in adresses):
        return None
    if o[i] != 0x03 or o[i + 1] != 0xF0:                             # trame UI, sans couche 3
        return None
    relais = [a[0] + ("*" if a[1] else "") for a in adresses[2:]]
    info = o[i + 2:].decode("latin-1").rstrip("\r\n")
    return f"{adresses[1][0]}>{','.join([adresses[0][0]] + relais)}:{info}"


# ======================================================================= module
FREQUENCES = [(144_800_000, "144,800 MHz · Europe"), (144_390_000, "144,390 MHz · Amérique du Nord"),
              (145_825_000, "145,825 MHz · ISS")]
TAUX_CANAL = 48_000


class DecodeurAPRS(Module):
    nom = "Décodeur APRS"
    description = ("Trames APRS (AFSK 1200 bauds) décodées sans logiciel externe, sur 144,800 MHz ou une "
                   "autre fréquence APRS de la bande reçue, même si le VFO écoute ailleurs.")

    def demarrer(self) -> None:
        self.frequence = FREQUENCES[0][0]
        self.blocs: queue.Queue = queue.Queue(maxsize=16)
        self.trames: queue.Queue = queue.Queue()
        self.etat = {"instant": 0.0, "couvert": False, "saturation": 0.0, "perdus": 0}
        self.nombre = 0
        self.journal = None
        self._actif = True
        self._fil = threading.Thread(target=self._boucle, name="decodeur-aprs", daemon=True)
        self._fil.start()
        self.abonner("iq", self._sur_iq)

    # ------------------------------------------------------------- thread DSP : rapide
    def _sur_iq(self, iq: np.ndarray, centre: float, taux: int) -> None:
        try:
            self.blocs.put_nowait((iq, centre, taux))
        except queue.Full:
            self.etat["perdus"] += 1

    # ------------------------------------------------------------- thread de décodage
    def _boucle(self) -> None:
        bande = None
        while self._actif:
            try:
                tache = self.blocs.get(timeout=0.5)
            except queue.Empty:
                continue
            if tache is None:
                break
            iq, centre, taux = tache
            f = self.frequence
            couvert = abs(f - centre) + 10_000 < 0.42 * taux
            sat = float(np.mean((np.abs(iq.real) > 0.95) | (np.abs(iq.imag) > 0.95)))
            self.etat.update(instant=time.monotonic(), couvert=couvert, saturation=sat)
            if not couvert:
                bande = None
                continue
            if bande != (centre, taux, f):
                bande = (centre, taux, f)
                d1, d2 = plan_decimation(taux, TAUX_CANAL)
                inter = taux / d1
                nco = NCO(taux)
                nco.freq = f - centre
                dec1, dec2 = Decimateur(d1, taux, 0.42 * inter), Decimateur(d2, inter, 12_000)
                dec_audio = Decimateur(2, TAUX_CANAL, 6_000)
                prec = 0j
                demod = DemodAFSK()
            canal = dec2.traiter(dec1.traiter(nco.melanger(iq)))
            z = np.concatenate(([prec], canal))
            prec = canal[-1] if len(canal) else prec
            fm = np.angle(z[1:] * np.conj(z[:-1])).astype(np.complex64)       # discriminateur FM
            audio = dec_audio.traiter(fm).real
            for o in demod.traiter(audio):
                texte = trame_vers_texte(o)
                if texte:
                    self.trames.put(texte)

    # ------------------------------------------------------------- interface
    def creer_widget(self) -> QWidget:
        w = QWidget()
        w.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        lay = QVBoxLayout(w)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(4)
        ligne = QHBoxLayout()
        self.choix = QComboBox()
        for f, texte in FREQUENCES:
            self.choix.addItem(texte, f)
        self.choix.currentIndexChanged.connect(lambda _: setattr(self, "frequence", self.choix.currentData()))
        accord = QPushButton("Accorder")
        accord.setToolTip("Règle le VFO sur la fréquence APRS en NFM (pour l'entendre aussi)")
        accord.clicked.connect(self._accorder)
        ligne.addWidget(self.choix, 1)
        ligne.addWidget(accord)
        lay.addLayout(ligne)
        self.statut = QLabel("—")
        self.statut.setWordWrap(True)
        lay.addWidget(self.statut)
        self.liste = QListWidget()
        self.liste.setWordWrap(True)
        hauteur = self.liste.fontMetrics().lineSpacing()
        self.liste.setMaximumHeight(9 * hauteur)
        lay.addWidget(self.liste)
        boutons = QHBoxLayout()
        effacer = QPushButton("Effacer")
        effacer.clicked.connect(self.liste.clear)
        copier = QPushButton("Copier")
        copier.clicked.connect(lambda: QGuiApplication.clipboard().setText(
            "\n".join(self.liste.item(i).text() for i in range(self.liste.count() - 1, -1, -1))))
        self.c_journal = QCheckBox("Journal")
        self.c_journal.setToolTip("Enregistre les trames, horodatées, dans le dossier du module")
        self.c_journal.toggled.connect(self._basculer_journal)
        for b in (effacer, copier, self.c_journal):
            boutons.addWidget(b)
        lay.addLayout(boutons)
        self.minuterie = QTimer(w, interval=250, timeout=self._rafraichir)
        self.minuterie.start()
        self._rafraichir()
        return w

    def _accorder(self) -> None:
        self.ctx.regler_mode("NFM")
        self.ctx.regler_frequence(self.frequence)

    def _rafraichir(self) -> None:
        while True:
            try:
                texte = self.trames.get_nowait()
            except queue.Empty:
                break
            self.nombre += 1
            ligne = f"{time.strftime('%H:%M:%S')}  {texte}"
            self.liste.insertItem(0, ligne)
            if self.liste.count() > 500:
                self.liste.takeItem(500)
            if self.journal:
                self.journal.write(ligne + "\n")
                self.journal.flush()
        e = self.etat
        if time.monotonic() - e["instant"] > 1:
            texte = "En attente des échantillons du récepteur."
        elif not e["couvert"]:
            texte = f"{self.choix.currentText().split(' ·')[0]} hors de la bande reçue : cliquez sur Accorder."
        else:
            texte = f"Écoute de {self.choix.currentText().split(' ·')[0]} · {self.nombre} trame(s)"
            if e["saturation"] > 0.001:
                texte += f" · SATURATION {100 * e['saturation']:.1f} % : baissez le gain RF ou éloignez l'émetteur"
        self.statut.setText(texte)

    def _basculer_journal(self, actif: bool) -> None:
        if actif:
            nom = time.strftime("aprs_%Y%m%d.txt")
            self.journal = open(self.ctx.dossier_donnees("decodeur_aprs") / nom, "a", encoding="utf-8")
        elif self.journal:
            self.journal.close()
            self.journal = None

    def arreter(self) -> None:
        self._actif = False
        try:
            self.blocs.put_nowait(None)
        except queue.Full:
            pass
        self._fil.join(timeout=2)
        if hasattr(self, "minuterie"):
            self.minuterie.stop()
        if self.journal:
            self.journal.close()
            self.journal = None

# SPDX-License-Identifier: MIT
"""Tonalités CTCSS et DTMF pour SDR Modulaire : sous-porteuse d'accès d'un relais, codes DTMF, 1750 Hz.

Le module démodule lui-même la FM sur la fréquence du VFO (le mode choisi n'a pas d'importance) :
NCO → décimation à 48 kHz → discriminateur FM (en Hz de déviation). Puis deux branches :

- CTCSS : filtre passe-bas et décimation à 8 kHz puis à 1 kHz ; toutes les 0,5 s, DFT d'une fenêtre de
  Hann de 2 s (raies de 0,5 Hz entre 55 et 270 Hz), raie la plus forte, interpolation gaussienne sur
  les trois raies voisines (précision de l'ordre de 0,05 Hz), comparaison aux 52 tonalités normalisées.
  La tonalité est retenue si elle porte au moins la moitié de l'énergie de la bande 55–270 Hz, deux
  analyses de suite ; la déviation est tirée de cette énergie (Parseval).
- DTMF et 1750 Hz : 8 kHz, passe-haut à 400 Hz, blocs de 205 échantillons (25,6 ms), Goertzel sur
  les 8 fréquences DTMF et autour de 1750 Hz ; une touche est retenue sur deux blocs de suite, deux
  tonalités qui portent l'essentiel de l'énergie du bloc, une seule par groupe.

Même algorithme dans SDR Web (web/tonalites.js).
"""

from __future__ import annotations

import math
import queue
import threading
import time

import numpy as np
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont, QGuiApplication
from PySide6.QtWidgets import (QCheckBox, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton,
                               QSizePolicy, QVBoxLayout, QWidget)
from scipy import signal

from sdrmod.dsp import Decimateur, NCO, plan_decimation
from sdrmod.modules import Module

TAUX = 48_000
TAUX_DTMF = 8_000
TAUX_CTCSS = 1_000
FENETRE_CTCSS = 2_000          # échantillons à 1 kHz : 2 s, raies de 0,5 Hz
PAS_CTCSS = 500                # une analyse toutes les 0,5 s
RAIE_MIN, RAIE_MAX = 110, 540  # 55 à 270 Hz
BLOC_DTMF = 205

CTCSS = (67.0, 69.3, 71.9, 74.4, 77.0, 79.7, 82.5, 85.4, 88.5, 91.5, 94.8, 97.4, 100.0, 103.5, 107.2,
         110.9, 114.8, 118.8, 123.0, 127.3, 131.8, 136.5, 141.3, 146.2, 150.0, 151.4, 156.7, 157.7, 159.8,
         162.2, 165.5, 167.9, 171.3, 173.8, 177.3, 179.9, 183.5, 186.2, 189.9, 192.8, 196.6, 199.5, 203.5,
         206.5, 210.7, 218.1, 225.7, 229.1, 233.6, 241.8, 250.3, 254.1)
DTMF_BAS = (697, 770, 852, 941)
DTMF_HAUT = (1209, 1336, 1477, 1633)
TOUCHES = ("123A", "456B", "789C", "*0#D")
F_1750 = (1720, 1735, 1750, 1765, 1780)


def norme_ctcss(f: float) -> float | None:
    """Tonalité normalisée la plus proche, ou None si aucune à moins de 0,7 Hz."""
    n = min(CTCSS, key=lambda c: abs(c - f))
    return n if abs(n - f) <= 0.7 else None


def passe_bas(fs: float, coupure: float, n: int) -> np.ndarray:
    """Sinus cardinal fenêtré (Hamming), gain unité ; écrit à la main pour être identique en JavaScript."""
    m = (n - 1) / 2
    h = np.empty(n)
    for i in range(n):
        x = i - m
        s = 2 * coupure / fs if x == 0 else math.sin(2 * math.pi * coupure / fs * x) / (math.pi * x)
        h[i] = s * (0.54 - 0.46 * math.cos(2 * math.pi * i / (n - 1)))
    return h / h.sum()


class FirDecimant:
    """Filtre FIR réel décimant, à état (phase de décimation gardée entre blocs)."""

    def __init__(self, h: np.ndarray, d: int) -> None:
        self.h, self.d = h, d
        self.hist = np.zeros(len(h) - 1)
        self.phase = 0

    def traiter(self, x: np.ndarray) -> np.ndarray:
        b = np.concatenate((self.hist, x))
        y = np.convolve(b, self.h, "valid")[self.phase::self.d]
        self.phase = (self.phase - len(x)) % self.d
        self.hist = b[len(b) - len(self.hist):]
        return y


def passe_haut_biquad(fs: float, f0: float, q: float = 0.7071) -> tuple[np.ndarray, np.ndarray]:
    """Passe-haut du second ordre (formules RBJ)."""
    w = 2 * math.pi * f0 / fs
    al = math.sin(w) / (2 * q)
    c = math.cos(w)
    b = np.array([(1 + c) / 2, -(1 + c), (1 + c) / 2])
    a = np.array([1 + al, -2 * c, 1 - al])
    return b / a[0], a / a[0]


_N_TR = PAS_CTCSS
_HANN_TR = 0.5 - 0.5 * np.cos(2 * np.pi * np.arange(_N_TR) / (_N_TR - 1))
_TR_MIN, _TR_MAX = 28, 135      # raies de 2 Hz : 56 à 270 Hz


def tranche_presente(brut: np.ndarray, f: float) -> bool:
    """La tonalité f porte-t-elle au moins 40 % de l'énergie 56–270 Hz de cette tranche de 0,5 s ?
    |X(f)|² · N · Σw² / ((Σw)² · Σ|X_k|²) vaut 1 pour une sinusoïde seule à f (Parseval)."""
    x = (brut - brut.mean()) * _HANN_TR
    bande = float(np.sum(np.abs(np.fft.rfft(x)[_TR_MIN:_TR_MAX + 1]) ** 2)) + 1e-30
    n = np.arange(_N_TR)
    xf = abs(np.dot(x, np.exp(-2j * np.pi * f * n / TAUX_CTCSS))) ** 2
    sw = float(_HANN_TR.sum())
    return xf * _N_TR * float(np.sum(_HANN_TR ** 2)) / (sw * sw * bande) >= 0.4


FREQ_BLOC = DTMF_BAS + DTMF_HAUT + F_1750
# |X(f)|² d'un bloc pour des fréquences quelconques : ce que donne Goertzel (web/tonalites.js), en une
# multiplication de matrices.
_DFT_BLOC = np.exp(-2j * np.pi * np.outer(np.arange(BLOC_DTMF), FREQ_BLOC) / TAUX_DTMF)


class MoteurTonalites:
    """Discriminateur FM à 48 kHz (en Hz) → évènements CTCSS, DTMF et 1750 Hz (listes de dict)."""

    def __init__(self) -> None:
        self.vers8k = FirDecimant(passe_bas(TAUX, 3_400, 63), TAUX // TAUX_DTMF)
        self.vers1k = FirDecimant(passe_bas(TAUX_DTMF, 300, 161), TAUX_DTMF // TAUX_CTCSS)
        self.hp_b, self.hp_a = passe_haut_biquad(TAUX_DTMF, 400)
        self.hp_zi = np.zeros(2)
        self.ctcss_buf = np.zeros(0)
        self.ctcss_n = 0                 # échantillons à 1 kHz reçus depuis la dernière analyse
        n = np.arange(FENETRE_CTCSS)
        self.hann = 0.5 - 0.5 * np.cos(2 * np.pi * n / (FENETRE_CTCSS - 1))
        self.somme_w2 = float(np.sum(self.hann ** 2))
        self.dtmf_buf = np.zeros(0)
        self.t = 0.0                     # secondes de signal traitées
        # CTCSS
        self.candidat = None             # (fréquence de référence, mesure) de la dernière analyse
        self.ctcss = None                # tonalité en cours : dict
        self.manques = 0
        self.mesure = None               # dernière analyse : (f, part, déviation)
        # DTMF et 1750
        self.precedent = None            # touche vue au bloc précédent
        self.tenue = None                # touche déjà annoncée, en attente d'un silence
        self.blocs_1750 = 0
        self.ton_1750 = None             # instant de début
        self.manques_1750 = 0
        self.touches = 0
        self.ev: list[dict] = []

    def traiter(self, fm: np.ndarray) -> list[dict]:
        self.ev = []
        x8 = self.vers8k.traiter(fm.astype(np.float64))
        self._ctcss(self.vers1k.traiter(x8))
        hp, self.hp_zi = signal.lfilter(self.hp_b, self.hp_a, x8, zi=self.hp_zi)
        self.dtmf_buf = np.concatenate((self.dtmf_buf, hp))
        while len(self.dtmf_buf) >= BLOC_DTMF:
            self._bloc(self.dtmf_buf[:BLOC_DTMF])
            self.dtmf_buf = self.dtmf_buf[BLOC_DTMF:]
            self.t += BLOC_DTMF / TAUX_DTMF
        return self.ev

    # ------------------------------------------------------------- CTCSS
    def _ctcss(self, x: np.ndarray) -> None:
        while len(x):                    # une analyse tous les PAS_CTCSS échantillons, pile
            k = min(len(x), PAS_CTCSS - self.ctcss_n)
            self.ctcss_buf = np.concatenate((self.ctcss_buf, x[:k]))[-FENETRE_CTCSS:]
            x = x[k:]
            self.ctcss_n += k
            if self.ctcss_n == PAS_CTCSS:
                self.ctcss_n = 0
                if len(self.ctcss_buf) == FENETRE_CTCSS:
                    self._analyser(self.ctcss_buf)

    def _analyser(self, brut: np.ndarray) -> None:
        x = (brut - brut.mean()) * self.hann
        p = np.abs(np.fft.rfft(x)[RAIE_MIN:RAIE_MAX + 1]) ** 2
        i = int(np.argmax(p[1:-1])) + 1
        total = float(p.sum()) + 1e-30
        tonalite = float(p[max(0, i - 2):i + 3].sum())
        part = tonalite / total
        a, b, c = (math.log(v + 1e-30) for v in p[i - 1:i + 2])
        den = 2 * (2 * b - a - c)
        delta = (c - a) / den if den > 0 else 0.0
        f = (RAIE_MIN + i + max(-0.5, min(0.5, delta))) * TAUX_CTCSS / FENETRE_CTCSS
        deviation = 2 * math.sqrt(tonalite / (FENETRE_CTCSS * self.somme_w2))
        self.mesure = (f, part, deviation)
        present = part >= 0.5
        ref = norme_ctcss(f) or round(f * 2) / 2
        if self.ctcss:
            # Tonalité suivie : présence jugée sur la dernière tranche de 0,5 s (la fenêtre de 2 s
            # garderait la tonalité 1,5 s de trop) ; fin après deux tranches sans elle.
            c = self.ctcss
            if tranche_presente(brut[-PAS_CTCSS:], c["f"]):
                c["fin"] = self.t
                self.manques = 0
                if present and abs(ref - c["ref"]) < 0.3:
                    c["n"] += 1
                    c["f"] += (f - c["f"]) / c["n"]
                    c["deviation"] += (deviation - c["deviation"]) / c["n"]
            else:
                self.manques += 1
                if self.manques >= 2:
                    self._fin_ctcss()
        k = 0
        if not self.ctcss and present and self.candidat and abs(ref - self.candidat[0]) < 0.3:
            # début : tranches de la fenêtre qui contiennent déjà la tonalité, en partant de la fin
            while k < FENETRE_CTCSS // PAS_CTCSS and tranche_presente(
                    brut[FENETRE_CTCSS - (k + 1) * PAS_CTCSS:FENETRE_CTCSS - k * PAS_CTCSS], f):
                k += 1
        if not self.ctcss and k:            # k = 0 : reste d'une tonalité finie dans la fenêtre de 2 s
            self.ctcss = {"ref": ref, "norme": norme_ctcss(f), "f": (f + self.candidat[1]) / 2, "n": 2,
                          "deviation": deviation, "debut": self.t - k * PAS_CTCSS / TAUX_CTCSS, "fin": self.t}
            self.manques = 0
            self.ev.append(dict(self.ctcss, type="ctcss"))
        self.candidat = (ref, f) if present else None

    def _fin_ctcss(self) -> None:
        c = self.ctcss
        self.ev.append(dict(c, type="ctcss_fin", duree=c["fin"] - c["debut"]))
        self.ctcss = None
        self.manques = 0

    # ------------------------------------------------------------- DTMF et 1750 Hz
    def _bloc(self, x: np.ndarray) -> None:
        e = float(np.dot(x, x)) * BLOC_DTMF / 2 + 1e-30        # |X|² d'une sinusoïde seule qui ferait toute l'énergie
        p = np.abs(x @ _DFT_BLOC) ** 2 / e
        bas, haut = list(p[:4]), list(p[4:8])
        ib, ih = int(np.argmax(bas)), int(np.argmax(haut))
        pb, ph = bas[ib], haut[ih]
        autres = max([v for j, v in enumerate(bas) if j != ib] + [v for j, v in enumerate(haut) if j != ih])
        touche = None
        if pb >= 0.08 and ph >= 0.08 and pb + ph >= 0.55 and autres < 0.25 * min(pb, ph):
            touche = TOUCHES[ib][ih]
        if touche and touche == self.precedent and touche != self.tenue:
            self.tenue = touche
            self.touches += 1
            self.ev.append({"type": "dtmf", "touche": touche, "t": self.t})
        elif not touche and not self.precedent:
            self.tenue = None                      # deux blocs sans touche : la même peut revenir
        self.precedent = touche
        # 1750 Hz : tonalité seule, sur au moins 4 blocs (~100 ms)
        p1750 = float(p[8:].max())
        if p1750 >= 0.5 and not touche:
            self.blocs_1750 += 1
            self.manques_1750 = 0
            if self.blocs_1750 == 4 and self.ton_1750 is None:
                self.ton_1750 = self.t - 3 * BLOC_DTMF / TAUX_DTMF
                self.ev.append({"type": "1750", "t": self.t})
        else:
            self.blocs_1750 = 0
            if self.ton_1750 is not None:
                self.manques_1750 += 1
                if self.manques_1750 >= 2:
                    self.ev.append({"type": "1750_fin", "duree": self.t - self.ton_1750 - BLOC_DTMF / TAUX_DTMF})
                    self.ton_1750 = None


def texte_ctcss(c: dict) -> str:
    if c["norme"] is not None:
        nom = f"{c['norme']:.1f} Hz".replace(".", ",")
        return f"CTCSS {nom} (mesuré {c['f']:.2f} Hz, déviation ≈ {c['deviation']:.0f} Hz)".replace(".", ",")
    return f"tonalité {c['f']:.1f} Hz hors norme (déviation ≈ {c['deviation']:.0f} Hz)".replace(".", ",")


def secondes(d: float) -> str:
    return f"{d:.1f} s".replace(".", ",")


# ======================================================================= module
class TonalitesModule(Module):
    nom = "Tonalités CTCSS et DTMF"
    description = ("Sous-porteuse CTCSS d'accès d'un relais (fréquence normalisée, mesure, déviation), codes "
                   "DTMF et tonalité 1750 Hz entendus sur la fréquence du VFO, quel que soit le mode.")

    def demarrer(self) -> None:
        self.frequence = self.ctx.frequence()
        self.blocs: queue.Queue = queue.Queue(maxsize=16)
        self.evenements: queue.Queue = queue.Queue()
        self.etat = {"instant": 0.0, "couvert": False, "perdus": 0, "crete": None, "bruit": None,
                     "calcul": 0.0, "mesure": None, "ctcss": None, "en_1750": False, "touches": 0}
        self.ligne_ctcss: QListWidgetItem | None = None
        self.ligne_dtmf: QListWidgetItem | None = None
        self.sequence = ""
        self.heure_sequence = ""
        self.derniere_touche = 0.0
        self.t_touche = 0.0
        self.journal = None
        self._actif = True
        self._fil = threading.Thread(target=self._boucle, name="tonalites", daemon=True)
        self._fil.start()
        self.abonner("iq", self._sur_iq)
        self.abonner("vfo", self._sur_vfo)

    def _sur_vfo(self, f: float, _mode: str) -> None:
        self.frequence = f

    def _sur_iq(self, iq: np.ndarray, centre: float, taux: int) -> None:
        try:
            self.blocs.put_nowait((iq, centre, taux))
        except queue.Full:
            self.etat["perdus"] += 1

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
            debut = time.perf_counter()
            f = self.frequence
            couvert = abs(f - centre) + 8_000 < 0.42 * taux
            self.etat.update(instant=time.monotonic(), couvert=couvert)
            if not couvert:
                bande = None
                continue
            if bande is None or bande[:2] != (centre, taux):
                d1, d2 = plan_decimation(taux, TAUX)
                inter = taux / d1
                nco = NCO(taux)
                dec1, dec2 = Decimateur(d1, taux, 0.42 * inter), Decimateur(d2, inter, 7_000)
                prec = 0j
                moteur = MoteurTonalites()
            if bande is not None and bande[2] != f:
                moteur = MoteurTonalites()      # autre fréquence : autres tonalités
                self.evenements.put({"type": "accord"})
            nco.freq = f - centre
            bande = (centre, taux, f)
            canal = dec2.traiter(dec1.traiter(nco.melanger(iq)))
            if not len(canal):
                continue
            p = 10 * np.log10(float(np.mean(np.abs(canal) ** 2)) + 1e-20)
            e = self.etat
            e["bruit"] = p if e["bruit"] is None else min(p, e["bruit"] + 0.02)
            e["crete"] = p if e["crete"] is None else max(p, e["crete"] - 0.5)
            z = np.concatenate(([prec], canal))
            prec = canal[-1]
            fm = np.angle(z[1:] * np.conj(z[:-1])) * (TAUX / (2 * np.pi))
            for ev in moteur.traiter(fm):
                self.evenements.put(ev)
            e.update(mesure=moteur.mesure, ctcss=dict(moteur.ctcss) if moteur.ctcss else None,
                     en_1750=moteur.ton_1750 is not None, touches=moteur.touches)
            e["calcul"] = 0.9 * e["calcul"] + 0.1 * (time.perf_counter() - debut) / (len(iq) / taux)

    # ------------------------------------------------------------- interface
    def creer_widget(self) -> QWidget:
        w = QWidget()
        w.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        lay = QVBoxLayout(w)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(4)
        self.statut = QLabel("—")
        self.statut.setWordWrap(True)
        lay.addWidget(self.statut)
        gras = QFont()
        gras.setBold(True)
        gras.setPointSizeF(gras.pointSizeF() * 1.3)
        self.l_ctcss = QLabel("CTCSS : —")
        self.l_ctcss.setFont(gras)
        self.l_ctcss.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self.l_ctcss)
        self.l_detail = QLabel("")
        self.l_detail.setWordWrap(True)
        lay.addWidget(self.l_detail)
        self.l_dtmf = QLabel("DTMF : —")
        self.l_dtmf.setFont(gras)
        self.l_dtmf.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self.l_dtmf)
        self.diag = QLabel("")
        self.diag.setWordWrap(True)
        self.diag.setStyleSheet("color: gray")
        self.diag.setToolTip("Crête du canal au-dessus du bruit, dernière analyse CTCSS (fréquence de la raie la plus "
                             "forte, part de l'énergie 55–270 Hz), touches DTMF reçues, part du temps réel utilisée")
        lay.addWidget(self.diag)
        self.liste = QListWidget()
        self.liste.setWordWrap(True)
        self.liste.setMaximumHeight(9 * self.liste.fontMetrics().lineSpacing())
        lay.addWidget(self.liste)
        boutons = QHBoxLayout()
        effacer = QPushButton("Effacer")
        effacer.clicked.connect(self.effacer)
        copier = QPushButton("Copier")
        copier.clicked.connect(self.copier)
        self.c_journal = QCheckBox("Journal")
        self.c_journal.setToolTip("Enregistre les tonalités et les codes, horodatés, dans le dossier du module")
        self.c_journal.toggled.connect(self._basculer_journal)
        for b in (effacer, copier, self.c_journal):
            boutons.addWidget(b)
        lay.addLayout(boutons)
        self.minuterie = QTimer(w, interval=200, timeout=self._rafraichir)
        self.minuterie.start()
        self._rafraichir()
        return w

    def effacer(self) -> None:
        self.liste.clear()
        self.ligne_ctcss = self.ligne_dtmf = None

    def copier(self) -> None:
        QGuiApplication.clipboard().setText("\n".join(self.liste.item(i).text()
                                                       for i in reversed(range(self.liste.count()))))

    def _basculer_journal(self, actif: bool) -> None:
        if self.journal:
            self.journal.close()
            self.journal = None
        if actif:
            nom = time.strftime("tonalites_%Y%m%d_%H%M%S.txt")
            self.journal = open(self.ctx.dossier_donnees("tonalites") / nom, "a", encoding="utf-8")

    def _ajouter(self, texte: str, journal: bool = True) -> QListWidgetItem:
        item = QListWidgetItem(texte)
        self.liste.insertItem(0, item)
        while self.liste.count() > 300:
            self.liste.takeItem(self.liste.count() - 1)
        if journal:
            self._ecrire(texte)
        return item

    def _ecrire(self, texte: str) -> None:
        if self.journal and texte:
            self.journal.write(texte + "\n")
            self.journal.flush()

    @staticmethod
    def _changer(item: QListWidgetItem | None, texte: str) -> None:
        try:
            if item is not None:
                item.setText(texte)
        except RuntimeError:            # ligne effacée entre-temps
            pass

    def _clore_sequence(self) -> None:
        if self.sequence:
            self._ecrire(f"{self.heure_sequence}  DTMF {self.sequence}")
        self.sequence = ""
        self.ligne_dtmf = None

    def _evenement(self, ev: dict) -> None:
        h = time.strftime("%H:%M:%S")
        t = ev["type"]
        if t == "ctcss":
            self.heure_ctcss = h
            self.ligne_ctcss = self._ajouter(f"{h}  {texte_ctcss(ev)}  …", journal=False)
        elif t == "ctcss_fin":
            texte = f"{getattr(self, 'heure_ctcss', h)}  {texte_ctcss(ev)}  {secondes(ev['duree'])}"
            self._changer(self.ligne_ctcss, texte)
            self._ecrire(texte)
            self.ligne_ctcss = None
        elif t == "dtmf":
            if not self.sequence or ev["t"] - self.t_touche > 2 or time.monotonic() - self.derniere_touche > 3:
                self._clore_sequence()
                self.heure_sequence = h
                self.ligne_dtmf = self._ajouter(f"{h}  DTMF", journal=False)
            self.sequence += ev["touche"]
            self.t_touche = ev["t"]
            self.derniere_touche = time.monotonic()
            self._changer(self.ligne_dtmf, f"{self.heure_sequence}  DTMF {self.sequence}")
        elif t == "1750":
            self.heure_1750 = h
            self.ligne_1750 = self._ajouter(f"{h}  tonalité 1750 Hz (ouverture de relais)  …", journal=False)
        elif t == "1750_fin":
            texte = f"{getattr(self, 'heure_1750', h)}  tonalité 1750 Hz (ouverture de relais)  {secondes(ev['duree'])}"
            self._changer(getattr(self, "ligne_1750", None), texte)
            self._ecrire(texte)
        elif t == "accord":
            self._clore_sequence()
            self.ligne_ctcss = None

    def _rafraichir(self) -> None:
        while True:
            try:
                self._evenement(self.evenements.get_nowait())
            except queue.Empty:
                break
        if self.sequence and time.monotonic() - self.derniere_touche > 3:
            self._clore_sequence()
        e = self.etat
        c = e["ctcss"]
        if time.monotonic() - e["instant"] > 1.5:
            self.statut.setText("En attente des échantillons du récepteur.")
        elif not e["couvert"]:
            self.statut.setText("Le VFO est trop près du bord de la bande reçue.")
        elif c or e["en_1750"]:
            self.statut.setText("Tonalité reçue.")
        else:
            self.statut.setText("Accordez le VFO sur un relais (sortie ou entrée) ou une fréquence FM. Le mode n'a "
                                "pas d'importance : le module démodule lui-même.")
        if c:
            nom = f"{c['norme']:.1f} Hz" if c["norme"] is not None else f"{c['f']:.1f} Hz (hors norme)"
            self.l_ctcss.setText(f"CTCSS : {nom}".replace(".", ","))
            self.l_detail.setText(f"mesuré {c['f']:.2f} Hz · déviation ≈ {c['deviation']:.0f} Hz".replace(".", ","))
        else:
            self.l_ctcss.setText("CTCSS : —")
            self.l_detail.setText("tonalité 1750 Hz en cours" if e["en_1750"] else "")
        if e["en_1750"] and c:
            self.l_detail.setText(self.l_detail.text() + " · tonalité 1750 Hz en cours")
        self.l_dtmf.setText(f"DTMF : {self.sequence}" if self.sequence else "DTMF : —")
        if e["crete"] is not None:
            m = e["mesure"]
            ana = f" · raie {m[0]:.1f} Hz, {100 * m[1]:.0f} %".replace(".", ",") if m else ""
            perdus = f" · {e['perdus']} blocs perdus" if e["perdus"] else ""
            self.diag.setText(f"crête {e['crete'] - e['bruit']:.0f} dB{ana} · {e['touches']} touches"
                              f"{perdus} · calcul {100 * e['calcul']:.0f} %")

    def arreter(self) -> None:
        self._actif = False
        try:
            self.blocs.put_nowait(None)
        except queue.Full:
            pass
        if hasattr(self, "minuterie"):
            self.minuterie.stop()
        if self.journal:
            self._clore_sequence()
            self.journal.close()
            self.journal = None

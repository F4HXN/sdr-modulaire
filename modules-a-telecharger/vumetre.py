# SPDX-License-Identifier: MIT
"""Vu-mètre égaliseur à LED pour SDR Modulaire.

L'audio démodulé est découpé en bandes de fréquence, chaque bande affichée en colonne de LED
(vert, jaune, rouge) comme l'égaliseur d'une chaîne hi-fi, avec la crête maintenue et un reflet au sol ;
une colonne VU à droite montre le niveau global.

- FFT de 4096 échantillons (fenêtre de Hann) sur les dernières 85 ms d'audio, à chaque image (30 par s) ;
- bandes en échelle logarithmique de 50 Hz à la limite du mode (WFM 16 kHz, AM 5 kHz, sinon 3,2 kHz) ;
  puissance de bande en dB, 0 dB = sinusoïde pleine échelle ;
- échelle automatique : le haut suit la bande la plus forte (montée immédiate, descente de 3 dB/s),
  45 dB de dynamique ; VU : niveau efficace global de −40 à 0 dB ;
- balistique : montée en 25 ms, retombée en 300 ms, crête tenue 0,8 s puis retombée de 12 dB/s.

Même calcul et même dessin dans SDR Web (web/vumetre.js).
"""

from __future__ import annotations

import json
import math
import time

import numpy as np
from PySide6.QtCore import QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QPainter
from PySide6.QtWidgets import (QCheckBox, QComboBox, QHBoxLayout, QLabel, QPushButton, QSizePolicy,
                               QVBoxLayout, QWidget)

from sdrmod.modules import Module

TAILLE = 4096
F_MIN = 50.0
DYNAMIQUE = 45.0
VU_MIN = -40.0
NB_LED = 12
BANDES = (8, 12, 16, 24, 32)
FENETRE = (0.5 - 0.5 * np.cos(2 * np.pi * np.arange(TAILLE) / TAILLE)).astype(np.float64)
SOMME_W2 = float(np.sum(FENETRE ** 2))


def frequence_max(mode: str) -> float:
    return 16000.0 if mode == "WFM" else 5000.0 if mode == "AM" else 3200.0


def _mix(a, b, t):
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def couleur_led(i: int, n: int) -> tuple[int, int, int]:
    """LED i (0 = bas) sur n : vert foncé → vert clair, jaune, rouge vif → rouge sombre."""
    p = (i + 0.5) / n
    if p < 4 / 12:
        return _mix((62, 125, 32), (150, 245, 100), p / (4 / 12))
    if p < 7 / 12:
        return _mix((235, 200, 40), (255, 240, 110), (p - 4 / 12) / (3 / 12))
    return _mix((255, 28, 10), (125, 26, 14), (p - 7 / 12) / (5 / 12))


def bords(n: int, fmax: float) -> np.ndarray:
    """Bords des bandes (Hz), répartis en échelle logarithmique."""
    return F_MIN * (fmax / F_MIN) ** (np.arange(n + 1) / n)


def analyser(x: np.ndarray, taux: float, n: int, fmax: float) -> tuple[np.ndarray, float]:
    """Puissance des bandes en dB (0 dB = sinusoïde pleine échelle) et niveau efficace global (dB)."""
    p = np.abs(np.fft.rfft(x * FENETRE)) ** 2
    b = bords(n, min(fmax, taux / 2))
    df = taux / TAILLE
    norme = 2 / (TAILLE * SOMME_W2) / 0.5
    db = np.empty(n)
    for j in range(n):
        k0, k1 = math.ceil(b[j] / df), math.ceil(b[j + 1] / df)
        if k1 > k0:
            s = float(np.sum(p[k0:k1]))
        else:                                          # bande plus étroite qu'une raie
            s = float(p[round((b[j] + b[j + 1]) / 2 / df)]) * (b[j + 1] - b[j]) / df
        db[j] = 10 * math.log10(s * norme + 1e-12)
    vu = 10 * math.log10(float(np.mean(x * x)) / 0.5 + 1e-12)
    return db, vu


class MoteurVumetre:
    """Analyse continue et balistique : position 0..1 de chaque colonne (bandes puis VU) et de sa crête."""

    def __init__(self) -> None:
        self.tampon = np.zeros(TAILLE)
        self.taux = 48000
        self.recu = 0
        self.n = 0
        self.haut = -60.0

    def pousser(self, a: np.ndarray, taux: int) -> None:
        self.taux = taux
        a = np.asarray(a, dtype=np.float64)
        if len(a) >= TAILLE:
            self.tampon[:] = a[-TAILLE:]
        else:
            self.tampon = np.roll(self.tampon, -len(a))
            self.tampon[-len(a):] = a
        self.recu = min(TAILLE, self.recu + len(a))

    def image(self, dt: float, n: int, fmax: float, actif: bool = True) -> tuple[np.ndarray, np.ndarray]:
        if n + 1 != self.n:
            self.n = n + 1
            self.niveaux = np.zeros(n + 1)
            self.cretes = np.zeros(n + 1)
            self.tenues = np.zeros(n + 1)
        cibles = np.zeros(n + 1)
        if actif and self.recu == TAILLE:
            db, vu = analyser(self.tampon.copy(), self.taux, n, fmax)
            m = float(db.max())
            self.haut = m if m > self.haut else max(m, self.haut - 3 * dt)
            sommet = max(self.haut, -60 + DYNAMIQUE) + 2
            cibles[:n] = np.clip((db - (sommet - DYNAMIQUE)) / DYNAMIQUE, 0, 1)
            cibles[n] = min(1.0, max(0.0, (vu - VU_MIN) / -VU_MIN))
        for j in range(n + 1):
            v, c = self.niveaux[j], cibles[j]
            self.niveaux[j] = v + (c - v) * (1 - math.exp(-dt / (0.025 if c > v else 0.3)))
            if self.niveaux[j] >= self.cretes[j]:
                self.cretes[j] = self.niveaux[j]
                self.tenues[j] = 0.8
            else:
                self.tenues[j] -= dt
                if self.tenues[j] < 0:
                    self.cretes[j] = max(self.niveaux[j], self.cretes[j] - 12 / DYNAMIQUE * dt)
        return self.niveaux, self.cretes


COULEURS = [QColor(*couleur_led(i, NB_LED)) for i in range(NB_LED)]


class VueVumetre(QWidget):
    """Colonnes de LED (bandes puis VU) avec reflet au sol, sur fond noir."""

    def __init__(self) -> None:
        super().__init__()
        self.niveaux = np.zeros(1)
        self.cretes = np.zeros(1)
        self.reflet = True
        self.setMinimumSize(260, 150)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def montrer(self, niveaux, cretes, reflet: bool) -> None:
        self.niveaux, self.cretes, self.reflet = niveaux, cretes, reflet
        self.update()

    def paintEvent(self, ev) -> None:
        W, H = self.width(), self.height()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor("#000"))
        p.setPen(Qt.PenStyle.NoPen)
        n_col = len(self.niveaux)
        marge = max(6.0, W * 0.025)
        haut = 15.0
        h_barres = (H - haut) * 0.7 if self.reflet else H - haut - marge * 0.6
        bas = haut + h_barres
        pas_x = (W - 2 * marge) / (n_col + 0.5)
        largeur = pas_x * 0.84
        pas_y = h_barres / NB_LED
        hauteur = pas_y * 0.66

        def x(j: int) -> float:
            return marge + j * pas_x + (pas_x * 0.5 if j == n_col - 1 else 0) + (pas_x - largeur) / 2

        def allumees(j: int) -> int:
            return round(self.niveaux[j] * NB_LED)

        def crete(j: int) -> int:
            return min(NB_LED - 1, round(self.cretes[j] * NB_LED) - 1)

        for j in range(n_col):
            a, c = allumees(j), crete(j)
            for i in range(NB_LED):
                y = bas - (i + 1) * pas_y + (pas_y - hauteur) / 2
                on = i < a or (i == c and c >= 0)
                col = QColor(COULEURS[i])
                if on:                                  # halo autour des LED allumées
                    halo = QColor(col)
                    halo.setAlphaF(0.22)
                    p.setBrush(halo)
                    e = hauteur * 0.35
                    p.drawRoundedRect(QRectF(x(j) - e, y - e, largeur + 2 * e, hauteur + 2 * e), hauteur / 2 + e, hauteur / 2 + e)
                col.setAlphaF(1.0 if on else 0.13)
                p.setBrush(col)
                p.drawRoundedRect(QRectF(x(j), y, largeur, hauteur), hauteur / 2, hauteur / 2)
        if self.reflet:                                 # reflet au sol : écrasé, de plus en plus transparent
            h_reflet = H - bas - 2
            pas_r = min(pas_y * 0.6, h_reflet / NB_LED)
            for j in range(n_col):
                a, c = allumees(j), crete(j)
                for i in range(NB_LED):
                    y = bas + 4 + i * pas_r
                    if y + pas_r * 0.6 > H:
                        break
                    on = i < a or (i == c and c >= 0)
                    col = QColor(COULEURS[i])
                    col.setAlphaF((0.34 if on else 0.05) * (1 - i / NB_LED) ** 1.3)
                    p.setBrush(col)
                    p.drawRoundedRect(QRectF(x(j), y, largeur, pas_r * 0.6), pas_r * 0.3, pas_r * 0.3)
        p.setPen(QColor(255, 255, 255, 140))
        f = QFont("DejaVu Sans Mono")
        f.setPixelSize(11)
        f.setBold(True)
        p.setFont(f)
        p.drawText(QRectF(x(n_col - 1) - 10, 0, largeur + 20, haut - 1),
                   Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom, "VU")
        p.end()


class FenetreVumetre(QWidget):
    """Le vu-mètre en grand, dans une fenêtre à part (redimensionnable)."""

    def __init__(self) -> None:
        super().__init__(None, Qt.WindowType.Window)
        self.setWindowTitle("Vu-mètre égaliseur")
        self.resize(900, 460)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.vue = VueVumetre()
        lay.addWidget(self.vue)
        self.setStyleSheet("background: #000")


class Vumetre(Module):
    nom = "Vu-mètre égaliseur"
    description = ("L'audio découpé en bandes de fréquence, en colonnes de LED vertes, jaunes et rouges avec "
                   "crête et reflet, comme un égaliseur de chaîne hi-fi ; colonne VU du niveau global.")

    def demarrer(self) -> None:
        self.moteur = MoteurVumetre()
        self.dernier = 0.0
        self.fenetre = None
        self._fichier = self.ctx.dossier_donnees("vumetre") / "reglages.json"
        try:
            r = json.loads(self._fichier.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            r = {}
        self.bandes = r.get("bandes", 16) if r.get("bandes") in BANDES else 16
        self.reflet = bool(r.get("reflet", True))
        self.abonner("audio", self._sur_audio)

    def _sur_audio(self, a, taux: int) -> None:          # thread DSP : on stocke seulement
        self.moteur.pousser(a, taux)
        self.dernier = time.monotonic()

    def _retenir(self) -> None:
        try:
            self._fichier.write_text(json.dumps({"bandes": self.bandes, "reflet": self.reflet}), encoding="utf-8")
        except OSError:
            pass

    def creer_widget(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        self.vue = VueVumetre()
        self.vue.setMinimumHeight(170)
        lay.addWidget(self.vue, 1)
        ligne = QHBoxLayout()
        self.c_bandes = QComboBox()
        for n in BANDES:
            self.c_bandes.addItem(f"{n} bandes", n)
        self.c_bandes.setCurrentIndex(BANDES.index(self.bandes))
        self.c_bandes.currentIndexChanged.connect(self._sur_bandes)
        ligne.addWidget(self.c_bandes)
        self.c_reflet = QCheckBox("Reflet")
        self.c_reflet.setChecked(self.reflet)
        self.c_reflet.toggled.connect(self._sur_reflet)
        ligne.addWidget(self.c_reflet)
        ligne.addStretch(1)
        b_grand = QPushButton("Agrandir")
        b_grand.setToolTip("Affiche le vu-mètre en grand dans une fenêtre à part")
        b_grand.clicked.connect(self.agrandir)
        ligne.addWidget(b_grand)
        lay.addLayout(ligne)
        self.statut = QLabel()
        self.statut.setStyleSheet("color: gray")
        lay.addWidget(self.statut)
        self._avant = time.monotonic()
        self._cle = None
        self.minuterie = QTimer(w, interval=33, timeout=self._rafraichir)
        self.minuterie.start()
        return w

    def _sur_bandes(self, i: int) -> None:
        self.bandes = BANDES[i]
        self._retenir()

    def _sur_reflet(self, oui: bool) -> None:
        self.reflet = oui
        self._retenir()

    def agrandir(self) -> None:
        if self.fenetre is None:
            self.fenetre = FenetreVumetre()
        self.fenetre.show()
        self.fenetre.raise_()
        self.fenetre.activateWindow()

    def _rafraichir(self) -> None:
        maintenant = time.monotonic()
        dt = min(0.1, maintenant - self._avant)
        self._avant = maintenant
        mode = self.ctx.mode()
        fmax = frequence_max(mode)
        niveaux, cretes = self.moteur.image(dt, self.bandes, fmax, maintenant - self.dernier < 0.5)
        self.vue.montrer(niveaux, cretes, self.reflet)
        if self.fenetre is not None and self.fenetre.isVisible():
            self.fenetre.vue.montrer(niveaux, cretes, self.reflet)
        cle = (self.bandes, fmax, mode)
        if cle != self._cle:
            self._cle = cle
            b = bords(self.bandes, fmax)

            def hz(f: float) -> str:
                return f"{f / 1000:.1f} kHz".replace(".0 ", " ").replace(".", ",") if f >= 1000 else f"{round(f)} Hz"

            self.statut.setText(f"{self.bandes} bandes de {hz(b[0])} à {hz(b[-1])} ({mode}) · échelle automatique")

    def arreter(self) -> None:
        if hasattr(self, "minuterie"):
            self.minuterie.stop()
        if self.fenetre is not None:
            self.fenetre.close()
            self.fenetre = None

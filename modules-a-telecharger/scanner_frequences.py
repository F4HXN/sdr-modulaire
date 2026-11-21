# SPDX-License-Identifier: MIT
"""Scanner de fréquences pour SDR Modulaire.

Balaye une plage (début, fin, pas, mode), s'arrête dès qu'un signal dépasse le bruit
(estimé sur les derniers pas) d'une marge réglable,
reste tant qu'il est présent, puis reprend après un délai de silence. Une fréquence
gênante (porteuse, parasite) peut être ignorée pour la suite du balayage.

Module à télécharger : placez ce fichier dans ~/.config/sdr-modulaire/modules/.
Aucune dépendance en plus de SDR Modulaire.
"""

from __future__ import annotations

import json
import math
import time
from collections import deque

import numpy as np

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QHBoxLayout, QLabel, QListWidget, QProgressBar,
    QPushButton, QSlider, QVBoxLayout, QWidget,
)

from sdrmod.modules import Module

PAS_KHZ = [5, 6.25, 8.33, 10, 12.5, 20, 25, 50, 100, 200]
MODES = ["NFM", "AM", "WFM", "USB", "LSB", "CW"]
HYSTERESIS = 3.0          # dB sous le seuil avant de considérer que le signal est parti
STABILISATION = 0.12      # s ignorées après chaque changement de fréquence
# Largeur du canal par mode (Hz) : sert à se caler au milieu d'une station plus large que le pas.
LARGEURS = {"WFM": 180_000, "NFM": 12_500, "AM": 8_000, "USB": 2_700, "LSB": 2_700, "CW": 500}


def bords_station(iq, centre, taux, f0, bw):
    """Bords (gauche, droite) d'une station dans le spectre d'un bloc IQ, autour de f0
    (là où le scanner l'a touchée) et dans une fenêtre de quelques largeurs de canal.
    None si rien de net."""
    n = 2048
    trames = min(8, len(iq) // n)
    if not trames:
        return None
    x = iq[: trames * n].reshape(trames, n) * np.hanning(n)
    p = (np.abs(np.fft.fftshift(np.fft.fft(x, axis=1), axes=1)) ** 2).mean(axis=0)
    res = taux / n
    w = max(1, round(bw / 20 / res))
    lisse = 10 * np.log10(np.convolve(p, np.ones(2 * w + 1) / (2 * w + 1), mode="same") + 1e-20)
    bruit = float(np.median(lisse))
    a = max(2, round((f0 - bw - centre) / res + n / 2))
    b = min(n - 3, round((f0 + 2.5 * bw - centre) / res + n / 2))
    if a > b:
        return None
    pic = a + int(np.argmax(lisse[a:b + 1]))
    if lisse[pic] < bruit + 6:
        return None
    seuil = max(bruit + 6, lisse[pic] - 30)
    g = d = pic
    while g > 2 and lisse[g - 1] > seuil:
        g -= 1
    while d < n - 3 and lisse[d + 1] > seuil:
        d += 1
    return centre + (g - n / 2) * res, centre + (d - n / 2) * res


class ScannerFrequences(Module):
    nom = "Scanner de fréquences"
    description = "Balaye une plage début–fin avec un pas, s'arrête sur un signal et reprend après un silence."

    def demarrer(self):
        self.fichier = self.ctx.dossier_donnees("scanner_frequences") / "reglages.json"
        try:
            r = json.loads(self.fichier.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            r = {}
        self.debut = float(r.get("debut", 144.0))       # MHz
        self.fin = float(r.get("fin", 146.0))           # MHz
        self.pas = float(r.get("pas", 12.5))            # kHz
        self.mode_scan = r.get("mode", "NFM")
        self.marge = float(r.get("marge", 10))           # dB au-dessus du bruit estimé
        self.duree = float(r.get("duree", 0.15))        # s par pas, après stabilisation
        self.reprise = float(r.get("reprise", 3.0))
        self.ignorees = set(int(f) for f in r.get("ignorees", []))
        self.boucle = bool(r.get("boucle", True))     # reprendre au début après la fin de la plage
        self.tours = 0
        self.fini = False
        self.niveaux: deque = deque(maxlen=200)          # (instant, dBFS), rempli par le thread DSP
        self.actif = False
        self.etat = "arret"
        self.f = None
        self.f_attendue = None
        self.t_accord = 0.0
        self.t_silence = None
        self.journal: list[str] = []
        self.k = -1
        self.k_sortie = None                             # où reprendre après une station centrée
        self.bloc_iq = None                              # bloc IQ lu pendant le centrage
        self.t_centrage = 0.0
        self.db_detection = 0.0
        self.mesures: deque = deque(maxlen=60)           # niveaux des derniers pas : leur médiane estime le bruit
        if self.ctx.mode() in MODES:
            self.mode_scan = self.ctx.mode()                 # on part du mode du récepteur
        self.abonner("niveau", lambda db: self.niveaux.append((time.monotonic(), db)))
        self.abonner("vfo", self._sur_vfo)                   # thread de l'interface
        self.abonner("iq", self._sur_iq)                     # thread DSP : un bloc, pendant le centrage seulement

    def _sur_iq(self, iq, centre, taux):
        if self.etat == "centrage" and self.bloc_iq is None and time.monotonic() > self.t_accord + STABILISATION:
            self.bloc_iq = (iq.copy(), float(centre), int(taux))

    def _sur_vfo(self, _frequence, mode):
        """Mode changé dans la barre du haut : le scanner suit."""
        if mode == self.mode_scan or mode not in MODES:
            return
        self.mode_scan = mode
        if hasattr(self, "c_mode"):
            self.c_mode.blockSignals(True)
            self.c_mode.setCurrentText(mode)
            self.c_mode.blockSignals(False)
            self._afficher()
        self.sauver()

    def sauver(self):
        try:
            self.fichier.write_text(json.dumps({
                "debut": self.debut, "fin": self.fin, "pas": self.pas, "mode": self.mode_scan,
                "marge": self.marge, "duree": self.duree, "reprise": self.reprise,
                "ignorees": sorted(self.ignorees), "boucle": self.boucle}), encoding="utf-8")
        except OSError:
            pass

    def frequences(self):
        """Nombre de pas et fréquence du pas k (Hz, arrondie)."""
        bas, haut = sorted((self.debut, self.fin))
        n = int((haut - bas) * 1e6 // (self.pas * 1e3)) + 1
        return n, lambda k: round(bas * 1e6 + k * self.pas * 1e3)

    # ------------------------------------------------------------ interface
    def creer_widget(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        form = QFormLayout()
        self.s_debut, self.s_fin = QDoubleSpinBox(), QDoubleSpinBox()
        for s, v in ((self.s_debut, self.debut), (self.s_fin, self.fin)):
            s.setRange(0.0, 6000.0)
            s.setDecimals(4)
            s.setSingleStep(0.1)
            s.setSuffix(" MHz")
            s.setValue(v)
            s.valueChanged.connect(self._lire_plage)
        form.addRow("Début", self.s_debut)
        form.addRow("Fin", self.s_fin)
        ligne = QHBoxLayout()
        self.c_pas = QComboBox()
        for p in PAS_KHZ:
            self.c_pas.addItem(f"{p:g} kHz", p)
        self.c_pas.setCurrentIndex(min(range(len(PAS_KHZ)), key=lambda i: abs(PAS_KHZ[i] - self.pas)))
        self.c_pas.currentIndexChanged.connect(self._lire_plage)
        self.c_mode = QComboBox()
        self.c_mode.addItems(MODES)
        self.c_mode.setCurrentText(self.mode_scan if self.mode_scan in MODES else "NFM")
        self.c_mode.currentTextChanged.connect(self._lire_plage)
        ligne.addWidget(self.c_pas)
        ligne.addWidget(self.c_mode)
        form.addRow("Pas, mode", ligne)
        for titre, nom, mini, maxi, echelle, texte in (
                ("Marge", "marge", 3, 30, 1, lambda v: f"+{v:.0f} dB"),
                ("Par pas", "duree", 5, 100, 100, lambda v: f"{v:.2f} s"),
                ("Reprise après", "reprise", 1, 15, 1, lambda v: f"{v:.0f} s")):
            form.addRow(titre, self._curseur(nom, mini, maxi, echelle, texte))
        lay.addLayout(form)

        boutons = QHBoxLayout()
        self.b_marche = QPushButton("▶ Scanner")
        self.b_marche.clicked.connect(self.basculer)
        b_passer = QPushButton("Passer")
        b_passer.clicked.connect(self.passer)
        b_ignorer = QPushButton("Ignorer")
        b_ignorer.setToolTip("Ne plus s'arrêter sur cette fréquence")
        b_ignorer.clicked.connect(self.ignorer)
        for b in (self.b_marche, b_passer, b_ignorer):
            boutons.addWidget(b)
        lay.addLayout(boutons)
        self.c_boucle = QCheckBox("En boucle")
        self.c_boucle.setToolTip("Reprendre au début après la fin de la plage ; sinon un seul passage")
        self.c_boucle.setChecked(self.boucle)
        self.c_boucle.toggled.connect(self._boucle)
        lay.addWidget(self.c_boucle)
        self.progression = QProgressBar()
        self.progression.setTextVisible(False)
        self.progression.setMaximumHeight(8)
        lay.addWidget(self.progression)
        self.statut = QLabel()
        self.statut.setWordWrap(True)
        lay.addWidget(self.statut)
        self.l_ignorees = QLabel()
        self.l_ignorees.setWordWrap(True)
        b_effacer = QPushButton("Oublier les fréquences ignorées")
        b_effacer.clicked.connect(self._effacer_ignorees)
        lay.addWidget(self.l_ignorees)
        lay.addWidget(b_effacer)
        lay.addWidget(QLabel("Derniers arrêts :"))
        self.l_journal = QListWidget()
        self.l_journal.setMaximumHeight(110)
        lay.addWidget(self.l_journal)
        self.minuterie = QTimer(w, interval=30, timeout=self._pas)
        self.minuterie.start()
        self._afficher()
        w.setMaximumHeight(w.sizeHint().height())
        return w

    def _curseur(self, nom, mini, maxi, echelle, texte):
        c = QSlider(Qt.Orientation.Horizontal)
        c.setRange(mini, maxi)
        c.setValue(round(getattr(self, nom) * echelle))
        l = QLabel(texte(getattr(self, nom)))
        l.setMinimumWidth(64)

        def changer(v):
            setattr(self, nom, v / echelle)
            l.setText(texte(v / echelle))
            self.sauver()
        c.valueChanged.connect(changer)
        boite = QWidget()
        h = QHBoxLayout(boite)
        h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(c, 1)
        h.addWidget(l)
        return boite

    def _lire_plage(self, *_):
        self.debut, self.fin = self.s_debut.value(), self.s_fin.value()
        self.pas = self.c_pas.currentData()
        self.mode_scan = self.c_mode.currentText()
        if self.mode_scan != self.ctx.mode():
            self.ctx.regler_mode(self.mode_scan)             # mode changé dans le scanner : le récepteur suit
        self.sauver()
        self._afficher()

    def _boucle(self, v):
        self.boucle = bool(v)
        self.sauver()

    def _effacer_ignorees(self):
        self.ignorees.clear()
        self.sauver()
        self._afficher()

    # ------------------------------------------------------------ balayage
    def basculer(self):
        if self.actif:
            self.actif, self.etat = False, "arret"
        else:
            n, _ = self.frequences()
            if self.debut == self.fin or n > 200_000:
                self.statut.setText("Plage invalide : début et fin différents, 200 000 pas au plus.")
                return
            self.actif = True
            self.mesures.clear()
            self.tours, self.fini = 1, False
            self.k_sortie = None
            self.ctx.regler_mode(self.mode_scan)
            self.k = -1
            self.suivant()
        self._afficher()

    def passer(self):
        if self.actif:
            self.suivant()

    def ignorer(self):
        if self.actif and self.f is not None:
            self.ignorees.add(self.f)
            self.sauver()
            self.suivant()

    def largeur(self):
        return LARGEURS.get(self.mode_scan, 12_500)

    def est_ignoree(self, f):
        """Une fréquence ignorée écarte toute la largeur du canal autour d'elle."""
        demi = max(self.largeur() / 2, self.pas * 500)
        return any(abs(f - g) < demi for g in self.ignorees)

    def suivant(self):
        n, f_de = self.frequences()
        if self.k_sortie is not None:          # reprise après une station : on la saute
            self.k = min(self.k_sortie, n) - 1
            self.k_sortie = None
        for _ in range(n):                    # pas suivant, en sautant les fréquences ignorées
            if self.k + 1 >= n:               # fin de la plage
                if not self.boucle:
                    self.actif, self.etat, self.fini = False, "arret", True
                    self._afficher()
                    return
                self.tours += 1
            self.k = (self.k + 1) % n
            if not self.est_ignoree(f_de(self.k)):
                break
        else:
            self.actif, self.etat = False, "arret"
            self._afficher()
            return
        self.f = f_de(self.k)
        self.ctx.regler_frequence(self.f)
        self.f_attendue = self.ctx.frequence()
        self.t_accord = time.monotonic()
        self.t_silence = None
        self.etat = "mesure"
        self._afficher()

    def _arreter_sur(self, k, db):
        _, f_de = self.frequences()
        if k != self.k:
            self.k, self.f = k, f_de(k)
            self.ctx.regler_frequence(self.f)
            self.f_attendue = self.ctx.frequence()
        self.etat, self.t_silence, self.t_accord = "signal", None, time.monotonic()
        self.journal.insert(0, f"{time.strftime('%H:%M:%S')}  {self.f / 1e6:.4f} MHz  {db:.0f} dBFS")
        del self.journal[30:]
        self.l_journal.clear()
        self.l_journal.addItems(self.journal)
        self._afficher()

    def bruit(self):
        """Médiane des derniers pas (presque tous vides) ; None tant qu'il y en a moins de 5."""
        if len(self.mesures) < 5:
            return None
        return sorted(self.mesures)[len(self.mesures) // 2]

    def _niveau_depuis(self, t):
        valeurs = [db for ti, db in list(self.niveaux) if ti >= t]
        return max(valeurs) if valeurs else None

    def _pas(self):
        if not self.actif:
            return
        maintenant = time.monotonic()
        if self.f_attendue is not None and abs(self.ctx.frequence() - self.f_attendue) > 1:
            self.actif, self.etat = False, "arret"
            self._afficher()
            self.statut.setText("Scanner arrêté : fréquence changée à la main.")
            return
        if self.etat == "mesure":
            if maintenant - self.t_accord < STABILISATION + self.duree:
                return
            db = self._niveau_depuis(self.t_accord + STABILISATION)
            if db is None:
                return                        # pas encore de mesure : on attend
            bruit = self.bruit()
            self.mesures.append(db)
            if bruit is not None and db > bruit + self.marge:
                pas_hz, bw = self.pas * 1e3, self.largeur()
                if bw > 2 * pas_hz:
                    # Canal plus large que le pas (WFM…) : on vient de toucher le flanc de la station.
                    # On lit le spectre pour trouver ses deux bords et se caler au milieu.
                    self.bloc_iq, self.db_detection, self.t_centrage = None, db, maintenant
                    self.etat = "centrage"
                    self._afficher()
                    return
                self._arreter_sur(self.k, db)
            else:
                self.suivant()
                return
        elif self.etat == "centrage":
            if self.bloc_iq is None:
                if maintenant - self.t_centrage > 1.0:         # pas de bloc IQ : on reste où on est
                    self._arreter_sur(self.k, self.db_detection)
                return
            iq, centre, taux = self.bloc_iq
            bw, pas_hz = self.largeur(), self.pas * 1e3
            bords = bords_station(iq, centre, taux, self.f, bw)
            if bords is None:
                self._arreter_sur(self.k, self.db_detection)
                return
            n, _ = self.frequences()
            bas = min(self.debut, self.fin) * 1e6
            kc = max(0, min(n - 1, round(((bords[0] + bords[1]) / 2 - bas) / pas_hz)))
            # à la reprise : premier pas dont le canal ne touche plus la station
            self.k_sortie = max(kc + 1, math.ceil((bords[1] + bw / 2 - bas) / pas_hz))
            self._arreter_sur(kc, self.db_detection)
            return
        elif self.etat == "signal":
            db = self._niveau_depuis(maintenant - 0.3)
            if db is None:
                pass
            elif db > (self.bruit() or -200.0) + self.marge - HYSTERESIS:
                self.t_silence = None
            elif self.t_silence is None:
                self.t_silence = maintenant
            elif maintenant - self.t_silence >= self.reprise:
                self.suivant()
                return
        self._afficher()

    def _afficher(self):
        self.b_marche.setText("■ Arrêter" if self.actif else "▶ Scanner")
        n, _ = self.frequences()
        self.progression.setRange(0, max(1, n - 1))
        self.progression.setValue(self.k if self.actif and self.k >= 0 else 0)
        self.l_ignorees.setText(
            "Ignorées : " + ", ".join(f"{f / 1e6:.4f}" for f in sorted(self.ignorees)[:12])
            + (" …" if len(self.ignorees) > 12 else "") if self.ignorees else "Aucune fréquence ignorée.")
        if not self.actif:
            bas, haut = sorted((self.debut, self.fin))
            self.statut.setText(f"{'Passage terminé' if self.fini else 'Arrêté'} · {bas:.4f} → {haut:.4f} MHz, {n} pas de {self.pas:g} kHz en {self.mode_scan}")
        elif self.etat == "centrage":
            self.statut.setText(f"Centrage sur la station… {self.f / 1e6:.4f} MHz")
        elif self.etat == "signal":
            reste = "" if self.t_silence is None else \
                f" · reprise dans {max(0.0, self.reprise - (time.monotonic() - self.t_silence)):.0f} s"
            self.statut.setText(f"● Signal sur {self.f / 1e6:.4f} MHz {self.mode_scan}{reste}")
        else:
            bruit = self.bruit()
            self.statut.setText(f"Balayage… {self.f / 1e6:.4f} MHz ({self.k + 1}/{n}, tour {self.tours})"
                                + (" · mesure du bruit" if bruit is None else f" · bruit {bruit:.0f} dBFS"))

    def arreter(self):
        self.actif = False

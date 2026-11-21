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
import time
from collections import deque

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QComboBox, QDoubleSpinBox, QFormLayout, QHBoxLayout, QLabel, QListWidget, QProgressBar,
    QPushButton, QSlider, QVBoxLayout, QWidget,
)

from sdrmod.modules import Module

PAS_KHZ = [5, 6.25, 8.33, 10, 12.5, 20, 25, 50, 100, 200]
MODES = ["NFM", "AM", "WFM", "USB", "LSB", "CW"]
HYSTERESIS = 3.0          # dB sous le seuil avant de considérer que le signal est parti
STABILISATION = 0.12      # s ignorées après chaque changement de fréquence


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
        self.niveaux: deque = deque(maxlen=200)          # (instant, dBFS), rempli par le thread DSP
        self.actif = False
        self.etat = "arret"
        self.f = None
        self.f_attendue = None
        self.t_accord = 0.0
        self.t_silence = None
        self.journal: list[str] = []
        self.mesures: deque = deque(maxlen=60)           # niveaux des derniers pas : leur médiane estime le bruit
        self.abonner("niveau", lambda db: self.niveaux.append((time.monotonic(), db)))

    def sauver(self):
        try:
            self.fichier.write_text(json.dumps({
                "debut": self.debut, "fin": self.fin, "pas": self.pas, "mode": self.mode_scan,
                "marge": self.marge, "duree": self.duree, "reprise": self.reprise,
                "ignorees": sorted(self.ignorees)}), encoding="utf-8")
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
        self.sauver()
        self._afficher()

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

    def suivant(self):
        n, f_de = self.frequences()
        for _ in range(n):                    # pas suivant, en sautant les fréquences ignorées
            self.k = (self.k + 1) % n
            if f_de(self.k) not in self.ignorees:
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
                self.etat = "signal"
                self.journal.insert(0, f"{time.strftime('%H:%M:%S')}  {self.f / 1e6:.4f} MHz  {db:.0f} dBFS")
                del self.journal[30:]
                self.l_journal.clear()
                self.l_journal.addItems(self.journal)
            else:
                self.suivant()
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
            self.statut.setText(f"Arrêté · {bas:.4f} → {haut:.4f} MHz, {n} pas de {self.pas:g} kHz en {self.mode_scan}")
        elif self.etat == "signal":
            reste = "" if self.t_silence is None else \
                f" · reprise dans {max(0.0, self.reprise - (time.monotonic() - self.t_silence)):.0f} s"
            self.statut.setText(f"● Signal sur {self.f / 1e6:.4f} MHz {self.mode_scan}{reste}")
        else:
            bruit = self.bruit()
            self.statut.setText(f"Balayage… {self.f / 1e6:.4f} MHz ({self.k + 1}/{n})"
                                + (" · mesure du bruit" if bruit is None else f" · bruit {bruit:.0f} dBFS"))

    def arreter(self):
        self.actif = False

# SPDX-License-Identifier: MIT
"""Scanner de mémoires pour SDR Modulaire.

Parcourt les fréquences du module « Mémoires » (même fichier, mêmes mode et fréquence),
s'arrête dès qu'un signal dépasse le seuil, reste tant qu'il est présent, puis reprend
le balayage après un délai de silence. Chaque arrêt est noté dans un petit journal.

Module à télécharger : placez ce fichier dans ~/.config/sdr-modulaire/modules/.
Aucune dépendance en plus de SDR Modulaire.
"""

from __future__ import annotations

import json
import time
from collections import deque

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QFormLayout, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton, QSlider,
    QVBoxLayout, QWidget,
)

from sdrmod.modules import Module

# Même liste par défaut que le module Mémoires, tant qu'aucune mémoire n'a été enregistrée.
MEMOIRES_DEFAUT = [
    {"nom": "Appel FM 2 m", "frequence": 145_500_000, "mode": "NFM"},
    {"nom": "APRS", "frequence": 144_800_000, "mode": "NFM"},
    {"nom": "Appel FM 70 cm", "frequence": 433_500_000, "mode": "NFM"},
]
HYSTERESIS = 3.0          # dB sous le seuil avant de considérer que le signal est parti
STABILISATION = 0.15      # s ignorées après chaque changement de fréquence


class ScannerMemoires(Module):
    nom = "Scanner de mémoires"
    description = "Parcourt les mémoires, s'arrête sur un signal et reprend quand il se tait."

    def demarrer(self):
        self.fichier_memoires = self.ctx.dossier_donnees("memoires") / "memoires.json"
        self.fichier_reglages = self.ctx.dossier_donnees("scanner_memoires") / "reglages.json"
        try:
            r = json.loads(self.fichier_reglages.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            r = {}
        self.seuil = float(r.get("seuil", -60))
        self.duree = float(r.get("duree", 0.35))      # temps passé sur chaque canal (s)
        self.reprise = float(r.get("reprise", 3.0))   # silence avant de reprendre (s)
        self.exclues = set(r.get("exclues", []))
        self.memoires = []
        self.niveaux: deque = deque(maxlen=200)       # (instant, dBFS), rempli par le thread DSP
        self.actif = False
        self.etat = "arret"        # arret, mesure, arret_signal
        self.indice = -1
        self.t_accord = 0.0
        self.t_silence = None
        self.f_attendue = None
        self.journal: list[str] = []
        self.charger_memoires()
        self.abonner("niveau", self._sur_niveau)

    # thread DSP : on note seulement
    def _sur_niveau(self, db):
        self.niveaux.append((time.monotonic(), db))

    def charger_memoires(self):
        try:
            liste = json.loads(self.fichier_memoires.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            liste = MEMOIRES_DEFAUT
        self.memoires = sorted(liste, key=lambda m: m["frequence"])

    def sauver_reglages(self):
        try:
            self.fichier_reglages.write_text(json.dumps({
                "seuil": self.seuil, "duree": self.duree, "reprise": self.reprise,
                "exclues": sorted(self.exclues)}), encoding="utf-8")
        except OSError:
            pass

    @staticmethod
    def cle(m):
        return f"{m['frequence']}/{m['mode']}"

    def canaux(self):
        return [i for i, m in enumerate(self.memoires) if self.cle(m) not in self.exclues]

    # ------------------------------------------------------------ interface
    def creer_widget(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        boutons = QHBoxLayout()
        self.b_marche = QPushButton("▶ Scanner")
        self.b_marche.clicked.connect(self.basculer)
        b_passer = QPushButton("Passer")
        b_passer.setToolTip("Quitter le canal actuel et continuer le balayage")
        b_passer.clicked.connect(self.passer)
        b_relire = QPushButton("Relire")
        b_relire.setToolTip("Relire la liste du module Mémoires")
        b_relire.clicked.connect(self._relire)
        for b in (self.b_marche, b_passer, b_relire):
            boutons.addWidget(b)
        lay.addLayout(boutons)

        self.statut = QLabel()
        self.statut.setWordWrap(True)
        lay.addWidget(self.statut)

        form = QFormLayout()
        self.c_seuil, self.l_seuil = self._curseur(-110, -20, round(self.seuil), lambda v: f"{v} dBFS")
        self.c_duree, self.l_duree = self._curseur(2, 20, round(self.duree * 10), lambda v: f"{v / 10:.1f} s")
        self.c_reprise, self.l_reprise = self._curseur(1, 15, round(self.reprise), lambda v: f"{v} s")
        self.c_seuil.valueChanged.connect(lambda v: self._regler("seuil", float(v)))
        self.c_duree.valueChanged.connect(lambda v: self._regler("duree", v / 10))
        self.c_reprise.valueChanged.connect(lambda v: self._regler("reprise", float(v)))
        for titre, c, l in (("Seuil", self.c_seuil, self.l_seuil), ("Par canal", self.c_duree, self.l_duree),
                            ("Reprise après", self.c_reprise, self.l_reprise)):
            ligne = QHBoxLayout()
            ligne.addWidget(c, 1)
            ligne.addWidget(l)
            form.addRow(titre, ligne)
        lay.addLayout(form)

        self.liste = QListWidget()
        self.liste.setToolTip("Décochez une mémoire pour qu'elle soit sautée")
        self.liste.setMaximumHeight(150)
        self.liste.itemChanged.connect(self._coche)
        lay.addWidget(self.liste)
        lay.addWidget(QLabel("Derniers arrêts :"))
        self.l_journal = QListWidget()
        self.l_journal.setMaximumHeight(100)
        lay.addWidget(self.l_journal)
        self._remplir()

        self.minuterie = QTimer(w, interval=50, timeout=self._pas)
        self.minuterie.start()
        self._afficher()
        w.setMaximumHeight(w.sizeHint().height())
        return w

    def _curseur(self, mini, maxi, valeur, texte):
        c = QSlider(Qt.Orientation.Horizontal)
        c.setRange(mini, maxi)
        c.setValue(valeur)
        l = QLabel(texte(valeur))
        l.setMinimumWidth(64)
        c.valueChanged.connect(lambda v: l.setText(texte(v)))
        return c, l

    def _regler(self, nom, valeur):
        setattr(self, nom, valeur)
        self.sauver_reglages()

    def _remplir(self):
        self.liste.blockSignals(True)
        self.liste.clear()
        for i, m in enumerate(self.memoires):
            item = QListWidgetItem(f"{m['frequence'] / 1e6:10.4f}  {m['mode']:4s} {m['nom']}")
            item.setData(Qt.ItemDataRole.UserRole, i)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked if self.cle(m) in self.exclues else Qt.CheckState.Checked)
            self.liste.addItem(item)
        self.liste.blockSignals(False)

    def _coche(self, item):
        m = self.memoires[item.data(Qt.ItemDataRole.UserRole)]
        if item.checkState() == Qt.CheckState.Checked:
            self.exclues.discard(self.cle(m))
        else:
            self.exclues.add(self.cle(m))
        self.sauver_reglages()

    def _relire(self):
        self.charger_memoires()
        self.indice = -1
        self._remplir()

    # ------------------------------------------------------------ balayage
    def basculer(self):
        if self.actif:
            self.actif, self.etat = False, "arret"
        else:
            if not self.canaux():
                self.statut.setText("Aucune mémoire à scanner : cochez-en au moins une.")
                return
            self.actif = True
            self.suivant()
        self._afficher()

    def passer(self):
        if self.actif:
            self.suivant()

    def suivant(self):
        canaux = self.canaux()
        if not canaux:
            self.actif, self.etat = False, "arret"
            return
        apres = [i for i in canaux if i > self.indice]
        self.indice = apres[0] if apres else canaux[0]
        m = self.memoires[self.indice]
        self.ctx.regler_mode(m["mode"])
        self.ctx.regler_frequence(m["frequence"])
        self.f_attendue = self.ctx.frequence()
        self.t_accord = time.monotonic()
        self.t_silence = None
        self.etat = "mesure"
        self._afficher()

    def _niveau_recent(self, depuis):
        valeurs = [db for t, db in list(self.niveaux) if t >= depuis]
        return max(valeurs) if valeurs else None

    def _pas(self):
        if not self.actif:
            return
        maintenant = time.monotonic()
        # Fréquence changée à la main : on rend la main à l'utilisateur.
        if self.f_attendue is not None and abs(self.ctx.frequence() - self.f_attendue) > 1:
            self.actif, self.etat = False, "arret"
            self.statut.setText("Scanner arrêté : fréquence changée à la main.")
            self._afficher(garder_statut=True)
            return
        if self.etat == "mesure":
            if maintenant - self.t_accord < STABILISATION + self.duree:
                return
            db = self._niveau_recent(self.t_accord + STABILISATION)
            if db is not None and db > self.seuil:
                self.etat = "arret_signal"
                m = self.memoires[self.indice]
                self.journal.insert(0, f"{time.strftime('%H:%M:%S')}  {m['frequence'] / 1e6:.4f}  {m['nom']}  {db:.0f} dBFS")
                del self.journal[20:]
                self.l_journal.clear()
                self.l_journal.addItems(self.journal)
            else:
                self.suivant()
        elif self.etat == "arret_signal":
            db = self._niveau_recent(maintenant - 0.3)
            if db is None:
                pass                                   # pas de mesure récente : on ne conclut rien
            elif db > self.seuil - HYSTERESIS:
                self.t_silence = None
            elif self.t_silence is None:
                self.t_silence = maintenant
            elif maintenant - self.t_silence >= self.reprise:
                self.suivant()
        self._afficher()

    def _afficher(self, garder_statut=False):
        self.b_marche.setText("■ Arrêter" if self.actif else "▶ Scanner")
        for k in range(self.liste.count()):
            item = self.liste.item(k)
            f = item.font()
            f.setBold(self.actif and item.data(Qt.ItemDataRole.UserRole) == self.indice)
            item.setFont(f)
        if garder_statut:
            return
        if not self.actif:
            self.statut.setText(f"Arrêté · {len(self.canaux())} mémoire(s) à scanner")
            return
        m = self.memoires[self.indice]
        texte = f"{m['frequence'] / 1e6:.4f} MHz {m['mode']} · {m['nom']}"
        if self.etat == "arret_signal":
            reste = "" if self.t_silence is None else f" · reprise dans {max(0, self.reprise - (time.monotonic() - self.t_silence)):.0f} s"
            self.statut.setText(f"● Signal sur {texte}{reste}")
        else:
            self.statut.setText(f"Balayage… {texte}")

    def arreter(self):
        self.actif = False

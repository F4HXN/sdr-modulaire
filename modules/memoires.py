"""Mémoires : liste de fréquences favorites (double-clic pour rappeler).

Montre un module qui pilote le récepteur via le contexte (fréquence + mode).
"""

import json

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QHBoxLayout, QInputDialog, QListWidget, QListWidgetItem,
                               QPushButton, QVBoxLayout, QWidget)

from sdrmod.modules import Module


class Memoires(Module):
    nom = "Mémoires"
    description = "Fréquences favorites : ajouter la fréquence courante, rappeler par double-clic."

    def demarrer(self) -> None:
        self.fichier = self.ctx.dossier_donnees("memoires") / "memoires.json"
        try:
            self.memoires = json.loads(self.fichier.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            self.memoires = [
                {"nom": "Appel FM 2 m", "frequence": 145_500_000, "mode": "NFM"},
                {"nom": "APRS", "frequence": 144_800_000, "mode": "NFM"},
                {"nom": "Appel FM 70 cm", "frequence": 433_500_000, "mode": "NFM"},
            ]

    def creer_widget(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        self.liste = QListWidget()
        self.liste.itemDoubleClicked.connect(self._rappeler)
        lay.addWidget(self.liste, 1)
        boutons = QHBoxLayout()
        b_ajout = QPushButton("Ajouter")
        b_ajout.clicked.connect(self._ajouter)
        b_suppr = QPushButton("Supprimer")
        b_suppr.clicked.connect(self._supprimer)
        boutons.addWidget(b_ajout)
        boutons.addWidget(b_suppr)
        lay.addLayout(boutons)
        self._remplir()
        return w

    def _remplir(self) -> None:
        self.liste.clear()
        for m in sorted(self.memoires, key=lambda m: m["frequence"]):
            item = QListWidgetItem(f"{m['frequence'] / 1e6:11.5f}  {m['mode']:4s}  {m['nom']}")
            item.setData(Qt.UserRole, m)
            self.liste.addItem(item)

    def _rappeler(self, item: QListWidgetItem) -> None:
        m = item.data(Qt.UserRole)
        self.ctx.regler_mode(m["mode"])
        self.ctx.regler_frequence(m["frequence"])

    def _ajouter(self) -> None:
        nom, ok = QInputDialog.getText(self.liste, "Mémoire", "Nom :")
        if ok:
            self.memoires.append({"nom": nom or "—", "frequence": round(self.ctx.frequence()),
                                  "mode": self.ctx.mode()})
            self._sauver()
            self._remplir()

    def _supprimer(self) -> None:
        item = self.liste.currentItem()
        if item:
            self.memoires.remove(item.data(Qt.UserRole))
            self._sauver()
            self._remplir()

    def _sauver(self) -> None:
        self.fichier.write_text(json.dumps(self.memoires, indent=2, ensure_ascii=False), encoding="utf-8")

"""Fenêtre de gestion des modules : cocher pour activer, décocher pour retirer."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QListWidget,
                               QListWidgetItem, QPushButton, QVBoxLayout)


class DialogueModules(QDialog):
    def __init__(self, fenetre) -> None:
        super().__init__(fenetre)
        self.fenetre = fenetre
        self.setWindowTitle("Modules")
        self.resize(520, 380)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Coche un module pour l'ajouter, décoche-le pour le retirer (à chaud)."))
        self.liste = QListWidget()
        lay.addWidget(self.liste, 1)
        self.detail = QLabel()
        self.detail.setWordWrap(True)
        lay.addWidget(self.detail)

        boutons = QHBoxLayout()
        b_redec = QPushButton("Rescanner les dossiers")
        b_redec.clicked.connect(self._rescanner)
        b_rech = QPushButton("Recharger le module")
        b_rech.clicked.connect(self._recharger)
        boutons.addWidget(b_redec)
        boutons.addWidget(b_rech)
        boutons.addStretch(1)
        fermer = QDialogButtonBox(QDialogButtonBox.Close)
        fermer.rejected.connect(self.reject)
        boutons.addWidget(fermer)
        lay.addLayout(boutons)

        self.liste.itemChanged.connect(self._bascule)
        self.liste.currentItemChanged.connect(self._afficher)
        self._remplir()

    def _remplir(self) -> None:
        g = self.fenetre.modules
        self.liste.blockSignals(True)
        self.liste.clear()
        for nom, (classe, chemin) in sorted(g.classes.items()):
            item = QListWidgetItem(nom)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if nom in g.actifs else Qt.Unchecked)
            item.setData(Qt.UserRole, f"{classe.description}\n\nFichier : {chemin}")
            self.liste.addItem(item)
        for fichier, err in g.erreurs.items():
            item = QListWidgetItem(f"⚠ {fichier}")
            item.setFlags(Qt.ItemIsEnabled)
            item.setData(Qt.UserRole, f"Erreur au chargement : {err}")
            self.liste.addItem(item)
        self.liste.blockSignals(False)

    def _bascule(self, item: QListWidgetItem) -> None:
        nom = item.text()
        if item.checkState() == Qt.Checked:
            if not self.fenetre.activer_module(nom):
                self.liste.blockSignals(True)
                item.setCheckState(Qt.Unchecked)
                self.liste.blockSignals(False)
        else:
            self.fenetre.desactiver_module(nom)

    def _afficher(self, item) -> None:
        self.detail.setText(item.data(Qt.UserRole) if item else "")

    def _rescanner(self) -> None:
        self.fenetre.modules.decouvrir()
        self._remplir()

    def _recharger(self) -> None:
        item = self.liste.currentItem()
        if item and item.text() in self.fenetre.modules.classes:
            self.fenetre.recharger_module(item.text())
            self._remplir()

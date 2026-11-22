"""Ajout de modules : catalogue de rtl-sdr.fr (installation, mises à jour) ou fichier local.

Les modules sont copiés dans ~/.config/sdr-modulaire/modules/, après vérification :
catalogue signé en Ed25519 par l'auteur (sdrmod/signature.py), modules téléchargés
uniquement depuis https://rtl-sdr.fr/, nom de fichier simple, taille raisonnable,
empreinte SHA-256 du catalogue, syntaxe Python.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import threading
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (QAbstractItemView, QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout,
                               QHeaderView, QLabel, QMessageBox, QPushButton, QTableWidget,
                               QTableWidgetItem, QVBoxLayout)

from ..modules import DOSSIERS_MODULES
from ..signature import verifier_catalogue

CATALOGUE = "https://rtl-sdr.fr/catalogue-modules.json"
SIGNATURE = CATALOGUE + ".sig"                  # signature Ed25519 du catalogue, en base64
BASE_AUTORISEE = "https://rtl-sdr.fr/"          # seule origine acceptée pour les modules
DOSSIER_FOURNIS = DOSSIERS_MODULES[0]           # modules livrés avec l'application
DOSSIER_UTILISATEUR = DOSSIERS_MODULES[-1]      # ~/.config/sdr-modulaire/modules
NOM_VALIDE = re.compile(r"^[a-z0-9][a-z0-9_]*\.py$")
TAILLE_MAX = 2_000_000
ENTETES = {"User-Agent": "SDR-Modulaire (gestionnaire de modules)"}


def telecharger(url: str) -> bytes:
    if not url.startswith("https://"):
        raise ValueError("adresse non sécurisée")
    with urllib.request.urlopen(urllib.request.Request(url, headers=ENTETES), timeout=15) as r:
        donnees = r.read(TAILLE_MAX + 1)
    if len(donnees) > TAILLE_MAX:
        raise ValueError("fichier trop gros")
    return donnees


def verifier_base(base) -> str:
    """Rend la base de téléchargement du catalogue, ou lève ValueError si elle sort de rtl-sdr.fr."""
    if not isinstance(base, str):
        raise ValueError("base du catalogue absente")
    morceaux = urlsplit(base)
    if (morceaux.scheme != "https" or morceaux.netloc != "rtl-sdr.fr" or not base.startswith(BASE_AUTORISEE)
            or not base.endswith("/") or ".." in morceaux.path or morceaux.query or morceaux.fragment):
        raise ValueError(f"base du catalogue refusée ({base}) : seule {BASE_AUTORISEE} est acceptée")
    return base


def lire_catalogue_signe(donnees: bytes, signature: bytes) -> tuple[list[dict], str]:
    """Vérifie la signature puis rend (modules, base). Lève ValueError si le catalogue est refusé."""
    verifier_catalogue(donnees, signature)
    cat = json.loads(donnees.decode("utf-8"))
    base = verifier_base(cat.get("base", BASE_AUTORISEE))
    modules = [m for m in cat.get("modules", [])
               if isinstance(m, dict) and NOM_VALIDE.match(str(m.get("fichier", "")))
               and re.fullmatch(r"[0-9a-f]{64}", str(m.get("sha256", "")))]
    return modules, base


def verifier_module(nom_fichier: str, donnees: bytes) -> None:
    """Lève ValueError si le fichier ne peut pas être un module installable."""
    if not NOM_VALIDE.match(nom_fichier):
        raise ValueError(f"nom de fichier refusé : {nom_fichier}")
    if len(donnees) > TAILLE_MAX:
        raise ValueError("fichier trop gros")
    try:
        texte = donnees.decode("utf-8")
        compile(texte, nom_fichier, "exec")
    except (UnicodeDecodeError, SyntaxError) as exc:
        raise ValueError(f"ce n'est pas un fichier Python valide ({exc})") from exc
    if "sdrmod.modules" not in texte or "Module" not in texte:
        raise ValueError("ce fichier ne ressemble pas à un module SDR Modulaire")


def installer(nom_fichier: str, donnees: bytes) -> Path:
    verifier_module(nom_fichier, donnees)
    DOSSIER_UTILISATEUR.mkdir(parents=True, exist_ok=True)
    cible = DOSSIER_UTILISATEUR / nom_fichier
    temporaire = cible.with_suffix(".py.part")
    temporaire.write_bytes(donnees)
    temporaire.replace(cible)                    # remplacement d'un coup
    return cible


def etat(entree: dict) -> str:
    local = DOSSIER_UTILISATEUR / entree["fichier"]
    if local.is_file():
        if hashlib.sha256(local.read_bytes()).hexdigest() == entree["sha256"]:
            return "installé"
        return "mise à jour"
    if (DOSSIER_FOURNIS / entree["fichier"]).is_file():
        return "fourni"
    return "absent"


class _Signaux(QObject):
    catalogue = Signal(object, str)          # liste des modules, erreur
    fichier = Signal(str, object, str)       # nom de fichier, données, erreur


class DialogueAjoutModules(QDialog):
    LIBELLES = {"installé": "✓ Installé", "mise à jour": "Mise à jour disponible",
                "fourni": "Fourni avec l'appli", "absent": "Non installé"}

    def __init__(self, fenetre, parent=None) -> None:
        super().__init__(parent or fenetre)
        self.fenetre = fenetre
        self.entrees: list[dict] = []
        self.base = BASE_AUTORISEE
        self.setWindowTitle("Ajouter des modules")
        self.resize(760, 420)
        self.signaux = _Signaux()
        self.signaux.catalogue.connect(self._catalogue_recu)
        self.signaux.fichier.connect(self._fichier_recu)

        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Modules proposés sur <b>rtl-sdr.fr</b>. Ils sont installés dans "
                             f"<code>{str(DOSSIER_UTILISATEUR).replace(str(Path.home()), '~', 1)}</code>."))
        self.tableau = QTableWidget(0, 4)
        self.tableau.setHorizontalHeaderLabels(["Module", "Description", "État", ""])
        self.tableau.verticalHeader().hide()
        self.tableau.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tableau.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.tableau.setWordWrap(True)
        en_tete = self.tableau.horizontalHeader()
        en_tete.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        en_tete.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        en_tete.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        en_tete.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        lay.addWidget(self.tableau, 1)
        self.statut = QLabel("Lecture du catalogue…")
        self.statut.setWordWrap(True)
        lay.addWidget(self.statut)
        avert = QLabel("Un module est du code Python exécuté avec vos droits : n'installez que des modules "
                       "de confiance.")
        avert.setWordWrap(True)
        avert.setStyleSheet("color: gray")
        lay.addWidget(avert)

        boutons = QHBoxLayout()
        self.b_actualiser = QPushButton("Actualiser")
        self.b_actualiser.clicked.connect(self.lire_catalogue)
        b_fichier = QPushButton("Installer depuis un fichier…")
        b_fichier.clicked.connect(self._depuis_fichier)
        b_dossier = QPushButton("Ouvrir le dossier")
        b_dossier.clicked.connect(self._ouvrir_dossier)
        for b in (self.b_actualiser, b_fichier, b_dossier):
            boutons.addWidget(b)
        boutons.addStretch(1)
        fermer = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        fermer.rejected.connect(self.reject)
        boutons.addWidget(fermer)
        lay.addLayout(boutons)
        self.lire_catalogue()

    # ------------------------------------------------------------ catalogue
    def lire_catalogue(self) -> None:
        self.b_actualiser.setEnabled(False)
        self.statut.setText("Lecture du catalogue sur rtl-sdr.fr…")

        def travail():
            try:
                donnees, signature = telecharger(CATALOGUE), telecharger(SIGNATURE)
                modules, self.base = lire_catalogue_signe(donnees, signature)
                self.signaux.catalogue.emit(modules, "")
            except Exception as exc:   # réseau, JSON…
                self.signaux.catalogue.emit([], str(exc))
        threading.Thread(target=travail, daemon=True).start()

    def _catalogue_recu(self, modules, erreur) -> None:
        self.b_actualiser.setEnabled(True)
        if erreur:
            self.entrees = []                    # rien d'installable sans catalogue authentifié
            self._remplir()
            self.statut.setText(f"Catalogue indisponible ({erreur}). Vous pouvez installer un fichier "
                                "téléchargé à la main avec « Installer depuis un fichier… ».")
            return
        self.entrees = modules
        self._remplir()
        self.statut.setText(f"{len(modules)} module(s) au catalogue (signature vérifiée).")

    def _remplir(self) -> None:
        for i in range(self.tableau.rowCount()):     # les anciens boutons sont supprimés
            w = self.tableau.cellWidget(i, 3)
            if w:
                w.hide()
                self.tableau.removeCellWidget(i, 3)
        self.tableau.setRowCount(0)
        self.tableau.setRowCount(len(self.entrees))
        for i, e in enumerate(self.entrees):
            et = etat(e)
            for j, texte in enumerate((e.get("nom") or e["fichier"], e.get("description", ""), self.LIBELLES[et])):
                item = QTableWidgetItem(texte)
                item.setToolTip(f"{e['fichier']} · {e.get('taille', 0) / 1000:.0f} ko")
                self.tableau.setItem(i, j, item)
            texte_bouton = {"absent": "Installer", "mise à jour": "Mettre à jour",
                            "installé": "Réinstaller", "fourni": "Installer ma copie"}[et]
            b = QPushButton(texte_bouton)
            b.clicked.connect(lambda _=False, e=e: self._installer(e))
            self.tableau.setCellWidget(i, 3, b)
        self.tableau.resizeRowsToContents()

    def _installer(self, e: dict) -> None:
        self.statut.setText(f"Téléchargement de {e['fichier']}…")
        self.tableau.setEnabled(False)

        def travail():
            try:
                donnees = telecharger(verifier_base(self.base) + e["fichier"])
                if hashlib.sha256(donnees).hexdigest() != e["sha256"]:
                    raise ValueError("empreinte différente de celle du catalogue (fichier modifié ou incomplet)")
                self.signaux.fichier.emit(e["fichier"], donnees, "")
            except Exception as exc:
                self.signaux.fichier.emit(e["fichier"], None, str(exc))
        threading.Thread(target=travail, daemon=True).start()

    def _fichier_recu(self, nom_fichier, donnees, erreur) -> None:
        self.tableau.setEnabled(True)
        if erreur:
            self.statut.setText(f"{nom_fichier} : échec ({erreur}).")
            return
        self._poser(nom_fichier, donnees)

    # ------------------------------------------------------------ fichier local
    def _depuis_fichier(self) -> None:
        chemin, _ = QFileDialog.getOpenFileName(self, "Choisir un module", str(Path.home() / "Téléchargements"),
                                                "Modules Python (*.py)")
        if not chemin:
            return
        source = Path(chemin)
        if source.resolve().parent == DOSSIER_UTILISATEUR.resolve():
            self.statut.setText("Ce fichier est déjà dans le dossier des modules.")
            return
        cible = DOSSIER_UTILISATEUR / source.name
        if cible.exists() and QMessageBox.question(
                self, "Remplacer ?", f"{source.name} existe déjà dans le dossier des modules. Le remplacer ?") \
                != QMessageBox.StandardButton.Yes:
            return
        try:
            self._poser(source.name, source.read_bytes())
        except OSError as exc:
            self.statut.setText(f"Lecture impossible : {exc}")

    def _poser(self, nom_fichier: str, donnees: bytes) -> None:
        try:
            installer(nom_fichier, donnees)
        except (ValueError, OSError) as exc:
            self.statut.setText(f"{nom_fichier} non installé : {exc}")
            return
        g = self.fenetre.modules
        actifs_avant = set(g.actifs)
        g.decouvrir()
        # un module déjà actif est rechargé pour prendre la nouvelle version
        nouveaux = [nom for nom, (_, chemin) in g.classes.items() if chemin.name == nom_fichier]
        for nom in nouveaux:
            if nom in actifs_avant:
                self.fenetre.recharger_module(nom)
        noms = ", ".join(nouveaux) or nom_fichier
        if nom_fichier in g.erreurs:
            self.statut.setText(f"{nom_fichier} installé, mais il ne se charge pas : {g.erreurs[nom_fichier]}")
        else:
            self.statut.setText(f"✓ {noms} installé. Cochez-le dans la liste des modules pour l'activer.")
        if self.entrees:
            self._remplir()

    def _ouvrir_dossier(self) -> None:
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices
        DOSSIER_UTILISATEUR.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(DOSSIER_UTILISATEUR)))

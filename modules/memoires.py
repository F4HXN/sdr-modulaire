"""Mémoires : gestion des fréquences favorites.

Tableau trié par fréquence (CH = même numéro que le scanner de mémoires), recherche, groupes,
ajout / modification / suppression, double-clic pour rappeler. Sauvegarde automatique dans
memoires.json à chaque changement ; export et import en CSV compatible CHIRP ou en JSON.
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
                               QFormLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox,
                               QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from sdrmod.modules import Module

MEMOIRES_DEFAUT = [
    {"nom": "Appel FM 2 m", "frequence": 145_500_000, "mode": "NFM", "groupe": ""},
    {"nom": "APRS", "frequence": 144_800_000, "mode": "NFM", "groupe": ""},
    {"nom": "Appel FM 70 cm", "frequence": 433_500_000, "mode": "NFM", "groupe": ""},
]
MODES_DEFAUT = ["AM", "NFM", "WFM", "USB", "LSB", "CW"]
TOUS = "Tous les groupes"

# CSV de CHIRP : colonnes de son format, modes CHIRP ↔ modes du récepteur.
COLONNES_CHIRP = ["Location", "Name", "Frequency", "Duplex", "Offset", "Tone", "rToneFreq", "cToneFreq",
                  "DtcsCode", "DtcsPolarity", "RxDtcsCode", "CrossMode", "Mode", "TStep", "Skip", "Power",
                  "Comment", "URCALL", "RPT1CALL", "RPT2CALL", "DVCODE"]
VERS_CHIRP = {"NFM": "FM", "WFM": "WFM", "AM": "AM", "USB": "USB", "LSB": "LSB", "CW": "CW"}
DEPUIS_CHIRP = {"FM": "NFM", "NFM": "NFM", "WFM": "WFM", "AM": "AM", "NAM": "AM", "USB": "USB",
                "LSB": "LSB", "CW": "CW", "CWR": "CW"}


def lire_frequence(texte) -> int | None:
    """« 145.5 », « 145,500 » (MHz) ou « 145500000 » (Hz) → Hz ; None si illisible."""
    try:
        v = float(str(texte).strip().replace(",", ".").replace(" ", ""))
    except ValueError:
        return None
    if not v > 0:
        return None
    return round(v * 1e6) if v < 100_000 else round(v)


def normaliser(m: dict, modes: list[str]) -> dict | None:
    """Mémoire venue d'un fichier → forme interne, ou None si inutilisable."""
    f = m.get("frequence", m.get("f"))
    if isinstance(f, bool):
        return None
    f = lire_frequence(f) if isinstance(f, str) else (round(f) if isinstance(f, (int, float)) and f > 0 else None)
    if f is None:
        return None
    mode = str(m.get("mode", "NFM")).upper()
    return {"nom": str(m.get("nom", "") or "—").strip(), "frequence": f,
            "mode": mode if mode in modes else "NFM", "groupe": str(m.get("groupe", "") or "").strip()}


def vers_csv_chirp(memoires: list[dict]) -> str:
    sortie = io.StringIO()
    w = csv.writer(sortie, lineterminator="\r\n")
    w.writerow(COLONNES_CHIRP)
    for i, m in enumerate(sorted(memoires, key=lambda m: m["frequence"])):
        ligne = dict.fromkeys(COLONNES_CHIRP, "")
        ligne.update(Location=i, Name=m["nom"], Frequency=f"{m['frequence'] / 1e6:.6f}",
                     Offset="0.000000", rToneFreq="88.5", cToneFreq="88.5", DtcsCode="023",
                     DtcsPolarity="NN", RxDtcsCode="023", CrossMode="Tone->Tone",
                     Mode=VERS_CHIRP.get(m["mode"], "FM"), TStep="5.00", Power="50W",   # CHIRP refuse une puissance vide
                     Comment=m.get("groupe", ""))
        w.writerow([ligne[c] for c in COLONNES_CHIRP])
    return sortie.getvalue()


def depuis_csv(texte: str, modes: list[str]) -> list[dict]:
    """CSV de CHIRP (Name, Frequency, Mode, Comment) ou en français (Nom, Fréquence, Mode, Groupe)."""
    try:
        dialecte = csv.Sniffer().sniff(texte[:2000], delimiters=",;\t")
    except csv.Error:
        dialecte = csv.excel
    trouves = []
    for l in csv.DictReader(io.StringIO(texte), dialect=dialecte):
        l = {(k or "").strip().lower(): (v or "").strip() for k, v in l.items() if isinstance(v, str) or v is None}
        f = next((l[k] for k in ("frequency", "fréquence", "frequence", "fréquence (mhz)", "mhz") if l.get(k)), "")
        mode = l.get("mode", "").upper()
        m = normaliser({"nom": l.get("name") or l.get("nom") or "", "frequence": f,
                        "mode": DEPUIS_CHIRP.get(mode, mode or "NFM"),
                        "groupe": l.get("comment") or l.get("groupe") or ""}, modes)
        if m:
            trouves.append(m)
    return trouves


class DialogueMemoire(QDialog):
    def __init__(self, parent, titre, m: dict, modes: list[str], groupes: list[str], f_actuelle, mode_actuel):
        super().__init__(parent)
        self.setWindowTitle(titre)
        form = QFormLayout(self)
        self.nom = QLineEdit(m["nom"])
        self.freq = QLineEdit(f"{m['frequence'] / 1e6:.6f}".rstrip("0").rstrip("."))
        self.freq.setPlaceholderText("MHz, ex. 145.500")
        b_actuelle = QPushButton("Fréquence actuelle")
        b_actuelle.setToolTip("Reprendre la fréquence et le mode du récepteur")
        ligne = QHBoxLayout()
        ligne.addWidget(self.freq, 1)
        ligne.addWidget(b_actuelle)
        self.mode = QComboBox()
        self.mode.addItems(modes)
        self.mode.setCurrentText(m["mode"])
        self.groupe = QComboBox()
        self.groupe.setEditable(True)
        self.groupe.addItems([""] + groupes)
        self.groupe.setCurrentText(m.get("groupe", ""))
        self.groupe.lineEdit().setPlaceholderText("aucun (ex. Relais, Aviation…)")

        def actuelle():
            self.freq.setText(f"{f_actuelle() / 1e6:.6f}".rstrip("0").rstrip("."))
            self.mode.setCurrentText(mode_actuel())
        b_actuelle.clicked.connect(actuelle)
        form.addRow("Nom", self.nom)
        form.addRow("Fréquence (MHz)", ligne)
        form.addRow("Mode", self.mode)
        form.addRow("Groupe", self.groupe)
        self.erreur = QLabel()
        self.erreur.setStyleSheet("color: #ff5252")
        form.addRow(self.erreur)
        boutons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        boutons.accepted.connect(self._valider)
        boutons.rejected.connect(self.reject)
        form.addRow(boutons)
        self.nom.setFocus()
        self.nom.selectAll()

    def _valider(self):
        if lire_frequence(self.freq.text()) is None:
            self.erreur.setText("Fréquence illisible : en MHz, par exemple 145.500")
            return
        self.accept()

    def resultat(self) -> dict:
        return {"nom": self.nom.text().strip() or "—", "frequence": lire_frequence(self.freq.text()),
                "mode": self.mode.currentText(), "groupe": self.groupe.currentText().strip()}


class Memoires(Module):
    nom = "Mémoires"
    description = "Fréquences favorites : ajouter, modifier, ranger par groupe, rappeler, sauvegarder dans un fichier."

    def demarrer(self) -> None:
        self.fichier = self.ctx.dossier_donnees("memoires") / "memoires.json"
        try:
            liste = json.loads(self.fichier.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            liste = MEMOIRES_DEFAUT
        self.memoires = [m for m in (normaliser(x, self.modes()) for x in liste if isinstance(x, dict)) if m]
        self.memoires.sort(key=lambda m: m["frequence"])
        self.dossier_export = str(Path.home())
        self.visibles: list[int] = []

    def modes(self) -> list[str]:
        modes = getattr(self.ctx, "modes", None)
        return list(modes()) if modes else MODES_DEFAUT

    # ------------------------------------------------------------ interface
    def creer_widget(self) -> QWidget:
        w = self.widget = QWidget()
        w.setMinimumWidth(340)                                  # assez pour lire les noms
        lay = QVBoxLayout(w)
        filtres = QHBoxLayout()
        self.recherche = QLineEdit()
        self.recherche.setPlaceholderText("Rechercher…")
        self.recherche.setClearButtonEnabled(True)
        self.recherche.textChanged.connect(self._remplir)
        self.c_groupe = QComboBox()
        self.c_groupe.setToolTip("N'afficher qu'un groupe")
        self.c_groupe.currentIndexChanged.connect(self._remplir)
        filtres.addWidget(self.recherche, 1)
        filtres.addWidget(self.c_groupe)
        lay.addLayout(filtres)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["CH", "MHz", "Mode", "Nom", "Groupe"])
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setToolTip("Double-clic : écouter · Entrée : modifier · Suppr : supprimer")
        en_tete = self.table.horizontalHeader()
        for c in (0, 1, 2):
            en_tete.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        en_tete.setSectionResizeMode(3, QHeaderView.Stretch)      # le nom prend la place libre
        en_tete.setSectionResizeMode(4, QHeaderView.Interactive)
        en_tete.setStretchLastSection(False)
        en_tete.resizeSection(4, 72)
        self.table.verticalHeader().setDefaultSectionSize(22)
        self.table.cellDoubleClicked.connect(lambda r, _c: self._rappeler(r))
        lay.addWidget(self.table, 1)
        QShortcut(QKeySequence.Delete, self.table, self._supprimer, context=Qt.WidgetShortcut)
        QShortcut(QKeySequence(Qt.Key_Return), self.table, self._modifier, context=Qt.WidgetShortcut)

        boutons = QHBoxLayout()
        for texte, action, aide in (
                ("Ajouter", self._ajouter, "Mémoriser la fréquence et le mode actuels"),
                ("Modifier", self._modifier, "Changer le nom, la fréquence, le mode ou le groupe"),
                ("Supprimer", self._supprimer, "Supprimer les mémoires sélectionnées")):
            b = QPushButton(texte)
            b.setToolTip(aide)
            b.clicked.connect(action)
            boutons.addWidget(b)
        lay.addLayout(boutons)
        fichiers = QHBoxLayout()
        b_export = QPushButton("Sauvegarder…")
        b_export.setToolTip("Enregistrer les mémoires dans un fichier CSV (compatible CHIRP) ou JSON")
        b_export.clicked.connect(self._exporter)
        b_import = QPushButton("Importer…")
        b_import.setToolTip("Ajouter des mémoires depuis un fichier CSV (CHIRP) ou JSON")
        b_import.clicked.connect(self._importer)
        fichiers.addWidget(b_export)
        fichiers.addWidget(b_import)
        lay.addLayout(fichiers)
        self.etat = QLabel()
        self.etat.setWordWrap(True)
        self.etat.setStyleSheet("color: #8899aa")
        lay.addWidget(self.etat)
        self._remplir()
        return w

    def groupes(self) -> list[str]:
        return sorted({m["groupe"] for m in self.memoires if m["groupe"]}, key=str.lower)

    def _remplir(self, *_):
        choix = self.c_groupe.currentText() or TOUS
        groupes = [TOUS] + self.groupes()
        self.c_groupe.blockSignals(True)
        self.c_groupe.clear()
        self.c_groupe.addItems(groupes)
        self.c_groupe.setCurrentText(choix if choix in groupes else TOUS)
        self.c_groupe.blockSignals(False)
        groupe = self.c_groupe.currentText()
        cherche = self.recherche.text().strip().lower().replace(",", ".")
        n = len(self.memoires)
        largeur = len(str(n))
        self.visibles = [
            i for i, m in enumerate(self.memoires)
            if (groupe == TOUS or m["groupe"] == groupe)
            and (not cherche or cherche in f"{m['nom']} {m['frequence'] / 1e6:.6f} {m['groupe']} {m['mode']}".lower())]
        self.table.setRowCount(len(self.visibles))
        for r, i in enumerate(self.visibles):
            m = self.memoires[i]
            for c, v in enumerate([f"{i + 1:0{largeur}d}", f"{m['frequence'] / 1e6:.4f}", m["mode"], m["nom"], m["groupe"]]):
                item = QTableWidgetItem(v)
                item.setToolTip(f"{m['nom']} · {m['frequence'] / 1e6:.6f} MHz {m['mode']}"
                                + (f" · {m['groupe']}" if m["groupe"] else ""))
                if c in (0, 1):
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self.table.setItem(r, c, item)
        self.etat.setText(f"{len(self.visibles)} / {n} mémoire(s) · enregistrées automatiquement")

    def _selection(self) -> list[int]:
        return sorted({self.visibles[ix.row()] for ix in self.table.selectionModel().selectedRows()})

    def _rappeler(self, rang: int) -> None:
        m = self.memoires[self.visibles[rang]]
        self.ctx.regler_mode(m["mode"])
        self.ctx.regler_frequence(m["frequence"])

    def _dialogue(self, titre, m):
        d = DialogueMemoire(self.widget, titre, m, self.modes(), self.groupes(), self.ctx.frequence, self.ctx.mode)
        return d.resultat() if d.exec() == QDialog.Accepted else None

    def _ajouter(self) -> None:
        groupe = self.c_groupe.currentText()
        m = self._dialogue("Nouvelle mémoire", {"nom": "", "frequence": round(self.ctx.frequence()),
                                                "mode": self.ctx.mode(), "groupe": "" if groupe == TOUS else groupe})
        if m:
            self.memoires.append(m)
            self._enregistrer(selection=m)

    def _modifier(self) -> None:
        sel = self._selection()
        if not sel:
            self.etat.setText("Sélectionnez la mémoire à modifier.")
            return
        m = self._dialogue("Modifier la mémoire", self.memoires[sel[0]])
        if m:
            self.memoires[sel[0]] = m
            self._enregistrer(selection=m)

    def _supprimer(self) -> None:
        sel = self._selection()
        if not sel:
            return
        if len(sel) > 1 and QMessageBox.question(self.widget, "Mémoires", f"Supprimer {len(sel)} mémoires ?") \
                != QMessageBox.Yes:
            return
        for i in reversed(sel):
            del self.memoires[i]
        self._enregistrer()

    def _enregistrer(self, selection: dict | None = None) -> None:
        self.memoires.sort(key=lambda m: m["frequence"])
        self._sauver()
        self._remplir()
        for r, i in enumerate(self.visibles):
            if self.memoires[i] is selection:
                self.table.selectRow(r)
                self.table.scrollToItem(self.table.item(r, 0))

    def _sauver(self) -> None:
        self.fichier.write_text(json.dumps(self.memoires, indent=2, ensure_ascii=False), encoding="utf-8")

    # ------------------------------------------------------------ fichiers
    def exporter_vers(self, p: Path) -> None:
        if p.suffix.lower() == ".json":
            p.write_text(json.dumps(self.memoires, indent=2, ensure_ascii=False), encoding="utf-8")
        else:
            p.write_text(vers_csv_chirp(self.memoires), encoding="utf-8")

    def _exporter(self) -> None:
        chemin, filtre = QFileDialog.getSaveFileName(
            self.widget, "Sauvegarder les mémoires", str(Path(self.dossier_export) / "memoires.csv"),
            "CSV compatible CHIRP (*.csv);;JSON (*.json)")
        if not chemin:
            return
        p = Path(chemin)
        if p.suffix.lower() not in (".csv", ".json"):
            p = p.with_suffix(".json" if "JSON" in filtre else ".csv")
        try:
            self.exporter_vers(p)
        except OSError as e:
            QMessageBox.warning(self.widget, "Mémoires", f"Impossible d'écrire {p} :\n{e}")
            return
        self.dossier_export = str(p.parent)
        self.etat.setText(f"{len(self.memoires)} mémoire(s) sauvegardée(s) dans {p.name}")

    def _importer(self) -> None:
        chemin, _ = QFileDialog.getOpenFileName(self.widget, "Importer des mémoires", self.dossier_export,
                                                "Mémoires (*.csv *.json);;Tous les fichiers (*)")
        if not chemin:
            return
        p = Path(chemin)
        try:
            texte = p.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError) as e:
            QMessageBox.warning(self.widget, "Mémoires", f"Impossible de lire {p.name} :\n{e}")
            return
        self.dossier_export = str(p.parent)
        n, doublons = self.importer_texte(texte)
        if n is None:
            QMessageBox.warning(self.widget, "Mémoires", f"Aucune mémoire reconnue dans {p.name}.")
            return
        self.etat.setText(f"{n} mémoire(s) ajoutée(s) depuis {p.name}"
                          + (f", {doublons} déjà présente(s)" if doublons else ""))

    def importer_texte(self, texte: str) -> tuple[int | None, int]:
        """Ajoute les mémoires d'un fichier JSON ou CSV, sans doublon (fréquence + mode) : (ajoutées, doublons)."""
        modes = self.modes()
        liste = []
        if texte.lstrip().startswith("["):
            try:
                liste = [m for m in (normaliser(x, modes) for x in json.loads(texte) if isinstance(x, dict)) if m]
            except ValueError:
                liste = []
        else:
            liste = depuis_csv(texte, modes)
        if not liste:
            return None, 0
        connues = {(m["frequence"], m["mode"]) for m in self.memoires}
        nouvelles = []
        for m in liste:
            if (m["frequence"], m["mode"]) not in connues:
                connues.add((m["frequence"], m["mode"]))
                nouvelles.append(m)
        self.memoires += nouvelles
        self._enregistrer()
        return len(nouvelles), len(liste) - len(nouvelles)

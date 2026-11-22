"""Fenêtre principale."""

from __future__ import annotations

import logging

import numpy as np
from PySide6.QtCore import QByteArray, Qt, Signal
from PySide6.QtGui import QAction, QActionGroup, QKeySequence, QShortcut
from PySide6.QtWidgets import (QComboBox, QDockWidget, QLabel, QMainWindow, QMessageBox,
                               QScrollArea, QToolButton, QWidget)

from .. import __version__
from ..audio import SortieAudio
from ..bus import Bus
from ..config import PAS_ACCORD, Reglages
from ..demods import decouvrir
from ..modules import Contexte, GestionnaireModules
from ..moteur import Moteur
from ..sources import SOURCES, TOUTES_SOURCES
from ..sources.rtlsdr_source import libelle_cle, lister_cles
from .afficheur import AfficheurFrequence
from .dialogue_modules import DialogueModules
from .panneau import PanneauReglages, texte_plages
from .vue_spectre import VueSpectre

log = logging.getLogger(__name__)


class DockModule(QDockWidget):
    ferme = Signal(str)

    def __init__(self, nom: str, parent) -> None:
        super().__init__(nom, parent)
        self.nom = nom
        self.setObjectName(f"module_{nom}")

    def closeEvent(self, ev) -> None:
        self.ferme.emit(self.nom)
        super().closeEvent(ev)


class FenetrePrincipale(QMainWindow):
    _erreur_module = Signal(object, str)

    def __init__(self, source: str | None = None) -> None:
        super().__init__()
        self.reglages = r = Reglages.charger()
        if source:                       # source imposée (tests automatiques)
            r.source = source
        elif r.source not in SOURCES:    # anciens réglages (source simulée retirée) : retour à la clé
            r.source = "rtlsdr"
        self.bus = Bus()
        self.bus.sur_erreur = lambda rappel, exc: self._erreur_module.emit(rappel, str(exc))
        self.modes = decouvrir()
        if r.mode not in self.modes:
            r.mode = next(iter(self.modes))

        self.infos_materiel: dict = {}
        self.sortie = SortieAudio()
        self.moteur = Moteur(self.bus, self.modes, self.sortie)
        m = self.moteur
        m.centre, m.taux, m.decalage = r.centre, r.taux, r.decalage
        m.convertisseur = r.convertisseur
        m.volume, m.squelch, m.agc_actif = r.volume, r.squelch, r.agc
        m.taille_fft, m.moyennage, m.zoom = r.taille_fft, r.moyennage, r.zoom

        self._construire_interface()
        self._connecter()

        self.modules = GestionnaireModules(Contexte(self.bus, self))
        self.modules.decouvrir()
        self.docks: dict[str, DockModule] = {}
        for nom in list(r.modules_actifs):
            if nom in self.modules.classes:
                self.activer_module(nom, silencieux=True)

        if r.geometrie:
            self.restoreGeometry(QByteArray.fromBase64(r.geometrie.encode()))
        else:
            self.resize(1280, 800)
        if r.etat_fenetre:
            self.restoreState(QByteArray.fromBase64(r.etat_fenetre.encode()))

        self.regler_mode(r.mode)
        self.vue.regler_niveaux(r.niveau_min, r.niveau_max)
        self._maj_vfo()
        if not self.sortie.disponible:
            self.statusBar().showMessage(self.sortie.erreur, 10_000)

    # ================================================================ interface
    def _construire_interface(self) -> None:
        r = self.reglages
        barre = self.addToolBar("Principal")
        barre.setObjectName("barre_principale")
        barre.setMovable(False)

        self.b_marche = QToolButton()
        self.b_marche.setCheckable(True)
        self.b_marche.setText("▶ Démarrer")
        self.b_marche.setMinimumWidth(110)
        barre.addWidget(self.b_marche)
        barre.addSeparator()

        self.afficheur = AfficheurFrequence()
        barre.addWidget(self.afficheur)
        barre.addSeparator()

        barre.addWidget(QLabel(" Mode "))
        self.c_mode = QComboBox()
        self.c_mode.addItems(list(self.modes))
        barre.addWidget(self.c_mode)

        barre.addWidget(QLabel("  Pas "))
        self.c_pas = QComboBox()
        for p in PAS_ACCORD:
            self.c_pas.addItem(self._texte_pas(p), p)
        self.c_pas.setCurrentIndex(max(0, self.c_pas.findData(r.pas)))
        barre.addWidget(self.c_pas)

        self.vue = VueSpectre()
        self.setCentralWidget(self.vue)

        self.panneau = PanneauReglages(r)
        defil = QScrollArea()
        defil.setWidget(self.panneau)
        defil.setWidgetResizable(True)
        defil.setMinimumWidth(300)
        dock = QDockWidget("Réglages", self)
        dock.setObjectName("dock_reglages")
        dock.setWidget(defil)
        dock.setFeatures(QDockWidget.DockWidgetMovable | QDockWidget.DockWidgetFloatable)
        self.addDockWidget(Qt.LeftDockWidgetArea, dock)

        # menus
        menu = self.menuBar().addMenu("&Fichier")
        menu.addAction("Quitter", QKeySequence.Quit, self.close)

        self.menu_source = self.menuBar().addMenu("&Source")
        self.menu_source.aboutToShow.connect(self._remplir_menu_source)
        self._groupe_source = None
        self._remplir_menu_source()

        self.menu_modules = self.menuBar().addMenu("&Modules")
        self.menu_modules.aboutToShow.connect(self._remplir_menu_modules)

        menu = self.menuBar().addMenu("&Aide")
        menu.addAction("À propos", self._a_propos)

        self._avertissement = ""
        self.lbl_source = QLabel()
        self.statusBar().addPermanentWidget(self.lbl_source)
        self._maj_statut()

        for touche, n in ((Qt.Key_Up, 1), (Qt.Key_Down, -1), (Qt.Key_PageUp, 10), (Qt.Key_PageDown, -10)):
            QShortcut(QKeySequence(touche), self, activated=lambda n=n: self._pas(n))

    def _connecter(self) -> None:
        m, p = self.moteur, self.panneau
        m.spectre_pret.connect(self.vue.maj)
        m.niveau.connect(p.afficher_niveau)
        m.erreur.connect(self._erreur_moteur)
        m.en_marche.connect(self._etat_marche)
        self._erreur_module.connect(self._sur_erreur_module)

        self.b_marche.clicked.connect(self._basculer_marche)
        self.afficheur.frequence_demandee.connect(self.regler_frequence)
        self.vue.clic_frequence.connect(self._clic_spectre)
        self.vue.molette.connect(self._pas)
        self.c_mode.currentTextChanged.connect(self.regler_mode)
        self.c_pas.currentIndexChanged.connect(lambda _: setattr(self.reglages, "pas", self.c_pas.currentData()))

        p.taux_change.connect(self._changer_taux)
        p.gain_change.connect(self._changer_gain)
        p.ppm_change.connect(lambda v: self._source("ppm", "regler_ppm", v))
        p.direct_change.connect(lambda v: self._source("echantillonnage_direct", "regler_echantillonnage_direct", v))
        p.convertisseur_change.connect(self._changer_convertisseur)
        p.bias_t_change.connect(lambda v: self._source("bias_t", "regler_bias_t", v))
        p.largeur_change.connect(self._changer_largeur)
        p.agc_change.connect(lambda v: self._param("agc", "agc_actif", v))
        p.squelch_change.connect(lambda v: self._param("squelch", "squelch", v))
        p.volume_change.connect(lambda v: self._param("volume", "volume", v))
        p.fft_change.connect(lambda v: self._param("taille_fft", "taille_fft", v))
        p.moyennage_change.connect(lambda v: self._param("moyennage", "moyennage", v))
        p.zoom_change.connect(lambda v: self._param("zoom", "zoom", v))
        p.niveaux_change.connect(self._changer_niveaux)
        p.auto_echelle.connect(self._echelle_auto)

    @staticmethod
    def _texte_pas(p: int) -> str:
        if p >= 1_000_000:
            return f"{p / 1e6:g} MHz"
        if p >= 1000:
            return f"{p / 1e3:g} kHz"
        return f"{p} Hz"

    # ================================================================ accord
    def _bande(self) -> tuple[float, float]:
        d = self.modes[self.reglages.mode]()
        d.largeur = self.reglages.largeurs.get(self.reglages.mode, d.largeur_defaut)
        return d.bande()

    def regler_frequence(self, f: float) -> None:
        r = self.reglages
        f = max(0.0, float(f))
        bas, haut = self._bande()
        limite = r.taux / 2 * 0.92
        if f + bas < r.centre - limite or f + haut > r.centre + limite:
            # hors de la bande visible : on recentre, VFO légèrement à droite du centre (évite le pic DC)
            centre = max(0.0, f - r.taux * 0.1)
            plages = self._plages()
            if plages and not r.echantillonnage_direct:
                # À la limite d'une bande, le décalage du VFO ne doit pas
                # entraîner le tuner hors de sa plage de fonctionnement.
                candidats = [min(max(f, mini), maxi) for mini, maxi in plages]
                proche = min(candidats, key=lambda candidat: abs(candidat - f))
                mini, maxi = next((lo, hi) for lo, hi in plages if lo <= proche <= hi)
                centre = min(max(centre, mini), maxi)
            if centre != r.centre:
                r.centre = centre
                self.moteur.regler_centre(centre)
        r.decalage = f - r.centre
        self.moteur.decalage = r.decalage
        self._maj_vfo()

    def _clic_spectre(self, f: float) -> None:
        pas = self.reglages.pas
        self.regler_frequence(round(f / pas) * pas)

    def _pas(self, n: int) -> None:
        pas = self.reglages.pas
        base = round(self.reglages.frequence / pas) * pas
        self.regler_frequence(base + n * pas)

    def regler_mode(self, mode: str) -> None:
        if mode not in self.modes:
            return
        r = self.reglages
        r.mode = mode
        classe = self.modes[mode]
        largeur = r.largeurs.get(mode, classe.largeur_defaut)
        self.panneau.regler_demod(classe.largeur_min, classe.largeur_max, classe.pas_largeur, largeur)
        self.moteur.regler_mode(mode, largeur)
        if self.c_mode.currentText() != mode:
            self.c_mode.blockSignals(True)
            self.c_mode.setCurrentText(mode)
            self.c_mode.blockSignals(False)
        self._maj_vfo()

    def _maj_vfo(self) -> None:
        r = self.reglages
        f = r.frequence
        bas, haut = self._bande()
        plages = self._plages()
        hors = bool(plages) and not any(a <= f <= b for a, b in plages)
        self.afficheur.hors_plage = hors
        self._avertissement = f"⚠ Hors couverture du tuner ({texte_plages(plages)})" if hors else ""
        self._maj_statut()
        self.afficheur.setToolTip(f"Hors couverture : {texte_plages(plages)}" if hors else
                                  "Molette sur un chiffre pour le modifier · clic haut/bas : ± · double-clic : saisie")
        self.afficheur.regler(f)
        self.vue.regler_vfo(f, bas, haut)
        self.setWindowTitle(f"SDR Modulaire — {f / 1e6:.6f} MHz {r.mode}")
        self.bus.publier("vfo", f, r.mode)

    # ================================================================ réglages
    def _param(self, cle: str, attr: str, valeur) -> None:
        setattr(self.reglages, cle, valeur)
        setattr(self.moteur, attr, valeur)

    def _plages(self) -> list[tuple[float, float]]:
        """Plages du tuner ramenées aux fréquences affichées (décalage du convertisseur HF)."""
        c = self.reglages.convertisseur
        return [(max(0.0, a - c), b - c) for a, b in (self.infos_materiel.get("plages") or []) if b - c > 0]

    def _changer_convertisseur(self, hz: float) -> None:
        r = self.reglages
        if hz == r.convertisseur:
            return
        r.convertisseur = self.moteur.convertisseur = hz
        self.moteur.regler_centre(r.centre)              # la clé est réaccordée avec le nouveau décalage
        self._maj_vfo()

    def _source(self, cle: str, methode: str, valeur) -> None:
        setattr(self.reglages, cle, valeur)
        self.moteur.commande_source(methode, valeur)

    def _changer_gain(self, g) -> None:
        self.reglages.gain = g
        self.moteur.commande_source("regler_gain", g)

    def _changer_largeur(self, v: float) -> None:
        self.reglages.largeurs[self.reglages.mode] = v
        self.moteur.regler_largeur(v)
        self._maj_vfo()

    def _ajouter_modules(self) -> None:
        from .dialogue_ajout_modules import DialogueAjoutModules
        DialogueAjoutModules(self).exec()

    def _changer_taux(self, t: int) -> None:
        etait = self.moteur.actif
        if etait:
            self.moteur.arreter()
        self.reglages.taux = self.moteur.taux = t
        self.panneau._zooms_pour(t, self.reglages.zoom)     # largeurs plus petites que la nouvelle bande
        self.regler_frequence(self.reglages.frequence)
        if etait:
            self.demarrer()

    def _changer_niveaux(self, mini: float, maxi: float) -> None:
        self.reglages.niveau_min, self.reglages.niveau_max = mini, maxi
        self.vue.regler_niveaux(mini, maxi)

    def _echelle_auto(self) -> None:
        db = self.vue.dernier_spectre()
        if db is None:
            return
        plancher = float(np.percentile(db, 20))
        mini = round(plancher - 8)
        maxi = round(max(float(np.percentile(db, 99.9)) + 5, plancher + 30))
        self.panneau.regler_niveaux(mini, maxi)
        self._changer_niveaux(mini, maxi)

    # ================================================================ marche / arrêt
    def _basculer_marche(self) -> None:
        if self.moteur.actif:
            self.moteur.arreter()
        else:
            self.demarrer()

    def demarrer(self) -> None:
        r = self.reglages
        m = self.moteur
        m.centre, m.taux, m.decalage = r.centre, r.taux, r.decalage
        m.convertisseur = r.convertisseur
        if r.source == "rtlsdr":
            source = SOURCES["rtlsdr"](index=r.rtlsdr_index, serie=r.rtlsdr_serie)
        else:
            source = TOUTES_SOURCES[r.source]()
        problemes = []

        def initialiser(src):
            for nom, valeur, defaut in (("regler_echantillonnage_direct", r.echantillonnage_direct, 0),
                                        ("regler_ppm", r.ppm, 0),
                                        ("regler_bias_t", r.bias_t, False),
                                        ("regler_gain", r.gain, object())):
                if valeur == defaut:
                    continue
                try:
                    getattr(src, nom)(valeur)
                except Exception as exc:
                    problemes.append(f"{nom} : {exc}")
            self.panneau.regler_gains(src.gains(), r.gain)
            try:
                self.infos_materiel = src.infos()
            except Exception as exc:
                log.exception("Lecture des infos matériel")
                self.infos_materiel = {"modele": src.nom, "remarque": str(exc)}

        try:
            m.demarrer(source, r.mode, r.largeurs.get(r.mode, 0), initialiser)
        except Exception as exc:
            log.exception("Démarrage")
            self.b_marche.setChecked(False)
            QMessageBox.warning(
                self, "Démarrage impossible",
                f"Impossible d'ouvrir la source « {source.nom} » :\n\n{exc}\n\n"
                "Vérifiez que la clé est branchée et qu'aucun autre programme ne l'utilise "
                "(SDR Web dans le navigateur, rtl_tcp, Gqrx…).")
            return
        self.panneau.appliquer_materiel(self.infos_materiel)
        self._maj_statut()
        self._maj_vfo()
        if problemes:
            self.statusBar().showMessage(" · ".join(problemes), 10_000)

    def _etat_marche(self, actif: bool) -> None:
        self.b_marche.setChecked(actif)
        self.b_marche.setText("■ Arrêter" if actif else "▶ Démarrer")
        self.panneau.taux.setEnabled(True)
        self._maj_statut()

    def _erreur_moteur(self, msg: str) -> None:
        self.statusBar().showMessage(msg, 8_000)
        if not self.moteur.actif and self.moteur._threads:
            self.moteur.arreter()

    def _remplir_menu_source(self) -> None:
        """Reconstruit le menu Source à chaque ouverture : les clés branchées à cet instant."""
        r, menu = self.reglages, self.menu_source
        menu.clear()
        if self._groupe_source is not None:
            self._groupe_source.deleteLater()
        groupe = self._groupe_source = QActionGroup(menu)

        def ajouter(texte, coche, rappel):
            a = QAction(texte, menu, checkable=True)
            a.setChecked(coche)
            a.triggered.connect(rappel)
            groupe.addAction(a)
            menu.addAction(a)

        cles = lister_cles()
        for c in cles:
            doublon = sum(x["serie"] == c["serie"] for x in cles) > 1
            coche = (r.source == "rtlsdr"
                     and (c["serie"] == r.rtlsdr_serie or (not r.rtlsdr_serie and c["index"] == 0))
                     and (not doublon or c["index"] == r.rtlsdr_index))
            ajouter(libelle_cle(c, cles), coche,
                    lambda _=False, c=c: self._choisir_source("rtlsdr", c["index"], c["serie"]))
        if not cles:
            a = menu.addAction("RTL-SDR : aucune clé détectée")
            a.setEnabled(False)
        for cle, classe in SOURCES.items():
            if cle != "rtlsdr":
                ajouter(classe.nom, cle == r.source, lambda _=False, c=cle: self._choisir_source(c))
        menu.addSeparator()
        menu.addAction("Informations sur le matériel…", self._afficher_materiel)

    def _choisir_source(self, cle: str, index: int | None = None, serie: str | None = None) -> None:
        etait = self.moteur.actif
        if etait:
            self.moteur.arreter()
        self.reglages.source = cle
        if cle == "rtlsdr" and index is not None:
            self.reglages.rtlsdr_index = index
            self.reglages.rtlsdr_serie = serie or ""
        self.infos_materiel = {}
        self._maj_statut()
        if etait:
            self.demarrer()

    def _maj_statut(self) -> None:
        etat = "en marche" if self.moteur.actif else "arrêtée"
        i = self.infos_materiel
        detail = f" — {i.get('modele', '')}, {i.get('tuner', '')}".rstrip(", ") if i else ""
        if i.get("numero_serie"):
            detail += f", n° {i['numero_serie']}"
        texte = f"Source : {TOUTES_SOURCES[self.reglages.source].nom}{detail} ({etat})"
        if getattr(self, "_avertissement", ""):
            texte = f"<span style='color:#ff7043;font-weight:bold'>{self._avertissement}</span>&nbsp;&nbsp;&nbsp;{texte}"
        self.lbl_source.setText(texte)

    # ================================================================ modules
    def activer_module(self, nom: str, silencieux: bool = False) -> bool:
        try:
            instance = self.modules.activer(nom)
            widget = instance.creer_widget()
        except Exception as exc:
            log.exception("Activation du module %s", nom)
            self.modules.desactiver(nom)
            if not silencieux:
                QMessageBox.warning(self, "Module", f"Le module « {nom} » n'a pas pu démarrer :\n{exc}")
            return False
        if widget is not None:
            dock = DockModule(nom, self)
            dock.setWidget(widget)
            dock.ferme.connect(self.desactiver_module)
            self.addDockWidget(Qt.RightDockWidgetArea, dock)
            self.docks[nom] = dock
        self.statusBar().showMessage(f"Module « {nom} » ajouté", 3_000)
        return True

    def desactiver_module(self, nom: str) -> None:
        self.modules.desactiver(nom)
        dock = self.docks.pop(nom, None)
        if dock is not None:
            dock.ferme.disconnect(self.desactiver_module)
            self.removeDockWidget(dock)
            dock.deleteLater()
        self.statusBar().showMessage(f"Module « {nom} » retiré", 3_000)

    def recharger_module(self, nom: str) -> None:
        actif = nom in self.modules.actifs
        if actif:
            self.desactiver_module(nom)
        self.modules.recharger(nom)
        if actif:
            self.activer_module(nom)

    def _remplir_menu_modules(self) -> None:
        menu = self.menu_modules
        menu.clear()
        menu.addAction("Gérer les modules…", lambda: DialogueModules(self).exec())
        menu.addAction("Ajouter des modules…", self._ajouter_modules)
        menu.addSeparator()
        for nom in sorted(self.modules.classes):
            a = menu.addAction(nom)
            a.setCheckable(True)
            a.setChecked(nom in self.modules.actifs)
            a.toggled.connect(lambda on, n=nom: self.activer_module(n) if on else self.desactiver_module(n))

    def _sur_erreur_module(self, rappel, msg: str) -> None:
        nom = self.modules.module_du_rappel(rappel)
        if nom:
            self.desactiver_module(nom)
            self.statusBar().showMessage(f"Module « {nom} » retiré après une erreur : {msg}", 10_000)

    # ================================================================ divers
    def _afficher_materiel(self) -> None:
        i = self.infos_materiel
        if not i:
            QMessageBox.information(self, "Matériel",
                                    "Démarre la source pour lire les informations de la clé.")
            return

        def oui_non(v):
            return {True: "oui", False: "non"}.get(v, "inconnu")
        gains = i.get("gains") or []
        lignes = [
            ("Fabricant", i.get("fabricant") or "—"),
            ("Modèle", i.get("modele") or "—"),
            ("Puce", i.get("puce") or "—"),
            ("N° de série", i.get("numero_serie") or "—"),
            ("Tuner", i.get("tuner") or "—"),
            ("Couverture", texte_plages(i.get("plages") or []) or "inconnue"),
            ("Gain", f"{min(gains):.1f} à {max(gains):.1f} dB" if gains else "—"),
            ("Paliers de gain", " ".join(f"{g:g}" for g in gains) or "—"),
            ("Taux max stable", f"{i['taux_max'] / 1e6:.3f} MS/s" if i.get("taux_max") else "—"),
            ("Bias-T", oui_non(i.get("bias_t"))),
            ("Échantillonnage direct (HF)", oui_non(i.get("echantillonnage_direct"))),
        ]
        html = "<table cellspacing=4>" + "".join(
            f"<tr><td><b>{k}</b></td><td>{v}</td></tr>" for k, v in lignes) + "</table>"
        if i.get("remarque"):
            html += f"<p>{i['remarque']}</p>"
        QMessageBox.information(self, "Matériel détecté", html)

    def _a_propos(self) -> None:
        QMessageBox.about(self, "SDR Modulaire",
                          f"<b>SDR Modulaire</b> {__version__}<br>Récepteur SDR à modules — F4HXN<br><br>"
                          f"Modes : {', '.join(self.modes)}<br>"
                          f"Modules disponibles : {len(self.modules.classes)}")

    def closeEvent(self, ev) -> None:
        r = self.reglages
        self.moteur.arreter()
        r.modules_actifs = list(self.modules.actifs)
        r.geometrie = bytes(self.saveGeometry().toBase64()).decode()
        r.etat_fenetre = bytes(self.saveState().toBase64()).decode()
        for nom in list(self.modules.actifs):
            self.modules.desactiver(nom)
        try:
            r.sauver()
        except Exception:
            log.exception("Sauvegarde des réglages")
        super().closeEvent(ev)

"""Système de modules : chargement, activation et retrait à chaud.

Un module est un fichier .py placé dans `modules/` (à côté du programme) ou
dans `~/.config/sdr-modulaire/modules/`, contenant une sous-classe de Module.
"""

from __future__ import annotations

import importlib.util
import inspect
import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from .config import DOSSIER_CONFIG

if TYPE_CHECKING:
    from PySide6.QtWidgets import QWidget

    from .bus import Bus

log = logging.getLogger(__name__)

DOSSIERS_MODULES = [
    Path(__file__).resolve().parent.parent / "modules",
    DOSSIER_CONFIG / "modules",
]


class Contexte:
    """Ce que le récepteur met à disposition des modules."""

    def __init__(self, bus: "Bus", fenetre) -> None:
        self.bus = bus
        self._fenetre = fenetre

    def frequence(self) -> float:
        return self._fenetre.reglages.frequence

    def mode(self) -> str:
        return self._fenetre.reglages.mode

    def regler_frequence(self, f: float) -> None:
        """À appeler depuis le thread de l'interface (ex. un bouton)."""
        self._fenetre.regler_frequence(f)

    def regler_mode(self, mode: str) -> None:
        self._fenetre.regler_mode(mode)

    def taux(self) -> int:
        """Taux d'échantillonnage courant de la clé (échantillons par seconde)."""
        return self._fenetre.reglages.taux

    def regler_taux(self, taux: int) -> None:
        """Change le taux d'échantillonnage (redémarre la source). Thread de l'interface uniquement."""
        choix = self._fenetre.panneau.taux
        i = choix.findData(taux)
        if i >= 0:
            choix.setCurrentIndex(i)

    def gains(self) -> list[float]:
        """Paliers de gain de la clé en dB (liste vide si la source n'en a pas)."""
        return list(self._fenetre.panneau._gains)

    def regler_gain(self, gain: float | None) -> None:
        """Gain en dB (palier le plus proche), ou None pour l'automatique. Thread de l'interface uniquement."""
        p = self._fenetre.panneau
        if gain is None:
            p.gain_auto.setChecked(True)
            return
        if not p._gains:
            return
        p.gain_auto.setChecked(False)
        p.gain.setValue(min(range(len(p._gains)), key=lambda i: abs(p._gains[i] - gain)))
        p._emettre_gain()

    def dossier_donnees(self, nom: str) -> Path:
        d = DOSSIER_CONFIG / "donnees" / nom
        d.mkdir(parents=True, exist_ok=True)
        return d


class Module:
    """Classe de base. Surcharger demarrer(), arreter() et éventuellement creer_widget()."""

    nom = ""
    description = ""

    def __init__(self, contexte: Contexte) -> None:
        self.ctx = contexte
        self._abonnements: list[tuple[str, Callable]] = []

    def abonner(self, sujet: str, rappel: Callable) -> None:
        self.ctx.bus.abonner(sujet, rappel)
        self._abonnements.append((sujet, rappel))

    def demarrer(self) -> None:
        pass

    def arreter(self) -> None:
        pass

    def creer_widget(self) -> "QWidget | None":
        return None

    def _liberer(self) -> None:
        for sujet, rappel in self._abonnements:
            self.ctx.bus.desabonner(sujet, rappel)
        self._abonnements.clear()


class GestionnaireModules:
    def __init__(self, contexte: Contexte, dossiers: list[Path] | None = None) -> None:
        self.ctx = contexte
        self.dossiers = dossiers or DOSSIERS_MODULES
        self.classes: dict[str, tuple[type[Module], Path]] = {}
        self.actifs: dict[str, Module] = {}
        self.erreurs: dict[str, str] = {}

    def decouvrir(self) -> None:
        self.classes.clear()
        self.erreurs.clear()
        for dossier in self.dossiers:
            if not dossier.is_dir():
                continue
            for chemin in sorted(dossier.glob("*.py")):
                if not chemin.name.startswith("_"):
                    self._charger_fichier(chemin)

    def _charger_fichier(self, chemin: Path) -> list[str]:
        nom_py = f"sdrmod_module_{chemin.stem}"
        try:
            spec = importlib.util.spec_from_file_location(nom_py, chemin)
            mod = importlib.util.module_from_spec(spec)
            sys.modules[nom_py] = mod
            spec.loader.exec_module(mod)
        except Exception as exc:
            log.exception("Module %s non chargé", chemin)
            self.erreurs[chemin.name] = str(exc)
            return []
        noms = []
        for _, obj in inspect.getmembers(mod, inspect.isclass):
            if issubclass(obj, Module) and obj is not Module and obj.__module__ == nom_py and obj.nom:
                self.classes[obj.nom] = (obj, chemin)
                noms.append(obj.nom)
        return noms

    def activer(self, nom: str) -> Module:
        if nom in self.actifs:
            return self.actifs[nom]
        classe, _ = self.classes[nom]
        instance = classe(self.ctx)
        try:
            instance.demarrer()
        except Exception:
            instance._liberer()
            raise
        self.actifs[nom] = instance
        return instance

    def desactiver(self, nom: str) -> None:
        instance = self.actifs.pop(nom, None)
        if instance is None:
            return
        try:
            instance.arreter()
        except Exception:
            log.exception("Arrêt du module %s", nom)
        finally:
            instance._liberer()

    def recharger(self, nom: str) -> None:
        """Relit le fichier du module (pratique pendant le développement)."""
        _, chemin = self.classes[nom]
        self.desactiver(nom)
        self._charger_fichier(chemin)

    def module_du_rappel(self, rappel: Callable) -> str | None:
        for nom, inst in self.actifs.items():
            if any(r == rappel for _, r in inst._abonnements):
                return nom
        return None

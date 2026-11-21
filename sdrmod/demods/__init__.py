"""Découverte automatique des démodulateurs présents dans ce dossier."""

from __future__ import annotations

import importlib
import inspect
import logging
import pkgutil

from .base import Demodulateur

log = logging.getLogger(__name__)


def decouvrir() -> dict[str, type[Demodulateur]]:
    trouves: list[type[Demodulateur]] = []
    for info in pkgutil.iter_modules(__path__):
        if info.name == "base":
            continue
        try:
            mod = importlib.import_module(f"{__name__}.{info.name}")
        except Exception:
            log.exception("Démodulateur « %s » ignoré (erreur au chargement)", info.name)
            continue
        for _, obj in inspect.getmembers(mod, inspect.isclass):
            if (issubclass(obj, Demodulateur) and obj.__module__ == mod.__name__
                    and obj.nom and not obj.__dict__.get("abstrait", False)):
                trouves.append(obj)
    trouves.sort(key=lambda c: (c.ordre, c.nom))
    return {c.nom: c for c in trouves}


__all__ = ["Demodulateur", "decouvrir"]

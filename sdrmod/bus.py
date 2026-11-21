"""Bus de publication/abonnement entre le moteur et les modules.

Sujets publiés par le moteur (depuis le thread DSP) :
    "iq"     (echantillons: complex64, centre: float, taux: int)
    "audio"  (echantillons: float32, taux: int)   — après AGC/squelch, avant volume
    "niveau" (dbfs: float)                         — puissance dans le canal
    "vfo"    (frequence: float, mode: str)         — à chaque changement (thread GUI)

Les rappels doivent être rapides : ils s'exécutent dans le thread qui publie.
Un rappel qui lève une exception est désabonné et signalé, il ne fait pas tomber
le récepteur.
"""

from __future__ import annotations

import logging
import threading
from collections import defaultdict
from typing import Any, Callable

log = logging.getLogger(__name__)


class Bus:
    def __init__(self) -> None:
        self._abonnes: dict[str, list[Callable]] = defaultdict(list)
        self._verrou = threading.Lock()
        self.sur_erreur: Callable[[Callable, Exception], None] | None = None

    def abonner(self, sujet: str, rappel: Callable) -> None:
        with self._verrou:
            if rappel not in self._abonnes[sujet]:
                self._abonnes[sujet].append(rappel)

    def desabonner(self, sujet: str, rappel: Callable) -> None:
        with self._verrou:
            try:
                self._abonnes[sujet].remove(rappel)
            except ValueError:
                pass

    def a_des_abonnes(self, sujet: str) -> bool:
        return bool(self._abonnes.get(sujet))

    def publier(self, sujet: str, *args: Any) -> None:
        with self._verrou:
            abonnes = list(self._abonnes.get(sujet, ()))
        for rappel in abonnes:
            try:
                rappel(*args)
            except Exception as exc:
                log.exception("Rappel %r en erreur sur « %s », désabonné", rappel, sujet)
                self.desabonner(sujet, rappel)
                if self.sur_erreur:
                    self.sur_erreur(rappel, exc)

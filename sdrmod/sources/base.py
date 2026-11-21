"""Interface commune des sources IQ."""

from __future__ import annotations

import numpy as np


class Source:
    """Toutes les méthodes sont appelées depuis le thread de lecture."""

    nom = "?"

    def ouvrir(self, taux: int, frequence: float) -> None:
        raise NotImplementedError

    def fermer(self) -> None:
        pass

    def lire(self, n: int) -> np.ndarray:
        """Retourne n échantillons complex64 normalisés dans [-1, 1]."""
        raise NotImplementedError

    def regler_frequence(self, frequence: float) -> None:
        raise NotImplementedError

    def regler_gain(self, gain: float | None) -> None:
        """gain en dB, ou None pour le gain automatique."""

    def gains(self) -> list[float]:
        return []

    def infos(self) -> dict:
        """Description du matériel ouvert. Clés reconnues : fabricant, modele, puce,
        numero_serie, tuner, plages [(min, max) Hz], gains, taux_max,
        bias_t / echantillonnage_direct (True, False ou None = inconnu), remarque."""
        return {"modele": self.nom}

    def regler_ppm(self, ppm: int) -> None:
        pass

    def regler_echantillonnage_direct(self, mode: int) -> None:
        pass

    def regler_bias_t(self, actif: bool) -> None:
        pass

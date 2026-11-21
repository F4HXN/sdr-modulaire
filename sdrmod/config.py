"""Réglages persistants (JSON dans ~/.config/sdr-modulaire)."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

log = logging.getLogger(__name__)

DOSSIER_CONFIG = Path.home() / ".config" / "sdr-modulaire"
FICHIER_REGLAGES = DOSSIER_CONFIG / "reglages.json"

# Taux d'échantillonnage proposés : tous multiples entiers de 48 kHz,
# ce qui permet une décimation entière jusqu'à l'audio.
TAUX_ECHANTILLONNAGE = [
    960_000, 1_200_000, 1_440_000, 1_536_000,
    1_920_000, 2_304_000, 2_400_000, 2_880_000,
]
TAUX_AUDIO = 48_000

PAS_ACCORD = [10, 100, 500, 1_000, 2_500, 5_000, 6_250, 8_330, 10_000,
              12_500, 25_000, 50_000, 100_000, 1_000_000]

TAILLES_FFT = [1024, 2048, 4096, 8192, 16384]


@dataclass
class Reglages:
    source: str = "rtlsdr"
    rtlsdr_serie: str = ""              # clé choisie (numéro de série) ; vide = première clé
    rtlsdr_index: int = 0               # position, pour départager deux numéros de série identiques
    centre: float = 145_300_000.0       # fréquence centrale du récepteur (Hz)
    decalage: float = 200_000.0         # position du VFO par rapport au centre (Hz)
    taux: int = 2_400_000
    gain: float | None = None           # None = AGC du tuner
    ppm: int = 0
    echantillonnage_direct: int = 0     # 0 = non, 1 = branche I, 2 = branche Q
    convertisseur: float = 0.0          # décalage d'un convertisseur HF en Hz (Ham It Up : 125e6), 0 sans
    bias_t: bool = False

    mode: str = "NFM"
    largeurs: dict = field(default_factory=dict)   # bande passante mémorisée par mode
    agc: bool = True
    squelch: float = -120.0             # dBFS ; -120 = ouvert
    volume: float = 0.5
    pas: int = 12_500

    taille_fft: int = 4096
    moyennage: float = 0.5
    niveau_min: float = -110.0
    niveau_max: float = -40.0

    modules_actifs: list = field(default_factory=list)
    geometrie: str = ""
    etat_fenetre: str = ""

    @property
    def frequence(self) -> float:
        return self.centre + self.decalage

    @classmethod
    def charger(cls) -> "Reglages":
        try:
            donnees = json.loads(FICHIER_REGLAGES.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return cls()
        except Exception as exc:  # fichier corrompu : on repart des valeurs par défaut
            log.warning("Réglages illisibles (%s), valeurs par défaut", exc)
            return cls()
        connus = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in donnees.items() if k in connus})

    def sauver(self) -> None:
        DOSSIER_CONFIG.mkdir(parents=True, exist_ok=True)
        tmp = FICHIER_REGLAGES.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(FICHIER_REGLAGES)

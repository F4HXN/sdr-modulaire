"""Vérifie chaque démodulateur sur la source simulée : la tonalité attendue doit dominer.

Lancer : python3 -m tests.test_dsp  (depuis la racine du projet)
"""

import time

import numpy as np

from sdrmod.bus import Bus
from sdrmod.config import TAUX_AUDIO
from sdrmod.demods import decouvrir
from sdrmod.moteur import Moteur
from sdrmod.sources import SourceSimulation

CENTRE = 145_300_000.0
# mode, décalage du VFO, tonalités attendues (Hz)
CAS = [
    ("NFM", +200_000, [1000]),
    ("AM", -250_000, [600]),
    ("USB", +412_500, [700, 1900]),
    ("LSB", +412_500 + 2600, [1900, 700]),   # LSB accordée au-dessus : tonalités inversées
    ("CW", -500_000, [700]),
    ("WFM", +700_000, [1000, 3000]),
]


def pics(audio, n=2):
    spec = np.abs(np.fft.rfft(audio * np.hanning(len(audio))))
    f = np.fft.rfftfreq(len(audio), 1 / TAUX_AUDIO)
    spec[f < 150] = 0
    idx = []
    for _ in range(n):
        i = int(np.argmax(spec))
        idx.append(round(f[i]))
        spec[max(0, i - 20): i + 20] = 0
    return idx


def main():
    modes = decouvrir()
    print("Modes découverts :", list(modes))
    ok = True
    for taux in (2_400_000, 1_536_000):
        for mode, decal, attendu in CAS:
            m = Moteur(Bus(), modes)
            m.centre, m.taux, m.decalage = CENTRE, taux, decal
            src = SourceSimulation()
            src.ouvrir(taux, CENTRE)
            src.temps_reel = False
            m._construire_chaine(mode, 0)
            t0 = time.perf_counter()
            blocs = [m.traiter_bloc(src.lire(m.n_bloc)) for _ in range(30)]
            duree = time.perf_counter() - t0
            audio = np.concatenate(blocs[10:])
            p = pics(audio, len(attendu))
            bon = all(any(abs(a - q) < 40 for q in p) for a in attendu)
            print(f"{taux/1e6:5.3f} MS/s {mode:4s} d1={m.d1:2d} d2={m.d2} pics={p} attendu={attendu} "
                  f"niveau={m.demod.niveau_dbfs:6.1f} dBFS  {'OK' if bon else 'ÉCHEC'}")
            ok &= bon
    # squelch : canal vide
    m = Moteur(Bus(), modes)
    m.centre, m.taux, m.decalage = CENTRE, 2_400_000, 50_000
    m.squelch = -60
    src = SourceSimulation(); src.ouvrir(2_400_000, CENTRE); src.temps_reel = False
    m._construire_chaine("NFM", 0)
    a = np.concatenate([m.traiter_bloc(src.lire(m.n_bloc)) for _ in range(5)])
    print("Squelch fermé sur canal vide :", "OK" if not a.any() else "ÉCHEC", f"(niveau {m.demod.niveau_dbfs:.1f})")
    ok &= not a.any()
    print("\nRÉSULTAT :", "TOUT OK" if ok else "ÉCHECS")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

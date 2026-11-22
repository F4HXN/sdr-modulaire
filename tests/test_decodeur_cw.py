"""Bout en bout : IQ avec du vrai Morse → moteur en mode CW → module Décodeur CW → texte.

python3 -m tests.test_decodeur_cw
"""

import importlib.util
import time
from pathlib import Path

import numpy as np

from sdrmod.bus import Bus
from sdrmod.demods import decouvrir
from sdrmod.moteur import Moteur

MORSE = {
    "A": ".-", "B": "-...", "C": "-.-.", "D": "-..", "E": ".", "F": "..-.", "G": "--.", "H": "....",
    "I": "..", "J": ".---", "K": "-.-", "L": ".-..", "M": "--", "N": "-.", "O": "---", "P": ".--.",
    "Q": "--.-", "R": ".-.", "S": "...", "T": "-", "U": "..-", "V": "...-", "W": ".--", "X": "-..-",
    "Y": "-.--", "Z": "--..", "0": "-----", "1": ".----", "2": "..---", "3": "...--", "4": "....-",
    "5": ".....", "6": "-....", "7": "--...", "8": "---..", "9": "----.", "/": "-..-.", "?": "..--..",
}
TEXTE = "CQ CQ DE F4HXN F4HXN JN33KW PSE K"
AGC_ACTIF = True        # comme en usage normal
FICHIER = Path(__file__).resolve().parent.parent / "modules-a-telecharger" / "decodeur_cw.py"


def manipulation(texte, wpm, fs, rng, jitter=0.08):
    u = 1.2 / wpm
    env = []
    def ajoute(val, unites):
        n = int(max(0.3, unites * (1 + jitter * rng.standard_normal())) * u * fs)
        env.append(np.full(n, val, np.float32))
    ajoute(0, 4)
    for i, mot in enumerate(texte.split()):
        if i:
            ajoute(0, 7)
        for j, c in enumerate(mot):
            if j:
                ajoute(0, 3)
            for k, el in enumerate(MORSE[c]):
                if k:
                    ajoute(0, 1)
                ajoute(1, 3 if el == "-" else 1)
    ajoute(0, 10)
    return np.concatenate(env)


def distance(a, b):
    d = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        prec, d[0] = d[0], i
        for j, cb in enumerate(b, 1):
            prec, d[j] = d[j], min(d[j] + 1, d[j - 1] + 1, prec + (ca != cb))
    return d[-1]


class Ctx:
    def __init__(self, bus):
        self.bus = bus
    def mode(self): return "CW"
    def frequence(self): return 7_020_000.0
    def regler_mode(self, m): pass
    def dossier_donnees(self, nom): return Path("/tmp")


def essai(wpm, snr_db, rng):
    spec = importlib.util.spec_from_file_location("decodeur_cw_test", FICHIER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    bus = Bus()
    m = Moteur(bus, decouvrir())
    taux, decal = 1_920_000, 150_000
    m.centre, m.taux, m.decalage = 7_000_000.0, taux, decal
    m._construire_chaine("CW", 500)
    m.agc_actif = AGC_ACTIF

    module = mod.DecodeurCW(Ctx(bus))
    module._abonnements = []
    module.ctx = Ctx(bus)
    module.demarrer()

    # manipulation à basse cadence puis suréchantillonnée (porteuse au décalage du VFO)
    fs_env = 8000
    env = np.concatenate([manipulation(TEXTE, wpm, fs_env, rng), np.zeros(int(7 * fs_env), np.float32)])
    n_total = int(len(env) / fs_env * taux)
    n_total -= n_total % m.n_bloc
    t_env = np.arange(n_total) / taux
    n = 0
    amp = 1e-2
    # SNR dans 2500 Hz : bruit complexe de densité uniforme sur toute la bande
    sigma2 = (amp ** 2) / (10 ** (snr_db / 10)) * taux / 2500
    while n < n_total:
        k = np.arange(n, n + m.n_bloc)
        e = np.interp(k / taux, np.arange(len(env)) / fs_env, env)
        porteuse = np.exp(2j * np.pi * decal * k / taux)
        bruit = (rng.standard_normal(m.n_bloc) + 1j * rng.standard_normal(m.n_bloc)) * np.sqrt(sigma2 / 2)
        m.traiter_bloc((amp * e * porteuse + bruit).astype(np.complex64))
        n += m.n_bloc
        while not module._file.empty():      # en direct, 3 s séparent deux fenêtres
            time.sleep(0.005)
    time.sleep(0.5)
    while not module._file.empty():
        time.sleep(0.1)
    time.sleep(0.3)
    module.arreter()
    texte = " ".join("".join(module.texte).split())
    return texte


def main():
    rng = np.random.default_rng(7)
    ok = True
    for wpm, snr in ((15, 15), (25, 5), (35, 10), (20, -3), (28, -7)):
        t0 = time.time()
        texte = essai(wpm, snr, rng)
        cer = distance(texte, TEXTE) / len(TEXTE)
        limite = snr < -5                    # au-delà des capacités du réseau : mesure indicative
        bon = cer < (0.05 if snr >= 0 else 0.10)
        if not limite:
            ok &= bon
        verdict = "limite" if limite else ("OK" if bon else "ÉCHEC")
        print(f"{wpm:2d} WPM  SNR {snr:+3d} dB  CER {cer * 100:5.1f}%  {verdict:6s}  "
              f"« {texte} »  ({time.time() - t0:.0f} s)")
    print("\nRÉSULTAT :", "TOUT OK" if ok else "ÉCHECS")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

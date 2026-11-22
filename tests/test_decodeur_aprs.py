"""Décodeur APRS sur trames AFSK 1200 synthétiques (bruit, pente entre tonalités).

python3 -m tests.test_decodeur_aprs
"""

import importlib.util
from pathlib import Path

import numpy as np
from scipy import signal

FICHIER = Path(__file__).resolve().parent.parent / "modules-a-telecharger" / "decodeur_aprs.py"
spec = importlib.util.spec_from_file_location("decodeur_aprs_test", FICHIER)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

FS = m.TAUX_AFSK


def adresse(ind: str, ssid: int, derniere: bool, h: bool = False) -> bytes:
    return bytes(ord(c) << 1 for c in ind.ljust(6)) + bytes([(0x80 if h else 0) | 0x60 | (ssid << 1) | int(derniere)])


def afsk(trame: bytes, rng) -> np.ndarray:
    c = m.crc_ax25(trame)
    bits = [(o >> i) & 1 for o in trame + bytes([c & 0xFF, c >> 8]) for i in range(8)]
    bourres, uns = [], 0
    for b in bits:
        bourres.append(b)
        uns = uns + 1 if b else 0
        if uns == 5:
            bourres.append(0)
            uns = 0
    fanion = [0, 1, 1, 1, 1, 1, 1, 0]
    niv, tons = 1, []
    for b in fanion * 30 + bourres + fanion * 2:
        niv ^= b == 0                                   # NRZI : 0 = changement de tonalité
        tons.append(niv)
    spb = FS / 1200 * (1 + rng.uniform(-0.01, 0.01))    # horloge de l'émetteur légèrement fausse
    f = np.array([1200.0 if tons[int(k / spb)] else 2200.0 for k in range(int(len(tons) * spb))])
    return np.sin(np.cumsum(2 * np.pi * f / FS) + rng.uniform(0, 6.3))


def main() -> int:
    rng = np.random.default_rng(5)
    corps = (adresse("APRS", 0, False) + adresse("F4HXN", 9, False) + adresse("WIDE1", 1, True, h=True)
             + b"\x03\xf0" + b"!4340.00N/00715.00E>Test SDR Modulaire")
    attendu = "F4HXN-9>APRS,WIDE1-1*:!4340.00N/00715.00E>Test SDR Modulaire"
    al = np.exp(-1 / (FS * 75e-6))
    ok = True
    for nom, pente, snr in (("propre", None, 30), ("bruit 6 dB", None, 6), ("désaccentuation", "des", 12),
                            ("préaccentuation", "pre", 12)):
        x = np.concatenate([np.zeros(FS // 4), afsk(corps, rng), np.zeros(FS // 4)])
        if pente == "des":
            x = signal.lfilter([1 - al], [1, -al], x)
        elif pente == "pre":
            x = signal.lfilter([1, -al], [1 - al], x)
        x = x / np.std(x) + rng.standard_normal(len(x)) * 10 ** (-snr / 20)
        d = m.DemodAFSK()
        textes = [m.trame_vers_texte(o) for i in range(0, len(x), 1200) for o in d.traiter(x[i:i + 1200])]
        bon = textes == [attendu]
        ok &= bon
        print(f"{nom:16s} {'OK' if bon else 'ÉCHEC'}  {textes}")
    print("\nRÉSULTAT :", "TOUT OK" if ok else "ÉCHECS")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

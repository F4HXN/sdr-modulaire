"""Vérifie le décodeur SSTV (modules-a-telecharger/sstv.py) sur des signaux synthétiques.

Chaque mode est généré avec son en-tête VIS et un écart d'horloge de 80 ppm (comme une clé
non corrigée), à deux niveaux de bruit : à 30 dB l'image doit être fidèle (couleurs, calage),
à 10 dB encore lisible. Écart moyen par pixel, sur 255.

Lancer : python3 -m tests.test_sstv  (depuis la racine du projet)
         python3 -m tests.test_sstv "Martin M1" PD120   (seulement ces modes)
"""

import importlib.util
import sys
import time
from pathlib import Path

import numpy as np

CHEMIN = Path(__file__).resolve().parent.parent / "modules-a-telecharger" / "sstv.py"
spec = importlib.util.spec_from_file_location("sstv", CHEMIN)
sstv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sstv)

TAUX = 48_000
RAPIDES = ["Martin M1", "Martin M2", "Scottie S1", "Scottie S2", "Robot 36", "Robot 72", "PD50", "PD90", "PD120"]


def image_essai(l, h):
    """Barres de couleur en haut, dégradés en bas : les erreurs de couleur et de calage se voient."""
    img = np.zeros((h, l, 3), np.uint8)
    barres = [(255, 255, 255), (255, 255, 0), (0, 255, 255), (0, 255, 0), (255, 0, 255), (255, 0, 0), (0, 0, 255), (0, 0, 0)]
    for i, c in enumerate(barres):
        img[: h // 2, i * l // 8:(i + 1) * l // 8] = c
    x = np.linspace(0, 255, l)
    img[h // 2:, :, 0] = x
    img[h // 2:, :, 1] = np.linspace(0, 255, h - h // 2)[:, None]
    img[h // 2:, :, 2] = 255 - x
    return img


def ycbcr(rgb):
    r, g, b = (rgb[..., i].astype(float) for i in range(3))
    y = 0.299 * r + 0.587 * g + 0.114 * b
    return y, 128 + (b - y) / 1.772, 128 + (r - y) / 1.402


def vers_hz(v):
    return 1500 + np.asarray(v, float) / 255 * 800


def sous_ech(c, n):
    return np.interp(np.linspace(0, len(c) - 1, n), np.arange(len(c)), c)


def segments(mode, img):
    """Liste (fréquence, durée ms) du signal complet : silence, VIS, lignes."""
    seg = [(0, 400), (1900, 300), (1200, 10), (1900, 300), (1200, 30)]
    bits = [(mode.vis >> k) & 1 for k in range(7)]
    bits.append(sum(bits) % 2)
    seg += [(1100 if b else 1300, 30) for b in bits] + [(1200, 30)]
    if mode.debut:
        seg.append((1200, mode.debut))
    y, cb, cr = ycbcr(img)
    for l in range(mode.lignes):
        evts = [(mode.pos_synchro, [(1200, mode.synchro)])]
        for nom, d, duree, px in mode.balayages:
            if nom == "sep":
                evts.append((d, [(2300 if l % 2 else 1500, duree)]))
                continue
            if mode.couleur == "RVB":
                v = img[l, :, {"R": 0, "V": 1, "B": 2}[nom]]
            elif mode.couleur == "PD":
                v = {"Y1": y[2 * l], "Y2": y[2 * l + 1], "Cb": (cb[2 * l] + cb[2 * l + 1]) / 2,
                     "Cr": (cr[2 * l] + cr[2 * l + 1]) / 2}[nom]
            elif nom == "Y":
                v = y[l]
            else:                                    # Robot : chrominance sur 160 px
                c = {"Cb": cb, "Cr": cr, "C": cb if l % 2 else cr}[nom][l]
                v = sous_ech(c, px)
            evts.append((d, [(f, duree / px) for f in vers_hz(v)]))
        t = 0.0
        for d, s in sorted(evts, key=lambda e: e[0]):
            if d > t + 1e-9:
                seg.append((1500, d - t))            # palier (noir)
            seg += s
            t = d + sum(x[1] for x in s)
        if mode.ligne > t + 1e-9:
            seg.append((1500, mode.ligne - t))
    seg.append((0, 500))
    return seg


def synthese(mode, img, ppm=80.0, snr_db=10.0, graine=1):
    f = np.array([s[0] for s in segments(mode, img)], float)
    d = np.array([s[1] for s in segments(mode, img)], float) / 1000
    bornes = np.concatenate([[0], np.cumsum(d)])
    taux = TAUX * (1 + ppm * 1e-6)                  # horloge de l'émetteur décalée
    t = np.arange(int(bornes[-1] * taux)) / taux
    fi = f[np.searchsorted(bornes, t, side="right") - 1]
    x = 0.5 * np.sin(np.cumsum(2 * np.pi * fi / taux)) * (fi > 0)
    rng = np.random.default_rng(graine)
    x += rng.normal(0, 0.5 / np.sqrt(2) / 10 ** (snr_db / 20), len(x))
    return x.astype(np.float32)


def essayer(nom, snr_db, seuil):
    mode = sstv.PAR_NOM[nom]
    img = image_essai(mode.largeur, mode.hauteur)
    audio = synthese(mode, img, snr_db=snr_db)
    d = sstv.DecodeurSSTV(TAUX)
    t0 = time.perf_counter()
    for i in range(0, len(audio), 2400):            # blocs de 50 ms, comme le moteur
        d.traiter(audio[i:i + 2400])
    duree = time.perf_counter() - t0
    if not d.terminees:
        return False, f"aucune image ({d.message})"
    recue, mode_recu, _, partielle = d.terminees[-1]
    marge = 2                                         # bords : transitions de couleur
    ecart = np.abs(recue.astype(int) - img.astype(int))[marge:-marge, marge:-marge].mean()
    ok = mode_recu == nom and not partielle and ecart < seuil
    vitesse = len(audio) / TAUX / duree
    return ok, f"{snr_db:2.0f} dB : écart {ecart:5.1f} (< {seuil}), {vitesse:3.0f}× le temps réel"


def main() -> int:
    noms = sys.argv[1:] or RAPIDES
    ok = True
    for nom in noms:
        for snr_db, seuil in ((30, 6), (10, 18)):
            bon, detail = essayer(nom, snr_db, seuil)
            print(f"{nom:12} {'OK   ' if bon else 'ÉCHEC'} {detail}")
            ok &= bon
    print("\nRÉSULTAT :", "TOUT OK" if ok else "ÉCHECS")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

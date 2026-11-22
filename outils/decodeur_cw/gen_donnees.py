"""Génère des lots d'exemples : python3 gen_donnees.py <premier_lot> <nb_lots>"""

import sys
from pathlib import Path

import numpy as np

from cw_commun import INDEX, pretraiter
from synthese import exemple

TAILLE_LOT = 2000
DOSSIER = Path("donnees")


def lot(numero: int) -> None:
    sortie = DOSSIER / f"lot_{numero:03d}.npz"
    if sortie.exists():
        return
    rng = np.random.default_rng(1000 + numero)
    cars, etiquettes, longueurs, snr = [], [], [], []
    for _ in range(TAILLE_LOT):
        x, texte, info = exemple(rng)
        c, _ = pretraiter(x)
        cars.append(c.astype(np.float16))
        etiquettes.extend(INDEX[ch] for ch in texte)
        longueurs.append(len(texte))
        snr.append(info["snr"])
    np.savez(sortie, cars=np.stack(cars), etiquettes=np.array(etiquettes, np.int16),
             longueurs=np.array(longueurs, np.int16), snr=np.array(snr, np.float32))
    print("fini", sortie, flush=True)


if __name__ == "__main__":
    DOSSIER.mkdir(exist_ok=True)
    premier, nb = int(sys.argv[1]), int(sys.argv[2])
    for i in range(premier, premier + nb):
        lot(i)

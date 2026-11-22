"""Entraînement CTC : python3 entrainer.py <minutes>"""

import glob
import sys
import time

import numpy as np
import torch
import torch.nn.functional as F

from cw_commun import ALPHABET, INDEX, decoder_ctc, pretraiter
from modele import construire_torch
from synthese import exemple

torch.set_num_threads(2)
torch.manual_seed(0)


def distance(a: str, b: str) -> int:
    d = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        prec, d[0] = d[0], i
        for j, cb in enumerate(b, 1):
            prec, d[j] = d[j], min(d[j] + 1, d[j - 1] + 1, prec + (ca != cb))
    return d[-1]


def charger():
    cars, etiq, snr = [], [], []
    for f in sorted(glob.glob("donnees/lot_*.npz")):
        z = np.load(f)
        cars.append(z["cars"])
        e, off = z["etiquettes"], 0
        for n in z["longueurs"]:
            etiq.append(e[off:off + n].astype(np.int64))
            off += n
        snr.append(z["snr"])
    return np.concatenate(cars), etiq, np.concatenate(snr)


def jeu_validation():
    rng = np.random.default_rng(999)
    jeu = []
    for s in (-10, -6, -3, 0, 5, 10, 20):
        for _ in range(60):
            x, texte, _ = exemple(rng, snr_db=s)
            c, _ = pretraiter(x)
            jeu.append((s, c, texte))
    return jeu


def evaluer(modele, jeu):
    modele.eval()
    par_snr = {}
    with torch.no_grad():
        for s, c, texte in jeu:
            sortie = modele(torch.from_numpy(c[None]))[0].numpy()
            pred = "".join(ch for _, ch in decoder_ctc(sortie)).strip()
            e, n = par_snr.get(s, (0, 0))
            par_snr[s] = (e + distance(pred, texte), n + max(1, len(texte)))
    modele.train()
    return {s: e / n for s, (e, n) in par_snr.items()}


def main():
    minutes = float(sys.argv[1]) if len(sys.argv) > 1 else 30
    cars, etiq, _ = charger()
    print(f"{len(cars)} exemples, {cars.shape}", flush=True)
    jeu = jeu_validation()
    modele = construire_torch()
    if len(sys.argv) > 2:                       # reprise depuis un réseau déjà entraîné
        modele.load_state_dict(torch.load(sys.argv[2]))
        print("reprise depuis", sys.argv[2], flush=True)
    print("paramètres :", sum(p.numel() for p in modele.parameters()), flush=True)
    opt = torch.optim.AdamW(modele.parameters(), lr=2e-3, weight_decay=1e-4)
    ctc = torch.nn.CTCLoss(blank=0, zero_infinity=True)
    lot = 32
    debut = time.time()
    etape, meilleur = 0, 9.0
    lr_max, duree = (1e-3 if len(sys.argv) > 2 else 1.5e-3), minutes * 60
    while time.time() - debut < duree:
        idx = np.random.randint(0, len(cars), lot)
        x = torch.from_numpy(cars[idx].astype(np.float32))
        # augmentation légère : gain et décalage dans l'espace normalisé
        x = x * torch.empty(lot, 1, 1).uniform_(0.8, 1.2) + torch.empty(lot, 1, 1).uniform_(-0.3, 0.3)
        cibles = [torch.from_numpy(etiq[i]) for i in idx]
        lc = torch.tensor([len(c) for c in cibles])
        cible = torch.cat(cibles) if lc.sum() else torch.zeros(0, dtype=torch.long)
        logits = modele(x)                                     # (B, T', C)
        lp = F.log_softmax(logits, dim=2).transpose(0, 1)      # (T', B, C)
        li = torch.full((lot,), lp.shape[0], dtype=torch.long)
        perte = ctc(lp, cible, li, lc)
        avancement = (time.time() - debut) / duree
        for g in opt.param_groups:                             # montée puis cosinus
            g["lr"] = lr_max * min(1.0, etape / (50 if len(sys.argv) > 2 else 300)) * (0.5 + 0.5 * np.cos(np.pi * min(avancement, 1)))
        opt.zero_grad()
        perte.backward()
        torch.nn.utils.clip_grad_norm_(modele.parameters(), 5.0)
        opt.step()
        etape += 1
        if etape % 250 == 0:
            cer = evaluer(modele, jeu)
            moy = float(np.mean(list(cer.values())))
            print(f"étape {etape:5d}  {time.time() - debut:6.0f} s  perte {perte.item():.3f}  CER "
                  + " ".join(f"{s:+d}dB:{v * 100:5.1f}%" for s, v in sorted(cer.items()))
                  + f"  moy {moy * 100:.1f}%", flush=True)
            if moy < meilleur:
                meilleur = moy
                torch.save(modele.state_dict(), "meilleur.pt")
    print("fin, meilleur CER moyen", meilleur, flush=True)


if __name__ == "__main__":
    main()

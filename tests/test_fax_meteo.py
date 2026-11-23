"""Fax météo (WEFAX) sur des émissions synthétiques.

Émission : signal de départ (300 Hz, 5 s), 60 lignes de phasage (impulsion blanche de 5 % au bord gauche),
une carte d'essai (barres verticales et dégradé), signal d'arrêt (450 Hz, 5 s) ; l'horloge de l'émetteur
est décalée de +150 ppm (la carte penche si la pente n'est pas corrigée) et le début des lignes tombe
n'importe où. On vérifie la carte décodée point par point, puis la chaîne complète du module (IQ en HF).

    QT_QPA_PLATFORM=offscreen python3 -m tests.test_fax_meteo [carte.png]
"""

import importlib.util
import sys
from pathlib import Path

import numpy as np

RACINE = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("fax_meteo", RACINE / "modules-a-telecharger" / "fax_meteo.py")
M = importlib.util.module_from_spec(spec)
spec.loader.exec_module(M)

FS = M.TAUX_FAX
PPM = 150.0
N_IMAGE = 120


def carte_essai() -> np.ndarray:
    """Barres verticales de 100 points (noir/blanc) en haut, dégradé gauche → droite en bas."""
    x = np.arange(M.LARGEUR)
    barres = ((x // 100) % 2).astype(float)
    degrade = x / (M.LARGEUR - 1)
    return np.array([barres if k < N_IMAGE // 2 else degrade for k in range(N_IMAGE)])


def valeurs_emises(avance: float) -> tuple[np.ndarray, np.ndarray]:
    """Niveaux 0 (noir) … 1 (blanc) à FS, et les lignes de la carte (phasage compris) attendues."""
    ligne = M.LIGNE * (1 + PPM * 1e-6)
    morceaux = [np.zeros(int(avance * FS))]
    t = np.arange(5 * FS) / FS
    morceaux.append((np.floor(2 * 300 * t) % 2).astype(float))          # départ : alternance à 300 Hz
    phasage = np.zeros(M.LARGEUR)
    phasage[: M.LARGEUR // 20] = 1.0                                     # impulsion blanche, 5 % de la ligne
    rangees = [phasage] * 60 + list(carte_essai())
    n_total = int(round(len(rangees) * ligne))
    pos = np.arange(n_total) / ligne                                     # position en lignes
    k = np.minimum(pos.astype(int), len(rangees) - 1)
    x = np.minimum(((pos - k) * M.LARGEUR).astype(int), M.LARGEUR - 1)
    morceaux.append(np.array(rangees)[k, x])
    t = np.arange(5 * FS) / FS
    morceaux.append((np.floor(2 * 450 * t) % 2).astype(float))          # arrêt : alternance à 450 Hz
    morceaux.append(np.zeros(4 * FS))
    return np.concatenate(morceaux), np.array(rangees)


def bande_base(v: np.ndarray, cn_db: float, rng) -> np.ndarray:
    """FSK ±400 Hz autour de la fréquence annoncée, à FS, bruit sur 3 kHz pour un C/N donné."""
    f = -M.ECART_HZ + 2 * M.ECART_HZ * v
    z = np.exp(2j * np.pi * np.cumsum(f) / FS)
    sigma = np.sqrt(10 ** (-cn_db / 10) * FS / 3000 / 2)
    return z + sigma * (rng.normal(size=len(z)) + 1j * rng.normal(size=len(z)))


def comparer(carte: np.ndarray, attendu: np.ndarray) -> tuple[float, int]:
    """Écart moyen (0…1) sur les lignes de la carte d'essai, au meilleur décalage de ±2 lignes."""
    c = carte.astype(float) / 255
    meilleur = (9.0, 0)
    for d in range(-2, 3):
        e = []
        for k in range(60 + 5, 60 + N_IMAGE - 5):
            if 0 <= k + d < len(c):
                e.append(np.mean(np.abs(c[k + d, 20:-20] - attendu[k, 20:-20])))
        if e:
            meilleur = min(meilleur, (float(np.mean(e)), d))
    return meilleur


def lire_png(chemin) -> np.ndarray:
    from PySide6.QtGui import QImage
    q = QImage(str(chemin)).convertToFormat(QImage.Format.Format_Grayscale8)
    b = np.frombuffer(q.constBits(), np.uint8).reshape(q.height(), q.bytesPerLine())
    return b[:, :q.width()].copy()


def essais_moteur(png: str | None) -> list[str]:
    j = []
    for cn in (25, 10):
        rng = np.random.default_rng(cn)
        v, attendu = valeurs_emises(rng.uniform(0.3, 2.0))
        z = bande_base(v, cn, rng)
        d = M.DecodeurFax()
        etats = set()
        for k in range(0, len(z), 2400):
            d.traiter(z[k:k + 2400])
            etats.add(d.statut().split(" · ")[0])
        cartes = d.terminees
        if not cartes:
            j.append(f"ÉCHEC  C/N {cn} dB : aucune carte ({sorted(etats)})")
            continue
        carte, partielle = cartes[0]
        ecart, dec = comparer(carte, attendu)
        j.append(("OK" if len(cartes) == 1 and not partielle else "ÉCHEC")
                 + f"  C/N {cn} dB : une carte complète, close au signal d'arrêt ({len(carte)} lignes)")
        j.append(("OK" if ecart < 0.08 else "ÉCHEC") + f"  C/N {cn} dB : carte fidèle (écart moyen {ecart:.3f}, décalage {dec} ligne)")
        if cn == 25 and png:
            M.FaxMeteoModule._qimage(carte).save(png, "PNG")
    # pente : mesurée au phasage
    rng = np.random.default_rng(3)
    v, _ = valeurs_emises(0.7)
    d = M.DecodeurFax()
    z = bande_base(v, 25, rng)
    pente = None
    for k in range(0, len(z), 2400):
        d.traiter(z[k:k + 2400])
        if d.en_cours and d.cale and pente is None:
            pente = d.pente_ppm
    j.append(("OK" if pente is not None and abs(pente - PPM) < 40 else "ÉCHEC")
             + f"  pente mesurée au phasage : {pente if pente is None else round(pente)} ppm (émetteur +{PPM:.0f} ppm)")
    # inversion (LSB) : sans « Inverser », pas de signal de départ reconnu… le départ est une alternance, il l'est quand
    # même ; la carte sort en négatif. Avec « Inverser », elle est juste.
    d = M.DecodeurFax()
    d.inverse = True
    zl = np.conj(bande_base(v, 25, rng))           # LSB : spectre retourné
    for k in range(0, len(zl), 2400):
        d.traiter(zl[k:k + 2400])
    ok_ = bool(d.terminees) and comparer(d.terminees[0][0], valeurs_emises(0.7)[1])[0] < 0.08
    j.append(("OK" if ok_ else "ÉCHEC") + "  station en LSB avec « Inverser » : carte juste")
    # bruit seul : pas de carte
    d = M.DecodeurFax()
    rng = np.random.default_rng(9)
    b = rng.normal(size=60 * FS) + 1j * rng.normal(size=60 * FS)
    for k in range(0, len(b), 2400):
        d.traiter(b[k:k + 2400])
    j.append(("OK" if not d.terminees and not d.en_cours else "ÉCHEC") + "  bruit seul (60 s) : aucune carte commencée")
    # démarrage à la main puis bord gauche cliqué
    d = M.DecodeurFax()
    v, attendu = valeurs_emises(0.0)
    z = bande_base(v, 30, np.random.default_rng(4))
    debut_carte = int((5 + 60 * 0.5 * (1 + PPM * 1e-6)) * FS)     # après le phasage, au milieu d'une ligne
    for k in range(0, debut_carte + 1700, 100):
        d.traiter(z[k:k + 100])
    d.reinitialiser()
    d.demarrer_maintenant()
    d.regler_pente(PPM)
    for k in range(debut_carte + 1700, debut_carte + 1700 + 20 * M.LIGNE, 2400):
        d.traiter(z[k:k + 2400])
    img = d.image()
    # bord réel : début de ligne de l'émetteur, ~1700 échantillons après le départ manuel → fraction
    d.regler_bord_gauche(1 - (1700 % M.LIGNE) / M.LIGNE)
    img2 = d.image()
    att = attendu[60:60 + len(img2)]
    e = min(np.mean(np.abs(img2[k, 20:-20] / 255 - att[min(k + s, len(att) - 1), 20:-20]))
            for k in range(2, len(img2) - 2) for s in (0, 1)) if img is not None and img2 is not None else 9
    j.append(("OK" if e < 0.1 else "ÉCHEC") + f"  démarrage à la main + clic sur le bord gauche : carte recalée ({e:.3f})")
    return j


def chaine_complete() -> list[str]:
    """IQ à 960 kHz centrées sur 7,7 MHz, station DWD 7 880 kHz : module complet, carte enregistrée en PNG."""
    import os
    import tempfile
    import time as horloge
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from sdrmod.bus import Bus
    app = QApplication.instance() or QApplication([])
    dossier = Path(tempfile.mkdtemp())
    reglages = {"f": 7_700_000.0, "mode": "AM"}

    class Ctx:
        bus = Bus()
        def frequence(self): return reglages["f"]
        def taux(self): return 960_000
        def regler_mode(self, m): reglages["mode"] = m
        def regler_frequence(self, f): reglages["f"] = f
        def dossier_donnees(self, nom): return dossier

    taux, centre, station = 960_000, 7_700_000, 7_880_000
    rng = np.random.default_rng(6)
    v, attendu = valeurs_emises(0.5)
    v = v[: int((5.5 + (60 + 40) * 0.5 * (1 + PPM * 1e-6)) * FS)]      # départ, phasage, 40 lignes de carte
    mod = M.FaxMeteoModule(Ctx())
    mod.demarrer()
    w = mod.creer_widget()
    mod.c_station.setCurrentIndex(1)
    phase, bloc = 0.0, 48_000                                       # 50 ms à 960 kHz
    n = int(len(v) / FS * taux)
    for a in range(0, n, bloc):
        idx = np.minimum(((a + np.arange(bloc)) * FS // taux), len(v) - 1)
        f = (station - centre) - M.ECART_HZ + 2 * M.ECART_HZ * v[idx]
        ph = phase + 2 * np.pi * np.cumsum(f) / taux
        phase = ph[-1] % (2 * np.pi)
        iq = (0.1 * np.exp(1j * ph) + 0.01 * (rng.normal(size=bloc) + 1j * rng.normal(size=bloc))).astype(np.complex64)
        mod.ctx.bus.publier("iq", iq, centre, taux)
        while not mod.blocs.empty():
            horloge.sleep(0.005)
        if a % (20 * bloc) == 0:
            app.processEvents()
            mod._rafraichir()
    horloge.sleep(0.3)
    mod._rafraichir()
    statut, diag = mod.statut.text(), mod.diag.text()
    mod.decodeur.terminer_maintenant()
    mod._rafraichir()
    pngs = list(dossier.glob("fax_*7880kHz_partielle.png"))
    mod.accorder()
    mod.arreter()
    w.deleteLater()
    print("--- chaîne complète :", statut, "|", diag, "|", [p.name for p in pngs])
    ecart = comparer(lire_png(pngs[0]), attendu)[0] if pngs else 9
    return [("OK" if "Carte en cours" in statut and "calée" in statut else "ÉCHEC") + f"  chaîne complète : {statut}",
            ("OK" if pngs and ecart < 0.1 else "ÉCHEC") + f"  chaîne complète : PNG enregistré et fidèle ({ecart:.3f})",
            ("OK" if reglages == {"f": 7_878_100, "mode": "USB"} else "ÉCHEC") + f"  Accorder : {reglages} (USB, 1,9 kHz sous 7 880)"]


def main() -> int:
    journal = essais_moteur(sys.argv[1] if len(sys.argv) > 1 else None) + chaine_complete()
    print("\n".join(journal))
    tout = all(not l.startswith("ÉCHEC") for l in journal)
    print("\nRÉSULTAT :", "TOUT OK" if tout else "ÉCHECS")
    return 0 if tout else 1


if __name__ == "__main__":
    raise SystemExit(main())

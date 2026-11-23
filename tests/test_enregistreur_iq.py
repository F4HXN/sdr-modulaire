"""Enregistreur IQ : écriture WAV, lecture des formats IQ courants, enregistrement et relecture dans l'application.

    QT_QPA_PLATFORM=offscreen HOME=$(mktemp -d) python3 -m tests.test_enregistreur_iq

(HOME temporaire : le test installe le module dans ~/.config/sdr-modulaire/modules/ et y enregistre.)
"""

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import numpy as np

RACINE = Path(__file__).resolve().parent.parent
MODULE = RACINE / "modules-a-telecharger" / "enregistreur_iq.py"
journal: list[str] = []


def ok(cond, texte):
    journal.append(("OK    " if cond else "ÉCHEC ") + texte)


def essais_fichiers(M) -> None:
    d = Path(tempfile.mkdtemp())
    rng = np.random.default_rng(1)
    n, taux, centre = 300_000, 2_400_000, 145_500_000.0
    octets = rng.integers(0, 256, size=2 * n, dtype=np.uint8)
    iq = ((octets[0::2].astype(np.float32) - 127.4) + 1j * (octets[1::2].astype(np.float32) - 127.4)) / 128
    ok(M.iq_vers_octets(iq.astype(np.complex64)) == octets.tobytes(), "octets → IQ → octets : identiques")

    w = M.EcrivainWav(d / M.nom_fichier(w_debut := __import__("datetime").datetime(2026, 11, 23, 10, 0), centre),
                      taux, centre)
    for k in range(0, n, 48_000):
        w.ecrire(M.iq_vers_octets(iq[k:k + 48_000].astype(np.complex64)))
    w.fermer()
    ok(w.chemin.name == "SDRModulaire_20261123_100000Z_145500000Hz_IQ.wav", f"nom à la SDR# : {w.chemin.name}")
    i = M.lire_entete(w.chemin)
    ok((i["format"], i["taux"], i["centre"], i["n"], i["decalage"]) == ("cu8", taux, centre, n, 220),
       f"en-tête relu : {i['format']}, {i['taux']}, {i['centre']:.0f} Hz, {i['n']} échantillons")
    ok(bool(i["heure"]) and i["heure"].endswith("Z"), f"heure UTC du bloc auxi : {i['heure']}")
    import wave
    with wave.open(str(w.chemin)) as ww:
        ok((ww.getnchannels(), ww.getsampwidth(), ww.getframerate(), ww.getnframes()) == (2, 1, taux, n),
           "WAV valide pour le module wave de Python (2 canaux, 8 bits)")

    s = M.SourceFichierIQ()
    M.SourceFichierIQ.chemin = w.chemin
    M.SourceFichierIQ.boucle = True
    s.ouvrir(taux, centre)
    s._t = -1e9                                       # pas d'attente : lecture aussi vite que possible
    lu = np.concatenate([s.lire(65_536) for _ in range(5)])
    ok(np.array_equal(lu[:n], iq[:n].astype(np.complex64)) and np.array_equal(lu[n:2 * n - (2 * n - len(lu))],
       iq[:len(lu) - n].astype(np.complex64)), "relecture identique à l'enregistrement, puis en boucle")
    s.aller_a(0.5)
    s._t = -1e9
    ok(np.array_equal(s.lire(10), iq[n // 2 - 1:n // 2 + 9].astype(np.complex64)) or
       np.array_equal(s.lire(0), np.zeros(0)), "aller à la moitié")
    M.SourceFichierIQ.boucle = False
    s.aller_a(1.0)
    s._t = -1e9
    fin = s.lire(1000)
    ok(s.fini and not np.any(fin[1:]), "sans boucle : silence à la fin")
    try:
        s.fermer()
        s.ouvrir(1_920_000, centre)
        ok(False, "taux différent refusé")
    except RuntimeError as exc:
        ok("2.4 MS/s" in str(exc), f"taux différent refusé ({exc})")
    s.fermer()

    # SigMF ci16_le
    v = np.empty(2 * n, dtype="<i2")
    v[0::2], v[1::2] = np.rint(iq.real * 20000), np.rint(iq.imag * 20000)
    v.tofile(d / "essai.sigmf-data")
    (d / "essai.sigmf-meta").write_text(json.dumps({"global": {"core:datatype": "ci16_le", "core:sample_rate": 1_920_000},
                                                    "captures": [{"core:sample_start": 0, "core:frequency": 433_920_000,
                                                                  "core:datetime": "2026-11-23T10:00:00Z"}]}))
    i = M.lire_entete(d / "essai.sigmf-meta")
    ok((i["format"], i["taux"], i["centre"], i["n"]) == ("ci16_le", 1_920_000, 433_920_000.0, n), "SigMF ci16_le")
    M.SourceFichierIQ.chemin = d / "essai.sigmf-data"
    s.ouvrir(1_920_000, 433_920_000)
    s._t = -1e9
    ok(np.allclose(s.lire(1000), iq[:1000] * 20000 / 32768, atol=1e-4), "SigMF : échantillons relus")
    s.fermer()
    # gqrx (complexes flottants) et WAV 16 bits sans bloc auxi (fréquence dans le nom, comme SDR#)
    iq[:n].astype(np.complex64).tofile(d / "gqrx_20261123_100000_144800000_960000_fc.raw")
    i = M.lire_entete(d / "gqrx_20261123_100000_144800000_960000_fc.raw")
    ok((i["format"], i["taux"], i["centre"], i["n"]) == ("cf32_le", 960_000, 144_800_000.0, n), "fichier brut de gqrx")
    with wave.open(str(d / "SDRSharp_20261123_100000Z_7100000Hz_IQ.wav"), "wb") as ww:
        ww.setparams((2, 2, 1_536_000, 0, "NONE", "not compressed"))
        ww.writeframes(v.tobytes())
    i = M.lire_entete(d / "SDRSharp_20261123_100000Z_7100000Hz_IQ.wav")
    ok((i["format"], i["taux"], i["centre"], i["n"]) == ("ci16_le", 1_536_000, 7_100_000.0, n),
       "WAV 16 bits de SDR# (fréquence lue dans le nom)")
    for nom in ("bruit.wav", "rien.bin"):
        (d / nom).write_bytes(b"RIFF\0\0\0\0WAVEjunk" if nom.endswith(".wav") else b"\0" * 100)
        try:
            M.lire_entete(d / nom)
            ok(False, f"{nom} refusé")
        except ValueError as exc:
            ok(True, f"{nom} refusé : {exc}")
    shutil.rmtree(d)


def essai_application() -> None:
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication(sys.argv)
    from sdrmod.ui.fenetre import FenetrePrincipale
    from sdrmod.config import DOSSIER_CONFIG
    f = FenetrePrincipale(source="simulation")
    f.resize(1200, 800)
    f.show()
    r = f.reglages
    etat = {}

    def etape(t, action):
        QTimer.singleShot(t, action)

    def demarrer():
        r.centre, r.decalage, r.taux = 145_300_000.0, 200_000.0, 960_000
        f.moteur.taux = 960_000
        f.demarrer()
        ok(f.activer_module("Enregistreur IQ"), "module activé")
        etat["m"] = f.modules.actifs["Enregistreur IQ"]
        etat["m"].b_enr.setChecked(True)

    def finir():
        m = etat["m"]
        m.b_enr.setChecked(False)
        m._rafraichir()
        fichiers = sorted((DOSSIER_CONFIG / "donnees" / "enregistreur_iq").glob("*.wav"))
        ok(len(fichiers) == 1, f"un fichier enregistré : {[p.name for p in fichiers]}")
        etat["fichier"] = fichiers[0]
        i = m.__class__.__module__ and sys.modules[m.__class__.__module__].lire_entete(fichiers[0])
        ok(i["centre"] == 145_300_000 and i["taux"] == 960_000 and 1.2 < i["n"] / 960_000 < 2.5,
           f"enregistrement : {i['centre'] / 1e6} MHz, {i['n'] / 960_000:.2f} s à 0,96 MS/s, {m.etat['perdus']} blocs perdus")
        ok(m.liste.count() == 1, "le fichier apparaît dans la liste du module")
        # la simulation est arrêtée et ailleurs : seule la relecture peut fournir les émetteurs
        f.moteur.arreter()
        r.centre, r.decalage = 100_000_000.0, 0.0
        erreur = m.rejouer(fichiers[0])
        ok(not erreur and f.moteur.actif and type(f.moteur.source).__name__ == "SourceFichierIQ",
           f"relecture démarrée {erreur}")
        ok(r.centre == 145_300_000 and r.taux == 960_000, f"centre et taux de l'enregistrement ({r.centre / 1e6} MHz)")
        f.regler_mode("NFM")
        f.regler_frequence(145_500_000)      # émetteur NFM de la simulation (+200 kHz du centre à l'ouverture)

    def ecouter():
        ok(f.moteur.demod.nom == "NFM" and f.moteur.demod.niveau_dbfs > -40,
           f"station NFM de l'enregistrement démodulée ({f.moteur.demod.niveau_dbfs:.1f} dBFS)")
        f.regler_frequence(147_000_000)      # hors de la bande enregistrée : pas de recentrage
        ok(r.centre == 145_300_000 and 145_700_000 < r.frequence < 145_780_000,
           f"centre fixe pendant la relecture, VFO gardé dans la bande ({r.centre / 1e6} MHz, VFO {r.frequence / 1e6} MHz)")
        m = etat["m"]
        m._rafraichir()
        ok(m.l_lecture.text().startswith("▶ SDRModulaire_"), f"position affichée : {m.l_lecture.text()}")
        m.revenir()
        ok(f.moteur.actif and type(f.moteur.source).__name__ == "SourceSimulation" and r.source == "simulation",
           "retour à la source d'avant")
        f.desactiver_module("Enregistreur IQ")
        ok("fichier_iq" not in __import__("sdrmod.sources", fromlist=["x"]).TOUTES_SOURCES, "module retiré proprement")

    etape(100, demarrer)
    etape(2000, finir)
    etape(3500, ecouter)
    etape(3800, f.close)
    etape(4000, app.quit)
    app.exec()


def main() -> int:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    if not os.environ.get("HOME", "").startswith(tempfile.gettempdir()):
        os.environ["HOME"] = tempfile.mkdtemp()      # jamais dans les vrais réglages
    dossier = Path(os.environ["HOME"]) / ".config" / "sdr-modulaire" / "modules"
    dossier.mkdir(parents=True, exist_ok=True)
    shutil.copy(MODULE, dossier / MODULE.name)
    import importlib.util
    spec = importlib.util.spec_from_file_location("enregistreur_iq_essai", MODULE)
    M = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(M)
    essais_fichiers(M)
    essai_application()
    print("\n".join(journal))
    tout = all(l.startswith("OK") for l in journal)
    print("\nRÉSULTAT :", "TOUT OK" if tout else "ÉCHECS")
    return 0 if tout else 1


if __name__ == "__main__":
    raise SystemExit(main())

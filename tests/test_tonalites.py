"""Tonalités CTCSS et DTMF sur des émissions NFM synthétiques.

Scénario (48 kHz, discriminateur FM en Hz) :
  0–1,5 s   porteuse seule (bruit) ;
  1,5–9 s   CTCSS 88,5 Hz (déviation 500 Hz) + « voix » (bruit 300–3 000 Hz, préaccentué), et pendant ce
            temps : DTMF « 1234#*0D » (70 ms par touche, 60 ms de silence), puis 2,5 s plus tard « 55 » (touche répétée) ;
  9,5–11 s  tonalité 1750 Hz seule (1 s) ;
  11–17 s   CTCSS 151,4 Hz (voisine de 150,0) puis 156,7 Hz : tonalités proches à ne pas confondre.

    python3 -m tests.test_tonalites            # moteur seul + chaîne complète (IQ → module)
    python3 -m tests.test_tonalites fm.f32     # écrit aussi le discriminateur (float32) pour web/tonalites.js
"""

import importlib.util
import sys
from pathlib import Path

import numpy as np
from scipy import signal

RACINE = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("tonalites", RACINE / "modules-a-telecharger" / "tonalites.py")
M = importlib.util.module_from_spec(spec)
spec.loader.exec_module(M)

TAUX = M.TAUX
DTMF = {t: (M.DTMF_BAS[i], M.DTMF_HAUT[j]) for i, l in enumerate(M.TOUCHES) for j, t in enumerate(l)}


def modulant(rng) -> np.ndarray:
    """Signal modulant en Hz de déviation."""
    n = 17 * TAUX
    t = np.arange(n) / TAUX
    m = np.zeros(n)
    voix = signal.lfilter(*signal.butter(4, [300, 3000], "bandpass", fs=TAUX), rng.normal(size=n))
    voix = np.diff(voix, prepend=0) * TAUX / (2 * np.pi * 1000)          # préaccentuation 6 dB/oct.
    voix *= 1500 / np.std(voix)
    sel = (t >= 1.5) & (t < 9)
    m[sel] += 500 * np.sin(2 * np.pi * 88.5 * t[sel])
    parle = sel & ((t < 3) | (t > 7.2))                    # la voix s'arrête pendant les codes DTMF
    m[parle] += voix[parle]
    debut = 3.2
    for seq in ("1234#*0D", "55"):
        for c in seq:
            fb, fh = DTMF[c]
            k = (t >= debut) & (t < debut + 0.07)
            m[k] += 1500 * np.sin(2 * np.pi * fb * t[k]) + 1800 * np.sin(2 * np.pi * fh * t[k])
            debut += 0.13
        debut += 2.5
    k = (t >= 9.5) & (t < 10.5)
    m[k] += 3000 * np.sin(2 * np.pi * 1750 * t[k])
    k = (t >= 11) & (t < 14)
    m[k] += 600 * np.sin(2 * np.pi * 151.4 * t[k])
    k = (t >= 14) & (t < 17)
    m[k] += 600 * np.sin(2 * np.pi * 156.7 * t[k])
    return m


def bande_base(snr_db: float, rng) -> np.ndarray:
    """NFM complexe à 48 kHz, bruit dans la bande pour un C/N donné (sur 12,5 kHz)."""
    phase = 2 * np.pi * np.cumsum(modulant(rng)) / TAUX
    s = np.exp(1j * phase)
    sigma = np.sqrt(10 ** (-snr_db / 10) * TAUX / 12_500 / 2)
    return s + sigma * (rng.normal(size=len(s)) + 1j * rng.normal(size=len(s)))


def discriminateur(z: np.ndarray) -> np.ndarray:
    z = signal.lfilter(signal.firwin(101, 7_000, fs=TAUX), 1, z)
    return (np.angle(z[1:] * np.conj(z[:-1])) * TAUX / (2 * np.pi)).astype(np.float32)


def verifier(ev: list[dict], titre: str) -> list[str]:
    j = []

    def ok(cond, texte):
        j.append(("OK" if cond else "ÉCHEC") + f"  {titre} : {texte}")

    touches = "".join(e["touche"] for e in ev if e["type"] == "dtmf")
    ctcss = [e for e in ev if e["type"] == "ctcss"]
    fins = [e for e in ev if e["type"] == "ctcss_fin"]
    t1750 = [e for e in ev if e["type"] == "1750_fin"]
    ok(touches == "1234#*0D55", f"DTMF « {touches} » (attendu 1234#*0D55)")
    ok([c["norme"] for c in ctcss] == [88.5, 151.4, 156.7], f"CTCSS {[c['norme'] for c in ctcss]}")
    ok(all(abs(f["f"] - f["norme"]) < 0.1 for f in fins if f["norme"]),
       "fréquences mesurées à moins de 0,1 Hz : " + ", ".join(f"{f['f']:.2f}" for f in fins))
    ok(any(f["norme"] == 88.5 and 7.0 < f["duree"] < 8.0 for f in fins), "durée du 88,5 Hz ≈ 7,5 s")
    ok(any(f["norme"] == 88.5 and 420 < f["deviation"] < 580 for f in fins), "déviation du 88,5 Hz ≈ 500 Hz")
    ok(len(t1750) == 1 and 0.8 < t1750[0]["duree"] < 1.2,
       f"1750 Hz : {[round(e['duree'], 2) for e in t1750]} s (attendu ≈ 1 s)")
    return j


def chaine_complete() -> list[str]:
    """IQ à 960 kHz (canal à +62,5 kHz du centre) → module complet, comme dans l'application."""
    import os
    import tempfile
    import time as horloge
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from sdrmod.bus import Bus
    app = QApplication.instance() or QApplication([])
    centre, taux, vfo = 145_000_000, 960_000, 145_062_500

    class Ctx:
        bus = Bus()
        def frequence(self): return vfo
        def dossier_donnees(self, nom): return Path(tempfile.mkdtemp())

    rng = np.random.default_rng(3)
    iq = signal.resample_poly(bande_base(25, rng), 20, 1)
    iq *= np.exp(2j * np.pi * (vfo - centre) * np.arange(len(iq)) / taux)
    iq = (0.3 * iq + 0.002 * (rng.normal(size=len(iq)) + 1j)).astype(np.complex64)
    mod = M.TonalitesModule(Ctx())
    mod.demarrer()
    w = mod.creer_widget()
    for k in range(0, len(iq), 48_000):
        mod.ctx.bus.publier("iq", iq[k:k + 48_000], centre, taux)
        horloge.sleep(0.005)
        while not mod.blocs.empty():
            horloge.sleep(0.01)
        app.processEvents()
        mod._rafraichir()
    for _ in range(60):                 # silence : la dernière tonalité se termine
        mod.ctx.bus.publier("iq", (0.002 * np.random.default_rng(6).normal(size=48_000)).astype(np.complex64),
                            centre, taux)
        horloge.sleep(0.01)
        app.processEvents()
    horloge.sleep(0.3)
    mod.etat["instant"] = horloge.monotonic()
    mod.derniere_touche = 0
    mod._rafraichir()
    diag = mod.diag.text()
    vus = [mod.liste.item(i).text() for i in range(mod.liste.count())][::-1]
    mod.arreter()
    w.deleteLater()
    print("--- chaîne complète :", diag)
    for ligne in vus:
        print("   ", ligne)
    j = []
    j.append(("OK" if any("CTCSS 88,5 Hz" in l and l.endswith(" s") for l in vus) else "ÉCHEC")
             + "  chaîne complète (IQ 960 kHz, canal décalé) : CTCSS 88,5 Hz et sa durée")
    j.append(("OK" if any(l.endswith("DTMF 1234#*0D") for l in vus) and any(l.endswith("DTMF 55") for l in vus)
              else "ÉCHEC") + "  chaîne complète : séquences DTMF 1234#*0D et 55")
    j.append(("OK" if any("1750 Hz" in l and l.endswith(" s") for l in vus) else "ÉCHEC")
             + "  chaîne complète : tonalité 1750 Hz")
    j.append(("OK" if mod.etat["perdus"] == 0 else "ÉCHEC") + "  chaîne complète : aucun bloc perdu")
    return j


def main() -> int:
    journal = []
    for snr in (30, 15):
        fm = discriminateur(bande_base(snr, np.random.default_rng(1)))
        if snr == 30 and len(sys.argv) > 1:
            fm.tofile(sys.argv[1])
        moteur = M.MoteurTonalites()
        ev = []
        for k in range(0, len(fm), 2400):
            ev += moteur.traiter(fm[k:k + 2400])
        ev += moteur.traiter(np.zeros(3 * TAUX, dtype=np.float32))
        print(f"--- C/N {snr} dB")
        for e in ev:
            print("   ", {k: (round(v, 3) if isinstance(v, float) else v) for k, v in e.items()})
        journal += verifier(ev, f"C/N {snr} dB")
    # bruit seul (pas de porteuse) : rien ne doit sortir
    rng = np.random.default_rng(2)
    bruit = discriminateur(rng.normal(size=20 * TAUX) + 1j * rng.normal(size=20 * TAUX))
    moteur = M.MoteurTonalites()
    ev = []
    for k in range(0, len(bruit), 2400):
        ev += moteur.traiter(bruit[k:k + 2400])
    journal.append(("OK" if not ev else "ÉCHEC") + f"  bruit seul (20 s) : aucune fausse détection ({len(ev)})")
    journal += chaine_complete()
    print("\n".join(journal))
    tout = all(l.startswith("OK") for l in journal)
    print("\nRÉSULTAT :", "TOUT OK" if tout else "ÉCHECS")
    return 0 if tout else 1


if __name__ == "__main__":
    raise SystemExit(main())

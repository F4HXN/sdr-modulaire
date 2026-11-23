"""Décodeur ACARS sur des émissions synthétiques (MSK 2 400 bauds en AM, comme ARINC 618).

Le modulateur écrit le signal comme une OQPSK à impulsions en demi-sinus (I et Q alternés, signe
inversé tous les deux bits) : c'est exactement le signal que le démodulateur cohérent d'acarsdec attend,
et on vérifie que c'est bien une MSK (enveloppe constante, seulement 1 200 et 2 400 Hz).

    QT_QPA_PLATFORM=offscreen python3 -m tests.test_decodeur_acars
"""

import importlib.util
import math
import sys
from pathlib import Path

import numpy as np
from scipy import signal

RACINE = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("decodeur_acars", RACINE / "modules-a-telecharger" / "decodeur_acars.py")
M = importlib.util.module_from_spec(spec)
spec.loader.exec_module(M)

FS = 48_000
T = 1 / 2400


def bits_de(octets: bytes) -> list[int]:
    return [(o >> i) & 1 for o in octets for i in range(8)]


def msk(bits: list[int], t0: float, duree: float) -> np.ndarray:
    """Signal audio MSK (centré sur 1 800 Hz) à FS : le bit k est lu à l'instant t0 + k·T."""
    t = np.arange(int(duree * FS)) / FS
    b = np.zeros(len(t), dtype=complex)
    for k, d in enumerate(bits):
        c = (1 if d else -1) * (-1 if k & 2 else 1) * (1 if k % 2 == 0 else 1j)
        tk = t0 + k * T
        a, z = int(math.ceil((tk - 2 * T) * FS)), int(math.floor(tk * FS))
        a, z = max(a, 0), min(z, len(t) - 1)
        if a <= z:
            tt = t[a:z + 1]
            b[a:z + 1] += c * np.sin(np.pi * (tt - (tk - 2 * T)) / (2 * T))
    return np.real(b * np.exp(2j * np.pi * 1800 * (t - t0))), b


def forme() -> list[str]:
    bits = bits_de(M.coder_message("2", ".F-GKXA", "H1", "3", "M01AAF1234ESSAI"))
    s, b = msk(bits, 0.01, len(bits) * T + 0.02)
    k = (np.arange(len(s)) / FS > 0.02) & (np.arange(len(s)) / FS < len(bits) * T)
    env = np.abs(b[k])
    j = [("OK" if env.min() > 0.999 and env.max() < 1.001 else "ÉCHEC") + "  modulateur : enveloppe constante (MSK)"]
    ph = np.unwrap(np.angle(b[k] * np.exp(2j * np.pi * 1800 * np.arange(len(b))[k] / FS)))
    f = np.diff(ph) * FS / (2 * np.pi)
    fr = set(np.unique(np.round(f[5:-5], -1)).tolist())
    j.append(("OK" if fr == {1200.0, 2400.0} else "ÉCHEC") + f"  modulateur : fréquences instantanées {sorted(fr)} Hz")
    pre = (np.arange(len(s)) / FS > 0.02) & (np.arange(len(s)) / FS < 0.01 + 120 * T)
    sp = np.abs(np.fft.rfft(s[pre] * np.hanning(pre.sum())))
    j.append(("OK" if abs(np.argmax(sp) * FS / pre.sum() - 2400) < 30 else "ÉCHEC") + "  préambule de « 1 » : 2 400 Hz (« mark »)")
    return j


MESSAGES = [
    dict(mode="2", immat=".F-GKXA", label="H1", bloc="3", texte="M01AAF1234POS N48.5 E002.3 FL350"),
    dict(mode="2", immat=".F-HBXA", label="_d", bloc="A", texte=""),
    dict(mode="2", immat=".EI-FNJ", label="5Z", bloc="7", texte="M12AEI0123/ETA LFPG 1542", suite=True),
    dict(mode="X", immat="", label="SQ", bloc="S", texte="02XAPARLFPG24849N00233EV136975/"),
]


def emission(msg: dict, t0: float, erreurs=(), inverse=False) -> tuple[np.ndarray, int]:
    """Audio d'un message (avec éventuelles erreurs : (octet, bit) comptés dans la partie message)."""
    o = bytearray(M.coder_message(**msg))
    debut = 16 + len(M.SYNCHRO)
    for i, b in erreurs:
        o[debut + i] ^= 1 << b
    bits = bits_de(bytes(o))
    s, _ = msk(bits, t0, t0 + len(bits) * T + 0.02)
    t = np.arange(len(s)) / FS
    porteuse = ((t > t0 - 0.01) & (t < t0 + len(bits) * T + 0.005)).astype(float)   # émetteur en marche
    return (-s if inverse else s) * porteuse, porteuse


def am(audio: np.ndarray, cn_db: float, rng, porteuse=None) -> np.ndarray:
    """AM (taux de modulation 60 %) + bruit pour un C/N donné sur 10 kHz, en bande de base à FS.
    porteuse : 1 pendant l'émission, 0 ailleurs (l'avion ne passe en émission que pour la salve)."""
    porteuse = np.ones(len(audio)) if porteuse is None else porteuse
    z = porteuse * (1 + 0.6 * audio) * np.exp(1j * rng.uniform(0, 2 * np.pi))
    sigma = math.sqrt(10 ** (-cn_db / 10) * FS / 10_000 / 2)
    return z + sigma * (rng.normal(size=len(z)) + 1j * rng.normal(size=len(z)))


def essais_moteur() -> list[str]:
    j = []
    rng = np.random.default_rng(2)

    def decoder(audio_bb: np.ndarray) -> tuple[list[dict], int]:
        canal = M.CanalACARS(0.0, FS)
        out = []
        for k in range(0, len(audio_bb), 2400):
            out += canal.traiter(audio_bb[k:k + 2400].astype(np.complex64), 0.0)
        return out, canal.trameur.rejets

    for cn in (20, 12, 9):
        trains, porteuses = [], []
        for m in MESSAGES:
            s, c = emission(m, rng.uniform(0.05, 0.2))
            trains.append(np.concatenate((s, np.zeros(int(0.3 * FS)))))
            porteuses.append(np.concatenate((c, np.zeros(int(0.3 * FS)))))
        recu, rejets = decoder(am(np.concatenate(trains), cn, rng, np.concatenate(porteuses)))
        ok_ = [any(r["immat"] == m["immat"].lstrip(".") and r["label"] == m["label"] for r in recu) for m in MESSAGES]
        j.append(("OK" if all(ok_) else "ÉCHEC") + f"  C/N {cn} dB : {sum(ok_)}/{len(MESSAGES)} messages ({rejets} rejets)")
        if cn == 20:
            r = recu[0] if recu else {}
            j.append(("OK" if r.get("vol") == "AF1234" and r.get("numero") == "M01A" and r.get("texte") == "POS N48.5 E002.3 FL350"
                      and r.get("descendant") else "ÉCHEC") + f"  champs : vol {r.get('vol')}, n° {r.get('numero')}, texte « {r.get('texte')} »")
            j.append(("OK" if any(x["suite"] and x["vol"] == "EI0123" for x in recu) else "ÉCHEC") + "  bloc ETB (suite à venir)")
            j.append(("OK" if any(x["label"] == "_d" and not x["descendant"] and x["texte"] == "" for x in recu) else "ÉCHEC")
                     + "  accusé montant sans texte (label _d)")
    # polarité inversée, erreurs corrigées
    s, c = emission(MESSAGES[0], 0.1, inverse=True)
    recu, _ = decoder(am(np.concatenate((s, np.zeros(FS // 4))), 25, rng, np.concatenate((c, np.zeros(FS // 4)))))
    j.append(("OK" if recu and recu[0]["vol"] == "AF1234" else "ÉCHEC") + "  polarité inversée")
    for erreurs, attendu, nom in ((((20, 3),), 1, "une erreur (parité fausse)"),
                                  (((5, 1), (20, 6), (30, 0)), 3, "trois caractères de parité fausse"),
                                  (((22, 1), (22, 4)), 2, "deux bits faux dans un même caractère")):
        s, c = emission(MESSAGES[0], 0.1, erreurs=erreurs)
        recu, _ = decoder(am(np.concatenate((s, np.zeros(FS // 4))), 25, rng, np.concatenate((c, np.zeros(FS // 4)))))
        j.append(("OK" if recu and recu[0]["texte"] == "POS N48.5 E002.3 FL350" and recu[0]["corrections"] == attendu
                  else "ÉCHEC") + f"  {nom} corrigée(s) ({recu[0]['corrections'] if recu else '—'})")
    s, c = emission(MESSAGES[0], 0.1, erreurs=((20, 3), (21, 3), (22, 3), (23, 3)))
    recu, rejets = decoder(am(np.concatenate((s, np.zeros(FS // 4))), 25, rng, np.concatenate((c, np.zeros(FS // 4)))))
    j.append(("OK" if not recu and rejets == 1 else "ÉCHEC") + "  quatre erreurs : message rejeté, rien d'inventé")
    rng2 = np.random.default_rng(9)
    bruit = (rng2.normal(size=30 * FS) + 1j * rng2.normal(size=30 * FS)) * 0.1
    recu, _ = decoder(bruit)
    j.append(("OK" if not recu else "ÉCHEC") + f"  bruit seul (30 s) : aucun message ({len(recu)})")
    return j


def essai_iq() -> list[str]:
    """Trois canaux européens à 2,4 MS/s, centre 131,485 MHz : décodés ensemble."""
    import time
    rng = np.random.default_rng(5)
    taux, centre = 2_400_000, 131_485_000
    duree = 3.0
    iq = np.zeros(int(duree * taux), dtype=np.complex64)
    t = np.arange(len(iq)) / taux
    for f, m in zip((131_725_000, 131_525_000, 131_825_000), MESSAGES[:3]):
        s, c = emission(m, rng.uniform(0.1, 0.8))
        reste = np.zeros(int(duree * FS) - len(s))
        bb = signal.resample_poly(am(np.concatenate((s, reste)), 25, rng, np.concatenate((c, reste))), 50, 1)
        iq += (0.05 * bb * np.exp(2j * np.pi * (f - centre) * t)).astype(np.complex64)
    iq += (0.01 * (rng.normal(size=len(iq)) + 1j * rng.normal(size=len(iq)))).astype(np.complex64)
    moteur = M.MoteurACARS(M.LISTES[0][1] + [129_000_000], centre, taux)
    recu = []
    debut = time.perf_counter()
    for k in range(0, len(iq), 122_880):
        recu += moteur.traiter(iq[k:k + 122_880])
    calcul = (time.perf_counter() - debut) / duree
    vus = {(r["frequence"], r["immat"]) for r in recu}
    attendu = {(131_725_000, "F-GKXA"), (131_525_000, "F-HBXA"), (131_825_000, "EI-FNJ")}
    for r in recu:
        print("   ", M.texte_message(r))
    return [("OK" if vus == attendu else "ÉCHEC") + f"  IQ 2,4 MS/s : 3 fréquences décodées ensemble {sorted(vus)}",
            ("OK" if moteur.dehors == [129_000_000] else "ÉCHEC") + "  fréquence hors de la bande signalée",
            f"INFO  calcul : {100 * calcul:.0f} % du temps réel pour 3 canaux"]


def chaine_complete() -> list[str]:
    """Module complet : IQ publiées sur le bus, liste « Europe », messages affichés, bouton Accorder."""
    import os
    import tempfile
    import time as horloge
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from sdrmod.bus import Bus
    app = QApplication.instance() or QApplication([])
    reglages = {"f": 145_500_000.0, "mode": "NFM"}

    class Ctx:
        bus = Bus()
        def frequence(self): return reglages["f"]
        def taux(self): return 960_000
        def regler_mode(self, m): reglages["mode"] = m
        def regler_frequence(self, f): reglages["f"] = f
        def dossier_donnees(self, nom): return Path(tempfile.mkdtemp())

    rng = np.random.default_rng(8)
    taux, centre = 960_000, 131_629_000
    s, c = emission(MESSAGES[0], 0.3)
    reste = np.zeros(2 * FS)
    bb = signal.resample_poly(am(np.concatenate((s, reste)), 20, rng, np.concatenate((c, reste))), 20, 1)
    iq = (0.05 * bb * np.exp(2j * np.pi * (131_725_000 - centre) * np.arange(len(bb)) / taux)).astype(np.complex64)
    iq += (0.01 * (rng.normal(size=len(iq)) + 1j * rng.normal(size=len(iq)))).astype(np.complex64)
    mod = M.DecodeurACARSModule(Ctx())
    mod.demarrer()
    w = mod.creer_widget()
    mod.c_liste.setCurrentIndex(0)
    for k in range(0, len(iq) - 48_000, 48_000):
        mod.ctx.bus.publier("iq", iq[k:k + 48_000], centre, taux)
        horloge.sleep(0.005)
        while not mod.blocs.empty():
            horloge.sleep(0.01)
        app.processEvents()
    horloge.sleep(0.3)
    mod._rafraichir()
    vus = [mod.liste.item(i).text() for i in range(mod.liste.count())]
    statut, diag = mod.statut.text(), mod.diag.text()
    mod.accorder()
    mod.arreter()
    w.deleteLater()
    print("--- chaîne complète :", statut, "|", diag)
    for l in vus:
        print("   ", l)
    return [("OK" if any("F-GKXA" in l and "AF1234" in l and "POS N48.5" in l for l in vus) else "ÉCHEC")
            + "  chaîne complète : message affiché (IQ 0,96 MS/s, liste Europe)",
            ("OK" if "131,525, 131,725, 131,825" in statut.replace("131,725, 131,525, 131,825", "131,525, 131,725, 131,825")
             else "ÉCHEC") + f"  chaîne complète : les 3 fréquences européennes décodées ({statut})",
            ("OK" if reglages == {"f": 131_725_000, "mode": "AM"} else "ÉCHEC") + f"  Accorder : VFO {reglages}"]


def main() -> int:
    journal = forme() + essais_moteur() + essai_iq() + chaine_complete()
    print("\n".join(journal))
    tout = all(not l.startswith("ÉCHEC") for l in journal)
    print("\nRÉSULTAT :", "TOUT OK" if tout else "ÉCHECS")
    return 0 if tout else 1


if __name__ == "__main__":
    raise SystemExit(main())

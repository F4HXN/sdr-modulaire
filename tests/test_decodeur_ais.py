"""Décodeur AIS : messages de référence (documentation de gpsd) et émissions GMSK synthétiques.

1. Décodage des champs sur des phrases !AIVDM publiques, décodées dans la documentation de gpsd
   (vérification indépendante de la lecture des messages).
2. Salves GMSK 9 600 bauds (BT 0,4, ±2 400 Hz) fabriquées à partir de ces messages : séquence
   d'apprentissage, fanions, bourrage, NRZI, CRC ; bruit, décalage de fréquence, début quelconque.
3. Chaîne complète : IQ à 2,4 MS/s avec les deux canaux (161,975 et 162,025 MHz), module complet.

    QT_QPA_PLATFORM=offscreen python3 -m tests.test_decodeur_ais
"""

import importlib.util
import math
from pathlib import Path

import numpy as np

RACINE = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("decodeur_ais", RACINE / "modules-a-telecharger" / "decodeur_ais.py")
M = importlib.util.module_from_spec(spec)
spec.loader.exec_module(M)

FS = M.TAUX_AIS
# Phrases de la documentation de gpsd (AIVDM/AIVDO protocol decoding) et valeurs qu'elle donne.
TYPE1 = "177KQJ5000G?tO`K>RA1wUbN0TKH"
TYPE5 = ("55?MbV02;H;s<HtKR20EHE:0@T4@Dn2222222216L961O5Gf0NSQEp6ClRp8" "88888888880", 2)


def gmsk(bits: list[int], decalage_hz: float, t0: float, duree: float) -> np.ndarray:
    """Bande de base GMSK à FS : NRZI (un 0 = changement), filtre gaussien BT 0,4, ±2 400 Hz."""
    niveaux, n = [], 1
    for b in bits:
        if b == 0:
            n = -n
        niveaux.append(n)
    spb = int(M.SPB)
    nrz = np.repeat(niveaux, spb).astype(float)
    bt, portee = 0.4, 3
    t = np.arange(-portee * spb, portee * spb + 1) / spb
    sigma = math.sqrt(math.log(2)) / (2 * math.pi * bt)
    h = np.exp(-t ** 2 / (2 * sigma ** 2))
    h /= h.sum()
    f = np.convolve(nrz, h)[portee * spb:portee * spb + len(nrz)] * M.DEVIATION
    total = int(duree * FS)
    freq = np.full(total, float(decalage_hz))
    a = int(t0 * FS)
    freq[a:a + len(f)] += f
    porteuse = np.zeros(total)
    porteuse[a - spb:a + len(f) + spb] = 1.0                    # l'émetteur ne passe en émission que pour la salve
    return porteuse * np.exp(2j * np.pi * np.cumsum(freq) / FS)


def avec_bruit(z: np.ndarray, cn_db: float, rng) -> np.ndarray:
    sigma = math.sqrt(10 ** (-cn_db / 10) * FS / 16_000 / 2)       # C/N sur 16 kHz
    return z + sigma * (rng.normal(size=len(z)) + 1j * rng.normal(size=len(z)))


def discriminateur(z: np.ndarray) -> np.ndarray:
    from scipy import signal
    z = signal.lfilter(signal.firwin(95, M.LARGEUR_CANAL, fs=FS), 1, z)
    return np.angle(z[1:] * np.conj(z[:-1])) * FS / (2 * math.pi)


def essais_champs() -> list[str]:
    j = []
    m = M.decoder_message(M.bits_depuis_nmea(TYPE1))
    ok = (m["type"] == 1 and m["mmsi"] == 477553000 and m["statut"] == 5 and m["vitesse"] == 0 and m["cap"] == 181
          and m["route"] == 510 and abs(m["position"][0] - 47.582833) < 1e-5 and abs(m["position"][1] + 122.345833) < 1e-5)
    j.append(("OK" if ok else "ÉCHEC") + f"  type 1 (gpsd) : MMSI {m['mmsi']}, amarré, 47,5828 N 122,3458 O, route 51°, cap 181°")
    m = M.decoder_message(M.bits_depuis_nmea(*TYPE5))
    ok = (m["type"] == 5 and m["mmsi"] == 351759000 and m["imo"] == 9134270 and m["indicatif"] == "3FOF8"
          and m["nom"] == "EVER DIADEM" and m["type_navire"] == 70 and m["destination"] == "NEW YORK"
          and m["tirant"] == 12.2 and m["longueur"] == 295 and m["largeur"] == 32)
    j.append(("OK" if ok else "ÉCHEC") + f"  type 5 (gpsd) : {m.get('nom')}, {m.get('indicatif')}, IMO {m.get('imo')}, "
             f"{M.categorie(m.get('type_navire'))}, → {m.get('destination')}")
    return j


def salves(rng, n: int = 6) -> tuple[np.ndarray, list]:
    """Une suite de salves (types 1 et 5 alternés) sur un canal, à FS, avec leurs bits de message."""
    msgs = [M.bits_depuis_nmea(TYPE1), M.bits_depuis_nmea(*TYPE5)]
    morceaux, attendus = [], []
    for k in range(n):
        b = msgs[k % 2]
        e = M.bits_emis(b)
        duree = 0.04 + len(e) / M.DEBIT
        morceaux.append(gmsk(e, rng.uniform(-1500, 1500), rng.uniform(0.004, 0.02), duree))
        attendus.append(b)
    return np.concatenate(morceaux), attendus


def decoder_fm(z: np.ndarray) -> list[bytes]:
    fm = discriminateur(z)
    d = M.DemodGMSK()
    recus = []
    for k in range(0, len(fm), 2400):
        recus += d.traiter(fm[k:k + 2400])
    return recus


def essais_radio() -> list[str]:
    """Banc de 40 salves par niveau (décalages de ±1,5 kHz, début quelconque), C/N sur 16 kHz."""
    j = []
    for cn, mini in ((25, 38), (15, 38), (13, 26), (11, 12)):
        rng = np.random.default_rng(cn)
        z, attendus = salves(rng, 40)
        recus = decoder_fm(avec_bruit(z, cn, rng))
        justes = all(any(M.octets_vers_bits(o)[:len(b)] == b for b in attendus[:2]) for o in recus)
        j.append(("OK" if len(recus) >= mini and justes and len(recus) <= 40 else "ÉCHEC")
                 + f"  C/N {cn} dB : {len(recus)}/40 salves décodées (attendu ≥ {mini}), toutes justes")
    rng = np.random.default_rng(3)
    recus = decoder_fm(rng.normal(size=20 * FS) + 1j * rng.normal(size=20 * FS))
    j.append(("OK" if not recus else "ÉCHEC") + f"  bruit seul (20 s) : aucun message ({len(recus)})")
    return j


def essai_iq() -> list[str]:
    """IQ à 2,4 MS/s centrées sur 161,9 MHz : AIS 1 (161,975) et AIS 2 (162,025) ensemble, module complet."""
    import os
    import tempfile
    import time as horloge
    from scipy import signal
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from sdrmod.bus import Bus
    app = QApplication.instance() or QApplication([])
    reglages = {"f": 145_000_000.0, "mode": "AM"}

    class Ctx:
        bus = Bus()
        def frequence(self): return reglages["f"]
        def taux(self): return 2_400_000
        def regler_mode(self, m): reglages["mode"] = m
        def regler_frequence(self, f): reglages["f"] = f
        def dossier_donnees(self, nom): return Path(tempfile.mkdtemp())

    taux, centre = 2_400_000, 161_900_000
    rng = np.random.default_rng(7)
    za, _ = salves(rng)
    zb = gmsk(M.bits_emis(M.bits_depuis_nmea(TYPE1)), 800, 0.3, 0.6)
    n = max(len(za), len(zb))
    za, zb = np.pad(za, (0, n - len(za))), np.pad(zb, (0, n - len(zb)))
    t = np.arange(n * 50) / taux
    iq = np.zeros(n * 50, dtype=np.complex64)
    for z, f in ((za, 161_975_000), (zb, 162_025_000)):
        iq += (0.05 * signal.resample_poly(z, 50, 1) * np.exp(2j * np.pi * (f - centre) * t)).astype(np.complex64)
    iq += (0.004 * (rng.normal(size=len(iq)) + 1j * rng.normal(size=len(iq)))).astype(np.complex64)
    mod = M.DecodeurAIS(Ctx())
    mod.demarrer()
    w = mod.creer_widget()
    for k in range(0, len(iq), 122_880):
        mod.ctx.bus.publier("iq", iq[k:k + 122_880], centre, taux)
        while not mod.blocs.empty():
            horloge.sleep(0.005)
    horloge.sleep(0.5)
    mod._rafraichir()
    lignes = [mod.liste.item(i).text() for i in range(mod.liste.count())]
    mod.ouvrir_carte()
    app.processEvents()
    tableau = mod.carte.tableau.rowCount()
    mod.accorder()
    mod.arreter()
    w.deleteLater()
    for l in lignes:
        print("   ", l)
    a = sum("  A  " in l for l in lignes)
    b = sum("  B  " in l for l in lignes)
    nav = mod.navires.get(351759000, {})
    return [("OK" if a == 6 and b == 1 else "ÉCHEC") + f"  IQ 2,4 MS/s : AIS 1 {a}/6 messages, AIS 2 {b}/1",
            ("OK" if nav.get("nom") == "EVER DIADEM" and 477553000 in mod.navires and tableau == 2 else "ÉCHEC")
            + f"  navires : {len(mod.navires)} dans le tableau, EVER DIADEM identifié",
            ("OK" if reglages == {"f": 161_975_000, "mode": "NFM"} else "ÉCHEC") + f"  Accorder : {reglages}",
            calcul_moteur()]


def calcul_moteur() -> str:
    """Temps de calcul du moteur seul sur 4 s d'IQ à 2,4 MS/s : salves sur AIS 1, bruit sur AIS 2."""
    import time as horloge
    from scipy import signal
    rng = np.random.default_rng(11)
    taux, centre = 2_400_000, 161_900_000
    z, _ = salves(rng, 40)
    z = z[: 4 * FS]
    t = np.arange(len(z) * 50) / taux
    iq = (0.05 * signal.resample_poly(z, 50, 1) * np.exp(2j * np.pi * (161_975_000 - centre) * t)).astype(np.complex64)
    iq += (0.004 * (rng.normal(size=len(iq)) + 1j * rng.normal(size=len(iq)))).astype(np.complex64)
    m = M.MoteurAIS(centre, taux)
    m.traiter(iq[:122_880])                                    # construction des filtres hors mesure
    debut = horloge.perf_counter()
    for k in range(122_880, len(iq), 122_880):
        m.traiter(iq[k:k + 122_880])
    return f"INFO  calcul du moteur : {100 * (horloge.perf_counter() - debut) / ((len(iq) - 122_880) / taux):.0f} % " \
           "du temps réel (2 canaux, 2,4 MS/s, un canal chargé de salves)"


def main() -> int:
    journal = essais_champs() + essais_radio() + essai_iq()
    print("\n".join(journal))
    tout = all(not l.startswith("ÉCHEC") for l in journal)
    print("\nRÉSULTAT :", "TOUT OK" if tout else "ÉCHECS")
    return 0 if tout else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Décodeur DMR (données) sur un flux de relais synthétique.

Le flux suit la norme ETSI TS 102 361-1 (salves de 30 ms, CACH, deux slots) :
  TS1 : en-tête vocal (TG 9 ← 2080001), 3 supertrames vocales (LC intégré, alias « F4XYZ TEST »),
        terminaison ; puis un appel privé pris en cours (sans en-tête : LC intégré seulement) ;
  TS2 : SMS Motorola (IP/UDP 4007) en blocs 1/2, puis un second en blocs 3/4 (treillis).
Modulation 4FSK (±648 / ±1 944 Hz, racine de cosinus surélevé α = 0,2), bruit ajouté, discriminateur.

    python3 -m tests.test_decodeur_dmr            # décodeur seul
    python3 -m tests.test_decodeur_dmr dmr.wav    # écrit aussi l'audio du discriminateur (48 kHz)
                                                  # pour le vérifier avec dsd-fme -fs -i dmr.wav
"""

import importlib.util
import random
import sys
import wave
from pathlib import Path

import numpy as np
from scipy import signal

RACINE = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("decodeur_dmr", RACINE / "modules-a-telecharger" / "decodeur_dmr.py")
M = importlib.util.module_from_spec(spec)
spec.loader.exec_module(M)

CC = 1
BS_VOIX, BS_DONNEES = 0x755FD7DF75F7, 0xDFF57D75DF5D
CACH_ORDRE = (0, 7, 8, 9, 1, 10, 11, 12, 2, 13, 14, 15, 3, 16, 4, 17, 18, 19, 5, 20, 21, 22, 6, 23)
alea = random.Random(7)


def bits_de(v: int, n: int) -> list[int]:
    return [(v >> (n - 1 - i)) & 1 for i in range(n)]


def cach(tc: int) -> list[int]:
    d = [1, tc, 0, 0]
    tact = d + [d[0] ^ d[1] ^ d[2], d[1] ^ d[2] ^ d[3], d[0] ^ d[1] ^ d[3]]
    donnees = tact + [0] * 17
    return [donnees[CACH_ORDRE[k]] for k in range(24)]


def salve_donnees(dt: int, octets: bytes = b"", info: list[int] | None = None) -> list[int]:
    info = info if info is not None else M.bptc_coder(octets)
    st = bits_de(M.golay_mot(CC << 4 | dt), 20)
    return info[:98] + st[:10] + bits_de(BS_DONNEES, 48) + st[10:] + info[98:]


def voix() -> list[int]:
    return [alea.randint(0, 1) for _ in range(216)]


def salve_voix(k: int, frag: list[int] | None, lcss: int) -> list[int]:
    v = voix()
    if k == 0:
        centre = bits_de(BS_VOIX, 48)
    else:
        emb = bits_de(M.qr_mot(CC << 3 | lcss), 16)
        centre = emb[:8] + (frag or [0] * 32) + emb[8:]
    return v[:108] + centre + v[108:]


def supertrame(lc: bytes) -> list[list[int]]:
    """Salves A à F ; le LC intégré dans B, C, D, E."""
    brut = M.lc_integre_coder(lc)
    out = [salve_voix(0, None, 0)]
    for k, lcss in zip(range(1, 5), (1, 3, 3, 2)):
        out.append(salve_voix(k, brut[(k - 1) * 32:k * 32], lcss))
    out.append(salve_voix(5, None, 0))
    return out


def lc_complet(flco: int, dst: int, src: int, masque: int, so: int = 0) -> bytes:
    o = bytes([flco, 0, so]) + dst.to_bytes(3, "big") + src.to_bytes(3, "big")
    return o + bytes(b ^ masque for b in M.rs_parite(o))


def lc_court(flco: int, reste: bytes) -> bytes:
    return bytes([flco, 0]) + reste[:7].ljust(7, b"\0")


def alias_lc(texte: str) -> list[bytes]:
    """Alias sur 7 bits : en-tête (49 bits) puis blocs de 56 bits."""
    bits = [b for c in texte for b in bits_de(ord(c), 7)]
    entete = [0, 0] + bits_de(len(texte), 5) + bits[:49]
    morceaux = [M.bits_octets(bits_de(4, 8) + [0] * 8 + entete[:56])]
    reste, flco = bits[49:], 5
    while reste:
        morceaux.append(M.bits_octets(bits_de(flco, 8) + [0] * 8 + (reste[:56] + [0] * 56)[:56]))
        reste, flco = reste[56:], flco + 1
    return morceaux


def avec_crc16(o: bytes, masque: int) -> bytes:
    c = M.crc_ccitt(o[:10]) ^ masque
    return o[:10] + bytes([c >> 8, c & 0xFF])


def paquet_sms(texte: str, src: int, dst: int) -> bytes:
    tms = bytes([0xA0, 0x00, 0x04, 0x0D]) + texte.encode("utf-16-le")
    udp = (4007).to_bytes(2, "big") * 2 + (8 + 2 + len(tms)).to_bytes(2, "big") + b"\0\0"
    charge = udp + len(tms).to_bytes(2, "big") + tms
    ip = bytes([0x45, 0, 0, 0, 0, 0, 0, 0, 64, 17, 0, 0]) + bytes([12, 0x1F, src >> 8 & 255, src & 255]) \
        + bytes([12, 0x1F, dst >> 8 & 255, dst & 255])
    ip = ip[:2] + (20 + len(charge)).to_bytes(2, "big") + ip[4:]
    return ip + charge


def donnees(paquet: bytes, src: int, dst: int, taille_bloc: int, dt: int, code=None) -> list[list[int]]:
    """En-tête non confirmé + blocs (CRC-32 dans le dernier)."""
    n = -(-(len(paquet) + 4) // taille_bloc)
    bourrage = n * taille_bloc - len(paquet) - 4
    corps = paquet + b"\0" * bourrage
    corps += M.crc32_dmr(corps).to_bytes(4, "big")
    entete = bytes([0x02 | (bourrage >> 4 & 1) << 4, 4 << 4 | (bourrage & 15)]) + dst.to_bytes(3, "big") \
        + src.to_bytes(3, "big") + bytes([0x80 | n, 0])
    salves = [salve_donnees(6, avec_crc16(entete + b"\0\0", 0xCCCC))]
    for k in range(n):
        bloc = corps[k * taille_bloc:(k + 1) * taille_bloc]
        if dt == 7:
            salves.append(salve_donnees(7, bloc))
        else:
            sym = M.treillis_coder(bloc, code or M.TREILLIS_CODE)
            dib = [M.SYMBOLE_DIBIT[s] for s in sym]
            bits = [b for d in dib for b in (d >> 1, d & 1)]
            salves.append(salve_donnees(8, info=bits))
    return salves


def flux() -> list[int]:
    ts1, ts2 = [], []
    tg, src = 9, 2080001
    ts1.append(salve_donnees(1, lc_complet(0, tg, src, 0x96)))
    al = alias_lc("F4XYZ TEST")
    for lc in (lc_complet(0, tg, src, 0)[:9], al[0], al[1]):
        ts1 += supertrame(lc)
    ts1.append(salve_donnees(2, lc_complet(0, tg, src, 0x99)))
    ts1 += [None] * 20
    for _ in range(3):                                   # appel privé pris en cours : pas d'en-tête
        ts1 += supertrame(lc_complet(3, 2080999, 2080002, 0)[:9])
    ts2 += [None] * 4
    ts2 += donnees(paquet_sms("Bonjour DMR", 2080003, 2080004), 2080003, 2080004, 12, 7)
    ts2 += [None] * 6
    ts2 += donnees(paquet_sms("Essai 3/4", 2080005, 2080006), 2080005, 2080006, 18, 8)
    ts2 += [None] * 6
    ts2 += donnees(paquet_sms("Autre treillis", 2080007, 2080008), 2080007, 2080008, 18, 8, M.TREILLIS_CODE_2)
    n = max(len(ts1), len(ts2)) + 40
    inactif = salve_donnees(9, bytes(12))
    bits = []
    for i in range(2 * n):
        slot = i % 2
        file = (ts1, ts2)[slot]
        k = i // 2
        s = file[k] if k < len(file) and file[k] is not None else inactif
        bits += cach(slot) + s
    return bits


def moduler(bits: list[int], snr_db: float, iq_seul: bool = False) -> np.ndarray:
    sym = np.array([M.DIBIT_SYMBOLE[bits[i] << 1 | bits[i + 1]] for i in range(0, len(bits), 2)], dtype=float)
    x = np.zeros(len(sym) * M.SPS)
    x[::M.SPS] = sym * M.ECART
    f = signal.lfilter(M.rrc(0.2, M.SPS, 8) * M.SPS, 1.0, x)
    iq = np.exp(1j * 2 * np.pi * np.cumsum(f) / M.TAUX)
    bruit = (np.random.default_rng(3).normal(size=len(iq)) + 1j * np.random.default_rng(4).normal(size=len(iq)))
    iq = iq + bruit * 10 ** (-snr_db / 20) / np.sqrt(2) * np.sqrt(M.TAUX / 12_500)
    if iq_seul:
        return iq
    fm = np.angle(iq[1:] * np.conj(iq[:-1])) * M.TAUX / (2 * np.pi)
    return fm.astype(np.float32)


def essais_codes() -> list[str]:
    j = []
    o = bytes(range(12))
    b = M.bptc_coder(o)
    for p in (3, 50, 120, 190):
        b[p] ^= 1
    j.append(("OK" if M.bptc_decoder(b) == o else "ÉCHEC") + "  BPTC (196,96), 4 erreurs corrigées")
    w = M.golay_mot(0x1A) ^ 0b1000100000010
    j.append(("OK" if M.golay_decoder(w) == 0x1A else "ÉCHEC") + "  Golay (20,8), 3 erreurs corrigées")
    q = M.qr_mot(0x35) ^ 0b100000000100
    j.append(("OK" if M.qr_decoder(q) == 0x35 else "ÉCHEC") + "  QR (16,7), 2 erreurs corrigées")
    lc = lc_complet(0, 9, 2080001, 0x96)
    j.append(("OK" if M.lc_valide(lc, 0x96) and not M.lc_valide(lc[:4] + b"\1" + lc[5:], 0x96) else "ÉCHEC")
             + "  Reed-Solomon (12,9)")
    brut = M.lc_integre_coder(lc[:9])
    brut[17] ^= 1
    j.append(("OK" if M.lc_integre_decoder(brut) == lc[:9] else "ÉCHEC") + "  LC intégré, 1 erreur corrigée")
    o18 = bytes(range(100, 118))
    s = [v + 0.6 * ((-1) ** i) * (i % 7 == 0) for i, v in enumerate(M.treillis_coder(o18))]
    s[20] = -s[20]
    j.append(("OK" if M.treillis_decoder(s) == o18 else "ÉCHEC") + "  treillis 3/4 (Viterbi), symbole faux corrigé")
    return j


def chaine_complete() -> list[str]:
    """IQ à 960 kHz (canal à +37,5 kHz du centre) → module complet, comme dans l'application."""
    import os
    import tempfile
    import time as horloge
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from sdrmod.bus import Bus
    app = QApplication.instance() or QApplication([])
    centre, taux, vfo = 145_000_000, 960_000, 145_037_500

    class Ctx:
        bus = Bus()
        def frequence(self): return vfo
        def dossier_donnees(self, nom): return Path(tempfile.mkdtemp())

    base = moduler(flux(), 20, iq_seul=True)
    iq = signal.resample_poly(base, 20, 1)
    iq *= np.exp(2j * np.pi * (vfo - centre) * np.arange(len(iq)) / taux)
    iq = (0.3 * iq + 0.002 * (np.random.default_rng(5).normal(size=len(iq)) + 1j)).astype(np.complex64)
    mod = M.DecodeurDMRModule(Ctx())
    mod.demarrer()
    w = mod.creer_widget()
    vus = []
    for k in range(0, len(iq), 48_000):
        mod.ctx.bus.publier("iq", iq[k:k + 48_000], centre, taux)
        horloge.sleep(0.01)
        while not mod.blocs.empty():
            horloge.sleep(0.01)
        app.processEvents()
    for _ in range(25):                 # silence : les appels se terminent
        mod.ctx.bus.publier("iq", (0.002 * np.random.default_rng(6).normal(size=48_000)).astype(np.complex64),
                            centre, taux)
        horloge.sleep(0.02)
        app.processEvents()
    horloge.sleep(0.5)
    mod.etat["instant"] = horloge.monotonic()
    mod._rafraichir()
    diag = mod.diag.text()
    vus = [mod.liste.item(i).text() for i in range(mod.liste.count())][::-1]
    mod.arreter()
    w.deleteLater()
    print("--- chaîne complète :", diag)
    for ligne in vus:
        print("   ", ligne)
    j = []
    j.append(("OK" if any("TS1" in l and "TG 9 ← 2080001 · F4XYZ TEST" in l and " s" in l[-4:] for l in vus)
              else "ÉCHEC") + "  chaîne complète (IQ 960 kHz, canal décalé) : appel TG 9 sur TS1, alias, durée")
    j.append(("OK" if any("Bonjour DMR" in l for l in vus) and any("Autre treillis" in l for l in vus) else "ÉCHEC")
             + "  chaîne complète : SMS affichés")
    j.append(("OK" if mod.etat["perdus"] == 0 else "ÉCHEC") + "  chaîne complète : aucun bloc perdu")
    return j


def main() -> int:
    journal = essais_codes()
    bits = flux()
    for snr in (25, 14):
        fm = moduler(bits, snr)
        if snr == 25 and len(sys.argv) > 1:
            pcm = np.clip(fm / 4000, -1, 1)
            with wave.open(sys.argv[1], "wb") as w:
                w.setparams((1, 2, M.TAUX, 0, "NONE", "not compressed"))
                w.writeframes((pcm * 32767).astype("<i2").tobytes())
        d = M.DecodeurDMR()
        ev = []
        for k in range(0, len(fm), 2400):
            ev += d.traiter(fm[k:k + 2400])
        ev += d.traiter(np.zeros(3 * M.TAUX, dtype=np.float32))
        appels = [e for e in ev if e["type"] == "appel"]
        fins = [e for e in ev if e["type"] == "fin"]
        sms = [e for e in ev if e["type"] == "sms"]
        print(f"--- SNR {snr} dB : {d.salves} salves, CC {d.cc}")
        for e in ev:
            if e["type"] != "csbk":
                print("   ", {k: v for k, v in e.items() if k not in ("debut", "dernier")})

        def ok(cond, texte):
            journal.append(("OK" if cond else "ÉCHEC") + f"  SNR {snr} dB : {texte}")

        ok(any(a["slot"] == 1 and a["groupe"] and a["dst"] == 9 and a["src"] == 2080001 and a["origine"] == "en-tête"
               for a in appels), "appel TS1, TG 9 ← 2080001 (en-tête)")
        ok(any(f["slot"] == 1 and f["dst"] == 9 and f["alias"] == "F4XYZ TEST" and 1.0 < f["duree"] < 1.6
               for f in fins), "fin d'appel, alias « F4XYZ TEST », durée ≈ 1,2 s")
        ok(any(a["slot"] == 1 and not a["groupe"] and a["dst"] == 2080999 and a["src"] == 2080002
               and a["origine"] == "en cours" for a in appels), "appel privé pris en cours (LC intégré)")
        ok(any(s["slot"] == 2 and "Bonjour DMR" in s["texte"] and s["crc"] for s in sms), "SMS en blocs 1/2, CRC-32 juste")
        ok(any(s["slot"] == 2 and "Essai 3/4" in s["texte"] and s["crc"] for s in sms), "SMS en blocs 3/4 (treillis)")
        ok(any(s["slot"] == 2 and "Autre treillis" in s["texte"] and s["crc"] for s in sms),
           "SMS en blocs 3/4, seconde table de treillis")
        ok(d.cc == CC, f"color code {CC}")
    journal += chaine_complete()
    print("\n".join(journal))
    tout = all(l.startswith("OK") for l in journal)
    print("\nRÉSULTAT :", "TOUT OK" if tout else "ÉCHECS")
    return 0 if tout else 1


if __name__ == "__main__":
    raise SystemExit(main())

# SPDX-License-Identifier: MIT
"""Décodeur DMR (données) pour SDR Modulaire, sans logiciel externe.

Affiche qui parle sur un relais ou en direct : slot (TS1, TS2), color code, groupe (TG) ou appel
privé, ID de l'émetteur, alias (indicatif transmis pendant l'appel), chiffrement, urgence ; et les
données : en-têtes, SMS (texte des paquets IP, Motorola TMS), CSBK sur demande. La voix (AMBE+2)
n'est pas décodée.

Chaîne : NCO sur la fréquence du VFO → décimation à 48 kHz → discriminateur FM → filtre en racine
de cosinus surélevé (α = 0,2, 10 échantillons par symbole) → recherche des synchros de 24 symboles
par corrélation normalisée (8 motifs : relais, mobile, direct TS1/TS2, voix et données) → niveaux
des 4 symboles lus sur la synchro → salve de 132 symboles → codes de la norme ETSI TS 102 361-1 :
Golay (20,8) du type de slot, BPTC (196,96) avec Hamming (15,11) et (13,9), Reed-Solomon (12,9),
CRC-CCITT, QR (16,7) de l'EMB, LC intégré (Hamming (16,11), CRC 5 bits), treillis 3/4, CRC-32.
Les salves vocales B à F, sans synchro, sont lues 60, 120… 300 ms après la synchro A du même slot :
c'est là qu'est le LC intégré, qui donne l'appel même quand l'en-tête a été manqué, et l'alias.
Même algorithme dans SDR Web (web/dmr.js).
"""

from __future__ import annotations

import queue
import threading
import time

import numpy as np
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (QCheckBox, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton,
                               QSizePolicy, QVBoxLayout, QWidget)
from scipy import signal

from sdrmod.dsp import Decimateur, NCO, plan_decimation
from sdrmod.modules import Module

TAUX = 48_000
SPS = 10                       # échantillons par symbole (4 800 bauds)
SLOT = 6 * TAUX // 100         # 60 ms : d'une salve à la suivante du même slot
SEUIL = 0.9                    # corrélation normalisée minimale d'une synchro
ECART = 648.0                  # Hz par unité de symbole (+3 → 1 944 Hz)

# ======================================================================= codes de la norme
# Synchros (48 bits) : source, voix ?, slot imposé.
SYNCS = {
    0x755FD7DF75F7: ("relais", True, None), 0xDFF57D75DF5D: ("relais", False, None),
    0x7F7D5DD57DFD: ("mobile", True, None), 0xD5D7F77FD757: ("mobile", False, None),
    0x5D577F7757FF: ("direct", True, 1), 0xF7FDD5DDFD55: ("direct", False, 1),
    0x7DFFD5F55D5F: ("direct", True, 2), 0xD7557F5FF7F5: ("direct", False, 2),
}
DIBIT_SYMBOLE = {0b01: 3, 0b00: 1, 0b10: -1, 0b11: -3}
SYMBOLE_DIBIT = {v: k for k, v in DIBIT_SYMBOLE.items()}


def symboles_de(valeur: int, n_bits: int) -> list[int]:
    return [DIBIT_SYMBOLE[(valeur >> (n_bits - 2 - 2 * i)) & 3] for i in range(n_bits // 2)]


MOTIFS = [(np.array(symboles_de(v, 48), dtype=np.float32) / 3, info) for v, info in SYNCS.items()]

# Golay (20,8) du type de slot et QR (16,7) de l'EMB : codes linéaires, un mot par vecteur de base.
_GOLAY = (0xB08E, 0xE093, 0x70A9, 0x60DC, 0x7036, 0xD06C, 0x90D9, 0xA03D)
_QR = (0x0273, 0x04E5, 0x09C9, 0x11E2, 0x21B7, 0x411E, 0x804F)


def _combine(base, v: int) -> int:
    x = 0
    for i, b in enumerate(base):
        if v >> i & 1:
            x ^= b
    return x


def golay_mot(d: int) -> int:
    c = _combine(_GOLAY, d)
    return d << 12 | (c & 0xFF) << 4 | (c >> 12) & 0xF


def qr_mot(v: int) -> int:
    return _combine(_QR, v)


GOLAY_MOTS = np.array([golay_mot(d) for d in range(256)], dtype=np.int64)
QR_MOTS = np.array([qr_mot(v) for v in range(128)], dtype=np.int64)
_POP = np.array([bin(i).count("1") for i in range(1 << 16)], dtype=np.int8)


def _popcount(x: np.ndarray) -> np.ndarray:
    return _POP[x & 0xFFFF] + _POP[(x >> 16) & 0xFFFF]


def golay_decoder(mot: int) -> int | None:
    d = _popcount(GOLAY_MOTS ^ mot)
    i = int(np.argmin(d))
    return i if d[i] <= 3 else None


def qr_decoder(mot: int) -> int | None:
    d = _popcount(QR_MOTS ^ mot)
    i = int(np.argmin(d))
    return i if d[i] <= 2 else None


# Hamming : bits de parité = sommes de bits de données (ETSI TS 102 361-1, annexe B).
H15 = ((0, 1, 2, 3, 5, 7, 8), (1, 2, 3, 4, 6, 8, 9), (2, 3, 4, 5, 7, 9, 10), (0, 1, 2, 4, 6, 7, 10))
H13 = ((0, 1, 3, 5, 6), (0, 1, 2, 4, 6, 7), (0, 1, 2, 3, 5, 7, 8), (0, 2, 4, 5, 8))
H16 = H15 + ((0, 2, 5, 6, 8, 9, 10),)


def _syndromes(eqs, k: int) -> dict[int, int]:
    """Syndrome → position du bit à corriger (une erreur)."""
    t = {}
    for p in range(k):
        t[sum(1 << j for j, e in enumerate(eqs) if p in e)] = p
    for j in range(len(eqs)):
        t[1 << j] = k + j
    return t


_S15, _S13, _S16 = _syndromes(H15, 11), _syndromes(H13, 9), _syndromes(H16, 11)


def hamming_parite(d, eqs) -> list[int]:
    return [sum(d[i] for i in e) & 1 for e in eqs]


def hamming_corriger(d: list[int], eqs, table) -> int:
    """Corrige une erreur en place. 0 : juste, 1 : corrigé, -1 : incorrigible."""
    k = len(d) - len(eqs)
    s = 0
    for j, e in enumerate(eqs):
        if (sum(d[i] for i in e) + d[k + j]) & 1:
            s |= 1 << j
    if not s:
        return 0
    p = table.get(s)
    if p is None:
        return -1
    d[p] ^= 1
    return 1


def bptc_decoder(brut: list[int], corriger: bool = True) -> bytes:
    """BPTC (196,96) : 196 bits reçus → 12 octets."""
    m = [brut[(a * 181) % 196] for a in range(196)]
    if corriger:
        for _ in range(5):
            fait = False
            for r in range(9):
                ligne = m[r * 15 + 1:r * 15 + 16]
                if hamming_corriger(ligne, H15, _S15) == 1:
                    m[r * 15 + 1:r * 15 + 16] = ligne
                    fait = True
            for c in range(15):
                col = [m[c + 1 + 15 * k] for k in range(13)]
                if hamming_corriger(col, H13, _S13) == 1:
                    for k in range(13):
                        m[c + 1 + 15 * k] = col[k]
                    fait = True
            if not fait:
                break
    bits = m[4:12] + [b for r in range(1, 9) for b in m[r * 15 + 1:r * 15 + 12]]
    return bits_octets(bits)


def bptc_coder(octets: bytes) -> list[int]:
    """BPTC (196,96) : 12 octets → 196 bits à émettre (pour les essais)."""
    bits = octets_bits(octets)
    m = [0] * 196
    pos = [*range(4, 12)] + [p for r in range(1, 9) for p in range(r * 15 + 1, r * 15 + 12)]
    for p, b in zip(pos, bits):
        m[p] = b
    for r in range(9):
        m[r * 15 + 12:r * 15 + 16] = hamming_parite(m[r * 15 + 1:r * 15 + 12], H15)
    for c in range(15):
        col = [m[c + 1 + 15 * k] for k in range(9)]
        for k, b in enumerate(hamming_parite(col, H13)):
            m[c + 1 + 15 * (9 + k)] = b
    brut = [0] * 196
    for a in range(196):
        brut[(a * 181) % 196] = m[a]
    return brut


def bits_octets(bits) -> bytes:
    return bytes(sum(b << (7 - i) for i, b in enumerate(bits[k:k + 8])) for k in range(0, len(bits) - 7, 8))


def octets_bits(o: bytes) -> list[int]:
    return [(x >> (7 - i)) & 1 for x in o for i in range(8)]


# Reed-Solomon (12,9) sur GF(256), polynôme 0x11D, racines α¹ à α³ : contrôle du LC complet.
_EXP = [0] * 512
_LOG = [0] * 256
_x = 1
for _i in range(255):
    _EXP[_i] = _x
    _LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, 512):
    _EXP[_i] = _EXP[_i - 255]


def _gmul(a: int, b: int) -> int:
    return 0 if a == 0 or b == 0 else _EXP[_LOG[a] + _LOG[b]]


def rs_parite(o: bytes) -> bytes:
    g = (64, 56, 14)                       # (x+α)(x+α²)(x+α³) = x³ + 14x² + 56x + 64
    r = [0, 0, 0]
    for x in o[:9]:
        f = x ^ r[0]
        r = [r[1] ^ _gmul(g[2], f), r[2] ^ _gmul(g[1], f), _gmul(g[0], f)]
    return bytes(r)


MASQUE_ENTETE, MASQUE_FIN = 0x96, 0x99


def lc_valide(o: bytes, masque: int) -> bool:
    return bytes(b ^ masque for b in o[9:12]) == rs_parite(o)


def crc_ccitt(o: bytes) -> int:
    c = 0
    for x in o:
        c ^= x << 8
        for _ in range(8):
            c = ((c << 1) ^ 0x1021) & 0xFFFF if c & 0x8000 else (c << 1) & 0xFFFF
    return c ^ 0xFFFF


def crc16_valide(o: bytes, masque: int) -> bool:
    return crc_ccitt(o[:10]) ^ masque == (o[10] << 8 | o[11])


def crc32_dmr(o: bytes) -> int:
    """CRC-32 des paquets de données : octets pris par paires inversées, résultat octets inversés."""
    c = 0
    for i in range(0, len(o) - 1, 2):
        for x in (o[i + 1], o[i]):
            for k in range(7, -1, -1):
                c = ((c << 1) ^ 0x04C11DB7) & 0xFFFFFFFF if ((c >> 31) ^ (x >> k)) & 1 else (c << 1) & 0xFFFFFFFF
    return int.from_bytes(c.to_bytes(4, "little"), "big")


def crc5(bits72) -> int:
    return sum(bits_octets(bits72)) % 31


# LC intégré : 72 bits + CRC 5 bits répartis dans une matrice 8 × 16 (Hamming (16,11) + parité).
_LC_POS = [*range(0, 11), *range(16, 27), *range(32, 42), *range(48, 58), *range(64, 74), *range(80, 90),
           *range(96, 106)]
_CRC_POS = (42, 58, 74, 90, 106)


def _lc_ordre() -> list[int]:
    o, b = [], 0
    for _ in range(128):
        o.append(b)
        b += 16
        if b > 127:
            b -= 127
    return o


LC_ORDRE = _lc_ordre()          # bit transmis a ← case LC_ORDRE[a] de la matrice


def lc_integre_decoder(brut: list[int]) -> bytes | None:
    m = [0] * 128
    for a, b in enumerate(brut):
        m[LC_ORDRE[a]] = b
    for r in range(7):
        ligne = m[r * 16:r * 16 + 16]
        if hamming_corriger(ligne, H16, _S16) < 0:
            return None
        m[r * 16:r * 16 + 16] = ligne
    if any(sum(m[c + 16 * r] for r in range(8)) & 1 for c in range(16)):
        return None
    bits = [m[p] for p in _LC_POS]
    crc = sum(m[p] << (4 - i) for i, p in enumerate(_CRC_POS))
    return bits_octets(bits) if crc5(bits) == crc else None


def lc_integre_coder(lc: bytes) -> list[int]:
    bits = octets_bits(lc[:9])
    m = [0] * 128
    for p, b in zip(_LC_POS, bits):
        m[p] = b
    crc = crc5(bits)
    for i, p in enumerate(_CRC_POS):
        m[p] = crc >> (4 - i) & 1
    for r in range(7):
        m[r * 16 + 11:r * 16 + 16] = hamming_parite(m[r * 16:r * 16 + 11], H16)
    for c in range(16):
        m[112 + c] = sum(m[c + 16 * r] for r in range(7)) & 1
    return [m[LC_ORDRE[a]] for a in range(128)]


# Treillis 3/4 (données) : 49 points de constellation, 8 états.
TREILLIS_ENTRELACEMENT = [k for d in range(0, 8, 2) for j in range(13 if d == 0 else 12)
                          for k in (8 * j + d, 8 * j + d + 1)]
TREILLIS_CODE = (0, 8, 4, 12, 2, 10, 6, 14, 4, 12, 2, 10, 6, 14, 0, 8, 1, 9, 5, 13, 3, 11, 7, 15, 5, 13, 3, 11,
                 7, 15, 1, 9, 3, 11, 7, 15, 1, 9, 5, 13, 7, 15, 3, 11, 5, 13, 1, 9, 2, 10, 6, 14, 0, 8, 4, 12,
                 6, 14, 0, 8, 4, 12, 2, 10)
# Ligne 5 (état 5) : les sources diffèrent (7,15,3,11,… ou 7,15,1,9,…). Le message est décodé avec
# la première, puis avec la seconde si son CRC-32 est faux.
TREILLIS_CODE_2 = TREILLIS_CODE[:40] + (7, 15, 1, 9, 5, 13, 3, 11) + TREILLIS_CODE[48:]
POINTS = ((1, -1), (-1, -1), (3, -3), (-3, -3), (-3, -1), (3, -1), (-1, -3), (1, -3), (-3, 3), (3, 3),
          (-1, 1), (1, 1), (1, 3), (-1, 3), (3, 1), (-3, 1))


def treillis_decoder(souples: list[float], code=TREILLIS_CODE) -> bytes | None:
    """98 symboles reçus (valeurs souples) → 18 octets, par Viterbi."""
    d = [0.0] * 98
    for i, v in enumerate(souples):
        d[TREILLIS_ENTRELACEMENT[i]] = v
    inf = float("inf")
    cout = [0.0] + [inf] * 7
    chemins: list[list[int]] = [[] for _ in range(8)]
    for j in range(49):
        r1, r2 = d[2 * j], d[2 * j + 1]
        nouveau, nchem = [inf] * 8, [None] * 8
        for s in range(8):
            if cout[s] == inf:
                continue
            for t in range(8 if j < 48 else 1):
                p1, p2 = POINTS[code[s * 8 + t]]
                c = cout[s] + (r1 - p1) ** 2 + (r2 - p2) ** 2
                if c < nouveau[t]:
                    nouveau[t], nchem[t] = c, s
        chemins = [chemins[nchem[t]] + [t] if nchem[t] is not None else [] for t in range(8)]
        cout = nouveau
    if cout[0] == inf or cout[0] / 98 > 1.0:
        return None
    bits = [(t >> k) & 1 for t in chemins[0][:48] for k in (2, 1, 0)]
    return bits_octets(bits)


def treillis_coder(octets: bytes, code=TREILLIS_CODE) -> list[int]:
    """18 octets → 98 symboles à émettre (pour les essais)."""
    bits = octets_bits(octets)
    tri = [bits[3 * i] << 2 | bits[3 * i + 1] << 1 | bits[3 * i + 2] for i in range(48)] + [0]
    pts, s = [], 0
    for t in tri:
        pts.append(code[s * 8 + t])
        s = t
    d = [v for p in pts for v in POINTS[p]]
    return [d[TREILLIS_ENTRELACEMENT[i]] for i in range(98)]


# ======================================================================= interprétation
FID = {0x00: "", 0x04: "Flyde", 0x08: "Hytera", 0x10: "Motorola", 0x58: "Tait", 0x68: "Hytera", 0x77: "Vertex"}
SAP = {0: "UDT", 2: "TCP comprimé", 3: "UDP comprimé", 4: "IP", 5: "ARP", 9: "propriétaire", 10: "données courtes"}
PORTS = {4001: "position (LRRP)", 4004: "XCMP", 4005: "ARS (enregistrement)", 4007: "SMS (TMS)",
         4008: "télémétrie", 5016: "SMS Hytera", 3002: "position Hytera"}


def lc_vers_appel(o: bytes) -> dict | None:
    flco, fid, so = o[0] & 0x3F, o[1], o[2]
    if flco not in (0, 3):
        return None
    return {"groupe": flco == 0, "dst": int.from_bytes(o[3:6], "big"), "src": int.from_bytes(o[6:9], "big"),
            "fid": fid, "chiffre": bool(so & 0x40), "urgence": bool(so & 0x80)}


def texte_utf16(o: bytes) -> str:
    """Plus long passage lisible en UTF-16 (LE ou BE) d'un paquet : le texte d'un SMS."""
    meilleur = ""
    for ordre in ("little", "big"):
        for depart in (0, 1):
            courant = ""
            for k in range(depart, len(o) - 1, 2):
                c = int.from_bytes(o[k:k + 2], ordre)
                if c >= 0x20 and c not in (0x7F, 0xFFFF) and not 0xD800 <= c <= 0xDFFF:
                    courant += chr(c)
                    if len(courant) > len(meilleur):
                        meilleur = courant
                else:
                    courant = ""
    # octets d'en-tête lus comme caractères devant un texte latin : on les écarte
    if any(" " <= c < "\u0180" for c in meilleur):
        while meilleur and not ("!" <= meilleur[0] < "\u0180" and meilleur[0] != "\xa0"):
            meilleur = meilleur[1:]
    meilleur = meilleur.strip()
    return meilleur if len(meilleur) >= 2 else ""


def paquet_vers_texte(sap: int, o: bytes) -> str:
    if sap == 4 and len(o) >= 28 and o[0] >> 4 == 4 and o[9] == 17:
        ihl = (o[0] & 15) * 4
        sport, dport = int.from_bytes(o[ihl:ihl + 2], "big"), int.from_bytes(o[ihl + 2:ihl + 4], "big")
        charge = o[ihl + 8:]
        nature = PORTS.get(dport) or PORTS.get(sport) or f"UDP {dport}"
        if dport in (4007, 5016) or sport in (4007, 5016):
            t = texte_utf16(charge)
            return f"SMS : « {t} »" if t else nature
        return nature
    t = texte_utf16(o)
    if len(t) >= 3:
        return f"texte : « {t} »"
    a = "".join(chr(c) if 32 <= c < 127 else "" for c in o)
    return f"texte : « {a} »" if len(a) >= max(4, len(o) // 2) else f"{len(o)} octets ({SAP.get(sap, sap)})"


# ======================================================================= démodulateur
def rrc(alpha: float, sps: int, portee: int) -> np.ndarray:
    t = np.arange(-portee * sps, portee * sps + 1) / sps
    h = np.empty_like(t)
    for i, x in enumerate(t):
        if abs(x) < 1e-9:
            h[i] = 1 - alpha + 4 * alpha / np.pi
        elif abs(abs(4 * alpha * x) - 1) < 1e-9:
            h[i] = alpha / np.sqrt(2) * ((1 + 2 / np.pi) * np.sin(np.pi / (4 * alpha))
                                         + (1 - 2 / np.pi) * np.cos(np.pi / (4 * alpha)))
        else:
            h[i] = (np.sin(np.pi * x * (1 - alpha)) + 4 * alpha * x * np.cos(np.pi * x * (1 + alpha))) \
                / (np.pi * x * (1 - (4 * alpha * x) ** 2))
    return (h / np.sum(h)).astype(np.float32)


class _Slot:
    def __init__(self) -> None:
        self.appel = None           # appel en cours : dict
        self.frag: list[int] = []   # fragments du LC intégré
        self.etape = 0
        self.alias = {}             # format, longueur, morceaux
        self.donnees = None         # en-tête de données en attente de ses blocs
        self.blocs: list[bytes] = []
        self.chiffre = False
        self.voix_debut = None      # première salve vocale vue sans appel connu


class DecodeurDMR:
    """Discriminateur FM à 48 kHz (en Hz) → évènements DMR (listes de dict)."""

    def __init__(self) -> None:
        self.h = rrc(0.2, SPS, 8)
        self.zi = np.zeros(len(self.h) - 1, dtype=np.float32)
        self.buf = np.zeros(0, dtype=np.float32)
        self.t0 = 0                 # index absolu de buf[0]
        self.cherche = 0            # prochain index absolu où chercher une synchro
        self.prevues: list[tuple] = []
        self.slots = {0: _Slot(), 1: _Slot(), 2: _Slot()}
        self.salves = 0             # salves DMR reconnues (compteur)
        self.cc = None
        self.source = None
        self.csbk = 0
        self.ev: list[dict] = []

    # ------------------------------------------------------------- flux
    def traiter(self, fm: np.ndarray) -> list[dict]:
        y, self.zi = signal.lfilter(self.h, 1.0, fm.astype(np.float32), zi=self.zi)
        self.buf = np.concatenate((self.buf, y.astype(np.float32)))
        self.ev = []
        fin = self.t0 + len(self.buf)
        avant, apres = 89 * SPS, 54 * SPS + 8
        debut = max(self.cherche, self.t0 + avant)
        if fin - apres > debut:
            self._chercher(debut, fin - apres)
        for p in sorted(self.prevues):
            if p[0] + apres <= fin:
                self.prevues.remove(p)
                if p[0] - avant >= self.t0:
                    self._salve_vocale(*p)
        for s, etat in self.slots.items():
            a = etat.appel
            if a and fin - a["dernier"] > TAUX:     # une seconde sans voix : fin d'appel
                self._fin(s, a["dernier"])
        garde = min([self.cherche - avant - 16] + [p[0] - avant - 16 for p in self.prevues])
        if garde > self.t0:
            self.buf = self.buf[garde - self.t0:]
            self.t0 = garde
        return self.ev

    def _chercher(self, a: int, b: int) -> None:
        y = self.buf
        i0, i1 = a - self.t0, b - self.t0
        n = i1 - i0
        idx = [i0 - (23 - k) * SPS for k in range(24)]
        somme = np.zeros(n, dtype=np.float64)
        carres = np.zeros(n, dtype=np.float64)
        for j in idx:
            v = y[j:j + n].astype(np.float64)
            somme += v
            carres += v * v
        moyenne = somme / 24
        energie = np.maximum(carres - 24 * moyenne ** 2, 1e-9)
        meilleur = np.zeros(n)
        quel = np.zeros(n, dtype=np.int8)
        for m, (motif, _) in enumerate(MOTIFS):
            s = np.zeros(n, dtype=np.float64)
            for k, j in enumerate(idx):
                s += motif[k] * y[j:j + n]
            c = (s - moyenne * motif.sum()) / np.sqrt(energie * (24 - motif.sum() ** 2 / 24))
            plus = c > meilleur          # pas de valeur absolue : voix et données sont des motifs inverses
            meilleur[plus] = c[plus]
            quel[plus] = m
        k = 0
        while k < n:
            if meilleur[k] < SEUIL:
                k += 1
                continue
            j = k + int(np.argmax(meilleur[k:k + 2 * SPS]))
            self._salve(a + j, quel[j])
            k = j + 120 * SPS
        self.cherche = max(b, a + k)

    # ------------------------------------------------------------- salves
    def _lire(self, n: int, premier: int, dernier: int, niveaux) -> np.ndarray:
        """Symboles premier..dernier (numérotés dans la salve) en unités de symbole."""
        milieu, pas = niveaux
        i = n - self.t0 + (np.arange(premier, dernier + 1) - 77) * SPS
        return (self.buf[i] - milieu) / pas

    @staticmethod
    def _bits(sym: np.ndarray) -> list[int]:
        bits = []
        for v in sym:
            d = SYMBOLE_DIBIT[int(min((3, 1, -1, -3), key=lambda s: abs(s - v)))]
            bits += [d >> 1, d & 1]
        return bits

    def _salve(self, n: int, m: int) -> None:
        motif, (source, voix, slot) = MOTIFS[m]
        v = self.buf[n - self.t0 + (np.arange(54, 78) - 77) * SPS]
        haut, bas = float(v[motif > 0].mean()), float(v[motif < 0].mean())
        if haut - bas < 1e-3:
            return
        niveaux = ((haut + bas) / 2, (haut - bas) / 6)
        self.salves += 1
        self.source = source
        if source == "relais":                            # CACH : bit TC du TACT → slot de la salve
            cach = self._bits(self._lire(n, -12, -1, niveaux))
            slot = cach[4] + 1
        slot = slot or 0
        if voix:
            self._voix(slot, n)
            for k in range(1, 6):
                self.prevues.append((n + k * SLOT, slot, k, niveaux))
            return
        sym = self._lire(n, 0, 131, niveaux)
        bits = self._bits(sym)
        st = golay_decoder(int("".join(map(str, bits[98:108] + bits[156:166])), 2))
        info = bits[0:98] + bits[166:264]
        types = [st & 15] if st is not None else []
        if st is not None:
            self.cc = st >> 4
        for dt in types + [t for t in (1, 2, 3, 6) if t not in types]:
            if self._donnee(slot, dt, info, np.concatenate((sym[0:49], sym[83:132])), st is not None and dt == types[0]):
                return

    def _bptc(self, info, controle) -> bytes | None:
        for corr in (True, False):
            o = bptc_decoder(info, corr)
            if controle(o):
                return o
        return None

    def _donnee(self, slot: int, dt: int, info, souples, sur: bool) -> bool:
        etat = self.slots[slot]
        if dt == 1:
            o = self._bptc(info, lambda o: lc_valide(o, MASQUE_ENTETE))
            if o is None:
                return False
            a = lc_vers_appel(o)
            if a:
                self._appel(slot, a, "en-tête")
            return True
        if dt == 2:
            o = self._bptc(info, lambda o: lc_valide(o, MASQUE_FIN))
            if o is None:
                return False
            a = lc_vers_appel(o)
            if etat.appel:
                self._fin(slot, etat.appel["dernier"])
            elif a:
                self._appel(slot, a, "fin")
                self._fin(slot, None)
            return True
        if dt == 3:
            o = self._bptc(info, lambda o: crc16_valide(o, 0xA5A5))
            if o is None:
                return False
            self.csbk += 1
            op, fid = o[0] & 0x3F, o[1]
            ev = {"type": "csbk", "slot": slot, "cc": self.cc, "opcode": op, "fid": fid}
            if op == 0x3D:                     # préambule : annonce des données ou d'un appel
                ev.update(groupe=bool(o[2] & 0x40), dst=int.from_bytes(o[4:7], "big"),
                          src=int.from_bytes(o[7:10], "big"), blocs=o[3])
            self.ev.append(ev)
            return True
        if dt == 6:
            o = self._bptc(info, lambda o: crc16_valide(o, 0xCCCC))
            if o is None:
                return False
            dpf = o[0] & 15
            h = {"type": "donnees", "slot": slot, "groupe": bool(o[0] & 0x80), "dpf": dpf, "sap": o[1] >> 4,
                 "dst": int.from_bytes(o[2:5], "big"), "src": int.from_bytes(o[5:8], "big")}
            if dpf in (2, 3):
                h["blocs"] = o[8] & 0x7F
                h["bourrage"] = (o[0] >> 4 & 1) << 4 | o[1] & 15
                h["confirme"] = dpf == 3
            elif dpf in (13, 14):
                h["blocs"] = (o[0] >> 4 & 3) << 4 | o[1] & 15
                h["bourrage"] = 0
                h["confirme"] = False
            else:
                h["blocs"] = 0
            self.ev.append(dict(h))
            etat.donnees, etat.blocs = (h if h["blocs"] else None), []
            return True
        if dt in (7, 8) and sur and etat.donnees:
            etat.blocs.append((dt, info, [float(v) for v in souples]))
            if len(etat.blocs) >= etat.donnees["blocs"]:
                self._paquet(slot, etat.donnees, etat.blocs)
                etat.donnees = None
            return True
        if dt == 0 and sur:
            etat.chiffre = True
            return True
        return sur

    def _paquet(self, slot: int, h: dict, blocs: list) -> None:
        o, juste = b"", False
        for code in (TREILLIS_CODE, TREILLIS_CODE_2):
            morceaux = [bptc_decoder(info) if dt == 7 else treillis_decoder(s, code) for dt, info, s in blocs]
            if any(m is None for m in morceaux):
                continue
            essai = b"".join(m[2:] if h["confirme"] else m for m in morceaux)
            if len(essai) < 4:
                continue
            ok = crc32_dmr(essai[:-4]) == int.from_bytes(essai[-4:], "big")
            if ok or not o:
                o, juste = essai, ok
            if ok or all(dt == 7 for dt, _, _ in blocs):
                break
        if len(o) < 4:
            return
        utile = o[:max(0, len(o) - 4 - h.get("bourrage", 0))]
        self.ev.append({"type": "sms", "slot": slot, "groupe": h["groupe"], "dst": h["dst"], "src": h["src"],
                        "texte": paquet_vers_texte(h["sap"], utile), "crc": juste})

    def _salve_vocale(self, n: int, slot: int, k: int, niveaux) -> None:
        bits = self._bits(self._lire(n, 54, 77, niveaux))
        emb = qr_decoder(int("".join(map(str, bits[0:8] + bits[40:48])), 2))
        if emb is None:
            return
        etat = self.slots[slot]
        self._voix(slot, n)
        lcss = emb & 3
        frag = bits[8:40]
        if lcss == 1:
            etat.frag, etat.etape = frag, 1
        elif lcss == 3 and etat.etape in (1, 2):
            etat.frag += frag
            etat.etape += 1
        elif lcss == 2 and etat.etape == 3:
            lc = lc_integre_decoder(etat.frag + frag)
            etat.etape = 0
            if lc:
                self._lc_integre(slot, lc)
        else:
            etat.etape = 0

    def _lc_integre(self, slot: int, o: bytes) -> None:
        flco, fid = o[0] & 0x3F, o[1]
        a = lc_vers_appel(o)
        if a:
            self._appel(slot, a, "en cours")
            return
        if 4 <= flco <= 7 and fid in (0, 0x68):
            etat = self.slots[slot]
            bits = octets_bits(o)
            if flco == 4:
                fmt, lg = bits[16] << 1 | bits[17], int("".join(map(str, bits[18:23])), 2)
                etat.alias = {"format": fmt, "longueur": lg, 0: bits[23:72] if fmt == 0 else bits[24:72]}
            elif etat.alias:
                etat.alias[flco - 4] = bits[16:72]
            self._alias(slot)

    def _alias(self, slot: int) -> None:
        etat = self.slots[slot]
        al = etat.alias
        bits = []
        for k in range(4):
            if k not in al:
                break
            bits += al[k]
        taille = (7, 8, 8, 16)[al["format"]]
        n = min(al["longueur"], len(bits) // taille)
        car = [int("".join(map(str, bits[i * taille:(i + 1) * taille])), 2) for i in range(n)]
        texte = "".join(chr(c) for c in car if c >= 0x20 and c != 0x7F).strip()
        if texte and etat.appel and etat.appel.get("alias") != texte:
            etat.appel["alias"] = texte
            self.ev.append({"type": "alias", "slot": slot, "texte": texte, **{k: etat.appel[k] for k in ("dst", "src")}})

    # ------------------------------------------------------------- appels
    def _voix(self, slot: int, n: int) -> None:
        etat = self.slots[slot]
        if etat.appel:
            etat.appel["dernier"] = n
        elif etat.voix_debut is None:
            etat.voix_debut = n

    def _appel(self, slot: int, a: dict, origine: str) -> None:
        etat = self.slots[slot]
        en_cours = etat.appel
        if en_cours and (en_cours["dst"], en_cours["src"], en_cours["groupe"]) == (a["dst"], a["src"], a["groupe"]):
            en_cours["dernier"] = self.t0 + len(self.buf)
            return
        if en_cours:
            self._fin(slot, en_cours["dernier"])
        maintenant = self.t0 + len(self.buf)
        debut = etat.voix_debut if origine == "en cours" and etat.voix_debut is not None else maintenant
        a.update(debut=debut, dernier=maintenant, cc=self.cc, chiffre=a["chiffre"] or etat.chiffre)
        etat.appel, etat.chiffre, etat.voix_debut = a, False, None
        self.ev.append({"type": "appel", "slot": slot, "origine": origine, **a})
        if etat.alias:                        # alias reçu avant que l'appel soit connu
            self._alias(slot)

    def _fin(self, slot: int, dernier) -> None:
        etat = self.slots[slot]
        a = etat.appel
        etat.appel, etat.etape, etat.alias, etat.voix_debut = None, 0, {}, None
        if not a:
            return
        duree = max(0.0, ((dernier or a["dernier"]) - a["debut"]) / TAUX)
        self.ev.append({"type": "fin", "slot": slot, "duree": duree, **{k: a[k] for k in ("dst", "src", "groupe")},
                        "alias": a.get("alias", "")})


# ======================================================================= textes
def nom_slot(slot: int) -> str:
    return f"TS{slot}" if slot else "—"


def texte_appel(a: dict) -> str:
    vers = f"TG {a['dst']}" if a["groupe"] else f"privé {a['dst']}"
    morceaux = [f"{vers} ← {a['src']}"]
    if a.get("alias"):
        morceaux.append(a["alias"])
    if a.get("chiffre"):
        morceaux.append("chiffré")
    if a.get("urgence"):
        morceaux.append("URGENCE")
    if FID.get(a.get("fid", 0)):
        morceaux.append(FID[a["fid"]])
    return " · ".join(morceaux)


# ======================================================================= module
class DecodeurDMRModule(Module):
    nom = "Décodeur DMR"
    description = ("Appels DMR (relais et direct) : slot, color code, groupe ou appel privé, ID et alias de "
                   "l'émetteur, SMS. Sans logiciel externe ; la voix n'est pas décodée.")

    def demarrer(self) -> None:
        self.frequence = self.ctx.frequence()
        self.blocs: queue.Queue = queue.Queue(maxsize=16)
        self.evenements: queue.Queue = queue.Queue()
        self.etat = {"instant": 0.0, "couvert": False, "perdus": 0, "crete": None, "bruit": None,
                     "calcul": 0.0, "salves": 0, "cc": None, "source": None, "csbk": 0}
        self.lignes: dict[int, QListWidgetItem] = {}
        self.en_cours: dict[int, dict] = {}
        self.journal = None
        self._actif = True
        self._fil = threading.Thread(target=self._boucle, name="decodeur-dmr", daemon=True)
        self._fil.start()
        self.abonner("iq", self._sur_iq)
        self.abonner("vfo", self._sur_vfo)

    def _sur_vfo(self, f: float, _mode: str) -> None:
        self.frequence = f

    def _sur_iq(self, iq: np.ndarray, centre: float, taux: int) -> None:
        try:
            self.blocs.put_nowait((iq, centre, taux))
        except queue.Full:
            self.etat["perdus"] += 1

    def _boucle(self) -> None:
        bande = None
        while self._actif:
            try:
                tache = self.blocs.get(timeout=0.5)
            except queue.Empty:
                continue
            if tache is None:
                break
            iq, centre, taux = tache
            debut = time.perf_counter()
            f = self.frequence
            couvert = abs(f - centre) + 8_000 < 0.42 * taux
            self.etat.update(instant=time.monotonic(), couvert=couvert)
            if not couvert:
                bande = None
                continue
            if bande is None or bande[:2] != (centre, taux):
                d1, d2 = plan_decimation(taux, TAUX)
                inter = taux / d1
                nco = NCO(taux)
                dec1, dec2 = Decimateur(d1, taux, 0.42 * inter), Decimateur(d2, inter, 7_000)
                prec = 0j
                dmr = DecodeurDMR()
            nco.freq = f - centre               # changer de fréquence garde le décodeur
            bande = (centre, taux, f)
            canal = dec2.traiter(dec1.traiter(nco.melanger(iq)))
            if not len(canal):
                continue
            p = 10 * np.log10(float(np.mean(np.abs(canal) ** 2)) + 1e-20)
            e = self.etat
            e["bruit"] = p if e["bruit"] is None else min(p, e["bruit"] + 0.02)
            e["crete"] = p if e["crete"] is None else max(p, e["crete"] - 0.5)
            z = np.concatenate(([prec], canal))
            prec = canal[-1]
            fm = np.angle(z[1:] * np.conj(z[:-1])) * (TAUX / (2 * np.pi))
            for ev in dmr.traiter(fm.astype(np.float32)):
                self.evenements.put(ev)
            e.update(salves=dmr.salves, cc=dmr.cc, source=dmr.source, csbk=dmr.csbk)
            e["calcul"] = 0.9 * e["calcul"] + 0.1 * (time.perf_counter() - debut) / (len(iq) / taux)

    # ------------------------------------------------------------- interface
    def creer_widget(self) -> QWidget:
        w = QWidget()
        w.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        lay = QVBoxLayout(w)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(4)
        self.statut = QLabel("—")
        self.statut.setWordWrap(True)
        lay.addWidget(self.statut)
        self.ts = {s: QLabel() for s in (1, 2)}
        for s, l in self.ts.items():
            l.setTextFormat(Qt.TextFormat.PlainText)
            l.setWordWrap(True)
            lay.addWidget(l)
        self.diag = QLabel("")
        self.diag.setWordWrap(True)
        self.diag.setStyleSheet("color: gray")
        self.diag.setToolTip("Crête du canal au-dessus du bruit, salves DMR reconnues, color code, type "
                             "d'émetteur (relais, mobile, direct), part du temps réel utilisée par le module")
        lay.addWidget(self.diag)
        self.liste = QListWidget()
        self.liste.setWordWrap(True)
        self.liste.setMaximumHeight(9 * self.liste.fontMetrics().lineSpacing())
        lay.addWidget(self.liste)
        boutons = QHBoxLayout()
        effacer = QPushButton("Effacer")
        effacer.clicked.connect(self.liste.clear)
        copier = QPushButton("Copier")
        copier.clicked.connect(self.copier)
        self.c_csbk = QCheckBox("CSBK")
        self.c_csbk.setToolTip("Affiche aussi les blocs de signalisation (CSBK) : nombreux sur certains relais")
        self.c_journal = QCheckBox("Journal")
        self.c_journal.setToolTip("Enregistre les appels et les messages, horodatés, dans le dossier du module")
        self.c_journal.toggled.connect(self._basculer_journal)
        for b in (effacer, copier, self.c_csbk, self.c_journal):
            boutons.addWidget(b)
        lay.addLayout(boutons)
        self.minuterie = QTimer(w, interval=250, timeout=self._rafraichir)
        self.minuterie.start()
        self._rafraichir()
        return w

    def copier(self) -> None:
        QGuiApplication.clipboard().setText("\n".join(self.liste.item(i).text() for i in range(self.liste.count())))

    def _basculer_journal(self, actif: bool) -> None:
        if self.journal:
            self.journal.close()
            self.journal = None
        if actif:
            nom = time.strftime("dmr_%Y%m%d_%H%M%S.txt")
            self.journal = open(self.ctx.dossier_donnees("decodeur_dmr") / nom, "a", encoding="utf-8")

    def _ajouter(self, texte: str, slot: int | None = None) -> QListWidgetItem:
        item = QListWidgetItem(texte)
        self.liste.insertItem(0, item)
        while self.liste.count() > 300:
            self.liste.takeItem(self.liste.count() - 1)
        if self.journal and slot is None:
            self.journal.write(texte + "\n")
            self.journal.flush()
        return item

    def _evenement(self, ev: dict) -> None:
        h = time.strftime("%H:%M:%S")
        s = ev.get("slot", 0)
        t = ev["type"]
        if t == "appel":
            self.en_cours[s] = dict(ev, heure=h)
            cc = f" · CC {ev['cc']}" if ev.get("cc") is not None else ""
            self.lignes[s] = self._ajouter(f"{h}  {nom_slot(s)}{cc}  {texte_appel(ev)}  …", slot=s)
        elif t == "alias" and s in self.en_cours:
            self.en_cours[s]["alias"] = ev["texte"]
            self._maj_ligne(s, "  …")
        elif t == "fin":
            if s in self.en_cours:
                self.en_cours[s]["alias"] = ev["alias"] or self.en_cours[s].get("alias", "")
                ligne = self._maj_ligne(s, f"  {ev['duree']:.1f} s".replace(".", ","))
                if self.journal and ligne:
                    self.journal.write(ligne + "\n")
                    self.journal.flush()
                del self.en_cours[s]
                self.lignes.pop(s, None)
        elif t == "donnees":
            vers = "groupe" if ev["groupe"] else "privé"
            nat = SAP.get(ev["sap"], f"SAP {ev['sap']}")
            if ev.get("blocs") == 0 or self.c_csbk.isChecked():
                self._ajouter(f"{h}  {nom_slot(s)}  données {nat} {ev['src']} → {vers} {ev['dst']}")
        elif t == "sms":
            vers = "TG" if ev["groupe"] else "privé"
            crc = "" if ev["crc"] else "  (CRC faux)"
            self._ajouter(f"{h}  {nom_slot(s)}  {ev['src']} → {vers} {ev['dst']} · {ev['texte']}{crc}")
        elif t == "csbk" and self.c_csbk.isChecked():
            fid = FID.get(ev["fid"], f"FID {ev['fid']:02X}")
            if ev["opcode"] == 0x3D:
                self._ajouter(f"{h}  {nom_slot(s)}  préambule {ev['src']} → {'TG' if ev['groupe'] else 'privé'} "
                              f"{ev['dst']}")
            else:
                self._ajouter(f"{h}  {nom_slot(s)}  CSBK {ev['opcode']:02X} {fid}".rstrip())

    def _maj_ligne(self, s: int, fin: str) -> str:
        a, item = self.en_cours.get(s), self.lignes.get(s)
        if not a or item is None:
            return ""
        cc = f" · CC {a['cc']}" if a.get("cc") is not None else ""
        texte = f"{a['heure']}  {nom_slot(s)}{cc}  {texte_appel(a)}{fin}"
        try:
            item.setText(texte)
        except RuntimeError:            # ligne effacée entre-temps
            pass
        return texte

    def _rafraichir(self) -> None:
        while True:
            try:
                self._evenement(self.evenements.get_nowait())
            except queue.Empty:
                break
        e = self.etat
        if time.monotonic() - e["instant"] > 1.5:
            self.statut.setText("En attente des échantillons du récepteur.")
        elif not e["couvert"]:
            self.statut.setText("Le VFO est trop près du bord de la bande reçue.")
        elif not e["salves"]:
            self.statut.setText("Accordez le VFO sur une fréquence DMR (relais ou direct). Le mode n'a pas "
                                "d'importance : le module démodule lui-même.")
        else:
            self.statut.setText("Signal DMR reçu." if self.en_cours else "Signal DMR reçu, pas d'appel en cours.")
        for s, l in self.ts.items():
            a = self.en_cours.get(s)
            l.setText(f"{nom_slot(s)} : {texte_appel(a)}" if a else f"{nom_slot(s)} : —")
        if e["crete"] is not None:
            cc = f" · CC {e['cc']}" if e["cc"] is not None else ""
            src = f" · {e['source']}" if e["source"] else ""
            perdus = f" · {e['perdus']} blocs perdus" if e["perdus"] else ""
            self.diag.setText(f"crête {e['crete'] - e['bruit']:.0f} dB · {e['salves']} salves{cc}{src}"
                              f"{perdus} · calcul {100 * e['calcul']:.0f} %")

    def arreter(self) -> None:
        self._actif = False
        try:
            self.blocs.put_nowait(None)
        except queue.Full:
            pass
        if hasattr(self, "minuterie"):
            self.minuterie.stop()
        if self.journal:
            self.journal.close()
            self.journal = None

# SPDX-License-Identifier: MIT
"""Décodeur ACARS pour SDR Modulaire : messages des avions et des stations sol en VHF AM (131 MHz).

Choix d'une liste de fréquences préenregistrées (Europe : 131,525 · 131,725 · 131,825 MHz, Amérique du
Nord, Japon, Asie…) ou de la fréquence du VFO. Toutes les fréquences de la liste qui tombent dans la bande
reçue sont décodées en même temps, quel que soit le mode du VFO : le module extrait ses canaux des IQ.

Chaîne par canal : étage commun (NCO au milieu des fréquences, décimation) → NCO du canal → 48 kHz →
12 kHz complexe → enveloppe AM → démodulateur MSK cohérent (2 400 bauds, 1 200 / 2 400 Hz autour de
1 800 Hz) : oscillateur et horloge bit liés, filtre adapté en demi-sinusoïde sur deux bits, symboles lus
alternativement sur I et Q, boucle à verrouillage de phase (proportionnelle et intégrale) accrochée sur
le préambule de « 1 ». Trame : synchro « + * SYN SYN SOH » cherchée bit à bit (deux erreurs tolérées,
polarité inversée reconnue), caractères 7 bits + parité impaire envoyés poids faible d'abord, fin ETX ou
ETB, CRC-16 (CCITT réfléchi) ; correction d'une erreur par caractère de parité fausse (trois au plus) ou
de deux bits dans un même caractère, par syndromes. Même algorithme dans SDR Web (web/acars.js).
"""

from __future__ import annotations

import math
import queue
import threading
import time

import numpy as np
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (QCheckBox, QComboBox, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
                               QPushButton, QSizePolicy, QVBoxLayout, QWidget)

from sdrmod.dsp import NCO, Decimateur
from sdrmod.modules import Module

TAUX_ACARS = 12_000
SPB = TAUX_ACARS // 2400                      # 5 échantillons par bit
LONG_FILTRE = 2 * SPB                         # filtre adapté : demi-sinusoïde sur deux bits
KI, KP = 71e-7 / LONG_FILTRE, 60e-3 / LONG_FILTRE

# Listes préenregistrées : (nom, fréquences en Hz). La première fréquence est celle où « Accorder » met le VFO.
LISTES = [
    ("Europe : 131,525 · 131,725 · 131,825 MHz", [131_725_000, 131_525_000, 131_825_000]),
    ("Europe principale : 131,725 MHz", [131_725_000]),
    ("Europe : 131,525 MHz", [131_525_000]),
    ("Europe : 131,825 MHz", [131_825_000]),
    ("Amérique du Nord : 129,125 à 131,550 MHz", [131_550_000, 131_125_000, 130_450_000, 130_025_000, 129_125_000]),
    ("Amérique du Nord principale : 131,550 MHz", [131_550_000]),
    ("Japon : 131,450 MHz", [131_450_000]),
    ("Asie et Océanie : 131,550 MHz", [131_550_000]),
    ("Fréquence du VFO", []),
]

LABELS = {
    "_d": "accusé, pas d'information", "Q0": "essai de liaison", "SQ": "annonce de la station sol",
    "SA": "état des liaisons (VHF, satellite, HF)", "H1": "message de l'équipage ou d'un système de bord",
    "5Z": "message de compagnie", "QA": "départ du parking (OUT) et carburant", "QB": "décollage (OFF)",
    "QC": "atterrissage (ON)", "QD": "arrivée au parking (IN) et carburant", "QE": "départ du parking et destination",
    "QF": "décollage et destination", "QP": "départ du parking (OUT)", "QQ": "décollage (OFF)",
    "QR": "atterrissage (ON)", "QS": "arrivée au parking (IN)", "AA": "CPDLC (vers l'avion)",
    "BA": "CPDLC (depuis l'avion)", "A6": "ADS-C (demande)", "B6": "ADS-C (rapport de position)",
}

# ======================================================================= trame
SYNCHRO = bytes([ord("+") | 0x80, ord("*"), 0x16, 0x16, 0x01])
ETX, ETB, DEL = 0x83, 0x97, 0x7F


def _bits_temps(octets: bytes) -> list[int]:
    return [(o >> i) & 1 for o in octets for i in range(8)]   # poids faible d'abord


MOTIF = int("".join(map(str, _bits_temps(SYNCHRO))), 2)      # premier bit reçu = bit de poids fort
N_MOTIF = 8 * len(SYNCHRO)
MASQUE = (1 << N_MOTIF) - 1
_TABLE_CRC = []
for _i in range(256):
    _c = _i
    for _ in range(8):
        _c = (_c >> 1) ^ 0x8408 if _c & 1 else _c >> 1
    _TABLE_CRC.append(_c)


def crc16(octets, crc: int = 0) -> int:
    for o in octets:
        crc = (crc >> 8) ^ _TABLE_CRC[(crc ^ o) & 0xFF]
    return crc


def parite_ok(o: int) -> bool:
    return bin(o).count("1") & 1 == 1


def avec_parite(c: int) -> int:
    return c if parite_ok(c) else c | 0x80


def _syndrome(distance: int) -> int:
    """CRC d'une erreur d'un bit placée `distance` bits avant la fin (CRC compris) : le CRC est linéaire."""
    o = bytearray((distance + 8) // 8)
    o[0] = 1 << (distance % 8)
    return crc16(o)


def corriger(octets: bytearray) -> int | None:
    """Corrige en place. Retourne le nombre de corrections (0 si juste) ou None si impossible.
    octets : texte (mode…ETX) suivi des deux octets de CRC."""
    crc = crc16(octets)
    n = len(octets)
    fausses = [i for i in range(n - 2) if not parite_ok(octets[i])]
    syn = lambda i, b: _syndrome(8 * (n - 1 - i) + b)       # bit b de l'octet i
    if not fausses:
        if crc == 0:
            return 0
        for i in range(n):                                    # deux bits faux dans un même octet
            for a in range(8):
                for b in range(a + 1, 8):
                    if crc ^ syn(i, a) ^ syn(i, b) == 0:
                        octets[i] ^= (1 << a) | (1 << b)
                        return 2
        return None
    if len(fausses) > 3:
        return None

    def essai(k: int, reste: int) -> bool:
        if k == len(fausses):
            return reste == 0
        i = fausses[k]
        for b in range(8):
            if essai(k + 1, reste ^ syn(i, b)):
                octets[i] ^= 1 << b
                return True
        return False
    return len(fausses) if essai(0, crc) else None


class Trameur:
    """Bits démodulés → messages ACARS (dict)."""

    def __init__(self) -> None:
        self.reg = 0
        self.dans = False
        self.octets = bytearray()
        self.courant = 0
        self.nbits = 0
        self.inverse = False      # demandé au démodulateur quand la synchro arrive inversée
        self.messages: list[dict] = []
        self.rejets = 0

    def bit(self, b: int) -> None:
        if not self.dans:
            self.reg = ((self.reg << 1) | b) & MASQUE
            e = bin(self.reg ^ MOTIF).count("1")
            if e <= 2 or e >= N_MOTIF - 2:
                if e >= N_MOTIF - 2:
                    self.inverse = not self.inverse
                self.dans, self.octets, self.courant, self.nbits = True, bytearray(), 0, 0
            return
        self.courant |= b << self.nbits
        self.nbits += 1
        if self.nbits < 8:
            return
        o, self.courant, self.nbits = self.courant, 0, 0
        self.octets.append(o)
        t = self.octets
        fin = None
        for k in range(12, len(t)):
            if t[k] & 0x7F in (0x03, 0x17):
                fin = k
                break
        if fin is not None and len(t) >= fin + 3:
            self._fin(fin)
        elif len(t) > 240:
            self.rejets += 1
            self.dans = False

    def _fin(self, fin: int) -> None:
        self.dans = False
        t = bytearray(self.octets[:fin + 3])
        t[fin] = ETX if t[fin] & 0x7F == 0x03 else ETB
        n = corriger(t)
        if n is None:
            self.rejets += 1
            return
        self.messages.append(champs(bytes(c & 0x7F for c in t[:fin + 1]), n))


def champs(t: bytes, corrections: int) -> dict:
    """Texte sans parité, de mode à ETX/ETB inclus → champs du message."""
    lab = t[9:11].decode("latin-1").replace("\x7f", "d")
    bid = chr(t[11])
    texte = t[13:-1].decode("latin-1") if t[12] == 0x02 else ""
    texte = "".join(c if c >= " " or c in "\r\n" else "·" for c in texte).replace("\r\n", "\n").replace("\r", "\n")
    m = {"mode": chr(t[0]), "immat": t[1:8].decode("latin-1").lstrip(".").strip(), "ack": t[8],
         "label": lab, "bloc": bid, "suite": t[-1] == 0x17, "corrections": corrections,
         "descendant": bid.isdigit(), "numero": "", "vol": "", "texte": texte}
    if m["descendant"] and len(texte) >= 10:               # avion → sol : numéro de message et vol
        m["numero"], m["vol"], m["texte"] = texte[:4], texte[4:10].strip(), texte[10:]
    return m


def coder_message(mode: str, immat: str, label: str, bloc: str, texte: str, ack: int = 0x15,
                  suite: bool = False) -> bytes:
    """Octets émis (préambule, synchro, message, CRC, DEL) : sert aux essais."""
    corps = bytes([ord(mode)]) + immat.rjust(7, ".").encode() + bytes([ack]) + label.replace("d", "\x7f").encode() \
        + bloc.encode() + (b"\x02" + texte.encode("latin-1") if texte else b"") + bytes([0x17 if suite else 0x03])
    corps = bytes(avec_parite(c) for c in corps)
    crc = crc16(corps)
    return b"\xff" * 16 + SYNCHRO + corps + bytes([crc & 0xFF, crc >> 8, DEL, 0xFF])


# ======================================================================= démodulateur MSK
class DemodMSK:
    """Enveloppe AM à 12 kHz → bits (démodulation cohérente, comme une OQPSK à impulsions en demi-sinus)."""

    def __init__(self, trameur: Trameur) -> None:
        self.t = trameur
        self.phase = 0.0
        self.horloge = 0.0
        self.df = 0.0
        self.dphi = 0.0
        self.buf = np.zeros(LONG_FILTRE, dtype=np.complex128)
        self.i = 0
        self.k = 0
        self.verrou = 0               # bits restants où la boucle reste active après un préambule
        self.suite = 0                # bits identiques consécutifs
        self.dernier = -1
        self.dc = 0.0

    def traiter(self, am: np.ndarray) -> None:
        w0 = 2 * math.pi * 1800 / TAUX_ACARS
        buf, n_f = self.buf, LONG_FILTRE
        p, clk, i = self.phase, self.horloge, self.i
        dc = self.dc
        for x in am.tolist():
            s = w0 + self.dphi
            clk += s
            if clk > 1.5 * math.pi:
                clk -= 1.5 * math.pi
                f = clk / s
                v = 0j
                for j in range(n_f):
                    v += math.sin(math.pi * (j + f) / n_f) * buf[(i + j) % n_f]
                v /= abs(v) + 1e-12
                if self.k & 1:
                    vo = v.imag
                    err = -v.real if vo >= 0 else v.real
                else:
                    vo = v.real
                    err = v.imag if vo >= 0 else -v.imag
                b = (vo > 0) ^ bool(self.k & 2) ^ self.t.inverse
                self.k += 1
                self._bit(int(b))
                if self.verrou > 0 or self.t.dans:
                    self.df += KI * err
                    self.dphi = self.df + KP * err
                else:
                    self.df = self.dphi = 0.0
            p += s
            if p >= 2 * math.pi:
                p -= 2 * math.pi
            dc += 0.002 * (x - dc)
            buf[i] = (x - dc) * complex(math.cos(p), -math.sin(p))
            i = (i + 1) % n_f
        self.phase, self.horloge, self.i, self.dc = p, clk, i, dc

    def _bit(self, b: int) -> None:
        self.suite = self.suite + 1 if b == self.dernier else 1
        self.dernier = b
        if self.suite >= 16:          # préambule (suite de « 1 », ou de « 0 » si la polarité est inversée)
            self.verrou = 80
            if b == 0 and self.suite == 16 and not self.t.dans:
                self.t.inverse = not self.t.inverse
        elif self.verrou > 0:
            self.verrou -= 1
        self.t.bit(b)


class NCORapide(NCO):
    """NCO dont l'oscillateur d'un bloc est calculé une fois pour toutes (blocs de taille constante)."""

    _cle = None

    def melanger(self, x: np.ndarray) -> np.ndarray:
        n = len(x)
        w = -2.0 * np.pi * self.freq / self.fs
        if self._cle != (n, w):
            self._cle = (n, w)
            self._base = np.exp(1j * w * np.arange(n)).astype(np.complex64)
        y = x * self._base
        y *= np.complex64(np.exp(1j * self._phase))
        self._phase = (self._phase + w * n) % (2.0 * np.pi)
        return y


class CanalACARS:
    """Un canal : IQ de l'étage commun → 12 kHz → enveloppe AM → MSK → messages."""

    def __init__(self, frequence: float, fs: float) -> None:
        self.frequence = frequence
        self.fs = fs
        self.nco = NCORapide(fs)
        d1 = int(round(fs / 48_000))
        self.dec1 = Decimateur(d1, fs, 0.42 * 48_000) if d1 > 1 else None
        self.dec2 = Decimateur(4, 48_000, 5_000)
        self.trameur = Trameur()
        self.demod = DemodMSK(self.trameur)
        self.niveau = None
        self.bruit = None
        self.messages = 0
        self.attente = None           # bloc précédent, pas encore démodulé
        self.actif = 0                # blocs à démoduler encore après la fin d'une porteuse

    def traiter(self, x: np.ndarray, centre: float) -> list[dict]:
        self.nco.freq = self.frequence - centre
        y = self.nco.melanger(x)
        if self.dec1 is not None:
            y = self.dec1.traiter(y)
        y = self.dec2.traiter(y)
        if not len(y):
            return []
        p = 10 * math.log10(float(np.mean(np.abs(y) ** 2)) + 1e-20)
        self.niveau = p if self.niveau is None else max(p, self.niveau - 0.3)
        self.bruit = p if self.bruit is None else min(p, self.bruit + 0.01)
        # Les messages ACARS sont des salves : on ne démodule que quand une porteuse dépasse le bruit
        # (plus le bloc d'avant, où commence le préambule, et un bloc après).
        env = np.abs(y).astype(np.float64)
        if p > self.bruit + 3:
            self.actif = 2
        if self.actif > 0:
            self.actif -= 1
            if self.attente is not None:
                self.demod.traiter(self.attente)
            self.demod.traiter(env)
            self.attente = None
        else:
            self.attente = env
        sortie = []
        for m in self.trameur.messages:
            self.messages += 1
            sortie.append(dict(m, frequence=self.frequence))
        self.trameur.messages = []
        return sortie


def etage_commun(taux: int, largeur: float) -> int:
    """Décimation de l'étage commun : la plus forte qui garde `largeur` Hz et un multiple de 48 kHz."""
    n = taux // 48_000
    d = 1
    for k in range(1, n + 1):
        if n % k == 0 and taux / k >= 1.25 * largeur:
            d = k
    return d


class MoteurACARS:
    """IQ de la clé → messages de toutes les fréquences demandées qui sont dans la bande."""

    def __init__(self, frequences: list[float], centre: float, taux: int) -> None:
        self.centre_bande, self.taux = centre, taux
        lim = 0.42 * taux - 15_000
        self.dedans = [f for f in frequences if abs(f - centre) < lim]
        self.dehors = [f for f in frequences if abs(f - centre) >= lim]
        self.canaux: list[CanalACARS] = []
        if not self.dedans:
            return
        bas, haut = min(self.dedans), max(self.dedans)
        self.milieu = (bas + haut) / 2
        d = etage_commun(taux, haut - bas + 60_000)
        self.fs = taux / d
        self.nco = NCORapide(taux)
        self.nco.freq = self.milieu - centre
        self.dec = Decimateur(d, taux, min(0.42 * self.fs, (haut - bas) / 2 + 30_000)) if d > 1 else None
        self.canaux = [CanalACARS(f, self.fs) for f in self.dedans]

    def traiter(self, iq: np.ndarray) -> list[dict]:
        if not self.canaux:
            return []
        y = self.nco.melanger(iq)
        if self.dec is not None:
            y = self.dec.traiter(y)
        sortie = []
        for c in self.canaux:
            sortie += c.traiter(y, self.milieu)
        return sortie


# ======================================================================= affichage
def mhz(f: float) -> str:
    return f"{f / 1e6:.3f}".replace(".", ",")


def texte_message(m: dict) -> str:
    sens = "↓" if m["descendant"] else "↑"
    morceaux = [f"{mhz(m['frequence'])} {sens}"]
    if m["immat"]:
        morceaux.append(m["immat"])
    if m["vol"]:
        morceaux.append(f"vol {m['vol']}")
    lab = LABELS.get(m["label"])
    morceaux.append(f"label {m['label']}" + (f" ({lab})" if lab else ""))
    if m["numero"]:
        morceaux.append(f"n° {m['numero']}")
    if m["suite"]:
        morceaux.append("suite à venir")
    if m["corrections"]:
        morceaux.append(f"{m['corrections']} bit(s) corrigé(s)")
    t = " · ".join(morceaux)
    corps = m["texte"].strip()
    return t + ("\n    " + corps.replace("\n", "\n    ") if corps else "")


# ======================================================================= module
class DecodeurACARSModule(Module):
    nom = "Décodeur ACARS"
    description = ("Messages ACARS des avions et des stations sol (VHF AM, 131 MHz) : immatriculation, vol, label, "
                   "texte. Fréquences préenregistrées (Europe, Amérique du Nord, Japon…) décodées ensemble.")

    def demarrer(self) -> None:
        self.choix = 0
        self.vfo = self.ctx.frequence()
        self.blocs: queue.Queue = queue.Queue(maxsize=16)
        self.evenements: queue.Queue = queue.Queue()
        self.etat = {"instant": 0.0, "canaux": [], "dehors": [], "perdus": 0, "calcul": 0.0}
        self.journal = None
        self.avions: dict[str, float] = {}
        self._actif = True
        self._fil = threading.Thread(target=self._boucle, name="decodeur-acars", daemon=True)
        self._fil.start()
        self.abonner("iq", self._sur_iq)
        self.abonner("vfo", self._sur_vfo)

    def frequences(self) -> list[float]:
        liste = LISTES[self.choix][1]
        return list(liste) if liste else [self.vfo]

    def _sur_vfo(self, f: float, _mode: str) -> None:
        self.vfo = f

    def _sur_iq(self, iq: np.ndarray, centre: float, taux: int) -> None:
        try:
            self.blocs.put_nowait((iq, centre, taux))
        except queue.Full:
            self.etat["perdus"] += 1

    def _boucle(self) -> None:
        moteur, cle = None, None
        while self._actif:
            try:
                tache = self.blocs.get(timeout=0.5)
            except queue.Empty:
                continue
            if tache is None:
                break
            iq, centre, taux = tache
            debut = time.perf_counter()
            voulu = (centre, taux, tuple(self.frequences()))
            if voulu != cle:
                cle = voulu
                moteur = MoteurACARS(list(voulu[2]), centre, taux)
            for m in moteur.traiter(iq):
                self.evenements.put(m)
            self.etat.update(instant=time.monotonic(), dehors=moteur.dehors,
                             canaux=[(c.frequence, (c.niveau or -99) - (c.bruit or -99), c.messages)
                                     for c in moteur.canaux])
            e = self.etat
            e["calcul"] = 0.9 * e["calcul"] + 0.1 * (time.perf_counter() - debut) / (len(iq) / taux)

    # ------------------------------------------------------------- interface
    def creer_widget(self) -> QWidget:
        w = QWidget()
        w.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        lay = QVBoxLayout(w)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(4)
        ligne = QHBoxLayout()
        self.c_liste = QComboBox()
        for nom, _ in LISTES:
            self.c_liste.addItem(nom)
        self.c_liste.setToolTip("Fréquences décodées : toutes celles de la liste qui tiennent dans la bande reçue")
        self.c_liste.currentIndexChanged.connect(lambda i: setattr(self, "choix", i))
        accorder = QPushButton("Accorder")
        accorder.setToolTip("Met la clé sur ces fréquences (VFO sur la première, en AM pour écouter)")
        accorder.clicked.connect(self.accorder)
        ligne.addWidget(self.c_liste, 1)
        ligne.addWidget(accorder)
        lay.addLayout(ligne)
        self.statut = QLabel("—")
        self.statut.setWordWrap(True)
        lay.addWidget(self.statut)
        self.diag = QLabel("")
        self.diag.setWordWrap(True)
        self.diag.setStyleSheet("color: gray")
        self.diag.setToolTip("Par fréquence : niveau du canal au-dessus du bruit et messages reçus ; avions entendus "
                             "dans les 10 dernières minutes ; part du temps réel utilisée par le module")
        lay.addWidget(self.diag)
        self.liste = QListWidget()
        self.liste.setWordWrap(True)
        self.liste.setMinimumHeight(10 * self.liste.fontMetrics().lineSpacing())
        lay.addWidget(self.liste)
        boutons = QHBoxLayout()
        effacer = QPushButton("Effacer")
        effacer.clicked.connect(self.liste.clear)
        copier = QPushButton("Copier")
        copier.clicked.connect(self.copier)
        self.c_vides = QCheckBox("Messages vides")
        self.c_vides.setToolTip("Affiche aussi les accusés et messages sans texte (label _d, essais de liaison…)")
        self.c_journal = QCheckBox("Journal")
        self.c_journal.setToolTip("Enregistre les messages, horodatés, dans le dossier du module")
        self.c_journal.toggled.connect(self._basculer_journal)
        for b in (effacer, copier, self.c_vides, self.c_journal):
            boutons.addWidget(b)
        lay.addLayout(boutons)
        self.minuterie = QTimer(w, interval=250, timeout=self._rafraichir)
        self.minuterie.start()
        self._rafraichir()
        return w

    def accorder(self) -> None:
        fs = LISTES[self.choix][1]
        if not fs:
            return
        cible, taux = fs[0], self.ctx.taux()
        self.ctx.regler_mode("AM")
        # La clé se recentre quand le VFO sort de la bande (centre = VFO − 10 % du taux) : en passant par une
        # fréquence hors bande, la bande reçue couvre ensuite la première fréquence et ses voisines.
        self.ctx.regler_frequence(cible + taux)
        self.ctx.regler_frequence(cible)

    def copier(self) -> None:
        QGuiApplication.clipboard().setText("\n".join(self.liste.item(i).text()
                                                       for i in reversed(range(self.liste.count()))))

    def _basculer_journal(self, actif: bool) -> None:
        if self.journal:
            self.journal.close()
            self.journal = None
        if actif:
            nom = time.strftime("acars_%Y%m%d_%H%M%S.txt")
            self.journal = open(self.ctx.dossier_donnees("decodeur_acars") / nom, "a", encoding="utf-8")

    def _message(self, m: dict) -> None:
        if m["immat"]:
            self.avions[m["immat"]] = time.monotonic()
        texte = f"{time.strftime('%H:%M:%S')}  {texte_message(m)}"
        if self.journal:
            self.journal.write(texte + "\n")
            self.journal.flush()
        if not m["texte"].strip() and not self.c_vides.isChecked():
            return
        self.liste.insertItem(0, QListWidgetItem(texte))
        while self.liste.count() > 500:
            self.liste.takeItem(self.liste.count() - 1)

    def _rafraichir(self) -> None:
        while True:
            try:
                self._message(self.evenements.get_nowait())
            except queue.Empty:
                break
        e = self.etat
        if time.monotonic() - e["instant"] > 1.5:
            self.statut.setText("En attente des échantillons du récepteur.")
        elif not e["canaux"]:
            self.statut.setText("Aucune de ces fréquences n'est dans la bande reçue : cliquez sur « Accorder ».")
        else:
            dehors = f" Hors de la bande : {', '.join(mhz(f) for f in e['dehors'])} MHz." if e["dehors"] else ""
            self.statut.setText(f"Décodage de {', '.join(mhz(c[0]) for c in e['canaux'])} MHz.{dehors}")
        maintenant = time.monotonic()
        avions = sum(maintenant - t < 600 for t in self.avions.values())
        if e["canaux"]:
            canaux = " · ".join(f"{mhz(f)} : {d:.0f} dB, {n} msg" for f, d, n in e["canaux"])
            perdus = f" · {e['perdus']} blocs perdus" if e["perdus"] else ""
            self.diag.setText(f"{canaux} · {avions} avion(s) · calcul {100 * e['calcul']:.0f} %{perdus}")

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
